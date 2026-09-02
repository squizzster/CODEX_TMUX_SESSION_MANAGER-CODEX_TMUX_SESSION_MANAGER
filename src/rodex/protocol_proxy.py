"""Codex WebSocket proxy with Rodex-only TUI notices and derived signals."""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import stat
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from typing import Any, Final

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect
from websockets.sync.server import ServerConnection, unix_serve

from .analytics_source_reader import AnalyticsSourceReadError, open_rollout_descriptor
from .app_server_contract import CODEX_APP_SERVER
from .status_bar import (
    CONTEXT_COMPACTION_FRAME_INTERVAL_SECONDS,
    RODEX_CONTEXT_STATUS_OPTION,
    RODEX_TOOL_CALL_STATUS_OPTION,
    compacting_status_segment,
    context_status_segment,
)
from .tmux_session_capability import TmuxRuntimeCapability
from .tmux_status import TmuxStatusOption

TOOL_CALL_ITEM_TYPES: Final = frozenset(
    {
        "collabAgentToolCall",
        "commandExecution",
        "dynamicToolCall",
        "fileChange",
        "imageGeneration",
        "imageView",
        "mcpToolCall",
        "sleep",
        "webSearch",
    }
)

ToolCountCallback = Callable[[int], None]
ProtocolEventCallback = Callable[[str | bytes, dict[str, Any] | None], None]
ContextStatusCallback = Callable[[str], None]
DisconnectCallback = Callable[[], None]
ROLLOUT_CONTEXT_POLL_INTERVAL_SECONDS: Final = 0.25
ROLLOUT_CONTEXT_MAX_IDLE_POLL_INTERVAL_SECONDS: Final = 2.0
CONTEXT_COMPACTION_WATCHDOG_SECONDS: Final = 5.0
_ROLLOUT_CONTEXT_TAIL_BYTES: Final = 8 * 1024 * 1024
_ROLLOUT_CONTEXT_LINE_BYTES: Final = 256 * 1024
_ROLLOUT_CONTEXT_BOUNDARY_BYTES: Final = 4 * 1024
_EVENT_STREAM_CLOSED: Final = object()
EVENT_STREAM_READY_METHOD: Final = "rodex/event-stream/ready"
CONTROL_CONNECTION_PATH: Final = "/rodex-control"
TUI_NOTICE_CONNECTION_PATH: Final = "/rodex-tui-notice"
TUI_NOTICE_METHOD: Final = "rodex/tui-notice"
ANALYTICS_EVENT_STREAM_PATH: Final = "/rodex-analytics"
AGENT_OBSERVER_EVENT_STREAM_PATH: Final = "/rodex-agent-observer"
ANALYTICS_WAKE_EVENT_METHODS: Final = frozenset(
    {
        CODEX_APP_SERVER.thread_started_method,
        CODEX_APP_SERVER.turn_started_method,
        CODEX_APP_SERVER.turn_completed_method,
        "item/started",
        "item/completed",
        "thread/tokenUsage/updated",
    }
)
EVENT_STREAM_READY_MESSAGE: Final = json.dumps(
    {
        "method": EVENT_STREAM_READY_METHOD,
        "params": {"activeTurns": {}, "knownThreads": []},
    },
    separators=(",", ":"),
)


@dataclass(frozen=True, slots=True)
class _RolloutFollowCheckpoint:
    source_device: int
    source_inode: int
    source_size_bytes: int
    source_mtime_ns: int
    source_ctime_ns: int
    cursor_offset: int
    boundary_sha256: str


class RodexProtocolProxyError(RuntimeError):
    """The local Codex protocol proxy could not start or stop cleanly."""


def publish_tui_notice(
    proxy_socket_path: Path,
    message: str,
    *,
    connector: Callable[..., Any] = unix_connect,
) -> bool:
    """Ask Rodex's proxy to show one TUI-owned warning without an App Server turn."""
    if not message.strip():
        return False
    request_id = 0
    request = json.dumps(
        {
            "method": TUI_NOTICE_METHOD,
            "id": request_id,
            "params": {"message": message},
        },
        separators=(",", ":"),
    )
    try:
        with connector(
            str(proxy_socket_path),
            uri=f"ws://localhost{TUI_NOTICE_CONNECTION_PATH}",
            compression=None,
            open_timeout=1,
            close_timeout=1,
            max_size=None,
        ) as connection:
            connection.send(request)
            response = _json_object(connection.recv(timeout=1))
    except (ConnectionClosed, OSError, TimeoutError):
        return False
    if response is None or response.get("id") != request_id:
        return False
    result = response.get("result")
    return isinstance(result, dict) and result.get("delivered") is True


