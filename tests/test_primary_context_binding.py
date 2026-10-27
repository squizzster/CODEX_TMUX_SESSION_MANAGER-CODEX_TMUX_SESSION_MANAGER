"""Context projections share correlated primary authority through callback delivery."""

from __future__ import annotations

import json
from threading import Event, Thread

import pytest
from runtime_peer_fixtures import TEST_PEER, LiveTestProcess
from test_context_threshold_steering import ROOT_THREAD, SteeringHarness, compaction, turn_started
from test_rodex_protocol_proxy import rollout_token_count, token_usage_updated

from rodex.protocol_proxy import CodexContextStatusObserver, CodexProtocolProxy, ToolCallCounter


@pytest.fixture
def bound_context(tmp_path):
    steering = SteeringHarness(bind=False)
    steering.controller.bind_primary_thread(None, None)
    steering.controller.bind_root_thread(ROOT_THREAD)
    statuses = []
    percentages = []

    def percentage(thread_id, percent, generation):
        percentages.append((thread_id, percent, generation))
        steering.controller.observe_context_percent(thread_id, percent, connection_generation=generation)

    observer = CodexContextStatusObserver(
        statuses.append,
        on_context_percent=percentage,
        codex_sessions_root=tmp_path,
        rollout_poll_interval_seconds=0.005,
    )
    proxy = CodexProtocolProxy(
        tmp_path / "proxy.sock",
        tmp_path / "app.sock",
        ToolCallCounter(lambda _: None),
        peer_identity=TEST_PEER,
        app_server_process=LiveTestProcess(),
        native_tui_process=LiveTestProcess(),
        context_observer=observer,
        context_steering=steering.controller,
    )
    connection = object()
    assert proxy._claim_primary_connection(connection)
    try:
        yield proxy, observer, steering, connection, statuses, percentages
    finally:
        observer.close()
        steering.close()


def bind_main(proxy, connection, *, path=None):
    proxy._observe_primary_input({"id": 1, "method": "thread/resume", "params": {"threadId": ROOT_THREAD}}, connection)
    thread = {"id": ROOT_THREAD, "ephemeral": False}
    if path is not None:
        thread["path"] = str(path)
    proxy._observe_primary_response({"id": 1, "result": {"thread": thread}}, connection)
    return proxy._primary_context_generation


def usage(percent, thread_id=ROOT_THREAD):
    return json.loads(token_usage_updated(percent, thread_id=thread_id, context_window=100))


@pytest.mark.parametrize("ephemeral", [False, True])
@pytest.mark.parametrize("helper_first", [False, True])
def test_helpers_cannot_choose_context_or_steering_main(bound_context, ephemeral, helper_first):
    proxy, observer, steering, connection, statuses, percentages = bound_context
    helper = {"method": "thread/started", "params": {"thread": {"id": "helper", "ephemeral": ephemeral}}}
    if not helper_first:
        bind_main(proxy, connection)
    proxy._observe_primary_response(helper, connection)
    proxy._project_primary_context(helper, connection, connection_generation=proxy._primary_context_generation)
    proxy._project_primary_context(
        usage(99, "helper"), connection, connection_generation=proxy._primary_context_generation
    )
    helper_compaction = compaction("item/started")
    helper_compaction["params"]["threadId"] = "helper"
    proxy._project_primary_context(helper_compaction, connection, connection_generation=proxy._primary_context_generation)
    assert not observer._active_compaction_item_ids
    assert not steering.controller.snapshot.compacting
    assert percentages == [] and statuses == []
    if helper_first:
        assert observer._primary_thread_id is None
        bind_main(proxy, connection)
    proxy._project_primary_context(
        turn_started("turn-1"), connection, connection_generation=proxy._primary_context_generation
    )
    proxy._project_primary_context(usage(70), connection, connection_generation=proxy._primary_context_generation)
    assert observer._primary_thread_id == ROOT_THREAD
    assert "Context: 70%" in statuses[-1]
    assert steering.controller.snapshot.pending_turn_id == "turn-1"
    steering.deferred_calls[-1].fire()
    assert len(steering.requests) == 1


