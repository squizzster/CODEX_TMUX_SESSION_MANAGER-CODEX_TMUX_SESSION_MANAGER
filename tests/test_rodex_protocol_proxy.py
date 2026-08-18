from __future__ import annotations

import json
import subprocess
from pathlib import Path
from threading import Event, Thread

import pytest
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

from rodex.protocol_proxy import (
    CONTROL_CONNECTION_PATH,
    EVENT_STREAM_READY_MESSAGE,
    TOOL_CALL_ITEM_TYPES,
    CodexContextStatusObserver,
    CodexProtocolEventTap,
    CodexProtocolProxy,
    TmuxContextStatus,
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


def token_usage_updated(
    total_tokens: int,
    *,
    thread_id: str = "thread-1",
    context_window: int = 258_400,
) -> str:
    return json.dumps(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": thread_id,
                "tokenUsage": {
                    "last": {"totalTokens": total_tokens},
                    "modelContextWindow": context_window,
                },
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


def test_tmux_context_status_uses_the_same_stable_pane_boundary(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0)

    status = TmuxContextStatus(
        "/usr/bin/tmux",
        tmp_path / "tmux.sock",
        "%7",
        runner=runner,
    )

    status.update("#[fg=red]| Context: 75% | ")

    assert calls[0][0] == [
        "/usr/bin/tmux",
        "-S",
        str(tmp_path / "tmux.sock"),
        "set-option",
        "-t",
        "%7",
        "@rodex_context_status",
        "#[fg=red]| Context: 75% | ",
    ]
    assert calls[0][1]["check"] is True


def test_context_observer_projects_last_usage_for_only_the_primary_thread() -> None:
    observed: list[str] = []
    observer = CodexContextStatusObserver(observed.append)
    observer.observe_server_message(
        json.dumps(
            {
                "method": "thread/started",
                "params": {"thread": {"id": "thread-1"}},
            }
        )
    )

    observer.observe_server_message(token_usage_updated(206_720, thread_id="thread-2"))
    observer.observe_server_message(token_usage_updated(180_880))
    observer.observe_server_message("not-json")
    observer.close()

    assert len(observed) == 1
    assert "colour208" in observed[0]
    assert "Context: 70% |" in observed[0]


def test_context_observer_animates_compaction_then_restores_fresh_usage() -> None:
    observed: list[str] = []
    two_frames_seen = Event()

    def record_status(rendered_status: str) -> None:
        observed.append(rendered_status)
        if len({status for status in observed if "COMPACTING" in status}) >= 2:
            two_frames_seen.set()

    observer = CodexContextStatusObserver(
        record_status,
        animation_interval_seconds=0.005,
    )
    observer.observe_server_message(token_usage_updated(193_800))
    observer.observe_server_message(item_started("contextCompaction", "compact-1"))
    try:
        assert two_frames_seen.wait(1)
        observer.observe_server_message(token_usage_updated(25_840))
        assert "COMPACTING" in observed[-1]
        observer.observe_server_message(
            item_started("contextCompaction", "compact-1").replace(
                '"item/started"',
                '"item/completed"',
            )
        )
        assert "Context: 10% |" in observed[-1]
    finally:
        observer.close()

    assert any("COMPACTING" in status for status in observed)


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
    observed_events: list[str | bytes] = []
    proxy = CodexProtocolProxy(
        proxy_socket,
        app_socket,
        ToolCallCounter(counts.append),
        observed_events.append,
    )
    try:
        proxy.start()
        assert proxy_socket.stat().st_mode & 0o777 == 0o600
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
    assert observed_events == [server_message]
    assert not proxy_socket.exists()


@pytest.mark.evolutionary_regression
def test_proxy_hands_primary_event_ownership_to_a_reconnecting_tui(tmp_path: Path) -> None:
    """Current evidence: an exact-resume retry must become the event-producing client."""
    app_socket = tmp_path / "app.sock"
    proxy_socket = tmp_path / "proxy.sock"

    def app_server(connection: object) -> None:
        request = json.loads(connection.recv())  # type: ignore[attr-defined]
        connection.send(  # type: ignore[attr-defined]
            item_started("commandExecution", request["method"])
        )

    upstream = unix_serve(app_server, path=str(app_socket), compression=None)
    upstream_thread = Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    observed_events: list[str | bytes] = []
    proxy = CodexProtocolProxy(
        proxy_socket,
        app_socket,
        ToolCallCounter(lambda _count: None),
        observed_events.append,
    )
    try:
        proxy.start()
        with unix_connect(
            str(proxy_socket), uri="ws://localhost/rpc", compression=None
        ) as first_tui:
            first_tui.send(json.dumps({"method": "first"}))
            first_tui.recv(timeout=1)
        proxy.wait_for_primary_connection_release(1)

        with unix_connect(
            str(proxy_socket),
            uri=f"ws://localhost{CONTROL_CONNECTION_PATH}",
            compression=None,
        ) as machine_control:
            machine_control.send(json.dumps({"method": "control"}))
            machine_control.recv(timeout=1)
            with unix_connect(
                str(proxy_socket), uri="ws://localhost/rpc", compression=None
            ) as retry_tui:
                retry_tui.send(json.dumps({"method": "retry"}))
                retry_tui.recv(timeout=1)
            proxy.wait_for_primary_connection_release(1)
    finally:
        proxy.close()
        upstream.shutdown(close_connections=True)
        upstream_thread.join(timeout=5)

    assert [json.loads(message)["params"]["item"]["id"] for message in observed_events] == [
        "first",
        "retry",
    ]


def test_event_tap_streams_runtime_events_and_removes_its_socket(tmp_path: Path) -> None:
    event_socket = tmp_path / "events.sock"
    tap = CodexProtocolEventTap(event_socket)
    message = item_started("collabAgentToolCall", "collab-1")

    try:
        tap.start()
        assert event_socket.stat().st_mode & 0o777 == 0o600
        with unix_connect(
            str(event_socket), uri="ws://localhost/events", compression=None
        ) as subscriber:
            assert subscriber.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE
            tap.publish(message)
            assert subscriber.recv(timeout=1) == message
    finally:
        tap.close()

    assert not event_socket.exists()


def test_event_tap_ready_signal_reports_the_current_active_turn(tmp_path: Path) -> None:
    event_socket = tmp_path / "events.sock"
    tap = CodexProtocolEventTap(event_socket)
    started = json.dumps(
        {
            "method": "turn/started",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "inProgress"},
            },
        }
    )
    completed = json.dumps(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }
    )

    try:
        tap.start()
        tap.publish(started)
        with unix_connect(
            str(event_socket), uri="ws://localhost/events", compression=None
        ) as subscriber:
            assert json.loads(subscriber.recv(timeout=1)) == {
                "method": "rodex/event-stream/ready",
                "params": {"activeTurns": {"thread-1": "turn-1"}},
            }
        tap.publish(completed)
        with unix_connect(
            str(event_socket), uri="ws://localhost/events", compression=None
        ) as subscriber:
            assert subscriber.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE
    finally:
        tap.close()
