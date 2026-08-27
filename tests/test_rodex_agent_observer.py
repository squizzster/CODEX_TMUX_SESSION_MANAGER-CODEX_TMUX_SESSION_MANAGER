from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

from rodex.agent_observer import (
    AgentObserverPaneController,
    AgentObserverView,
    notify_agent_observer_trace_publication,
    observer_control_socket_path,
    project_subagent_activity_event,
)
from rodex.protocol_proxy import CodexProtocolEventTap
from rodex_registry import (
    RodexAgentTraceEvent,
    RodexAgentTracePublication,
    RodexAgentTraceSnapshot,
    TraceSubagentActivity,
    create_a_rodex_session,
)
from rodex_registry.agent_trace import publish_agent_trace_in_transaction
from rodex_sql import open_rodex_transaction

ROOT_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
CHILD_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f83")
OTHER_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f84")
TRACE_CURSOR = uuid.UUID("10000000-0000-4000-8000-000000000001")


def _spawn_event(
    *,
    method: str = "item/started",
    item_id: str = "call-spawn-1",
    activity_kind: str = "started",
) -> dict[str, object]:
    return {
        "emittedAtMs": 1787794770053,
        "method": method,
        "params": {
            "startedAtMs": 1787794770052,
            "threadId": str(ROOT_THREAD_ID),
            "turnId": "turn-1",
            "item": {
                "type": "subAgentActivity",
                "id": item_id,
                "agentPath": "/root/live-review",
                "agentThreadId": str(CHILD_THREAD_ID),
                "kind": activity_kind,
            },
        },
    }


