"""Dedicated tmux presentation for exact live and durable sub-agent facts."""

from __future__ import annotations

import argparse
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

OBSERVER_SCHEMA: Final = "rodex-agent-observer-v1"
OBSERVER_CONTROL_SOCKET_NAME: Final = "agent-observer.sock"
OBSERVER_PRIMARY_PANE_OPTION: Final = "@rodex_agent_observer_pane_id"
OBSERVER_OWNER_PANE_OPTION: Final = "@rodex_agent_observer_for"
OBSERVER_TRACE_PAGE_SIZE: Final = 500
_PANE_ID_PATTERN: Final = re.compile(r"%[0-9]+")
_RODEX_SESSION_ID_PATTERN: Final = re.compile(r"[0-9a-f]{16}")
_COLLABORATION_METHODS: Final = frozenset({"item/started", "item/completed"})
_COLLABORATION_CALL_STATUSES: Final = frozenset({"inProgress", "completed", "failed"})
_COLLABORATION_AGENT_STATUSES: Final = frozenset(
    {
        "pendingInit",
        "running",
        "interrupted",
        "completed",
        "errored",
        "shutdown",
        "notFound",
    }
)
Runner = Callable[..., subprocess.CompletedProcess[str]]
CursorReader = Callable[[int, Path], uuid.UUID | None]
EventSender = Callable[[Path, dict[str, object]], None]


def observer_control_socket_path(protocol_event_socket_path: Path) -> Path:
    """Return the private runtime socket used for observer wake notifications."""
    return protocol_event_socket_path.with_name(OBSERVER_CONTROL_SOCKET_NAME)


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


