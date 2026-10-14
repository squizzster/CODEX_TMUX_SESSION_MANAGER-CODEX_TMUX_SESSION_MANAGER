"""Exact process-group receipts for daemon crash reconciliation."""

from __future__ import annotations

import errno
import json
import os
import re
import select
import signal
import stat
import time
from pathlib import Path
from typing import Any, Final

from rodex_registry.identity import RodexRuntimeId, parse_rodex_runtime_id

from .implementation_identity import RODEX_IMPLEMENTATION_KEY

PROCESS_RECEIPT_PROTOCOL: Final = "rodex-process-receipt-v3"
PROCESS_KINDS: Final = frozenset({"app-server", "native-tui"})
_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
PROCESS_RECEIPT_PATTERN: Final = f"{RODEX_IMPLEMENTATION_KEY}.process-*.json"
# Linux UAPI since 6.9 (also available in some enterprise backports). Python
# 3.11 can pass the flag even though it does not expose a named constant.
PIDFD_SIGNAL_PROCESS_GROUP: Final = 1 << 2
PROCESS_GROUP_TERM_GRACE_SECONDS: Final = 2.0
PROCESS_GROUP_EXIT_CONFIRM_SECONDS: Final = 2.0


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
        for path in sorted(self._runtime_root.glob(PROCESS_RECEIPT_PATTERN)):
            receipt = self._read(path)
            pid = receipt["pid"]
            if pid == os.getpid():
                raise ProcessReceiptError("process receipt points at the current daemon")
            try:
                process_descriptor = os.pidfd_open(pid)
            except OSError as error:
                # Some kernels return EINVAL when only the process-group PID
                # remains after leader reap. That is not proof of group exit.
                if not isinstance(error, ProcessLookupError) and error.errno != errno.EINVAL:
                    raise ProcessReceiptError(
                        "process group remains unreconciled: leader pidfd could not be acquired"
                    ) from error
                _require_unowned_group_absent(receipt["process_group_id"])
                path.unlink()
                continue
            try:
                try:
                    state = _read_process_state_from_descriptor(pid, process_descriptor)
                except ProcessLookupError:
                    _require_unowned_group_absent(receipt["process_group_id"])
                    path.unlink()
                    continue
                if (
                    state["uid"] != receipt["uid"]
                    or state["start_time_ticks"] != receipt["start_time_ticks"]
                    or state["process_group_id"] != receipt["process_group_id"]
                ):
                    _require_unowned_group_absent(receipt["process_group_id"])
                    path.unlink()
                    continue
                _terminate_exact_process_group(process_descriptor)
            finally:
                os.close(process_descriptor)
            path.unlink()
        _fsync_directory(self._runtime_root)

    def _path(self, kind: str, runtime_id: RodexRuntimeId) -> Path:
        if kind not in PROCESS_KINDS:
            raise ValueError("unknown daemon child process kind")
        return self._runtime_root / (
            f"{RODEX_IMPLEMENTATION_KEY}.process-{parse_rodex_runtime_id(runtime_id)}-{kind}.json"
        )

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
    descriptor = os.pidfd_open(pid)
    try:
        return _read_process_state_from_descriptor(pid, descriptor)
    finally:
        os.close(descriptor)


def _read_process_state_from_descriptor(pid: int, descriptor: int) -> dict[str, int]:
    """Authenticate the numeric /proc sample against the retained live pidfd."""
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
        uid = Path(f"/proc/{pid}").stat().st_uid
        fields = stat_text.rsplit(")", 1)[1].split()
        process_group_id = int(fields[2])
        start_time_ticks = int(fields[19])
        readiness = select.poll()
        readiness.register(descriptor, select.POLLIN)
        events = readiness.poll(0)
        if any(mask & select.POLLNVAL for _fd, mask in events):
            raise ProcessReceiptError("daemon child pidfd is invalid")
        if events:
            # Unlike waitid, readiness also works for non-children. If the
            # retained process exited during /proc reads, that numeric sample
            # cannot authorize signals to a replacement process/group.
            raise ProcessLookupError(pid)
    except FileNotFoundError as error:
        raise ProcessLookupError(pid) from error
    except (IndexError, ValueError) as error:
        raise ProcessReceiptError("could not parse daemon child process identity") from error
    return {
        "uid": uid,
        "process_group_id": process_group_id,
        "start_time_ticks": start_time_ticks,
    }


def _require_unowned_group_absent(process_group_id: int) -> None:
    """A numeric lookup may prove absence, never grant termination authority."""
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return
    except OSError as error:
        raise ProcessReceiptError("process group remains unreconciled: group absence could not be verified") from error
    raise ProcessReceiptError("process group remains unreconciled: group exists without a verified leader")


def _signal_exact_process_group(process_descriptor: int, signum: int) -> bool:
    """Signal the pidfd's original group; false means that exact group is gone.

    Linux routes PIDTYPE_PGID through the fd's retained struct pid, including
    after leader reap. Never fall back to a numeric target on unsupported hosts.
    A successful send is not proof that every member received it or exited.
    """
    try:
        signal.pidfd_send_signal(process_descriptor, signum, None, PIDFD_SIGNAL_PROCESS_GROUP)
    except ProcessLookupError:
        return False
    except OSError as error:
        raise ProcessReceiptError(
            f"process group remains unreconciled: group-scoped pidfd signalling failed (errno={error.errno})"
        ) from error
    return True


def _wait_for_exact_group_exit(process_descriptor: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while _signal_exact_process_group(process_descriptor, 0):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))
    return True


def _terminate_exact_process_group(process_descriptor: int) -> None:
    if not _signal_exact_process_group(process_descriptor, signal.SIGTERM):
        return
    # Resume already-stopped members so they can handle graceful termination.
    # Group identity no longer depends on stopping or retaining a live leader.
    if not _signal_exact_process_group(process_descriptor, signal.SIGCONT):
        return
    if _wait_for_exact_group_exit(process_descriptor, PROCESS_GROUP_TERM_GRACE_SECONDS):
        return
    if not _signal_exact_process_group(process_descriptor, signal.SIGKILL):
        return
    if not _wait_for_exact_group_exit(process_descriptor, PROCESS_GROUP_EXIT_CONFIRM_SECONDS):
        raise ProcessReceiptError("process group remains unreconciled: exit was not confirmed after SIGKILL")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