class CodexProtocolEventTap:
    """Fan out primary TUI protocol events without blocking its live stream."""

    def __init__(self, event_socket_path: Path, *, queue_size: int = 1024) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._event_socket_path = event_socket_path
        self._queue_size = queue_size
        self._subscribers: dict[queue.Queue[str | bytes | object], tuple[bool, Any]] = {}
        self._subscribers_lock = Lock()
        self._closed = False
        self._active_turns: dict[str, str] = {}
        self._known_threads: dict[str, dict[str, object]] = {}
        self._server: Any | None = None
        self._server_thread: Thread | None = None

    def start(self) -> None:
        """Bind the runtime-only event socket and accept event subscribers."""
        with self._subscribers_lock:
            if self._closed:
                raise RodexProtocolProxyError("Codex protocol event tap is closed")
            if self._server is not None:
                raise RodexProtocolProxyError("Codex protocol event tap is already running")
            self._event_socket_path.unlink(missing_ok=True)
            try:
                server = unix_serve(
                    self._handle_subscriber,
                    path=str(self._event_socket_path),
                    compression=None,
                    max_size=None,
                )
                self._event_socket_path.chmod(0o600)
            except OSError as error:
                raise RodexProtocolProxyError(
                    f"could not bind Codex protocol event tap: {error}"
                ) from error
            server_thread = Thread(
                target=server.serve_forever,
                name="rodex-codex-protocol-event-tap",
                daemon=True,
            )
            self._server = server
            self._server_thread = server_thread
            server_thread.start()

    def publish(self, message: str | bytes) -> None:
        """Offer one event to every live subscriber without delaying the TUI."""
        self.publish_protocol_event(message, _json_object(message))

    def publish_protocol_event(
        self,
        message: str | bytes,
        event: dict[str, Any] | None,
    ) -> None:
        """Publish raw transport bytes using the proxy's single decoded value."""
        with self._subscribers_lock:
            if self._closed:
                return
            if event is not None:
                _update_active_turns(self._active_turns, event)
                _update_known_threads(self._known_threads, self._active_turns, event)
            subscribers = tuple(self._subscribers.items())
        is_analytics_event = (
            event is not None and event.get("method") in ANALYTICS_WAKE_EVENT_METHODS
        )
        for subscriber, (analytics_only, _connection) in subscribers:
            if analytics_only and not is_analytics_event:
                continue
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                self._disconnect_slow_subscriber(subscriber)

    def close(self) -> None:
        """Close subscribers, stop the event server, and remove its socket."""
        with self._subscribers_lock:
            self._closed = True
            server = self._server
            server_thread = self._server_thread
            self._server = None
            self._server_thread = None
            subscribers = tuple(self._subscribers.items())
            self._subscribers.clear()
            self._active_turns.clear()
            self._known_threads.clear()
        for subscriber, (_analytics_only, connection) in subscribers:
            _close_subscriber_queue(subscriber)
            _interrupt_subscriber_connection(connection)
        if server is not None:
            server.shutdown(close_connections=True)
        if server_thread is not None:
            server_thread.join(timeout=5)
            if server_thread.is_alive():
                raise RodexProtocolProxyError("Codex protocol event tap did not stop")
        self._event_socket_path.unlink(missing_ok=True)

    def reset_after_disconnect(self) -> None:
        """Forget identities scoped to the released primary connection."""
        with self._subscribers_lock:
            self._active_turns.clear()
            self._known_threads.clear()

    def _handle_subscriber(self, connection: Any) -> None:
        subscriber: queue.Queue[str | bytes | object] = queue.Queue(self._queue_size)
        semantic_only = _connection_path(connection) in {
            ANALYTICS_EVENT_STREAM_PATH,
            AGENT_OBSERVER_EVENT_STREAM_PATH,
        }
        with self._subscribers_lock:
            if self._closed:
                admitted = False
                ready_message = ""
            else:
                admitted = True
                self._subscribers[subscriber] = (semantic_only, connection)
                ready_message = json.dumps(
                    {
                        "method": EVENT_STREAM_READY_METHOD,
                        "params": {
                            "activeTurns": dict(self._active_turns),
                            "knownThreads": list(self._known_threads.values()),
                        },
                    },
                    separators=(",", ":"),
                )
        if not admitted:
            _interrupt_subscriber_connection(connection)
            return
        try:
            connection.send(ready_message)
            while True:
                message = subscriber.get()
                if message is _EVENT_STREAM_CLOSED:
                    return
                connection.send(message)
        except (ConnectionClosed, OSError):
            return
        finally:
            with self._subscribers_lock:
                self._subscribers.pop(subscriber, None)
            _interrupt_subscriber_connection(connection)

    def _disconnect_slow_subscriber(
        self, subscriber: queue.Queue[str | bytes | object]
    ) -> None:
        with self._subscribers_lock:
            registration = self._subscribers.pop(subscriber, None)
            if registration is None:
                return
        _close_subscriber_queue(subscriber)
        _semantic_only, connection = registration
        _interrupt_subscriber_connection(connection)


class ToolCallCounter:
    """Count unique Codex tool-call items when their lifecycle starts."""

    def __init__(self, on_count_changed: ToolCountCallback) -> None:
        self._on_count_changed = on_count_changed
        self._item_ids: set[str] = set()
        self._count = 0
        self._lock = Lock()

    @property
    def count(self) -> int:
        """Return the current runtime's tool-call count."""
        with self._lock:
            return self._count

    def observe_server_message(self, message: str | bytes) -> None:
        """Update the count from one app-server-to-TUI protocol message."""
        self.observe_protocol_event(_json_object(message))

    def observe_protocol_event(self, event: dict[str, Any] | None) -> None:
        """Update the count from an already decoded protocol event."""
        lifecycle = _tool_call_item_lifecycle(event)
        if lifecycle is None:
            return
        method, item_id = lifecycle
        with self._lock:
            if method == "item/completed":
                self._item_ids.discard(item_id)
                return
            if item_id in self._item_ids:
                return
            self._item_ids.add(item_id)
            self._count += 1
            count = self._count
        try:
            self._on_count_changed(count)
        except (OSError, subprocess.SubprocessError):
            # Status rendering must never interrupt the Codex protocol stream.
            return


