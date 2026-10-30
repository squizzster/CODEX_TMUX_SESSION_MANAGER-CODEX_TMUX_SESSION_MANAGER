"""Deleted coordination files must never authorize cleanup of a live owner's children."""

import errno
import os
import subprocess
import sys
import time
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from test_rodex_daemon import short_runtime_root as short_runtime_root

import rodex.daemon as daemon
from rodex.daemon_client import RodexDaemonClient, RodexDaemonError, daemon_instance_path, daemon_socket_path
from rodex.process_receipts import ProcessReceiptError, RuntimeProcessReceipts
from rodex.runtime_endpoint import ExclusiveUnixEndpoint


def test_deleted_daemon_paths_cannot_admit_a_second_manager_or_kill_live_children(short_runtime_root, monkeypatch):
    ready = Event()
    managers = []
    failures = []

    class SentinelManager:
        def __init__(self, _root):
            self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            managers.append(self)

        def refresh_owned_paths(self):
            ready.set()

        def close(self):
            self.child.terminate()
            self.child.wait(timeout=3)

    monkeypatch.setattr(daemon, "DaemonRuntimeManager", SentinelManager)
    original = daemon.RodexDaemonServer(short_runtime_root)

    def run():
        try:
            original.run()
        except BaseException as error:
            failures.append(error)

    worker = Thread(target=run)
    worker.start()
    try:
        assert ready.wait(3)
        original_instance = daemon_instance_path(short_runtime_root).read_text()
        for path in (original._endpoint.path, original._endpoint.lock_path, daemon_instance_path(short_runtime_root)):
            path.unlink()
        replacement = daemon.RodexDaemonServer(short_runtime_root)
        with pytest.raises(OSError) as refused:
            replacement.run()
        assert refused.value.errno == errno.EADDRINUSE
        assert len(managers) == 1 and managers[0].child.poll() is None
        assert not daemon_instance_path(short_runtime_root).exists()
        # The original identity remains in memory; no replacement published one.
        assert original_instance.strip() == original._instance_id
    finally:
        original.stop()
        worker.join(timeout=5)
    assert not worker.is_alive() and failures == []
    assert managers[0].child.poll() is not None


def test_kernel_claim_covers_symlinked_parent_and_releases_after_owner_exit(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    first = ExclusiveUnixEndpoint(directory / "service.sock")
    second = ExclusiveUnixEndpoint(alias / "service.sock")
    try:
        first.acquire()
        first.lock_path.unlink()
        with pytest.raises(OSError) as refused:
            second.acquire()
        assert refused.value.errno == errno.EADDRINUSE
        first.close()
        second.acquire()
    finally:
        first.close()
        second.close()


def test_daemon_refresh_preserves_coordination_and_live_receipts_then_resize_works(short_runtime_root, monkeypatch):
    ready = Event()
    resized = Event()
    failures = []
    receipts = RuntimeProcessReceipts(short_runtime_root)
    receipt = short_runtime_root / "owned-process.json"
    receipts._publish(receipt, {"runtime": "sentinel"})

    class Manager:
        def __init__(self, _root):
            pass

        def refresh_owned_paths(self):
            receipts.refresh_owned_paths()
            ready.set()

        def wake_runtime(self, runtime_id, cause):
            assert (runtime_id, cause) == ("0123456789abcdef", "terminal_resize")
            resized.set()

        def close(self):
            pass

    monkeypatch.setattr(daemon, "DaemonRuntimeManager", Manager)
    server = daemon.RodexDaemonServer(short_runtime_root)

    def run():
        try:
            server.run()
        except BaseException as error:
            failures.append(error)

    worker = Thread(target=run)
    worker.start()
    try:
        assert ready.wait(3)
        paths = [server._endpoint.path, server._endpoint.lock_path, daemon_instance_path(short_runtime_root), receipt]
        for path in paths:
            os.utime(path, (1, 1))
        server._path_keepalive._refresh()
        # Simulate an age-based cleaner while the owner is still alive.
        for path in paths:
            if path.stat().st_mtime < time.time() - 86400:
                path.unlink()
        assert all(path.exists() for path in paths)
        client = RodexDaemonClient(short_runtime_root, sys.executable)
        assert client.notify_runtime("0123456789abcdef", "terminal_resize")
        assert resized.wait(1)
    finally:
        server.stop()
        worker.join(timeout=5)
    assert not worker.is_alive() and failures == []


def test_receipt_refresh_does_not_touch_replacement_or_retired_paths(tmp_path):
    store = RuntimeProcessReceipts(tmp_path)
    path = tmp_path / "receipt.json"
    payload = {"kind": "sentinel"}
    store._publish(path, payload)
    displaced = tmp_path / "displaced.json"
    path.rename(displaced)
    path.write_text("replacement")
    os.utime(path, (1, 1))
    with pytest.raises(ProcessReceiptError, match="pathname changed"):
        store.refresh_owned_paths()
    assert path.stat().st_mtime == 1
    path.unlink()
    displaced.rename(path)
    # _retire's content decoder belongs to the receipt-schema tests.
    store._read = lambda _path: payload
    store._retire(path, payload)
    store.refresh_owned_paths()
    assert not path.exists()


def test_startup_crash_reports_exit_and_log_without_waiting_for_readiness(short_runtime_root):
    failed = SimpleNamespace(returncode=17, poll=lambda: 17)
    client = RodexDaemonClient(
        short_runtime_root,
        sys.executable,
        process_spawner=lambda *_args, **_kwargs: failed,
        sleep=lambda _: pytest.fail("must not wait after the startup process has exited"),
    )
    with pytest.raises(RodexDaemonError, match=r"exited during startup.*status 17.*see .*log"):
        client.ensure_running()
    assert not daemon_socket_path(short_runtime_root).exists()
