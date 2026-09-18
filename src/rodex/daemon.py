"""Single shared Rodex runtime and analytics daemon."""

from __future__ import annotations

import argparse
import array
import json
import os
import re
import select
import signal
import socket
import struct
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from typing import Any, Final

from .analytics import SharedAnalyticsCoordinator
from .daemon_client import (
    DAEMON_MESSAGE_LIMIT_BYTES,
    RODEX_DAEMON_PROTOCOL,
    RODEX_RUNTIME_WAKE_REGISTRATION,
    RODEX_RUNTIME_WAKE_TERMINAL_RESIZE,
    daemon_socket_path,
    encode_daemon_message,
)
from .process_contracts import RuntimeServiceConfig
from .process_guard import LINUX_TASK_NAME_MAX_BYTES, set_current_linux_task_name
from .process_receipts import RuntimeProcessReceipts
from .runtime import admit_runtime_terminal_fd, run_runtime_service
from .runtime_endpoint import ExclusiveUnixEndpoint
from .tmux_session_capability import runtime_tmux_socket_name
from .version import RODEX_VERSION

_OPERATION_ID: Final = re.compile(r"[0-9a-f]{32}")
_RODEX_RELEASE_VERSION: Final = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:(?P<prerelease>a|b|rc)(?P<prerelease_number>0|[1-9][0-9]*))?"
)


def _versioned_daemon_process_name(version: str) -> str:
    """Build a readable, untruncated Linux task name from a Rodex release."""
    match = _RODEX_RELEASE_VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"Rodex version cannot form a Linux daemon task name: {version!r}")

    major = match["major"]
    minor = match["minor"]
    patch = match["patch"]
    prerelease = match["prerelease"] or ""
    prerelease_number = match["prerelease_number"] or ""
    prerelease_suffix = f"{prerelease}{prerelease_number}"
    readable_release = f"{major}_{minor}"
    if patch != "0" or not prerelease:
        readable_release += f"_{patch}"

    candidates = (
        f"rodexd_v{readable_release}{prerelease_suffix}",
        f"rodexd_v{major}{minor}{patch}{prerelease_suffix}",
    )
    for candidate in candidates:
        if len(candidate.encode("ascii")) <= LINUX_TASK_NAME_MAX_BYTES:
            return candidate
    raise ValueError(f"Rodex version {version!r} cannot fit a {LINUX_TASK_NAME_MAX_BYTES}-byte Linux daemon task name")


RODEX_DAEMON_PROCESS_NAME: Final = _versioned_daemon_process_name(RODEX_VERSION)


class RodexDaemonServerError(RuntimeError):
    """A daemon request violated the current singular runtime contract."""


@dataclass(slots=True)
class _ManagedRuntime:
    operation_id: str
    config: RuntimeServiceConfig
    stop: Event = field(default_factory=Event)
    ready: Event = field(default_factory=Event)
    done: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)
    bridge_connection: socket.socket | None = None
    terminal_fd: int | None = None
    terminal_environment: dict[str, str] | None = None
    thread: Thread | None = None
    error: str | None = None
    supervisor_wake: Callable[[], None] | None = None
    terminal_resize: Callable[[], None] | None = None