class TmuxToolCallStatus:
    """Publish a proxy-derived tool count into one tmux session option."""

    def __init__(
        self,
        tmux_binary: str,
        capability: TmuxRuntimeCapability,
        tmux_pane_target: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    ) -> None:
        self._option = TmuxStatusOption(
            tmux_binary,
            capability,
            tmux_pane_target,
            RODEX_TOOL_CALL_STATUS_OPTION,
            runner=runner,
        )

    def update(self, count: int) -> None:
        """Set the stable tmux user option consumed by the status format."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("tool-call count must be a non-negative integer")
        self._option.publish(str(count))


class TmuxContextStatus:
    """Publish proxy-derived context state into one tmux session option."""

    def __init__(
        self,
        tmux_binary: str,
        capability: TmuxRuntimeCapability,
        tmux_pane_target: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    ) -> None:
        self._option = TmuxStatusOption(
            tmux_binary,
            capability,
            tmux_pane_target,
            RODEX_CONTEXT_STATUS_OPTION,
            runner=runner,
        )

    def update(self, rendered_status: str) -> None:
        """Set the stable tmux user option consumed by the base status format."""
        if not isinstance(rendered_status, str) or not rendered_status:
            raise ValueError("rendered context status must be non-empty")
        self._option.publish(rendered_status)


class CodexContextStatusObserver:
    """Project live App Server usage and compaction events into tmux status."""

    def __init__(
        self,
        on_status_changed: ContextStatusCallback,
        *,
        animation_interval_seconds: float = CONTEXT_COMPACTION_FRAME_INTERVAL_SECONDS,
        compaction_watchdog_seconds: float = CONTEXT_COMPACTION_WATCHDOG_SECONDS,
        codex_sessions_root: Path | None = None,
        rollout_poll_interval_seconds: float = ROLLOUT_CONTEXT_POLL_INTERVAL_SECONDS,
        rollout_max_idle_poll_interval_seconds: float = (
            ROLLOUT_CONTEXT_MAX_IDLE_POLL_INTERVAL_SECONDS
        ),
    ) -> None:
        if animation_interval_seconds <= 0 or not math.isfinite(animation_interval_seconds):
            raise ValueError("animation interval must be finite and positive")
        if compaction_watchdog_seconds <= 0 or not math.isfinite(
            compaction_watchdog_seconds
        ):
            raise ValueError("compaction watchdog must be finite and positive")
        if rollout_poll_interval_seconds <= 0 or not math.isfinite(
            rollout_poll_interval_seconds
        ):
            raise ValueError("rollout poll interval must be finite and positive")
        if (
            rollout_max_idle_poll_interval_seconds < rollout_poll_interval_seconds
            or not math.isfinite(rollout_max_idle_poll_interval_seconds)
        ):
            raise ValueError(
                "rollout maximum idle poll interval must be finite and no shorter "
                "than the rollout poll interval"
            )
        self._on_status_changed = on_status_changed
        self._animation_interval_seconds = animation_interval_seconds
        self._compaction_watchdog_seconds = compaction_watchdog_seconds
        self._monotonic = time.monotonic
        self._codex_sessions_root = (
            None
            if codex_sessions_root is None
            else Path(codex_sessions_root).resolve(strict=False)
        )
        self._rollout_poll_interval_seconds = rollout_poll_interval_seconds
        self._rollout_max_idle_poll_interval_seconds = (
            rollout_max_idle_poll_interval_seconds
        )
        self._primary_thread_id: str | None = None
        self._latest_context_status = context_status_segment(None)
        self._active_compaction_item_ids: set[str] = set()
        self._animation_generation = 0
        self._animation_stop: Event | None = None
        self._animation_threads: list[Thread] = []
        self._rollout_stop: Event | None = None
        self._rollout_thread: Thread | None = None
        self._rollout_wake_condition = Condition()
        self._rollout_wake_generation = 0
        self._closed = False
        self._lock = Lock()

    def observe_server_message(self, message: str | bytes) -> None:
        """Consume one primary app-server-to-TUI protocol message."""
        self.observe_protocol_event(_json_object(message))

    def observe_protocol_event(self, payload: dict[str, Any] | None) -> None:
        """Consume the proxy's single decoded primary protocol event."""
        if payload is None:
            return
        method = payload.get("method")
        params = payload.get("params")
        if not isinstance(params, dict):
            return
        event_thread_id = (
            _started_thread_id(params)
            if method == CODEX_APP_SERVER.thread_started_method
            else _event_thread_id(params)
        )
        if event_thread_id is not None:
            self._wake_rollout_follower(event_thread_id)
        if method == CODEX_APP_SERVER.thread_started_method:
            thread_id = _started_thread_id(params)
            if thread_id is not None:
                with self._lock:
                    if self._closed or not self._accept_thread_locked(thread_id):
                        return
                    rollout_path = _started_thread_rollout_path(
                        params,
                        thread_id=thread_id,
                        codex_sessions_root=self._codex_sessions_root,
                    )
                    if rollout_path is not None and self._rollout_thread is None:
                        rollout_stop = Event()
                        self._rollout_stop = rollout_stop
                        self._rollout_thread = Thread(
                            target=self._follow_rollout_context,
                            args=(thread_id, rollout_path, rollout_stop),
                            name="rodex-rollout-context-follower",
                            daemon=True,
                        )
                        self._rollout_thread.start()
            return
        if method == "thread/tokenUsage/updated":
            context_percent = _context_percent(params)
            thread_id = _event_thread_id(params)
            if context_percent is None or thread_id is None:
                return
            rendered_status = context_status_segment(context_percent)
            with self._lock:
                if self._closed or not self._accept_thread_locked(thread_id):
                    return
                self._latest_context_status = rendered_status
                if not self._active_compaction_item_ids:
                    self._publish_status_locked(rendered_status)
            return
        if method == CODEX_APP_SERVER.turn_completed_method:
            thread_id = _event_thread_id(params)
            if thread_id is None:
                return
            with self._lock:
                if self._closed or not self._accept_thread_locked(thread_id):
                    return
                self._finish_compaction_locked()
            return
        if method not in {"item/started", "item/completed"}:
            return
        item = params.get("item")
        if not isinstance(item, dict) or item.get("type") != "contextCompaction":
            return
        item_id = item.get("id")
        thread_id = _event_thread_id(params)
        if not isinstance(item_id, str) or not item_id or thread_id is None:
            return
        if method == "item/started":
            with self._lock:
                if self._closed or not self._accept_thread_locked(thread_id):
                    return
                if item_id in self._active_compaction_item_ids:
                    return
                should_start_animation = not self._active_compaction_item_ids
                self._active_compaction_item_ids.add(item_id)
                if should_start_animation:
                    # The pre-compaction percentage no longer describes the live context.
                    self._latest_context_status = context_status_segment(None)
                    self._new_animation_thread_locked().start()
            return
        with self._lock:
            if self._closed or not self._accept_thread_locked(thread_id):
                return
            if item_id not in self._active_compaction_item_ids:
                return
            if len(self._active_compaction_item_ids) > 1:
                self._active_compaction_item_ids.remove(item_id)
                return
            self._finish_compaction_locked()

    def observe_rollout_context_percent(
        self,
        thread_id: str,
        context_percent: float,
    ) -> None:
        """Accept one authenticated primary-rollout context snapshot."""
        self._observe_rollout_context_percent(thread_id, context_percent)

    def _observe_rollout_context_percent(
        self,
        thread_id: str,
        context_percent: float,
        *,
        rollout_stop: Event | None = None,
    ) -> None:
        rendered_status = context_status_segment(context_percent)
        with self._lock:
            if (
                self._closed
                or (rollout_stop is not None and rollout_stop is not self._rollout_stop)
                or not self._accept_thread_locked(thread_id)
            ):
                return
            status_changed = self._latest_context_status != rendered_status
            self._latest_context_status = rendered_status
            if status_changed and not self._active_compaction_item_ids:
                self._publish_status_locked(rendered_status)

    def close(self) -> None:
        """Stop any live animation without delaying protocol shutdown."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._active_compaction_item_ids.clear()
            self._animation_generation += 1
            if self._animation_stop is not None:
                self._animation_stop.set()
                self._animation_stop = None
            animation_threads = tuple(self._animation_threads)
            rollout_stop = self._rollout_stop
            rollout_thread = self._rollout_thread
            self._rollout_stop = None
            self._rollout_thread = None
            if rollout_stop is not None:
                rollout_stop.set()
        if rollout_stop is not None:
            self._signal_rollout_follower()
        for animation_thread in animation_threads:
            animation_thread.join(timeout=1)
        if rollout_thread is not None:
            rollout_thread.join(timeout=1)

    def reset_after_disconnect(self) -> None:
        """Discard state and workers owned by the disconnected primary transport."""
        with self._lock:
            if self._closed:
                return
            had_active_compaction = bool(self._active_compaction_item_ids)
            self._active_compaction_item_ids.clear()
            self._animation_generation += 1
            if self._animation_stop is not None:
                self._animation_stop.set()
                self._animation_stop = None
            animation_threads = tuple(self._animation_threads)
            rollout_stop = self._rollout_stop
            rollout_thread = self._rollout_thread
            self._rollout_stop = None
            self._rollout_thread = None
            if rollout_stop is not None:
                rollout_stop.set()
            self._primary_thread_id = None
            disconnected_status = context_status_segment(None)
            should_publish = (
                had_active_compaction or self._latest_context_status != disconnected_status
            )
            self._latest_context_status = disconnected_status
            if should_publish:
                self._publish_status_locked(disconnected_status)
        if rollout_stop is not None:
            self._signal_rollout_follower()
        for animation_thread in animation_threads:
            animation_thread.join(timeout=1)
        if rollout_thread is not None:
            rollout_thread.join(timeout=1)
        with self._lock:
            self._animation_threads = [
                thread for thread in self._animation_threads if thread.is_alive()
            ]

    def _accept_thread_locked(self, thread_id: str) -> bool:
        if self._primary_thread_id is None:
            self._primary_thread_id = thread_id
        return self._primary_thread_id == thread_id

    def _wake_rollout_follower(self, thread_id: str) -> None:
        with self._lock:
            should_wake = (
                not self._closed
                and self._primary_thread_id == thread_id
                and self._rollout_thread is not None
            )
        if should_wake:
            self._signal_rollout_follower()

    def _signal_rollout_follower(self) -> None:
        with self._rollout_wake_condition:
            self._rollout_wake_generation += 1
            self._rollout_wake_condition.notify_all()

    def _rollout_wake_snapshot(self) -> int:
        with self._rollout_wake_condition:
            return self._rollout_wake_generation

    def _wait_for_rollout_activity(
        self,
        stop: Event,
        wake_generation: int,
        timeout_seconds: float,
    ) -> tuple[bool, int]:
        """Wait for shutdown, a primary protocol event, or the bounded poll."""
        with self._rollout_wake_condition:
            if not stop.is_set() and self._rollout_wake_generation == wake_generation:
                self._rollout_wake_condition.wait_for(
                    lambda: (
                        stop.is_set() or self._rollout_wake_generation != wake_generation
                    ),
                    timeout=timeout_seconds,
                )
            return stop.is_set(), self._rollout_wake_generation

    def _new_animation_thread_locked(self) -> Thread:
        self._animation_generation += 1
        generation = self._animation_generation
        stop = Event()
        self._animation_stop = stop
        self._animation_threads = [
            thread for thread in self._animation_threads if thread.is_alive()
        ]
        animation_thread = Thread(
            target=self._animate_compaction,
            args=(generation, stop),
            name="rodex-context-compaction-animation",
            daemon=True,
        )
        self._animation_threads.append(animation_thread)
        return animation_thread

    def _animate_compaction(self, generation: int, stop: Event) -> None:
        frame_index = 0
        deadline = self._monotonic() + self._compaction_watchdog_seconds
        while not stop.is_set():
            with self._lock:
                if self._closed or generation != self._animation_generation:
                    return
                if self._monotonic() >= deadline:
                    self._finish_compaction_locked()
                    return
                self._publish_status_locked(compacting_status_segment(frame_index))
            frame_index += 1
            stop.wait(self._animation_interval_seconds)

    def _finish_compaction_locked(self) -> None:
        if not self._active_compaction_item_ids:
            return
        self._active_compaction_item_ids.clear()
        if self._animation_stop is not None:
            self._animation_stop.set()
            self._animation_stop = None
        self._animation_generation += 1
        self._publish_status_locked(self._latest_context_status)

    def _publish_status_locked(self, rendered_status: str) -> None:
        try:
            self._on_status_changed(rendered_status)
        except (OSError, subprocess.SubprocessError):
            # Status rendering must never interrupt the Codex protocol stream.
            return

    def _follow_rollout_context(
        self,
        thread_id: str,
        rollout_path: Path,
        stop: Event,
    ) -> None:
        """Follow bounded JSONL additions without retaining arbitrary rollout bodies."""
        wake_generation = self._rollout_wake_snapshot()
        retry_interval_seconds = self._rollout_poll_interval_seconds
        while not stop.is_set():
            try:
                with _open_rollout_for_following(
                    rollout_path,
                    allowed_root=self._codex_sessions_root,
                ) as rollout:
                    retry_interval_seconds = self._rollout_poll_interval_seconds
                    idle_interval_seconds = self._rollout_poll_interval_seconds
                    initial_percent, initial_offset = (
                        _latest_rollout_context_from_open_file(rollout)
                    )
                    if initial_percent is not None:
                        self._observe_rollout_context_percent(
                            thread_id,
                            initial_percent,
                            rollout_stop=stop,
                        )
                    rollout.seek(initial_offset)
                    checkpoint = _rollout_follow_checkpoint(rollout)
                    discarding_long_line = False
                    while not stop.is_set():
                        line_start = rollout.tell()
                        line = rollout.readline(_ROLLOUT_CONTEXT_LINE_BYTES + 1)
                        if not line:
                            if rollout.tell() != checkpoint.cursor_offset:
                                advanced_checkpoint = _advance_rollout_follow_checkpoint(
                                    rollout_path,
                                    rollout,
                                    checkpoint,
                                )
                                if advanced_checkpoint is None:
                                    break
                                checkpoint = advanced_checkpoint
                                idle_interval_seconds = self._rollout_poll_interval_seconds
                            stopped, wake_generation = self._wait_for_rollout_activity(
                                stop,
                                wake_generation,
                                idle_interval_seconds,
                            )
                            if stopped:
                                continue
                            if _rollout_path_requires_reopen(
                                rollout_path,
                                rollout,
                                checkpoint,
                            ):
                                break
                            idle_interval_seconds = min(
                                idle_interval_seconds * 2,
                                self._rollout_max_idle_poll_interval_seconds,
                            )
                            continue
                        idle_interval_seconds = self._rollout_poll_interval_seconds
                        if discarding_long_line:
                            if line.endswith(b"\n"):
                                discarding_long_line = False
                            continue
                        if line.endswith(b"\n"):
                            context_percent = _rollout_context_percent(line)
                            if context_percent is not None:
                                advanced_checkpoint = _advance_rollout_follow_checkpoint(
                                    rollout_path,
                                    rollout,
                                    checkpoint,
                                )
                                if advanced_checkpoint is None:
                                    break
                                checkpoint = advanced_checkpoint
                                self._observe_rollout_context_percent(
                                    thread_id,
                                    context_percent,
                                    rollout_stop=stop,
                                )
                            continue
                        if len(line) > _ROLLOUT_CONTEXT_LINE_BYTES:
                            discarding_long_line = True
                            continue
                        # The writer has not committed the newline yet. Re-read this
                        # bounded partial record after the next append.
                        rollout.seek(line_start)
                        advanced_checkpoint = _advance_rollout_follow_checkpoint(
                            rollout_path,
                            rollout,
                            checkpoint,
                        )
                        if advanced_checkpoint is None:
                            break
                        checkpoint = advanced_checkpoint
                        stopped, wake_generation = self._wait_for_rollout_activity(
                            stop,
                            wake_generation,
                            self._rollout_poll_interval_seconds,
                        )
                        if stopped:
                            continue
                        if _rollout_path_requires_reopen(
                            rollout_path,
                            rollout,
                            checkpoint,
                        ):
                            break
            except (AnalyticsSourceReadError, OSError):
                pass
            if not stop.is_set():
                stopped, wake_generation = self._wait_for_rollout_activity(
                    stop,
                    wake_generation,
                    retry_interval_seconds,
                )
                if not stopped:
                    retry_interval_seconds = min(
                        retry_interval_seconds * 2,
                        self._rollout_max_idle_poll_interval_seconds,
                    )


class CodexProtocolProxy:
    """Forward App Server traffic and accept isolated Rodex TUI notices."""

    def __init__(
        self,
        proxy_socket_path: Path,
        app_server_socket_path: Path,
        tool_call_counter: ToolCallCounter,
        on_primary_server_message: ProtocolEventCallback | None = None,
        on_primary_disconnect: DisconnectCallback | None = None,
    ) -> None:
        self._proxy_socket_path = proxy_socket_path
        self._app_server_socket_path = app_server_socket_path
        self._tool_call_counter = tool_call_counter
        self._on_primary_server_message = on_primary_server_message
        self._on_primary_disconnect = on_primary_disconnect
        self._primary_lifecycle_lock = Lock()
        self._connection_lock = Lock()
        self._primary_send_lock = Lock()
        self._primary_connection_claimed = False
        self._primary_tui_connection: Any | None = None
        self._primary_thread_id: str | None = None
        self._primary_connection_released = Event()
        self._primary_connection_released.set()
        self._server: Any | None = None
        self._server_thread: Thread | None = None

    def start(self) -> None:
        """Bind the proxy socket and begin accepting WebSocket connections."""
        if self._server is not None:
            raise RodexProtocolProxyError("Codex protocol proxy is already running")
        self._proxy_socket_path.unlink(missing_ok=True)
        try:
            self._server = unix_serve(
                self._handle_connection,
                path=str(self._proxy_socket_path),
                compression=None,
                max_size=None,
            )
            self._proxy_socket_path.chmod(0o600)
        except OSError as error:
            raise RodexProtocolProxyError(
                f"could not bind Codex protocol proxy: {error}"
            ) from error
        self._server_thread = Thread(
            target=self._server.serve_forever,
            name="rodex-codex-protocol-proxy",
            daemon=True,
        )
        self._server_thread.start()

    def close(self) -> None:
        """Stop accepting connections and remove the proxy socket."""
        server = self._server
        server_thread = self._server_thread
        self._server = None
        self._server_thread = None
        if server is not None:
            server.shutdown(close_connections=True)
        if server_thread is not None:
            server_thread.join(timeout=5)
            if server_thread.is_alive():
                raise RodexProtocolProxyError("Codex protocol proxy did not stop")
        self._proxy_socket_path.unlink(missing_ok=True)

    def __enter__(self) -> CodexProtocolProxy:
        self.start()
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def wait_for_primary_connection_release(self, timeout_seconds: float) -> None:
        """Wait until the event-producing client transport has fully disconnected."""
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        if not self._primary_connection_released.wait(timeout_seconds):
            raise RodexProtocolProxyError(
                "primary Codex protocol connection did not close before retry"
            )

    def _handle_connection(self, tui_connection: Any) -> None:
        if _connection_path(tui_connection) == TUI_NOTICE_CONNECTION_PATH:
            self._handle_tui_notice_connection(tui_connection)
            return
        is_primary_connection = _connection_path(
            tui_connection
        ) != CONTROL_CONNECTION_PATH and self._claim_primary_connection(tui_connection)
        try:
            with unix_connect(
                str(self._app_server_socket_path),
                uri="ws://localhost/rpc",
                compression=None,
                open_timeout=5,
                close_timeout=1,
                max_size=None,
            ) as app_server_connection:
                tui_to_server = Thread(
                    target=_forward_messages,
                    args=(tui_connection, app_server_connection),
                    name="rodex-codex-protocol-client-forwarder",
                    daemon=True,
                )
                tui_to_server.start()
                try:
                    for message in app_server_connection:
                        event = _json_object(message) if is_primary_connection else None
                        if is_primary_connection:
                            if not self._send_primary_tui_message(message):
                                break
                            self._observe_primary_thread(event)
                            self._tool_call_counter.observe_protocol_event(event)
                            if self._on_primary_server_message is not None:
                                self._on_primary_server_message(message, event)
                        else:
                            tui_connection.send(message)
                except (ConnectionClosed, OSError):
                    pass
                finally:
                    if is_primary_connection:
                        self._release_primary_connection(tui_connection)
                    tui_connection.close()
                    app_server_connection.close()
                    tui_to_server.join(timeout=2)
        except (ConnectionClosed, OSError):
            tui_connection.close()
        finally:
            if is_primary_connection:
                self._release_primary_connection(tui_connection)

    def _handle_tui_notice_connection(self, connection: Any) -> None:
        request_id: object = None
        delivered = False
        try:
            request = _json_object(connection.recv(timeout=1))
            if request is not None:
                request_id = request.get("id")
                params = request.get("params")
                message = params.get("message") if isinstance(params, dict) else None
                if (
                    request.get("method") == TUI_NOTICE_METHOD
                    and isinstance(message, str)
                    and message.strip()
                ):
                    delivered = self._send_primary_tui_message(
                        self._warning_notification(message)
                    )
            connection.send(
                json.dumps(
                    {"id": request_id, "result": {"delivered": delivered}},
                    separators=(",", ":"),
                )
            )
        except (ConnectionClosed, OSError, TimeoutError):
            return
        finally:
            connection.close()

    def _warning_notification(self, message: str) -> str:
        with self._connection_lock:
            thread_id = self._primary_thread_id
        return json.dumps(
            {
                "method": CODEX_APP_SERVER.warning_method,
                "params": {"threadId": thread_id, "message": message},
            },
            separators=(",", ":"),
        )

    def _send_primary_tui_message(self, message: str | bytes) -> bool:
        with self._primary_send_lock:
            with self._connection_lock:
                connection = self._primary_tui_connection
            if connection is None:
                return False
            try:
                connection.send(message)
            except (ConnectionClosed, OSError):
                return False
        return True

    def _observe_primary_thread(self, event: dict[str, Any] | None) -> None:
        if event is None or event.get("method") != CODEX_APP_SERVER.thread_started_method:
            return
        params = event.get("params")
        thread_id = _started_thread_id(params) if isinstance(params, dict) else None
        if thread_id is not None:
            with self._connection_lock:
                self._primary_thread_id = thread_id

    def _claim_primary_connection(self, tui_connection: Any) -> bool:
        with self._primary_lifecycle_lock, self._connection_lock:
            if self._primary_connection_claimed:
                return False
            self._primary_connection_claimed = True
            self._primary_tui_connection = tui_connection
            self._primary_thread_id = None
            self._primary_connection_released.clear()
            return True

    def _release_primary_connection(self, tui_connection: Any) -> None:
        with self._primary_lifecycle_lock:
            released = False
            with self._primary_send_lock, self._connection_lock:
                if self._primary_tui_connection is tui_connection:
                    self._primary_connection_claimed = False
                    self._primary_tui_connection = None
                    self._primary_thread_id = None
                    released = True
            if released and self._on_primary_disconnect is not None:
                with suppress(Exception):
                    self._on_primary_disconnect()
            if released:
                self._primary_connection_released.set()


def _forward_messages(source: Any, destination: Any) -> None:
    try:
        for message in source:
            destination.send(message)
    except (ConnectionClosed, OSError):
        pass
    finally:
        destination.close()


def _connection_path(connection: ServerConnection) -> str | None:
    request = connection.request
    return None if request is None else request.path


def _close_subscriber_queue(
    subscriber: queue.Queue[str | bytes | object],
) -> None:
    while True:
        try:
            subscriber.get_nowait()
        except queue.Empty:
            break
    try:
        subscriber.put_nowait(_EVENT_STREAM_CLOSED)
    except queue.Full:
        return


def _interrupt_subscriber_connection(connection: Any) -> None:
    """Interrupt a blocked writer without waiting for a WebSocket close handshake."""
    try:
        connection.close_socket()
    except Exception:
        # Reclaiming one failed subscriber must never reach the primary stream.
        return


def _tool_call_item_lifecycle(
    payload: dict[str, Any] | None,
) -> tuple[str, str] | None:
    if payload is None or payload.get("method") not in {
        "item/started",
        "item/completed",
    }:
        return None
    method = payload["method"]
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    if not isinstance(item, dict):
        return None
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        return None
    if method == "item/started" and item.get("type") not in TOOL_CALL_ITEM_TYPES:
        return None
    return method, item_id


def _update_active_turns(active_turns: dict[str, str], payload: dict[str, Any]) -> None:
    """Track only the live turn identity needed for safe external steering."""
    method = payload.get("method")
    params = payload.get("params")
    if not isinstance(params, dict):
        return
    thread_id = params.get("threadId")
    if not isinstance(thread_id, str) or not thread_id:
        return
    if method == CODEX_APP_SERVER.turn_started_method:
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if isinstance(turn_id, str) and turn_id:
            active_turns[thread_id] = turn_id
        return
    if method == CODEX_APP_SERVER.turn_completed_method:
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if active_turns.get(thread_id) == turn_id:
            active_turns.pop(thread_id, None)
        return
    if method == CODEX_APP_SERVER.thread_status_changed_method:
        status = params.get("status")
        if isinstance(status, dict) and status.get("type") != "active":
            active_turns.pop(thread_id, None)


def _json_object(message: str | bytes) -> dict[str, Any] | None:
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


def _started_thread_id(params: dict[str, Any]) -> str | None:
    thread = params.get("thread")
    if isinstance(thread, dict):
        thread_id = thread.get("id")
        if isinstance(thread_id, str) and thread_id:
            return thread_id
    return _event_thread_id(params)


def _started_thread_rollout_path(
    params: dict[str, Any],
    *,
    thread_id: str,
    codex_sessions_root: Path | None,
) -> Path | None:
    """Accept only the exact primary rollout path beneath the configured Codex root."""
    if codex_sessions_root is None:
        return None
    thread = params.get("thread")
    path_value = thread.get("path") if isinstance(thread, dict) else None
    if not isinstance(path_value, str) or not path_value:
        return None
    candidate = Path(path_value)
    if not candidate.is_absolute() or not candidate.name.endswith(f"-{thread_id}.jsonl"):
        return None
    if candidate.is_symlink():
        return None
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(codex_sessions_root):
        return None
    return resolved


def _update_known_threads(
    known_threads: dict[str, dict[str, object]],
    active_turns: dict[str, str],
    payload: dict[str, Any],
) -> None:
    method = payload.get("method")
    params = payload.get("params")
    if not isinstance(params, dict):
        return
    if method in {
        CODEX_APP_SERVER.turn_completed_method,
        CODEX_APP_SERVER.thread_status_changed_method,
    }:
        thread_id = _event_thread_id(params)
        if thread_id is not None and thread_id not in active_turns:
            known_threads.pop(thread_id, None)
        return
    if method == CODEX_APP_SERVER.turn_started_method:
        thread_id = _event_thread_id(params)
        if thread_id is not None:
            known_threads.setdefault(thread_id, {"id": thread_id})
        return
    if method != CODEX_APP_SERVER.thread_started_method:
        return
    thread = params.get("thread")
    if not isinstance(thread, dict):
        return
    thread_id = _started_thread_id(params)
    if thread_id is None:
        return
    remembered: dict[str, object] = {"id": thread_id}
    created_at = thread.get("createdAt")
    if isinstance(created_at, (str, int, float)) and not isinstance(created_at, bool):
        remembered["createdAt"] = created_at
    known_threads[thread_id] = remembered


def _event_thread_id(params: dict[str, Any]) -> str | None:
    thread_id = params.get("threadId")
    return thread_id if isinstance(thread_id, str) and thread_id else None


def _context_percent(params: dict[str, Any]) -> float | None:
    """Use the pinned analyzer's last-usage/context-window calculation."""
    token_usage = params.get("tokenUsage")
    if not isinstance(token_usage, dict):
        return None
    last_usage: dict[str, Any] | None = None
    for key in ("last", "lastTokenUsage", "last_token_usage"):
        candidate = token_usage.get(key)
        if isinstance(candidate, dict):
            last_usage = candidate
            break
    if last_usage is None:
        return None
    total_tokens = _finite_number(last_usage, "totalTokens", "total_tokens")
    context_window = _finite_number(
        token_usage,
        "modelContextWindow",
        "model_context_window",
    )
    if total_tokens is None or context_window is None:
        return None
    if total_tokens < 0 or context_window <= 0:
        return None
    return 100.0 * total_tokens / context_window


