from __future__ import annotations

import json
import shlex
import shutil
import socket
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread

import pytest

import rodex.agent_observer as observer_module
from rodex.agent_observer import (
    AgentObserverCoordinator,
    AgentObserverView,
    notify_agent_observer_trace_publication,
    observer_control_socket_path,
    project_agent_message_event,
    project_collaboration_invocation_event,
    project_subagent_activity_event,
    project_user_message_event,
)
from rodex.observer_contract import OBSERVER_PROJECTED_TEXT_MAX_CHARS
from rodex.observer_pane import ObserverPaneController
from rodex.protocol_proxy import CodexProtocolEventTap
from rodex.tmux_session_capability import TmuxRuntimeCapability
from rodex_registry import (
    RodexAgentObserverTurnEvidence,
    RodexAgentTraceEvent,
    RodexAgentTracePublication,
    RodexAgentTraceSnapshot,
    RodexRuntimeId,
    TraceSubagentActivity,
    TraceToolCall,
    create_a_rodex_session,
    read_rodex_agent_observer_turn_evidence,
    split_codex_thread_id_into_signed_bigints,
    split_codex_turn_id_into_signed_bigints,
)
from rodex_registry.agent_trace_contract import prepare_agent_trace_publication
from rodex_registry.agent_trace_writer import publish_agent_trace_in_transaction
from rodex_registry.statistics_fields import TURN_STATISTICS_SCALARS
from rodex_sql import open_rodex_transaction

ROOT_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
CHILD_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f83")
OTHER_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f84")
TRACE_CURSOR = uuid.UUID("10000000-0000-4000-8000-000000000001")
RUNTIME_ID = RodexRuntimeId.parse("0123456789abcdef")


def _runtime_capability(socket_path: Path) -> TmuxRuntimeCapability:
    return TmuxRuntimeCapability(
        socket_path,
        "0123456789abcdef0123456789abcdef",
        "$7",
        "%7",
        RUNTIME_ID,
    )


def _observer_capability_read_output(
    command: list[str],
    *,
    current_path: str = "/workspace",
) -> str | None:
    """Model the direct if-shell ownership gate before its display payload."""
    if command[3] != "if-shell" or "display-message" not in command[-2]:
        return None
    if "#{pane_current_path}" in command[-2]:
        return f"%7|{current_path}\n"
    if "@rodex_agent_observer_for" in command[-2]:
        return "%9|%7|0\n"
    raise AssertionError(f"unexpected capability read: {command}")


def _receive_control_event(listener: socket.socket) -> dict[str, object]:
    connection, _address = listener.accept()
    with connection:
        header = _receive_exactly(connection, 8)
        payload = _receive_exactly(connection, int.from_bytes(header, "big"))
    event = json.loads(payload)
    assert isinstance(event, dict)
    return event


def _receive_exactly(connection: socket.socket, size: int) -> bytes:
    payload = bytearray()
    while len(payload) < size:
        chunk = connection.recv(min(size - len(payload), 64 * 1024))
        if not chunk:
            raise AssertionError("observer control frame ended early")
        payload.extend(chunk)
    return bytes(payload)


def _observer_snapshot_events(
    snapshot: dict[str, object],
) -> list[dict[str, object]]:
    assert snapshot["kind"] == "observer_state_snapshot"
    state = snapshot["state"]
    assert isinstance(state, dict)
    events = state["events"]
    assert isinstance(events, list)
    assert all(isinstance(event, dict) for event in events)
    return events  # type: ignore[return-value]


def _turn_evidence(
    *,
    thread_id: uuid.UUID = CHILD_THREAD_ID,
    turn_id: str = "turn-child",
    history_inheritance_kind: str = "clean",
    inherited_history_start_ordinal: int | None = None,
    actions: int = 2,
    web_operations: int = 0,
    web_queries: int = 0,
    web_results: int = 0,
    compactions: int = 0,
    agent_path: str = "/root/live-review",
) -> RodexAgentObserverTurnEvidence:
    return RodexAgentObserverTurnEvidence(
        codex_thread_id=thread_id,
        codex_turn_id=turn_id,
        agent_path=agent_path,
        agent_nickname="Ada",
        history_inheritance_kind=history_inheritance_kind,
        inherited_history_start_ordinal=inherited_history_start_ordinal,
        outcome="completed",
        model="gpt-5.6-luna",
        reasoning_effort="max",
        actions_completed_count=actions,
        commands_executed_count=0,
        file_change_operations_count=0,
        web_operations_count=web_operations,
        web_queries_count=web_queries,
        web_result_records_count=web_results,
        compactions_count=compactions,
        input_tokens=1_100,
        cached_input_tokens=900,
        output_tokens=100,
        reasoning_output_tokens=40,
        total_tokens=1_200,
    )


def _spawn_event(
    *,
    method: str = "item/started",
    item_id: str = "call-spawn-1",
    activity_kind: str = "started",
    turn_id: str = "turn-1",
) -> dict[str, object]:
    return {
        "emittedAtMs": 1787794770053,
        "method": method,
        "params": {
            "startedAtMs": 1787794770052,
            "threadId": str(ROOT_THREAD_ID),
            "turnId": turn_id,
            "item": {
                "type": "subAgentActivity",
                "id": item_id,
                "agentPath": "/root/live-review",
                "agentThreadId": str(CHILD_THREAD_ID),
                "kind": activity_kind,
            },
        },
    }


def _agent_message_event(
    *,
    thread_id: uuid.UUID = CHILD_THREAD_ID,
    item_id: str = "message-1",
    item_type: str = "agentMessage",
    phase: str = "commentary",
    text: str = "I am checking the official source now.",
    turn_id: str = "turn-child",
) -> dict[str, object]:
    return {
        "emittedAtMs": 1787794775000,
        "method": "item/completed",
        "params": {
            "completedAtMs": 1787794774999,
            "threadId": str(thread_id),
            "turnId": turn_id,
            "item": {
                "type": item_type,
                "id": item_id,
                "phase": phase,
                "text": text,
            },
        },
    }


def _user_message_event(
    *,
    thread_id: uuid.UUID = ROOT_THREAD_ID,
    turn_id: str = "turn-1",
    item_id: str = "user-message-1",
    text: str = "Please review the parser exactly as requested.",
) -> dict[str, object]:
    return {
        "emittedAtMs": 1787794769000,
        "method": "item/completed",
        "params": {
            "completedAtMs": 1787794768999,
            "threadId": str(thread_id),
            "turnId": turn_id,
            "item": {
                "type": "userMessage",
                "id": item_id,
                "content": [{"type": "text", "text": text}],
            },
        },
    }


def _collaboration_invocation_event(
    *,
    method: str = "item/started",
    item_id: str = "call-spawn-1",
    tool: str = "spawnAgent",
    prompt: str | None = "Review the parser implementation.",
    turn_id: str = "turn-1",
    status: str = "completed",
) -> dict[str, object]:
    return {
        "method": method,
        "params": {
            "threadId": str(ROOT_THREAD_ID),
            "turnId": turn_id,
            "item": {
                "type": "collabAgentToolCall",
                "id": item_id,
                "tool": tool,
                "prompt": prompt,
                "status": status,
                "senderThreadId": str(ROOT_THREAD_ID),
                "receiverThreadIds": [str(CHILD_THREAD_ID)],
            },
        },
    }


def _projected_activity(
    *,
    method: str = "item/started",
    item_id: str = "call-spawn-1",
    activity_kind: str = "started",
    tool: str = "spawnAgent",
    prompt: str | None = "Review the parser implementation.",
    turn_id: str = "turn-1",
) -> dict[str, object]:
    activity = project_subagent_activity_event(
        _spawn_event(
            method=method,
            item_id=item_id,
            activity_kind=activity_kind,
            turn_id=turn_id,
        )
    )
    invocation = project_collaboration_invocation_event(
        _collaboration_invocation_event(
            method=method,
            item_id=item_id,
            tool=tool,
            prompt=prompt,
            turn_id=turn_id,
        )
    )
    assert activity is not None
    assert invocation is not None
    activity["collaboration_invocation"] = invocation["item"]
    return activity


def test_observed_subagent_activity_projection_is_exact_and_content_free() -> None:
    event = _spawn_event()
    event["params"]["item"]["private"] = "must not enter the observer"  # type: ignore[index]

    projected = project_subagent_activity_event(event)

    assert projected == {
        "schema": "rodex-agent-observer-v2",
        "kind": "app_server_subagent_activity",
        "method": "item/started",
        "thread_id": str(ROOT_THREAD_ID),
        "turn_id": "turn-1",
        "item": {
            "type": "subAgentActivity",
            "id": "call-spawn-1",
            "activity_kind": "started",
            "agent_thread_id": str(CHILD_THREAD_ID),
            "agent_path": "/root/live-review",
        },
    }
    assert "must not enter the observer" not in json.dumps(projected)


def test_subagent_activity_projection_rejects_near_matches() -> None:
    wrong_method = _spawn_event()
    wrong_method["method"] = "item/outputDelta"
    wrong_type = _collaboration_invocation_event()
    missing_id = _spawn_event()
    missing_id["params"]["item"]["id"] = ""  # type: ignore[index]

    assert project_subagent_activity_event(wrong_method) is None
    assert project_subagent_activity_event(wrong_type) is None
    assert project_subagent_activity_event(missing_id) is None


