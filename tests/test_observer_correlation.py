"""Bounded correlation retires prompts independently of event arrival order."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from test_observer_agent_lifecycle import FIRST, ROOT, SECOND, activity, invocation, turn
from test_observer_agent_lifecycle import harness as harness

import rodex.observer_state as state_module
from rodex.observer_contract import (
    OBSERVER_CORRELATION_METADATA_LIMIT,
    OBSERVER_CORRELATION_PAYLOAD_LIMIT,
    OBSERVER_CORRELATION_PAYLOAD_MAX_BYTES,
    OBSERVER_CORRELATION_RETENTION_SECONDS,
)


def private_metadata(state):
    return json.dumps([asdict(record) for record in state._correlations.values()], default=sorted)


def payload_bytes(state):
    return sum(
        len(json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode())
        for event in state._collaboration_invocations.values()
    )


@pytest.mark.parametrize("invocation_first", [False, True])
def test_192_completed_requests_release_private_prompts_in_both_orders(harness, invocation_first):
    prompt = "PRIVATE-CORRELATION-PAYLOAD-" + "x" * 1024
    for number in range(192):
        item_id = f"spawn-{number}"
        request = invocation(tool="spawnAgent", item_id=item_id)
        request["params"]["item"]["prompt"] = prompt
        events = [request, activity(item_id=item_id, method="item/completed")]
        for event in events if invocation_first else reversed(events):
            harness.send(event)
        assert harness.state.running_agent_count == 1
        assert harness.state._collaboration_invocations == {}
        harness.send(turn(method="turn/started", turn_id=f"turn-{number}"))
        harness.send(turn(turn_id=f"turn-{number}"))
        assert harness.state.running_agent_count == 0
    assert prompt not in private_metadata(harness.state)
    assert len(harness.state._correlations) == 192
    assert all(key[:2] == (0, ROOT) for key in harness.state._correlations)


@pytest.mark.parametrize("ordering", ["activity-finish-invocation", "invocation-finish-activity"])
def test_counterpart_after_target_completion_does_not_retain_or_resurrect(harness, ordering):
    harness.send(activity(item_id="seed"))
    harness.send(turn())
    request = invocation(item_id="followup-late")
    request["params"]["item"]["prompt"] = "late-private-prompt"
    child_activity = activity(item_id="followup-late", kind="interacted", method="item/completed")
    if ordering == "activity-finish-invocation":
        harness.send(turn(method="turn/started"))
        harness.send(child_activity)
        harness.send(turn())
        harness.send(request)
    else:
        harness.send(request)
        harness.send(turn())
        harness.send(child_activity)
    assert harness.state.running_agent_count == 0
    assert harness.state._collaboration_invocations == {}
    assert "late-private-prompt" not in private_metadata(harness.state)
    for _ in range(3):
        harness.send(request)
        harness.send(child_activity)
    assert harness.state.running_agent_count == 0
    assert harness.state._collaboration_invocations == {}


def test_invocation_only_target_index_retires_payload_without_activity(harness):
    harness.send(activity())
    harness.send(turn())
    request = invocation()
    harness.send(request)
    assert harness.state.running_agent_count == 1
    assert harness.state._collaboration_invocations
    harness.send(turn())
    assert harness.state._collaboration_invocations == {}
    assert "Test request" not in private_metadata(harness.state)


def test_multi_recipient_retirement_preserves_unresolved_target(harness):
    for target, item_id in ((FIRST, "seed-first"), (SECOND, "seed-second")):
        harness.send(activity(target, item_id=item_id))
    request = invocation(tool="sendMessage", item_id="both")
    request["params"]["item"]["receiverThreadIds"] = [FIRST, SECOND]
    request["params"]["item"]["prompt"] = "shared-private-prompt"
    harness.send(request)
    assert harness.state._collaboration_invocations
    harness.send(turn(FIRST))
    assert harness.state.running_agent_count == 1
    assert harness.state._collaboration_invocations
    snapshot = harness.state.snapshot()
    retained = next(
        event for event in snapshot["state"]["tombstones"] if event["kind"] == "app_server_collaboration_invocation"
    )
    assert retained["item"]["receiver_thread_ids"] == [SECOND]
    harness.send(activity(SECOND, item_id="both", kind="interacted", method="item/completed"))
    assert harness.state._collaboration_invocations == {}
    assert "shared-private-prompt" not in private_metadata(harness.state)
    harness.send(turn(SECOND))
    assert harness.state.running_agent_count == 0


@pytest.mark.parametrize("large_payload", [False, True])
def test_missing_counterparts_have_bounded_private_storage(harness, large_payload):
    for number in range(OBSERVER_CORRELATION_METADATA_LIMIT + 10):
        request = invocation(item_id=f"missing-{number}")
        request["params"]["item"]["prompt"] = ("💬" * 16_384) if large_payload else "unmatched prompt"
        harness.send(request)
        assert len(harness.state._collaboration_invocations) <= OBSERVER_CORRELATION_PAYLOAD_LIMIT
        assert payload_bytes(harness.state) <= OBSERVER_CORRELATION_PAYLOAD_MAX_BYTES
        assert len(harness.state._correlations) <= OBSERVER_CORRELATION_METADATA_LIMIT
    assert harness.state.snapshot()["overflow"]["state_complete"] is False
    assert len(harness.state._invocation_targets[FIRST]) <= OBSERVER_CORRELATION_METADATA_LIMIT


@pytest.mark.parametrize("current_work", ["unavailable", "idle", "newer-turn"])
@pytest.mark.parametrize("expiry", ["time", "capacity"])
def test_evicted_request_needs_current_work_evidence_without_old_prompt_attribution(
    harness, monkeypatch, current_work, expiry
):
    now = [0.0]
    harness.state._monotonic = lambda: now[0]
    if expiry == "capacity":
        monkeypatch.setattr(state_module, "OBSERVER_CORRELATION_METADATA_LIMIT", 2)
    harness.send(activity())
    harness.send(turn())
    request = invocation(item_id="old-request")
    request["params"]["item"]["prompt"] = "old-prompt-must-not-return"
    harness.send(request)
    harness.send(turn())
    if expiry == "time":
        now[0] = OBSERVER_CORRELATION_RETENTION_SECONDS + 1
    else:
        for number in range(3):
            harness.send(invocation(tool="sendMessage", item_id=f"other-{number}"))
    reads = []

    def active_turn_reader(target):
        reads.append(target)
        return current_work != "unavailable", "current-turn" if current_work == "newer-turn" else None

    harness.state.bind_active_turn_reader(active_turn_reader)
    harness.send(request)
    assert reads == [FIRST]
    assert harness.state.running_agent_count == (1 if current_work == "newer-turn" else 0)
    assert harness.state._collaboration_invocations == {}
    assert "old-prompt-must-not-return" not in json.dumps(harness.state.snapshot())
    if current_work == "newer-turn":
        active = harness.state._active_agent_work[FIRST]
        assert active.turn_id == "current-turn" and active.request_id is None
    harness.send(activity(item_id="old-request", kind="interacted", method="item/completed"))
    assert "old-prompt-must-not-return" not in json.dumps(harness.state.snapshot())
    assert harness.state.snapshot()["overflow"]["state_complete"] is False


def test_fresh_authoritative_turn_continues_after_correlation_coverage_loss(harness, monkeypatch):
    monkeypatch.setattr(state_module, "OBSERVER_CORRELATION_METADATA_LIMIT", 1)
    harness.send(activity())
    harness.send(turn())
    harness.send(invocation(tool="sendMessage", item_id="evict-seed"))
    harness.send(invocation(item_id="new-followup"))
    assert harness.state.running_agent_count == 0
    harness.send(turn(method="turn/started", turn_id="new-current-turn"))
    assert harness.state.running_agent_count == 1
    assert harness.state._active_agent_work[FIRST].turn_id == "new-current-turn"
    assert harness.state._active_agent_work[FIRST].request_id is None
    harness.send(turn(turn_id="new-current-turn"))
    assert harness.state.running_agent_count == 0


def test_reset_clears_payloads_metadata_indexes_and_fences_old_source(harness):
    harness.controller.bind_protocol_source("source-old", active_turn_reader=lambda _: (True, "old-turn"))
    harness.controller.observe_protocol_event(activity(), source_generation="source-old")
    harness.controller.observe_protocol_event(invocation(), source_generation="source-old")
    assert harness.state._correlations
    harness.controller.reset_after_disconnect()
    assert harness.state.epoch == 1
    assert not harness.state._collaboration_invocations
    assert not harness.state._correlations
    assert not harness.state._invocation_targets
    harness.controller.reset_after_disconnect()
    assert harness.state.epoch == 1
    harness.controller.bind_protocol_source("source-new", active_turn_reader=lambda _: (True, "new-turn"))
    harness.controller.observe_protocol_event(invocation(), source_generation="source-old")
    assert not harness.state._correlations
    harness.controller.observe_protocol_event(activity(), source_generation="source-new")
    assert harness.state.running_agent_count == 1
    assert all(key[:2] == (1, ROOT) for key in harness.state._correlations)


def test_wrong_root_cannot_enter_current_correlation_scope(harness):
    request = invocation()
    request["params"]["threadId"] = SECOND
    request["params"]["item"]["senderThreadId"] = SECOND
    harness.send(request)
    assert harness.state._correlations == {}


@pytest.mark.parametrize("newer_work", [False, True])
def test_terminal_evidence_retires_invocation_only_payload_without_clearing_newer_work(harness, newer_work):
    harness.send(activity())
    harness.send(turn(turn_id="old-turn"))
    request = invocation(tool="sendMessage", item_id="message-after-idle")
    harness.send(request)
    assert harness.state._collaboration_invocations
    if newer_work:
        harness.send(turn(method="turn/started", turn_id="newer-turn"))
    harness.send(turn(turn_id="old-turn"))
    assert bool(harness.state._collaboration_invocations) is newer_work
    assert harness.state.running_agent_count == int(newer_work)
    if newer_work:
        harness.send(turn(turn_id="newer-turn"))
    assert not harness.state._collaboration_invocations


def test_recovery_reader_and_projection_are_fenced_to_verified_primary_source(harness, tmp_path):
    from runtime_peer_fixtures import TEST_PEER, LiveTestProcess

    from rodex.protocol_proxy import CodexProtocolEventTap, CodexProtocolProxy, ToolCallCounter

    tap = CodexProtocolEventTap(tmp_path / "event.sock", peer_identity=TEST_PEER)
    proxy = CodexProtocolProxy(
        tmp_path / "proxy.sock",
        tmp_path / "app.sock",
        ToolCallCounter(lambda _: None),
        peer_identity=TEST_PEER,
        app_server_process=LiveTestProcess(),
        native_tui_process=LiveTestProcess(),
        agent_observer=harness.controller,
        event_tap=tap,
    )
    connection = object()
    assert proxy._claim_primary_connection(connection)
    epoch = proxy._activate_primary_source(connection)
    generation = proxy._primary_observer_generation
    old_reader = harness.state._active_turn_reader

    def deliver(event):
        tap.publish_protocol_event(json.dumps(event), event, source_epoch=epoch)
        proxy._project_primary_agent_observer(event, connection, source_generation=generation)

    try:
        deliver(activity())
        deliver(turn())
        request = invocation(item_id="old-duplicate")
        deliver(request)
        deliver(turn())
        for record in harness.state._correlations.values():
            record.expires_at = 0
        # Native lifecycle evidence can recover current work even when its local
        # observer projection was missed; an old request cannot supply causality.
        current = turn(method="turn/started", turn_id="native-current-turn")
        tap.publish_protocol_event(json.dumps(current), current, source_epoch=epoch)
        deliver(request)
        work = harness.state._active_agent_work[FIRST]
        assert work.turn_id == "native-current-turn" and work.request_id is None
        assert old_reader(FIRST) == (True, "native-current-turn")
        proxy._invalidate_primary_binding(connection)
        tap.reset_after_disconnect(source_epoch=epoch)
        assert harness.state.running_agent_count == 0
        assert old_reader(FIRST) == (False, None)
        proxy._release_primary_connection(connection)
        replacement = object()
        assert proxy._claim_primary_connection(replacement)
        proxy._activate_primary_source(replacement)
        assert old_reader(FIRST) == (False, None)
        proxy._project_primary_agent_observer(activity(), connection, source_generation=generation)
        proxy._project_primary_agent_observer(activity(), replacement, source_generation=generation)
        assert not harness.state._correlations
    finally:
        tap.close()


def test_retained_payload_budget_includes_final_correlation_publication(harness, monkeypatch):
    from rodex.observer_projection import project_collaboration_invocation_event

    harness.send(
        {
            "method": "item/completed",
            "params": {
                "threadId": ROOT,
                "turnId": "root-turn",
                "item": {
                    "type": "userMessage",
                    "id": "parent",
                    "content": [{"type": "text", "text": "Exact parent request"}],
                },
            },
        }
    )
    harness.send(activity(item_id="tight-budget"))
    request = invocation(item_id="tight-budget")
    projected = project_collaboration_invocation_event(request)
    budget = len(json.dumps(projected, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode())
    monkeypatch.setattr(state_module, "OBSERVER_CORRELATION_PAYLOAD_MAX_BYTES", budget)
    harness.send(request)
    assert payload_bytes(harness.state) == budget
    retained = next(iter(harness.state._collaboration_invocations.values()))
    assert "root_request_context_follows" not in retained
    assert any(event["kind"] == "root_request_context" for event in harness.state.snapshot()["state"]["events"])


def test_after_bound_source_loss_even_a_missing_source_token_is_rejected(harness):
    harness.controller.bind_protocol_source("old", active_turn_reader=lambda _: (True, "turn"))
    harness.controller.reset_after_disconnect()
    harness.send(activity())
    assert harness.state._correlations == {}
    harness.controller.bind_protocol_source("new", active_turn_reader=lambda _: (True, "turn"))
    harness.controller.reset_protocol_source("old")
    harness.controller.observe_protocol_event(activity(), source_generation="new")
    assert harness.state.running_agent_count == 1