def _open_rollout_for_following(
    rollout_path: Path,
    *,
    allowed_root: Path | None,
) -> Any:
    """Open one owned regular rollout and bind its descriptor to the allowed root."""
    if allowed_root is None:
        raise AnalyticsSourceReadError("rollout following requires an allowed root")
    root = allowed_root.resolve(strict=True)
    descriptor = open_rollout_descriptor(rollout_path)
    try:
        descriptor_state = os.fstat(descriptor)
        path_state = os.stat(rollout_path, follow_symlinks=False)
        resolved_path = rollout_path.resolve(strict=True)
        if not resolved_path.is_relative_to(root):
            raise AnalyticsSourceReadError(
                "rollout source escapes the configured sessions root"
            )
        if not stat.S_ISREG(path_state.st_mode) or (
            descriptor_state.st_dev,
            descriptor_state.st_ino,
        ) != (path_state.st_dev, path_state.st_ino):
            raise AnalyticsSourceReadError(
                "rollout source identity changed while it was opened"
            )
        return os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        os.close(descriptor)
        raise


def _latest_rollout_context_from_open_file(rollout: Any) -> tuple[float | None, int]:
    """Read a bounded complete-line baseline and retain a trailing partial offset."""
    rollout.seek(0, 2)
    end_offset = rollout.tell()
    start_offset = max(0, end_offset - _ROLLOUT_CONTEXT_TAIL_BYTES)
    rollout.seek(start_offset)
    tail = rollout.read(end_offset - start_offset)
    content_offset = start_offset
    if start_offset:
        first_newline = tail.find(b"\n")
        if first_newline < 0:
            return None, end_offset
        content_offset += first_newline + 1
        tail = tail[first_newline + 1 :]
    if tail.endswith(b"\n"):
        complete_content = tail
        follow_offset = end_offset
    else:
        final_newline = tail.rfind(b"\n")
        if final_newline < 0:
            complete_content = b""
            follow_offset = content_offset
        else:
            complete_content = tail[: final_newline + 1]
            follow_offset = content_offset + final_newline + 1
    for line in reversed(complete_content.splitlines()):
        if not line or len(line) > _ROLLOUT_CONTEXT_LINE_BYTES:
            continue
        context_percent = _rollout_context_percent(line)
        if context_percent is not None:
            return context_percent, follow_offset
    return None, follow_offset