def test_current_codex_collaboration_invocation_projection_is_exact() -> None:
    projected = project_collaboration_invocation_event(
        _collaboration_invocation_event(
            tool="sendMessage",
            prompt="Please send a concise checkpoint.",
        )
    )

    assert projected == {
        "schema": "rodex-agent-observer-v2",
        "kind": "app_server_collaboration_invocation",
        "method": "item/started",
        "thread_id": str(ROOT_THREAD_ID),
        "turn_id": "turn-1",
        "item": {
            "type": "collabAgentToolCall",
            "id": "call-spawn-1",
            "raw_tool_name": "sendMessage",
            "tool_name": "collaboration.send_message",
            "status": "completed",
            "sender_thread_id": str(ROOT_THREAD_ID),
            "receiver_thread_ids": [str(CHILD_THREAD_ID)],
            "prompt": "Please send a concise checkpoint.",
        },
    }


def test_collaboration_invocation_waits_for_its_correlated_activity(
    tmp_path: Path,
) -> None:
    sent: list[tuple[Path, dict[str, object]]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unsupported spawn item reached tmux: {command}")

    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%7",
        tmp_path / "events.sock",
        runner=runner,
        event_sender=lambda path, event: sent.append((path, event)),
    )
    controller.activate(
        database_path=tmp_path / "rodex.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )

    controller.observe_protocol_event(_collaboration_invocation_event())

    assert sent == []


def test_agent_message_projection_accepts_only_completed_agent_authored_text() -> None:
    projected = project_agent_message_event(_agent_message_event())

    assert projected == {
        "schema": "rodex-agent-observer-v2",
        "kind": "app_server_agent_message",
        "thread_id": str(CHILD_THREAD_ID),
        "turn_id": "turn-child",
        "item": {
            "type": "agentMessage",
            "id": "message-1",
            "phase": "commentary",
            "text": "I am checking the official source now.",
        },
    }
    assert (
        project_agent_message_event(_agent_message_event(item_type="userMessage")) is None
    )
    started = _agent_message_event()
    started["method"] = "item/started"
    assert project_agent_message_event(started) is None


def test_user_message_projection_preserves_exact_text_blocks_and_provenance() -> None:
    event = _user_message_event(text="First line.\nSecond line — unchanged.")
    event["params"]["item"]["content"].append(  # type: ignore[index,union-attr]
        {"type": "image", "url": "ignored-without-inference"}
    )

    assert project_user_message_event(event) == {
        "schema": "rodex-agent-observer-v2",
        "kind": "app_server_user_message",
        "thread_id": str(ROOT_THREAD_ID),
        "turn_id": "turn-1",
        "item": {
            "type": "userMessage",
            "id": "user-message-1",
            "text_blocks": ["First line.\nSecond line — unchanged."],
        },
    }
    assert project_user_message_event(_agent_message_event()) is None


def test_observer_control_socket_is_stable_and_unique_per_runtime(tmp_path: Path) -> None:
    first = observer_control_socket_path(tmp_path / "events-first.sock")
    second = observer_control_socket_path(tmp_path / "events-second.sock")

    assert first == observer_control_socket_path(tmp_path / "events-first.sock")
    assert first != second
    assert first.parent == second.parent == tmp_path
    assert first.name.startswith("agent-observer-")


def test_exact_spawn_creates_a_disabled_top_third_without_changing_focus(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    sent: list[tuple[Path, dict[str, object]]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        operation = command[3]
        if operation == "show-options":
            return subprocess.CompletedProcess(command, 0, "", "")
        read_output = _observer_capability_read_output(command)
        if read_output is not None:
            return subprocess.CompletedProcess(command, 0, read_output, "")
        if operation == "if-shell" and "split-window" in command[-2]:
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%7",
        tmp_path / "events.sock",
        runner=runner,
        cursor_reader=lambda session_id, database: (
            TRACE_CURSOR
            if (session_id, database) == (3, tmp_path / "rodex.sqlite3")
            else None
        ),
        event_sender=lambda path, event: sent.append((path, event)),
        python_executable="/usr/bin/python3",
    )
    controller.activate(
        database_path=tmp_path / "rodex.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )

    controller.observe_protocol_event(_spawn_event())

    split = shlex.split(
        next(command[-2] for command in calls if "split-window" in command[-2])
    )
    assert split[1:11] == [
        "-v",
        "-b",
        "-d",
        "-p",
        "33",
        "-t",
        "%7",
        "-c",
        "/workspace",
        "-P",
    ]
    assert split[11:15] == ["-F", "#{pane_id}", "-e", "PWD=/workspace"]
    assert "exec" not in split
    scrubber_start = split.index("/usr/bin/python3", 15)
    assert split[scrubber_start : scrubber_start + 4] == [
        "/usr/bin/python3",
        "-I",
        "-m",
        "rodex.environment_exec",
    ]
    observer_start = split.index("--", scrubber_start) + 1
    assert split[observer_start : observer_start + 3] == [
        "/usr/bin/python3",
        "-m",
        "rodex.agent_observer",
    ]
    assert "--initial-event" in split
    joined_split = shlex.join(split)
    assert str(TRACE_CURSOR) in joined_split
    assert "subAgentActivity" in joined_split
    assert "/root/live-review" in joined_split
    assert "prompt" not in joined_split
    assert [shlex.split(command[-2]) for command in calls[-4:]] == [
        ["set-option", "-p", "-t", "%7", "@rodex_agent_observer_pane_id", "%9"],
        ["set-option", "-p", "-t", "%9", "@rodex_agent_observer_for", "%7"],
        ["select-pane", "-d", "-t", "%9"],
        ["select-pane", "-t", "%7"],
    ]
    assert sent == []


@pytest.mark.parametrize("failed_registration_step", range(4))
def test_observer_creation_rolls_back_every_partial_registration_failure(
    tmp_path: Path,
    failed_registration_step: int,
) -> None:
    calls: list[list[str]] = []
    registration_step = 0
    failure_injected = False

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        nonlocal failure_injected, registration_step
        calls.append(command)
        operation = command[3]
        read_output = _observer_capability_read_output(command)
        if read_output is not None:
            return subprocess.CompletedProcess(command, 0, read_output, "")
        if operation != "if-shell":
            raise AssertionError(f"unexpected tmux command: {command}")
        action = shlex.split(command[-2])
        if action[0] == "split-window":
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        if not failure_injected:
            if registration_step == failed_registration_step:
                failure_injected = True
                return subprocess.CompletedProcess(command, 1, "", "injected failure")
            registration_step += 1
        return subprocess.CompletedProcess(command, 0, "", "")

    controller = ObserverPaneController(
        "/usr/bin/tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%7",
        runner=runner,
        python_executable="/usr/bin/python3",
    )

    created = controller.create(
        database_path=tmp_path / "rodex.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
        protocol_event_socket_path=tmp_path / "events.sock",
        initial_event={"kind": "test"},
    )

    assert created is None
    assert failure_injected
    actions = [shlex.split(command[-2]) for command in calls if command[3] == "if-shell"]
    assert ["kill-pane", "-t", "%9"] in actions


def test_existing_observer_pane_is_reused_and_receives_exact_new_spawn(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    sent: list[tuple[Path, dict[str, object]]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        operation = command[3]
        if operation == "show-options":
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        read_output = _observer_capability_read_output(command)
        if read_output is not None:
            return subprocess.CompletedProcess(command, 0, read_output, "")
        raise AssertionError(f"unexpected tmux mutation: {command}")

    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%7",
        tmp_path / "events.sock",
        runner=runner,
        cursor_reader=lambda *_args: TRACE_CURSOR,
        event_sender=lambda path, event: sent.append((path, event)),
    )
    controller.activate(
        database_path=tmp_path / "rodex.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )

    controller.observe_protocol_event(_spawn_event(item_id="call-spawn-2"))

    assert not any("split-window" in " ".join(command) for command in calls)
    assert len(sent) == 1
    assert sent[0][0] == observer_control_socket_path(tmp_path / "events.sock")
    events = _observer_snapshot_events(sent[0][1])
    assert events[0]["after_event_id"] == str(TRACE_CURSOR)
    assert events[0]["item"]["id"] == "call-spawn-2"  # type: ignore[index]
    assert "scope" not in events[0]


def test_same_turn_parent_request_is_sent_exactly_without_entering_process_args(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    sent: list[tuple[Path, dict[str, object]]] = []
    request = 'Ask the agent exactly: "How many languages are spoken?"'

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        operation = command[3]
        if operation == "show-options":
            return subprocess.CompletedProcess(command, 0, "", "")
        read_output = _observer_capability_read_output(command)
        if read_output is not None:
            return subprocess.CompletedProcess(command, 0, read_output, "")
        if operation == "if-shell" and "split-window" in command[-2]:
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%7",
        tmp_path / "events-runtime-a.sock",
        runner=runner,
        cursor_reader=lambda *_args: TRACE_CURSOR,
        event_sender=lambda path, event: sent.append((path, event)),
        python_executable="/usr/bin/python3",
    )
    controller.activate(
        database_path=tmp_path / "rodex.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )

    controller.observe_protocol_event(_user_message_event(text=request))
    controller.observe_protocol_event(_collaboration_invocation_event())
    controller.observe_protocol_event(_spawn_event())

    split = shlex.split(
        next(command[-2] for command in calls if "split-window" in command[-2])
    )
    assert request not in split[-1]
    assert "root_request_context_follows" in split[-1]
    assert len(sent) == 1
    path, snapshot = sent[0]
    assert path == observer_control_socket_path(tmp_path / "events-runtime-a.sock")
    root_request_context = next(
        event
        for event in _observer_snapshot_events(snapshot)
        if event["kind"] == "root_request_context"
    )
    assert root_request_context == {
        "schema": "rodex-agent-observer-v2",
        "kind": "root_request_context",
        "thread_id": str(ROOT_THREAD_ID),
        "turn_id": "turn-1",
        "target_thread_id": str(CHILD_THREAD_ID),
        "activity_item_id": "call-spawn-1",
        "item": {
            "type": "userMessage",
            "id": "user-message-1",
            "text_blocks": [request],
        },
    }


def test_parent_request_is_not_correlated_across_turns_or_roots(tmp_path: Path) -> None:
    sent: list[tuple[Path, dict[str, object]]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        operation = command[3]
        if operation == "show-options":
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        read_output = _observer_capability_read_output(command)
        if read_output is not None:
            return subprocess.CompletedProcess(command, 0, read_output, "")
        raise AssertionError(f"unexpected tmux mutation: {command}")

    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%7",
        tmp_path / "events.sock",
        runner=runner,
        cursor_reader=lambda *_args: TRACE_CURSOR,
        event_sender=lambda path, event: sent.append((path, event)),
    )
    controller.activate(
        database_path=tmp_path / "rodex.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )

    controller.observe_protocol_event(_user_message_event(turn_id="turn-before"))
    controller.observe_protocol_event(
        _user_message_event(thread_id=OTHER_THREAD_ID, text="other root")
    )
    controller.observe_protocol_event(_collaboration_invocation_event())
    controller.observe_protocol_event(_spawn_event())

    assert len(sent) == 1
    events = _observer_snapshot_events(sent[0][1])
    assert [event["kind"] for event in events] == ["app_server_subagent_activity"]
    assert "root_request_context_follows" not in events[0]


def test_same_agent_followup_receives_its_new_exact_parent_request(tmp_path: Path) -> None:
    sent: list[tuple[Path, dict[str, object]]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        operation = command[3]
        if operation == "show-options":
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        read_output = _observer_capability_read_output(command)
        if read_output is not None:
            return subprocess.CompletedProcess(command, 0, read_output, "")
        raise AssertionError(f"unexpected tmux mutation: {command}")

    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%7",
        tmp_path / "events.sock",
        runner=runner,
        cursor_reader=lambda *_args: TRACE_CURSOR,
        event_sender=lambda path, event: sent.append((path, event)),
    )
    controller.activate(
        database_path=tmp_path / "rodex.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )
    controller.observe_protocol_event(_collaboration_invocation_event())
    controller.observe_protocol_event(_spawn_event())
    sent.clear()

    controller.observe_protocol_event(
        _user_message_event(
            turn_id="turn-2",
            item_id="user-message-2",
            text="Now analyze the stock market, Iran, tariffs, and Truth Social.",
        )
    )
    controller.observe_protocol_event(
        _collaboration_invocation_event(
            method="item/completed",
            item_id="call-followup-1",
            tool="followupTask",
            prompt="Now analyze the stock market, Iran, tariffs, and Truth Social.",
            turn_id="turn-2",
        )
    )
    controller.observe_protocol_event(
        _spawn_event(
            method="item/completed",
            item_id="call-followup-1",
            activity_kind="interacted",
            turn_id="turn-2",
        )
    )

    latest_state = sent[-1][1]["state"]
    assert isinstance(latest_state, dict)
    terminal_events = latest_state["tombstones"]
    assert isinstance(terminal_events, list)
    assert [event["kind"] for event in terminal_events] == [  # type: ignore[index]
        "app_server_collaboration_invocation",
        "app_server_subagent_activity",
    ]
    assert terminal_events[1]["root_request_context_follows"] is True  # type: ignore[index]
    root_request_context = next(
        event
        for event in _observer_snapshot_events(sent[-1][1])
        if event["kind"] == "root_request_context"
    )
    assert root_request_context["item"] == {
        "type": "userMessage",
        "id": "user-message-2",
        "text_blocks": ["Now analyze the stock market, Iran, tariffs, and Truth Social."],
    }


def test_primary_event_path_forwards_tracked_agent_prose_without_subscriber_gap(
    tmp_path: Path,
) -> None:
    sent: list[tuple[Path, dict[str, object]]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        operation = command[3]
        if operation == "show-options":
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        read_output = _observer_capability_read_output(command)
        if read_output is not None:
            return subprocess.CompletedProcess(command, 0, read_output, "")
        raise AssertionError(f"unexpected tmux mutation: {command}")

    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%7",
        tmp_path / "events.sock",
        runner=runner,
        cursor_reader=lambda *_args: TRACE_CURSOR,
        event_sender=lambda path, event: sent.append((path, event)),
    )
    controller.activate(
        database_path=tmp_path / "rodex.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )
    controller.observe_protocol_event(_spawn_event())
    sent.clear()

    controller.observe_protocol_event(_agent_message_event())

    assert len(sent) == 1
    assert sent[0][0] == observer_control_socket_path(tmp_path / "events.sock")
    assert _observer_snapshot_events(sent[0][1])[-1] == project_agent_message_event(
        _agent_message_event()
    )


def test_controller_close_waits_for_inflight_callback_and_blocks_late_mutation(
    tmp_path: Path,
) -> None:
    callback_entered = Event()
    release_callback = Event()
    close_complete = Event()
    calls: list[list[str]] = []
    calls_after_close: list[list[str]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if close_complete.is_set():
            calls_after_close.append(command)
        operation = command[3]
        if operation == "show-options":
            callback_entered.set()
            assert release_callback.wait(2)
            return subprocess.CompletedProcess(command, 1, "", "missing")
        read_output = _observer_capability_read_output(command, current_path=str(tmp_path))
        if read_output is not None:
            return subprocess.CompletedProcess(command, 0, read_output, "")
        if operation == "if-shell" and "split-window" in command[-2]:
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%7",
        tmp_path / "events.sock",
        runner=runner,
        cursor_reader=lambda *_args: TRACE_CURSOR,
        event_sender=lambda *_args: None,
    )
    controller.activate(
        database_path=tmp_path / "rodex.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )
    callback = Thread(target=controller.observe_protocol_event, args=(_spawn_event(),))
    callback.start()
    assert callback_entered.wait(1)

    def close_controller() -> None:
        controller.close()
        close_complete.set()

    closer = Thread(target=close_controller)
    closer.start()
    assert not close_complete.wait(0.05), "close overtook an in-flight observer callback"
    release_callback.set()
    callback.join(timeout=2)
    closer.join(timeout=2)
    assert not callback.is_alive()
    assert not closer.is_alive()
    assert close_complete.is_set()
    assert calls_after_close == []

    calls_at_close = len(calls)
    controller.observe_protocol_event(_spawn_event(item_id="late-call"))
    assert len(calls) == calls_at_close
    assert controller._observer_state.tracked_target_thread_ids == frozenset()


def test_existing_observer_reconnects_with_current_bounded_semantic_snapshot(
    tmp_path: Path,
) -> None:
    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        operation = command[3]
        if operation == "show-options":
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        read_output = _observer_capability_read_output(command)
        if read_output is not None:
            return subprocess.CompletedProcess(command, 0, read_output, "")
        raise AssertionError(f"unexpected tmux mutation: {command}")

    event_socket = tmp_path / "events.sock"
    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%7",
        event_socket,
        runner=runner,
        cursor_reader=lambda *_args: TRACE_CURSOR,
    )
    controller.activate(
        database_path=tmp_path / "rodex.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )
    controller.observe_protocol_event(_spawn_event(item_id="call-during-startup"))

    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    receiver.settimeout(2)
    try:
        receiver.bind(str(observer_control_socket_path(event_socket)))
        receiver.listen()
        payload = _receive_control_event(receiver)

        long_text = "Exact long agent prose: " + ("evidence " * 25_000)
        controller.observe_protocol_event(
            _agent_message_event(
                item_id="message-long",
                text=long_text,
                turn_id="turn-child-long",
            )
        )
        long_payload = _receive_control_event(receiver)

        controller.observe_protocol_event(
            _agent_message_event(
                item_id="message-after-long",
                text="Later agent reply.",
                turn_id="turn-child-long",
            )
        )
        later_payload = _receive_control_event(receiver)
    finally:
        receiver.close()
        controller.close()

    startup_events = _observer_snapshot_events(payload)
    long_events = _observer_snapshot_events(long_payload)
    later_events = _observer_snapshot_events(later_payload)

    assert [event["item"]["id"] for event in startup_events] == [  # type: ignore[index]
        "call-during-startup"
    ]
    assert startup_events[0]["after_event_id"] == str(TRACE_CURSOR)
    projected_long_text = long_events[-1]["item"]["text"]  # type: ignore[index]
    assert projected_long_text == long_text[: len(projected_long_text)]
    assert len(projected_long_text) == OBSERVER_PROJECTED_TEXT_MAX_CHARS
    assert long_events[-1]["projection_overflow"] == {
        "truncated_fields": ["item.text"],
        "omitted_list_items": {},
    }
    assert later_events[-1]["item"]["text"] == "Later agent reply."  # type: ignore[index]
    assert [len(startup_events), len(long_events), len(later_events)] == [1, 2, 3]


def test_observer_view_renders_exact_parent_request_after_tracked_spawn() -> None:
    initial = _projected_activity()
    parent = project_user_message_event(
        _user_message_event(text="First line exactly.\nSecond line exactly.")
    )
    assert initial is not None
    assert parent is not None
    initial["root_request_context_follows"] = True
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)
    request_event = {
        "schema": "rodex-agent-observer-v2",
        "kind": "root_request_context",
        "thread_id": str(ROOT_THREAD_ID),
        "turn_id": "turn-1",
        "target_thread_id": str(CHILD_THREAD_ID),
        "activity_item_id": "call-spawn-1",
        "item": parent["item"],
    }

    assert view.initial_lines == (
        "▶ live-review · agent started",
        "",
        "INVOKED · spawn_agent",
        "  New agent thread · context inheritance awaiting verification",
        "",
        "DELEGATED TASK · exact collaboration prompt",
        "  Review the parser implementation.",
    )
    assert view.accept_root_request_context_event(request_event) == [
        "",
        "ROOT TURN REQUEST · exact user message",
        "  Parent-turn provenance; not the collaboration payload.",
        "  First line exactly.",
        "  Second line exactly.",
    ]
    assert view.accept_root_request_context_event(request_event) == []


def test_parent_request_can_arrive_after_durable_turn_binding_without_being_lost() -> None:
    initial = _projected_activity()
    parent = project_user_message_event(_user_message_event(text="Exact delayed request."))
    assert initial is not None
    assert parent is not None
    initial["root_request_context_follows"] = True
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            1,
            "rodex-agent-trace-v2",
            "2026-08-27T00:00:01Z",
            "complete",
            1,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000001",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T00:00:01Z",
                    "detail": None,
                },
            ),
        )
    )

    lines = view.accept_root_request_context_event(
        {
            "schema": "rodex-agent-observer-v2",
            "kind": "root_request_context",
            "thread_id": str(ROOT_THREAD_ID),
            "turn_id": "turn-1",
            "target_thread_id": str(CHILD_THREAD_ID),
            "activity_item_id": "call-spawn-1",
            "item": parent["item"],
        }
    )

    assert lines == [
        "",
        "ROOT TURN REQUEST · exact user message",
        "  Parent-turn provenance; not the collaboration payload.",
        "  Exact delayed request.",
    ]


def test_observer_view_rejects_cross_root_activity_and_parent_request() -> None:
    wrong_root_spawn = _spawn_event()
    wrong_root_spawn["params"]["threadId"] = str(OTHER_THREAD_ID)  # type: ignore[index]
    initial = project_subagent_activity_event(wrong_root_spawn)
    assert initial is not None
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)

    assert view.initial_lines == ()
    assert view.target_thread_ids == frozenset()
    assert (
        view.accept_root_request_context_event(
            {
                "schema": "rodex-agent-observer-v2",
                "kind": "root_request_context",
                "thread_id": str(OTHER_THREAD_ID),
                "turn_id": "turn-1",
                "target_thread_id": str(CHILD_THREAD_ID),
                "activity_item_id": "call-spawn-1",
                "item": {
                    "type": "userMessage",
                    "id": "user-message-1",
                    "text_blocks": ["must not cross roots"],
                },
            }
        )
        == []
    )


def test_observer_view_renders_only_exact_target_trace_metadata() -> None:
    initial = _projected_activity(prompt=None)
    assert initial is not None
    initial["after_event_id"] = str(TRACE_CURSOR)
    view = AgentObserverView(
        root_thread_id=ROOT_THREAD_ID,
        initial_event=initial,
    )
    terminal_event_id = uuid.UUID("10000000-0000-4000-8000-000000000009")
    snapshot = RodexAgentTraceSnapshot(
        trace_publication_sequence=8,
        trace_schema_version="rodex-agent-trace-v2",
        calculated_at_utc="2026-08-27T00:00:02Z",
        coverage_state="complete",
        durable_event_count=8,
        unrecognized_record_count=0,
        events=(
            {
                "event_id": "10000000-0000-4000-8000-000000000002",
                "codex_thread_id": str(CHILD_THREAD_ID),
                "codex_turn_id": "turn-child",
                "event_kind": "turn_started",
                "event_time_utc": "2026-08-27T00:00:00Z",
                "detail": None,
            },
            {
                "event_id": "10000000-0000-4000-8000-000000000003",
                "codex_thread_id": str(CHILD_THREAD_ID),
                "codex_turn_id": "turn-child",
                "event_kind": "turn_context",
                "event_time_utc": "2026-08-27T00:00:00Z",
                "detail": {
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "working_directory": "/workspace",
                    "sandbox_mode": "read-only",
                    "approval_policy": "never",
                    "permission_profile_type": "disabled",
                    "workspace_root_count": 1,
                },
            },
            {
                "event_id": "10000000-0000-4000-8000-000000000004",
                "codex_thread_id": str(OTHER_THREAD_ID),
                "codex_turn_id": "turn-other",
                "event_kind": "command_execution",
                "event_time_utc": "2026-08-27T00:00:01Z",
                "detail": {"command_status": "completed"},
            },
            {
                "event_id": "10000000-0000-4000-8000-000000000005",
                "codex_thread_id": str(CHILD_THREAD_ID),
                "codex_turn_id": "turn-child",
                "event_kind": "tool_call",
                "event_time_utc": "2026-08-27T00:00:01Z",
                "detail": {
                    "activity_kind": "output",
                    "tool_call_id": "tool-1",
                    "tool_name": "exec",
                },
            },
            {
                "event_id": "10000000-0000-4000-8000-000000000006",
                "codex_thread_id": str(CHILD_THREAD_ID),
                "codex_turn_id": "turn-child",
                "event_kind": "tool_call",
                "event_time_utc": "2026-08-27T00:00:01Z",
                "detail": {
                    "activity_kind": "output",
                    "tool_call_id": "tool-2",
                    "tool_name": "exec",
                },
            },
            {
                "event_id": "10000000-0000-4000-8000-000000000007",
                "codex_thread_id": str(CHILD_THREAD_ID),
                "codex_turn_id": "turn-child",
                "event_kind": "token_usage",
                "event_time_utc": "2026-08-27T00:00:01Z",
                "detail": {"total_tokens": 1234},
            },
            {
                "event_id": "10000000-0000-4000-8000-000000000008",
                "codex_thread_id": str(CHILD_THREAD_ID),
                "codex_turn_id": "turn-child",
                "event_kind": "rate_limit",
                "event_time_utc": "2026-08-27T00:00:01Z",
                "detail": {
                    "windows": [
                        {"window_minutes": 300, "used_percent": 10.0},
                        {"window_minutes": 10080, "used_percent": 44.0},
                    ]
                },
            },
            {
                "event_id": str(terminal_event_id),
                "codex_thread_id": str(CHILD_THREAD_ID),
                "codex_turn_id": "turn-child",
                "event_kind": "turn_completed",
                "event_time_utc": "2026-08-27T00:00:02Z",
                "detail": None,
            },
        ),
    )

    lines = view.accept_trace_snapshot(snapshot)

    assert view.initial_lines == (
        "▶ live-review · agent started",
        "",
        "INVOKED · spawn_agent",
        "  New agent thread · context inheritance awaiting verification",
        "",
        "ROOT TURN REQUEST UNAVAILABLE",
        "  Rodex could not correlate an exact same-turn root user message.",
    )
    assert lines == [
        "",
        "MODEL · live-review",
        "  gpt-5.6-luna · MAX",
    ]
    assert view.flush_pending_terminal_events() == [
        "",
        "✓ live-review finished · 2s",
        "  Invocation: spawn_agent · new agent thread",
        "  Work: 2 actions",
        "  Tokens: 1,234 processed",
        "  Weekly limit: 44% used",
    ]
    assert view.after_event_id == terminal_event_id
    assert view.monitoring is True
    view.accept_trace_publication_wake(8, caught_up=True)
    assert view.monitoring is False


def test_observer_view_shows_agent_english_but_not_other_threads_or_control_codes() -> None:
    initial = _projected_activity()
    commentary = project_agent_message_event(
        _agent_message_event(text="Checking GOV.UK.\n\x1b[31mOne moment…\x1b[0m")
    )
    final = project_agent_message_event(
        _agent_message_event(
            item_id="message-2",
            phase="final_answer",
            text="The Rt Hon Andy Burnham MP — Prime Minister.",
        )
    )
    other = project_agent_message_event(_agent_message_event(thread_id=OTHER_THREAD_ID))
    assert initial is not None
    assert commentary is not None
    assert final is not None
    assert other is not None
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)

    assert view.accept_agent_message_event(commentary) == [
        "",
        "AGENT UPDATE · live-review · commentary returned",
        "  Checking GOV.UK.",
        "  One moment…",
    ]
    assert view.accept_agent_message_event(commentary) == []
    assert view.accept_agent_message_event(other) == []
    assert view.accept_agent_message_event(final) == [
        "",
        "AGENT ANSWER · live-review · final answer returned",
        "  The Rt Hon Andy Burnham MP — Prime Minister.",
    ]


def test_observer_view_fails_closed_when_activity_has_no_exact_invocation() -> None:
    # A subagent activity alone carries lifecycle, not the exact invocation tool.
    initial = project_subagent_activity_event(_spawn_event())
    assert initial is not None

    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)

    assert view.initial_lines == (
        "• live-review · agent interaction observed",
        "  Invocation awaiting authenticated rollout correlation",
    )


def test_interacted_activity_does_not_guess_followup_task() -> None:
    interacted = project_subagent_activity_event(_spawn_event(activity_kind="interacted"))
    assert interacted is not None
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=interacted)

    assert view.initial_lines == (
        "• live-review · agent interaction observed",
        "  Invocation awaiting authenticated rollout correlation",
    )


def test_send_message_continues_the_current_turn_without_queuing_another() -> None:
    view = AgentObserverView(
        root_thread_id=ROOT_THREAD_ID,
        initial_event=_projected_activity(),
    )
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            1,
            "rodex-agent-trace-v2",
            "2026-08-27T23:54:00Z",
            "complete",
            1,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000060",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-1",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T23:54:00Z",
                    "detail": None,
                },
            ),
        )
    )
    live_invocation = project_collaboration_invocation_event(
        _collaboration_invocation_event(
            method="item/completed",
            item_id="call_fnfkboC80cNGCck6HG8rT3Z5",
            tool="sendMessage",
            prompt="Please send a concise checkpoint while continuing your work.",
        )
    )
    activity = project_subagent_activity_event(
        _spawn_event(
            method="item/completed",
            item_id="call_fnfkboC80cNGCck6HG8rT3Z5",
            activity_kind="interacted",
        )
    )
    assert live_invocation is not None
    assert activity is not None

    assert view.accept_collaboration_invocation_event(live_invocation) == []
    assert view.accept_app_server_event(activity) == [
        "→ live-review · message sent",
        "",
        "INVOKED · send_message",
        "  Existing agent thread · current agent turn continues",
        "  No new turn requested",
        "",
        "MESSAGE · exact collaboration prompt",
        "  Please send a concise checkpoint while continuing your work.",
    ]
    assert view.target_turn_keys == ((str(CHILD_THREAD_ID), "turn-child-1"),)

    lines = view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            2,
            "rodex-agent-trace-v2",
            "2026-08-27T23:54:16Z",
            "complete",
            3,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000061",
                    "codex_thread_id": str(ROOT_THREAD_ID),
                    "codex_turn_id": "turn-root",
                    "event_kind": "subagent_activity",
                    "event_time_utc": "2026-08-27T23:54:14Z",
                    "detail": {
                        "target_codex_thread_id": str(CHILD_THREAD_ID),
                        "activity_kind": "interacted",
                        "agent_path": "/root/live-review",
                        "collaboration_invocation": {
                            "tool_call_id": "10000000-0000-4000-8000-000000000062",
                            "source_call_id": "call_fnfkboC80cNGCck6HG8rT3Z5",
                            "tool_name": "collaboration.send_message",
                            "arguments_utf8_bytes": 378,
                            "arguments_capture_state": "encrypted",
                        },
                        "turn_request": None,
                    },
                },
                {
                    "event_id": "10000000-0000-4000-8000-000000000063",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-1",
                    "event_kind": "turn_completed",
                    "event_time_utc": "2026-08-27T23:54:16Z",
                    "detail": None,
                },
            ),
        )
    )

    assert "INVOKED · followup_task" not in lines
    terminal_lines = view.flush_pending_terminal_events()
    assert [line for line in terminal_lines if line.startswith("  Invocation:")] == [
        "  Invocation: spawn_agent · new agent thread"
    ]
    view.accept_trace_publication_wake(2, caught_up=True)
    assert view.monitoring is False


