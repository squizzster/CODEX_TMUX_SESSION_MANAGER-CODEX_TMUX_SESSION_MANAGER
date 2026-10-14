"""Fault boundaries for acquired pidfds and exact group-signal authority."""

import ast
import errno
import inspect
import json
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from rodex import process_receipts as receipts
from rodex_registry import RodexRuntimeId


def test_numeric_group_calls_can_only_check_absence():
    tree = ast.parse(inspect.getsource(receipts))
    numeric_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) in {"os.killpg", "os.kill"}
    ]
    assert len(numeric_calls) == 1
    assert ast.unparse(numeric_calls[0].func) == "os.killpg"
    assert isinstance(numeric_calls[0].args[1], ast.Constant) and numeric_calls[0].args[1].value == 0


@pytest.mark.parametrize(
    "scenario",
    [
        "graceful",
        "escalation",
        "stopped",
        "leader_reaped_before_term",
        "leader_reaped_before_cont",
        "extinct_before_cont",
        "leader_absent",
        "already_extinct",
    ],
)
def test_isolated_group_lifetimes_preserve_signal_authority(tmp_path, scenario):
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("process_receipt_group_fixture.py")),
            str(tmp_path),
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode == 77:
        pytest.skip("kernel lacks group-scoped pidfd signalling")
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    assert evidence["sentinel_untouched"]
    assert evidence["receipt_retained"] == (scenario == "leader_absent")
    assert evidence["numeric_probes"] == ([0] if scenario in {"leader_absent", "already_extinct"} else [])


@pytest.mark.parametrize(
    "scenario, failure",
    [("setup_failure", "setup"), ("setup_failure_after_child", "setup"), ("watchdog_stopped", "watchdog")],
)
def test_isolated_fixture_cleans_up_after_setup_failure_and_stopped_group_timeout(tmp_path, scenario, failure):
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("process_receipt_group_fixture.py")),
            str(tmp_path),
            scenario,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode == 77:
        pytest.skip("kernel lacks group-scoped pidfd signalling")
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"cleanup_verified": True, "failure": failure}


@pytest.mark.parametrize("read", ["read_text", "stat"])
@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_process_state_closes_acquired_pidfd_on_filesystem_failure(monkeypatch, read, failure):
    closed = []
    monkeypatch.setattr(receipts.os, "pidfd_open", lambda _pid: 123)
    monkeypatch.setattr(receipts.os, "close", closed.append)
    monkeypatch.setattr(Path, "read_text", lambda _path: "process) " + "1 " * 20)

    def fail(_path):
        raise failure("injected process race")

    monkeypatch.setattr(Path, read, fail)
    expected = ProcessLookupError if failure is FileNotFoundError else failure
    with pytest.raises(expected):
        receipts._read_process_state(456)
    assert closed == [123]


@pytest.mark.parametrize("text", ["invalid", "process) S 1 nope"])
def test_process_state_closes_pidfd_on_parse_failure(monkeypatch, text):
    closed = []
    monkeypatch.setattr(receipts.os, "pidfd_open", lambda _pid: 123)
    monkeypatch.setattr(receipts.os, "close", closed.append)
    monkeypatch.setattr(Path, "read_text", lambda _path: text)
    monkeypatch.setattr(Path, "stat", lambda _path: SimpleNamespace(st_uid=42))
    with pytest.raises(receipts.ProcessReceiptError, match="parse"):
        receipts._read_process_state(456)
    assert closed == [123]


def test_process_state_success_retains_identity_and_closes_pidfd(monkeypatch):
    closed = []
    fields = ["S", "1", "456", *(["0"] * 16), "789"]
    monkeypatch.setattr(receipts.os, "pidfd_open", lambda _pid: 123)
    monkeypatch.setattr(receipts.os, "close", closed.append)
    monkeypatch.setattr(
        receipts.select, "poll", lambda: SimpleNamespace(register=lambda *_args: None, poll=lambda _timeout: [])
    )
    monkeypatch.setattr(Path, "read_text", lambda _path: "456 (name with ) parentheses) " + " ".join(fields))
    monkeypatch.setattr(Path, "stat", lambda _path: SimpleNamespace(st_uid=42))
    assert receipts._read_process_state(456) == {"uid": 42, "process_group_id": 456, "start_time_ticks": 789}
    assert closed == [123]


def _fake_retained_receipt(tmp_path, monkeypatch):
    store = receipts.RuntimeProcessReceipts(tmp_path)
    path = store._path("app-server", RodexRuntimeId(1))
    path.touch(mode=0o600)
    identity = {"uid": 42, "process_group_id": 456, "start_time_ticks": 789}
    closed = []
    monkeypatch.setattr(store, "_read", lambda _path: {"pid": 456, **identity})
    monkeypatch.setattr(receipts, "_read_process_state_from_descriptor", lambda _pid, _fd: identity)
    monkeypatch.setattr(receipts.os, "pidfd_open", lambda _pid: 123)
    monkeypatch.setattr(receipts.os, "close", closed.append)

    return store, path, closed


@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.ENOSYS, errno.EPERM])
@pytest.mark.parametrize("failing_signal", [signal.SIGTERM, signal.SIGCONT, signal.SIGKILL, 0])
def test_group_signal_failure_keeps_receipt_without_numeric_fallback(tmp_path, monkeypatch, error_number, failing_signal):
    store, path, closed = _fake_retained_receipt(tmp_path, monkeypatch)
    sent = []

    def send(descriptor, signum, info, flags):
        sent.append(signum)
        assert (descriptor, info, flags) == (123, None, 1 << 2)
        if signum == failing_signal:
            raise OSError(error_number, "injected group-scope failure")

    monkeypatch.setattr(receipts.signal, "pidfd_send_signal", send)
    monkeypatch.setattr(receipts, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0)
    monkeypatch.setattr(receipts.os, "killpg", lambda *_args: pytest.fail("numeric group was signalled"))
    with pytest.raises(receipts.ProcessReceiptError, match="unreconciled"):
        store.reconcile()
    assert path.exists()
    assert closed == [123]
    assert signal.SIGSTOP not in sent


