"""Control one verified live Codex thread through Rodex runtime metadata."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import unix_connect

from rodex_registry.identity import (
    CodexSessionId,
    RodexRegistryId,
    RodexRuntimeIdentifier,
    RodexSessionId,
)

from .app_server_contract import require_supported_app_server
from .protocol_proxy import EVENT_STREAM_READY_METHOD
from .version import RODEX_VERSION

Connector = Callable[..., Any]
Revalidate = Callable[[], None]
EventWriter = Callable[[str], None]


class RodexControlError(RuntimeError):
    """A named live Codex control or event operation failed."""


class RodexDispatchIndeterminateError(RodexControlError):
    """A mutating request was sent but its acceptance could not be observed."""


class RodexWaitTimeoutError(RodexControlError):
    """An exact wait timed out without interrupting its target turn."""


@dataclass(frozen=True, slots=True)
class LiveRodexControl:
    """Runtime-only endpoints advertised by an exact live tmux session."""

    protocol_proxy_socket_path: Path
    protocol_event_socket_path: Path
    codex_session_id: CodexSessionId
    rodex_session_id: RodexSessionId | None = None
    rodex_registry_id: RodexRegistryId | None = None
    registration_state: str | None = None
    runtime_identifier: RodexRuntimeIdentifier | None = None


@dataclass(frozen=True, slots=True)
class CodexThreadState:
    """The small live status surface needed by Rodex control commands."""

    thread_id: str
    session_id: str
    status: str
    active_flags: tuple[str, ...]
    active_turn_id: str | None
    can_accept_direct_input: bool | None


@dataclass(frozen=True, slots=True)
class PromptDispatch:
    """How one prompt was accepted by the existing Codex thread."""

    action: Literal["started", "steered"]
    turn_id: str
    thread_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CodexTurnResult:
    """One live App Server turn reduced to Rodex's bounded result contract."""

    turn_id: str
    status: Literal["completed", "interrupted", "failed", "inProgress"]
    final_agent_message: str | None
    structured_output: object | None
    error: object | None
    started_at: int | None
    completed_at: int | None
    duration_ms: int | None
    changed_paths: tuple[str, ...]
    changed_paths_truncated: bool