def test_observer_view_starts_a_new_same_agent_turn_for_followup_request() -> None:
    initial = _projected_activity()
    followup = _projected_activity(
        method="item/completed",
        item_id="call-followup-1",
        activity_kind="interacted",
        tool="followupTask",
        prompt="Second scope on the same agent.",
        turn_id="turn-2",
    )
    parent = project_user_message_event(
        _user_message_event(
            turn_id="turn-2",
            item_id="user-message-2",
            text="Second scope on the same agent.",
        )
    )
    assert initial is not None
    assert followup is not None
    assert parent is not None
    followup["root_request_context_follows"] = True
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            1,
            "rodex-agent-trace-v2",
            "2026-08-27T00:00:00Z",
            "complete",
            1,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000009",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-1",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T00:00:00Z",
                    "detail": None,
                },
            ),
        )
    )
    send_message = _projected_activity(
        method="item/completed",
        item_id="call-send-before-followup",
        activity_kind="interacted",
        tool="sendMessage",
        prompt="Send a checkpoint before the follow-up turn.",
        turn_id="turn-2",
    )
    assert "INVOKED · send_message" in view.accept_app_server_event(send_message)

    assert view.accept_app_server_event(followup) == [
        "↻ live-review · new turn requested",
        "",
        "INVOKED · followup_task",
        "  Existing agent thread · new agent turn requested · context continues",
        "",
        "FOLLOW-UP TASK · exact collaboration prompt",
        "  Second scope on the same agent.",
    ]
    assert view.accept_root_request_context_event(
        {
            "schema": "rodex-agent-observer-v2",
            "kind": "root_request_context",
            "thread_id": str(ROOT_THREAD_ID),
            "turn_id": "turn-2",
            "target_thread_id": str(CHILD_THREAD_ID),
            "activity_item_id": "call-followup-1",
            "item": parent["item"],
        }
    ) == [
        "",
        "ROOT TURN REQUEST · exact user message",
        "  Parent-turn provenance; not the collaboration payload.",
        "  Second scope on the same agent.",
    ]
    assert view.monitoring is True

    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            2,
            "rodex-agent-trace-v2",
            "2026-08-27T00:00:01Z",
            "complete",
            1,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000010",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-2",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T00:00:01Z",
                    "detail": None,
                },
            ),
        )
    )
    assert view.accept_turn_evidence((_turn_evidence(turn_id="turn-child-2"),))[:3] == [
        "",
        "CONTEXT · live-review",
        "  SAME AGENT · NEW TURN · existing agent context continues",
    ]


