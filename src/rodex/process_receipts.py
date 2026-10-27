"""Exact process-group receipts for daemon crash reconciliation."""

from __future__ import annotations

import errno
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import RLock
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


# Failed construction may leave no gateway to retain its owner. Keep capabilities
# reachable until an explicit finalizer proves extinction and retires the receipt.
_RETAINED_GROUPS: set[OwnedProcessGroup] = set()
_RECONCILED_GROUPS: dict[Path, OwnedProcessGroup] = {}


class OwnedProcessGroup:
    """One gated child, authenticated group capability, and cleanup obligation."""

    def __init__(
        self,
        on_started: Callable[[OwnedProcessGroup], None],
        on_stopped: Callable[[OwnedProcessGroup], None],
    ) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self._reconciled_pid = 0
        self.descriptor = -1
        self.identity: dict[str, int] | None = None
        self.extinct = False
        self.closed = False
        self._announced = False
        self._on_started = on_started
        self._on_stopped = on_stopped
        self._lock = RLock()

    @property
    def pid(self) -> int:
        return self.process.pid if self.process is not None else self._reconciled_pid

    @classmethod
    def adopt_authenticated(
        cls,
        pid: int,
        descriptor: int,
        identity: dict[str, int],
        on_stopped: Callable[[OwnedProcessGroup], None],
    ) -> OwnedProcessGroup:
        """Keep reacquired crash-reconciliation authority through later failures."""
        owner = cls(lambda _group: None, on_stopped)
        owner._reconciled_pid = pid
        owner.descriptor = descriptor
        owner.identity = identity
        owner._announced = True
        _RETAINED_GROUPS.add(owner)
        return owner

    def start(
        self,
        command: Sequence[str],
        *,
        terminal: bool = False,
        process_spawner: Callable[..., subprocess.Popen[bytes]] | None = None,
        python_executable: str = sys.executable,
        bind_process: Callable[[subprocess.Popen[bytes]], None] = lambda _process: None,
        **options: Any,
    ) -> subprocess.Popen[bytes]:
        with self._lock:
            if self.process is not None or self.closed:
                raise ProcessReceiptError("process-group owner cannot be reused")
            gate_read, gate_write = os.pipe2(os.O_CLOEXEC)
            try:
                try:
                    self._spawn_process = subprocess.Popen if process_spawner is None else process_spawner
                    self.process = self._spawn_process(
                        [
                            python_executable,
                            "-I",
                            "-m",
                            "rodex.terminal_exec" if terminal else "rodex.process_guard",
                            "--parent-pid",
                            str(os.getpid()),
                            "--start-gate-fd",
                            str(gate_read),
                            "--",
                            *command,
                        ],
                        start_new_session=True,
                        pass_fds=(gate_read,),
                        **options,
                    )
                    _RETAINED_GROUPS.add(self)
                    self.descriptor = os.pidfd_open(self.pid)
                    self.identity = _read_process_state_from_descriptor(self.pid, self.descriptor)
                    if self.identity["uid"] != os.getuid() or self.identity["process_group_id"] != self.pid:
                        self.identity = None
                        raise ProcessReceiptError("gated child does not own its expected process group")
                    # Mark before publication: a callback can fail after its write.
                    self._announced = True
                    self._on_started(self)
                    bind_process(self.process)
                    os.write(gate_write, b"1")
                    return self.process
                finally:
                    os.close(gate_read)
                    os.close(gate_write)
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        """Retain capability on any failure, including receipt retirement failure."""
        with self._lock:
            if self.closed:
                return
            if (self.process is not None or self.descriptor >= 0) and not self.extinct:
                if self.identity is None:
                    # Admission failed before the execution gate opened. Only the
                    # un-reaped direct helper can exist; EOF makes it exit unused.
                    assert self.process is not None
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
                else:
                    _terminate_exact_process_group(
                        self.descriptor, reap=self.process.poll if self.process is not None else lambda: None
                    )
                self.extinct = True
            if self._announced:
                self._on_stopped(self)
                self._announced = False
            if self.descriptor >= 0:
                os.close(self.descriptor)
                self.descriptor = -1
            self.closed = True
            _RETAINED_GROUPS.discard(self)