class CodexControlClient:
    """Verify, inspect, steer, wait for, and tail one loaded Codex thread."""

    def __init__(
        self,
        *,
        connector: Connector = unix_connect,
        request_id_factory: Callable[[], str] = lambda: f"rodex:{uuid.uuid4()}",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connect = connector
        self._request_id_factory = request_id_factory
        self._monotonic = monotonic

    def inspect(self, control: LiveRodexControl) -> CodexThreadState:
        """Return the verified thread's current runtime state."""
        with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
            thread = self._verify_and_read_thread(websocket, control.codex_session_id)
        return _thread_state(thread)

    def inspect_live(self, control: LiveRodexControl) -> CodexThreadState:
        """Return thread state with the event tap's exact active-turn identity."""
        try:
            with self._open_events(control.protocol_event_socket_path) as events:
                ready = _expect_event_stream_ready(events)
                with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                    thread = self._verify_and_read_thread(
                        websocket, control.codex_session_id
                    )
                return _thread_state(
                    thread,
                    active_turn_id=_ready_active_turn_id(ready, control.codex_session_id),
                )
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex control connection ended: {error}") from error

    def exact_control_version(self, control: LiveRodexControl) -> str:
        """Return the characterized live App Server version or fail closed."""
        try:
            with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                return self._initialize_protocol(websocket, require_compatible=True)
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex control connection ended: {error}") from error

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
                active_turn_id = _ready_active_turn_id(ready, control.codex_session_id)
                with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                    thread = self._verify_and_read_thread(
                        websocket, control.codex_session_id
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
                            self._request_id_factory(),
                            "turn/steer",
                            {
                                "threadId": str(control.codex_session_id),
                                "expectedTurnId": state.active_turn_id,
                                "input": user_input,
                            },
                            indeterminate_on_connection_loss=True,
                        )
                        return PromptDispatch(
                            "steered",
                            _turn_id(result),
                            state.thread_id,
                            state.session_id,
                        )
                    if state.status != "idle":
                        raise RodexControlError(
                            f"Codex thread cannot accept a turn while {state.status}"
                        )
                    result = _request(
                        websocket,
                        self._request_id_factory(),
                        "turn/start",
                        {
                            "threadId": str(control.codex_session_id),
                            "input": user_input,
                        },
                        indeterminate_on_connection_loss=True,
                    )
                    return PromptDispatch(
                        "started",
                        _turn_id(result),
                        state.thread_id,
                        state.session_id,
                    )
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex control connection ended: {error}") from error

    def start_turn(
        self,
        control: LiveRodexControl,
        prompt: str,
        *,
        revalidate: Revalidate = lambda: None,
    ) -> PromptDispatch:
        """Start only when the verified thread is observed idle."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        try:
            with self._open_events(control.protocol_event_socket_path) as events:
                ready = _expect_event_stream_ready(events)
                active_turn_id = _ready_active_turn_id(ready, control.codex_session_id)
                with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                    thread = self._verify_and_read_thread(
                        websocket,
                        control.codex_session_id,
                        require_compatible=True,
                    )
                    state = _thread_state(thread, active_turn_id=active_turn_id)
                    if state.status != "idle" or state.active_turn_id is not None:
                        raise RodexControlError(
                            "Codex thread is not idle; use _steer with the exact turn ID"
                        )
                    if state.can_accept_direct_input is False:
                        raise RodexControlError("Codex thread does not accept direct input")
                    revalidate()
                    result = _request(
                        websocket,
                        self._request_id_factory(),
                        "turn/start",
                        {
                            "threadId": state.thread_id,
                            "input": [{"type": "text", "text": prompt}],
                        },
                        indeterminate_on_connection_loss=True,
                    )
                    return PromptDispatch(
                        "started",
                        _turn_id(result),
                        state.thread_id,
                        state.session_id,
                    )
        except RodexDispatchIndeterminateError:
            raise
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex control connection ended: {error}") from error

    def steer_turn(
        self,
        control: LiveRodexControl,
        turn_id: str,
        prompt: str,
        *,
        revalidate: Revalidate = lambda: None,
    ) -> PromptDispatch:
        """Steer only the caller-specified exact active turn."""
        _require_turn_id(turn_id)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        try:
            with self._open_events(control.protocol_event_socket_path) as events:
                ready = _expect_event_stream_ready(events)
                active_turn_id = _ready_active_turn_id(ready, control.codex_session_id)
                with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                    thread = self._verify_and_read_thread(
                        websocket,
                        control.codex_session_id,
                        require_compatible=True,
                    )
                    state = _thread_state(thread, active_turn_id=active_turn_id)
                    if state.status != "active" or state.active_turn_id != turn_id:
                        observed = state.active_turn_id or state.status
                        raise RodexControlError(
                            f"exact Codex turn is not active: expected {turn_id}, "
                            f"observed {observed}"
                        )
                    if state.can_accept_direct_input is False:
                        raise RodexControlError("Codex thread does not accept direct input")
                    revalidate()
                    result = _request(
                        websocket,
                        self._request_id_factory(),
                        "turn/steer",
                        {
                            "threadId": state.thread_id,
                            "expectedTurnId": turn_id,
                            "input": [{"type": "text", "text": prompt}],
                        },
                        indeterminate_on_connection_loss=True,
                    )
                    return PromptDispatch(
                        "steered",
                        _turn_id(result),
                        state.thread_id,
                        state.session_id,
                    )
        except RodexDispatchIndeterminateError:
            raise
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex control connection ended: {error}") from error

    def interrupt_turn(
        self,
        control: LiveRodexControl,
        turn_id: str,
        *,
        revalidate: Revalidate = lambda: None,
    ) -> CodexThreadState:
        """Request interruption only for the exact observed active turn."""
        _require_turn_id(turn_id)
        try:
            with self._open_events(control.protocol_event_socket_path) as events:
                ready = _expect_event_stream_ready(events)
                active_turn_id = _ready_active_turn_id(ready, control.codex_session_id)
                with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                    thread = self._verify_and_read_thread(
                        websocket,
                        control.codex_session_id,
                        require_compatible=True,
                    )
                    state = _thread_state(thread, active_turn_id=active_turn_id)
                    if state.status != "active" or state.active_turn_id != turn_id:
                        observed = state.active_turn_id or state.status
                        raise RodexControlError(
                            f"exact Codex turn is not active: expected {turn_id}, "
                            f"observed {observed}"
                        )
                    revalidate()
                    _request(
                        websocket,
                        self._request_id_factory(),
                        "turn/interrupt",
                        {"threadId": state.thread_id, "turnId": turn_id},
                        indeterminate_on_connection_loss=True,
                    )
                    return state
        except RodexDispatchIndeterminateError:
            raise
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex control connection ended: {error}") from error

    def result(
        self,
        control: LiveRodexControl,
        turn_id: str,
        *,
        revalidate: Revalidate = lambda: None,
    ) -> tuple[CodexThreadState, CodexTurnResult]:
        """Read one exact turn directly from App Server without persisting content."""
        _require_turn_id(turn_id)
        try:
            with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                thread = self._verify_and_read_thread(
                    websocket,
                    control.codex_session_id,
                    include_turns=True,
                    require_compatible=True,
                )
            revalidate()
            turn = _turn_from_thread(thread, turn_id)
            return _thread_state(thread), _turn_result(turn)
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex control connection ended: {error}") from error

    def wait_for_turn(
        self,
        control: LiveRodexControl,
        turn_id: str,
        *,
        timeout_seconds: float | None = None,
        revalidate: Revalidate = lambda: None,
    ) -> tuple[CodexThreadState, CodexTurnResult]:
        """Wait for one exact turn; timeout never interrupts it."""
        _require_turn_id(turn_id)
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        deadline = None if timeout_seconds is None else self._monotonic() + timeout_seconds
        try:
            with self._open_events(control.protocol_event_socket_path) as events:
                ready = _expect_event_stream_ready(events)
                with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                    thread = self._verify_and_read_thread(
                        websocket,
                        control.codex_session_id,
                        include_turns=True,
                        require_compatible=True,
                    )
                state = _thread_state(
                    thread,
                    active_turn_id=_ready_active_turn_id(ready, control.codex_session_id),
                )
                turn = _turn_from_thread(thread, turn_id)
                result = _turn_result(turn)
                revalidate()
                if result.status != "inProgress":
                    return state, result
                if state.active_turn_id != turn_id:
                    observed = state.active_turn_id or state.status
                    raise RodexControlError(
                        f"exact Codex turn is not active: expected {turn_id}, "
                        f"observed {observed}"
                    )
                while True:
                    remaining = None if deadline is None else deadline - self._monotonic()
                    if remaining is not None and remaining <= 0:
                        raise RodexWaitTimeoutError(
                            f"timed out waiting for exact Codex turn {turn_id}"
                        )
                    try:
                        message = events.recv(timeout=remaining)
                    except TimeoutError as error:
                        raise RodexWaitTimeoutError(
                            f"timed out waiting for exact Codex turn {turn_id}"
                        ) from error
                    payload = _protocol_payload(message)
                    if payload is None or not _belongs_to_thread(
                        payload, control.codex_session_id
                    ):
                        continue
                    if payload.get("method") != "turn/completed":
                        continue
                    completed = _nested(payload, "params", "turn")
                    if not isinstance(completed, dict) or completed.get("id") != turn_id:
                        continue
                    with self._open_protocol(
                        control.protocol_proxy_socket_path
                    ) as websocket:
                        terminal_thread = self._verify_and_read_thread(
                            websocket,
                            control.codex_session_id,
                            include_turns=True,
                            require_compatible=True,
                        )
                    terminal_result = _turn_result(
                        _turn_from_thread(terminal_thread, turn_id)
                    )
                    if terminal_result.status == "inProgress":
                        raise RodexControlError(
                            "Codex completion event preceded terminal turn state"
                        )
                    revalidate()
                    return _thread_state(terminal_thread), terminal_result
        except RodexWaitTimeoutError:
            raise
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex event stream ended: {error}") from error
        raise RodexControlError("Codex event stream ended before the turn completed")

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
                        payload, control.codex_session_id
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
                        payload, control.codex_session_id
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
        self,
        websocket: Any,
        expected_codex_session_id: CodexSessionId,
        *,
        include_turns: bool = False,
        require_compatible: bool = False,
    ) -> dict[str, Any]:
        self._initialize_protocol(websocket, require_compatible=require_compatible)
        loaded = _request(websocket, 1, "thread/loaded/list", {})
        loaded_ids = loaded.get("data")
        expected = str(expected_codex_session_id)
        if not isinstance(loaded_ids, list) or expected not in loaded_ids:
            raise RodexControlError(
                f"live endpoint does not contain expected Codex session {expected}"
            )
        result = _request(
            websocket,
            2,
            "thread/read",
            {"threadId": expected, "includeTurns": include_turns},
        )
        thread = result.get("thread")
        if (
            not isinstance(thread, dict)
            or thread.get("id") != expected
            or not isinstance(thread.get("sessionId"), str)
        ):
            raise RodexControlError("app-server returned an unexpected Codex thread")
        return thread

    def _initialize_protocol(self, websocket: Any, *, require_compatible: bool) -> str:
        initialize_result = _request(
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
        version = (
            require_supported_app_server(initialize_result)
            if require_compatible
            else _app_server_version(initialize_result)
        )
        websocket.send(json.dumps({"method": "initialized", "params": {}}))
        return version


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


def _app_server_version(initialize_result: dict[str, Any]) -> str:
    user_agent = initialize_result.get("userAgent")
    if not isinstance(user_agent, str):
        return "unknown"
    product = user_agent.split(" ", 1)[0]
    _client_name, separator, version = product.rpartition("/")
    return version if separator and version else "unknown"


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
        thread_id=str(thread["id"]),
        session_id=str(thread["sessionId"]),
        status=status,
        active_flags=tuple(raw_flags),
        active_turn_id=active_turn_id,
        can_accept_direct_input=can_accept,
    )


def _request(
    websocket: Any,
    request_id: int | str,
    method: str,
    params: dict[str, Any],
    *,
    indeterminate_on_connection_loss: bool = False,
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
        except (ConnectionClosed, OSError, TimeoutError) as error:
            if indeterminate_on_connection_loss:
                raise RodexDispatchIndeterminateError(
                    f"{method} was sent but its acceptance is unknown"
                ) from error
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


def _require_turn_id(turn_id: str) -> None:
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise ValueError("turn ID must be non-empty")


def _turn_from_thread(thread: dict[str, Any], turn_id: str) -> dict[str, Any]:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise RodexControlError("app-server returned invalid thread turns")
    matches = [
        turn for turn in turns if isinstance(turn, dict) and turn.get("id") == turn_id
    ]
    if len(matches) != 1:
        raise RodexControlError(f"Codex turn was not found on the exact thread: {turn_id}")
    return matches[0]


def _turn_result(turn: dict[str, Any]) -> CodexTurnResult:
    turn_id = turn.get("id")
    status = turn.get("status")
    if not isinstance(turn_id, str) or status not in {
        "completed",
        "interrupted",
        "failed",
        "inProgress",
    }:
        raise RodexControlError("app-server returned an invalid Codex turn")
    items = turn.get("items")
    if not isinstance(items, list):
        raise RodexControlError("app-server returned invalid Codex turn items")
    agent_messages = [
        item.get("text")
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "agentMessage"
        and isinstance(item.get("text"), str)
    ]
    final_agent_message = agent_messages[-1] if agent_messages else None
    structured_output: object | None = None
    if final_agent_message is not None:
        with suppress(json.JSONDecodeError):
            structured_output = json.loads(final_agent_message)
    all_paths = sorted(
        {
            str(change["path"])
            for item in items
            if isinstance(item, dict) and item.get("type") == "fileChange"
            for change in item.get("changes", [])
            if isinstance(change, dict)
            and isinstance(change.get("path"), str)
            and change["path"]
        }
    )
    path_limit = 100
    return CodexTurnResult(
        turn_id=turn_id,
        status=status,
        final_agent_message=final_agent_message,
        structured_output=structured_output,
        error=turn.get("error"),
        started_at=_optional_int(turn.get("startedAt"), "startedAt"),
        completed_at=_optional_int(turn.get("completedAt"), "completedAt"),
        duration_ms=_optional_int(turn.get("durationMs"), "durationMs"),
        changed_paths=tuple(all_paths[:path_limit]),
        changed_paths_truncated=len(all_paths) > path_limit,
    )


def _optional_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RodexControlError(f"app-server returned invalid {field_name}")
    return value


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


def _belongs_to_thread(payload: dict[str, Any], codex_session_id: CodexSessionId) -> bool:
    params = payload.get("params")
    return isinstance(params, dict) and params.get("threadId") == str(codex_session_id)


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
    ready: dict[str, Any], codex_session_id: CodexSessionId
) -> str | None:
    active_turns = _nested(ready, "params", "activeTurns")
    if not isinstance(active_turns, dict):
        raise RodexControlError("Codex event stream sent invalid active-turn state")
    turn_id = active_turns.get(str(codex_session_id))
    if turn_id is not None and not isinstance(turn_id, str):
        raise RodexControlError("Codex event stream sent an invalid active turn id")
    return turn_id
