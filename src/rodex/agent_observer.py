"""Dedicated tmux presentation for exact live and durable sub-agent facts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Final

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect

from rodex_registry import (
    RodexAgentObserverTurnEvidence,
    RodexAgentTraceSnapshot,
    read_rodex_agent_observer_turn_evidence,
    read_rodex_agent_trace,
    read_rodex_agent_trace_cursor,
)

from .app_server_contract import CODEX_APP_SERVER
from .observer_contract import (
    OBSERVER_CONTROL_SOCKET_PREFIX,
    OBSERVER_FRAME_LENGTH,
    OBSERVER_MAX_FRAME_BYTES,
    OBSERVER_RECEIVE_DEADLINE_SECONDS,
    OBSERVER_RECOVERED_TARGET_LIMIT,
    OBSERVER_SCHEMA,
)
from .observer_pane import ObserverPaneController
from .observer_projection import (
    optional_uuid_text,
    project_agent_message_event,
    project_collaboration_invocation_event,
    project_subagent_activity_event,
    project_user_message_event,
)
from .observer_state import ObserverStateReducer
from .protocol_proxy import AGENT_OBSERVER_EVENT_STREAM_PATH
from .tmux_session_capability import TmuxRuntimeCapability

OBSERVER_TRACE_PAGE_SIZE: Final = 500
_RODEX_SESSION_ID_PATTERN: Final = re.compile(r"[0-9a-f]{16}")
_TURN_REQUEST_KINDS: Final = {
    "collaboration.spawn_agent": "initial",
    "collaboration.followup_task": "follow_up",
}
_ANSI_ESCAPE_PATTERN: Final = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_SCOPE_UNAVAILABLE_DETAIL: Final = (
    "Rodex could not correlate an exact same-turn root user message."
)
Runner = Callable[..., subprocess.CompletedProcess[str]]
CursorReader = Callable[[int, Path], uuid.UUID | None]
EventSender = Callable[[Path, dict[str, object]], None]
_OBSERVER_FRAME_LENGTH = OBSERVER_FRAME_LENGTH
OBSERVER_SEND_MAX_ATTEMPTS: Final = 8
OBSERVER_SEND_RETRY_BUDGET_SECONDS: Final = 1.0
OBSERVER_SEND_RETRY_INITIAL_SECONDS: Final = 0.01
OBSERVER_SEND_RETRY_MAX_SECONDS: Final = 0.25
OBSERVER_SEND_PARKED_RETRY_SECONDS: Final = 1.0
OBSERVER_SOCKET_OPERATION_TIMEOUT_SECONDS: Final = 0.25


def observer_control_socket_path(protocol_event_socket_path: Path) -> Path:
    """Return one private observer-control socket per exact runtime event socket."""
    runtime_digest = hashlib.sha256(
        os.fsencode(os.path.abspath(protocol_event_socket_path))
    ).hexdigest()[:16]
    return protocol_event_socket_path.with_name(
        f"{OBSERVER_CONTROL_SOCKET_PREFIX}{runtime_digest}.sock"
    )


def _observer_event_frame(event: dict[str, object]) -> bytes:
    payload = json.dumps(
        event,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _OBSERVER_FRAME_LENGTH.pack(len(payload)) + payload


def _send_observer_event_frame(path: Path, event: dict[str, object]) -> None:
    frame = _observer_event_frame(event)
    if len(frame) - _OBSERVER_FRAME_LENGTH.size > OBSERVER_MAX_FRAME_BYTES:
        raise ValueError("observer control payload exceeds the frame limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
        sender.settimeout(OBSERVER_SOCKET_OPERATION_TIMEOUT_SECONDS)
        sender.connect(str(path))
        sender.sendall(frame)


def _try_send_observer_event_frame(path: Path, event: dict[str, object]) -> None:
    """Send one small wake without waiting on the analytics publication path."""
    frame = _observer_event_frame(event)
    if len(frame) - _OBSERVER_FRAME_LENGTH.size > OBSERVER_MAX_FRAME_BYTES:
        raise ValueError("observer control payload exceeds the frame limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
        sender.setblocking(False)
        result = sender.connect_ex(str(path))
        if result != 0:
            raise OSError(result, os.strerror(result), str(path))
        if sender.send(frame) != len(frame):
            raise BlockingIOError("observer control frame was not accepted atomically")


class _ObserverEventDispatcher:
    """Transport the newest complete snapshot without reducing semantic state."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        parked_retry_seconds: float = OBSERVER_SEND_PARKED_RETRY_SECONDS,
    ) -> None:
        if parked_retry_seconds <= 0:
            raise ValueError("observer parked retry interval must be positive")
        self._events: queue.Queue[tuple[int, Path, dict[str, object]] | None] = queue.Queue(
            maxsize=1
        )
        self._stop = Event()
        self._updated = Event()
        self._monotonic = monotonic
        self._parked_retry_seconds = parked_retry_seconds
        self._lock = Lock()
        self._thread: Thread | None = None
        self._closed = False
        self._generation = 0

    def send(self, path: Path, snapshot: dict[str, object]) -> None:
        if snapshot.get("kind") != "observer_state_snapshot":
            raise ValueError("observer dispatcher accepts complete snapshots only")
        with self._lock:
            if self._closed:
                return
            self._generation += 1
            generation = self._generation
            self._replace_pending_locked((generation, path, snapshot))
            self._updated.set()
            if self._thread is None:
                self._thread = Thread(
                    target=self._run,
                    name="rodex-agent-observer-dispatch",
                    daemon=True,
                )
                self._thread.start()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            self._stop.set()
            self._replace_pending_locked(None)
            self._updated.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=OBSERVER_SOCKET_OPERATION_TIMEOUT_SECONDS + 1)

    def _run(self) -> None:
        while True:
            item = self._events.get()
            if item is None:
                return
            generation, path, event = item
            retry_delay = OBSERVER_SEND_RETRY_INITIAL_SECONDS
            deadline = self._monotonic() + OBSERVER_SEND_RETRY_BUDGET_SECONDS
            delivered = False
            for attempt in range(OBSERVER_SEND_MAX_ATTEMPTS):
                if self._superseded(generation) or self._monotonic() >= deadline:
                    break
                try:
                    _send_observer_event_frame(path, event)
                except ValueError:
                    break
                except OSError:
                    if attempt + 1 >= OBSERVER_SEND_MAX_ATTEMPTS:
                        break
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        break
                    if self._wait_for_update(generation, min(retry_delay, remaining)):
                        return
                    retry_delay = min(
                        retry_delay * 2,
                        OBSERVER_SEND_RETRY_MAX_SECONDS,
                    )
                else:
                    delivered = True
                    break
            while not delivered and not self._superseded(generation):
                if self._wait_for_update(generation, self._parked_retry_seconds):
                    return
                if self._superseded(generation):
                    break
                try:
                    _send_observer_event_frame(path, event)
                except ValueError:
                    break
                except OSError:
                    continue
                else:
                    delivered = True

    def _wait_for_update(self, generation: int, delay: float) -> bool:
        with self._lock:
            if self._closed:
                return True
            if generation != self._generation:
                return False
            self._updated.clear()
        self._updated.wait(delay)
        return self._stop.is_set()

    def _superseded(self, generation: int) -> bool:
        with self._lock:
            return self._closed or generation != self._generation

    def _replace_pending_locked(
        self,
        item: tuple[int, Path, dict[str, object]] | None,
    ) -> None:
        with suppress(queue.Empty):
            self._events.get_nowait()
        self._events.put_nowait(item)


def notify_agent_observer_trace_publication(
    protocol_event_socket_path: Path,
    trace_publication_sequence: int,
    caught_up: bool,
) -> None:
    """Best-effort wake after SQLite has committed a trace publication."""
    if (
        isinstance(trace_publication_sequence, bool)
        or not isinstance(trace_publication_sequence, int)
        or trace_publication_sequence < 1
    ):
        raise ValueError("trace publication sequence must be a positive integer")
    if not isinstance(caught_up, bool):
        raise ValueError("caught_up must be a boolean")
    event: dict[str, object] = {
        "schema": OBSERVER_SCHEMA,
        "kind": "trace_published",
        "trace_publication_sequence": trace_publication_sequence,
        "caught_up": caught_up,
    }
    with suppress(OSError):
        _try_send_observer_event_frame(
            observer_control_socket_path(protocol_event_socket_path),
            event,
        )


