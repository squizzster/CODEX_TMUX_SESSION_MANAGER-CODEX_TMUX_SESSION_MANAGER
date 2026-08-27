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
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Final

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect

from rodex_registry import (
    RodexAgentTraceSnapshot,
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
_ANSI_ESCAPE_PATTERN: Final = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_SCOPE_UNAVAILABLE_DETAIL: Final = (
    "Codex did not expose the delegated plaintext for this spawn."
)
Runner = Callable[..., subprocess.CompletedProcess[str]]
CursorReader = Callable[[int, Path], uuid.UUID | None]
EventSender = Callable[[Path, dict[str, object]], None]


def observer_control_socket_path(protocol_event_socket_path: Path) -> Path:
    """Return one private observer-control socket per exact runtime event socket."""
    runtime_digest = hashlib.sha256(
        os.fsencode(os.path.abspath(protocol_event_socket_path))
    ).hexdigest()[:16]
    return protocol_event_socket_path.with_name(
        f"{OBSERVER_CONTROL_SOCKET_PREFIX}{runtime_digest}.sock"
    )


def _send_observer_datagram(path: Path, event: dict[str, object]) -> None:
    payload = json.dumps(
        event,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
        sender.setblocking(False)
        sender.sendto(payload, str(path))


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
                    _send_observer_datagram(path, event)
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
        _send_observer_datagram(
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
        is_request_activity = is_new_spawn or item["activity_kind"] == "interacted"
        request_event = (
            self._parent_request_event(projected) if is_request_activity else None
        )
        if request_event is not None:
            projected["parent_request_follows"] = True
        self._tracked_target_thread_ids.add(target_thread_id)
        pane_target = self._locate_observer_pane()
        if pane_target is None:
            if not is_new_spawn:
                return
            self._observer_pane_target = self._create_observer_pane(projected)
            if self._observer_pane_target is not None and request_event is not None:
                self._send_observer_event(request_event)
            return
        self._send_observer_event(projected)
        if request_event is not None:
            self._send_observer_event(request_event)

    def close(self) -> None:
        """Release only in-process state; tmux owns the persistent presentation pane."""
        if self._event_dispatcher is not None:
            self._event_dispatcher.close()
        self._known_activity_item_ids.clear()
        self._tracked_target_thread_ids.clear()
        self._latest_parent_user_message = None

    def _parent_request_event(
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
            "kind": "parent_request",
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
        self._durable_terminal_target_ids: set[str] = set()
        self._terminal_drain_complete = False
        self._last_trace_publication_sequence = 0
        self._target_paths: dict[str, str] = {}
        self._seen_activity_item_ids: set[str] = set()
        self._seen_agent_message_ids: set[str] = set()
        self._seen_parent_request_item_ids: set[str] = set()
        self._turn_started_at: dict[str, str] = {}
        self._completed_tool_call_ids: dict[str, set[str]] = {}
        self._latest_total_tokens: dict[str, int] = {}
        self._weekly_limit_used_percent: dict[str, float] = {}
        self._last_context: dict[str, tuple[object, object]] = {}
        self._work_line_is_last = False
        self._initial_lines = self.accept_app_server_event(initial_event)

    @property
    def after_event_id(self) -> uuid.UUID | None:
        return self._after_event_id

    @property
    def target_thread_ids(self) -> frozenset[str]:
        return frozenset(self._target_states)

    @property
    def monitoring(self) -> bool:
        return bool(self._target_states) and not self._terminal_drain_complete

    @property
    def initial_lines(self) -> tuple[str, ...]:
        return tuple(self._initial_lines)

    def accept_app_server_event(self, event: Mapping[str, object]) -> list[str]:
        """Accept one already-sanitized sub-agent activity event."""
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
        if activity_kind in {"started", "interacted"}:
            self._durable_terminal_target_ids.discard(target_thread_id)
            self._terminal_drain_complete = False
            self._turn_started_at.pop(target_thread_id, None)
            self._completed_tool_call_ids[target_thread_id] = set()
            self._latest_total_tokens.pop(target_thread_id, None)
            self._weekly_limit_used_percent.pop(target_thread_id, None)
            self._last_context.pop(target_thread_id, None)
            self._work_line_is_last = False
        if activity_kind == "started":
            if event.get("parent_request_follows") is True:
                return [f"▶ {_agent_display_name(agent_path)} started"]
            return [
                f"▶ {_agent_display_name(agent_path)} started",
                "",
                "SCOPE UNAVAILABLE",
                f"  {_SCOPE_UNAVAILABLE_DETAIL}",
            ]
        if activity_kind == "interacted" and event.get("parent_request_follows") is True:
            return [f"↻ {_agent_display_name(agent_path)} received follow-up"]
        self._work_line_is_last = False
        return [f"• {_agent_display_name(agent_path)} · {activity_kind}"]

    def accept_parent_request_event(self, event: Mapping[str, object]) -> list[str]:
        """Render exact same-turn parent-user text for an already tracked request."""
        if (
            event.get("schema") != OBSERVER_SCHEMA
            or event.get("kind") != "parent_request"
            or event.get("thread_id") != self._root_thread_id
        ):
            return []
        target_thread_id = event.get("target_thread_id")
        if target_thread_id not in self._target_states:
            return []
        item = event.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "userMessage":
            return []
        item_id = item.get("id")
        text_blocks = item.get("text_blocks")
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id in self._seen_parent_request_item_ids
            or not isinstance(text_blocks, list)
            or not all(isinstance(block, str) for block in text_blocks)
        ):
            return []
        rendered = _render_exact_text_blocks(text_blocks)
        if not rendered:
            return []
        self._seen_parent_request_item_ids.add(item_id)
        self._work_line_is_last = False
        return ["", "REQUEST · exact parent message", *rendered]

    def accept_agent_message_event(self, event: Mapping[str, object]) -> list[str]:
        """Render exact assistant-authored text for an agent already being followed."""
        if event.get("schema") != OBSERVER_SCHEMA:
            return []
        if event.get("kind") != "app_server_agent_message":
            return []
        thread_id = event.get("thread_id")
        item = event.get("item")
        if thread_id not in self._target_states or not isinstance(item, Mapping):
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
        self._work_line_is_last = False
        label = "ANSWER" if phase == "final_answer" else "UPDATE"
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
            if not relevant:
                continue
            if not isinstance(thread_id, str) or not isinstance(event_kind, str):
                continue
            lines.extend(
                self._accept_target_trace_event(
                    thread_id,
                    event_kind,
                    event.get("event_time_utc"),
                    detail if isinstance(detail, Mapping) else {},
                )
            )
        return lines

    def _accept_target_trace_event(
        self,
        thread_id: str,
        event_kind: str,
        event_time_utc: object,
        detail: Mapping[str, object],
    ) -> list[str]:
        if event_kind == "turn_started":
            if isinstance(event_time_utc, str):
                self._turn_started_at[thread_id] = event_time_utc
            self._durable_terminal_target_ids.discard(thread_id)
            self._terminal_drain_complete = False
            return []
        if event_kind == "turn_context":
            context = (detail.get("model"), detail.get("reasoning_effort"))
            if context == self._last_context.get(thread_id):
                return []
            self._last_context[thread_id] = context
            model, effort = context
            if not isinstance(model, str) and not isinstance(effort, str):
                return []
            fields = []
            if isinstance(model, str):
                fields.append(model)
            if isinstance(effort, str):
                fields.append(effort.upper())
            self._work_line_is_last = False
            return ["", "MODEL", "  " + " · ".join(fields)]
        if event_kind == "tool_call" and detail.get("activity_kind") == "output":
            tool_call_id = detail.get("tool_call_id")
            if not isinstance(tool_call_id, str):
                return []
            completed = self._completed_tool_call_ids.setdefault(thread_id, set())
            if tool_call_id in completed:
                return []
            completed.add(tool_call_id)
            count = len(completed)
            progress = f"  {count} {'action' if count == 1 else 'actions'} completed"
            if self._work_line_is_last:
                return [f"\x1b[1A\r\x1b[2K{progress}"]
            self._work_line_is_last = True
            return ["", "WORK", progress]
        if event_kind == "token_usage":
            total_tokens = detail.get("total_tokens")
            if isinstance(total_tokens, int) and not isinstance(total_tokens, bool):
                self._latest_total_tokens[thread_id] = total_tokens
            return []
        if event_kind == "rate_limit":
            weekly_used = _weekly_limit_used_percent(detail.get("windows"))
            if weekly_used is not None:
                self._weekly_limit_used_percent[thread_id] = weekly_used
            return []
        if event_kind == "compaction":
            self._work_line_is_last = False
            return ["", "UPDATE", "  Context compacted"]
        if event_kind not in {"turn_completed", "turn_aborted"}:
            return []
        self._durable_terminal_target_ids.add(thread_id)
        self._work_line_is_last = False
        self._target_states[thread_id] = (
            "turnCompleted" if event_kind == "turn_completed" else "turnAborted"
        )
        path = self._target_paths.get(thread_id, thread_id)
        verb = "finished" if event_kind == "turn_completed" else "stopped"
        summary = f"{'✓' if event_kind == 'turn_completed' else '■'} "
        summary += f"{_agent_display_name(path)} {verb}"
        details = []
        elapsed = _format_elapsed(self._turn_started_at.get(thread_id), event_time_utc)
        if elapsed is not None:
            details.append(elapsed)
        tool_count = len(self._completed_tool_call_ids.get(thread_id, set()))
        details.append(f"{tool_count} {'action' if tool_count == 1 else 'actions'}")
        total_tokens = self._latest_total_tokens.get(thread_id)
        if total_tokens is not None:
            details.append(f"{total_tokens:,} tokens")
        if details:
            summary += " · " + " · ".join(details)
        lines = ["", summary]
        weekly_used = self._weekly_limit_used_percent.get(thread_id)
        if weekly_used is not None:
            lines.append(f"  Weekly limit: {_display_percentage(weekly_used)} used")
        return lines

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
        if (
            caught_up
            and self._target_states
            and self.target_thread_ids <= self._durable_terminal_target_ids
        ):
            self._terminal_drain_complete = True

    def header_lines(self) -> tuple[str, ...]:
        return (
            "RODEX · LIVE AGENT",
            "Agent replies and high-signal progress from exact live + durable events.",
        )


def _agent_display_name(agent_path: str) -> str:
    return agent_path.rstrip("/").rsplit("/", 1)[-1] or agent_path


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
            payload = control_socket.recv(64 * 1024)
        except OSError:
            return
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(event, dict) and event.get("schema") == OBSERVER_SCHEMA:
            events.put(event)


def _observer_runtime_liveness(
    protocol_event_socket_path: Path,
    events: queue.Queue[dict[str, object]],
) -> None:
    try:
        with unix_connect(
            str(protocol_event_socket_path),
            uri=f"ws://localhost{AGENT_OBSERVER_EVENT_STREAM_PATH}",
            compression=None,
            max_size=None,
        ) as connection:
            while True:
                message = connection.recv()
                try:
                    decoded = json.loads(message)
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                    continue
                if not isinstance(decoded, dict):
                    continue
                projected = project_agent_message_event(decoded)
                if projected is not None:
                    events.put(projected)
    except (ConnectionClosed, OSError):
        pass
    finally:
        events.put({"schema": OBSERVER_SCHEMA, "kind": "runtime_closed"})


def _read_and_render_available_trace(
    view: AgentObserverView,
    rodex_sessions_id: int,
    database_path: Path,
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
            return


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
    controls = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    controls.bind(str(control_path))
    control_path.chmod(0o600)
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
                elif kind == "app_server_agent_message":
                    _print_lines(view.accept_agent_message_event(event))
                elif kind == "parent_request":
                    _print_lines(view.accept_parent_request_event(event))
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
