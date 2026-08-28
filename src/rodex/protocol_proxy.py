"""Codex WebSocket proxy with Rodex-only TUI notices and derived signals."""

from __future__ import annotations

import json
import math
import queue
import subprocess
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Final

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

from .app_server_contract import CODEX_APP_SERVER
from .status_bar import (
    CONTEXT_COMPACTION_FRAME_INTERVAL_SECONDS,
    RODEX_CONTEXT_STATUS_OPTION,
    RODEX_TOOL_CALL_STATUS_OPTION,
    compacting_status_segment,
    context_status_segment,
)
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
ROLLOUT_CONTEXT_POLL_INTERVAL_SECONDS: Final = 0.25
_ROLLOUT_CONTEXT_TAIL_BYTES: Final = 8 * 1024 * 1024
_ROLLOUT_CONTEXT_LINE_BYTES: Final = 256 * 1024
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
        self._subscribers: dict[queue.Queue[str | bytes | object], bool] = {}
        self._subscribers_lock = Lock()
        self._active_turns: dict[str, str] = {}
        self._known_threads: dict[str, dict[str, object]] = {}
        self._server: Any | None = None
        self._server_thread: Thread | None = None

    def start(self) -> None:
        """Bind the runtime-only event socket and accept event subscribers."""
        if self._server is not None:
            raise RodexProtocolProxyError("Codex protocol event tap is already running")
        self._event_socket_path.unlink(missing_ok=True)
        try:
            self._server = unix_serve(
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
        self._server_thread = Thread(
            target=self._server.serve_forever,
            name="rodex-codex-protocol-event-tap",
            daemon=True,
        )
        self._server_thread.start()

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
            if event is not None:
                _update_active_turns(self._active_turns, event)
                _update_known_threads(self._known_threads, event)
            subscribers = tuple(self._subscribers.items())
        is_analytics_event = (
            event is not None and event.get("method") in ANALYTICS_WAKE_EVENT_METHODS
        )
        for subscriber, analytics_only in subscribers:
            if analytics_only and not is_analytics_event:
                continue
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                self._disconnect_slow_subscriber(subscriber)

    def close(self) -> None:
        """Close subscribers, stop the event server, and remove its socket."""
        server = self._server
        server_thread = self._server_thread
        self._server = None
        self._server_thread = None
        with self._subscribers_lock:
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            _close_subscriber_queue(subscriber)
        if server is not None:
            server.shutdown(close_connections=True)
        if server_thread is not None:
            server_thread.join(timeout=5)
            if server_thread.is_alive():
                raise RodexProtocolProxyError("Codex protocol event tap did not stop")
        self._event_socket_path.unlink(missing_ok=True)

    def _handle_subscriber(self, connection: Any) -> None:
        subscriber: queue.Queue[str | bytes | object] = queue.Queue(self._queue_size)
        semantic_only = _connection_path(connection) in {
            ANALYTICS_EVENT_STREAM_PATH,
            AGENT_OBSERVER_EVENT_STREAM_PATH,
        }
        with self._subscribers_lock:
            self._subscribers[subscriber] = semantic_only
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
            connection.close()

    def _disconnect_slow_subscriber(
        self, subscriber: queue.Queue[str | bytes | object]
    ) -> None:
        with self._subscribers_lock:
            if subscriber not in self._subscribers:
                return
            self._subscribers.pop(subscriber)
        _close_subscriber_queue(subscriber)


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
        item_id = _started_tool_call_item_id(event)
        if item_id is None:
            return
        with self._lock:
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
        tmux_server_socket_path: Path,
        tmux_pane_target: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    ) -> None:
        self._option = TmuxStatusOption(
            tmux_binary,
            tmux_server_socket_path,
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
        tmux_server_socket_path: Path,
        tmux_pane_target: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    ) -> None:
        self._option = TmuxStatusOption(
            tmux_binary,
            tmux_server_socket_path,
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
        codex_sessions_root: Path | None = None,
        rollout_poll_interval_seconds: float = ROLLOUT_CONTEXT_POLL_INTERVAL_SECONDS,
    ) -> None:
        if animation_interval_seconds <= 0 or not math.isfinite(animation_interval_seconds):
            raise ValueError("animation interval must be finite and positive")
        if rollout_poll_interval_seconds <= 0 or not math.isfinite(
            rollout_poll_interval_seconds
        ):
            raise ValueError("rollout poll interval must be finite and positive")
        self._on_status_changed = on_status_changed
        self._animation_interval_seconds = animation_interval_seconds
        self._codex_sessions_root = (
            None
            if codex_sessions_root is None
            else Path(codex_sessions_root).resolve(strict=False)
        )
        self._rollout_poll_interval_seconds = rollout_poll_interval_seconds
        self._primary_thread_id: str | None = None
        self._latest_context_status = context_status_segment(None)
        self._active_compaction_item_ids: set[str] = set()
        self._animation_generation = 0
        self._animation_stop: Event | None = None
        self._animation_threads: list[Thread] = []
        self._rollout_stop: Event | None = None
        self._rollout_thread: Thread | None = None
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
            animation_thread: Thread | None = None
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
                    animation_thread = self._new_animation_thread_locked()
            if animation_thread is not None:
                animation_thread.start()
            return
        with self._lock:
            if self._closed or not self._accept_thread_locked(thread_id):
                return
            if item_id not in self._active_compaction_item_ids:
                return
            self._active_compaction_item_ids.remove(item_id)
            if self._active_compaction_item_ids:
                return
            if self._animation_stop is not None:
                self._animation_stop.set()
                self._animation_stop = None
            self._animation_generation += 1
            self._publish_status_locked(self._latest_context_status)

    def observe_rollout_context_percent(
        self,
        thread_id: str,
        context_percent: float,
    ) -> None:
        """Accept one authenticated primary-rollout context snapshot."""
        rendered_status = context_status_segment(context_percent)
        with self._lock:
            if self._closed or not self._accept_thread_locked(thread_id):
                return
            self._latest_context_status = rendered_status
            if not self._active_compaction_item_ids:
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
        for animation_thread in animation_threads:
            animation_thread.join(timeout=1)
        if rollout_thread is not None:
            rollout_thread.join(timeout=1)

    def _accept_thread_locked(self, thread_id: str) -> bool:
        if self._primary_thread_id is None:
            self._primary_thread_id = thread_id
        return self._primary_thread_id == thread_id

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
        while not stop.is_set():
            with self._lock:
                if self._closed or generation != self._animation_generation:
                    return
                self._publish_status_locked(compacting_status_segment(frame_index))
            frame_index += 1
            stop.wait(self._animation_interval_seconds)

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
        while not stop.is_set():
            try:
                initial_percent, initial_offset = _latest_rollout_context(rollout_path)
                if initial_percent is not None:
                    self.observe_rollout_context_percent(thread_id, initial_percent)
                with rollout_path.open("rb") as rollout:
                    rollout.seek(initial_offset)
                    discarding_long_line = False
                    while not stop.is_set():
                        line_start = rollout.tell()
                        line = rollout.readline(_ROLLOUT_CONTEXT_LINE_BYTES + 1)
                        if not line:
                            stop.wait(self._rollout_poll_interval_seconds)
                            continue
                        if discarding_long_line:
                            if line.endswith(b"\n"):
                                discarding_long_line = False
                            continue
                        if line.endswith(b"\n"):
                            context_percent = _rollout_context_percent(line)
                            if context_percent is not None:
                                self.observe_rollout_context_percent(
                                    thread_id,
                                    context_percent,
                                )
                            continue
                        if len(line) > _ROLLOUT_CONTEXT_LINE_BYTES:
                            discarding_long_line = True
                            continue
                        # The writer has not committed the newline yet. Re-read this
                        # bounded partial record after the next append.
                        rollout.seek(line_start)
                        stop.wait(self._rollout_poll_interval_seconds)
            except OSError:
                stop.wait(self._rollout_poll_interval_seconds)


