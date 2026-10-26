"""Group ownership survives native leader exit, partial startup and failed cleanup."""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from rodex import process_receipts as receipts
from rodex.runtime import RodexRuntimeLauncher
from rodex_registry import RodexRuntimeId


@pytest.mark.parametrize("route", ["runtime", "gateway"])
@pytest.mark.parametrize(
    "scenario",
    [
        "resistant_child",
        "leader_first",
        "stopped",
        "partial_start",
        "partial_start_cleanup_failure",
        "failed_signal",
        "unresolved_exit",
        "retirement_failure",
        "concurrent_finalizers",
    ],
)
def test_native_owned_lifecycle(tmp_path, route, scenario):
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("process_lifecycle_fixture.py")),
            str(tmp_path),
            route,
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == dict(
        route=route, scenario=scenario, receipt_retired=True, sentinel_untouched=True
    )


def _receipt_lifecycle(tmp_path):
    store = receipts.RuntimeProcessReceipts(tmp_path)
    runtime_id = RodexRuntimeId(7)
    lifecycle = receipts.RuntimeProcessLifecycle(
        lambda kind, group: store.record(kind, runtime_id, "b" * 32, group),
        lambda kind, group: store.release(kind, runtime_id, "b" * 32, group),
    )
    return store, lifecycle, store._path("app-server", runtime_id)


def test_post_unlink_sync_failure_restores_receipt_and_retains_capability(tmp_path, monkeypatch):
    _store, lifecycle, path = _receipt_lifecycle(tmp_path)
    process = lifecycle.start("app-server", [sys.executable, "-c", "import time; time.sleep(20)"])
    owner = lifecycle._groups["app-server"]
    original = path.read_bytes()
    syncs = []

    def fail_sync(_path):
        syncs.append(path.exists())
        raise OSError("injected directory sync failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(receipts, "_fsync_directory", fail_sync)
            with pytest.raises(receipts.ProcessReceiptError, match="sync failure"):
                lifecycle.close()
        assert syncs == [False, True]
        assert path.read_bytes() == original
        assert owner.extinct and not owner.closed
        os.fstat(owner.descriptor)
        lifecycle.close()
        assert not path.exists() and owner.closed
        assert process.poll() is not None
    finally:
        lifecycle.close()


def test_receipt_publication_cannot_overwrite_and_retirement_checks_full_identity(tmp_path):
    store, lifecycle, path = _receipt_lifecycle(tmp_path)
    lifecycle.start("app-server", [sys.executable, "-c", "import time; time.sleep(20)"])
    owner = lifecycle._groups["app-server"]
    original = path.read_bytes()
    try:
        with pytest.raises(FileExistsError):
            store.record("app-server", RodexRuntimeId(7), "c" * 32, owner)
        assert path.read_bytes() == original
        with pytest.raises(receipts.ProcessReceiptError, match="before group extinction"):
            store.release("app-server", RodexRuntimeId(7), "b" * 32, owner)
        altered = json.loads(original)
        altered["start_time_ticks"] += 1
        path.write_text(json.dumps(altered))
        with pytest.raises(receipts.ProcessReceiptError, match="changed before release"):
            lifecycle.close()
        assert owner.extinct and not owner.closed
        os.fstat(owner.descriptor)
        assert json.loads(path.read_text()) == altered
    finally:
        path.write_bytes(original)
        lifecycle.close()
    assert owner.closed and not path.exists()


def test_failed_retirement_restore_cannot_turn_missing_file_into_success(tmp_path, monkeypatch):
    store, lifecycle, path = _receipt_lifecycle(tmp_path)
    lifecycle.start("app-server", [sys.executable, "-c", "import time; time.sleep(20)"])
    owner = lifecycle._groups["app-server"]

    def fail(*_args):
        raise OSError("storage temporarily unavailable")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(receipts, "_fsync_directory", fail)
            patch.setattr(store, "_publish", fail)
            with pytest.raises(receipts.ProcessReceiptError):
                lifecycle.close()
            assert not path.exists() and not owner.closed
            with pytest.raises(receipts.ProcessReceiptError):
                lifecycle.close()
            assert not owner.closed and path in store._pending_retirements
        lifecycle.close()
        assert owner.closed and not store._pending_retirements
    finally:
        lifecycle.close()


def test_failed_pidfd_acquisition_never_executes_workload_and_reaps_helper(tmp_path, monkeypatch):
    _store, lifecycle, path = _receipt_lifecycle(tmp_path)
    marker = tmp_path / "workload-ran"
    spawned = []
    original_spawn = subprocess.Popen

    def spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        spawned.append(process)
        return process

    def fail_acquisition(_pid):
        raise OSError("pidfd admission failed")

    monkeypatch.setattr(receipts.os, "pidfd_open", fail_acquisition)
    with pytest.raises(OSError, match="pidfd admission failed"):
        lifecycle.start(
            "app-server",
            [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)],
            process_spawner=spawn,
            stderr=subprocess.DEVNULL,
        )
    assert len(spawned) == 1 and spawned[0].poll() is not None
    assert not marker.exists() and not path.exists()
    assert not lifecycle._groups


def test_catalog_probe_retains_failed_cleanup_and_retries_before_next_launch(tmp_path, monkeypatch):
    executable = tmp_path / "catalog-native"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import signal, socket, sys\n"
        "path = sys.argv[sys.argv.index('--listen') + 1].removeprefix('unix://')\n"
        "listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "listener.bind(path)\n"
        "listener.listen()\n"
        "while True: signal.pause()\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(tmp_path))
    spawned = []

    def spawn(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        spawned.append(process)
        return process

    launcher = RodexRuntimeLauncher(str(executable), "/usr/bin/tmux", process_spawner=spawn)
    monkeypatch.setattr(launcher, "_read_persisted_codex_session", lambda *_args: True)

    def fail_signal(*_args):
        raise PermissionError("catalog group cleanup unavailable")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(receipts.signal, "pidfd_send_signal", fail_signal)
            for _attempt in range(2):
                with pytest.raises(receipts.ProcessReceiptError, match="unresolved"):
                    launcher.codex_session_is_persisted(uuid.UUID(int=1))
            assert len(spawned) == 1 and spawned[0].poll() is None
            owner = launcher._catalog_lifecycle._groups["app-server"]
            os.fstat(owner.descriptor)
            assert owner in receipts._RETAINED_GROUPS
            catalog_receipts = list(tmp_path.glob("*.catalog-process-*.json"))
            assert len(catalog_receipts) == 1
            # The daemon cannot take over another live launcher's transient group.
            receipts.RuntimeProcessReceipts(tmp_path).reconcile()
            assert catalog_receipts[0].exists() and spawned[0].poll() is None
        assert launcher.codex_session_is_persisted(uuid.UUID(int=1))
        assert len(spawned) == 2 and all(process.poll() is not None for process in spawned)
        assert not list(tmp_path.glob("*.catalog-process-*.json"))
    finally:
        if launcher._catalog_lifecycle is not None:
            launcher._catalog_lifecycle.close()
