from __future__ import annotations

import json
import subprocess
from pathlib import Path
from threading import Event, Thread

import pytest
from websockets.sync.client import unix_connect
from websockets.sync.server import unix_serve

from rodex.protocol_proxy import (
    AGENT_OBSERVER_EVENT_STREAM_PATH,
    ANALYTICS_EVENT_STREAM_PATH,
    CONTROL_CONNECTION_PATH,
    EVENT_STREAM_READY_MESSAGE,
    TOOL_CALL_ITEM_TYPES,
    TUI_NOTICE_CONNECTION_PATH,
    TUI_NOTICE_METHOD,
    CodexContextStatusObserver,
    CodexProtocolEventTap,
    CodexProtocolProxy,
    TmuxContextStatus,
    TmuxToolCallStatus,
    ToolCallCounter,
    publish_tui_notice,
)
from rodex.status_bar import RODEX_STATUS_COLOURS


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


def rollout_token_count(total_tokens: int, *, context_window: int = 258_400) -> str:
    return json.dumps(
        {
            "timestamp": "2026-08-28T03:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": total_tokens},
                    "model_context_window": context_window,
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
    assert RODEX_STATUS_COLOURS.context_warning in observed[0]
    assert "Context: 70% |" in observed[0]


def test_context_observer_follows_primary_rollout_during_a_live_turn(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    rollout_path = sessions_root / "2026/08/28/rollout-2026-08-28T03-00-00-thread-1.jsonl"
    rollout_path.parent.mkdir(parents=True)
    rollout_path.write_text(rollout_token_count(25_840) + "\n", encoding="utf-8")
    observed: list[str] = []
    restored = Event()
    advanced = Event()

    def record_status(rendered_status: str) -> None:
        observed.append(rendered_status)
        if "Context: 10% |" in rendered_status:
            restored.set()
        if "Context: 70% |" in rendered_status:
            advanced.set()

    observer = CodexContextStatusObserver(
        record_status,
        codex_sessions_root=sessions_root,
        rollout_poll_interval_seconds=0.005,
    )
    observer.observe_protocol_event(
        {
            "method": "thread/started",
            "params": {
                "thread": {"id": "thread-1", "path": str(rollout_path)},
            },
        }
    )
    try:
        assert restored.wait(1)
        with rollout_path.open("a", encoding="utf-8") as rollout:
            rollout.write('{"type":"response_item","payload":{}}\n')
            rollout.write(rollout_token_count(180_880) + "\n")
        assert advanced.wait(1)
    finally:
        observer.close()

    assert any("Context: 10% |" in status for status in observed)
    assert any("Context: 70% |" in status for status in observed)


def test_context_observer_rejects_a_rollout_outside_the_codex_sessions_root(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    rollout_path = tmp_path / "rollout-2026-08-28T03-00-00-thread-1.jsonl"
    rollout_path.write_text(rollout_token_count(180_880) + "\n", encoding="utf-8")
    observed: list[str] = []
    observer = CodexContextStatusObserver(
        observed.append,
        codex_sessions_root=sessions_root,
        rollout_poll_interval_seconds=0.005,
    )

    observer.observe_protocol_event(
        {
            "method": "thread/started",
            "params": {
                "thread": {"id": "thread-1", "path": str(rollout_path)},
            },
        }
    )
    observer.close()

    assert observed == []


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
        observer.observe_rollout_context_percent("thread-1", 10.0)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rodex import protocol_proxy as proxy_module

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
    decode_calls = 0
    original_decode = proxy_module._json_object

    def count_decode(message: str | bytes) -> dict[str, object] | None:
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(message)

    monkeypatch.setattr(proxy_module, "_json_object", count_decode)
    proxy = CodexProtocolProxy(
        proxy_socket,
        app_socket,
        ToolCallCounter(counts.append),
        lambda message, _event: observed_events.append(message),
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
    assert decode_calls == 1
    assert not proxy_socket.exists()


def test_proxy_delivers_rodex_notice_to_tui_without_forwarding_it_upstream(
    tmp_path: Path,
) -> None:
    app_socket = tmp_path / "app.sock"
    proxy_socket = tmp_path / "proxy.sock"
    release_upstream = Event()
    received_by_server: list[str | bytes] = []
    thread_started = json.dumps(
        {
            "method": "thread/started",
            "params": {"thread": {"id": "thread-1"}},
        }
    )

    def app_server(connection: object) -> None:
        received_by_server.append(connection.recv())  # type: ignore[attr-defined]
        connection.send(thread_started)  # type: ignore[attr-defined]
        release_upstream.wait(2)

    upstream = unix_serve(app_server, path=str(app_socket), compression=None)
    upstream_thread = Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    observed_events: list[str | bytes] = []
    proxy = CodexProtocolProxy(
        proxy_socket,
        app_socket,
        ToolCallCounter(lambda _count: None),
        lambda message, _event: observed_events.append(message),
    )
    client_message = json.dumps({"method": "initialize", "id": 1, "params": {}})
    try:
        proxy.start()
        with unix_connect(
            str(proxy_socket), uri="ws://localhost/rpc", compression=None
        ) as tui:
            tui.send(client_message)
            assert tui.recv(timeout=1) == thread_started

            assert publish_tui_notice(proxy_socket, "Rodex: Codex update available")
            assert json.loads(tui.recv(timeout=1)) == {
                "method": "warning",
                "params": {
                    "threadId": "thread-1",
                    "message": "Rodex: Codex update available",
                },
            }
    finally:
        release_upstream.set()
        proxy.close()
        upstream.shutdown(close_connections=True)
        upstream_thread.join(timeout=5)

    assert received_by_server == [client_message]
    assert observed_events == [thread_started]


def test_tui_notice_reports_undelivered_without_a_primary_tui(tmp_path: Path) -> None:
    app_socket = tmp_path / "app.sock"
    proxy_socket = tmp_path / "proxy.sock"
    upstream = unix_serve(lambda _connection: None, path=str(app_socket), compression=None)
    upstream_thread = Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    proxy = CodexProtocolProxy(
        proxy_socket,
        app_socket,
        ToolCallCounter(lambda _count: None),
    )
    try:
        proxy.start()
        assert not publish_tui_notice(proxy_socket, "Rodex: notice")
    finally:
        proxy.close()
        upstream.shutdown(close_connections=True)
        upstream_thread.join(timeout=5)


def test_tui_notice_client_uses_the_rodex_only_proxy_path(tmp_path: Path) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_error: object) -> None:
            return None

        def send(self, message: str) -> None:
            assert json.loads(message) == {
                "method": TUI_NOTICE_METHOD,
                "id": 0,
                "params": {"message": "Rodex: notice"},
            }

        def recv(self, timeout: float) -> str:
            assert timeout == 1
            return '{"id":0,"result":{"delivered":true}}'

    def connector(*args: object, **kwargs: object) -> Connection:
        calls.append((args, kwargs))
        return Connection()

    assert publish_tui_notice(
        tmp_path / "proxy.sock",
        "Rodex: notice",
        connector=connector,
    )
    assert calls[0][0] == (str(tmp_path / "proxy.sock"),)
    assert calls[0][1]["uri"] == f"ws://localhost{TUI_NOTICE_CONNECTION_PATH}"


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
        lambda message, _event: observed_events.append(message),
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


def test_event_tap_sends_only_semantic_wake_events_to_internal_workers(
    tmp_path: Path,
) -> None:
    event_socket = tmp_path / "events.sock"
    tap = CodexProtocolEventTap(event_socket)
    thread_started = json.dumps(
        {"method": "thread/started", "params": {"thread": {"id": "thread-1"}}}
    )
    token_delta = json.dumps(
        {"method": "item/agentMessage/delta", "params": {"delta": "noise"}}
    )
    turn_completed = json.dumps(
        {"method": "turn/completed", "params": {"threadId": "thread-1"}}
    )
    item_completed = json.dumps(
        {"method": "item/completed", "params": {"threadId": "thread-1"}}
    )

    try:
        tap.start()
        with (
            unix_connect(
                str(event_socket),
                uri=f"ws://localhost{ANALYTICS_EVENT_STREAM_PATH}",
                compression=None,
            ) as analytics,
            unix_connect(
                str(event_socket),
                uri=f"ws://localhost{AGENT_OBSERVER_EVENT_STREAM_PATH}",
                compression=None,
            ) as observer,
            unix_connect(
                str(event_socket), uri="ws://localhost/events", compression=None
            ) as external,
        ):
            assert analytics.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE
            assert observer.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE
            assert external.recv(timeout=1) == EVENT_STREAM_READY_MESSAGE

            tap.publish(thread_started)
            tap.publish(token_delta)
            tap.publish(item_completed)
            tap.publish(turn_completed)

            assert analytics.recv(timeout=1) == thread_started
            assert analytics.recv(timeout=1) == item_completed
            assert analytics.recv(timeout=1) == turn_completed
            with pytest.raises(TimeoutError):
                analytics.recv(timeout=0.05)
            assert observer.recv(timeout=1) == thread_started
            assert observer.recv(timeout=1) == item_completed
            assert observer.recv(timeout=1) == turn_completed
            with pytest.raises(TimeoutError):
                observer.recv(timeout=0.05)
            assert [external.recv(timeout=1) for _ in range(4)] == [
                thread_started,
                token_delta,
                item_completed,
                turn_completed,
            ]
    finally:
        tap.close()


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
    thread_started = json.dumps(
        {
            "method": "thread/started",
            "params": {
                "thread": {
                    "id": "thread-1",
                    "createdAt": 1_787_692_800,
                }
            },
        }
    )

    try:
        tap.start()
        tap.publish(thread_started)
        tap.publish(started)
        with unix_connect(
            str(event_socket), uri="ws://localhost/events", compression=None
        ) as subscriber:
            assert json.loads(subscriber.recv(timeout=1)) == {
                "method": "rodex/event-stream/ready",
                "params": {
                    "activeTurns": {"thread-1": "turn-1"},
                    "knownThreads": [{"id": "thread-1", "createdAt": 1_787_692_800}],
                },
            }
        tap.publish(completed)
        with unix_connect(
            str(event_socket), uri="ws://localhost/events", compression=None
        ) as subscriber:
            assert json.loads(subscriber.recv(timeout=1)) == {
                "method": "rodex/event-stream/ready",
                "params": {
                    "activeTurns": {},
                    "knownThreads": [{"id": "thread-1", "createdAt": 1_787_692_800}],
                },
            }
    finally:
        tap.close()
