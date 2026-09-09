"""Agent work counts drive real coordinator/pane/pipeline adapters; tmux is test-owned fiction."""

import json
import shlex
import subprocess
import uuid
from types import SimpleNamespace

import pytest

import rodex.agent_observer as observer_module
from rodex.agent_observer import (
    AgentObserverCoordinator,
    AgentObserverView,
    _ObserverEventDispatcher,
    _read_and_render_available_trace,
)
from rodex.interaction_pipeline import InteractionOperation, SessionInteractionPipeline
from rodex.observer_projection import project_agent_message_event
from rodex.observer_state import ObserverStateReducer
from rodex.tmux_session_capability import TmuxRuntimeCapability
from rodex_registry import RodexRuntimeId

ROOT = str(uuid.UUID(int=1))
FIRST = str(uuid.UUID(int=2))
SECOND = str(uuid.UUID(int=3))


def activity(target=FIRST, *, kind="started", method="item/started", item_id="spawn-1"):
    return {
        "method": method,
        "params": {
            "threadId": ROOT,
            "turnId": "root-turn",
            "item": {
                "type": "subAgentActivity",
                "id": item_id,
                "agentPath": "/root/test",
                "agentThreadId": target,
                "kind": kind,
            },
        },
    }


def turn(target=FIRST, *, method="turn/completed", turn_id="child-turn"):
    return {"method": method, "params": {"threadId": target, "turn": {"id": turn_id}}}


def invocation(*, tool="followupTask", item_id="followup-1", status="completed"):
    return {
        "method": "item/completed",
        "params": {
            "threadId": ROOT,
            "turnId": "root-turn",
            "item": {
                "type": "collabAgentToolCall",
                "id": item_id,
                "tool": tool,
                "prompt": "Test request",
                "status": status,
                "senderThreadId": ROOT,
                "receiverThreadIds": [FIRST],
            },
        },
    }


@pytest.fixture
def harness(tmp_path):
    panes = SimpleNamespace(observer=None, next_id=9, mutations=[], calls=[], trace_cursor=None)

    def run(command, **_options):
        panes.calls.append(command)
        output = ""
        if command[3] == "show-options":
            output = panes.observer or ""
        else:
            assert command[3] == "if-shell"
            action = shlex.split(command[-2])
            if action[0] == "display-message":
                if "pane_current_path" in action[-1]:
                    output = "%7|/workspace"
                elif panes.observer:
                    output = f"{panes.observer}|%7|0"
            else:
                panes.mutations.append(action)
                if action[0] == "split-window":
                    assert panes.observer is None
                    panes.observer = f"%{panes.next_id}"
                    panes.next_id += 1
                    output = panes.observer
                elif action[0] == "kill-pane":
                    assert action == ["kill-pane", "-t", panes.observer]
                    assert panes.observer != "%7"
                    panes.observer = None
        return subprocess.CompletedProcess(command, 0, output, "")

    pipeline = SessionInteractionPipeline()
    snapshots = []
    controller = AgentObserverCoordinator(
        "tmux",
        TmuxRuntimeCapability(tmp_path / "tmux.sock", "a" * 32, "$7", "%7", RodexRuntimeId.parse("1234567890abcdef")),
        "%7",
        tmp_path / "events.sock",
        runner=run,
        cursor_reader=lambda *_: panes.trace_cursor,
        event_sender=lambda _path, snapshot: snapshots.append(snapshot),
        interaction_pipeline=pipeline,
    )
    controller.activate(
        database_path=tmp_path / "registry.sqlite3",
        rodex_sessions_id=1,
        rodex_session_id="1234567890abcdef",
        root_thread_id=uuid.UUID(ROOT),
    )
    yield SimpleNamespace(
        controller=controller,
        panes=panes,
        pipeline=pipeline,
        snapshots=snapshots,
        state=controller._observer_state,
        send=controller.observe_protocol_event,
    )
    controller.close()


def test_last_of_two_agents_closes_the_pane_once_and_later_work_reopens(harness):
    harness.send(activity())
    original_pane = harness.panes.observer
    harness.send(activity(SECOND, item_id="spawn-2"))
    assert harness.state.running_agent_count == 2 and harness.panes.observer == original_pane
    harness.send(turn())
    assert harness.state.running_agent_count == 1 and harness.panes.observer == original_pane
    harness.send(turn(SECOND))
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None
    harness.send(turn(SECOND))
    assert harness.state.running_agent_count == 0
    assert sum(action[0] == "kill-pane" for action in harness.panes.mutations) == 1
    harness.send(turn(method="turn/started", turn_id="later-turn"))
    assert harness.state.running_agent_count == 1 and harness.panes.observer != original_pane
    assert harness.panes.observer is not None
    assert any(record.operation == InteractionOperation.CLOSE for record in harness.pipeline.records)
    assert all(not record.start_model_turn for record in harness.pipeline.records)