class RuntimeProcessLifecycle:
    """Serialize creation, replacement and finalization of one runtime's groups."""

    def __init__(
        self,
        on_started: Callable[[str, OwnedProcessGroup], None] = lambda _kind, _group: None,
        on_stopped: Callable[[str, OwnedProcessGroup], None] = lambda _kind, _group: None,
    ) -> None:
        self._groups: dict[str, OwnedProcessGroup] = {}
        self._on_started = on_started
        self._on_stopped = on_stopped
        self._lock = RLock()

    def start(
        self,
        kind: str,
        command: Sequence[str],
        *,
        on_acquired: Callable[[OwnedProcessGroup], None] = lambda _group: None,
        **options: Any,
    ) -> subprocess.Popen[bytes]:
        with self._lock:
            if kind in self._groups:
                raise ProcessReceiptError(f"{kind} process group still has an unresolved cleanup obligation")
            owner = OwnedProcessGroup(
                lambda group: self._on_started(kind, group), lambda group: self._on_stopped(kind, group)
            )
            self._groups[kind] = owner
            try:
                on_acquired(owner)
                return owner.start(command, **options)
            except BaseException:
                try:
                    owner.close()
                finally:
                    if owner.closed:
                        del self._groups[kind]
                raise

    def duplicate_exit_descriptor(self, kind: str) -> int:
        """Give the relay independent exit readiness while retaining group authority."""
        with self._lock:
            return os.dup(self._groups[kind].descriptor)

    def stop(self, kind: str, *, expected: OwnedProcessGroup | None = None) -> None:
        with self._lock:
            owner = self._groups.get(kind)
            if expected is not None and owner is not expected:
                if expected.closed:
                    return
                raise ProcessReceiptError("process group changed before exact finalization")
            if owner is not None:
                owner.close()
                del self._groups[kind]

    def close(self) -> None:
        with self._lock:
            errors = []
            for kind in tuple(reversed(self._groups)):
                try:
                    self.stop(kind)
                except Exception as error:
                    errors.append(error)
            if errors:
                raise ProcessReceiptError(f"runtime process cleanup remains unresolved: {errors}") from errors[0]