class DaemonRuntimeManager:
    """Own every runtime transition through one exact state map."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        analytics: SharedAnalyticsCoordinator | None = None,
        process_receipts: RuntimeProcessReceipts | None = None,
    ) -> None:
        self._runtime_root = runtime_root
        self._runtimes: dict[str, _ManagedRuntime] = {}
        self._lock = Lock()
        self._closing = False
        self._process_receipts = process_receipts or RuntimeProcessReceipts(runtime_root)
        self._process_receipts.reconcile()
        self._analytics = analytics or SharedAnalyticsCoordinator()
        self._analytics.start()

    def reserve(self, operation_id: str, config: RuntimeServiceConfig) -> None:
        _require_operation_id(operation_id)
        self._validate_runtime_root(config)
        runtime_id = str(config.runtime_id)
        with self._lock:
            if self._closing:
                raise RodexDaemonServerError("daemon is shutting down")
            existing = self._runtimes.get(runtime_id)
            if existing is not None:
                if existing.operation_id == operation_id and existing.config == config:
                    return
                raise RodexDaemonServerError("runtime identity is already reserved by another operation")
            self._analytics.reserve(config.analytics)
            self._runtimes[runtime_id] = _ManagedRuntime(operation_id, config)

    def bind_terminal(
        self,
        operation_id: str,
        runtime_id: str,
        tmux_server_id: str,
        tmux_pane_target: str,
        terminal_fd: int,
        bridge_connection: socket.socket,
        *,
        peer_pid: int,
    ) -> _ManagedRuntime:
        context = self._exact_context(operation_id, runtime_id)
        config = context.config
        if config.tmux_server_id != tmux_server_id or config.tmux_pane_target != tmux_pane_target:
            raise RodexDaemonServerError("terminal bridge capability disagrees with its reservation")
        _capability, terminal_environment = admit_runtime_terminal_fd(config, terminal_fd, peer_pid=peer_pid)
        with context.lock:
            if context.bridge_connection is not None or context.thread is not None:
                raise RodexDaemonServerError("terminal bridge was already consumed")
            context.bridge_connection = bridge_connection
            context.terminal_fd = terminal_fd
            context.terminal_environment = terminal_environment
        return context

    def start_bound_runtime(self, context: _ManagedRuntime) -> None:
        with context.lock:
            terminal_fd = context.terminal_fd
            terminal_environment = context.terminal_environment
            if terminal_fd is None or terminal_environment is None or context.bridge_connection is None:
                raise RodexDaemonServerError("runtime has no admitted terminal bridge")
            if context.thread is not None:
                raise RodexDaemonServerError("runtime service was already started")
            context.thread = Thread(
                target=self._run_context,
                args=(context, terminal_fd, terminal_environment),
                name=f"rodex-runtime-{context.config.runtime_id}",
                daemon=True,
            )
            context.thread.start()

    def await_ready(self, operation_id: str, runtime_id: str, timeout_seconds: float) -> None:
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise RodexDaemonServerError("ready timeout must be positive")
        context = self._exact_context(operation_id, runtime_id)
        if not context.ready.wait(float(timeout_seconds)):
            raise RodexDaemonServerError("runtime service did not become ready")
        with context.lock:
            error = context.error
        if error is not None:
            raise RodexDaemonServerError(error)

    def stop_runtime(self, operation_id: str, runtime_id: str) -> None:
        context = self._exact_context(operation_id, runtime_id)
        self._stop_context(context)

    def wake_runtime(self, runtime_id: str, cause: str) -> None:
        """Deliver a same-user hint; the runtime revalidates authoritative state."""
        with self._lock:
            context = self._runtimes.get(runtime_id)
        if context is None:
            raise RodexDaemonServerError("runtime identity is not reserved")
        with context.lock:
            callback = (
                context.supervisor_wake
                if cause == RODEX_RUNTIME_WAKE_REGISTRATION
                else context.terminal_resize
                if cause == RODEX_RUNTIME_WAKE_TERMINAL_RESIZE
                else None
            )
        if cause not in {RODEX_RUNTIME_WAKE_REGISTRATION, RODEX_RUNTIME_WAKE_TERMINAL_RESIZE}:
            raise RodexDaemonServerError("runtime wake cause is invalid")
        if callback is not None:
            callback()

    def close(self) -> None:
        with self._lock:
            self._closing = True
            contexts = tuple(self._runtimes.values())
        for context in contexts:
            self._stop_context(context)
        self._analytics.close()

    def _run_context(
        self,
        context: _ManagedRuntime,
        terminal_fd: int,
        terminal_environment: dict[str, str],
    ) -> None:
        try:
            run_runtime_service(
                context.config,
                terminal_fd=terminal_fd,
                terminal_environment=terminal_environment,
                stop=context.stop,
                on_started=context.ready.set,
                on_analytics_activated=self._analytics.activate,
                on_analytics_event=lambda event: self._analytics.observe_protocol_event(
                    str(context.config.runtime_id), event
                ),
                on_process_started=lambda kind, process: self._process_receipts.record(
                    kind,
                    context.config.runtime_id,
                    context.operation_id,
                    process,
                ),
                on_process_stopped=lambda kind, process: self._process_receipts.release(
                    kind,
                    context.config.runtime_id,
                    context.operation_id,
                    process,
                ),
                on_terminal_gateway_ready=lambda supervisor_wake, terminal_resize: self._bind_runtime_controls(
                    context,
                    supervisor_wake,
                    terminal_resize,
                ),
            )
        except BaseException as error:
            with context.lock:
                context.error = f"runtime service failed: {error}"
            context.ready.set()
        finally:
            self._analytics.retire(str(context.config.runtime_id))
            context.done.set()
            with context.lock:
                context.supervisor_wake = None
                context.terminal_resize = None
                bridge, context.bridge_connection = context.bridge_connection, None
            if bridge is not None:
                bridge.close()

    def _stop_context(self, context: _ManagedRuntime) -> None:
        context.stop.set()
        with context.lock:
            thread = context.thread
            bridge = context.bridge_connection
            supervisor_wake = context.supervisor_wake
        if supervisor_wake is not None:
            supervisor_wake()
        if thread is not None:
            thread.join(timeout=10)
        if bridge is not None:
            with context.lock:
                if context.bridge_connection is bridge:
                    context.bridge_connection = None
            bridge.close()
        with context.lock:
            terminal_fd, context.terminal_fd = context.terminal_fd, None
            context.terminal_environment = None
        if terminal_fd is not None and (thread is None or thread.is_alive()):
            with suppress(OSError):
                os.close(terminal_fd)

    @staticmethod
    def _bind_runtime_controls(
        context: _ManagedRuntime,
        supervisor_wake: Callable[[], None],
        terminal_resize: Callable[[], None],
    ) -> None:
        with context.lock:
            context.supervisor_wake = supervisor_wake
            context.terminal_resize = terminal_resize
            stopped = context.stop.is_set()
        if stopped:
            supervisor_wake()

    def _exact_context(self, operation_id: str, runtime_id: str) -> _ManagedRuntime:
        _require_operation_id(operation_id)
        with self._lock:
            context = self._runtimes.get(runtime_id)
        if context is None or context.operation_id != operation_id:
            raise RodexDaemonServerError("runtime operation does not match an exact reservation")
        return context

    def _validate_runtime_root(self, config: RuntimeServiceConfig) -> None:
        root = self._runtime_root
        runtime_id = str(config.runtime_id)
        expected = {
            config.tmux_server_socket_path: root / runtime_tmux_socket_name(config.runtime_id),
            config.app_server_socket_path: root / f"app-{runtime_id}.sock",
            config.app_server_log_path: root / f"app-{runtime_id}.log",
            config.protocol_proxy_socket_path: root / f"proxy-{runtime_id}.sock",
            config.protocol_event_socket_path: root / f"events-{runtime_id}.sock",
        }
        if any(actual != wanted for actual, wanted in expected.items()):
            raise RodexDaemonServerError("runtime endpoints are outside their canonical daemon root")


class RodexDaemonServer:
    """Admit same-user requests and transfer exact pane TTY ownership."""

    def __init__(self, runtime_root: Path) -> None:
        self._runtime_root = runtime_root
        self._endpoint = ExclusiveUnixEndpoint(daemon_socket_path(runtime_root))
        self._manager: DaemonRuntimeManager | None = None
        self._stop = Event()
        self._wake_fd = os.eventfd(0, os.EFD_CLOEXEC | os.EFD_NONBLOCK)
        self._workers: set[Thread] = set()
        self._workers_lock = Lock()

    def stop(self) -> None:
        self._stop.set()
        with suppress(OSError):
            os.eventfd_write(self._wake_fd, 1)

    def run(self) -> int:
        try:
            listener = self._endpoint.open()
        except BaseException:
            with suppress(OSError):
                os.close(self._wake_fd)
            self._wake_fd = -1
            raise
        try:
            # The single daemon endpoint is the root ownership claim. Reconcile
            # multi-runtime process receipts only after that claim is exclusive.
            self._manager = DaemonRuntimeManager(self._runtime_root)
            while not self._stop.is_set():
                readable, _writable, _exceptional = select.select((listener, self._wake_fd), (), ())
                if self._wake_fd in readable:
                    with suppress(BlockingIOError, OSError):
                        os.eventfd_read(self._wake_fd)
                    if self._stop.is_set():
                        break
                if listener not in readable:
                    continue
                connection, _address = listener.accept()
                worker = Thread(target=self._handle_connection, args=(connection,), daemon=True)
                with self._workers_lock:
                    self._workers.add(worker)
                worker.start()
        finally:
            if self._manager is not None:
                self._manager.close()
            self._endpoint.close()
            with self._workers_lock:
                workers = tuple(self._workers)
            for worker in workers:
                worker.join(timeout=2)
            with suppress(OSError):
                os.close(self._wake_fd)
            self._wake_fd = -1
        return 0

    def _handle_connection(self, connection: socket.socket) -> None:
        retained = False
        descriptors: tuple[int, ...] = ()
        try:
            manager = self._manager
            if manager is None:
                raise RodexDaemonServerError("daemon runtime manager is not ready")
            peer_pid, peer_uid, _peer_gid = struct.unpack(
                "3i",
                connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
            )
            if peer_uid != os.getuid():
                raise RodexDaemonServerError("daemon peer is not the current user")
            request, descriptors = _receive_request(connection)
            if request.get("protocol") != RODEX_DAEMON_PROTOCOL:
                raise RodexDaemonServerError("daemon protocol does not match this generation")
            operation = request.get("operation")
            if operation == "ping":
                _require_fields(request, {"protocol", "operation"})
            elif operation == "reserve":
                _require_fields(request, {"protocol", "operation", "operation_id", "runtime"})
                runtime = request.get("runtime")
                if not isinstance(runtime, dict):
                    raise RodexDaemonServerError("runtime reservation must be an object")
                manager.reserve(_text(request, "operation_id"), RuntimeServiceConfig.from_payload(runtime))
            elif operation == "await_ready":
                _require_fields(
                    request,
                    {"protocol", "operation", "operation_id", "runtime_id", "timeout_seconds"},
                )
                manager.await_ready(
                    _text(request, "operation_id"),
                    _text(request, "runtime_id"),
                    request.get("timeout_seconds"),
                )
            elif operation == "stop":
                _require_fields(request, {"protocol", "operation", "operation_id", "runtime_id"})
                manager.stop_runtime(_text(request, "operation_id"), _text(request, "runtime_id"))
            elif operation == "wake_runtime":
                _require_fields(request, {"protocol", "operation", "runtime_id", "cause"})
                manager.wake_runtime(_text(request, "runtime_id"), _text(request, "cause"))
            elif operation == "bind_terminal":
                _require_fields(
                    request,
                    {
                        "protocol",
                        "operation",
                        "operation_id",
                        "runtime_id",
                        "tmux_server_id",
                        "tmux_pane_target",
                    },
                )
                if len(descriptors) != 1:
                    raise RodexDaemonServerError("terminal bridge must pass exactly one descriptor")
                terminal_fd = descriptors[0]
                context = manager.bind_terminal(
                    _text(request, "operation_id"),
                    _text(request, "runtime_id"),
                    _text(request, "tmux_server_id"),
                    _text(request, "tmux_pane_target"),
                    terminal_fd,
                    connection,
                    peer_pid=peer_pid,
                )
                retained = True
                descriptors = ()
                try:
                    manager.start_bound_runtime(context)
                    connection.sendall(_success_response())
                except BaseException:
                    manager.stop_runtime(context.operation_id, str(context.config.runtime_id))
                    raise
                return
            else:
                raise RodexDaemonServerError("unknown daemon operation")
            if descriptors:
                raise RodexDaemonServerError("daemon operation did not permit passed descriptors")
            connection.sendall(_success_response())
        except BaseException as error:
            with suppress(OSError):
                connection.sendall(_error_response(str(error)))
        finally:
            for descriptor in descriptors:
                with suppress(OSError):
                    os.close(descriptor)
            if not retained:
                connection.close()
            with self._workers_lock:
                self._workers.discard(current_thread())


def _receive_request(connection: socket.socket) -> tuple[dict[str, Any], tuple[int, ...]]:
    content = bytearray()
    descriptors: list[int] = []
    first = True
    while len(content) <= DAEMON_MESSAGE_LIMIT_BYTES:
        if first:
            chunk, ancillary, _flags, _address = connection.recvmsg(
                min(65536, DAEMON_MESSAGE_LIMIT_BYTES + 1),
                socket.CMSG_SPACE(array.array("i").itemsize * 4),
                socket.MSG_CMSG_CLOEXEC,
            )
            first = False
            for level, kind, data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    values.frombytes(data[: len(data) - (len(data) % values.itemsize)])
                    descriptors.extend(values)
        else:
            chunk = connection.recv(min(65536, DAEMON_MESSAGE_LIMIT_BYTES + 1 - len(content)))
        if not chunk:
            raise RodexDaemonServerError("daemon request closed before a complete message")
        content.extend(chunk)
        newline = content.find(b"\n")
        if newline >= 0:
            if content[newline + 1 :]:
                raise RodexDaemonServerError("daemon request contained trailing data")
            try:
                payload = json.loads(content[:newline])
            except (UnicodeError, json.JSONDecodeError) as error:
                raise RodexDaemonServerError("daemon request was not valid JSON") from error
            if not isinstance(payload, dict):
                raise RodexDaemonServerError("daemon request must be an object")
            return payload, tuple(descriptors)
    raise RodexDaemonServerError("daemon request exceeds its bounded contract")


def _require_fields(payload: dict[str, Any], expected: set[str]) -> None:
    if set(payload) != expected:
        raise RodexDaemonServerError("daemon request fields do not match the current contract")


def _text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise RodexDaemonServerError(f"{name} must be non-empty text")
    return value


def _require_operation_id(operation_id: str) -> None:
    if _OPERATION_ID.fullmatch(operation_id) is None:
        raise RodexDaemonServerError("operation ID must be 32 lowercase hexadecimal characters")


def _success_response() -> bytes:
    return encode_daemon_message({"protocol": RODEX_DAEMON_PROTOCOL, "ok": True})


def _error_response(detail: str) -> bytes:
    return encode_daemon_message({"protocol": RODEX_DAEMON_PROTOCOL, "ok": False, "error": detail[:4096]})


def main() -> int:
    set_current_linux_task_name(RODEX_DAEMON_PROCESS_NAME)
    parser = argparse.ArgumentParser(prog=RODEX_DAEMON_PROCESS_NAME)
    parser.add_argument("--runtime-root", required=True, type=Path)
    arguments = parser.parse_args()
    runtime_root = arguments.runtime_root.expanduser().resolve()
    state = runtime_root.stat()
    if not runtime_root.is_dir() or state.st_uid != os.getuid() or state.st_mode & 0o077:
        parser.error("runtime root must be a private current-user directory")
    server = RodexDaemonServer(runtime_root)

    def request_stop(_signum: int, _frame: object) -> None:
        server.stop()

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, request_stop)
    return server.run()


if __name__ == "__main__":
    raise SystemExit(main())
