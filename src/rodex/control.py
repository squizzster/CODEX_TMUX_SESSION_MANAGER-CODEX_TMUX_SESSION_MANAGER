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

from .app_server_contract import CODEX_APP_SERVER, RODEX_CONTROL_APP_SERVER_CLIENT
from .protocol_proxy import CONTROL_CONNECTION_PATH, EVENT_STREAM_READY_METHOD

Connector = Callable[..., Any]
Revalidate = Callable[[], None]
EventWriter = Callable[[str], None]
_FINAL_AGENT_MESSAGE_BYTE_LIMIT = 64 * 1024
_MUTATION_RESPONSE_TIMEOUT_SECONDS = 5.0


class RodexControlError(RuntimeError):
    """A named live Codex control or event operation failed."""


class RodexDispatchIndeterminateError(RodexControlError):
    """A mutating request was sent but its acceptance could not be observed."""

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        dispatch_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.dispatch_id = dispatch_id
        self.thread_id = thread_id
        self.turn_id = turn_id


class RodexWaitTimeoutError(RodexControlError):
    """An exact wait timed out without interrupting its target turn."""


class _RodexRequestDeadlineError(RodexControlError):
    """An internal App Server request exhausted its caller's total deadline."""


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
    cwd: str
    status: str
    active_flags: tuple[str, ...]
    active_turn_id: str | None
    can_accept_direct_input: bool | None


@dataclass(frozen=True, slots=True)
class PromptDispatch:
    """How one prompt was accepted by the existing Codex thread."""

    action: Literal["started", "steered"]
    turn_id: str
    dispatch_id: str
    thread_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CodexDispatchMatch:
    """One user-message observation attributable to an opaque dispatch ID."""

    turn_id: str
    turn_status: Literal["completed", "interrupted", "failed", "inProgress"]
    user_message_item_id: str


@dataclass(frozen=True, slots=True)
class CodexDispatchStatus:
    """Live evidence for a caller-owned dispatch ID on one exact thread."""

    dispatch_id: str
    observation: Literal["not_observed", "accepted", "ambiguous"]
    matches: tuple[CodexDispatchMatch, ...]


@dataclass(frozen=True, slots=True)
class CodexTurnResult:
    """One live App Server turn reduced to Rodex's bounded result contract."""

    turn_id: str
    status: Literal["completed", "interrupted", "failed", "inProgress"]
    final_agent_message: str | None
    final_agent_message_bytes: int
    final_agent_message_truncated: bool
    structured_output: object | None
    error: object | None
    started_at: int | None
    completed_at: int | None
    duration_ms: int | None
    changed_paths: tuple[str, ...]
    changed_paths_truncated: bool


@dataclass(frozen=True, slots=True)
class _MutationDispatchContext:
    dispatch_id: str | None
    thread_id: str
    turn_id: str | None = None