def test_unseen_first_turn_stays_bound_to_first_request_after_followup_arrives() -> None:
    initial = _projected_activity()
    followup = _projected_activity(
        method="item/completed",
        item_id="call-followup-before-first-turn",
        activity_kind="interacted",
        tool="followupTask",
        prompt="Second request already queued on the same agent.",
        turn_id="turn-2",
    )
    first_parent = project_user_message_event(
        _user_message_event(text="First request before its agent turn is observed.")
    )
    second_parent = project_user_message_event(
        _user_message_event(
            turn_id="turn-2",
            item_id="user-message-2",
            text="Second request already queued on the same agent.",
        )
    )
    assert initial is not None
    assert followup is not None
    assert first_parent is not None
    assert second_parent is not None
    initial["root_request_context_follows"] = True
    followup["root_request_context_follows"] = True
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)
    view.accept_root_request_context_event(
        {
            "schema": "rodex-agent-observer-v2",
            "kind": "root_request_context",
            "thread_id": str(ROOT_THREAD_ID),
            "turn_id": "turn-1",
            "target_thread_id": str(CHILD_THREAD_ID),
            "activity_item_id": "call-spawn-1",
            "item": first_parent["item"],
        }
    )
    view.accept_app_server_event(followup)
    view.accept_root_request_context_event(
        {
            "schema": "rodex-agent-observer-v2",
            "kind": "root_request_context",
            "thread_id": str(ROOT_THREAD_ID),
            "turn_id": "turn-2",
            "target_thread_id": str(CHILD_THREAD_ID),
            "activity_item_id": "call-followup-before-first-turn",
            "item": second_parent["item"],
        }
    )

    delayed_first_answer = project_agent_message_event(
        _agent_message_event(
            item_id="message-first-delayed",
            phase="final_answer",
            text="First answer arrived late.",
            turn_id="turn-child-1",
        )
    )
    assert delayed_first_answer is not None
    assert "  First answer arrived late." in view.accept_agent_message_event(
        delayed_first_answer
    )
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            2,
            "rodex-agent-trace-v2",
            "2026-08-27T00:00:04Z",
            "complete",
            3,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000038",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-1",
                    "event_kind": "turn_completed",
                    "event_time_utc": "2026-08-27T00:00:02Z",
                    "detail": None,
                },
                {
                    "event_id": "10000000-0000-4000-8000-000000000039",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-2",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T00:00:03Z",
                    "detail": None,
                },
                {
                    "event_id": "10000000-0000-4000-8000-000000000040",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-2",
                    "event_kind": "turn_completed",
                    "event_time_utc": "2026-08-27T00:00:04Z",
                    "detail": None,
                },
            ),
        )
    )

    lines = view.flush_pending_terminal_events()

    assert [line for line in lines if line.startswith("  Invocation:")] == [
        "  Invocation: spawn_agent · new agent thread",
        "  Invocation: followup_task · SAME AGENT · NEW TURN · existing context",
    ]
    assert lines.index("  First request before its agent turn is observed.") < lines.index(
        "  Second request already queued on the same agent."
    )
    view.accept_trace_publication_wake(2, caught_up=True)
    assert view.monitoring is False


