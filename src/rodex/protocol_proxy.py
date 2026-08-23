"""Transparent Codex WebSocket proxy with small protocol-derived signals."""

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
ProtocolEventCallback = Callable[[str | bytes], None]
ContextStatusCallback = Callable[[str], None]
_EVENT_STREAM_CLOSED: Final = object()
EVENT_STREAM_READY_METHOD: Final = "rodex/event-stream/ready"
CONTROL_CONNECTION_PATH: Final = "/rodex-control"
EVENT_STREAM_READY_MESSAGE: Final = json.dumps(
    {"method": EVENT_STREAM_READY_METHOD, "params": {"activeTurns": {}}},
    separators=(",", ":"),
)


class RodexProtocolProxyError(RuntimeError):
    """The local Codex protocol proxy could not start or stop cleanly."""


class CodexProtocolEventTap:
    """Fan out primary TUI protocol events without blocking its live stream."""

    def __init__(self, event_socket_path: Path, *, queue_size: int = 1024) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._event_socket_path = event_socket_path
        self._queue_size = queue_size
        self._subscribers: set[queue.Queue[str | bytes | object]] = set()
        self._subscribers_lock = Lock()
        self._active_turns: dict[str, str] = {}
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
        with self._subscribers_lock:
            _update_active_turns(self._active_turns, message)
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
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
        with self._subscribers_lock:
            self._subscribers.add(subscriber)
            ready_message = json.dumps(
                {
                    "method": EVENT_STREAM_READY_METHOD,
                    "params": {"activeTurns": dict(self._active_turns)},
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
                self._subscribers.discard(subscriber)
            connection.close()

    def _disconnect_slow_subscriber(
        self, subscriber: queue.Queue[str | bytes | object]
    ) -> None:
        with self._subscribers_lock:
            if subscriber not in self._subscribers:
                return
            self._subscribers.remove(subscriber)
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
        item_id = _started_tool_call_item_id(message)
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
    ) -> None:
        if animation_interval_seconds <= 0 or not math.isfinite(animation_interval_seconds):
            raise ValueError("animation interval must be finite and positive")
        self._on_status_changed = on_status_changed
        self._animation_interval_seconds = animation_interval_seconds
        self._primary_thread_id: str | None = None
        self._latest_context_status = context_status_segment(None)
        self._active_compaction_item_ids: set[str] = set()
        self._animation_generation = 0
        self._animation_stop: Event | None = None
        self._animation_threads: list[Thread] = []
        self._closed = False
        self._lock = Lock()

    def observe_server_message(self, message: str | bytes) -> None:
        """Consume one primary app-server-to-TUI protocol message."""
        payload = _json_object(message)
        if payload is None:
            return
        method = payload.get("method")
        params = payload.get("params")
        if not isinstance(params, dict):
            return
        if method == "thread/started":
            thread_id = _started_thread_id(params)
            if thread_id is not None:
                with self._lock:
                    self._accept_thread_locked(thread_id)
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
        for animation_thread in animation_threads:
            animation_thread.join(timeout=1)

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


class CodexProtocolProxy:
    """Forward one local WebSocket endpoint to the private Codex app-server."""

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
        self._primary_connection_claimed = False
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
        is_primary_connection = (
            _connection_path(tui_connection) != CONTROL_CONNECTION_PATH
            and self._claim_primary_connection()
        )
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
                        tui_connection.send(message)
                        if is_primary_connection:
                            self._tool_call_counter.observe_server_message(message)
                            if self._on_primary_server_message is not None:
                                self._on_primary_server_message(message)
                except (ConnectionClosed, OSError):
                    pass
                finally:
                    tui_connection.close()
                    app_server_connection.close()
                    tui_to_server.join(timeout=2)
        except (ConnectionClosed, OSError):
            tui_connection.close()
        finally:
            if is_primary_connection:
                self._release_primary_connection()

    def _claim_primary_connection(self) -> bool:
        with self._connection_lock:
            if self._primary_connection_claimed:
                return False
            self._primary_connection_claimed = True
            self._primary_connection_released.clear()
            return True

    def _release_primary_connection(self) -> None:
        with self._connection_lock:
            self._primary_connection_claimed = False
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


def _started_tool_call_item_id(message: str | bytes) -> str | None:
    if isinstance(message, bytes):
        try:
            message = message.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("method") != "item/started":
        return None
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    if not isinstance(item, dict) or item.get("type") not in TOOL_CALL_ITEM_TYPES:
        return None
    item_id = item.get("id")
    return item_id if isinstance(item_id, str) and item_id else None


def _update_active_turns(active_turns: dict[str, str], message: str | bytes) -> None:
    """Track only the live turn identity needed for safe external steering."""
    payload = _json_object(message)
    if payload is None:
        return
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