class CodexControlClient:
    """Verify, inspect, steer, wait for, and tail one loaded Codex thread."""

    def __init__(
        self,
        *,
        connector: Connector = unix_connect,
        request_id_factory: Callable[[], str] = lambda: f"rodex:{uuid.uuid4()}",
        dispatch_id_factory: Callable[[], str] = lambda: f"rodex:dispatch:{uuid.uuid4()}",
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connect = connector
        self._request_id_factory = request_id_factory
        self._dispatch_id_factory = dispatch_id_factory
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
        dispatch_id = self._new_dispatch_id()
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
                            CODEX_APP_SERVER.turn_steer_method,
                            {
                                "threadId": str(control.codex_session_id),
                                "expectedTurnId": state.active_turn_id,
                                "input": user_input,
                                "clientUserMessageId": dispatch_id,
                            },
                            indeterminate_context=_MutationDispatchContext(
                                dispatch_id,
                                state.thread_id,
                                state.active_turn_id,
                            ),
                            monotonic=self._monotonic,
                        )
                        return PromptDispatch(
                            "steered",
                            _turn_id(result),
                            dispatch_id,
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
                        CODEX_APP_SERVER.turn_start_method,
                        {
                            "threadId": str(control.codex_session_id),
                            "input": user_input,
                            "clientUserMessageId": dispatch_id,
                        },
                        indeterminate_context=_MutationDispatchContext(
                            dispatch_id,
                            state.thread_id,
                        ),
                        monotonic=self._monotonic,
                    )
                    return PromptDispatch(
                        "started",
                        _turn_id(result),
                        dispatch_id,
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
        dispatch_id: str | None = None,
        revalidate: Revalidate = lambda: None,
    ) -> PromptDispatch:
        """Start only when the verified thread is observed idle."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        resolved_dispatch_id = self._resolve_dispatch_id(dispatch_id)
        try:
            with self._open_events(control.protocol_event_socket_path) as events:
                _expect_event_stream_ready(events)
                with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                    thread = self._verify_and_read_thread(
                        websocket,
                        control.codex_session_id,
                        require_compatible=True,
                    )
                    state = _thread_state(thread)
                    if state.status != "idle":
                        raise RodexControlError(
                            "Codex thread is not idle; use _steer with the exact turn ID"
                        )
                    if state.can_accept_direct_input is False:
                        raise RodexControlError("Codex thread does not accept direct input")
                    revalidate()
                    result = _request(
                        websocket,
                        self._request_id_factory(),
                        CODEX_APP_SERVER.turn_start_method,
                        {
                            "threadId": state.thread_id,
                            "input": [{"type": "text", "text": prompt}],
                            "clientUserMessageId": resolved_dispatch_id,
                        },
                        indeterminate_context=_MutationDispatchContext(
                            resolved_dispatch_id,
                            state.thread_id,
                        ),
                        monotonic=self._monotonic,
                    )
                    return PromptDispatch(
                        "started",
                        _turn_id(result),
                        resolved_dispatch_id,
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
        dispatch_id: str | None = None,
        revalidate: Revalidate = lambda: None,
    ) -> PromptDispatch:
        """Steer only the caller-specified exact active turn."""
        _require_turn_id(turn_id)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        resolved_dispatch_id = self._resolve_dispatch_id(dispatch_id)
        try:
            with self._open_events(control.protocol_event_socket_path) as events:
                _expect_event_stream_ready(events)
                with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                    thread = self._verify_and_read_thread(
                        websocket,
                        control.codex_session_id,
                        require_compatible=True,
                    )
                    state = _thread_state(thread, active_turn_id=turn_id)
                    if state.status != "active":
                        raise RodexControlError(
                            f"exact Codex turn is not active: expected {turn_id}, "
                            f"observed thread {state.status}"
                        )
                    if state.can_accept_direct_input is False:
                        raise RodexControlError("Codex thread does not accept direct input")
                    revalidate()
                    result = _request(
                        websocket,
                        self._request_id_factory(),
                        CODEX_APP_SERVER.turn_steer_method,
                        {
                            "threadId": state.thread_id,
                            "expectedTurnId": turn_id,
                            "input": [{"type": "text", "text": prompt}],
                            "clientUserMessageId": resolved_dispatch_id,
                        },
                        indeterminate_context=_MutationDispatchContext(
                            resolved_dispatch_id,
                            state.thread_id,
                            turn_id,
                        ),
                        monotonic=self._monotonic,
                    )
                    return PromptDispatch(
                        "steered",
                        _turn_id(result),
                        resolved_dispatch_id,
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
                _expect_event_stream_ready(events)
                with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                    thread = self._verify_and_read_thread(
                        websocket,
                        control.codex_session_id,
                        require_compatible=True,
                    )
                    state = _thread_state(thread, active_turn_id=turn_id)
                    if state.status != "active":
                        raise RodexControlError(
                            f"exact Codex turn is not active: expected {turn_id}, "
                            f"observed thread {state.status}"
                        )
                    revalidate()
                    _request(
                        websocket,
                        self._request_id_factory(),
                        CODEX_APP_SERVER.turn_interrupt_method,
                        {"threadId": state.thread_id, "turnId": turn_id},
                        indeterminate_context=_MutationDispatchContext(
                            None,
                            state.thread_id,
                            turn_id,
                        ),
                        monotonic=self._monotonic,
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

    def dispatch_status(
        self,
        control: LiveRodexControl,
        dispatch_id: str,
        *,
        revalidate: Revalidate = lambda: None,
    ) -> tuple[CodexThreadState, CodexDispatchStatus]:
        """Observe where one caller-owned dispatch ID appears on the exact thread."""
        _require_dispatch_id(dispatch_id)
        try:
            with self._open_protocol(control.protocol_proxy_socket_path) as websocket:
                thread = self._verify_and_read_thread(
                    websocket,
                    control.codex_session_id,
                    include_turns=True,
                    require_compatible=True,
                )
            revalidate()
            return _thread_state(thread), _dispatch_status(thread, dispatch_id)
        except (ConnectionClosed, InvalidHandshake, OSError) as error:
            raise RodexControlError(f"Codex control connection ended: {error}") from error

    def _new_dispatch_id(self) -> str:
        return self._resolve_dispatch_id(None)

    def _resolve_dispatch_id(self, dispatch_id: str | None) -> str:
        resolved = self._dispatch_id_factory() if dispatch_id is None else dispatch_id
        _require_dispatch_id(resolved)
        return resolved

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
            with self._open_events(
                control.protocol_event_socket_path,
                open_timeout=_wait_remaining(deadline, self._monotonic, turn_id, 2),
            ) as events:
                ready = _expect_event_stream_ready(
                    events,
                    timeout_seconds=_wait_remaining(deadline, self._monotonic, turn_id, 2),
                    preserve_timeout=True,
                )
                with self._open_protocol(
                    control.protocol_proxy_socket_path,
                    open_timeout=_wait_remaining(deadline, self._monotonic, turn_id, 2),
                ) as websocket:
                    thread = self._verify_and_read_thread(
                        websocket,
                        control.codex_session_id,
                        include_turns=True,
                        require_compatible=True,
                        deadline=deadline,
                    )
                state = _thread_state(
                    thread,
                    active_turn_id=_ready_active_turn_id(ready, control.codex_session_id),
                )
                turn = _turn_from_thread(thread, turn_id)
                result = _turn_result(turn)
                revalidate()
                _wait_remaining(deadline, self._monotonic, turn_id)
                if result.status != "inProgress":
                    return state, result
                if state.status != "active":
                    raise RodexControlError(
                        f"exact Codex turn is not active: expected {turn_id}, "
                        f"observed thread {state.status}"
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
                    if payload.get("method") != CODEX_APP_SERVER.turn_completed_method:
                        continue
                    completed = _nested(payload, "params", "turn")
                    if not isinstance(completed, dict) or completed.get("id") != turn_id:
                        continue
                    with self._open_protocol(
                        control.protocol_proxy_socket_path,
                        open_timeout=_wait_remaining(deadline, self._monotonic, turn_id, 2),
                    ) as websocket:
                        terminal_thread = self._verify_and_read_thread(
                            websocket,
                            control.codex_session_id,
                            include_turns=True,
                            require_compatible=True,
                            deadline=deadline,
                        )
                    terminal_result = _turn_result(
                        _turn_from_thread(terminal_thread, turn_id)
                    )
                    if terminal_result.status == "inProgress":
                        raise RodexControlError(
                            "Codex completion event preceded terminal turn state"
                        )
                    revalidate()
                    _wait_remaining(deadline, self._monotonic, turn_id)
                    return _thread_state(terminal_thread), terminal_result
        except RodexWaitTimeoutError:
            raise
        except _RodexRequestDeadlineError as error:
            raise RodexWaitTimeoutError(
                f"timed out waiting for exact Codex turn {turn_id}"
            ) from error
        except TimeoutError as error:
            if deadline is not None and self._monotonic() >= deadline:
                raise RodexWaitTimeoutError(
                    f"timed out waiting for exact Codex turn {turn_id}"
                ) from error
            raise RodexControlError("timed out opening Codex event stream") from error
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
                    if method == CODEX_APP_SERVER.turn_completed_method or (
                        method == CODEX_APP_SERVER.thread_status_changed_method
                        and status == "idle"
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

    def stream_events(
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

    def _open_protocol(self, socket_path: Path, *, open_timeout: float = 2) -> Any:
        return self._connect(
            str(socket_path),
            uri=f"ws://localhost{CONTROL_CONNECTION_PATH}",
            compression=None,
            open_timeout=open_timeout,
            close_timeout=1,
            max_size=None,
        )

    def _open_events(self, socket_path: Path, *, open_timeout: float = 2) -> Any:
        return self._connect(
            str(socket_path),
            uri="ws://localhost/events",
            compression=None,
            open_timeout=open_timeout,
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
        deadline: float | None = None,
    ) -> dict[str, Any]:
        self._initialize_protocol(
            websocket,
            require_compatible=require_compatible,
            deadline=deadline,
        )
        loaded = _request(
            websocket,
            1,
            CODEX_APP_SERVER.thread_loaded_list_method,
            {},
            deadline=deadline,
            monotonic=self._monotonic,
        )
        loaded_ids = loaded.get("data")
        expected = str(expected_codex_session_id)
        if not isinstance(loaded_ids, list) or expected not in loaded_ids:
            raise RodexControlError(
                f"live endpoint does not contain expected Codex session {expected}"
            )
        result = _request(
            websocket,
            2,
            CODEX_APP_SERVER.thread_read_method,
            {"threadId": expected, "includeTurns": include_turns},
            deadline=deadline,
            monotonic=self._monotonic,
        )
        thread = result.get("thread")
        if (
            not isinstance(thread, dict)
            or thread.get("id") != expected
            or not isinstance(thread.get("sessionId"), str)
        ):
            raise RodexControlError("app-server returned an unexpected Codex thread")
        return thread

    def _initialize_protocol(
        self,
        websocket: Any,
        *,
        require_compatible: bool,
        deadline: float | None = None,
    ) -> str:
        initialize_result = _request(
            websocket,
            0,
            CODEX_APP_SERVER.initialize_method,
            CODEX_APP_SERVER.initialize_params(RODEX_CONTROL_APP_SERVER_CLIENT),
            deadline=deadline,
            monotonic=self._monotonic,
        )
        version = (
            CODEX_APP_SERVER.require_supported_version(initialize_result)
            if require_compatible
            else CODEX_APP_SERVER.version(initialize_result)
        )
        websocket.send(json.dumps(CODEX_APP_SERVER.initialized_notification()))
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
        or method == CODEX_APP_SERVER.thread_status_changed_method
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
    cwd = thread.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise RodexControlError("app-server returned an invalid thread working directory")
    if status != "active":
        active_turn_id = None
    return CodexThreadState(
        thread_id=str(thread["id"]),
        session_id=str(thread["sessionId"]),
        cwd=cwd,
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
    indeterminate_context: _MutationDispatchContext | None = None,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    response_deadline = deadline
    if response_deadline is None and indeterminate_context is not None:
        response_deadline = monotonic() + _MUTATION_RESPONSE_TIMEOUT_SECONDS
    try:
        websocket.send(
            json.dumps(
                CODEX_APP_SERVER.request(request_id, method, params),
                separators=(",", ":"),
            )
        )
    except (ConnectionClosed, OSError, TimeoutError) as error:
        if indeterminate_context is not None:
            raise RodexDispatchIndeterminateError(
                f"{method} dispatch failed before acceptance could be observed",
                method=method,
                dispatch_id=indeterminate_context.dispatch_id,
                thread_id=indeterminate_context.thread_id,
                turn_id=indeterminate_context.turn_id,
            ) from error
        raise RodexControlError(f"could not send {method}") from error
    while True:
        receive_timeout = 5.0
        if response_deadline is not None:
            remaining = response_deadline - monotonic()
            if remaining <= 0:
                if indeterminate_context is not None:
                    raise RodexDispatchIndeterminateError(
                        f"{method} was sent but its acceptance is unknown",
                        method=method,
                        dispatch_id=indeterminate_context.dispatch_id,
                        thread_id=indeterminate_context.thread_id,
                        turn_id=indeterminate_context.turn_id,
                    )
                raise _RodexRequestDeadlineError(f"deadline expired during {method}")
            receive_timeout = min(receive_timeout, remaining)
        try:
            message = websocket.recv(timeout=receive_timeout)
        except (ConnectionClosed, OSError, TimeoutError) as error:
            if response_deadline is not None and monotonic() >= response_deadline:
                if indeterminate_context is not None:
                    raise RodexDispatchIndeterminateError(
                        f"{method} was sent but its acceptance is unknown",
                        method=method,
                        dispatch_id=indeterminate_context.dispatch_id,
                        thread_id=indeterminate_context.thread_id,
                        turn_id=indeterminate_context.turn_id,
                    ) from error
                raise _RodexRequestDeadlineError(
                    f"deadline expired during {method}"
                ) from error
            if indeterminate_context is not None:
                raise RodexDispatchIndeterminateError(
                    f"{method} was sent but its acceptance is unknown",
                    method=method,
                    dispatch_id=indeterminate_context.dispatch_id,
                    thread_id=indeterminate_context.thread_id,
                    turn_id=indeterminate_context.turn_id,
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


def _require_dispatch_id(dispatch_id: str) -> None:
    if not isinstance(dispatch_id, str) or not dispatch_id.strip():
        raise ValueError("dispatch ID must be non-empty")


def _dispatch_status(thread: dict[str, Any], dispatch_id: str) -> CodexDispatchStatus:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise RodexControlError("app-server returned invalid thread turns")
    matches: list[CodexDispatchMatch] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_id = turn.get("id")
        turn_status = turn.get("status")
        items = turn.get("items")
        if not isinstance(turn_id, str) or turn_status not in {
            "completed",
            "interrupted",
            "failed",
            "inProgress",
        }:
            raise RodexControlError("app-server returned an invalid Codex turn")
        if not isinstance(items, list):
            raise RodexControlError("app-server returned invalid Codex turn items")
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("type") != "userMessage"
                or item.get("clientId") != dispatch_id
            ):
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                raise RodexControlError("app-server returned an invalid user message item")
            matches.append(CodexDispatchMatch(turn_id, turn_status, item_id))
    observation: Literal["not_observed", "accepted", "ambiguous"]
    if not matches:
        observation = "not_observed"
    elif len(matches) == 1:
        observation = "accepted"
    else:
        observation = "ambiguous"
    return CodexDispatchStatus(dispatch_id, observation, tuple(matches))


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
    explicit_final_messages = [
        item.get("text")
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "agentMessage"
        and item.get("phase") == "final_answer"
        and isinstance(item.get("text"), str)
    ]
    phase_unknown_messages = [
        item.get("text")
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "agentMessage"
        and item.get("phase") is None
        and isinstance(item.get("text"), str)
    ]
    full_final_agent_message = (
        explicit_final_messages[-1]
        if explicit_final_messages
        else phase_unknown_messages[-1]
        if phase_unknown_messages
        else None
    )
    final_agent_message, final_agent_message_bytes, final_agent_message_truncated = (
        _bounded_utf8_text(full_final_agent_message, _FINAL_AGENT_MESSAGE_BYTE_LIMIT)
    )
    structured_output: object | None = None
    if final_agent_message is not None and not final_agent_message_truncated:
        with suppress(json.JSONDecodeError, ValueError):
            structured_output = json.loads(
                final_agent_message,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-standard JSON constant: {value}")
                ),
            )
    all_paths = sorted(
        {
            str(change["path"])
            for item in items
            if isinstance(item, dict)
            and item.get("type") == "fileChange"
            and item.get("status") == "completed"
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
        final_agent_message_bytes=final_agent_message_bytes,
        final_agent_message_truncated=final_agent_message_truncated,
        structured_output=structured_output,
        error=turn.get("error"),
        started_at=_optional_int(turn.get("startedAt"), "startedAt"),
        completed_at=_optional_int(turn.get("completedAt"), "completedAt"),
        duration_ms=_optional_int(turn.get("durationMs"), "durationMs"),
        changed_paths=tuple(all_paths[:path_limit]),
        changed_paths_truncated=len(all_paths) > path_limit,
    )


def _bounded_utf8_text(text: str | None, limit: int) -> tuple[str | None, int, bool]:
    if text is None:
        return None, 0, False
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, len(encoded), False
    return encoded[:limit].decode("utf-8", errors="ignore"), len(encoded), True


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


def _expect_event_stream_ready(
    events: Any, *, timeout_seconds: float = 2, preserve_timeout: bool = False
) -> dict[str, Any]:
    try:
        message = events.recv(timeout=timeout_seconds)
    except TimeoutError as error:
        if preserve_timeout:
            raise
        raise RodexControlError("timed out opening Codex event stream") from error
    payload = _protocol_payload(message)
    if payload is None or payload.get("method") != EVENT_STREAM_READY_METHOD:
        raise RodexControlError("Codex event stream did not send its ready signal")
    return payload


def _wait_remaining(
    deadline: float | None,
    monotonic: Callable[[], float],
    turn_id: str,
    cap: float | None = None,
) -> float:
    if deadline is None:
        return 2 if cap is None else cap
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise RodexWaitTimeoutError(f"timed out waiting for exact Codex turn {turn_id}")
    return remaining if cap is None else min(remaining, cap)


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