def test_delayed_old_terminal_and_new_followup_keep_exact_turn_evidence_isolated() -> None:
    initial = _projected_activity()
    first_parent = project_user_message_event(_user_message_event(text="First request."))
    assert initial is not None
    assert first_parent is not None
    initial["root_request_context_follows"] = True
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)
    view.accept_root_request_context_event(
        {
            "schema": "rodex-agent-observer-v2",
            "kind": "root_request_context",
            "thread_id": str(ROOT_THREAD_ID),
            "turn_id": "turn-1",
            "target_thread_id": str(CHILD_THREAD_ID),
            "activity_item_id": "call-spawn-1",
            "item": first_parent["item"],
        }
    )
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            1,
            "rodex-agent-trace-v2",
            "2026-08-27T00:00:01Z",
            "complete",
            1,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000041",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-1",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T00:00:01Z",
                    "detail": None,
                },
            ),
        )
    )
    followup = _projected_activity(
        method="item/completed",
        item_id="call-followup-1",
        activity_kind="interacted",
        tool="followupTask",
        prompt="Second request.",
        turn_id="turn-2",
    )
    second_parent = project_user_message_event(
        _user_message_event(
            turn_id="turn-2",
            item_id="user-message-2",
            text="Second request.",
        )
    )
    assert followup is not None
    assert second_parent is not None
    followup["root_request_context_follows"] = True
    view.accept_app_server_event(followup)
    view.accept_root_request_context_event(
        {
            "schema": "rodex-agent-observer-v2",
            "kind": "root_request_context",
            "thread_id": str(ROOT_THREAD_ID),
            "turn_id": "turn-2",
            "target_thread_id": str(CHILD_THREAD_ID),
            "activity_item_id": "call-followup-1",
            "item": second_parent["item"],
        }
    )
    delayed_old_answer = project_agent_message_event(
        _agent_message_event(
            item_id="message-old-final",
            phase="final_answer",
            text="First answer.",
            turn_id="turn-child-1",
        )
    )
    assert delayed_old_answer is not None
    assert "  First answer." in view.accept_agent_message_event(delayed_old_answer)
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            2,
            "rodex-agent-trace-v2",
            "2026-08-27T00:00:04Z",
            "complete",
            4,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000042",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-1",
                    "event_kind": "turn_completed",
                    "event_time_utc": "2026-08-27T00:00:02Z",
                    "detail": None,
                },
                {
                    "event_id": "10000000-0000-4000-8000-000000000043",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-2",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T00:00:03Z",
                    "detail": None,
                },
                {
                    "event_id": "10000000-0000-4000-8000-000000000044",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-2",
                    "event_kind": "turn_completed",
                    "event_time_utc": "2026-08-27T00:00:04Z",
                    "detail": None,
                },
            ),
        )
    )

    lines = view.flush_pending_terminal_events()

    assert [line for line in lines if line.startswith("  Invocation:")] == [
        "  Invocation: spawn_agent · new agent thread",
        "  Invocation: followup_task · SAME AGENT · NEW TURN · existing context",
    ]
    assert lines.count("ROOT TURN REQUEST RECAP · exact user message") == 2
    assert lines.index("  First request.") < lines.index("  Second request.")
    assert view.target_turn_keys == ()