def _rollout_follow_checkpoint(rollout: Any) -> _RolloutFollowCheckpoint:
    state = os.fstat(rollout.fileno())
    cursor_offset = rollout.tell()
    return _RolloutFollowCheckpoint(
        source_device=state.st_dev,
        source_inode=state.st_ino,
        source_size_bytes=state.st_size,
        source_mtime_ns=state.st_mtime_ns,
        source_ctime_ns=state.st_ctime_ns,
        cursor_offset=cursor_offset,
        boundary_sha256=_rollout_boundary_sha256(rollout.fileno(), cursor_offset),
    )


def _advance_rollout_follow_checkpoint(
    rollout_path: Path,
    rollout: Any,
    checkpoint: _RolloutFollowCheckpoint,
) -> _RolloutFollowCheckpoint | None:
    """Advance only after the preceding trusted boundary still validates."""
    if _rollout_path_requires_reopen(rollout_path, rollout, checkpoint):
        return None
    state = os.fstat(rollout.fileno())
    if rollout.tell() == checkpoint.cursor_offset and (
        state.st_size,
        state.st_mtime_ns,
        state.st_ctime_ns,
    ) == (
        checkpoint.source_size_bytes,
        checkpoint.source_mtime_ns,
        checkpoint.source_ctime_ns,
    ):
        return checkpoint
    advanced = _rollout_follow_checkpoint(rollout)
    if _rollout_path_requires_reopen(rollout_path, rollout, checkpoint):
        return None
    return advanced