@pytest.mark.parametrize("method", ["item/started", "item/completed"])
def test_spawn_evidence_adds_one_agent_regardless_of_tool_item_completion(harness, method):
    harness.send(activity(method=method))
    harness.send(activity(method="item/completed"))
    assert harness.state.running_agent_count == 1 and harness.panes.observer is not None
    harness.send(turn())
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None


@pytest.mark.parametrize("invocation_first", [True, False])
def test_same_agent_followup_reopens_after_completion_in_either_correlation_order(harness, invocation_first):
    harness.send(activity())
    harness.send(turn())
    events = [invocation(), activity(kind="interacted", method="item/completed", item_id="followup-1")]
    for event in events if invocation_first else reversed(events):
        harness.send(event)
    assert harness.state.running_agent_count == 1 and harness.panes.observer is not None
    harness.send(turn(method="turn/started", turn_id="followup-turn"))
    harness.send(turn(turn_id="followup-turn"))
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None


@pytest.mark.parametrize("followup", [False, True])
def test_delayed_activity_and_duplicate_request_cannot_resurrect_finished_work(harness, followup):
    harness.send(activity())
    if followup:
        harness.send(turn())
        harness.send(invocation())
    harness.send(turn(method="turn/started"))
    harness.send(turn())
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None
    if followup:
        harness.send(activity(kind="interacted", method="item/completed", item_id="followup-1"))
        harness.send(invocation())
    else:
        harness.send(activity(method="item/completed"))
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None


def test_old_terminal_activity_cannot_remove_new_followup_work(harness):
    harness.send(activity())
    harness.send(turn())
    harness.send(invocation())
    harness.send(turn(method="turn/started", turn_id="new-turn"))
    harness.send(activity(kind="completed", method="item/completed"))
    assert harness.state.running_agent_count == 1 and harness.panes.observer is not None
    harness.send(turn(turn_id="new-turn"))
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None


@pytest.mark.parametrize(
    "start_event",
    [
        turn(method="turn/started"),
        {"method": "thread/status/changed", "params": {"threadId": FIRST, "status": {"type": "active"}}},
        invocation(),
    ],
)
@pytest.mark.parametrize("completion_event", [turn(), activity(kind="completed", method="item/completed")])
def test_reopened_pane_can_display_current_agent_work_without_replaying_its_original_request(
    harness, start_event, completion_event
):
    spawn = invocation(tool="spawnAgent", item_id="spawn-1")
    spawn["method"] = "item/started"
    spawn["params"]["item"]["prompt"] = "Original request must not reappear"
    harness.send(spawn)
    harness.send(activity())
    harness.send(completion_event)
    harness.send(start_event)
    assert "Original request must not reappear" not in json.dumps(harness.snapshots[-1])
    view = AgentObserverView(root_thread_id=uuid.UUID(ROOT), initial_event={})
    view.accept_observer_state_snapshot(harness.snapshots[-1])
    assert view.monitoring and view.target_thread_ids == {FIRST}
    message = project_agent_message_event(
        {
            "method": "item/completed",
            "params": {
                "threadId": FIRST,
                "turnId": "child-turn",
                "item": {
                    "type": "agentMessage",
                    "id": "new-message",
                    "text": "Current agent work",
                    "phase": "commentary",
                },
            },
        }
    )
    assert message is not None
    assert "Current agent work" in "\n".join(view.accept_agent_message_event(message))
    assert view.target_turn_keys == ((FIRST, "child-turn"),)
    assert view._root_request_text_by_activity == {} and view._activity_items == {}


def test_reopened_view_starts_its_trace_read_after_old_agent_history(harness, monkeypatch):
    harness.send(activity())
    harness.send(turn())
    old_id, cursor, current_id = (uuid.UUID(int=value) for value in (10, 20, 30))
    harness.panes.trace_cursor = cursor
    harness.send(turn(method="turn/started", turn_id="new-turn"))
    view = AgentObserverView(root_thread_id=uuid.UUID(ROOT), initial_event={})
    view.accept_observer_state_snapshot(harness.snapshots[-1])
    history = (
        {
            "event_id": str(old_id),
            "codex_thread_id": ROOT,
            "event_kind": "subagent_activity",
            "detail": {
                "target_codex_thread_id": FIRST,
                "agent_path": "/root/test",
                "activity_kind": "started",
                "collaboration_invocation": {
                    "source_call_id": "old-spawn",
                    "tool_name": "collaboration.spawn_agent",
                    "prompt": "Old request must not replay",
                },
            },
        },
        {
            "event_id": str(current_id),
            "codex_thread_id": FIRST,
            "codex_turn_id": "new-turn",
            "event_kind": "turn_started",
            "event_time_utc": "2026-09-09T00:00:00Z",
            "detail": None,
        },
    )
    reads = []
    lines = []

    def read_trace(_session, _database, *, after_event_id, limit):
        reads.append(after_event_id)
        return SimpleNamespace(
            events=tuple(
                event for event in history if after_event_id is None or uuid.UUID(event["event_id"]) > after_event_id
            )
        )

    monkeypatch.setattr(observer_module, "read_rodex_agent_trace", read_trace)
    monkeypatch.setattr(observer_module, "read_rodex_agent_observer_turn_evidence", lambda *_: ())
    monkeypatch.setattr(observer_module, "_print_lines", lines.extend)
    _read_and_render_available_trace(view, 1, harness.controller._database_path, flush_terminal_events=False)
    assert reads == [cursor] and view.after_event_id == current_id
    assert "Old request must not replay" not in "\n".join(lines)
    assert view._activity_items == {} and view.target_turn_keys == ((FIRST, "new-turn"),)


