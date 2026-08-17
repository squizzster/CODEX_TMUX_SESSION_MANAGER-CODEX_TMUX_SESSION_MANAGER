"""Transparent Codex WebSocket proxy with small protocol-derived signals."""

from __future__ import annotations

import json
import queue
import subprocess
from collections.abc import Callable
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Final

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

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
_EVENT_STREAM_CLOSED: Final = object()
EVENT_STREAM_READY_METHOD: Final = "rodex/event-stream/ready"
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
        """Bind the runtime-only event socket and accept tail subscribers."""
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
        if not tmux_pane_target.strip():
            raise ValueError("tmux_pane_target must be non-empty")
        self._tmux_binary = tmux_binary
        self._tmux_server_socket_path = tmux_server_socket_path
        self._tmux_pane_target = tmux_pane_target
        self._run = runner

    def update(self, count: int) -> None:
        """Set the stable tmux user option consumed by the status format."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("tool-call count must be a non-negative integer")
        self._run(
            [
                self._tmux_binary,
                "-S",
                str(self._tmux_server_socket_path),
                "set-option",
                "-t",
                self._tmux_pane_target,
                "@rodex_tool_calls",
                str(count),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


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

    def _handle_connection(self, tui_connection: Any) -> None:
        is_primary_connection = self._claim_primary_connection()
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

    def _claim_primary_connection(self) -> bool:
        with self._connection_lock:
            if self._primary_connection_claimed:
                return False
            self._primary_connection_claimed = True
            return True


def _forward_messages(source: Any, destination: Any) -> None:
    try:
        for message in source:
            destination.send(message)
    except (ConnectionClosed, OSError):
        pass
    finally:
        destination.close()


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
    if method == "turn/started":
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if isinstance(turn_id, str) and turn_id:
            active_turns[thread_id] = turn_id
        return
    if method == "turn/completed":
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if active_turns.get(thread_id) == turn_id:
            active_turns.pop(thread_id, None)
        return
    if method == "thread/status/changed":
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
