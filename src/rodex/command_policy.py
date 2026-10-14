"""Private, bounded command policy beside the native App Server.

The broker never executes commands or publishes their content outside its volatile
status callback. Codex retains hook trust, approvals, and sandbox enforcement.
"""

from __future__ import annotations

import json
import os
import re
import select
import socket
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from types import MappingProxyType
from typing import Any, Final, Literal

from rodex_registry.identity import parse_rodex_runtime_id

from .pre_tool_use_customer_library.find_rg import Outcome as FindRgOutcome
from .pre_tool_use_customer_library.find_rg import evaluate_find_rg_source
from .runtime_endpoint import ExclusiveUnixEndpoint
from .runtime_peer import BoundProcessOwner, require_unix_socket_peer_process

POLICY_PROTOCOL: Final = "rodex-command-policy-v1"
POLICY_SOCKET_ENV: Final = "RODEX_COMMAND_POLICY_SOCKET"
POLICY_MESSAGE_LIMIT: Final = 256 * 1024
POLICY_TIMEOUT_SECONDS: Final = 5.0
POLICY_MAX_WORKERS: Final = 16
POLICY_TEXT_LIMIT: Final = 8192
FIND_RG_POLICY_SECONDS: Final = 0.5
MINIMUM_INTEGER_SLEEP_SECONDS: Final = 10
SHORT_SLEEP_DENIAL_REASON: Final = "Re-try your sleep command with 10 seconds."
# Recognize one standalone shell command without claiming to parse command chains.
_INTEGER_SLEEP_COMMAND: Final = re.compile(r"[ \t]*sleep[ \t]+(?P<seconds>[0-9]+)[ \t]*;?[ \t]*")


def encode_policy_message(payload: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    if len(encoded) > POLICY_MESSAGE_LIMIT:
        raise ValueError("policy message exceeds its bound")
    return encoded


def receive_policy_message(connection: socket.socket) -> dict[str, Any]:
    content = bytearray()
    deadline = time.monotonic() + POLICY_TIMEOUT_SECONDS
    while len(content) < POLICY_MESSAGE_LIMIT:
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            raise TimeoutError("policy message deadline exceeded")
        chunk = connection.recv(min(65536, POLICY_MESSAGE_LIMIT - len(content)))
        if not chunk:
            break
        content.extend(chunk)
        if content.endswith(b"\n"):
            payload = json.loads(content)
            if isinstance(payload, dict):
                return payload
            break
        if b"\n" in content:
            break
    raise ValueError("incomplete or invalid policy message")


def _text(payload: Mapping[str, Any], key: str, limit: int = POLICY_TEXT_LIMIT) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ValueError("invalid policy field")
    return value


@dataclass(frozen=True, slots=True)
class CommandPolicyRequest:
    runtime_id: str
    session_id: str
    turn_id: str
    cwd: str
    permission_mode: str
    tool_name: str
    tool_input: dict[str, Any]

    @property
    def command(self) -> str:
        return self.tool_input["command"]

    @classmethod
    def from_hook(cls, runtime_id: str, event: Mapping[str, Any]) -> CommandPolicyRequest:
        if event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") != "Bash":
            raise ValueError("unsupported command policy event")
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
            raise ValueError("invalid command policy tool input")
        if "\x00" in tool_input["command"]:
            raise ValueError("invalid command policy command")
        return cls(
            str(parse_rodex_runtime_id(runtime_id)),
            _text(event, "session_id", 256),
            _text(event, "turn_id", 256),
            _text(event, "cwd"),
            _text(event, "permission_mode", 128),
            "Bash",
            dict(tool_input),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "protocol": POLICY_PROTOCOL,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "cwd": self.cwd,
            "permission_mode": self.permission_mode,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "command": {"kind": "shell", "text": self.command},
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CommandPolicyRequest:
        if (
            set(payload)
            != {
                "protocol",
                "runtime_id",
                "session_id",
                "turn_id",
                "cwd",
                "permission_mode",
                "tool_name",
                "tool_input",
                "command",
            }
            or payload.get("protocol") != POLICY_PROTOCOL
        ):
            raise ValueError("unsupported policy request contract")
        request = cls.from_hook(_text(payload, "runtime_id", 16), {**payload, "hook_event_name": "PreToolUse"})
        if payload["command"] != {"kind": "shell", "text": request.command}:
            raise ValueError("inconsistent normalized command")
        return request


@dataclass(frozen=True, slots=True)
class CommandPolicyDecision:
    """V1 can change only command text, never tool-input permission fields."""

    action: Literal["allow", "deny", "rewrite"]
    reason: str | None = None
    command: str | None = None

    def __post_init__(self) -> None:
        if self.action == "allow" and self.reason is None and self.command is None:
            return
        if self.action == "deny" and self.command is None:
            _text({"reason": self.reason}, "reason")
            return
        if (
            self.action == "rewrite"
            and self.reason is None
            and isinstance(self.command, str)
            and "\x00" not in self.command
        ):
            return
        raise ValueError("invalid policy decision")

    def to_payload(self, runtime_id: str) -> dict[str, Any]:
        payload = {"protocol": POLICY_PROTOCOL, "runtime_id": runtime_id, "decision": self.action}
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.command is not None:
            payload["command"] = self.command
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], runtime_id: str) -> CommandPolicyDecision:
        decision = cls(payload.get("decision"), payload.get("reason"), payload.get("command"))
        if dict(payload) != decision.to_payload(runtime_id):
            raise ValueError("unsupported policy response contract")
        return decision