@pytest.mark.parametrize("extinction_call", range(5))
def test_group_extinction_at_each_phase_retires_receipt_without_later_signals(tmp_path, monkeypatch, extinction_call):
    store, path, closed = _fake_retained_receipt(tmp_path, monkeypatch)
    sent = []

    def send(descriptor, signum, info, flags):
        assert (descriptor, info, flags) == (123, None, 1 << 2)
        sent.append(signum)
        if len(sent) == extinction_call + 1:
            raise ProcessLookupError(errno.ESRCH, "original group is extinct")

    monkeypatch.setattr(receipts.signal, "pidfd_send_signal", send)
    monkeypatch.setattr(receipts, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0)
    monkeypatch.setattr(receipts, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(receipts.os, "killpg", lambda *_args: pytest.fail("numeric group was signalled"))
    store.reconcile()
    assert sent == [signal.SIGTERM, signal.SIGCONT, 0, signal.SIGKILL, 0][: extinction_call + 1]
    assert not path.exists()
    assert closed == [123]


def test_successful_kill_without_confirmed_exit_keeps_receipt_and_bounds_wait(tmp_path, monkeypatch):
    store, path, closed = _fake_retained_receipt(tmp_path, monkeypatch)
    clock = [0.0]
    sent = []
    monkeypatch.setattr(receipts.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(receipts.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(receipts.signal, "pidfd_send_signal", lambda _fd, signum, _info, _flags: sent.append(signum))
    with pytest.raises(receipts.ProcessReceiptError, match="exit was not confirmed"):
        store.reconcile()
    assert clock[0] == 4.0
    assert [signum for signum in sent if signum] == [signal.SIGTERM, signal.SIGCONT, signal.SIGKILL]
    assert path.exists()
    assert closed == [123]


@pytest.mark.parametrize("leader_state", ["absent", "group_only", "exited", "replacement"])
@pytest.mark.parametrize("group_state", ["absent", "exists", "permission-error"])
def test_unverified_leader_allows_only_numeric_absence_probe(tmp_path, monkeypatch, leader_state, group_state):
    store, path, closed = _fake_retained_receipt(tmp_path, monkeypatch)

    def missing(*_args):
        if leader_state == "group_only":
            raise OSError(errno.EINVAL, "only process-group identity remains")
        raise ProcessLookupError(errno.ESRCH, "leader is gone")

    if leader_state in {"absent", "group_only"}:
        monkeypatch.setattr(receipts.os, "pidfd_open", missing)
    elif leader_state == "exited":
        monkeypatch.setattr(receipts, "_read_process_state_from_descriptor", missing)
    else:
        monkeypatch.setattr(
            receipts,
            "_read_process_state_from_descriptor",
            lambda *_args: dict(uid=42, process_group_id=456, start_time_ticks=999),
        )
    probes = []

    def probe(pgid, signum):
        probes.append((pgid, signum))
        assert (pgid, signum) == (456, 0)
        if group_state == "absent":
            raise ProcessLookupError(errno.ESRCH, "no group")
        if group_state == "permission-error":
            raise PermissionError(errno.EPERM, "unknown group")

    monkeypatch.setattr(receipts.os, "killpg", probe)
    monkeypatch.setattr(receipts.signal, "pidfd_send_signal", lambda *_args: pytest.fail("unverified pidfd signalled"))
    monkeypatch.setattr(receipts, "_fsync_directory", lambda _path: None)
    if group_state == "absent":
        store.reconcile()
        assert not path.exists()
    else:
        with pytest.raises(receipts.ProcessReceiptError, match="unreconciled"):
            store.reconcile()
        assert path.exists()
    assert probes == [(456, 0)]
    assert closed == ([] if leader_state in {"absent", "group_only"} else [123])


@pytest.mark.parametrize("mask", [0, receipts.select.POLLIN, receipts.select.POLLHUP, receipts.select.POLLNVAL])
def test_borrowed_pidfd_validation_checks_readiness_without_reopening_or_closing(monkeypatch, mask):
    fields = ["S", "1", "456", *(["0"] * 16), "789"]
    monkeypatch.setattr(Path, "read_text", lambda _path: "456 (name) " + " ".join(fields))
    monkeypatch.setattr(Path, "stat", lambda _path: SimpleNamespace(st_uid=42))
    monkeypatch.setattr(receipts.os, "pidfd_open", lambda *_args: pytest.fail("reopened pidfd"))
    monkeypatch.setattr(receipts.os, "close", lambda *_args: pytest.fail("closed borrowed pidfd"))
    registered = []
    monkeypatch.setattr(
        receipts.select,
        "poll",
        lambda: SimpleNamespace(
            register=lambda *args: registered.append(args), poll=lambda _timeout: [(123, mask)] if mask else []
        ),
    )
    if mask:
        expected = receipts.ProcessReceiptError if mask == receipts.select.POLLNVAL else ProcessLookupError
        with pytest.raises(expected):
            receipts._read_process_state_from_descriptor(456, 123)
    else:
        assert receipts._read_process_state_from_descriptor(456, 123) == dict(
            uid=42, process_group_id=456, start_time_ticks=789
        )
    assert registered == [(123, receipts.select.POLLIN)]