class AgentObserverCoordinator:
    """Coordinate stateless projections, canonical state, and tmux presentation."""

    def __init__(
        self,
        tmux_binary: str,
        capability: TmuxRuntimeCapability,
        primary_pane_target: str,
        protocol_event_socket_path: Path,
        *,
        runner: Runner = subprocess.run,
        cursor_reader: CursorReader = read_rodex_agent_trace_cursor,
        event_sender: EventSender | None = None,
        python_executable: str = sys.executable,
    ) -> None:
        self._protocol_event_socket_path = protocol_event_socket_path
        self._pane = ObserverPaneController(
            tmux_binary,
            capability,
            primary_pane_target,
            runner=runner,
            python_executable=python_executable,
        )
        self._cursor_reader = cursor_reader
        self._event_dispatcher = (
            _ObserverEventDispatcher() if event_sender is None else None
        )
        self._event_sender = (
            self._event_dispatcher.send
            if self._event_dispatcher is not None
            else event_sender
        )
        self._observer_state = ObserverStateReducer.producer()
        self._database_path: Path | None = None
        self._rodex_sessions_id: int | None = None
        self._rodex_session_id: str | None = None
        self._root_thread_id: uuid.UUID | None = None
        self._lifecycle_lock = Lock()
        self._closed = False

    def activate(
        self,
        *,
        database_path: Path,
        rodex_sessions_id: int,
        rodex_session_id: str,
        root_thread_id: uuid.UUID,
    ) -> None:
        """Bind the controller to the exact identity committed at registration."""
        with self._lifecycle_lock:
            self._activate_locked(
                database_path=database_path,
                rodex_sessions_id=rodex_sessions_id,
                rodex_session_id=rodex_session_id,
                root_thread_id=root_thread_id,
            )

    def _activate_locked(
        self,
        *,
        database_path: Path,
        rodex_sessions_id: int,
        rodex_session_id: str,
        root_thread_id: uuid.UUID,
    ) -> None:
        if self._closed:
            raise RuntimeError("agent observer pane controller is closed")
        if self._database_path is not None:
            raise RuntimeError("agent observer pane controller is already activated")
        if rodex_sessions_id < 1:
            raise ValueError("rodex_sessions_id must be positive")
        if _RODEX_SESSION_ID_PATTERN.fullmatch(rodex_session_id) is None:
            raise ValueError("rodex_session_id must be 16 lowercase hexadecimal characters")
        self._database_path = Path(os.path.abspath(database_path))
        self._rodex_sessions_id = rodex_sessions_id
        self._rodex_session_id = rodex_session_id
        self._root_thread_id = uuid.UUID(str(root_thread_id))

    def observe_protocol_event(self, event: Mapping[str, object] | None) -> None:
        """Create, reuse, or update the observer from one typed App Server item."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._observe_protocol_event_locked(event)

    def _observe_protocol_event_locked(
        self,
        event: Mapping[str, object] | None,
    ) -> None:
        if self._closed:
            return
        self._prune_protocol_terminal_state(event)
        collaboration_invocation = project_collaboration_invocation_event(event)
        if collaboration_invocation is not None:
            self._observe_collaboration_invocation(collaboration_invocation)
            return
        agent_message = project_agent_message_event(event)
        if agent_message is not None:
            target_thread_id = agent_message["thread_id"]
            if (
                self._observer_state.tracks_target(str(target_thread_id))
                and self._pane.locate() is not None
            ):
                self._send_observer_event(agent_message)
            return
        parent_user_message = project_user_message_event(event)
        if parent_user_message is not None:
            root_thread_id = self._root_thread_id
            if root_thread_id is not None and parent_user_message["thread_id"] == str(
                root_thread_id
            ):
                self._observer_state.remember_parent_user_message(parent_user_message)
            return
        projected = project_subagent_activity_event(event)
        if projected is None or self._database_path is None:
            return
        root_thread_id = self._root_thread_id
        if root_thread_id is None or projected["thread_id"] != str(root_thread_id):
            return
        item = projected["item"]
        assert isinstance(item, dict)
        item_id = str(item["id"])
        target_thread_id = str(item["agent_thread_id"])
        collaboration_invocation = self._observer_state.collaboration_invocation(item_id)
        if collaboration_invocation is not None:
            projected["collaboration_invocation"] = collaboration_invocation["item"]
        is_new_spawn = (
            projected["method"] == "item/started" and item["activity_kind"] == "started"
        )
        is_known = self._observer_state.is_known_activity(item_id)
        target_is_tracked = self._observer_state.tracks_target(target_thread_id)
        if not is_new_spawn and not is_known and not target_is_tracked:
            return
        if is_new_spawn:
            assert self._rodex_sessions_id is not None
            cursor = self._cursor_reader(self._rodex_sessions_id, self._database_path)
            projected["after_event_id"] = None if cursor is None else str(cursor)
        self._observer_state.remember_activity(
            item_id,
            target_thread_id,
            projected,
            new_spawn=is_new_spawn,
        )
        tool_name = _projected_invocation_tool_name(collaboration_invocation)
        has_root_request_context = tool_name in _TURN_REQUEST_KINDS or tool_name == (
            "collaboration.send_message"
        )
        request_event = (
            self._root_request_context_event(projected)
            if has_root_request_context
            else None
        )
        if request_event is not None:
            projected["root_request_context_follows"] = True
        pane_target = self._pane.locate()
        if pane_target is None:
            if not is_new_spawn:
                self._prune_completed_activity(projected)
                return
            assert self._rodex_sessions_id is not None
            assert self._rodex_session_id is not None
            assert self._root_thread_id is not None
            pane_target = self._pane.create(
                database_path=self._database_path,
                rodex_sessions_id=self._rodex_sessions_id,
                rodex_session_id=self._rodex_session_id,
                root_thread_id=self._root_thread_id,
                protocol_event_socket_path=self._protocol_event_socket_path,
                initial_event=projected,
            )
            if pane_target is not None:
                self._observer_state.observe(projected)
                if request_event is not None:
                    self._observer_state.mark_root_request_context_sent(item_id)
                    self._send_observer_event(request_event)
            self._prune_completed_activity(projected)
            return
        self._send_observer_event(projected)
        if request_event is not None:
            self._observer_state.mark_root_request_context_sent(item_id)
            self._send_observer_event(request_event)
        self._prune_completed_activity(projected)

    def _observe_collaboration_invocation(
        self,
        invocation: dict[str, object],
    ) -> None:
        if invocation.get("thread_id") != str(self._root_thread_id):
            return
        item = invocation.get("item")
        if not isinstance(item, dict):
            return
        if item.get("sender_thread_id") != str(self._root_thread_id):
            return
        item_id = str(item["id"])
        self._observer_state.remember_collaboration_invocation(item_id, invocation)
        activity = self._observer_state.activity(item_id)
        if activity is not None:
            activity["collaboration_invocation"] = item
        receiver_ids = item.get("receiver_thread_ids")
        targets_tracked = isinstance(receiver_ids, list) and any(
            isinstance(target, str) and self._observer_state.tracks_target(target)
            for target in receiver_ids
        )
        if activity is None and not targets_tracked:
            if invocation.get("method") == "item/completed":
                self._observer_state.forget_collaboration_invocation(item_id)
            return
        pane_target = self._pane.locate()
        if pane_target is None:
            return
        request_event = None
        if activity is not None and not self._observer_state.root_request_context_was_sent(
            item_id
        ):
            request_event = self._root_request_context_event(activity)
            if request_event is not None:
                invocation["root_request_context_follows"] = True
        self._send_observer_event(invocation)
        if request_event is not None:
            self._observer_state.mark_root_request_context_sent(item_id)
            self._send_observer_event(request_event)

    def _prune_completed_activity(self, event: Mapping[str, object]) -> None:
        if event.get("method") != "item/completed":
            return
        item = event.get("item")
        if not isinstance(item, Mapping):
            return
        item_id = str(item.get("id", ""))
        target_thread_id = str(item.get("agent_thread_id", ""))
        self._observer_state.complete_activity(item_id, target_thread_id)

    def _prune_protocol_terminal_state(
        self,
        event: Mapping[str, object] | None,
    ) -> None:
        if event is None:
            return
        method = event.get("method")
        params = event.get("params")
        if not isinstance(params, Mapping):
            return
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            return
        if method == CODEX_APP_SERVER.turn_completed_method and thread_id == str(
            self._root_thread_id
        ):
            self._observer_state.remember_parent_user_message(None)
        inactive = method == CODEX_APP_SERVER.turn_completed_method
        if method == CODEX_APP_SERVER.thread_status_changed_method:
            status = params.get("status")
            inactive = isinstance(status, Mapping) and status.get("type") != "active"
        if inactive and self._observer_state.tracks_target(thread_id):
            self._prune_target_thread(thread_id)

    def _prune_target_thread(self, target_thread_id: str) -> None:
        self._send_observer_snapshot(
            self._observer_state.prune_protocol_target(target_thread_id)
        )

    def reset_after_disconnect(self) -> None:
        """Prune all primary-connection correlation state after its producer exits."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._send_observer_snapshot(self._observer_state.reset_epoch())

    def close(self) -> None:
        """Release only in-process state; tmux owns the persistent presentation pane."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            if self._event_dispatcher is not None:
                self._event_dispatcher.close()
            self._observer_state.discard_producer_state()

    def _root_request_context_event(
        self,
        activity_event: Mapping[str, object],
    ) -> dict[str, object] | None:
        parent_user_message = self._observer_state.latest_parent_user_message
        if parent_user_message is None or parent_user_message.get(
            "turn_id"
        ) != activity_event.get("turn_id"):
            return None
        activity_item = activity_event.get("item")
        user_item = parent_user_message.get("item")
        if not isinstance(activity_item, Mapping) or not isinstance(user_item, Mapping):
            return None
        return {
            "schema": OBSERVER_SCHEMA,
            "kind": "root_request_context",
            "thread_id": activity_event["thread_id"],
            "turn_id": activity_event.get("turn_id"),
            "target_thread_id": activity_item["agent_thread_id"],
            "activity_item_id": activity_item["id"],
            "item": dict(user_item),
        }

    def _send_observer_event(self, event: dict[str, object]) -> None:
        self._send_observer_snapshot(self._observer_state.observe(event))

    def _send_observer_snapshot(self, snapshot: dict[str, object]) -> None:
        try:
            assert self._event_sender is not None
            self._event_sender(
                observer_control_socket_path(self._protocol_event_socket_path),
                snapshot,
            )
        except OSError:
            return


@dataclass(slots=True)
class _PendingAgentRequest:
    activity_item_id: str
    request_kind: str
    text_blocks: tuple[str, ...] = ()


@dataclass(slots=True)
class _AgentTurnPresentation:
    activity_item_id: str | None = None
    request_kind: str | None = None
    request_text_blocks: tuple[str, ...] = ()
    started_at_utc: str | None = None
    completed_tool_call_ids: set[str] = field(default_factory=set)
    token_usage: dict[str, int] = field(default_factory=dict)
    weekly_limit_used_percent: float | None = None
    last_context: tuple[object, object] | None = None
    evidence: RodexAgentObserverTurnEvidence | None = None
    lineage_rendered: bool = False
    last_work_signature: tuple[str, ...] | None = None


AgentTurnKey = tuple[str, str]
AgentActivity = tuple[str, str, str]
ObserverEventIdentity = tuple[str, str, str]
AgentActivityKey = tuple[str, str]


class AgentObserverView:
    """Render exact agent activity as a concise, human-readable feed."""

    def __init__(
        self,
        *,
        root_thread_id: uuid.UUID,
        initial_event: Mapping[str, object],
    ) -> None:
        self._root_thread_id = str(root_thread_id)
        self._after_event_id: uuid.UUID | None = None
        self._target_states: dict[str, str | None] = {}
        self._active_request_ids: set[tuple[str, str]] = set()
        self._terminal_drain_complete = False
        self._last_trace_publication_sequence = 0
        self._target_paths: dict[str, str] = {}
        self._seen_activity_item_ids: set[ObserverEventIdentity] = set()
        self._activity_items: dict[AgentActivityKey, AgentActivity] = {}
        self._pending_live_invocations: dict[AgentActivityKey, dict[str, object]] = {}
        self._invocation_tool_names: dict[ObserverEventIdentity, str] = {}
        self._rendered_invocation_ids: set[ObserverEventIdentity] = set()
        self._rendered_prompt_ids: set[ObserverEventIdentity] = set()
        self._reported_unavailable_prompt_ids: set[ObserverEventIdentity] = set()
        self._seen_agent_message_ids: set[ObserverEventIdentity] = set()
        self._seen_parent_request_keys: set[tuple[str, str, str]] = set()
        self._root_request_text_by_activity: dict[AgentActivityKey, tuple[str, ...]] = {}
        self._pending_requests: dict[str, deque[_PendingAgentRequest]] = {}
        self._turn_presentations: dict[AgentTurnKey, _AgentTurnPresentation] = {}
        self._activity_turn_keys: dict[tuple[str, str], AgentTurnKey] = {}
        self._turns_needing_evidence: set[AgentTurnKey] = set()
        self._pending_terminal_events: dict[AgentTurnKey, tuple[str, object]] = {}
        self._terminal_cleanup_targets: set[str] = set()
        self._observer_state = ObserverStateReducer.consumer()
        self._observer_transport_epoch: int | None = None
        self._initial_lines = self.accept_app_server_event(initial_event)

    @property
    def after_event_id(self) -> uuid.UUID | None:
        return self._after_event_id

    @property
    def target_thread_ids(self) -> frozenset[str]:
        return frozenset(self._target_states)

    @property
    def target_turn_keys(self) -> tuple[tuple[str, str], ...]:
        """Return only active or terminal-pending exact agent turns."""
        return tuple(sorted(self._turns_needing_evidence))

    @property
    def monitoring(self) -> bool:
        return bool(self._target_states) and not self._terminal_drain_complete

    @property
    def initial_lines(self) -> tuple[str, ...]:
        return tuple(self._initial_lines)

    def accept_observer_state_snapshot(
        self,
        snapshot: Mapping[str, object],
    ) -> tuple[dict[str, object], ...]:
        """Return only semantic changes from one complete transport snapshot."""
        delta = self._observer_state.consume_snapshot(snapshot)
        if delta.state_replaced:
            if self._observer_transport_epoch is not None:
                self._reset_connection_epoch_presentation()
            self._observer_transport_epoch = delta.epoch
        for target_thread_id in delta.removed_target_thread_ids:
            self._prune_terminal_target(target_thread_id)
        return (*delta.tombstone_events, *delta.upserted_events)

    def _reset_connection_epoch_presentation(self) -> None:
        """Discard only presentation facts derived from the old primary connection."""
        self._target_states.clear()
        self._active_request_ids.clear()
        self._terminal_drain_complete = False
        self._target_paths.clear()
        self._seen_activity_item_ids.clear()
        self._activity_items.clear()
        self._pending_live_invocations.clear()
        self._invocation_tool_names.clear()
        self._rendered_invocation_ids.clear()
        self._rendered_prompt_ids.clear()
        self._reported_unavailable_prompt_ids.clear()
        self._seen_agent_message_ids.clear()
        self._seen_parent_request_keys.clear()
        self._root_request_text_by_activity.clear()
        self._pending_requests.clear()
        self._turn_presentations.clear()
        self._activity_turn_keys.clear()
        self._turns_needing_evidence.clear()
        self._pending_terminal_events.clear()
        self._terminal_cleanup_targets.clear()

    def accept_app_server_event(self, event: Mapping[str, object]) -> list[str]:
        """Accept one sanitized activity without inferring its collaboration tool."""
        if event.get("schema") != OBSERVER_SCHEMA:
            return []
        if event.get("kind") != "app_server_subagent_activity":
            return []
        if event.get("thread_id") != self._root_thread_id:
            return []
        item = event.get("item")
        if not isinstance(item, Mapping):
            return []
        was_monitoring = self.monitoring
        cursor = event.get("after_event_id")
        if not was_monitoring and isinstance(cursor, str):
            self._after_event_id = uuid.UUID(cursor)
        item_id = item.get("id")
        activity_kind = item.get("activity_kind")
        target_thread_id = item.get("agent_thread_id")
        agent_path = item.get("agent_path")
        if (
            not isinstance(item_id, str)
            or not isinstance(activity_kind, str)
            or not isinstance(target_thread_id, str)
            or not isinstance(agent_path, str)
        ):
            return []
        activity_identity = (
            target_thread_id,
            item_id,
            "app_server_subagent_activity",
        )
        if activity_identity in self._seen_activity_item_ids:
            return []
        self._seen_activity_item_ids.add(activity_identity)
        self._terminal_cleanup_targets.discard(target_thread_id)
        self._target_states[target_thread_id] = activity_kind
        self._target_paths[target_thread_id] = agent_path
        activity_key = (target_thread_id, item_id)
        self._activity_items[activity_key] = (
            target_thread_id,
            agent_path,
            activity_kind,
        )
        invocation = event.get("collaboration_invocation")
        if not isinstance(invocation, Mapping):
            invocation = self._pending_live_invocations.pop(activity_key, None)
        if isinstance(invocation, Mapping):
            return self._accept_collaboration_invocation(
                item_id=item_id,
                target_thread_id=target_thread_id,
                agent_path=agent_path,
                activity_kind=activity_kind,
                invocation=invocation,
                root_request_context_follows=(
                    event.get("root_request_context_follows") is True
                ),
            )
        if activity_kind in {"started", "interacted"}:
            return [
                f"• {_agent_display_name(agent_path)} · agent interaction observed",
                "  Invocation awaiting authenticated rollout correlation",
            ]
        return [f"• {_agent_display_name(agent_path)} · {activity_kind}"]

    def accept_collaboration_invocation_event(
        self,
        event: Mapping[str, object],
    ) -> list[str]:
        """Accept one exact live tool item and correlate it by collaboration call ID."""
        if (
            event.get("schema") != OBSERVER_SCHEMA
            or event.get("kind") != "app_server_collaboration_invocation"
            or event.get("thread_id") != self._root_thread_id
        ):
            return []
        item = event.get("item")
        if not isinstance(item, dict):
            return []
        item_id = item.get("id")
        if (
            not isinstance(item_id, str)
            or not item_id
            or item.get("sender_thread_id") != self._root_thread_id
        ):
            return []
        receiver_ids = item.get("receiver_thread_ids")
        if not isinstance(receiver_ids, list):
            return []
        activity_keys = [
            (target_thread_id, item_id)
            for target_thread_id in receiver_ids
            if isinstance(target_thread_id, str)
        ]
        matching_activities = [
            activity
            for activity_key in activity_keys
            if (activity := self._activity_items.get(activity_key)) is not None
        ]
        if not matching_activities:
            for activity_key in activity_keys:
                self._pending_live_invocations[activity_key] = item
            return []
        lines: list[str] = []
        for target_thread_id, agent_path, activity_kind in matching_activities:
            lines.extend(
                self._accept_collaboration_invocation(
                    item_id=item_id,
                    target_thread_id=target_thread_id,
                    agent_path=agent_path,
                    activity_kind=activity_kind,
                    invocation=item,
                    root_request_context_follows=(
                        event.get("root_request_context_follows") is True
                    ),
                )
            )
        return lines

    def _accept_collaboration_invocation(
        self,
        *,
        item_id: str,
        target_thread_id: str,
        agent_path: str,
        activity_kind: str,
        invocation: Mapping[str, object],
        root_request_context_follows: bool,
    ) -> list[str]:
        tool_name = invocation.get("tool_name")
        prompt = invocation.get("prompt")
        capture_state = invocation.get("arguments_capture_state")
        sender_thread_id = invocation.get("sender_thread_id")
        if (
            not isinstance(tool_name, str)
            or tool_name not in {*_TURN_REQUEST_KINDS, "collaboration.send_message"}
            or (prompt is not None and not isinstance(prompt, str))
            or (sender_thread_id is not None and sender_thread_id != self._root_thread_id)
        ):
            return [
                f"• {_agent_display_name(agent_path)} · agent interaction observed",
                "  Invocation tool is unavailable or unsupported",
            ]
        invocation_identity = (
            target_thread_id,
            item_id,
            "app_server_collaboration_invocation",
        )
        prior_tool_name = self._invocation_tool_names.setdefault(
            invocation_identity,
            tool_name,
        )
        if prior_tool_name != tool_name:
            return [
                "",
                f"INVOCATION CONFLICT · {_agent_display_name(agent_path)}",
                f"  Live/durable tool disagreement: {prior_tool_name} != {tool_name}",
            ]
        request_kind = _TURN_REQUEST_KINDS.get(tool_name)
        if request_kind is not None:
            self._queue_turn_request(
                target_thread_id,
                item_id,
                request_kind,
            )
        lines: list[str] = []
        if invocation_identity not in self._rendered_invocation_ids:
            self._rendered_invocation_ids.add(invocation_identity)
            lines.extend(
                _invocation_lines(
                    agent_path,
                    tool_name,
                    activity_kind=activity_kind,
                )
            )
            if not root_request_context_follows and request_kind is not None:
                lines.extend(
                    ["", "ROOT TURN REQUEST UNAVAILABLE", f"  {_SCOPE_UNAVAILABLE_DETAIL}"]
                )
        if (
            isinstance(prompt, str)
            and prompt
            and invocation_identity not in self._rendered_prompt_ids
        ):
            self._rendered_prompt_ids.add(invocation_identity)
            lines.extend(_exact_collaboration_prompt_lines(tool_name, prompt))
        elif (
            capture_state == "encrypted"
            and invocation_identity not in self._reported_unavailable_prompt_ids
            and invocation_identity not in self._rendered_prompt_ids
        ):
            self._reported_unavailable_prompt_ids.add(invocation_identity)
            lines.extend(_unavailable_collaboration_prompt_lines(tool_name))
        return lines

    def _queue_turn_request(
        self,
        target_thread_id: str,
        activity_item_id: str,
        request_kind: str,
    ) -> None:
        self._terminal_cleanup_targets.discard(target_thread_id)
        request_id = (target_thread_id, activity_item_id)
        if request_id in self._active_request_ids or request_id in self._activity_turn_keys:
            return
        pending = self._pending_requests.setdefault(target_thread_id, deque())
        pending.append(
            _PendingAgentRequest(
                activity_item_id=activity_item_id,
                request_kind=request_kind,
                text_blocks=self._root_request_text_by_activity.get(
                    (target_thread_id, activity_item_id),
                    (),
                ),
            )
        )
        self._active_request_ids.add(request_id)
        self._terminal_drain_complete = False

    def accept_root_request_context_event(
        self,
        event: Mapping[str, object],
    ) -> list[str]:
        """Render exact root-turn provenance without calling it the agent payload."""
        if (
            event.get("schema") != OBSERVER_SCHEMA
            or event.get("kind") != "root_request_context"
            or event.get("thread_id") != self._root_thread_id
        ):
            return []
        target_thread_id = event.get("target_thread_id")
        activity_item_id = event.get("activity_item_id")
        pending_requests = self._pending_requests.get(str(target_thread_id), ())
        pending_request = next(
            (
                request
                for request in pending_requests
                if request.activity_item_id == activity_item_id
            ),
            None,
        )
        matching_turn_key = self._activity_turn_keys.get(
            (str(target_thread_id), str(activity_item_id))
        )
        matching_turn = self._turn_presentations.get(matching_turn_key)
        if (
            target_thread_id not in self._target_states
            or not isinstance(activity_item_id, str)
            or (str(target_thread_id), activity_item_id) not in self._activity_items
        ):
            return []
        item = event.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "userMessage":
            return []
        item_id = item.get("id")
        text_blocks = item.get("text_blocks")
        request_key = (str(target_thread_id), activity_item_id, str(item_id))
        if (
            not isinstance(item_id, str)
            or not item_id
            or request_key in self._seen_parent_request_keys
            or not isinstance(text_blocks, list)
            or not all(isinstance(block, str) for block in text_blocks)
        ):
            return []
        rendered = _render_exact_text_blocks(text_blocks)
        if not rendered:
            return []
        self._seen_parent_request_keys.add(request_key)
        self._root_request_text_by_activity[(str(target_thread_id), activity_item_id)] = (
            tuple(text_blocks)
        )
        if pending_request is not None:
            pending_request.text_blocks = tuple(text_blocks)
        elif matching_turn is not None:
            matching_turn.request_text_blocks = tuple(text_blocks)
        return [
            "",
            "ROOT TURN REQUEST · exact user message",
            "  Parent-turn provenance; not the collaboration payload.",
            *rendered,
        ]

    def accept_agent_message_event(self, event: Mapping[str, object]) -> list[str]:
        """Render exact assistant-authored text for an agent already being followed."""
        if event.get("schema") != OBSERVER_SCHEMA:
            return []
        if event.get("kind") != "app_server_agent_message":
            return []
        thread_id = event.get("thread_id")
        turn_id = event.get("turn_id")
        item = event.get("item")
        if (
            thread_id not in self._target_states
            or not isinstance(turn_id, str)
            or not turn_id
            or not isinstance(item, Mapping)
        ):
            return []
        item_id = item.get("id")
        text = item.get("text")
        phase = item.get("phase")
        if (
            not isinstance(item_id, str)
            or not isinstance(text, str)
            or (phase is not None and not isinstance(phase, str))
        ):
            return []
        message_identity = (str(thread_id), item_id, "app_server_agent_message")
        if message_identity in self._seen_agent_message_ids:
            return []
        self._seen_agent_message_ids.add(message_identity)
        plain_text = _plain_terminal_text(text).strip()
        if not plain_text:
            return []
        turn_key = (str(thread_id), turn_id)
        if (
            turn_key not in self._turn_presentations
            or self._turn_presentations[turn_key].activity_item_id is None
        ):
            self._bind_turn(*turn_key)
        path = self._target_paths.get(str(thread_id), str(thread_id))
        agent_name = _agent_display_name(path)
        label = (
            f"AGENT ANSWER · {agent_name} · final answer returned"
            if phase == "final_answer"
            else f"AGENT UPDATE · {agent_name} · commentary returned"
        )
        return ["", label, *[f"  {line}" for line in plain_text.splitlines()]]

    def accept_trace_snapshot(
        self,
        snapshot: RodexAgentTraceSnapshot,
        *,
        recover_unknown_targets: bool = False,
    ) -> list[str]:
        """Advance through one durable page and render only exact tracked identities."""
        lines: list[str] = []
        for event in snapshot.events:
            event_id = event.get("event_id")
            if isinstance(event_id, str):
                self._after_event_id = uuid.UUID(event_id)
            thread_id = event.get("codex_thread_id")
            event_kind = event.get("event_kind")
            detail = event.get("detail")
            relevant = thread_id in self._target_states
            if (
                thread_id == self._root_thread_id
                and event_kind == "subagent_activity"
                and isinstance(detail, Mapping)
            ):
                target = optional_uuid_text(detail.get("target_codex_thread_id"))
                if (
                    recover_unknown_targets
                    and target is not None
                    and target not in self._target_states
                    and len(self._target_states) < OBSERVER_RECOVERED_TARGET_LIMIT
                ):
                    recovered_activity_kind = detail.get("activity_kind")
                    self._target_states[target] = (
                        recovered_activity_kind
                        if isinstance(recovered_activity_kind, str)
                        else None
                    )
                    recovered_path = detail.get("agent_path")
                    self._target_paths[target] = (
                        recovered_path if isinstance(recovered_path, str) else target
                    )
                relevant = target in self._target_states
                if relevant and isinstance(target, str):
                    path = detail.get("agent_path")
                    if isinstance(path, str):
                        self._target_paths[target] = path
                    else:
                        path = self._target_paths.get(target, target)
                    activity_kind = detail.get("activity_kind")
                    invocation = detail.get("collaboration_invocation")
                    turn_request = detail.get("turn_request")
                    if isinstance(invocation, Mapping):
                        source_call_id = invocation.get("source_call_id")
                        if isinstance(source_call_id, str) and isinstance(
                            activity_kind,
                            str,
                        ):
                            self._activity_items.setdefault(
                                (target, source_call_id),
                                (target, path, activity_kind),
                            )
                            lines.extend(
                                self._accept_collaboration_invocation(
                                    item_id=source_call_id,
                                    target_thread_id=target,
                                    agent_path=path,
                                    activity_kind=activity_kind,
                                    invocation=invocation,
                                    root_request_context_follows=(
                                        (target, source_call_id)
                                        in self._root_request_text_by_activity
                                    ),
                                )
                            )
                    request_kind = (
                        turn_request.get("request_kind")
                        if isinstance(turn_request, Mapping)
                        else None
                    )
                    target_turn_id = (
                        turn_request.get("target_codex_turn_id")
                        if isinstance(turn_request, Mapping)
                        else None
                    )
                    if isinstance(target_turn_id, str):
                        turn = self._bind_turn(target, target_turn_id)
                        if isinstance(request_kind, str):
                            turn.request_kind = request_kind
                continue
            if not relevant:
                continue
            if not isinstance(thread_id, str) or not isinstance(event_kind, str):
                continue
            lines.extend(
                self._accept_target_trace_event(
                    thread_id,
                    event_kind,
                    event.get("event_time_utc"),
                    event.get("codex_turn_id"),
                    detail if isinstance(detail, Mapping) else {},
                )
            )
        return lines

    def accept_turn_evidence(
        self,
        evidence_items: tuple[RodexAgentObserverTurnEvidence, ...],
    ) -> list[str]:
        """Render bounded SQL facts for exact target turns after a publication wake."""
        lines: list[str] = []
        for evidence in evidence_items:
            thread_id = str(evidence.codex_thread_id)
            turn_key = (thread_id, evidence.codex_turn_id)
            if turn_key not in self._turns_needing_evidence:
                continue
            turn = self._turn_presentations.setdefault(turn_key, _AgentTurnPresentation())
            turn.evidence = evidence
            self._target_paths[thread_id] = evidence.agent_path
            if not turn.lineage_rendered:
                turn.lineage_rendered = True
                lines.extend(self._lineage_lines(turn_key, evidence))
            self._remember_evidence_tokens(turn, evidence)
            work_fields = _work_fields_from_evidence(evidence)
            if work_fields:
                lines.extend(self._render_work(turn_key, work_fields))
        return lines

    def flush_pending_terminal_events(self) -> list[str]:
        """Render terminal summaries after the matching statistics read."""
        lines: list[str] = []
        pending = tuple(self._pending_terminal_events.items())
        self._pending_terminal_events.clear()
        for turn_key, (event_kind, event_time_utc) in pending:
            lines.extend(self._terminal_lines(turn_key, event_kind, event_time_utc))
            self._turns_needing_evidence.discard(turn_key)
            self._prune_terminal_turn(turn_key)
        return lines

    def _prune_terminal_turn(self, turn_key: AgentTurnKey) -> None:
        """Release correlation and presentation state after its final render."""
        thread_id, _turn_id = turn_key
        turn = self._turn_presentations.pop(turn_key, None)
        if turn is not None and turn.activity_item_id is not None:
            activity_key = (thread_id, turn.activity_item_id)
            self._activity_turn_keys.pop(activity_key, None)
            self._active_request_ids.discard(activity_key)
            self._activity_items.pop(activity_key, None)
            self._pending_live_invocations.pop(activity_key, None)
            self._root_request_text_by_activity.pop(activity_key, None)
        if any(key[0] == thread_id for key in self._turn_presentations):
            return
        if self._pending_requests.get(thread_id):
            return
        self._terminal_cleanup_targets.add(thread_id)

    def _prune_terminal_target(self, thread_id: str) -> None:
        """Release a target only after the durable reader reports caught up."""
        self._pending_requests.pop(thread_id, None)
        self._target_states.pop(thread_id, None)
        self._target_paths.pop(thread_id, None)
        self._active_request_ids = {
            key for key in self._active_request_ids if key[0] != thread_id
        }
        for collection in (
            self._seen_activity_item_ids,
            self._rendered_invocation_ids,
            self._rendered_prompt_ids,
            self._reported_unavailable_prompt_ids,
            self._seen_agent_message_ids,
        ):
            for identity in tuple(collection):
                if identity[0] == thread_id:
                    collection.discard(identity)
        for identity in tuple(self._invocation_tool_names):
            if identity[0] == thread_id:
                self._invocation_tool_names.pop(identity, None)
        for collection in (
            self._activity_items,
            self._pending_live_invocations,
            self._root_request_text_by_activity,
            self._activity_turn_keys,
        ):
            for identity in tuple(collection):
                if identity[0] == thread_id:
                    collection.pop(identity, None)
        self._seen_parent_request_keys = {
            key for key in self._seen_parent_request_keys if key[0] != thread_id
        }

    def _accept_target_trace_event(
        self,
        thread_id: str,
        event_kind: str,
        event_time_utc: object,
        codex_turn_id: object,
        detail: Mapping[str, object],
    ) -> list[str]:
        if not isinstance(codex_turn_id, str) or not codex_turn_id:
            return []
        turn_key = (thread_id, codex_turn_id)
        turn_was_known = turn_key in self._turn_presentations
        turn = self._turn_presentations.setdefault(turn_key, _AgentTurnPresentation())
        if event_kind == "turn_started":
            if isinstance(event_time_utc, str):
                turn.started_at_utc = event_time_utc
            turn = self._bind_turn(thread_id, codex_turn_id)
            self._terminal_drain_complete = False
            return []
        if event_kind == "turn_context":
            context = (detail.get("model"), detail.get("reasoning_effort"))
            if context == turn.last_context:
                return []
            turn.last_context = context
            model, effort = context
            if not isinstance(model, str) and not isinstance(effort, str):
                return []
            fields = []
            if isinstance(model, str):
                fields.append(model)
            if isinstance(effort, str):
                fields.append(effort.upper())
            path = self._target_paths.get(thread_id, thread_id)
            return [
                "",
                f"MODEL · {_agent_display_name(path)}",
                "  " + " · ".join(fields),
            ]
        if event_kind == "tool_call" and detail.get("activity_kind") == "output":
            tool_call_id = detail.get("tool_call_id")
            if not isinstance(tool_call_id, str):
                return []
            completed = turn.completed_tool_call_ids
            if tool_call_id in completed:
                return []
            completed.add(tool_call_id)
            return []
        if event_kind == "token_usage":
            usage = {
                name: value
                for name in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                )
                if isinstance((value := detail.get(name)), int)
                and not isinstance(value, bool)
            }
            if usage:
                turn.token_usage = usage
            return []
        if event_kind == "rate_limit":
            weekly_used = _weekly_limit_used_percent(detail.get("windows"))
            if weekly_used is not None:
                turn.weekly_limit_used_percent = weekly_used
            return []
        if event_kind == "compaction":
            path = self._target_paths.get(thread_id, thread_id)
            return [
                "",
                f"CONTEXT · {_agent_display_name(path)}",
                "  Context compacted",
            ]
        if event_kind not in {"turn_completed", "turn_aborted"}:
            return []
        if thread_id in self._pending_requests and (
            not turn_was_known or turn.activity_item_id is None
        ):
            turn = self._bind_turn(thread_id, codex_turn_id)
            turn_key = (thread_id, codex_turn_id)
        self._target_states[thread_id] = (
            "turnCompleted" if event_kind == "turn_completed" else "turnAborted"
        )
        if turn.activity_item_id is not None:
            self._active_request_ids.discard((thread_id, turn.activity_item_id))
        self._turns_needing_evidence.add(turn_key)
        self._pending_terminal_events[turn_key] = (event_kind, event_time_utc)
        return []

    def _lineage_lines(
        self,
        turn_key: AgentTurnKey,
        evidence: RodexAgentObserverTurnEvidence,
    ) -> list[str]:
        thread_id, _turn_id = turn_key
        turn = self._turn_presentations[turn_key]
        path = self._target_paths.get(thread_id, thread_id)
        agent_name = _agent_display_name(path)
        if turn.request_kind == "follow_up":
            description = "SAME AGENT · NEW TURN · existing agent context continues"
        elif evidence.history_inheritance_kind == "inherited":
            ordinal = evidence.inherited_history_start_ordinal
            suffix = "" if ordinal is None else f" at source ordinal {ordinal}"
            description = (
                "NEW INHERITED AGENT · separate thread/turn · "
                f"inherited-history cutoff{suffix}"
            )
        else:
            description = (
                "NEW CLEAN AGENT · separate thread/turn · no parent history inherited"
            )
        return ["", f"CONTEXT · {agent_name}", f"  {description}"]

    def _render_work(self, turn_key: AgentTurnKey, fields: tuple[str, ...]) -> list[str]:
        turn = self._turn_presentations[turn_key]
        if fields == turn.last_work_signature:
            return []
        turn.last_work_signature = fields
        progress = "  " + " · ".join(fields)
        thread_id, _turn_id = turn_key
        path = self._target_paths.get(thread_id, thread_id)
        return ["", f"WORK · {_agent_display_name(path)}", progress]

    def _remember_evidence_tokens(
        self,
        turn: _AgentTurnPresentation,
        evidence: RodexAgentObserverTurnEvidence,
    ) -> None:
        usage = {
            name: value
            for name in (
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
            )
            if isinstance((value := getattr(evidence, name)), int)
        }
        if usage:
            turn.token_usage = usage

    def _terminal_lines(
        self,
        turn_key: AgentTurnKey,
        event_kind: str,
        event_time_utc: object,
    ) -> list[str]:
        thread_id, _turn_id = turn_key
        turn = self._turn_presentations[turn_key]
        path = self._target_paths.get(thread_id, thread_id)
        agent_name = _agent_display_name(path)
        verb = "finished" if event_kind == "turn_completed" else "stopped"
        summary = f"{'✓' if event_kind == 'turn_completed' else '■'} "
        summary += f"{agent_name} {verb}"
        details = []
        elapsed = _format_elapsed(turn.started_at_utc, event_time_utc)
        if elapsed is not None:
            details.append(elapsed)
        if details:
            summary += " · " + " · ".join(details)
        lines = ["", summary]
        invocation = self._invocation_summary(turn_key)
        if invocation is not None:
            lines.append(f"  Invocation: {invocation}")
        evidence = turn.evidence
        work_fields = (
            _work_fields_from_evidence(evidence)
            if evidence is not None
            else _fallback_work_fields(turn.completed_tool_call_ids)
        )
        if work_fields:
            lines.append("  Work: " + " · ".join(work_fields))
        usage = turn.token_usage
        if usage:
            lines.append("  " + _token_summary(usage))
        weekly_used = turn.weekly_limit_used_percent
        if weekly_used is not None:
            lines.append(f"  Weekly limit: {_display_percentage(weekly_used)} used")
        request = turn.request_text_blocks
        if request:
            lines.extend(
                [
                    "",
                    "ROOT TURN REQUEST RECAP · exact user message",
                    "  Parent-turn provenance; not the collaboration payload.",
                    *_render_exact_text_blocks(list(request)),
                ]
            )
        return lines

    def _invocation_summary(self, turn_key: AgentTurnKey) -> str | None:
        turn = self._turn_presentations[turn_key]
        request_kind = turn.request_kind
        evidence = turn.evidence
        if request_kind == "follow_up":
            return "followup_task · SAME AGENT · NEW TURN · existing context"
        if request_kind != "initial":
            return None
        if evidence is None:
            return "spawn_agent · new agent thread"
        if evidence.history_inheritance_kind == "inherited":
            return "spawn_agent · NEW INHERITED AGENT"
        return "spawn_agent · NEW CLEAN AGENT"

    def _bind_turn(self, thread_id: str, turn_id: str) -> _AgentTurnPresentation:
        turn_key = (thread_id, turn_id)
        turn = self._turn_presentations.setdefault(turn_key, _AgentTurnPresentation())
        pending_requests = self._pending_requests.get(thread_id)
        if turn.activity_item_id is None and pending_requests:
            pending = pending_requests.popleft()
            turn.activity_item_id = pending.activity_item_id
            turn.request_kind = pending.request_kind
            turn.request_text_blocks = pending.text_blocks
            self._activity_turn_keys[(thread_id, pending.activity_item_id)] = turn_key
            if not pending_requests:
                self._pending_requests.pop(thread_id, None)
        self._turns_needing_evidence.add(turn_key)
        return turn

    def accept_trace_publication_wake(
        self,
        trace_publication_sequence: int,
        *,
        caught_up: bool,
    ) -> None:
        """Accept exact worker state after the corresponding SQL page has been read."""
        if trace_publication_sequence < self._last_trace_publication_sequence:
            return
        self._last_trace_publication_sequence = trace_publication_sequence
        if caught_up and not self._active_request_ids and not self._pending_terminal_events:
            for thread_id in tuple(self._terminal_cleanup_targets):
                self._prune_terminal_target(thread_id)
            self._terminal_cleanup_targets.clear()
            self._terminal_drain_complete = True

    def header_lines(self) -> tuple[str, ...]:
        return (
            "RODEX · LIVE AGENTS",
            "Developer view of exact invocations, request context, replies, and outcomes.",
        )


def _agent_display_name(agent_path: str) -> str:
    return agent_path.rstrip("/").rsplit("/", 1)[-1] or agent_path


def _invocation_lines(
    agent_path: str,
    tool_name: str,
    *,
    activity_kind: str,
) -> list[str]:
    agent_name = _agent_display_name(agent_path)
    if tool_name == "collaboration.spawn_agent":
        return [
            f"▶ {agent_name} · agent started",
            "",
            "INVOKED · spawn_agent",
            "  New agent thread · context inheritance awaiting verification",
        ]
    if tool_name == "collaboration.followup_task":
        return [
            f"↻ {agent_name} · new turn requested",
            "",
            "INVOKED · followup_task",
            "  Existing agent thread · new agent turn requested · context continues",
        ]
    if tool_name == "collaboration.send_message":
        return [
            f"→ {agent_name} · message sent",
            "",
            "INVOKED · send_message",
            "  Existing agent thread · current agent turn continues",
            "  No new turn requested",
        ]
    return [f"• {agent_name} · {activity_kind}"]


def _exact_collaboration_prompt_lines(tool_name: str, prompt: str) -> list[str]:
    label = {
        "collaboration.spawn_agent": "DELEGATED TASK",
        "collaboration.followup_task": "FOLLOW-UP TASK",
        "collaboration.send_message": "MESSAGE",
    }[tool_name]
    return [
        "",
        f"{label} · exact collaboration prompt",
        *_render_exact_text_blocks([prompt]),
    ]


def _unavailable_collaboration_prompt_lines(tool_name: str) -> list[str]:
    label = {
        "collaboration.spawn_agent": "DELEGATED TASK",
        "collaboration.followup_task": "FOLLOW-UP TASK",
        "collaboration.send_message": "MESSAGE",
    }[tool_name]
    return [
        "",
        f"{label} UNAVAILABLE",
        "  Encrypted in the authenticated rollout; plaintext not exposed",
    ]


def _projected_invocation_tool_name(
    invocation: Mapping[str, object] | None,
) -> str | None:
    if invocation is None:
        return None
    item = invocation.get("item")
    if not isinstance(item, Mapping):
        return None
    tool_name = item.get("tool_name")
    return tool_name if isinstance(tool_name, str) else None


def _plain_terminal_text(value: str) -> str:
    without_escapes = _ANSI_ESCAPE_PATTERN.sub("", value)
    return "".join(
        character
        for character in without_escapes
        if character in {"\n", "\t"} or 32 <= ord(character) != 127
    )


def _render_exact_text_blocks(text_blocks: list[str]) -> list[str]:
    rendered: list[str] = []
    for text in text_blocks:
        plain_text = _plain_terminal_text(text)
        lines = plain_text.splitlines()
        if not lines and plain_text:
            lines = [plain_text]
        rendered.extend(f"  {line}" for line in lines)
    return rendered


def _work_fields_from_evidence(
    evidence: RodexAgentObserverTurnEvidence,
) -> tuple[str, ...]:
    fields: list[str] = []
    values = (
        (evidence.actions_completed_count, "action", "actions"),
        (evidence.commands_executed_count, "command", "commands"),
        (evidence.file_change_operations_count, "file change", "file changes"),
        (evidence.web_operations_count, "web operation", "web operations"),
        (evidence.web_queries_count, "query", "queries"),
        (evidence.web_result_records_count, "result record", "result records"),
        (evidence.compactions_count, "compaction", "compactions"),
    )
    for count, singular, plural in values:
        if count is None or count <= 0:
            continue
        fields.append(f"{count} {singular if count == 1 else plural}")
    return tuple(fields)


def _fallback_work_fields(completed: set[str]) -> tuple[str, ...]:
    count = len(completed)
    return (f"{count} {'action' if count == 1 else 'actions'}",)


def _token_summary(usage: Mapping[str, int]) -> str:
    fields: list[str] = []
    total = usage.get("total_tokens")
    cached = usage.get("cached_input_tokens")
    output = usage.get("output_tokens")
    reasoning = usage.get("reasoning_output_tokens")
    if total is not None:
        fields.append(f"{total:,} processed")
    if cached is not None:
        fields.append(f"{cached:,} cached input")
    if output is not None:
        fields.append(f"{output:,} output")
    if reasoning is not None:
        fields.append(f"{reasoning:,} reasoning")
    return "Tokens: " + " · ".join(fields)


def _weekly_limit_used_percent(value: object) -> float | None:
    if not isinstance(value, list):
        return None
    for window in value:
        if not isinstance(window, Mapping) or window.get("window_minutes") != 10_080:
            continue
        used_percent = window.get("used_percent")
        if isinstance(used_percent, (int, float)) and not isinstance(used_percent, bool):
            return float(used_percent)
    return None


def _display_percentage(value: float) -> str:
    return f"{value:g}%"


def _format_elapsed(start: str | None, end: object) -> str | None:
    if start is None or not isinstance(end, str):
        return None
    with suppress(ValueError):
        seconds = round(
            (
                datetime.fromisoformat(end.replace("Z", "+00:00"))
                - datetime.fromisoformat(start.replace("Z", "+00:00"))
            ).total_seconds()
        )
        if seconds < 0:
            return None
        minutes, remaining_seconds = divmod(seconds, 60)
        if minutes:
            return f"{minutes}m {remaining_seconds}s"
        return f"{remaining_seconds}s"
    return None


def _observer_control_receiver(
    control_socket: socket.socket,
    events: queue.Queue[dict[str, object]],
    stop: Event,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    while not stop.is_set():
        try:
            connection, _address = control_socket.accept()
        except OSError:
            return
        with connection:
            deadline = monotonic() + OBSERVER_RECEIVE_DEADLINE_SECONDS
            try:
                header = _receive_exactly(
                    connection,
                    _OBSERVER_FRAME_LENGTH.size,
                    deadline=deadline,
                    monotonic=monotonic,
                )
                payload_size = _OBSERVER_FRAME_LENGTH.unpack(header)[0]
                if payload_size > OBSERVER_MAX_FRAME_BYTES:
                    continue
                payload = _receive_exactly(
                    connection,
                    payload_size,
                    deadline=deadline,
                    monotonic=monotonic,
                )
            except (EOFError, OSError, TimeoutError):
                continue
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(event, dict) and event.get("schema") == OBSERVER_SCHEMA:
            events.put(event)


def _receive_exactly(
    connection: socket.socket,
    size: int,
    *,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> bytes:
    if size < 0:
        raise ValueError("observer receive size cannot be negative")
    payload = bytearray()
    while len(payload) < size:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("observer control frame receive deadline expired")
        settimeout = getattr(connection, "settimeout", None)
        if callable(settimeout):
            settimeout(remaining)
        chunk = connection.recv(min(size - len(payload), 64 * 1024))
        if not chunk:
            raise EOFError("observer control frame ended early")
        payload.extend(chunk)
    return bytes(payload)


def _observer_runtime_liveness(
    protocol_event_socket_path: Path,
    events: queue.Queue[dict[str, object]],
) -> None:
    """Keep the pane tied to its runtime; live content arrives on the control socket."""
    try:
        with unix_connect(
            str(protocol_event_socket_path),
            uri=f"ws://localhost{AGENT_OBSERVER_EVENT_STREAM_PATH}",
            compression=None,
            max_size=None,
        ) as connection:
            while True:
                connection.recv()
    except (ConnectionClosed, OSError):
        pass
    finally:
        events.put({"schema": OBSERVER_SCHEMA, "kind": "runtime_closed"})


def _read_and_render_available_trace(
    view: AgentObserverView,
    rodex_sessions_id: int,
    database_path: Path,
    *,
    flush_terminal_events: bool,
    recover_unknown_targets: bool = False,
) -> None:
    if not view.monitoring or not view.target_thread_ids:
        return
    while True:
        snapshot = read_rodex_agent_trace(
            rodex_sessions_id,
            database_path,
            after_event_id=view.after_event_id,
            limit=OBSERVER_TRACE_PAGE_SIZE,
        )
        _print_lines(
            view.accept_trace_snapshot(
                snapshot,
                recover_unknown_targets=recover_unknown_targets,
            )
        )
        if len(snapshot.events) < OBSERVER_TRACE_PAGE_SIZE:
            break
    evidence = read_rodex_agent_observer_turn_evidence(
        rodex_sessions_id,
        view.target_turn_keys,
        database_path,
    )
    _print_lines(view.accept_turn_evidence(evidence))
    if flush_terminal_events:
        _print_lines(view.flush_pending_terminal_events())


def _print_lines(lines: tuple[str, ...] | list[str]) -> None:
    if not lines:
        return
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.agent_observer")
    parser.add_argument("--rodex-database", required=True, type=Path)
    parser.add_argument("--rodex-sessions-id", required=True, type=int)
    parser.add_argument("--rodex-session-id", required=True)
    parser.add_argument("--root-thread-id", required=True, type=uuid.UUID)
    parser.add_argument("--protocol-event-socket", required=True, type=Path)
    parser.add_argument("--initial-event", required=True)
    return parser


def main(arguments: list[str] | None = None) -> int:
    namespace = _parser().parse_args(arguments)
    if namespace.rodex_sessions_id < 1:
        _parser().error("--rodex-sessions-id must be positive")
    if _RODEX_SESSION_ID_PATTERN.fullmatch(namespace.rodex_session_id) is None:
        _parser().error("--rodex-session-id must be 16 lowercase hexadecimal characters")
    try:
        initial_event = json.loads(namespace.initial_event)
    except json.JSONDecodeError:
        _parser().error("--initial-event must be valid JSON")
    if not isinstance(initial_event, dict):
        _parser().error("--initial-event must be a JSON object")
    view = AgentObserverView(
        root_thread_id=namespace.root_thread_id,
        initial_event=initial_event,
    )
    control_path = observer_control_socket_path(namespace.protocol_event_socket)
    control_path.unlink(missing_ok=True)
    controls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    controls.bind(str(control_path))
    control_path.chmod(0o600)
    controls.listen()
    events: queue.Queue[dict[str, object]] = queue.Queue()
    stop = Event()
    control_thread = Thread(
        target=_observer_control_receiver,
        args=(controls, events, stop),
        name="rodex-agent-observer-control",
        daemon=True,
    )
    liveness_thread = Thread(
        target=_observer_runtime_liveness,
        args=(namespace.protocol_event_socket, events),
        name="rodex-agent-observer-liveness",
        daemon=True,
    )
    control_thread.start()
    liveness_thread.start()
    last_transport_overflow_count = 0
    try:
        _print_lines([*view.header_lines(), "", *view.initial_lines])
        _read_and_render_available_trace(
            view,
            namespace.rodex_sessions_id,
            namespace.rodex_database,
            flush_terminal_events=False,
        )
        while True:
            batch = [events.get()]
            while True:
                try:
                    batch.append(events.get_nowait())
                except queue.Empty:
                    break
            if any(event.get("kind") == "runtime_closed" for event in batch):
                return 0
            snapshots = [
                event for event in batch if event.get("kind") == "observer_state_snapshot"
            ]
            latest_snapshot = max(
                snapshots,
                key=lambda event: (
                    event.get("epoch")
                    if isinstance(event.get("epoch"), int)
                    and not isinstance(event.get("epoch"), bool)
                    else -1,
                    event.get("revision")
                    if isinstance(event.get("revision"), int)
                    and not isinstance(event.get("revision"), bool)
                    else -1,
                ),
                default=None,
            )
            semantic_events = [
                event for event in batch if event.get("kind") != "observer_state_snapshot"
            ]
            transport_overflowed = False
            if latest_snapshot is not None:
                semantic_events = [
                    *view.accept_observer_state_snapshot(latest_snapshot),
                    *semantic_events,
                ]
                overflow = latest_snapshot.get("overflow")
                dropped_event_count = (
                    overflow.get("dropped_event_count")
                    if isinstance(overflow, Mapping)
                    else None
                )
                if (
                    isinstance(dropped_event_count, int)
                    and not isinstance(dropped_event_count, bool)
                    and dropped_event_count > last_transport_overflow_count
                ):
                    last_transport_overflow_count = dropped_event_count
                    transport_overflowed = True
            trace_publications: list[tuple[int, bool]] = []
            for event in semantic_events:
                kind = event.get("kind")
                if kind == "app_server_subagent_activity":
                    _print_lines(view.accept_app_server_event(event))
                elif kind == "app_server_collaboration_invocation":
                    _print_lines(view.accept_collaboration_invocation_event(event))
                elif kind == "app_server_agent_message":
                    _print_lines(view.accept_agent_message_event(event))
                elif kind == "root_request_context":
                    _print_lines(view.accept_root_request_context_event(event))
                elif kind == "trace_published":
                    sequence = event.get("trace_publication_sequence")
                    caught_up = event.get("caught_up")
                    if (
                        isinstance(sequence, int)
                        and not isinstance(sequence, bool)
                        and sequence > 0
                        and isinstance(caught_up, bool)
                    ):
                        trace_publications.append((sequence, caught_up))
            if not trace_publications:
                if transport_overflowed:
                    _read_and_render_available_trace(
                        view,
                        namespace.rodex_sessions_id,
                        namespace.rodex_database,
                        flush_terminal_events=False,
                        recover_unknown_targets=True,
                    )
                continue
            trace_publication_sequence, caught_up = max(trace_publications)
            _read_and_render_available_trace(
                view,
                namespace.rodex_sessions_id,
                namespace.rodex_database,
                flush_terminal_events=caught_up,
            )
            view.accept_trace_publication_wake(
                trace_publication_sequence,
                caught_up=caught_up,
            )
    finally:
        stop.set()
        controls.close()
        control_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
