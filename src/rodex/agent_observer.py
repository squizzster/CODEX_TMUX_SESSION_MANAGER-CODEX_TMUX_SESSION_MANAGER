"""Dedicated tmux presentation for exact live and durable sub-agent facts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shlex
import socket
import struct
import subprocess
import sys
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

from .protocol_proxy import AGENT_OBSERVER_EVENT_STREAM_PATH

OBSERVER_SCHEMA: Final = "rodex-agent-observer-v2"
OBSERVER_CONTROL_SOCKET_PREFIX: Final = "agent-observer-"
OBSERVER_PRIMARY_PANE_OPTION: Final = "@rodex_agent_observer_pane_id"
OBSERVER_OWNER_PANE_OPTION: Final = "@rodex_agent_observer_for"
OBSERVER_TRACE_PAGE_SIZE: Final = 500
_PANE_ID_PATTERN: Final = re.compile(r"%[0-9]+")
_RODEX_SESSION_ID_PATTERN: Final = re.compile(r"[0-9a-f]{16}")
_SUBAGENT_ACTIVITY_METHODS: Final = frozenset({"item/started", "item/completed"})
_COLLABORATION_INVOCATION_METHODS: Final = frozenset({"item/started", "item/completed"})
_COLLABORATION_TOOL_NAMES: Final = {
    "spawnAgent": "collaboration.spawn_agent",
    "followupTask": "collaboration.followup_task",
    "sendMessage": "collaboration.send_message",
}
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
_OBSERVER_FRAME_LENGTH = struct.Struct("!Q")


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
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
        sender.settimeout(1)
        sender.connect(str(path))
        sender.sendall(frame)


def _try_send_observer_event_frame(path: Path, event: dict[str, object]) -> None:
    """Send one small wake without waiting on the analytics publication path."""
    frame = _observer_event_frame(event)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
        sender.setblocking(False)
        result = sender.connect_ex(str(path))
        if result != 0:
            raise OSError(result, os.strerror(result), str(path))
        if sender.send(frame) != len(frame):
            raise BlockingIOError("observer control frame was not accepted atomically")


class _ObserverEventDispatcher:
    """Deliver ordered live events across the observer's socket-startup boundary."""

    def __init__(self) -> None:
        self._events: queue.Queue[tuple[Path, dict[str, object]] | None] = queue.Queue()
        self._stop = Event()
        self._start_lock = Lock()
        self._thread: Thread | None = None

    def send(self, path: Path, event: dict[str, object]) -> None:
        if self._stop.is_set():
            return
        self._events.put((path, event))
        with self._start_lock:
            if self._thread is None:
                self._thread = Thread(
                    target=self._run,
                    name="rodex-agent-observer-dispatch",
                    daemon=True,
                )
                self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._events.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._events.get()
            if item is None:
                return
            path, event = item
            retry_delay = 0.01
            while not self._stop.is_set():
                try:
                    _send_observer_event_frame(path, event)
                except OSError:
                    if self._stop.wait(retry_delay):
                        return
                    retry_delay = min(retry_delay * 2, 0.5)
                else:
                    break