def _rollout_boundary_sha256(descriptor: int, cursor_offset: int) -> str:
    """Fingerprint bounded head and cursor-boundary bytes without moving the cursor."""
    head_size = min(cursor_offset, _ROLLOUT_CONTEXT_BOUNDARY_BYTES)
    boundary_start = max(head_size, cursor_offset - _ROLLOUT_CONTEXT_BOUNDARY_BYTES)
    head = os.pread(descriptor, head_size, 0)
    boundary = os.pread(descriptor, cursor_offset - boundary_start, boundary_start)
    digest = hashlib.sha256()
    digest.update(head)
    digest.update(boundary_start.to_bytes(8, "big"))
    digest.update(boundary)
    return digest.hexdigest()


def _rollout_path_requires_reopen(
    rollout_path: Path,
    rollout: Any,
    checkpoint: _RolloutFollowCheckpoint,
) -> bool:
    """Reject replacement, truncation, rewrite, or truncate-regrow at a boundary."""
    try:
        descriptor_state = os.fstat(rollout.fileno())
        path_state = os.stat(rollout_path, follow_symlinks=False)
    except OSError:
        return True
    if (
        not stat.S_ISREG(path_state.st_mode)
        or (
            descriptor_state.st_dev,
            descriptor_state.st_ino,
        )
        != (
            checkpoint.source_device,
            checkpoint.source_inode,
        )
        or (path_state.st_dev, path_state.st_ino)
        != (
            checkpoint.source_device,
            checkpoint.source_inode,
        )
    ):
        return True
    if (
        descriptor_state.st_size < checkpoint.cursor_offset
        or descriptor_state.st_size < checkpoint.source_size_bytes
    ):
        return True
    metadata_changed = (
        descriptor_state.st_size,
        descriptor_state.st_mtime_ns,
        descriptor_state.st_ctime_ns,
    ) != (
        checkpoint.source_size_bytes,
        checkpoint.source_mtime_ns,
        checkpoint.source_ctime_ns,
    )
    if not metadata_changed:
        return False
    if (
        _rollout_boundary_sha256(rollout.fileno(), checkpoint.cursor_offset)
        != checkpoint.boundary_sha256
    ):
        return True
    return descriptor_state.st_size == checkpoint.source_size_bytes


def _rollout_context_percent(line: bytes) -> float | None:
    try:
        record = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    last_usage = info.get("last_token_usage")
    if not isinstance(last_usage, dict):
        return None
    total_tokens = _finite_number(last_usage, "total_tokens", "totalTokens")
    context_window = _finite_number(
        info,
        "model_context_window",
        "modelContextWindow",
    )
    if total_tokens is None or context_window is None:
        return None
    if total_tokens < 0 or context_window <= 0:
        return None
    return 100.0 * total_tokens / context_window


def _finite_number(source: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = source.get(key)
        if (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
        ):
            return float(value)
    return None