def test_observed_subagent_activity_projection_is_exact_and_content_free() -> None:
    event = _spawn_event()
    event["params"]["item"]["private"] = "must not enter the observer"  # type: ignore[index]

    projected = project_subagent_activity_event(event)

    assert projected == {
        "schema": "rodex-agent-observer-v1",
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
    wrong_type = _spawn_event()
    wrong_type["params"]["item"]["type"] = "dynamicToolCall"  # type: ignore[index]
    missing_id = _spawn_event()
    missing_id["params"]["item"]["id"] = ""  # type: ignore[index]

    assert project_subagent_activity_event(wrong_method) is None
    assert project_subagent_activity_event(wrong_type) is None
    assert project_subagent_activity_event(missing_id) is None


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
        if operation == "display-message":
            return subprocess.CompletedProcess(command, 0, "/workspace\n", "")
        if operation == "split-window":
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    controller = AgentObserverPaneController(
        "/usr/bin/tmux",
        tmp_path / "tmux.sock",
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

    split = next(command for command in calls if command[3] == "split-window")
    assert split[4:14] == [
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
    assert "exec /usr/bin/python3 -m rodex.agent_observer" in split[-1]
    assert "--initial-event" in split[-1]
    assert str(TRACE_CURSOR) in split[-1]
    assert "subAgentActivity" in split[-1]
    assert "/root/live-review" in split[-1]
    assert [command[3:] for command in calls[-4:]] == [
        ["set-option", "-p", "-t", "%7", "@rodex_agent_observer_pane_id", "%9"],
        ["set-option", "-p", "-t", "%9", "@rodex_agent_observer_for", "%7"],
        ["select-pane", "-d", "-t", "%9"],
        ["select-pane", "-t", "%7"],
    ]
    assert sent == []


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
        if operation == "display-message":
            return subprocess.CompletedProcess(command, 0, "%9|%7|0\n", "")
        raise AssertionError(f"unexpected tmux mutation: {command}")

    controller = AgentObserverPaneController(
        "/usr/bin/tmux",
        tmp_path / "tmux.sock",
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

    assert not any(command[3] == "split-window" for command in calls)
    assert len(sent) == 1
    assert sent[0][0] == observer_control_socket_path(tmp_path / "events.sock")
    assert sent[0][1]["after_event_id"] == str(TRACE_CURSOR)
    assert sent[0][1]["item"]["id"] == "call-spawn-2"  # type: ignore[index]


def test_existing_observer_retries_live_event_until_control_socket_is_ready(
    tmp_path: Path,
) -> None:
    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        operation = command[3]
        if operation == "show-options":
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        if operation == "display-message":
            return subprocess.CompletedProcess(command, 0, "%9|%7|0\n", "")
        raise AssertionError(f"unexpected tmux mutation: {command}")

    event_socket = tmp_path / "events.sock"
    controller = AgentObserverPaneController(
        "/usr/bin/tmux",
        tmp_path / "tmux.sock",
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

    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.settimeout(2)
    try:
        receiver.bind(str(observer_control_socket_path(event_socket)))
        payload = json.loads(receiver.recv(4096))
    finally:
        receiver.close()
        controller.close()

    assert payload["item"]["id"] == "call-during-startup"
    assert payload["after_event_id"] == str(TRACE_CURSOR)


def test_observer_view_renders_only_exact_target_trace_metadata() -> None:
    initial = project_subagent_activity_event(_spawn_event())
    assert initial is not None
    initial["after_event_id"] = str(TRACE_CURSOR)
    view = AgentObserverView(
        root_thread_id=ROOT_THREAD_ID,
        rodex_session_id="1234567890abcdef",
        initial_event=initial,
    )
    target_event_id = uuid.UUID("10000000-0000-4000-8000-000000000002")
    terminal_event_id = uuid.UUID("10000000-0000-4000-8000-000000000004")
    snapshot = RodexAgentTraceSnapshot(
        trace_publication_sequence=8,
        trace_schema_version="rodex-agent-trace-v1",
        calculated_at_utc="2026-08-27T00:00:02Z",
        coverage_state="complete",
        durable_event_count=4,
        unrecognized_record_count=0,
        events=(
            {
                "event_id": str(target_event_id),
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
                "event_id": "10000000-0000-4000-8000-000000000003",
                "codex_thread_id": str(OTHER_THREAD_ID),
                "codex_turn_id": "turn-other",
                "event_kind": "command_execution",
                "event_time_utc": "2026-08-27T00:00:01Z",
                "detail": {"command_status": "completed"},
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

    rendered = "\n".join(lines)
    assert "gpt-5.6-luna" in rendered
    assert "effort=max" in rendered
    assert str(OTHER_THREAD_ID) not in rendered
    assert "turn completed" in rendered
    assert view.after_event_id == terminal_event_id
    assert view.monitoring is True
    view.accept_trace_publication_wake(8, caught_up=True)
    assert view.monitoring is False


def test_app_item_completion_cannot_suppress_the_final_durable_trace_read() -> None:
    initial = project_subagent_activity_event(_spawn_event())
    completed = project_subagent_activity_event(_spawn_event(method="item/completed"))
    assert initial is not None
    assert completed is not None
    view = AgentObserverView(
        root_thread_id=ROOT_THREAD_ID,
        rodex_session_id="1234567890abcdef",
        initial_event=initial,
    )

    view.accept_app_server_event(completed)

    assert view.monitoring is True
    terminal_event_id = uuid.UUID("10000000-0000-4000-8000-000000000004")
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            trace_publication_sequence=8,
            trace_schema_version="rodex-agent-trace-v1",
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
    view.accept_trace_publication_wake(9, caught_up=True)
    assert view.monitoring is False


def test_trace_publication_notification_is_a_nonblocking_datagram(tmp_path: Path) -> None:
    event_socket = tmp_path / "events.sock"
    control_socket = observer_control_socket_path(event_socket)
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(control_socket))
    receiver.settimeout(1)
    try:
        notify_agent_observer_trace_publication(event_socket, 17, True)
        payload = json.loads(receiver.recv(4096))
    finally:
        receiver.close()

    assert payload == {
        "schema": "rodex-agent-observer-v1",
        "kind": "trace_published",
        "trace_publication_sequence": 17,
        "caught_up": True,
    }


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_real_tmux_observer_is_not_a_shell_and_exits_with_its_runtime(
    tmp_path: Path,
) -> None:
    tmux = shutil.which("tmux")
    assert tmux is not None
    tmux_socket = tmp_path / "t.sock"
    event_socket = tmp_path / "e.sock"
    database = tmp_path / "r.sqlite3"
    create_a_rodex_session(database, codex_session_id=ROOT_THREAD_ID)
    tap = CodexProtocolEventTap(event_socket)
    controller: AgentObserverPaneController | None = None
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
        controller = AgentObserverPaneController(
            tmux,
            tmux_socket,
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
        controller.observe_protocol_event(_spawn_event())

        panes = _wait_for_tmux_panes(tmux, tmux_socket, 2)
        observer = next(pane for pane in panes if pane[0] != primary)
        primary_state = next(pane for pane in panes if pane[0] == primary)
        assert observer[1] == "1"
        assert observer[2] not in {"bash", "sh", "zsh"}
        assert observer[3] == "0"
        assert primary_state[5] == "1"
        assert abs(int(observer[4]) * 2 - int(primary_state[4])) <= 2
        assert "RODEX AGENT OBSERVER" in _wait_for_captured_text(
            tmux,
            tmux_socket,
            observer[0],
            "RODEX AGENT OBSERVER",
        )

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
            ),
        )
        with open_rodex_transaction(database) as connection:
            receipt = publish_agent_trace_in_transaction(
                connection,
                1,
                publication,
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
            "agent started",
        )
        assert "agent started" in captured

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
