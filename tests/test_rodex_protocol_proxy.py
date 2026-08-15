from __future__ import annotations

import json
import subprocess
from pathlib import Path
from threading import Thread

import pytest
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

from rodex.protocol_proxy import (
    TOOL_CALL_ITEM_TYPES,
    CodexProtocolProxy,
    TmuxToolCallStatus,
    ToolCallCounter,
)


def item_started(item_type: str, item_id: str = "item-1") -> str:
    return json.dumps(
        {
            "method": "item/started",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": item_type, "id": item_id},
            },
        }
    )


@pytest.mark.parametrize("item_type", sorted(TOOL_CALL_ITEM_TYPES))
def test_counter_counts_each_protocol_tool_item_type(item_type: str) -> None:
    observed: list[int] = []
    counter = ToolCallCounter(observed.append)

    counter.observe_server_message(item_started(item_type))

    assert counter.count == 1
    assert observed == [1]


def test_counter_ignores_non_tools_malformed_messages_and_duplicate_items() -> None:
    observed: list[int] = []
    counter = ToolCallCounter(observed.append)

    counter.observe_server_message("not-json")
    counter.observe_server_message(item_started("agentMessage"))
    counter.observe_server_message(item_started("commandExecution", "command-1"))
    counter.observe_server_message(item_started("commandExecution", "command-1"))
    counter.observe_server_message(json.dumps({"method": "item/completed", "params": {}}))

    assert counter.count == 1
    assert observed == [1]


def test_tmux_status_uses_the_stable_pane_target_across_session_renames(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0)

    status = TmuxToolCallStatus(
        "/usr/bin/tmux",
        tmp_path / "tmux.sock",
        "%7",
        runner=runner,
    )

    status.update(3)

    assert calls[0][0] == [
        "/usr/bin/tmux",
        "-S",
        str(tmp_path / "tmux.sock"),
        "set-option",
        "-t",
        "%7",
        "@rodex_tool_calls",
        "3",
    ]
    assert calls[0][1]["check"] is True


def test_proxy_forwards_both_directions_and_counts_server_tool_items(
    tmp_path: Path,
) -> None:
    app_socket = tmp_path / "app.sock"
    proxy_socket = tmp_path / "proxy.sock"
    client_message = json.dumps({"method": "thread/read", "id": 7, "params": {}})
    server_message = item_started("commandExecution", "command-7")
    received_by_server: list[str | bytes] = []

    def app_server(connection: object) -> None:
        message = connection.recv()  # type: ignore[attr-defined]
        received_by_server.append(message)
        connection.send(server_message)  # type: ignore[attr-defined]

    upstream = unix_serve(app_server, path=str(app_socket), compression=None)
    upstream_thread = Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    counts: list[int] = []
    proxy = CodexProtocolProxy(
        proxy_socket,
        app_socket,
        ToolCallCounter(counts.append),
    )
    try:
        proxy.start()
        with unix_connect(
            str(proxy_socket), uri="ws://localhost/rpc", compression=None
        ) as tui:
            tui.send(client_message)
            assert tui.recv() == server_message
    finally:
        proxy.close()
        upstream.shutdown(close_connections=True)
        upstream_thread.join(timeout=5)

    assert received_by_server == [client_message]
    assert counts == [1]
    assert not proxy_socket.exists()