def test_unavailable_followup_never_recaps_the_previous_turn_request() -> None:
    initial = _projected_activity()
    parent = project_user_message_event(_user_message_event(text="Previous request."))
    assert initial is not None
    assert parent is not None
    initial["root_request_context_follows"] = True
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)
    view.accept_root_request_context_event(
        {
            "schema": "rodex-agent-observer-v2",
            "kind": "root_request_context",
            "thread_id": str(ROOT_THREAD_ID),
            "turn_id": "turn-1",
            "target_thread_id": str(CHILD_THREAD_ID),
            "activity_item_id": "call-spawn-1",
            "item": parent["item"],
        }
    )
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            1,
            "rodex-agent-trace-v2",
            "2026-08-27T00:00:01Z",
            "complete",
            2,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000051",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-1",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T00:00:01Z",
                    "detail": None,
                },
                {
                    "event_id": "10000000-0000-4000-8000-000000000054",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-1",
                    "event_kind": "turn_completed",
                    "event_time_utc": "2026-08-27T00:00:01Z",
                    "detail": None,
                },
            ),
        )
    )
    view.flush_pending_terminal_events()
    followup = _projected_activity(
        method="item/completed",
        item_id="call-followup-2",
        activity_kind="interacted",
        tool="followupTask",
        prompt=None,
        turn_id="turn-2",
    )
    assert followup is not None
    request_lines = view.accept_app_server_event(followup)
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            2,
            "rodex-agent-trace-v2",
            "2026-08-27T00:00:03Z",
            "complete",
            3,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000052",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-2",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T00:00:02Z",
                    "detail": None,
                },
                {
                    "event_id": "10000000-0000-4000-8000-000000000053",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child-2",
                    "event_kind": "turn_completed",
                    "event_time_utc": "2026-08-27T00:00:03Z",
                    "detail": None,
                },
            ),
        )
    )

    terminal_lines = view.flush_pending_terminal_events()

    assert "ROOT TURN REQUEST UNAVAILABLE" in request_lines
    assert "ROOT TURN REQUEST RECAP · exact user message" not in terminal_lines
    assert "  Previous request." not in terminal_lines


def test_observer_view_renders_exact_clean_lineage_work_and_terminal_recap() -> None:
    initial = _projected_activity()
    parent = project_user_message_event(
        _user_message_event(text="Challenge the prior theory exactly.\nKeep its caveats.")
    )
    assert initial is not None
    assert parent is not None
    initial["root_request_context_follows"] = True
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)
    view.accept_root_request_context_event(
        {
            "schema": "rodex-agent-observer-v2",
            "kind": "root_request_context",
            "thread_id": str(ROOT_THREAD_ID),
            "turn_id": "turn-1",
            "target_thread_id": str(CHILD_THREAD_ID),
            "activity_item_id": "call-spawn-1",
            "item": parent["item"],
        }
    )
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            2,
            "rodex-agent-trace-v2",
            "2026-08-27T00:00:03Z",
            "complete",
            3,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000011",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T00:00:01Z",
                    "detail": None,
                },
                {
                    "event_id": "10000000-0000-4000-8000-000000000012",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child",
                    "event_kind": "turn_completed",
                    "event_time_utc": "2026-08-27T00:00:03Z",
                    "detail": None,
                },
            ),
        )
    )

    evidence_lines = view.accept_turn_evidence(
        (
            _turn_evidence(
                actions=35,
                web_operations=27,
                web_queries=15,
                web_results=437,
                compactions=1,
            ),
        )
    )

    assert evidence_lines == [
        "",
        "CONTEXT · live-review",
        "  NEW CLEAN AGENT · separate thread/turn · no parent history inherited",
        "",
        "WORK · live-review",
        "  35 actions · 27 web operations · 15 queries · 437 result records · 1 compaction",
    ]
    assert view.flush_pending_terminal_events() == [
        "",
        "✓ live-review finished · 2s",
        "  Invocation: spawn_agent · NEW CLEAN AGENT",
        "  Work: 35 actions · 27 web operations · 15 queries · "
        "437 result records · 1 compaction",
        "  Tokens: 1,200 processed · 900 cached input · 100 output · 40 reasoning",
        "",
        "ROOT TURN REQUEST RECAP · exact user message",
        "  Parent-turn provenance; not the collaboration payload.",
        "  Challenge the prior theory exactly.",
        "  Keep its caveats.",
    ]


def test_observer_view_renders_inherited_agent_without_calling_it_same_agent() -> None:
    initial = _projected_activity()
    assert initial is not None
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            2,
            "rodex-agent-trace-v2",
            "2026-08-27T00:00:01Z",
            "complete",
            1,
            0,
            (
                {
                    "event_id": "10000000-0000-4000-8000-000000000013",
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T00:00:01Z",
                    "detail": None,
                },
            ),
        )
    )

    lines = view.accept_turn_evidence(
        (
            _turn_evidence(
                history_inheritance_kind="inherited",
                inherited_history_start_ordinal=12,
            ),
        )
    )

    assert lines[:3] == [
        "",
        "CONTEXT · live-review",
        "  NEW INHERITED AGENT · separate thread/turn · "
        "inherited-history cutoff at source ordinal 12",
    ]
    assert "SAME AGENT" not in "\n".join(lines)


