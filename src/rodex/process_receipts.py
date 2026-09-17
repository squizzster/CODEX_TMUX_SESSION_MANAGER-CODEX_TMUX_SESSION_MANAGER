"""Exact process-group receipts for daemon crash reconciliation."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Final

from rodex_registry.identity import RodexRuntimeId, parse_rodex_runtime_id

PROCESS_RECEIPT_PROTOCOL: Final = "rodex-process-receipt-v1"
PROCESS_KINDS: Final = frozenset({"app-server", "native-tui"})
_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_RECEIPT_PATTERN = "rodexd-v1-process-*.json"


class ProcessReceiptError(RuntimeError):
    """A daemon child ownership receipt could not be trusted."""


class RuntimeProcessReceipts:
    """Persist and reconcile daemon-owned process groups by exact incarnation."""

    def __init__(self, runtime_root: Path) -> None:
        self._runtime_root = runtime_root

    def record(self, kind: str, runtime_id: RodexRuntimeId, operation_id: str, process: Any) -> None:
        path = self._path(kind, runtime_id)
        pid = getattr(process, "pid", None)
        if type(pid) is not int or pid <= 0 or process.poll() is not None:
            raise ProcessReceiptError("cannot record an exited daemon child")
        state = _read_process_state(pid)
        if state["uid"] != os.getuid() or state["process_group_id"] != pid:
            raise ProcessReceiptError("daemon child does not own its expected process group")
        payload = {
            "protocol": PROCESS_RECEIPT_PROTOCOL,
            "kind": kind,
            "runtime_id": str(parse_rodex_runtime_id(runtime_id)),
            "operation_id": _validated_operation_id(operation_id),
            "pid": pid,
            "process_group_id": pid,
            "start_time_ticks": state["start_time_ticks"],
            "uid": os.getuid(),
        }
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            content = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode() + b"\n"
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise ProcessReceiptError("daemon child receipt write did not make progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, path)
            _fsync_directory(self._runtime_root)
        finally:
            temporary.unlink(missing_ok=True)

    def release(self, kind: str, runtime_id: RodexRuntimeId, operation_id: str, process: Any) -> None:
        path = self._path(kind, runtime_id)
        try:
            receipt = self._read(path)
        except FileNotFoundError:
            return
        if receipt["operation_id"] != _validated_operation_id(operation_id) or receipt["pid"] != getattr(
            process, "pid", None
        ):
            raise ProcessReceiptError("daemon child receipt changed before release")
        path.unlink()
        _fsync_directory(self._runtime_root)

    def reconcile(self) -> None:
        for path in sorted(self._runtime_root.glob(_RECEIPT_PATTERN)):
            receipt = self._read(path)
            pid = receipt["pid"]
            if pid == os.getpid():
                raise ProcessReceiptError("process receipt points at the current daemon")
            try:
                process_descriptor = os.pidfd_open(pid)
            except ProcessLookupError:
                path.unlink()
                continue
            try:
                try:
                    state = _read_process_state(pid)
                except ProcessLookupError:
                    path.unlink()
                    continue
                if (
                    state["uid"] != receipt["uid"]
                    or state["start_time_ticks"] != receipt["start_time_ticks"]
                    or state["process_group_id"] != receipt["process_group_id"]
                ):
                    path.unlink()
                    continue
                _terminate_exact_process_group(receipt, process_descriptor)
            finally:
                os.close(process_descriptor)
            path.unlink()
        _fsync_directory(self._runtime_root)

    def _path(self, kind: str, runtime_id: RodexRuntimeId) -> Path:
        if kind not in PROCESS_KINDS:
            raise ValueError("unknown daemon child process kind")
        return self._runtime_root / f"rodexd-v1-process-{parse_rodex_runtime_id(runtime_id)}-{kind}.json"

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError as error:
            if isinstance(error, FileNotFoundError):
                raise
            raise ProcessReceiptError("daemon child receipt could not be opened exactly") from error
        try:
            state = os.fstat(descriptor)
            if not stat.S_ISREG(state.st_mode) or state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o600:
                raise ProcessReceiptError("daemon child receipt is not a private current-user file")
            with os.fdopen(os.dup(descriptor), encoding="utf-8") as receipt_file:
                payload = json.load(receipt_file)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProcessReceiptError("daemon child receipt is malformed") from error
        finally:
            os.close(descriptor)
        expected = {
            "protocol",
            "kind",
            "runtime_id",
            "operation_id",
            "pid",
            "process_group_id",
            "start_time_ticks",
            "uid",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ProcessReceiptError("daemon child receipt fields do not match the current contract")
        if payload["protocol"] != PROCESS_RECEIPT_PROTOCOL or payload["kind"] not in PROCESS_KINDS:
            raise ProcessReceiptError("daemon child receipt contract is incompatible")
        parse_rodex_runtime_id(payload["runtime_id"])
        _validated_operation_id(payload["operation_id"])
        process_identity_fields = ("pid", "process_group_id", "start_time_ticks")
        if any(type(payload[name]) is not int or payload[name] <= 0 for name in process_identity_fields):
            raise ProcessReceiptError("daemon child receipt process identity is invalid")
        if type(payload["uid"]) is not int or payload["uid"] != os.getuid():
            raise ProcessReceiptError("daemon child receipt user identity is invalid")
        if payload["process_group_id"] != payload["pid"]:
            raise ProcessReceiptError("daemon child receipt does not own a process group")
        return payload


def _validated_operation_id(operation_id: str) -> str:
    if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
        raise ValueError("operation ID must be 32 lowercase hexadecimal characters")
    return operation_id


def _read_process_state(pid: int) -> dict[str, int]:
    try:
        descriptor = os.pidfd_open(pid)
        stat_text = Path(f"/proc/{pid}/stat").read_text()
        uid = Path(f"/proc/{pid}").stat().st_uid
    except FileNotFoundError as error:
        raise ProcessLookupError(pid) from error
    try:
        fields = stat_text.rsplit(")", 1)[1].split()
        process_group_id = int(fields[2])
        start_time_ticks = int(fields[19])
        if os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
            raise ProcessLookupError(pid)
    except (IndexError, ValueError) as error:
        raise ProcessReceiptError("could not parse daemon child process identity") from error
    except ChildProcessError:
        # waitid(P_PIDFD) is permitted only for children; a live non-child is
        # still pinned by the pidfd and verified through /proc.
        pass
    finally:
        os.close(descriptor)
    return {
        "uid": uid,
        "process_group_id": process_group_id,
        "start_time_ticks": start_time_ticks,
    }


def _terminate_exact_process_group(receipt: dict[str, Any], process_descriptor: int) -> None:
    process_group_id = receipt["process_group_id"]
    # Stop the exact retained leader before addressing its numeric process group.
    # This pins the verified group against exit/reuse while group-wide shutdown runs.
    with suppress(ProcessLookupError):
        signal.pidfd_send_signal(process_descriptor, signal.SIGSTOP)
    with suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGTERM)
    with suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGCONT)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _process_group_has_live_members(process_group_id):
            return
        time.sleep(0.02)
    with suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGKILL)


def _process_group_has_live_members(process_group_id: int) -> bool:
    for process_path in Path("/proc").glob("[0-9]*"):
        try:
            fields = (process_path / "stat").read_text().rsplit(")", 1)[1].split()
            state, observed_process_group = fields[0], int(fields[2])
        except (FileNotFoundError, PermissionError, IndexError, ValueError):
            continue
        if observed_process_group == process_group_id and state != "Z":
            return True
    return False


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
