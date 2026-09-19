"""Private client for the single shared Rodex runtime daemon."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from .implementation_identity import RODEX_IMPLEMENTATION_ID
from .process_contracts import RuntimeServiceConfig

RODEX_DAEMON_PROTOCOL: Final = "rodex-daemon-v2"
RODEX_DAEMON_SOCKET_NAME: Final = "rodexd-v2.sock"
RODEX_DAEMON_LOG_NAME: Final = "rodexd-v2.log"
RODEX_DAEMON_START_LOCK_NAME: Final = "rodexd-v2.start.lock"
RODEX_DAEMON_IMPLEMENTATION_FIELD: Final = "implementation_id"
LEGACY_DAEMON_SOCKET_NAMES: Final = ("rodexd-v1.sock",)
RODEX_RUNTIME_WAKE_REGISTRATION: Final = "registration"
RODEX_RUNTIME_WAKE_TERMINAL_RESIZE: Final = "terminal_resize"
RODEX_RUNTIME_WAKE_CAUSES: Final = frozenset({RODEX_RUNTIME_WAKE_REGISTRATION, RODEX_RUNTIME_WAKE_TERMINAL_RESIZE})
DAEMON_MESSAGE_LIMIT_BYTES: Final = 1024 * 1024
DAEMON_START_TIMEOUT_SECONDS: Final = 10.0


class RodexDaemonError(RuntimeError):
    """The shared daemon could not execute an exact runtime operation."""


def daemon_socket_path(runtime_root: Path) -> Path:
    return runtime_root / RODEX_DAEMON_SOCKET_NAME


def encode_daemon_message(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    if len(encoded) > DAEMON_MESSAGE_LIMIT_BYTES:
        raise RodexDaemonError("daemon message exceeds its bounded contract")
    return encoded


def receive_daemon_message(connection: socket.socket) -> dict[str, Any]:
    content = bytearray()
    while len(content) <= DAEMON_MESSAGE_LIMIT_BYTES:
        chunk = connection.recv(min(65536, DAEMON_MESSAGE_LIMIT_BYTES + 1 - len(content)))
        if not chunk:
            raise RodexDaemonError("daemon connection closed before a complete response")
        content.extend(chunk)
        newline = content.find(b"\n")
        if newline >= 0:
            if content[newline + 1 :]:
                raise RodexDaemonError("daemon response contained trailing data")
            try:
                payload = json.loads(content[:newline])
            except (UnicodeError, json.JSONDecodeError) as error:
                raise RodexDaemonError("daemon response was not valid JSON") from error
            if not isinstance(payload, dict):
                raise RodexDaemonError("daemon response must be an object")
            return payload
    raise RodexDaemonError("daemon response exceeds its bounded contract")


class RodexDaemonClient:
    """Start, verify, and address one daemon for an exact runtime root."""

    def __init__(
        self,
        runtime_root: Path,
        python_executable: str,
        *,
        process_spawner: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.runtime_root = runtime_root
        self.socket_path = daemon_socket_path(runtime_root)
        self._python_executable = python_executable
        self._spawn_process = process_spawner
        self._monotonic = monotonic
        self._sleep = sleep

    def ensure_running(self) -> None:
        """Serialize first start and accept only the exact current daemon code."""
        self.runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.runtime_root / RODEX_DAEMON_START_LOCK_NAME
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._reject_live_legacy_daemon()
            if self._probe():
                return
            log_path = self.runtime_root / RODEX_DAEMON_LOG_NAME
            log_descriptor = os.open(
                log_path,
                os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            try:
                self._spawn_process(
                    [
                        self._python_executable,
                        "-I",
                        "-m",
                        "rodex.daemon",
                        "--runtime-root",
                        os.fspath(self.runtime_root),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log_descriptor,
                    stderr=log_descriptor,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                os.close(log_descriptor)
            deadline = self._monotonic() + DAEMON_START_TIMEOUT_SECONDS
            while self._monotonic() < deadline:
                if self._probe():
                    return
                self._sleep(0.05)
            raise RodexDaemonError("shared Rodex daemon did not become ready")
        finally:
            os.close(descriptor)

    def reserve(self, operation_id: str, config: RuntimeServiceConfig) -> None:
        self.ensure_running()
        self._request(
            {
                "protocol": RODEX_DAEMON_PROTOCOL,
                "operation": "reserve",
                "operation_id": operation_id,
                "runtime": config.to_payload(),
            }
        )

    def await_ready(self, operation_id: str, runtime_id: str, *, timeout_seconds: float) -> None:
        self._request(
            {
                "protocol": RODEX_DAEMON_PROTOCOL,
                "operation": "await_ready",
                "operation_id": operation_id,
                "runtime_id": runtime_id,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds + 1,
        )

    def stop(self, operation_id: str, runtime_id: str) -> str:
        """Acknowledge cancellation admission; a stopping service still owns its resources."""
        response = self._request(
            {
                "protocol": RODEX_DAEMON_PROTOCOL,
                "operation": "stop",
                "operation_id": operation_id,
                "runtime_id": runtime_id,
            }
        )
        state = response.get("state")
        if state not in {"stopping", "terminal"}:
            raise RodexDaemonError("daemon stop response lacks a valid completion state")
        return state

    def notify_runtime(self, runtime_id: str, cause: str) -> bool:
        """Wake one runtime after an external state transition.

        False means an already-running daemon predates this backwards-compatible
        protocol extension and still owns its legacy supervisory timer.
        """
        if cause not in RODEX_RUNTIME_WAKE_CAUSES:
            raise ValueError("runtime wake cause is invalid")
        try:
            self._request(
                {
                    "protocol": RODEX_DAEMON_PROTOCOL,
                    "operation": "wake_runtime",
                    "runtime_id": runtime_id,
                    "cause": cause,
                }
            )
        except RodexDaemonError as error:
            if str(error) == "unknown daemon operation":
                return False
            raise
        return True

    def _probe(self) -> bool:
        try:
            self._request({"protocol": RODEX_DAEMON_PROTOCOL, "operation": "ping"}, timeout_seconds=0.25)
        except (OSError, TimeoutError):
            return False
        return True

    def _reject_live_legacy_daemon(self) -> None:
        for socket_name in LEGACY_DAEMON_SOCKET_NAMES:
            legacy_path = self.runtime_root / socket_name
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(0.25)
                    connection.connect(os.fspath(legacy_path))
            except (FileNotFoundError, ConnectionRefusedError):
                continue
            except (OSError, TimeoutError) as error:
                raise RodexDaemonError(f"could not prove legacy daemon endpoint is inactive: {legacy_path}") from error
            raise RodexDaemonError(
                f"legacy Rodex daemon is still running at {legacy_path}; "
                "stop all old Rodex runtimes and that daemon before starting this release"
            )

    def _request(self, payload: dict[str, object], *, timeout_seconds: float = 5.0) -> dict[str, Any]:
        if RODEX_DAEMON_IMPLEMENTATION_FIELD in payload:
            raise ValueError("daemon implementation identity is transport-owned")
        request = dict(payload)
        request[RODEX_DAEMON_IMPLEMENTATION_FIELD] = RODEX_IMPLEMENTATION_ID
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(os.fspath(self.socket_path))
            connection.sendall(encode_daemon_message(request))
            response = receive_daemon_message(connection)
        if response.get("protocol") != RODEX_DAEMON_PROTOCOL:
            raise RodexDaemonError("daemon protocol does not match this Rodex generation")
        if response.get(RODEX_DAEMON_IMPLEMENTATION_FIELD) != RODEX_IMPLEMENTATION_ID:
            raise RodexDaemonError(
                "shared Rodex daemon implementation does not match the current code; "
                "stop all Rodex runtimes and the daemon before starting new sessions"
            )
        if response.get("ok") is not True:
            detail = response.get("error")
            raise RodexDaemonError(detail if isinstance(detail, str) and detail else "daemon operation failed")
        return response