def _bounded_policy_reason(reason: str) -> str:
    if len(reason) <= POLICY_TEXT_LIMIT:
        return reason
    return reason[: POLICY_TEXT_LIMIT - 3] + "..."


def _evaluate_find_rg_policy(
    request: CommandPolicyRequest, *, environment: Mapping[str, str]
) -> CommandPolicyDecision | None:
    decision = evaluate_find_rg_source(
        request.command,
        cwd=request.cwd,
        environment=environment,
        evaluation_seconds=FIND_RG_POLICY_SECONDS,
    )
    if decision.outcome in {FindRgOutcome.REJECTED, FindRgOutcome.NEEDS_FILESYSTEM}:
        return CommandPolicyDecision("deny", reason=_bounded_policy_reason(decision.message))
    return None


def _evaluate_short_sleep_policy(request: CommandPolicyRequest) -> CommandPolicyDecision | None:
    match = _INTEGER_SLEEP_COMMAND.fullmatch(request.command)
    if match is not None:
        significant_seconds = match.group("seconds").lstrip("0") or "0"
        minimum_seconds = str(MINIMUM_INTEGER_SLEEP_SECONDS)
        below_minimum = len(significant_seconds) < len(minimum_seconds) or (
            len(significant_seconds) == len(minimum_seconds) and significant_seconds < minimum_seconds
        )
        if below_minimum:
            return CommandPolicyDecision("deny", reason=SHORT_SLEEP_DENIAL_REASON)
    return None


def evaluate_command_policy(
    request: CommandPolicyRequest, *, environment: Mapping[str, str] | None = None
) -> CommandPolicyDecision:
    """Apply ordered customer policies to one exact proposed Bash command."""
    for policy in (
        partial(_evaluate_find_rg_policy, environment=os.environ if environment is None else environment),
        _evaluate_short_sleep_policy,
    ):
        decision = policy(request)
        if decision is not None:
            return decision
    return CommandPolicyDecision("allow")


def allow_response(runtime_id: str) -> dict[str, Any]:
    return CommandPolicyDecision("allow").to_payload(runtime_id)


class CommandPolicyBroker:
    """One runtime's endpoint, bounded workers, and exact App Server ancestry."""

    def __init__(
        self,
        path: Path,
        runtime_id: str,
        publish_allowed: Callable[[str], None],
        *,
        policy: Callable[[CommandPolicyRequest], CommandPolicyDecision] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._endpoint = ExclusiveUnixEndpoint(path)
        self._runtime_id = str(parse_rodex_runtime_id(runtime_id))
        self._publish_allowed = publish_allowed
        reservation_environment = MappingProxyType(dict(os.environ if environment is None else environment))
        self._policy = (
            policy if policy is not None else partial(evaluate_command_policy, environment=reservation_environment)
        )
        self._owner = BoundProcessOwner()
        self._stop = Event()
        self._lock = Lock()
        self._workers: set[Thread] = set()
        self._connections: set[socket.socket] = set()
        self._thread: Thread | None = None
        self._wake: tuple[socket.socket, socket.socket] | None = None

    def start(self) -> None:
        try:
            listener = self._endpoint.open()
            self._wake = socket.socketpair()
            self._thread = Thread(target=self._accept, args=(listener,), name="rodex-command-policy", daemon=True)
            self._thread.start()
        except BaseException:
            self.close()
            raise

    def bind_app_server(self, process: Any) -> None:
        self._owner.bind(process)

    def _accept(self, listener: socket.socket) -> None:
        assert self._wake is not None
        poller = select.poll()
        poller.register(listener, select.POLLIN)
        poller.register(self._wake[0], select.POLLIN)
        while not self._stop.is_set():
            readable = dict(poller.poll())
            if self._stop.is_set() or self._wake[0].fileno() in readable:
                return
            connection, _ = listener.accept()
            with self._lock:
                if len(self._workers) >= POLICY_MAX_WORKERS:
                    connection.close()
                    continue
                worker = Thread(target=self._serve, args=(connection,), name="rodex-policy-request", daemon=True)
                self._workers.add(worker)
                self._connections.add(connection)
            try:
                worker.start()
            except RuntimeError:
                with self._lock:
                    self._workers.discard(worker)
                    self._connections.discard(connection)
                connection.close()

    def _serve(self, connection: socket.socket) -> None:
        try:
            with connection:
                require_unix_socket_peer_process(connection, self._owner)
                request = CommandPolicyRequest.from_payload(receive_policy_message(connection))
                if request.runtime_id != self._runtime_id:
                    return
                decision = self._policy(request)
                connection.sendall(encode_policy_message(decision.to_payload(self._runtime_id)))
            # The native decision has already left the broker. This callback only
            # queues a bounded, transient tmux presentation; it must never log.
            if decision.action == "allow":
                self._publish_allowed(request.command)
            elif decision.action == "rewrite":
                assert decision.command is not None
                self._publish_allowed(decision.command)
        except Exception:
            # Hooks are advisory. Do not turn command-bearing exceptions into logs.
            pass
        finally:
            with self._lock:
                self._workers.discard(current_thread())
                self._connections.discard(connection)

    def close(self) -> None:
        self._stop.set()
        if self._wake is not None:
            with suppress(OSError):
                self._wake[1].shutdown(socket.SHUT_RDWR)
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=2)
        with self._lock:
            for connection in self._connections:
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
            workers = tuple(self._workers)
        for worker in workers:
            worker.join(timeout=2)
        self._endpoint.close()
        if self._wake is not None:
            for endpoint in self._wake:
                endpoint.close()
            self._wake = None