def test_resume_reply_starts_exact_rollout_without_start_notification(bound_context, tmp_path):
    proxy, observer, steering, connection, statuses, _percentages = bound_context
    rollout = tmp_path / f"rollout-{ROOT_THREAD}.jsonl"
    rollout.write_text(rollout_token_count(180_880) + "\n")
    delivered = Event()
    original = observer._on_context_percent

    def percentage(*args):
        original(*args)
        delivered.set()

    observer._on_context_percent = percentage
    bind_main(proxy, connection, path=rollout)
    assert delivered.wait(1)
    assert "Context: 70%" in statuses[-1]
    proxy._project_primary_context(
        turn_started("turn-1"), connection, connection_generation=proxy._primary_context_generation
    )
    assert steering.controller.snapshot.pending_turn_id == "turn-1"


@pytest.mark.parametrize("change", ["reconnect", "same_connection_selection"])
def test_stale_percentage_and_pending_dispatch_cannot_cross_binding_change(bound_context, change):
    proxy, observer, steering, connection, statuses, percentages = bound_context
    old_generation = bind_main(proxy, connection)
    old_revision = observer._binding_revision
    proxy._project_primary_context(
        turn_started("turn-1"), connection, connection_generation=proxy._primary_context_generation
    )
    proxy._project_primary_context(usage(75), connection, connection_generation=proxy._primary_context_generation)
    old_call = steering.deferred_calls[-1]
    if change == "reconnect":
        proxy._release_primary_connection(connection)
        new_connection = object()
        assert proxy._claim_primary_connection(new_connection)
        bind_main(proxy, new_connection)
    else:
        new_connection = connection
        proxy._observe_primary_input(
            {"id": 2, "method": "thread/resume", "params": {"threadId": ROOT_THREAD}}, connection
        )
        assert observer._primary_thread_id is None
        proxy._observe_primary_response({"id": 2, "error": {"code": -1, "message": "rejected"}}, connection)
    assert proxy._primary_context_generation != old_generation
    assert observer._primary_thread_id == ROOT_THREAD
    assert "Context: --" in statuses[-1]
    assert steering.controller.snapshot.context_percent is None
    assert old_call.cancelled
    old_call.fire(even_if_cancelled=True)
    assert steering.requests == []
    observer._publish_context_percent(ROOT_THREAD, 99, old_generation, old_revision)
    observer.observe_rollout_context_percent(ROOT_THREAD, 99, connection_generation=old_generation)
    steering.controller.observe_context_percent(ROOT_THREAD, 99, connection_generation=old_generation)
    assert len(percentages) == 1
    assert steering.controller.snapshot.context_percent is None
    proxy._project_primary_context(
        turn_started("new-turn"), new_connection, connection_generation=proxy._primary_context_generation
    )
    proxy._project_primary_context(usage(70), new_connection, connection_generation=proxy._primary_context_generation)
    assert steering.controller.snapshot.pending_turn_id == "new-turn"


def test_rollout_callback_delayed_at_delivery_is_revoked(bound_context, monkeypatch):
    proxy, observer, steering, connection, _statuses, percentages = bound_context
    generation = bind_main(proxy, connection)
    entered, resume = Event(), Event()
    original = observer._publish_context_percent

    def delayed(*args):
        entered.set()
        assert resume.wait(2)
        original(*args)

    monkeypatch.setattr(observer, "_publish_context_percent", delayed)
    worker = Thread(
        target=observer.observe_rollout_context_percent,
        args=(ROOT_THREAD, 75),
        kwargs={"connection_generation": generation},
    )
    worker.start()
    assert entered.wait(1)
    try:
        proxy._release_primary_connection(connection)
        replacement = object()
        assert proxy._claim_primary_connection(replacement)
        bind_main(proxy, replacement)
        proxy._project_primary_context(
            turn_started("turn-1"), replacement, connection_generation=proxy._primary_context_generation
        )
    finally:
        resume.set()
        worker.join(2)
    assert not worker.is_alive()
    assert percentages == []
    assert steering.controller.snapshot.context_percent is None