class RuntimeProcessReceipts:
    """Persist and reconcile daemon-owned process groups by exact incarnation."""

    def __init__(self, runtime_root: Path, *, catalog: bool = False) -> None:
        self._runtime_root = runtime_root
        self._catalog = catalog
        self._pending_retirements: dict[Path, dict[str, Any]] = {}

    def record(self, kind: str, runtime_id: RodexRuntimeId, operation_id: str, process: Any) -> None:
        path = self._path(kind, runtime_id)
        pid = getattr(process, "pid", None)
        if type(pid) is not int or pid <= 0:
            raise ProcessReceiptError("cannot record an exited daemon child")
        state = process.identity if isinstance(process, OwnedProcessGroup) else _read_process_state(pid)
        if state is None:
            raise ProcessReceiptError("cannot record an unauthenticated process group")
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
        self._publish(path, payload)

    def _publish(self, path: Path, payload: dict[str, Any]) -> None:
        """Publish without replacement, including restoration after failed retirement."""
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
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
            # Publication may never overwrite an unresolved predecessor receipt.
            os.link(temporary, path)
            _fsync_directory(self._runtime_root)
        finally:
            temporary.unlink(missing_ok=True)

    def release(self, kind: str, runtime_id: RodexRuntimeId, operation_id: str, process: Any) -> None:
        path = self._path(kind, runtime_id)
        try:
            receipt = self._read(path)
        except FileNotFoundError:
            receipt = self._pending_retirements.get(path)
            if receipt is None:
                return
            self._publish(path, receipt)
        if isinstance(process, OwnedProcessGroup):
            if not process.extinct:
                raise ProcessReceiptError("process receipt cannot retire before group extinction")
            identity = process.identity
        else:
            _require_unowned_group_absent(process.pid)
            identity = None
        if (
            receipt["operation_id"] != _validated_operation_id(operation_id)
            or receipt["runtime_id"] != str(runtime_id)
            or receipt["kind"] != kind
            or receipt["pid"] != getattr(process, "pid", None)
            or (identity is not None and any(receipt[key] != value for key, value in identity.items()))
        ):
            raise ProcessReceiptError("daemon child receipt changed before release")
        self._retire(path, receipt)

    def _retire(self, path: Path, receipt: dict[str, Any]) -> None:
        if path in self._pending_retirements and not path.exists():
            self._publish(path, receipt)
        if self._read(path) != receipt:
            raise ProcessReceiptError("process receipt changed before retirement")
        self._pending_retirements[path] = receipt
        path.unlink()
        try:
            _fsync_directory(self._runtime_root)
        except OSError:
            # Re-publish the exact obligation without replacing a competing file.
            # Even if directory sync still fails, publication retains the receipt
            # and the live owner keeps its pidfd for the next finalization attempt.
            self._publish(path, receipt)
            raise
        del self._pending_retirements[path]

    def reconcile(self) -> None:
        prefix = f"{RODEX_IMPLEMENTATION_KEY}.{'catalog-' if self._catalog else ''}process-"
        paths = set(self._runtime_root.glob(f"{prefix}*.json"))
        paths.update(
            path for path in _RECONCILED_GROUPS if path.parent == self._runtime_root and path.name.startswith(prefix)
        )
        for path in sorted(paths):
            retained = _RECONCILED_GROUPS.get(path)
            if retained is not None:
                retained.close()
                del _RECONCILED_GROUPS[path]
                continue
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
                self._retire(path, receipt)
                continue
            try:
                try:
                    state = _read_process_state_from_descriptor(pid, process_descriptor)
                except ProcessLookupError:
                    _require_unowned_group_absent(receipt["process_group_id"])
                    self._retire(path, receipt)
                    continue
                if (
                    state["uid"] != receipt["uid"]
                    or state["start_time_ticks"] != receipt["start_time_ticks"]
                    or state["process_group_id"] != receipt["process_group_id"]
                ):
                    _require_unowned_group_absent(receipt["process_group_id"])
                    self._retire(path, receipt)
                    continue
                owner = OwnedProcessGroup.adopt_authenticated(
                    pid,
                    process_descriptor,
                    state,
                    lambda _group, path=path, receipt=receipt: self._retire(path, receipt),
                )
                _RECONCILED_GROUPS[path] = owner
                process_descriptor = -1  # Ownership transferred before any termination.
                owner.close()
                del _RECONCILED_GROUPS[path]
            finally:
                if process_descriptor >= 0:
                    os.close(process_descriptor)
        _fsync_directory(self._runtime_root)

    def _path(self, kind: str, runtime_id: RodexRuntimeId) -> Path:
        if kind not in PROCESS_KINDS:
            raise ValueError("unknown daemon child process kind")
        return self._runtime_root / (
            f"{RODEX_IMPLEMENTATION_KEY}.{'catalog-' if self._catalog else ''}process-"
            f"{parse_rodex_runtime_id(runtime_id)}-{kind}.json"
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


def _wait_for_exact_group_exit(process_descriptor: int, timeout_seconds: float, reap: Callable[[], object]) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        reap()
        if not _signal_exact_process_group(process_descriptor, 0):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.02, remaining))


def _terminate_exact_process_group(process_descriptor: int, *, reap: Callable[[], object] = lambda: None) -> None:
    reap()
    if not _signal_exact_process_group(process_descriptor, signal.SIGTERM):
        return
    # Resume already-stopped members so they can handle graceful termination.
    # Group identity no longer depends on stopping or retaining a live leader.
    if not _signal_exact_process_group(process_descriptor, signal.SIGCONT):
        return
    if _wait_for_exact_group_exit(process_descriptor, PROCESS_GROUP_TERM_GRACE_SECONDS, reap):
        return
    if not _signal_exact_process_group(process_descriptor, signal.SIGKILL):
        return
    if not _wait_for_exact_group_exit(process_descriptor, PROCESS_GROUP_EXIT_CONFIRM_SECONDS, reap):
        raise ProcessReceiptError("process group remains unreconciled: exit was not confirmed after SIGKILL")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
