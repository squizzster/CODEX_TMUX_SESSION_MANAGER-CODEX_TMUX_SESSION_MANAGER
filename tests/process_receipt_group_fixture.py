"""Run destructive receipt checks only inside this isolated child supervisor.

The helper is a subreaper, so every created group member is reaped here rather
than left to a container's PID 1. No signal target comes from outside the fixture.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from rodex import process_receipts as receipts
from rodex_registry import RodexRuntimeId


class FixtureWatchdogExpired(RuntimeError):
    pass


class FixtureSetupFailed(RuntimeError):
    pass


def _watchdog_expired(_signum, _frame) -> None:
    raise FixtureWatchdogExpired("isolated process fixture exceeded its work deadline")


def _reap_remaining_owned_children(send_signal) -> None:
    """Clean up even a grandchild whose PID was never received during setup.

    Only this single-threaded subreaper's direct children are enumerated. No
    concurrent reaper can release a PID between enumeration and pidfd_open;
    an exited child remains a zombie until this function reaps it.
    """
    children_path = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    deadline = time.monotonic() + 3
    while children := children_path.read_text().split():
        for text_pid in children:
            pid = int(text_pid)
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited:
                continue
            descriptor = os.pidfd_open(pid)
            try:
                with suppress(ProcessLookupError):
                    send_signal(descriptor, signal.SIGKILL)
            finally:
                os.close(descriptor)
        assert time.monotonic() < deadline, "fixture could not kill and reap its remaining children"
        time.sleep(0.005)


def _wait_until_stopped(*pids: int) -> None:
    deadline = time.monotonic() + 3
    while any(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "T" for pid in pids):
        assert time.monotonic() < deadline, "fixture group never reached stopped state"
        time.sleep(0.005)


def _run_group_child(read_fd: int, write_fd: int, scenario: str) -> None:
    # Forked processes must not inherit the supervisor's exception/cleanup path.
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(20)
    try:
        os.close(read_fd)
        os.setsid()
        child = os.fork()
        if child == 0:
            if scenario not in {"graceful", "extinct_before_cont"}:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.alarm(20)
            os.write(write_fd, str(os.getpid()).encode() + b"\n")
        os.close(write_fd)
        while True:
            signal.pause()
    finally:
        os._exit(1)


def exercise_group_lifetime(root: Path, scenario: str) -> dict:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER, this helper only.
        raise OSError(ctypes.get_errno(), "fixture could not become a subreaper")
    pipe_descriptors = set()
    process_descriptors = []
    stop_reaper = Event()
    reaper = None
    exit_codes = {}
    original_send = signal.pidfd_send_signal
    original_killpg = os.killpg
    sent = []
    numeric_probes = []
    used_descriptors = set()
    signal.signal(signal.SIGALRM, _watchdog_expired)

    # This deadline raises in the supervisor, which is never in the stopped
    # group. It leaves time for cleanup before the caller's 20-second timeout.
    try:
        signal.setitimer(signal.ITIMER_REAL, 10)
        read_fd, write_fd = os.pipe()
        pipe_descriptors.update((read_fd, write_fd))
        leader = os.fork()
        if leader == 0:
            _run_group_child(read_fd, write_fd, scenario)
        if scenario == "setup_failure":
            raise FixtureSetupFailed("injected failure before acquiring any child pidfd")
        leader_fd = os.pidfd_open(leader)
        process_descriptors.append(leader_fd)
        os.close(write_fd)
        pipe_descriptors.remove(write_fd)
        with os.fdopen(read_fd) as ready:
            pipe_descriptors.remove(read_fd)
            child = int(ready.readline())
        if scenario == "setup_failure_after_child":
            raise FixtureSetupFailed("injected failure before acquiring the grandchild pidfd")
        child_fd = os.pidfd_open(child)
        process_descriptors.append(child_fd)
        sentinel = os.fork()
        if sentinel == 0:
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(20)
            try:
                os.setsid()
                while True:
                    signal.pause()
            finally:
                os._exit(1)
        sentinel_fd = os.pidfd_open(sentinel)
        process_descriptors.append(sentinel_fd)
        reaped = {leader: Event(), child: Event()}

        def reap_owned_children() -> None:
            pending = {leader, child}
            while pending and not stop_reaper.is_set():
                for pid in tuple(pending):
                    try:
                        waited, status = os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        continue  # Grandchild adoption follows leader exit.
                    if waited:
                        exit_codes[pid] = os.waitstatus_to_exitcode(status)
                        pending.remove(pid)
                        reaped[pid].set()
                stop_reaper.wait(0.005)

        reaper = Thread(target=reap_owned_children, daemon=True)
        reaper.start()

        def wait_reaped(pid: int) -> None:
            assert reaped[pid].wait(3), f"fixture child {pid} did not exit"

        def send(descriptor, signum, info=None, flags=0):
            assert flags == receipts.PIDFD_SIGNAL_PROCESS_GROUP
            sent.append(signum)
            if scenario == "reconcile_retry" and signum == signal.SIGCONT and sent.count(signal.SIGCONT) == 1:
                raise PermissionError(errno.EPERM, "injected reconciliation failure after TERM")
            used_descriptors.add(descriptor)
            if scenario == "leader_reaped_before_term" and signum == signal.SIGTERM:
                original_send(leader_fd, signal.SIGKILL)
                wait_reaped(leader)
            if scenario in {"leader_reaped_before_cont", "extinct_before_cont"} and signum == signal.SIGCONT:
                wait_reaped(leader)
                if scenario == "extinct_before_cont":
                    wait_reaped(child)
            return original_send(descriptor, signum, info, flags)

        def probe(pgid, signum):
            assert (pgid, signum) == (leader, 0), "numeric termination is forbidden"
            numeric_probes.append(signum)
            return original_killpg(pgid, signum)

        # Fork return does not prove that the sentinel has established its own
        # session yet; await the actual state before recording the identities.
        while receipts._read_process_state(sentinel)["process_group_id"] != sentinel:
            time.sleep(0.005)
        identities = {
            role: {"pid": pid, **receipts._read_process_state(pid)}
            for role, pid in (("leader", leader), ("child", child), ("sentinel", sentinel))
        }
        assert identities["child"]["process_group_id"] == leader
        assert identities["sentinel"]["process_group_id"] != leader
        try:
            original_send(leader_fd, 0, None, receipts.PIDFD_SIGNAL_PROCESS_GROUP)
        except OSError as error:
            if error.errno in {errno.EINVAL, errno.ENOSYS}:
                raise SystemExit(77) from error
            raise
        store = receipts.RuntimeProcessReceipts(root)
        store.record("app-server", RodexRuntimeId(1), "1" * 32, SimpleNamespace(pid=leader, poll=lambda: None))
        receipt_path = store._path("app-server", RodexRuntimeId(1))
        if scenario in {"stopped", "watchdog_stopped"}:
            original_send(leader_fd, signal.SIGSTOP, None, receipts.PIDFD_SIGNAL_PROCESS_GROUP)
            _wait_until_stopped(leader, child)
            if scenario == "watchdog_stopped":
                signal.setitimer(signal.ITIMER_REAL, 0.05)
                while True:
                    signal.pause()
        if scenario in {"leader_absent", "already_extinct"}:
            original_send(leader_fd, signal.SIGKILL)
            wait_reaped(leader)
            if scenario == "already_extinct":
                original_send(child_fd, signal.SIGKILL)
                wait_reaped(child)
        with (
            patch.object(receipts.signal, "pidfd_send_signal", send),
            patch.object(receipts.os, "killpg", probe),
            patch.object(receipts, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.1),
        ):
            if scenario == "leader_absent":
                try:
                    store.reconcile()
                except receipts.ProcessReceiptError as error:
                    assert "without a verified leader" in str(error)
                else:
                    raise AssertionError("surviving unowned group was silently retired")
                assert receipt_path.exists()
                original_send(child_fd, 0)
            else:
                if scenario == "reconcile_retry":
                    try:
                        store.reconcile()
                    except receipts.ProcessReceiptError:
                        pass
                    else:
                        raise AssertionError("injected reconciliation failure was ignored")
                    assert receipt_path.exists()
                    wait_reaped(leader)

                    def forbid_reacquisition(_pid):
                        raise AssertionError("lost the retained group capability after leader exit")

                    with patch.object(receipts.os, "pidfd_open", forbid_reacquisition):
                        receipts.RuntimeProcessReceipts(root).reconcile()
                store.reconcile()
                assert not receipt_path.exists()
        if scenario != "leader_absent":
            wait_reaped(leader)
            wait_reaped(child)
            assert exit_codes[child] == (
                -signal.SIGTERM if scenario in {"graceful", "extinct_before_cont"} else -signal.SIGKILL
            )
            # Every subsequent action on the extinct original group must refuse.
            for signum in (0, signal.SIGTERM, signal.SIGCONT, signal.SIGKILL):
                try:
                    original_send(leader_fd, signum, None, receipts.PIDFD_SIGNAL_PROCESS_GROUP)
                except ProcessLookupError:
                    pass
                else:
                    raise AssertionError("extinct group accepted a signal")
        original_send(sentinel_fd, 0)
        assert os.waitpid(sentinel, os.WNOHANG) == (0, 0)
        assert signal.SIGSTOP not in sent
        assert len(used_descriptors) <= 1
        return dict(
            scenario=scenario,
            identities=identities,
            exit_codes=exit_codes,
            signals=sent,
            numeric_probes=numeric_probes,
            sentinel_untouched=True,
            receipt_retained=receipt_path.exists(),
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        stop_reaper.set()
        if reaper is not None and reaper.ident is not None:
            reaper.join(timeout=1)
            assert not reaper.is_alive(), "fixture's concurrent reaper did not stop"
        for descriptor in pipe_descriptors:
            os.close(descriptor)
        for descriptor in process_descriptors:
            with suppress(ProcessLookupError):
                original_send(descriptor, signal.SIGKILL)
            os.close(descriptor)
        _reap_remaining_owned_children(original_send)


if __name__ == "__main__":
    try:
        evidence = exercise_group_lifetime(Path(sys.argv[1]), sys.argv[2])
    except FixtureSetupFailed:
        assert sys.argv[2] in {"setup_failure", "setup_failure_after_child"}
        evidence = {"cleanup_verified": True, "failure": "setup"}
    except FixtureWatchdogExpired:
        assert sys.argv[2] == "watchdog_stopped"
        evidence = {"cleanup_verified": True, "failure": "watchdog"}
    print(json.dumps(evidence))