@pytest.mark.parametrize("finish_first", [True, False])
def test_plain_agent_messages_neither_start_nor_finish_work(harness, finish_first):
    harness.send(activity())
    if finish_first:
        harness.send(turn())
    harness.send(invocation(tool="sendMessage", item_id="message-1"))
    harness.send(activity(kind="interacted", method="item/completed", item_id="message-1"))
    assert harness.state.running_agent_count == (0 if finish_first else 1)
    assert (harness.panes.observer is None) == finish_first


def test_root_completion_child_message_and_stale_child_turn_do_not_close_active_work(harness):
    harness.send(activity())
    harness.send(turn(method="turn/started", turn_id="newer-turn"))
    harness.send(turn(ROOT))
    harness.send(turn(turn_id="older-turn"))
    harness.send(
        {
            "method": "item/completed",
            "params": {
                "threadId": FIRST,
                "turnId": "newer-turn",
                "item": {"id": "answer", "type": "agentMessage", "text": "Finished writing", "phase": "final"},
            },
        }
    )
    assert harness.state.running_agent_count == 1 and harness.panes.observer is not None
    harness.send(turn(turn_id="newer-turn"))
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None


def test_unrelated_protocol_deltas_do_not_query_panes_while_an_agent_is_running(harness):
    harness.send(activity())
    before = len(harness.panes.calls)
    for _ in range(10):
        harness.send({"method": "item/agentMessage/delta", "params": {"threadId": ROOT, "delta": "Text"}})
    assert len(harness.panes.calls) == before and harness.state.running_agent_count == 1


@pytest.mark.parametrize("kind", ["completed", "failed", "aborted", "shutdown"])
def test_explicit_terminal_activity_closes_without_waiting_for_another_event(harness, kind):
    harness.send(activity())
    harness.send(activity(kind=kind, method="item/completed"))
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None


@pytest.mark.parametrize("status", ["idle", "notLoaded", "systemError"])
def test_inactive_status_closes_and_known_active_status_reopens(harness, status):
    harness.send(activity())
    harness.send({"method": "thread/status/changed", "params": {"threadId": FIRST, "status": {"type": status}}})
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None
    harness.send({"method": "thread/status/changed", "params": {"threadId": FIRST, "status": {"type": "active"}}})
    assert harness.state.running_agent_count == 1 and harness.panes.observer is not None


def test_unknown_child_events_and_failed_followup_cannot_open_an_idle_observer(harness):
    harness.send(turn(method="turn/started"))
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None
    harness.send(activity())
    harness.send(turn())
    harness.send(invocation(status="failed"))
    harness.send(activity(kind="interacted", method="item/completed", item_id="followup-1"))
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None


def test_disconnect_closes_and_forgets_old_agent_identity(harness):
    harness.send(activity())
    harness.controller.reset_after_disconnect()
    assert harness.state.running_agent_count == 0 and harness.panes.observer is None
    harness.send(turn(method="turn/started"))
    assert harness.state.running_agent_count == 0
    harness.send(activity())
    assert harness.state.running_agent_count == 1 and harness.panes.observer is not None


def test_presentation_pruning_cannot_change_the_running_count():
    state = ObserverStateReducer.producer()
    state.start_agent_work(FIRST, "turn-1")
    state.start_agent_work(FIRST, "turn-2")
    state.prune_target(FIRST)
    assert state.running_agent_count == 1
    assert not state.finish_agent_work(FIRST, "turn-1")
    assert state.finish_agent_work(FIRST, "turn-2")
    assert not state.finish_agent_work(FIRST, "turn-2")
    assert state.running_agent_count == 0 and state.knows_agent(FIRST)


def test_closing_a_pane_discards_queued_snapshots_without_closing_the_dispatcher():
    dispatcher = _ObserverEventDispatcher()
    dispatcher._events.put_nowait((0, None, {}))
    dispatcher.discard_pending()
    assert dispatcher._events.empty() and dispatcher._generation == 1
    assert not dispatcher._closed
    dispatcher.close()
