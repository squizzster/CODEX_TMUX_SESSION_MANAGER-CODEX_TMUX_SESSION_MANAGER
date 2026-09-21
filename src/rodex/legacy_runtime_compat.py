"""Narrow bridge for live pre-retention Rodex runtimes.

Rodex 0.14 launched hooks from its mutable bootstrap environment.  A checkout
upgrade could therefore redirect a still-running tmux-v4 hook into newer code.
This module preserves only the protocol-stable resize notification needed to
keep that live terminal usable.  It never starts, adopts, stops, or otherwise
owns a legacy runtime.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import struct
from pathlib import Path
from typing import Any, Final

from rodex_registry.identity import RodexRuntimeId, parse_rodex_runtime_id

from .daemon_client import RodexDaemonError, encode_daemon_message, receive_daemon_message

LEGACY_MUTABLE_TMUX_PROTOCOL: Final = "rodex-isolated-tmux-v4"
LEGACY_DAEMON_PROTOCOL: Final = "rodex-daemon-v2"
LEGACY_PROCESS_RECEIPT_PROTOCOL: Final = "rodex-process-receipt-v2"
_LEGACY_APP_SERVER_RECEIPT = re.compile(
    r"(?P<implementation>[0-9a-f]{64})\.process-(?P<runtime>[0-9a-f]{16})-app-server\.json"
)
_LEGACY_IMPLEMENTATION_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z.-]*\+sha256\.(?P<digest>[0-9a-f]{64})")
_OPERATION_ID = re.compile(r"[0-9a-f]{32}")


class LegacyRuntimeCompatibilityError(RuntimeError):
    """A live legacy runtime could not be addressed without weakening identity checks."""


def notify_legacy_runtime_resize(runtime_root: Path, runtime_id: str | RodexRuntimeId) -> None:
    """Wake one exact tmux-v4 runtime through its still-running v2 daemon."""
    parsed_runtime_id = parse_rodex_runtime_id(runtime_id)
    _require_private_runtime_root(runtime_root)
    implementation_digest = _legacy_runtime_implementation(runtime_root, parsed_runtime_id)
    daemon_socket = runtime_root / f"{implementation_digest}.sock"
    _require_private_daemon_socket(daemon_socket)

    discovery = _request(
        daemon_socket,
        {
            "protocol": LEGACY_DAEMON_PROTOCOL,
            "implementation_id": "legacy-runtime-owner-discovery",
            "operation": "ping",
        },
    )
    implementation_id = discovery.get("implementation_id")
    match = _LEGACY_IMPLEMENTATION_ID.fullmatch(implementation_id) if isinstance(implementation_id, str) else None
    if (
        discovery.get("protocol") != LEGACY_DAEMON_PROTOCOL
        or match is None
        or match.group("digest") != implementation_digest
    ):
        raise LegacyRuntimeCompatibilityError("legacy daemon did not prove its receipt-bound implementation")

    response = _request(
        daemon_socket,
        {
            "protocol": LEGACY_DAEMON_PROTOCOL,
            "implementation_id": implementation_id,
            "operation": "wake_runtime",
            "runtime_id": str(parsed_runtime_id),
            "cause": "terminal_resize",
        },
    )
    if (
        response.get("protocol") != LEGACY_DAEMON_PROTOCOL
        or response.get("implementation_id") != implementation_id
        or response.get("ok") is not True
    ):
        detail = response.get("error")
        raise LegacyRuntimeCompatibilityError(
            detail if isinstance(detail, str) and detail else "legacy daemon rejected the resize notification"
        )


def _require_private_runtime_root(runtime_root: Path) -> None:
    try:
        observed = runtime_root.lstat()
    except OSError as error:
        raise LegacyRuntimeCompatibilityError("legacy runtime root is unavailable") from error
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.getuid() or stat.S_IMODE(observed.st_mode) & 0o077:
        raise LegacyRuntimeCompatibilityError("legacy runtime root is not private to the current user")


def _legacy_runtime_implementation(runtime_root: Path, runtime_id: RodexRuntimeId) -> str:
    paths = tuple(sorted(runtime_root.glob(f"*.process-{runtime_id}-app-server.json")))
    if len(paths) != 1:
        raise LegacyRuntimeCompatibilityError("legacy runtime does not have one exact app-server receipt")
    path = paths[0]
    match = _LEGACY_APP_SERVER_RECEIPT.fullmatch(path.name)
    if match is None or match.group("runtime") != str(runtime_id):
        raise LegacyRuntimeCompatibilityError("legacy app-server receipt name is malformed")
    receipt = _read_private_json(path)
    expected_fields = {
        "kind",
        "operation_id",
        "pid",
        "process_group_id",
        "protocol",
        "runtime_id",
        "start_time_ticks",
        "uid",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_fields:
        raise LegacyRuntimeCompatibilityError("legacy app-server receipt fields are malformed")
    if (
        receipt.get("protocol") != LEGACY_PROCESS_RECEIPT_PROTOCOL
        or receipt.get("kind") != "app-server"
        or receipt.get("runtime_id") != str(runtime_id)
        or type(receipt.get("uid")) is not int
        or receipt.get("uid") != os.getuid()
        or not isinstance(receipt.get("operation_id"), str)
        or _OPERATION_ID.fullmatch(receipt["operation_id"]) is None
    ):
        raise LegacyRuntimeCompatibilityError("legacy app-server receipt contract does not match")
    process_fields = (receipt.get("pid"), receipt.get("process_group_id"), receipt.get("start_time_ticks"))
    if any(type(value) is not int or value <= 0 for value in process_fields):
        raise LegacyRuntimeCompatibilityError("legacy app-server process identity is malformed")
    pid, process_group_id, start_time_ticks = process_fields
    if pid != process_group_id:
        raise LegacyRuntimeCompatibilityError("legacy app-server does not own its process group")
    _require_live_process(pid, process_group_id, start_time_ticks)
    return match.group("implementation")


def _read_private_json(path: Path) -> Any:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise LegacyRuntimeCompatibilityError("legacy app-server receipt could not be opened exactly") from error
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise LegacyRuntimeCompatibilityError("legacy app-server receipt is not a private current-user file")
        with os.fdopen(os.dup(descriptor), encoding="utf-8") as receipt_file:
            return json.load(receipt_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LegacyRuntimeCompatibilityError("legacy app-server receipt is malformed") from error
    finally:
        os.close(descriptor)


def _require_live_process(pid: int, process_group_id: int, start_time_ticks: int) -> None:
    try:
        descriptor = os.pidfd_open(pid)
    except OSError as error:
        raise LegacyRuntimeCompatibilityError("legacy app-server process is no longer live") from error
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
        uid = Path(f"/proc/{pid}").stat().st_uid
    except (OSError, FileNotFoundError) as error:
        os.close(descriptor)
        raise LegacyRuntimeCompatibilityError("legacy app-server process is no longer live") from error
    try:
        fields = stat_text.rsplit(")", 1)[1].split()
        observed_process_group = int(fields[2])
        observed_start_time = int(fields[19])
        if uid != os.getuid() or observed_process_group != process_group_id or observed_start_time != start_time_ticks:
            raise LegacyRuntimeCompatibilityError("legacy app-server process identity changed")
        try:
            exited = os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            exited = None
        if exited is not None:
            raise LegacyRuntimeCompatibilityError("legacy app-server process exited")
    except (IndexError, ValueError) as error:
        raise LegacyRuntimeCompatibilityError("legacy app-server process identity could not be parsed") from error
    finally:
        os.close(descriptor)


def _require_private_daemon_socket(path: Path) -> None:
    try:
        observed = path.lstat()
    except OSError as error:
        raise LegacyRuntimeCompatibilityError("legacy daemon socket is unavailable") from error
    if not stat.S_ISSOCK(observed.st_mode) or observed.st_uid != os.getuid() or stat.S_IMODE(observed.st_mode) & 0o077:
        raise LegacyRuntimeCompatibilityError("legacy daemon socket is not private to the current user")


def _request(path: Path, payload: dict[str, object]) -> dict[str, Any]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5.0)
            connection.connect(os.fspath(path))
            _peer_pid, peer_uid, _peer_gid = struct.unpack(
                "3i",
                connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
            )
            if peer_uid != os.getuid():
                raise LegacyRuntimeCompatibilityError("legacy daemon peer belongs to another user")
            connection.sendall(encode_daemon_message(payload))
            return receive_daemon_message(connection)
    except LegacyRuntimeCompatibilityError:
        raise
    except (OSError, TimeoutError, RodexDaemonError) as error:
        raise LegacyRuntimeCompatibilityError("legacy daemon request failed") from error