def project_subagent_activity_event(
    event: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Project one observed App Server sub-agent activity without content fields."""
    if event is None or event.get("method") not in _SUBAGENT_ACTIVITY_METHODS:
        return None
    method = event["method"]
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    item = params.get("item")
    if not isinstance(item, Mapping):
        return None
    if item.get("type") != "subAgentActivity":
        return None
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        return None
    thread_id = _optional_uuid_text(params.get("threadId"))
    if thread_id is None:
        return None
    turn_id = params.get("turnId")
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
        return None
    agent_thread_id = _optional_uuid_text(item.get("agentThreadId"))
    if agent_thread_id is None:
        return None
    agent_path = item.get("agentPath")
    if not isinstance(agent_path, str) or not agent_path:
        return None
    activity_kind = item.get("kind")
    if not isinstance(activity_kind, str) or not activity_kind:
        return None
    return {
        "schema": OBSERVER_SCHEMA,
        "kind": "app_server_subagent_activity",
        "method": method,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "item": {
            "type": "subAgentActivity",
            "id": item_id,
            "activity_kind": activity_kind,
            "agent_thread_id": agent_thread_id,
            "agent_path": agent_path,
        },
    }


def project_collaboration_invocation_event(
    event: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Project one exact current-Codex collaboration invocation for live display."""
    if event is None or event.get("method") not in _COLLABORATION_INVOCATION_METHODS:
        return None
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    item = params.get("item")
    if not isinstance(item, Mapping) or item.get("type") != "collabAgentToolCall":
        return None
    item_id = item.get("id")
    raw_tool_name = item.get("tool")
    status = item.get("status")
    prompt = item.get("prompt")
    if (
        not isinstance(item_id, str)
        or not item_id
        or not isinstance(raw_tool_name, str)
        or not raw_tool_name
        or not isinstance(status, str)
        or not status
        or (prompt is not None and not isinstance(prompt, str))
    ):
        return None
    thread_id = _optional_uuid_text(params.get("threadId"))
    sender_thread_id = _optional_uuid_text(item.get("senderThreadId"))
    raw_receiver_ids = item.get("receiverThreadIds")
    if (
        thread_id is None
        or sender_thread_id is None
        or not isinstance(raw_receiver_ids, list)
    ):
        return None
    receiver_thread_ids = [_optional_uuid_text(value) for value in raw_receiver_ids]
    if any(value is None for value in receiver_thread_ids):
        return None
    turn_id = params.get("turnId")
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
        return None
    return {
        "schema": OBSERVER_SCHEMA,
        "kind": "app_server_collaboration_invocation",
        "method": event["method"],
        "thread_id": thread_id,
        "turn_id": turn_id,
        "item": {
            "type": "collabAgentToolCall",
            "id": item_id,
            "raw_tool_name": raw_tool_name,
            "tool_name": _COLLABORATION_TOOL_NAMES.get(raw_tool_name),
            "status": status,
            "sender_thread_id": sender_thread_id,
            "receiver_thread_ids": receiver_thread_ids,
            "prompt": prompt,
        },
    }


def project_agent_message_event(
    event: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Project completed agent-authored text without prompts or reasoning content."""
    if event is None or event.get("method") != "item/completed":
        return None
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    item = params.get("item")
    if not isinstance(item, Mapping) or item.get("type") != "agentMessage":
        return None
    item_id = item.get("id")
    text = item.get("text")
    if not isinstance(item_id, str) or not item_id or not isinstance(text, str) or not text:
        return None
    thread_id = _optional_uuid_text(params.get("threadId"))
    if thread_id is None:
        return None
    turn_id = params.get("turnId")
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
        return None
    phase = item.get("phase")
    if phase is not None and not isinstance(phase, str):
        return None
    return {
        "schema": OBSERVER_SCHEMA,
        "kind": "app_server_agent_message",
        "thread_id": thread_id,
        "turn_id": turn_id,
        "item": {
            "type": "agentMessage",
            "id": item_id,
            "phase": phase,
            "text": text,
        },
    }


def project_user_message_event(
    event: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Project exact completed parent-user text for same-turn request correlation."""
    if event is None or event.get("method") != "item/completed":
        return None
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    item = params.get("item")
    if not isinstance(item, Mapping) or item.get("type") != "userMessage":
        return None
    item_id = item.get("id")
    content = item.get("content")
    if not isinstance(item_id, str) or not item_id or not isinstance(content, list):
        return None
    text_blocks = [
        block.get("text")
        for block in content
        if isinstance(block, Mapping)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    if not text_blocks or not any(text_blocks):
        return None
    thread_id = _optional_uuid_text(params.get("threadId"))
    turn_id = params.get("turnId")
    if thread_id is None or not isinstance(turn_id, str) or not turn_id:
        return None
    return {
        "schema": OBSERVER_SCHEMA,
        "kind": "app_server_user_message",
        "thread_id": thread_id,
        "turn_id": turn_id,
        "item": {
            "type": "userMessage",
            "id": item_id,
            "text_blocks": text_blocks,
        },
    }


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


class AgentObserverPaneController:
    """React to exact sub-agent activity at the live tmux boundary."""

    def __init__(
        self,
        tmux_binary: str,
        tmux_server_socket_path: Path,
        primary_pane_target: str,
        protocol_event_socket_path: Path,
        *,
        runner: Runner = subprocess.run,
        cursor_reader: CursorReader = read_rodex_agent_trace_cursor,
        event_sender: EventSender | None = None,
        python_executable: str = sys.executable,
    ) -> None:
        self._tmux_binary = tmux_binary
        self._tmux_server_socket_path = tmux_server_socket_path
        self._primary_pane_target = primary_pane_target
        self._protocol_event_socket_path = protocol_event_socket_path
        self._runner = runner
        self._cursor_reader = cursor_reader
        self._event_dispatcher = (
            _ObserverEventDispatcher() if event_sender is None else None
        )
        self._event_sender = (
            self._event_dispatcher.send
            if self._event_dispatcher is not None
            else event_sender
        )
        self._python_executable = python_executable
        self._database_path: Path | None = None
        self._rodex_sessions_id: int | None = None
        self._rodex_session_id: str | None = None
        self._root_thread_id: uuid.UUID | None = None
        self._observer_pane_target: str | None = None
        self._known_activity_item_ids: set[str] = set()
        self._tracked_target_thread_ids: set[str] = set()
        self._latest_parent_user_message: dict[str, object] | None = None
        self._collaboration_invocations: dict[str, dict[str, object]] = {}
        self._subagent_activities: dict[str, dict[str, object]] = {}
        self._sent_root_request_context_ids: set[str] = set()

    def activate(
        self,
        *,
        database_path: Path,
        rodex_sessions_id: int,
        rodex_session_id: str,
        root_thread_id: uuid.UUID,
    ) -> None:
        """Bind the controller to the exact identity committed at registration."""
        if self._database_path is not None:
            raise RuntimeError("agent observer pane controller is already activated")
        if rodex_sessions_id < 1:
            raise ValueError("rodex_sessions_id must be positive")
        if _RODEX_SESSION_ID_PATTERN.fullmatch(rodex_session_id) is None:
            raise ValueError("rodex_session_id must be 16 lowercase hexadecimal characters")
        if _PANE_ID_PATTERN.fullmatch(self._primary_pane_target) is None:
            raise ValueError("primary pane target must be an exact tmux pane ID")
        self._database_path = Path(os.path.abspath(database_path))
        self._rodex_sessions_id = rodex_sessions_id
        self._rodex_session_id = rodex_session_id
        self._root_thread_id = uuid.UUID(str(root_thread_id))

    def observe_protocol_event(self, event: Mapping[str, object] | None) -> None:
        """Create, reuse, or update the observer from one typed App Server item."""
        collaboration_invocation = project_collaboration_invocation_event(event)
        if collaboration_invocation is not None:
            self._observe_collaboration_invocation(collaboration_invocation)
            return
        agent_message = project_agent_message_event(event)
        if agent_message is not None:
            target_thread_id = agent_message["thread_id"]
            if (
                target_thread_id in self._tracked_target_thread_ids
                and self._locate_observer_pane() is not None
            ):
                self._send_observer_event(agent_message)
            return
        parent_user_message = project_user_message_event(event)
        if parent_user_message is not None:
            root_thread_id = self._root_thread_id
            if root_thread_id is not None and parent_user_message["thread_id"] == str(
                root_thread_id
            ):
                self._latest_parent_user_message = parent_user_message
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
        self._subagent_activities[item_id] = projected
        collaboration_invocation = self._collaboration_invocations.get(item_id)
        if collaboration_invocation is not None:
            projected["collaboration_invocation"] = collaboration_invocation["item"]
        is_new_spawn = (
            projected["method"] == "item/started" and item["activity_kind"] == "started"
        )
        is_known = item_id in self._known_activity_item_ids
        target_is_tracked = target_thread_id in self._tracked_target_thread_ids
        if not is_new_spawn and not is_known and not target_is_tracked:
            return
        if is_new_spawn:
            assert self._rodex_sessions_id is not None
            cursor = self._cursor_reader(self._rodex_sessions_id, self._database_path)
            projected["after_event_id"] = None if cursor is None else str(cursor)
            self._known_activity_item_ids.add(item_id)
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
        self._tracked_target_thread_ids.add(target_thread_id)
        pane_target = self._locate_observer_pane()
        if pane_target is None:
            if not is_new_spawn:
                return
            self._observer_pane_target = self._create_observer_pane(projected)
            if self._observer_pane_target is not None and request_event is not None:
                self._sent_root_request_context_ids.add(item_id)
                self._send_observer_event(request_event)
            return
        self._send_observer_event(projected)
        if request_event is not None:
            self._sent_root_request_context_ids.add(item_id)
            self._send_observer_event(request_event)

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
        self._collaboration_invocations[item_id] = invocation
        activity = self._subagent_activities.get(item_id)
        if activity is not None:
            activity["collaboration_invocation"] = item
        receiver_ids = item.get("receiver_thread_ids")
        targets_tracked = isinstance(receiver_ids, list) and any(
            target in self._tracked_target_thread_ids for target in receiver_ids
        )
        if activity is None and not targets_tracked:
            return
        pane_target = self._locate_observer_pane()
        if pane_target is None:
            return
        request_event = None
        if activity is not None and item_id not in self._sent_root_request_context_ids:
            request_event = self._root_request_context_event(activity)
            if request_event is not None:
                invocation["root_request_context_follows"] = True
        self._send_observer_event(invocation)
        if request_event is not None:
            self._sent_root_request_context_ids.add(item_id)
            self._send_observer_event(request_event)

    def close(self) -> None:
        """Release only in-process state; tmux owns the persistent presentation pane."""
        if self._event_dispatcher is not None:
            self._event_dispatcher.close()
        self._known_activity_item_ids.clear()
        self._tracked_target_thread_ids.clear()
        self._latest_parent_user_message = None
        self._collaboration_invocations.clear()
        self._subagent_activities.clear()
        self._sent_root_request_context_ids.clear()

    def _root_request_context_event(
        self,
        activity_event: Mapping[str, object],
    ) -> dict[str, object] | None:
        parent_user_message = self._latest_parent_user_message
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
        try:
            assert self._event_sender is not None
            self._event_sender(
                observer_control_socket_path(self._protocol_event_socket_path),
                event,
            )
        except OSError:
            return

    def _locate_observer_pane(self) -> str | None:
        candidate = self._observer_pane_target
        if candidate is None:
            shown = self._tmux(
                "show-options",
                "-p",
                "-v",
                "-t",
                self._primary_pane_target,
                OBSERVER_PRIMARY_PANE_OPTION,
            )
            if shown.returncode != 0:
                return None
            candidate = shown.stdout.strip()
        if _PANE_ID_PATTERN.fullmatch(candidate) is None:
            return None
        identity = self._tmux(
            "display-message",
            "-p",
            "-t",
            candidate,
            "-F",
            f"#{{pane_id}}|#{{{OBSERVER_OWNER_PANE_OPTION}}}|#{{pane_dead}}",
        )
        if identity.returncode != 0:
            self._observer_pane_target = None
            return None
        if identity.stdout.strip() != f"{candidate}|{self._primary_pane_target}|0":
            self._observer_pane_target = None
            return None
        self._observer_pane_target = candidate
        return candidate

    def _create_observer_pane(self, initial_event: dict[str, object]) -> str | None:
        assert self._database_path is not None
        assert self._rodex_sessions_id is not None
        assert self._rodex_session_id is not None
        assert self._root_thread_id is not None
        cwd = self._tmux(
            "display-message",
            "-p",
            "-t",
            self._primary_pane_target,
            "-F",
            "#{pane_current_path}",
        )
        if cwd.returncode != 0 or not cwd.stdout.rstrip("\n"):
            return None
        command = [
            self._python_executable,
            "-m",
            "rodex.agent_observer",
            "--rodex-database",
            str(self._database_path),
            "--rodex-sessions-id",
            str(self._rodex_sessions_id),
            "--rodex-session-id",
            self._rodex_session_id,
            "--root-thread-id",
            str(self._root_thread_id),
            "--protocol-event-socket",
            str(self._protocol_event_socket_path),
            "--initial-event",
            json.dumps(
                initial_event,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        ]
        split = self._tmux(
            "split-window",
            "-v",
            "-b",
            "-d",
            "-p",
            "33",
            "-t",
            self._primary_pane_target,
            "-c",
            cwd.stdout.rstrip("\n"),
            "-P",
            "-F",
            "#{pane_id}",
            f"exec {shlex.join(command)}",
        )
        pane_target = split.stdout.strip()
        if split.returncode != 0 or _PANE_ID_PATTERN.fullmatch(pane_target) is None:
            return None
        self._tmux(
            "set-option",
            "-p",
            "-t",
            self._primary_pane_target,
            OBSERVER_PRIMARY_PANE_OPTION,
            pane_target,
        )
        self._tmux(
            "set-option",
            "-p",
            "-t",
            pane_target,
            OBSERVER_OWNER_PANE_OPTION,
            self._primary_pane_target,
        )
        self._tmux("select-pane", "-d", "-t", pane_target)
        self._tmux("select-pane", "-t", self._primary_pane_target)
        return pane_target

    def _tmux(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._runner(
            [
                self._tmux_binary,
                "-S",
                str(self._tmux_server_socket_path),
                *arguments,
            ],
            check=False,
            text=True,
            capture_output=True,
        )


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
        self._seen_activity_item_ids: set[str] = set()
        self._activity_items: dict[str, AgentActivity] = {}
        self._pending_live_invocations: dict[str, dict[str, object]] = {}
        self._invocation_tool_names: dict[str, str] = {}
        self._rendered_invocation_ids: set[str] = set()
        self._rendered_prompt_ids: set[str] = set()
        self._reported_unavailable_prompt_ids: set[str] = set()
        self._seen_agent_message_ids: set[str] = set()
        self._seen_parent_request_keys: set[tuple[str, str, str]] = set()
        self._root_request_text_by_activity: dict[str, tuple[str, ...]] = {}
        self._pending_requests: dict[str, deque[_PendingAgentRequest]] = {}
        self._turn_presentations: dict[AgentTurnKey, _AgentTurnPresentation] = {}
        self._activity_turn_keys: dict[tuple[str, str], AgentTurnKey] = {}
        self._turns_needing_evidence: set[AgentTurnKey] = set()
        self._pending_terminal_events: dict[AgentTurnKey, tuple[str, object]] = {}
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
        if item_id in self._seen_activity_item_ids:
            return []
        self._seen_activity_item_ids.add(item_id)
        self._target_states[target_thread_id] = activity_kind
        self._target_paths[target_thread_id] = agent_path
        self._activity_items[item_id] = (target_thread_id, agent_path, activity_kind)
        invocation = event.get("collaboration_invocation")
        if not isinstance(invocation, Mapping):
            invocation = self._pending_live_invocations.pop(item_id, None)
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
        activity = self._activity_items.get(item_id)
        if activity is None:
            self._pending_live_invocations[item_id] = item
            return []
        target_thread_id, agent_path, activity_kind = activity
        return self._accept_collaboration_invocation(
            item_id=item_id,
            target_thread_id=target_thread_id,
            agent_path=agent_path,
            activity_kind=activity_kind,
            invocation=item,
            root_request_context_follows=(
                event.get("root_request_context_follows") is True
            ),
        )

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
        prior_tool_name = self._invocation_tool_names.setdefault(item_id, tool_name)
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
        if item_id not in self._rendered_invocation_ids:
            self._rendered_invocation_ids.add(item_id)
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
        if isinstance(prompt, str) and prompt and item_id not in self._rendered_prompt_ids:
            self._rendered_prompt_ids.add(item_id)
            lines.extend(_exact_collaboration_prompt_lines(tool_name, prompt))
        elif (
            capture_state == "encrypted"
            and item_id not in self._reported_unavailable_prompt_ids
            and item_id not in self._rendered_prompt_ids
        ):
            self._reported_unavailable_prompt_ids.add(item_id)
            lines.extend(_unavailable_collaboration_prompt_lines(tool_name))
        return lines

    def _queue_turn_request(
        self,
        target_thread_id: str,
        activity_item_id: str,
        request_kind: str,
    ) -> None:
        request_id = (target_thread_id, activity_item_id)
        if request_id in self._active_request_ids or request_id in self._activity_turn_keys:
            return
        pending = self._pending_requests.setdefault(target_thread_id, deque())
        pending.append(
            _PendingAgentRequest(
                activity_item_id=activity_item_id,
                request_kind=request_kind,
                text_blocks=self._root_request_text_by_activity.get(
                    activity_item_id,
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
            or activity_item_id not in self._activity_items
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
        self._root_request_text_by_activity[activity_item_id] = tuple(text_blocks)
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
        if item_id in self._seen_agent_message_ids:
            return []
        self._seen_agent_message_ids.add(item_id)
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

    def accept_trace_snapshot(self, snapshot: RodexAgentTraceSnapshot) -> list[str]:
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
                target = detail.get("target_codex_thread_id")
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
                                source_call_id,
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
                                        source_call_id
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
        return lines

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
) -> None:
    while not stop.is_set():
        try:
            connection, _address = control_socket.accept()
        except OSError:
            return
        with connection:
            connection.settimeout(1)
            try:
                header = _receive_exactly(connection, _OBSERVER_FRAME_LENGTH.size)
                payload_size = _OBSERVER_FRAME_LENGTH.unpack(header)[0]
                payload = _receive_exactly(connection, payload_size)
            except (EOFError, OSError):
                continue
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(event, dict) and event.get("schema") == OBSERVER_SCHEMA:
            events.put(event)


def _receive_exactly(connection: socket.socket, size: int) -> bytes:
    payload = bytearray()
    while len(payload) < size:
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
        _print_lines(view.accept_trace_snapshot(snapshot))
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


def _optional_uuid_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    with suppress(ValueError):
        parsed = uuid.UUID(value)
        if str(parsed) == value:
            return value
    return None


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
            trace_publications: list[tuple[int, bool]] = []
            for event in batch:
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