def test_input_eof_revokes_follower_compaction_and_same_thread_late_output(bound_context, tmp_path):
    proxy, observer, steering, connection, statuses, _percentages = bound_context
    rollout = tmp_path / f"rollout-{ROOT_THREAD}.jsonl"
    rollout.write_text("")
    bind_main(proxy, connection, path=rollout)
    stop = observer._rollout_stop
    follower = observer._rollout_thread
    proxy._project_primary_context(
        turn_started("turn-1"), connection, connection_generation=proxy._primary_context_generation
    )
    proxy._project_primary_context(
        compaction("item/started"), connection, connection_generation=proxy._primary_context_generation
    )
    proxy._invalidate_primary_binding(connection)
    assert stop.is_set()
    assert not observer._active_compaction_item_ids
    assert observer._rollout_thread is None
    assert observer._primary_thread_id is None
    assert steering.controller.snapshot.root_thread_id is None
    assert "Context: --" in statuses[-1]
    proxy._project_primary_context(usage(99), connection, connection_generation=proxy._primary_context_generation)
    assert steering.controller.snapshot.context_percent is None
    follower.join(1)
    assert not follower.is_alive()


def test_uncorrelated_and_helper_replies_cannot_supply_primary_rollout(bound_context, tmp_path):
    proxy, observer, _steering, connection, _statuses, _percentages = bound_context
    bind_main(proxy, connection)
    rollout = tmp_path / f"rollout-{ROOT_THREAD}.jsonl"
    rollout.write_text(rollout_token_count(180_880) + "\n")
    proxy._observe_primary_response(
        {"id": "unknown", "result": {"thread": {"id": ROOT_THREAD, "ephemeral": False, "path": str(rollout)}}}, connection
    )
    assert observer._rollout_thread is None
    proxy._observe_primary_input({"id": "helper", "method": "thread/start", "params": {"ephemeral": True}}, connection)
    proxy._observe_primary_response(
        {"id": "helper", "result": {"thread": {"id": "helper", "ephemeral": True, "path": str(rollout)}}}, connection
    )
    assert observer._rollout_thread is None


@pytest.mark.parametrize("second_selection_pending", [False, True])
def test_rejected_selection_restores_follower_from_accepted_metadata(bound_context, tmp_path, second_selection_pending):
    proxy, observer, _steering, connection, _statuses, percentages = bound_context
    rollout = tmp_path / f"rollout-{ROOT_THREAD}.jsonl"
    rollout.write_text("")
    if second_selection_pending:
        proxy._observe_primary_input(
            {"id": 2, "method": "thread/resume", "params": {"threadId": ROOT_THREAD}}, connection
        )
    old_generation = bind_main(proxy, connection, path=rollout)
    old_stop = observer._rollout_stop
    if not second_selection_pending:
        proxy._observe_primary_input(
            {"id": 2, "method": "thread/resume", "params": {"threadId": ROOT_THREAD}}, connection
        )
        assert old_stop.is_set()
    assert observer._rollout_thread is None
    proxy._observe_primary_response({"id": 2, "error": {"code": -1, "message": "rejected"}}, connection)
    assert observer._rollout_thread is not None
    assert proxy._primary_context_generation != old_generation
    delivered = Event()
    original = observer._on_context_percent

    def percentage(*args):
        original(*args)
        delivered.set()

    observer._on_context_percent = percentage
    rollout.write_text(rollout_token_count(180_880) + "\n")
    assert delivered.wait(1)
    assert percentages[-1][1] == 70


def test_deferred_protocol_projection_keeps_its_admitted_generation(bound_context):
    proxy, observer, steering, connection, statuses, _percentages = bound_context
    old_generation = bind_main(proxy, connection)
    proxy._observe_primary_input({"id": 2, "method": "thread/resume", "params": {"threadId": ROOT_THREAD}}, connection)
    proxy._observe_primary_response({"id": 2, "error": {"code": -1, "message": "rejected"}}, connection)
    proxy._project_primary_context(usage(99), connection, connection_generation=old_generation)
    assert statuses == []
    assert observer._latest_context_status.endswith("Context: -- | ")
    assert steering.controller.snapshot.context_percent is None