def project_collaboration_event(
    event: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Project one exact App Server collaboration item without prompt/message bodies."""
    if event is None or event.get("method") not in _COLLABORATION_METHODS:
        return None
    method = event["method"]
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    item = params.get("item")
    if not isinstance(item, Mapping):
        return None
    if item.get("type") != "collabAgentToolCall":
        return None
    item_id = item.get("id")
    tool = item.get("tool")
    if not isinstance(item_id, str) or not item_id or not isinstance(tool, str) or not tool:
        return None
    thread_id = _optional_uuid_text(params.get("threadId"))
    if thread_id is None:
        return None
    turn_id = params.get("turnId")
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
        return None
    status = item.get("status")
    if status is not None and status not in _COLLABORATION_CALL_STATUSES:
        return None
    sender_thread_id = item.get("senderThreadId")
    if sender_thread_id is not None:
        sender_thread_id = _optional_uuid_text(sender_thread_id)
        if sender_thread_id is None:
            return None
    receiver_thread_ids = item.get("receiverThreadIds", [])
    if not isinstance(receiver_thread_ids, list):
        return None
    projected_receivers: list[str] = []
    for receiver_thread_id in receiver_thread_ids:
        projected_receiver = _optional_uuid_text(receiver_thread_id)
        if projected_receiver is None:
            return None
        projected_receivers.append(projected_receiver)
    model = item.get("model")
    if model is not None and not isinstance(model, str):
        return None
    reasoning_effort = item.get("reasoningEffort")
    if reasoning_effort is not None and not isinstance(reasoning_effort, str):
        return None
    agent_states = item.get("agentsStates", {})
    if not isinstance(agent_states, Mapping):
        return None
    projected_states: dict[str, str] = {}
    for target, raw_state in agent_states.items():
        target_id = _optional_uuid_text(target)
        if target_id is None or not isinstance(raw_state, Mapping):
            return None
        agent_status = raw_state.get("status")
        if agent_status not in _COLLABORATION_AGENT_STATUSES:
            return None
        projected_states[target_id] = str(agent_status)
    return {
        "schema": OBSERVER_SCHEMA,
        "kind": "app_server_collaboration",
        "method": method,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "item": {
            "type": "collabAgentToolCall",
            "id": item_id,
            "tool": tool,
            "status": status,
            "sender_thread_id": sender_thread_id,
            "receiver_thread_ids": projected_receivers,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "agent_states": projected_states,
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
    """React to exact collaboration events at the live tmux boundary."""

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
        self._known_collaboration_item_ids: set[str] = set()
        self._tracked_target_thread_ids: set[str] = set()

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
        projected = project_collaboration_event(event)
        if projected is None or self._database_path is None:
            return
        root_thread_id = self._root_thread_id
        if root_thread_id is None or projected["thread_id"] != str(root_thread_id):
            return
        item = projected["item"]
        assert isinstance(item, dict)
        item_id = str(item["id"])
        receivers = item["receiver_thread_ids"]
        assert isinstance(receivers, list)
        is_new_spawn = (
            projected["method"] == "item/started" and item["tool"] == "spawnAgent"
        )
        is_known = item_id in self._known_collaboration_item_ids
        receiver_is_tracked = bool(
            self._tracked_target_thread_ids.intersection(str(value) for value in receivers)
        )
        if not is_new_spawn and not is_known and not receiver_is_tracked:
            return
        if is_new_spawn:
            assert self._rodex_sessions_id is not None
            cursor = self._cursor_reader(self._rodex_sessions_id, self._database_path)
            projected["after_event_id"] = None if cursor is None else str(cursor)
            self._known_collaboration_item_ids.add(item_id)
        self._tracked_target_thread_ids.update(str(value) for value in receivers)
        pane_target = self._locate_observer_pane()
        if pane_target is None:
            if not is_new_spawn:
                return
            self._observer_pane_target = self._create_observer_pane(projected)
            return
        try:
            assert self._event_sender is not None
            self._event_sender(
                observer_control_socket_path(self._protocol_event_socket_path),
                projected,
            )
        except OSError:
            # The raw event stream provides the same exact update if the datagram
            # endpoint is still starting or has just closed.
            return

    def close(self) -> None:
        """Release only in-process state; tmux owns the persistent presentation pane."""
        if self._event_dispatcher is not None:
            self._event_dispatcher.close()
        self._known_collaboration_item_ids.clear()
        self._tracked_target_thread_ids.clear()

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
    """State and privacy-safe rendering for one observer pane."""

    def __init__(
        self,
        *,
        root_thread_id: uuid.UUID,
        rodex_session_id: str,
        initial_event: Mapping[str, object],
    ) -> None:
        self._root_thread_id = str(root_thread_id)
        self._rodex_session_id = rodex_session_id
        self._after_event_id: uuid.UUID | None = None
        self._target_states: dict[str, str | None] = {}
        self._durable_terminal_target_ids: set[str] = set()
        self._terminal_drain_complete = False
        self._last_trace_publication_sequence = 0
        self._target_paths: dict[str, str] = {}
        self._seen_app_events: set[str] = set()
        self._coverage: tuple[str | None, int] | None = None
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
        """Accept one already-sanitized collaboration event."""
        if event.get("schema") != OBSERVER_SCHEMA:
            return []
        if event.get("kind") != "app_server_collaboration":
            return []
        item = event.get("item")
        if not isinstance(item, Mapping):
            return []
        fingerprint = json.dumps(
            {key: value for key, value in event.items() if key != "after_event_id"},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        was_monitoring = self.monitoring
        if fingerprint in self._seen_app_events:
            return []
        self._seen_app_events.add(fingerprint)
        cursor = event.get("after_event_id")
        if not was_monitoring and isinstance(cursor, str):
            self._after_event_id = uuid.UUID(cursor)
        item_id = item.get("id")
        tool = item.get("tool")
        status = item.get("status")
        if not isinstance(item_id, str) or not isinstance(tool, str):
            return []
        if status is not None and not isinstance(status, str):
            return []
        receivers = item.get("receiver_thread_ids", [])
        if not isinstance(receivers, list):
            return []
        receiver_ids = frozenset(target for target in receivers if isinstance(target, str))
        new_receiver_ids = receiver_ids - self.target_thread_ids
        for target in receiver_ids:
            self._target_states.setdefault(target, None)
        for target in new_receiver_ids:
            self._durable_terminal_target_ids.discard(target)
        if new_receiver_ids:
            self._terminal_drain_complete = False
        states = item.get("agent_states", {})
        if isinstance(states, Mapping):
            for target, agent_status in states.items():
                if isinstance(target, str) and isinstance(agent_status, str):
                    self._target_states[target] = agent_status
        model = item.get("model")
        effort = item.get("reasoning_effort")
        details = [
            f"APP {event.get('method')} {tool}",
            f"call={item_id}",
            f"status={status or '--'}",
        ]
        if model:
            details.append(f"model={model}")
        if effort:
            details.append(f"effort={effort}")
        if receivers:
            details.append(
                "targets=" + ",".join(_short_identity(str(target)) for target in receivers)
            )
        return [" | ".join(details)]

    def accept_trace_snapshot(self, snapshot: RodexAgentTraceSnapshot) -> list[str]:
        """Advance through one durable page and render only exact tracked identities."""
        lines: list[str] = []
        coverage = (snapshot.coverage_state, snapshot.unrecognized_record_count)
        if coverage != self._coverage:
            self._coverage = coverage
            lines.append(
                "SQL trace "
                f"schema={snapshot.trace_schema_version or '--'} "
                f"coverage={snapshot.coverage_state or '--'} "
                f"unknown={snapshot.unrecognized_record_count}"
            )
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
            if thread_id in self._target_states and event_kind in {
                "turn_completed",
                "turn_aborted",
            }:
                self._durable_terminal_target_ids.add(str(thread_id))
                self._target_states[str(thread_id)] = (
                    "turnCompleted" if event_kind == "turn_completed" else "turnAborted"
                )
            rendered = _format_trace_event(event, self._target_paths)
            if rendered is not None:
                lines.append(rendered)
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
            "RODEX AGENT OBSERVER  ·  exact App Server + durable SQL trace",
            f"Rodex {self._rodex_session_id}  ·  "
            f"root {_short_identity(self._root_thread_id)}",
            "No prompt, message body, command text, or hidden reasoning is displayed.",
        )


def _format_trace_event(
    event: Mapping[str, object],
    target_paths: Mapping[str, str],
) -> str | None:
    kind = event.get("event_kind")
    thread_id = str(event.get("codex_thread_id"))
    detail = event.get("detail")
    values = detail if isinstance(detail, Mapping) else {}
    prefix = f"{_event_clock(event.get('event_time_utc'))} SQL {_short_identity(thread_id)}"
    path = target_paths.get(thread_id)
    if path:
        prefix += f" {path}"
    if kind == "turn_context":
        fields = [
            f"model={values.get('model') or '--'}",
            f"effort={values.get('reasoning_effort') or '--'}",
            f"cwd={values.get('working_directory') or '--'}",
            f"sandbox={values.get('sandbox_mode') or '--'}",
        ]
        return f"{prefix} context | " + " | ".join(fields)
    if kind in {"turn_started", "turn_completed", "turn_aborted"}:
        return f"{prefix} {str(kind).replace('_', ' ')}"
    if kind == "subagent_activity":
        target = values.get("target_codex_thread_id")
        return (
            f"{prefix} agent {values.get('activity_kind') or '--'} | "
            f"target={_short_identity(str(target))} | "
            f"path={values.get('agent_path') or '--'}"
        )
    if kind == "message":
        return (
            f"{prefix} message | role={values.get('message_role') or '--'} | "
            f"phase={values.get('message_phase') or '--'} | "
            f"blocks={values.get('content_block_count')} | "
            f"bytes={values.get('body_utf8_bytes')} | "
            f"capture={values.get('body_capture_state') or '--'}"
        )
    if kind == "tool_call":
        return (
            f"{prefix} tool {values.get('tool_name') or '--'} | "
            f"activity={values.get('activity_kind') or '--'} | "
            f"status={values.get('tool_status') or '--'} | "
            f"request={values.get('request_utf8_bytes')}B | "
            f"response={values.get('response_utf8_bytes')}B"
        )
    if kind == "command_execution":
        return (
            f"{prefix} command | status={values.get('command_status') or '--'} | "
            f"exit={values.get('exit_code')} | duration={values.get('duration_ms')}ms | "
            f"args={values.get('command_argument_count')} | "
            f"out={values.get('aggregated_output_utf8_bytes')}B"
        )
    if kind == "token_usage":
        return (
            f"{prefix} tokens | total={values.get('total_tokens')} | "
            f"in={values.get('input_tokens')} | "
            f"cached={values.get('cached_input_tokens')} | "
            f"out={values.get('output_tokens')} | "
            f"reasoning={values.get('reasoning_output_tokens')} | "
            f"context={values.get('context_used_percent')}%"
        )
    if kind == "rate_limit":
        windows = values.get("windows", [])
        if not isinstance(windows, list):
            return None
        rendered = []
        for window in windows:
            if isinstance(window, Mapping):
                rendered.append(
                    f"{window.get('limit_id')}={window.get('used_percent')}%/"
                    f"{window.get('window_minutes')}m"
                )
        return f"{prefix} rate | " + ", ".join(rendered)
    if kind == "compaction":
        return f"{prefix} context compacted"
    if kind == "session_metadata":
        return f"{prefix} session metadata"
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
                projected = project_collaboration_event(decoded)
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


def _short_identity(value: str) -> str:
    return value if len(value) <= 12 else f"{value[:8]}…{value[-4:]}"


def _event_clock(value: object) -> str:
    if isinstance(value, str) and len(value) >= 19 and value[10] == "T":
        return value[11:19]
    return "--:--:--"


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
        rodex_session_id=namespace.rodex_session_id,
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
                if kind == "app_server_collaboration":
                    _print_lines(view.accept_app_server_event(event))
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