def test_agent_observer_evidence_reader_uses_exact_thread_and_turn(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=ROOT_THREAD_ID)
    root_turn_id = uuid.UUID("00000000-0000-7000-8000-000000000021")
    root_turn_public_id = uuid.UUID("10000000-0000-4000-8000-000000000021")
    child_turn_id = uuid.UUID("00000000-0000-7000-8000-000000000022")
    child_turn_public_id = uuid.UUID("10000000-0000-4000-8000-000000000022")
    metric_overrides = {
        "input_tokens": 4_000,
        "cached_input_tokens": 3_000,
        "output_tokens": 400,
        "reasoning_output_tokens": 120,
        "total_tokens": 4_400,
        "model_tool_requests_count": 9,
        "model_tool_outputs_paired_count": 8,
        "web_operations_count": 6,
        "web_queries_count": 3,
        "web_result_records_count": 44,
        "compactions_count": 1,
    }
    metric_values = [
        metric_overrides.get(field.name, None if field.nullable else 0)
        for field in TURN_STATISTICS_SCALARS.fields
    ]
    with open_rodex_transaction(database) as connection:
        root_turn_row = connection.execute(
            "INSERT INTO rodex_sessions_codex_turns "
            "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
            "turn_public_id_signed_bigint_1, turn_public_id_signed_bigint_2, "
            "codex_turn_id_signed_bigint_1, codex_turn_id_signed_bigint_2) "
            "VALUES (1, 1, ?, ?, ?, ?) RETURNING id",
            (
                *split_codex_turn_id_into_signed_bigints(root_turn_public_id),
                *split_codex_turn_id_into_signed_bigints(root_turn_id),
            ),
        ).fetchone()[0]
        child_identity_row = connection.execute(
            "INSERT INTO codex_threads "
            "(codex_thread_public_id_signed_bigint_1, "
            "codex_thread_public_id_signed_bigint_2) VALUES (?, ?) RETURNING id",
            split_codex_thread_id_into_signed_bigints(CHILD_THREAD_ID),
        ).fetchone()[0]
        child_membership_row = connection.execute(
            "INSERT INTO rodex_sessions_codex_threads "
            "(rodex_sessions_id, codex_threads_id, first_linked_at_utc) "
            "VALUES (1, ?, ?) RETURNING id",
            (child_identity_row, "2026-08-27T00:00:00Z"),
        ).fetchone()[0]
        child_turn_row = connection.execute(
            "INSERT INTO rodex_sessions_codex_turns "
            "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
            "turn_public_id_signed_bigint_1, turn_public_id_signed_bigint_2, "
            "codex_turn_id_signed_bigint_1, codex_turn_id_signed_bigint_2) "
            "VALUES (1, ?, ?, ?, ?, ?) RETURNING id",
            (
                child_membership_row,
                *split_codex_turn_id_into_signed_bigints(child_turn_public_id),
                *split_codex_turn_id_into_signed_bigints(child_turn_id),
            ),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO rodex_sessions_codex_turn_states "
            "(rodex_sessions_id, rodex_sessions_codex_turns_id, "
            "started_at_utc, terminal_at_utc, outcome) VALUES (1, ?, ?, ?, ?)",
            (
                child_turn_row,
                "2026-08-27T00:00:01Z",
                "2026-08-27T00:00:03Z",
                "completed",
            ),
        )
        connection.execute(
            "INSERT INTO rodex_sessions_subagent_spawns "
            "(rodex_sessions_id, subagent_rodex_sessions_codex_threads_id, "
            "parent_rodex_sessions_codex_threads_id, "
            "spawning_rodex_sessions_codex_turns_id, agent_path, agent_nickname, "
            "history_inheritance_kind, inherited_history_start_ordinal) "
            "VALUES (1, ?, 1, ?, ?, ?, 'inherited', 7)",
            (child_membership_row, root_turn_row, "/root/reviewer", "Ada"),
        )
        connection.execute(
            "INSERT INTO rodex_sessions_statistics_turn_metrics "
            "(rodex_sessions_id, rodex_sessions_codex_turns_id, "
            f"{TURN_STATISTICS_SCALARS.columns_sql}) VALUES (?, ?, "
            f"{TURN_STATISTICS_SCALARS.placeholders_sql})",
            (1, child_turn_row, *metric_values),
        )

    evidence = read_rodex_agent_observer_turn_evidence(
        1,
        ((CHILD_THREAD_ID, str(child_turn_id)),),
        database,
    )

    assert len(evidence) == 1
    assert evidence[0].agent_path == "/root/reviewer"
    assert evidence[0].agent_nickname == "Ada"
    assert evidence[0].history_inheritance_kind == "inherited"
    assert evidence[0].inherited_history_start_ordinal == 7
    assert evidence[0].actions_completed_count == 8
    assert evidence[0].web_queries_count == 3
    assert evidence[0].web_result_records_count == 44
    assert evidence[0].cached_input_tokens == 3_000


def test_interleaved_agents_never_rewrite_another_agents_work_line() -> None:
    initial = _projected_activity()
    assert initial is not None
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)
    projected_second = _projected_activity(
        item_id="call-spawn-2",
        prompt="Review the second agent.",
    )
    projected_second["item"]["agent_thread_id"] = str(OTHER_THREAD_ID)  # type: ignore[index]
    projected_second["item"]["agent_path"] = "/root/second-review"  # type: ignore[index]
    projected_second["root_request_context_follows"] = True
    view.accept_app_server_event(projected_second)
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            2,
            "rodex-agent-trace-v2",
            "2026-08-27T00:00:02Z",
            "complete",
            3,
            0,
            tuple(
                {
                    "event_id": f"10000000-0000-4000-8000-00000000002{ordinal}",
                    "codex_thread_id": str(thread_id),
                    "codex_turn_id": turn_id,
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-27T00:00:02Z",
                    "detail": None,
                }
                for ordinal, (thread_id, turn_id) in enumerate(
                    (
                        (CHILD_THREAD_ID, "turn-a"),
                        (OTHER_THREAD_ID, "turn-b"),
                    ),
                    start=1,
                )
            ),
        )
    )

    lines = [
        *view.accept_turn_evidence((_turn_evidence(turn_id="turn-a", actions=1),)),
        *view.accept_turn_evidence(
            (
                _turn_evidence(
                    thread_id=OTHER_THREAD_ID,
                    turn_id="turn-b",
                    actions=1,
                    agent_path="/root/second-review",
                ),
            )
        ),
        *view.accept_turn_evidence((_turn_evidence(turn_id="turn-a", actions=2),)),
    ]

    assert [line for line in lines if line.startswith("WORK ·") or "action" in line] == [
        "WORK · live-review",
        "  1 action",
        "WORK · second-review",
        "  1 action",
        "WORK · live-review",
        "  2 actions",
    ]
    assert "\x1b[1A" not in "\n".join(lines)


def test_view_dedupes_same_item_id_by_child_thread_and_event_kind() -> None:
    initial = _projected_activity(item_id="shared-call-id")
    second = _projected_activity(item_id="shared-call-id")
    second_item = second["item"]
    assert isinstance(second_item, dict)
    second_item["agent_thread_id"] = str(OTHER_THREAD_ID)
    second_item["agent_path"] = "/root/second-review"
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)

    lines = view.accept_app_server_event(second)

    assert "INVOKED · spawn_agent" in lines
    assert view.target_thread_ids == frozenset({str(CHILD_THREAD_ID), str(OTHER_THREAD_ID)})


def test_transport_overflow_recovers_65_unknown_same_root_targets_from_trace() -> None:
    initial = _projected_activity(item_id="seed-call")
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)
    target_ids = [uuid.UUID(int=10_000 + index) for index in range(65)]
    durable_events = tuple(
        {
            "event_id": str(uuid.UUID(int=20_000 + index)),
            "codex_thread_id": str(ROOT_THREAD_ID),
            "codex_turn_id": "root-turn",
            "event_kind": "subagent_activity",
            "event_time_utc": "2026-08-29T00:00:00Z",
            "detail": {
                "target_codex_thread_id": str(target_id),
                "agent_path": f"/root/child-{index}",
                "activity_kind": "started",
            },
        }
        for index, target_id in enumerate(target_ids)
    )
    snapshot = RodexAgentTraceSnapshot(
        trace_publication_sequence=1,
        trace_schema_version="rodex-agent-trace-v2",
        calculated_at_utc="2026-08-29T00:00:00Z",
        coverage_state="complete",
        durable_event_count=len(durable_events),
        unrecognized_record_count=0,
        events=durable_events,
    )

    view.accept_trace_snapshot(snapshot, recover_unknown_targets=True)

    assert {str(target_id) for target_id in target_ids}.issubset(view.target_thread_ids)
    assert len(view.target_thread_ids) <= observer_module.OBSERVER_RECOVERED_TARGET_LIMIT


def test_app_item_completion_cannot_suppress_the_final_durable_trace_read() -> None:
    initial = _projected_activity()
    completed = project_subagent_activity_event(_spawn_event(method="item/completed"))
    assert initial is not None
    assert completed is not None
    view = AgentObserverView(
        root_thread_id=ROOT_THREAD_ID,
        initial_event=initial,
    )

    view.accept_app_server_event(completed)

    assert view.monitoring is True
    terminal_event_id = uuid.UUID("10000000-0000-4000-8000-000000000004")
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            trace_publication_sequence=8,
            trace_schema_version="rodex-agent-trace-v2",
            calculated_at_utc="2026-08-27T00:00:02Z",
            coverage_state="complete",
            durable_event_count=1,
            unrecognized_record_count=0,
            events=(
                {
                    "event_id": str(terminal_event_id),
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "turn-child",
                    "event_kind": "turn_completed",
                    "event_time_utc": "2026-08-27T00:00:02Z",
                    "detail": None,
                },
            ),
        )
    )
    view.accept_trace_publication_wake(8, caught_up=False)

    assert view.monitoring is True
    view.flush_pending_terminal_events()
    view.accept_trace_publication_wake(9, caught_up=True)
    assert view.monitoring is False


def test_trace_publication_notification_uses_a_nonblocking_framed_stream(
    tmp_path: Path,
) -> None:
    event_socket = tmp_path / "events.sock"
    control_socket = observer_control_socket_path(event_socket)
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    receiver.bind(str(control_socket))
    receiver.listen()
    receiver.settimeout(1)
    try:
        notify_agent_observer_trace_publication(event_socket, 17, True)
        payload = _receive_control_event(receiver)
    finally:
        receiver.close()

    assert payload == {
        "schema": "rodex-agent-observer-v2",
        "kind": "trace_published",
        "trace_publication_sequence": 17,
        "caught_up": True,
    }


