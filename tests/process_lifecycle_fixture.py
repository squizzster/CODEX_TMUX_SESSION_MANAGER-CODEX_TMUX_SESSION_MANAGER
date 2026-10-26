"""Isolated subreaper for production startup/cleanup acceptance, never user groups."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import pty
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from rodex import process_receipts as receipts
from rodex.interaction_pipeline import SessionInteractionPipeline
from rodex.terminal_gateway import TerminalSessionGateway
from rodex_registry import RodexRuntimeId


def workload(ready: Path) -> None:
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        # Existence is the parent's readiness signal. Publish only a complete
        # PID, so cleanup cannot start before it has acquired the child pidfd.
        pending = ready.with_name("workload-child.pending.pid")
        pending.write_text(str(os.getpid()))
        pending.replace(ready)
    while True:
        signal.pause()


def exercise(root: Path, route: str, scenario: str) -> dict:
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # Fixture-only adoption of orphaned grandchildren.
    signal.alarm(15)
    ready = root / "workload-child.pid"
    store = receipts.RuntimeProcessReceipts(root)
    runtime_id = RodexRuntimeId(42)
    kind = "native-tui" if route == "gateway" else "app-server"
    receipt_path = store._path(kind, runtime_id)
    published = []
    retired = []

    def publish(process_kind, group):
        assert not ready.exists(), "workload ran before authenticated receipt publication"
        assert group.descriptor >= 0 and group.identity is not None
        store.record(process_kind, runtime_id, "a" * 32, group)
        published.append(group)
        if scenario in {"partial_start", "partial_start_cleanup_failure"}:
            raise RuntimeError("injected failure after receipt publication")

    def retire(process_kind, group):
        store.release(process_kind, runtime_id, "a" * 32, group)
        retired.append(group)

    lifecycle = receipts.RuntimeProcessLifecycle(publish, retire)
    gateway = None
    descriptors = []
    stop_reaper = Event()
    reaper = None
    child_reaped = Event()
    child_descriptor = -1
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True)
    original_send = signal.pidfd_send_signal
    original_probe = receipts._signal_exact_process_group
    command = [sys.executable, "-I", __file__, "workload", str(ready)]
    receipts.PROCESS_GROUP_TERM_GRACE_SECONDS = 0.1
    receipts.PROCESS_GROUP_EXIT_CONFIRM_SECONDS = 0.2

    def fail_signal(*_args, **_kwargs):
        raise PermissionError(errno.EPERM, "injected signalling failure")

    def start():
        nonlocal gateway
        if route == "gateway":
            master, slave = pty.openpty()
            descriptors.extend((master, slave))
            gateway = TerminalSessionGateway(
                command,
                env=dict(os.environ),
                cwd=root,
                pipeline=SessionInteractionPipeline(),
                runtime_identity="lifecycle-acceptance",
                registrations=(),
                confirm_native_prefix=lambda _prefix: False,
                input_fd=slave,
                output_fd=slave,
                process_lifecycle=lifecycle,
            )
            return gateway.process
        return lifecycle.start(kind, command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

    def finish():
        if gateway is not None:
            gateway.close()
        else:
            lifecycle.close()

    try:
        if scenario in {"partial_start", "partial_start_cleanup_failure"}:
            with patch.object(
                receipts.signal,
                "pidfd_send_signal",
                fail_signal if scenario.endswith("failure") else original_send,
            ):
                try:
                    start()
                except (RuntimeError, receipts.ProcessReceiptError):
                    pass
                else:
                    raise AssertionError("partial startup did not fail")
            assert len(published) == 1 and not ready.exists()
            if scenario.endswith("failure"):
                assert receipt_path.exists()
                os.fstat(published[0].descriptor)
                assert published[0] in receipts._RETAINED_GROUPS
                lifecycle.close()
            assert not receipt_path.exists()
            assert published[0].process.poll() is not None
        else:
            process = start()
            deadline = time.monotonic() + 3
            while not ready.exists():
                assert time.monotonic() < deadline, "workload did not start"
                time.sleep(0.005)
            child = int(ready.read_text())
            child_descriptor = os.pidfd_open(child)
            owner = published[0]
            retained_descriptor = owner.descriptor
            original_receipt = receipt_path.read_bytes()

            def reap_fixture_grandchild():
                while not stop_reaper.is_set():
                    try:
                        waited, _status = os.waitpid(child, os.WNOHANG)
                    except ChildProcessError:
                        waited = 0  # It belongs to the native leader until adoption.
                    if waited:
                        child_reaped.set()
                        return
                    stop_reaper.wait(0.005)

            reaper = Thread(target=reap_fixture_grandchild)
            reaper.start()
            if scenario == "leader_first":
                original_send(retained_descriptor, signal.SIGKILL)
                process.wait(timeout=2)
            elif scenario == "stopped":
                original_send(retained_descriptor, signal.SIGSTOP, None, receipts.PIDFD_SIGNAL_PROCESS_GROUP)
                for pid in (process.pid, child):
                    while Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "T":
                        assert time.monotonic() < deadline
                        time.sleep(0.005)

            if scenario in {"failed_signal", "unresolved_exit", "retirement_failure"}:

                def uncertain_probe(descriptor, signum):
                    if signum == 0:
                        return True
                    return original_probe(descriptor, signum)

                def failed_retirement(*_args):
                    raise OSError("receipt retirement unavailable")

                with (
                    patch.object(
                        receipts.signal,
                        "pidfd_send_signal",
                        fail_signal if scenario == "failed_signal" else original_send,
                    ),
                    patch.object(
                        receipts,
                        "_signal_exact_process_group",
                        uncertain_probe if scenario == "unresolved_exit" else original_probe,
                    ),
                    patch.object(
                        store, "release", failed_retirement if scenario == "retirement_failure" else store.release
                    ),
                ):
                    try:
                        finish()
                    except (receipts.ProcessReceiptError, OSError):
                        pass
                    else:
                        raise AssertionError("uncertain cleanup reported success")
                assert receipt_path.read_bytes() == original_receipt
                assert owner.descriptor == retained_descriptor
                os.fstat(retained_descriptor)
                assert owner in receipts._RETAINED_GROUPS
                if gateway is not None:
                    assert gateway._closed and gateway._master == -1
                try:
                    lifecycle.start(kind, command)
                except receipts.ProcessReceiptError:
                    pass
                else:
                    raise AssertionError("retry replaced unresolved process ownership")
                assert receipt_path.read_bytes() == original_receipt
            if scenario == "concurrent_finalizers":
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(lambda _index: finish(), range(4)))
            else:
                finish()
            finish()
            assert process.poll() is not None
            assert child_reaped.wait(1)
            assert not receipt_path.exists()
            assert owner.closed and owner.descriptor == -1
            assert owner not in receipts._RETAINED_GROUPS
        assert len(retired) == 1
        assert sentinel.poll() is None
        return dict(route=route, scenario=scenario, receipt_retired=True, sentinel_untouched=True)
    finally:
        # Emergency fixture cleanup addresses only retained children and descriptors.
        for group in published:
            if group.descriptor >= 0:
                with suppress(ProcessLookupError):
                    original_send(group.descriptor, signal.SIGKILL, None, receipts.PIDFD_SIGNAL_PROCESS_GROUP)
        if child_descriptor >= 0:
            with suppress(ProcessLookupError):
                original_send(child_descriptor, signal.SIGKILL)
        for group in published:
            group.process.wait(timeout=2)
        if reaper is not None:
            child_reaped.wait(2)
            stop_reaper.set()
            reaper.join(timeout=1)
        lifecycle.close()
        sentinel.terminate()
        sentinel.wait(timeout=2)
        if child_descriptor >= 0:
            os.close(child_descriptor)
        for descriptor in descriptors:
            os.close(descriptor)
        signal.alarm(0)


if __name__ == "__main__":
    if sys.argv[1] == "workload":
        workload(Path(sys.argv[2]))
    else:
        print(json.dumps(exercise(Path(sys.argv[1]), sys.argv[2], sys.argv[3])))
