"""Control one verified live Codex thread through Rodex runtime metadata."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import unix_connect

from rodex_registry.identity import RodexSessionIdentifier

from .protocol_proxy import EVENT_STREAM_READY_METHOD
from .version import RODEX_VERSION

Connector = Callable[..., Any]
Revalidate = Callable[[], None]
EventWriter = Callable[[str], None]


class RodexControlError(RuntimeError):
    """A named live Codex control or event operation failed."""


@dataclass(frozen=True, slots=True)
class LiveRodexControl:
    """Runtime-only endpoints advertised by an exact live tmux session."""

    protocol_proxy_socket_path: Path
    protocol_event_socket_path: Path
    codex_session_uuid: uuid.UUID
    rodex_session_identifier: RodexSessionIdentifier | None = None
    rodex_registry_uuid: uuid.UUID | None = None
    registration_state: str | None = None


@dataclass(frozen=True, slots=True)
class CodexThreadState:
    """The small live status surface needed by Rodex control commands."""

    status: str
    active_flags: tuple[str, ...]
    active_turn_id: str | None
    can_accept_direct_input: bool | None


@dataclass(frozen=True, slots=True)
class PromptDispatch:
    """How one prompt was accepted by the existing Codex thread."""

    action: Literal["started", "steered"]
    turn_id: str


class CodexControlClient:
    """Verify, inspect, steer, wait for, and tail one loaded Codex thread."""

    def __init__(self, *, connector: Connector = unix_connect) -> None:
        self._connect = connector

    def inspect(self, control: LiveRodexControl) -> CodexThreadState:
        """Return the verified thread's current runtime state."""
        with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
            thread = self._verify_and_read_thread(websocket, control.codex_session_uuid)
        return _thread_state(thread)

    def send_prompt(
        self,
        control: LiveRodexControl,
        prompt: str,
        *,
        revalidate: Revalidate = lambda: None,
    ) -> PromptDispatch:
        """Start an idle turn or steer the exact active turn with user text."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        try:
            with self._open_events(control.protocol_event_socket_path) as events:
                ready = _expect_event_stream_ready(events)
                active_turn_id = _ready_active_turn_id(ready, control.codex_session_uuid)
                with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                    thread = self._verify_and_read_thread(
                        websocket, control.codex_session_uuid
                    )
                    state = _thread_state(thread, active_turn_id=active_turn_id)
                    if state.can_accept_direct_input is False:
                        raise RodexControlError("Codex thread does not accept direct input")
                    revalidate()
                    user_input = [{"type": "text", "text": prompt}]
                    if state.status == "active":
                        if state.active_turn_id is None:
                            raise RodexControlError(
                                "active Codex thread has no observable active turn"
                            )
                        result = _request(
                            websocket,
                            3,
                            "turn/steer",
                            {
                                "threadId": str(control.codex_session_uuid),
                                "expectedTurnId": state.active_turn_id,
                                "input": user_input,
                            },
                        )
                        return PromptDispatch("steered", _turn_id(result))
                    if state.status != "idle":
                        raise RodexControlError(
                            f"Codex thread cannot accept a turn while {state.status}"
                        )
                    result = _request(
                        websocket,
                        3,
                        "turn/start",
                        {
                            "threadId": str(control.codex_session_uuid),
                            "input": user_input,
                        },
                    )
                    return PromptDispatch("started", _turn_id(result))
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex control connection ended: {error}") from error

    def wait_until_idle(
        self,
        control: LiveRodexControl,
        *,
        revalidate: Revalidate = lambda: None,
    ) -> None:
        """Return once the current turn completes, or immediately when idle."""
        try:
            with self._open_events(control.protocol_event_socket_path) as events:
                _expect_event_stream_ready(events)
                state = self.inspect(control)
                revalidate()
                if state.status == "idle":
                    return
                if state.status != "active":
                    raise RodexControlError(
                        f"Codex thread cannot be waited on while {state.status}"
                    )
                for message in events:
                    payload = _protocol_payload(message)
                    if payload is None or not _belongs_to_thread(
                        payload, control.codex_session_uuid
                    ):
                        continue
                    method = payload.get("method")
                    status = _nested(payload, "params", "status", "type")
                    if method == "turn/completed" or (
                        method == "thread/status/changed" and status == "idle"
                    ):
                        current = self.inspect(control)
                        revalidate()
                        if current.status == "idle":
                            return
                        if current.status != "active":
                            detail = "Codex thread entered an unexpected wait state"
                            raise RodexControlError(f"{detail}: {current.status}")
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex event stream ended: {error}") from error
        raise RodexControlError("Codex event stream ended before the turn completed")

    def tail(
        self,
        control: LiveRodexControl,
        write_event: EventWriter,
        *,
        revalidate: Revalidate = lambda: None,
    ) -> None:
        """Stream future structured lifecycle events for the verified thread."""
        try:
            with self._open_events(control.protocol_event_socket_path) as events:
                _expect_event_stream_ready(events)
                self.inspect(control)
                revalidate()
                for message in events:
                    payload = _protocol_payload(message)
                    if payload is None or not _belongs_to_thread(
                        payload, control.codex_session_uuid
                    ):
                        continue
                    formatted = format_protocol_log_event(payload)
                    if formatted is not None:
                        write_event(formatted)
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex event stream ended: {error}") from error

    def _open_protocol(self, socket_path: Path) -> Any:
        return self._connect(
            str(socket_path),
            uri="ws://localhost/rpc",
            compression=None,
            open_timeout=2,
            close_timeout=1,
            max_size=None,
        )

    def _open_events(self, socket_path: Path) -> Any:
        return self._connect(
            str(socket_path),
            uri="ws://localhost/events",
            compression=None,
            open_timeout=2,
            close_timeout=1,
            max_size=None,
        )

    def _verify_and_read_thread(
        self, websocket: Any, expected_codex_uuid: uuid.UUID
    ) -> dict[str, Any]:
        _request(
            websocket,
            0,
            "initialize",
            {
                "clientInfo": {
                    "name": "rodex-control",
                    "title": "Rodex Control",
                    "version": RODEX_VERSION,
                }
            },
        )
        websocket.send(json.dumps({"method": "initialized", "params": {}}))
        loaded = _request(websocket, 1, "thread/loaded/list", {})
        loaded_ids = loaded.get("data")
        expected = str(expected_codex_uuid)
        if not isinstance(loaded_ids, list) or expected not in loaded_ids:
            raise RodexControlError(
                f"live endpoint does not contain expected Codex session {expected}"
            )
        result = _request(
            websocket,
            2,
            "thread/read",
            {"threadId": expected, "includeTurns": False},
        )
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != expected:
            raise RodexControlError("app-server returned an unexpected Codex thread")
        return thread


def format_protocol_log_event(payload: dict[str, Any]) -> str | None:
    """Render useful lifecycle events as stable compact JSON lines."""
    method = payload.get("method")
    if not isinstance(method, str):
        return None
    if method.endswith("/delta"):
        return None
    if not (
        method.startswith("item/")
        or method.startswith("turn/")
        or method == "thread/status/changed"
        or method == "error"
    ):
        return None
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _thread_state(
    thread: dict[str, Any], *, active_turn_id: str | None = None
) -> CodexThreadState:
    status_value = thread.get("status")
    if not isinstance(status_value, dict) or not isinstance(status_value.get("type"), str):
        raise RodexControlError("app-server returned an invalid thread status")
    status = status_value["type"]
    raw_flags = status_value.get("activeFlags", [])
    if not isinstance(raw_flags, list) or not all(
        isinstance(flag, str) for flag in raw_flags
    ):
        raise RodexControlError("app-server returned invalid active flags")
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise RodexControlError("app-server returned invalid thread turns")
    can_accept = thread.get("canAcceptDirectInput")
    if can_accept is not None and not isinstance(can_accept, bool):
        raise RodexControlError("app-server returned invalid direct-input capability")
    return CodexThreadState(
        status=status,
        active_flags=tuple(raw_flags),
        active_turn_id=active_turn_id,
        can_accept_direct_input=can_accept,
    )


def _request(
    websocket: Any,
    request_id: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    websocket.send(
        json.dumps(
            {"method": method, "id": request_id, "params": params},
            separators=(",", ":"),
        )
    )
    while True:
        try:
            message = websocket.recv(timeout=5)
        except TimeoutError as error:
            raise RodexControlError(f"timed out waiting for {method}") from error
        payload = _protocol_payload(message)
        if payload is None or payload.get("id") != request_id:
            continue
        if "error" in payload:
            raise RodexControlError(f"{method} failed: {payload['error']}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RodexControlError(f"{method} returned an invalid result")
        return result


def _turn_id(result: dict[str, Any]) -> str:
    direct_turn_id = result.get("turnId")
    if isinstance(direct_turn_id, str) and direct_turn_id:
        return direct_turn_id
    turn = result.get("turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
        raise RodexControlError("Codex did not return a turn id")
    return turn["id"]


def _protocol_payload(message: str | bytes) -> dict[str, Any] | None:
    if isinstance(message, bytes):
        try:
            message = message.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _belongs_to_thread(payload: dict[str, Any], codex_uuid: uuid.UUID) -> bool:
    params = payload.get("params")
    return isinstance(params, dict) and params.get("threadId") == str(codex_uuid)


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _expect_event_stream_ready(events: Any) -> dict[str, Any]:
    try:
        message = events.recv(timeout=2)
    except TimeoutError as error:
        raise RodexControlError("timed out opening Codex event stream") from error
    payload = _protocol_payload(message)
    if payload is None or payload.get("method") != EVENT_STREAM_READY_METHOD:
        raise RodexControlError("Codex event stream did not send its ready signal")
    return payload


def _ready_active_turn_id(
    ready: dict[str, Any], codex_session_uuid: uuid.UUID
) -> str | None:
    active_turns = _nested(ready, "params", "activeTurns")
    if not isinstance(active_turns, dict):
        raise RodexControlError("Codex event stream sent invalid active-turn state")
    turn_id = active_turns.get(str(codex_session_uuid))
    if turn_id is not None and not isinstance(turn_id, str):
        raise RodexControlError("Codex event stream sent an invalid active turn id")
    return turn_id
