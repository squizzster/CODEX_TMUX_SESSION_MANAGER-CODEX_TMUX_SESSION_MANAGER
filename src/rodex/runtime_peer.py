"""Bind a Rodex WebSocket to the expected runtime and tmux server incarnation."""

from __future__ import annotations

import os
import re
import select
import socket
import struct
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from threading import Lock
from typing import Any, Final

from websockets.exceptions import InvalidHandshake

from rodex_registry.identity import RodexRuntimeId, parse_rodex_runtime_id

_CONTRACT_HEADER: Final = "X-Rodex-Peer-Contract"
_RUNTIME_HEADER: Final = "X-Rodex-Runtime-Id"
_SERVER_HEADER: Final = "X-Rodex-Tmux-Server-Id"
_CONTRACT: Final = "rodex-runtime-peer-v4"
_SERVER_ID = re.compile(r"[0-9a-f]{32}")


class RuntimePeerIdentityError(InvalidHandshake):
    """The opened transport does not belong to its expected runtime incarnation."""


@dataclass(frozen=True, slots=True)
class CurrentProcessOwner:
    """The current host process; inherited objects do not authorize a fork."""

    pid: int = field(default_factory=os.getpid)

    def poll(self) -> int | None:
        return None if os.getpid() == self.pid else 0


class BoundProcessOwner:
    """Authorize exactly one retained process tree after an explicit start barrier."""

    def __init__(self) -> None:
        self._process: Any | None = None
        self._lock = Lock()

    def bind(self, process: Any) -> None:
        if not isinstance(getattr(process, "pid", None), int) or process.pid <= 0:
            raise ValueError("bound process owner requires a live process identity")
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("process owner is already bound to a live process")
            if process.poll() is not None:
                raise RuntimeError("process owner cannot bind an exited process")
            self._process = process

    @property
    def pid(self) -> int:
        with self._lock:
            process = self._process
            return 0 if process is None else process.pid

    def poll(self) -> int | None:
        with self._lock:
            process = self._process
            return 0 if process is None else process.poll()


def require_unix_peer_process(connection: Any, process: Any) -> None:
    """Require the retained child or its live descendant on this exact transport.

    Codex's supported launcher may retain a Node wrapper and serve from its
    native child. Pin every process traversed so PID recycling cannot supply
    another ancestry while it is being checked.
    """
    try:
        credentials = connection.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        peer_pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
        matches = (
            peer_uid == os.getuid()
            and process.poll() is None
            and _is_live_process_descendant(peer_pid, process.pid)
            and process.poll() is None
        )
    except (AttributeError, IndexError, OSError, ValueError, struct.error) as error:
        raise RuntimePeerIdentityError("could not verify the retained App Server process") from error
    if not matches:
        raise RuntimePeerIdentityError("Unix peer is not the retained live App Server process")


def _parent_process_id(pid: int) -> int:
    # The comm field may contain whitespace and parentheses. All later fields
    # are scalar; state is first and ppid is second after the last close paren.
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])


def _is_live_process_descendant(peer_pid: int, owner_pid: int) -> bool:
    if type(owner_pid) is not int or owner_pid <= 0:
        return False
    pinned: dict[int, tuple[int, int]] = {}
    try:
        current = peer_pid
        for _ in range(64):
            if current <= 0 or current in pinned:
                return False
            descriptor = os.pidfd_open(current)
            pinned[current] = (descriptor, -1)
            parent = _parent_process_id(current)
            pinned[current] = (descriptor, parent)
            if current == owner_pid:
                break
            current = parent
        else:
            return False
        poller = select.poll()
        for pid, (descriptor, parent) in pinned.items():
            poller.register(descriptor, select.POLLIN)
            if _parent_process_id(pid) != parent:
                return False
        return not poller.poll(0)
    finally:
        for descriptor, _parent in pinned.values():
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class RuntimePeerIdentity:
    runtime_id: RodexRuntimeId
    tmux_server_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_id", parse_rodex_runtime_id(self.runtime_id))
        if not isinstance(self.tmux_server_id, str) or _SERVER_ID.fullmatch(self.tmux_server_id) is None:
            raise ValueError("tmux server identity must be 32 lowercase hexadecimal characters")

    def headers(self) -> dict[str, str]:
        return {
            _CONTRACT_HEADER: _CONTRACT,
            _RUNTIME_HEADER: str(self.runtime_id),
            _SERVER_HEADER: self.tmux_server_id,
        }

    def require_headers(self, headers: Any) -> None:
        try:
            valid = all(headers.get(name) == value for name, value in self.headers().items())
        except (AttributeError, KeyError, ValueError):
            valid = False
        if not valid:
            raise RuntimePeerIdentityError("WebSocket peer does not match the expected runtime and tmux server")


def runtime_peer_server_hooks(
    identity: RuntimePeerIdentity,
    *,
    native_paths: frozenset[str] = frozenset(),
    native_owner: Any | None = None,
) -> dict[str, Callable[..., Any]]:
    """Require peer identity during HTTP admission, before any handler can run."""
    if native_paths and native_owner is None:
        raise ValueError("native protocol paths require their owning host process")

    def process_request(connection: Any, request: Any) -> Any:
        try:
            if request.path in native_paths:
                require_unix_peer_process(connection, native_owner)
            else:
                identity.require_headers(request.headers)
        except RuntimePeerIdentityError:
            return connection.respond(HTTPStatus.FORBIDDEN, "Rodex runtime identity does not match\n")
        return None

    def process_response(_connection: Any, request: Any, response: Any) -> Any:
        if request.path not in native_paths and response.status_code == HTTPStatus.SWITCHING_PROTOCOLS:
            for name, value in identity.headers().items():
                response.headers[name] = value
        return response

    return {"process_request": process_request, "process_response": process_response}


@contextmanager
def verified_runtime_connection(
    connector: Callable[..., Any],
    socket_path: Path,
    *,
    peer_identity: RuntimePeerIdentity,
    **options: Any,
) -> Iterator[Any]:
    """Verify the handshake on the same connection used for all later traffic."""
    if "additional_headers" in options:
        raise ValueError("runtime peer headers are owned by the identity contract")
    with connector(str(socket_path), additional_headers=peer_identity.headers(), **options) as connection:
        peer_identity.require_headers(getattr(getattr(connection, "response", None), "headers", None))
        yield connection
