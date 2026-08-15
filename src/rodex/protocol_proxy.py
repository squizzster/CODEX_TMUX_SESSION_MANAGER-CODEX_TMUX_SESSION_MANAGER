"""Transparent Codex WebSocket proxy with small protocol-derived signals."""

from __future__ import annotations

import json
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


class RodexProtocolProxyError(RuntimeError):
    """The local Codex protocol proxy could not start or stop cleanly."""


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
    ) -> None:
        self._proxy_socket_path = proxy_socket_path
        self._app_server_socket_path = app_server_socket_path
        self._tool_call_counter = tool_call_counter
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
                        self._tool_call_counter.observe_server_message(message)
                except (ConnectionClosed, OSError):
                    pass
                finally:
                    tui_connection.close()
                    app_server_connection.close()
                    tui_to_server.join(timeout=2)
        except (ConnectionClosed, OSError):
            tui_connection.close()


def _forward_messages(source: Any, destination: Any) -> None:
    try:
        for message in source:
            destination.send(message)
    except (ConnectionClosed, OSError):
        pass
    finally:
        destination.close()


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
