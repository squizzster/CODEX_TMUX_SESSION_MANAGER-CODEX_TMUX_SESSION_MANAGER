"""Fault boundaries for acquired pidfds and unresolved group ownership."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from rodex import process_receipts as receipts
from rodex_registry import RodexRuntimeId


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
    monkeypatch.setattr(receipts.os, "waitid", lambda *_args: None)
    monkeypatch.setattr(Path, "read_text", lambda _path: "456 (name with ) parentheses) " + " ".join(fields))
    monkeypatch.setattr(Path, "stat", lambda _path: SimpleNamespace(st_uid=42))
    assert receipts._read_process_state(456) == {"uid": 42, "process_group_id": 456, "start_time_ticks": 789}
    assert closed == [123]


@pytest.mark.parametrize("failure", [ProcessLookupError, PermissionError])
def test_failed_exact_stop_keeps_receipt_and_never_signals_numeric_group(tmp_path, monkeypatch, failure):
    store = receipts.RuntimeProcessReceipts(tmp_path)
    path = store._path("app-server", RodexRuntimeId(1))
    path.touch(mode=0o600)
    identity = {"uid": 42, "process_group_id": 456, "start_time_ticks": 789}
    closed = []
    monkeypatch.setattr(store, "_read", lambda _path: {"pid": 456, **identity})
    monkeypatch.setattr(receipts, "_read_process_state", lambda _pid: identity)
    monkeypatch.setattr(receipts.os, "pidfd_open", lambda _pid: 123)
    monkeypatch.setattr(receipts.os, "close", closed.append)

    def fail_stop(*_args):
        raise failure("injected lost authority")

    monkeypatch.setattr(receipts.signal, "pidfd_send_signal", fail_stop)
    monkeypatch.setattr(receipts.os, "killpg", lambda *_args: pytest.fail("numeric group was signalled"))
    with pytest.raises(receipts.ProcessReceiptError, match="unreconciled"):
        store.reconcile()
    assert path.exists()
    assert closed == [123]