def test_two_runtime_observer_notifications_are_socket_isolated(tmp_path: Path) -> None:
    first_event_socket = tmp_path / "events-runtime-one.sock"
    second_event_socket = tmp_path / "events-runtime-two.sock"
    first_control = observer_control_socket_path(first_event_socket)
    second_control = observer_control_socket_path(second_event_socket)
    first_receiver = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    second_receiver = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    first_receiver.bind(str(first_control))
    second_receiver.bind(str(second_control))
    first_receiver.listen()
    second_receiver.listen()
    first_receiver.settimeout(1)
    second_receiver.settimeout(1)
    try:
        notify_agent_observer_trace_publication(first_event_socket, 11, False)
        notify_agent_observer_trace_publication(second_event_socket, 22, True)
        first_payload = _receive_control_event(first_receiver)
        second_payload = _receive_control_event(second_receiver)
    finally:
        first_receiver.close()
        second_receiver.close()

    assert first_control != second_control
    assert first_payload["trace_publication_sequence"] == 11
    assert first_payload["caught_up"] is False
    assert second_payload["trace_publication_sequence"] == 22
    assert second_payload["caught_up"] is True


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_real_tmux_observer_renders_request_and_exits_with_its_runtime(
    tmp_path: Path,
) -> None:
    tmux = shutil.which("tmux")
    assert tmux is not None
    tmux_socket = tmp_path / "t.sock"
    event_socket = tmp_path / "e.sock"
    database = tmp_path / "r.sqlite3"
    create_a_rodex_session(database, codex_session_id=ROOT_THREAD_ID)
    with open_rodex_transaction(database) as connection:
        child_identity_id = connection.execute(
            "INSERT INTO codex_threads "
            "(codex_thread_public_id_signed_bigint_1, "
            "codex_thread_public_id_signed_bigint_2) VALUES (?, ?) RETURNING id",
            split_codex_thread_id_into_signed_bigints(CHILD_THREAD_ID),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO rodex_sessions_codex_threads "
            "(rodex_sessions_id, codex_threads_id, first_linked_at_utc) "
            "VALUES (1, ?, ?)",
            (child_identity_id, "2026-08-27T00:00:00Z"),
        )
    tap = CodexProtocolEventTap(event_socket)
    controller: AgentObserverCoordinator | None = None
    tap.start()
    try:
        subprocess.run(
            [
                tmux,
                "-S",
                str(tmux_socket),
                "new-session",
                "-d",
                "-s",
                "pane-id-anchor",
                "sleep 30",
            ],
            check=True,
        )
        for pane_number in range(1, 4):
            dummy_name = f"pane-id-{pane_number}"
            subprocess.run(
                [
                    tmux,
                    "-S",
                    str(tmux_socket),
                    "new-session",
                    "-d",
                    "-s",
                    dummy_name,
                    "sleep 30",
                ],
                check=True,
            )
            subprocess.run(
                [
                    tmux,
                    "-S",
                    str(tmux_socket),
                    "kill-session",
                    "-t",
                    f"={dummy_name}",
                ],
                check=True,
            )
        subprocess.run(
            [
                tmux,
                "-S",
                str(tmux_socket),
                "new-session",
                "-d",
                "-s",
                "observer-test",
                "-x",
                "120",
                "-y",
                "45",
                "sleep 30",
            ],
            check=True,
        )
        primary = subprocess.run(
            [
                tmux,
                "-S",
                str(tmux_socket),
                "display-message",
                "-p",
                "-t",
                "observer-test",
                "#{pane_id}",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert primary == "%4"
        server_id = "0123456789abcdef0123456789abcdef"
        subprocess.run(
            [
                tmux,
                "-S",
                str(tmux_socket),
                "set-option",
                "-s",
                "@rodex_shared_tmux_protocol",
                "rodex-shared-tmux-v1",
                ";",
                "set-option",
                "-s",
                "@rodex_shared_tmux_server_id",
                server_id,
                ";",
                "set-option",
                "-t",
                primary,
                "@rodex_runtime_id",
                str(RUNTIME_ID),
                ";",
                "set-option",
                "-t",
                primary,
                "@rodex_primary_pane_id",
                primary,
            ],
            check=True,
        )
        tmux_session_id = subprocess.run(
            [
                tmux,
                "-S",
                str(tmux_socket),
                "display-message",
                "-p",
                "-t",
                primary,
                "-F",
                "#{session_id}",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        controller = AgentObserverCoordinator(
            tmux,
            TmuxRuntimeCapability(
                tmux_socket,
                server_id,
                tmux_session_id,
                primary,
                RUNTIME_ID,
            ),
            primary,
            event_socket,
            python_executable=sys.executable,
        )
        controller.activate(
            database_path=database,
            rodex_sessions_id=1,
            rodex_session_id="1234567890abcdef",
            root_thread_id=ROOT_THREAD_ID,
        )
        exact_request = "Review the real tmux boundary exactly as requested."
        controller.observe_protocol_event(_user_message_event(text=exact_request))
        controller.observe_protocol_event(
            _collaboration_invocation_event(prompt=exact_request)
        )
        controller.observe_protocol_event(_spawn_event())

        panes = _wait_for_tmux_panes(tmux, tmux_socket, 2)
        observer = next(pane for pane in panes if pane[0] != primary)
        primary_state = next(pane for pane in panes if pane[0] == primary)
        assert observer[1] == "1"
        assert observer[2] not in {"bash", "sh", "zsh"}
        assert observer[3] == "0"
        assert primary_state[5] == "1"
        assert abs(int(observer[4]) * 2 - int(primary_state[4])) <= 2
        assert "RODEX · LIVE AGENTS" in _wait_for_captured_text(
            tmux,
            tmux_socket,
            observer[0],
            "RODEX · LIVE AGENTS",
        )
        rendered_request = _wait_for_captured_text(
            tmux,
            tmux_socket,
            observer[0],
            exact_request,
        )
        rendered_request = _wait_for_captured_text(
            tmux,
            tmux_socket,
            observer[0],
            "ROOT TURN REQUEST · exact user message",
        )
        assert exact_request in rendered_request
        assert "REQUEST UNAVAILABLE" not in rendered_request

        child_turn_id = "00000000-0000-7000-8000-000000000002"
        publication = RodexAgentTracePublication(
            None,
            "test-v1",
            "2026-08-27T00:00:00Z",
            "complete",
            (
                RodexAgentTraceEvent(
                    ROOT_THREAD_ID,
                    "00000000-0000-7000-8000-000000000001",
                    1,
                    0,
                    "subagent_activity",
                    "2026-08-27T00:00:00Z",
                    TraceSubagentActivity(
                        CHILD_THREAD_ID,
                        "started",
                        "/root/live-review",
                    ),
                ),
                RodexAgentTraceEvent(
                    CHILD_THREAD_ID,
                    child_turn_id,
                    1,
                    0,
                    "turn_started",
                    "2026-08-27T00:00:01Z",
                ),
                RodexAgentTraceEvent(
                    CHILD_THREAD_ID,
                    child_turn_id,
                    2,
                    0,
                    "tool_call",
                    "2026-08-27T00:00:02Z",
                    TraceToolCall(
                        "tool-1",
                        None,
                        "exec",
                        "completed",
                        0,
                        10,
                        "rollout_reference",
                        "output",
                    ),
                ),
                RodexAgentTraceEvent(
                    CHILD_THREAD_ID,
                    child_turn_id,
                    3,
                    0,
                    "tool_call",
                    "2026-08-27T00:00:02Z",
                    TraceToolCall(
                        "tool-2",
                        None,
                        "exec",
                        "completed",
                        0,
                        10,
                        "rollout_reference",
                        "output",
                    ),
                ),
                RodexAgentTraceEvent(
                    CHILD_THREAD_ID,
                    child_turn_id,
                    4,
                    0,
                    "turn_completed",
                    "2026-08-27T00:00:03Z",
                ),
            ),
        )
        prepared = prepare_agent_trace_publication(publication)
        with open_rodex_transaction(database) as connection:
            receipt = publish_agent_trace_in_transaction(
                connection,
                1,
                prepared,
                model_name_ids={},
                reasoning_effort_name_ids={},
            )
        notify_agent_observer_trace_publication(
            event_socket,
            receipt.trace_publication_sequence,
            True,
        )
        captured = _wait_for_captured_text(
            tmux,
            tmux_socket,
            observer[0],
            "live-review finished",
        )
        assert "live-review finished" in captured
        assert "2 actions" in captured
        assert "1 action completed" not in captured

        subprocess.run(
            [
                tmux,
                "-S",
                str(tmux_socket),
                "send-keys",
                "-t",
                observer[0],
                "ls",
                "Enter",
            ],
            check=True,
        )
        time.sleep(0.05)
        assert "\nls\n" not in _capture_tmux_pane(tmux, tmux_socket, observer[0])

        tap.close()
        panes = _wait_for_tmux_panes(tmux, tmux_socket, 1)
        assert panes[0][0] == primary
    finally:
        if controller is not None:
            controller.close()
        with suppress(Exception):
            tap.close()
        subprocess.run(
            [tmux, "-S", str(tmux_socket), "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _wait_for_tmux_panes(
    tmux: str,
    tmux_socket: Path,
    count: int,
) -> list[tuple[str, str, str, str, str, str]]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                tmux,
                "-S",
                str(tmux_socket),
                "list-panes",
                "-t",
                "observer-test",
                "-F",
                "#{pane_id}|#{pane_input_off}|#{pane_current_command}|"
                "#{pane_top}|#{pane_height}|#{pane_active}",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        panes = [tuple(line.split("|")) for line in result.stdout.splitlines()]
        if len(panes) == count:
            return panes  # type: ignore[return-value]
        time.sleep(0.05)
    raise AssertionError(f"tmux did not reach {count} panes")


def _capture_tmux_pane(tmux: str, tmux_socket: Path, pane: str) -> str:
    return subprocess.run(
        [
            tmux,
            "-S",
            str(tmux_socket),
            "capture-pane",
            "-p",
            "-S",
            "-",
            "-t",
            pane,
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout


def _wait_for_captured_text(
    tmux: str,
    tmux_socket: Path,
    pane: str,
    expected: str,
) -> str:
    deadline = time.monotonic() + 3
    captured = ""
    while time.monotonic() < deadline:
        captured = _capture_tmux_pane(tmux, tmux_socket, pane)
        if expected in captured:
            return captured
        time.sleep(0.05)
    raise AssertionError(
        f"observer pane did not display {expected!r}; captured={captured!r}"
    )