class CodexProtocolProxy:
    """Forward App Server traffic and accept isolated Rodex TUI notices."""

    def __init__(
        self,
        proxy_socket_path: Path,
        app_server_socket_path: Path,
        tool_call_counter: ToolCallCounter,
        on_primary_server_message: ProtocolEventCallback | None = None,
    ) -> None:
        self._proxy_socket_path = proxy_socket_path
        self._app_server_socket_path = app_server_socket_path
        self._tool_call_counter = tool_call_counter
        self._on_primary_server_message = on_primary_server_message
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
        with self._connection_lock:
            if self._primary_connection_claimed:
                return False
            self._primary_connection_claimed = True
            self._primary_tui_connection = tui_connection
            self._primary_thread_id = None
            self._primary_connection_released.clear()
            return True

    def _release_primary_connection(self, tui_connection: Any) -> None:
        with self._primary_send_lock, self._connection_lock:
            if self._primary_tui_connection is tui_connection:
                self._primary_connection_claimed = False
                self._primary_tui_connection = None
                self._primary_thread_id = None
                self._primary_connection_released.set()


def _forward_messages(source: Any, destination: Any) -> None:
    try:
        for message in source:
            destination.send(message)
    except (ConnectionClosed, OSError):
        pass
    finally:
        destination.close()


def _connection_path(connection: Any) -> str | None:
    request = getattr(connection, "request", None)
    path = getattr(request, "path", None)
    return path if isinstance(path, str) else None


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


def _started_tool_call_item_id(payload: dict[str, Any] | None) -> str | None:
    if payload is None or payload.get("method") != "item/started":
        return None
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    if not isinstance(item, dict) or item.get("type") not in TOOL_CALL_ITEM_TYPES:
        return None
    item_id = item.get("id")
    return item_id if isinstance(item_id, str) and item_id else None


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
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(codex_sessions_root):
        return None
    return resolved


def _update_known_threads(
    known_threads: dict[str, dict[str, object]], payload: dict[str, Any]
) -> None:
    if payload.get("method") != CODEX_APP_SERVER.thread_started_method:
        return
    params = payload.get("params")
    if not isinstance(params, dict):
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
    """Use the analyzer-compatible last-usage/context-window calculation."""
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


def _latest_rollout_context(rollout_path: Path) -> tuple[float | None, int]:
    """Read only a bounded tail and return its newest complete token snapshot."""
    with rollout_path.open("rb") as rollout:
        rollout.seek(0, 2)
        end_offset = rollout.tell()
        start_offset = max(0, end_offset - _ROLLOUT_CONTEXT_TAIL_BYTES)
        rollout.seek(start_offset)
        tail = rollout.read(end_offset - start_offset)
    lines = tail.split(b"\n")
    if start_offset:
        lines = lines[1:]
    for line in reversed(lines):
        if not line or len(line) > _ROLLOUT_CONTEXT_LINE_BYTES:
            continue
        context_percent = _rollout_context_percent(line)
        if context_percent is not None:
            return context_percent, end_offset
    return None, end_offset


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
