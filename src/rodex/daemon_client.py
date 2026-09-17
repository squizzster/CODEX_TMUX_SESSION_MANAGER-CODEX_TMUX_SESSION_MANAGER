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

from .process_contracts import RuntimeServiceConfig

RODEX_DAEMON_PROTOCOL: Final = "rodex-daemon-v1"
RODEX_DAEMON_SOCKET_NAME: Final = "rodexd-v1.sock"
RODEX_DAEMON_LOG_NAME: Final = "rodexd-v1.log"
RODEX_DAEMON_START_LOCK_NAME: Final = "rodexd-v1.start.lock"
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
        """Serialize first start and accept only the current daemon protocol."""
        self.runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.runtime_root / RODEX_DAEMON_START_LOCK_NAME
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
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

    def stop(self, operation_id: str, runtime_id: str) -> None:
        self._request(
            {
                "protocol": RODEX_DAEMON_PROTOCOL,
                "operation": "stop",
                "operation_id": operation_id,
                "runtime_id": runtime_id,
            }
        )

    def _probe(self) -> bool:
        try:
            self._request({"protocol": RODEX_DAEMON_PROTOCOL, "operation": "ping"}, timeout_seconds=0.25)
        except (OSError, RodexDaemonError, TimeoutError):
            return False
        return True

    def _request(self, payload: dict[str, object], *, timeout_seconds: float = 5.0) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(os.fspath(self.socket_path))
            connection.sendall(encode_daemon_message(payload))
            response = receive_daemon_message(connection)
        if response.get("protocol") != RODEX_DAEMON_PROTOCOL:
            raise RodexDaemonError("daemon protocol does not match this Rodex generation")
        if response.get("ok") is not True:
            detail = response.get("error")
            raise RodexDaemonError(detail if isinstance(detail, str) and detail else "daemon operation failed")
        return response
