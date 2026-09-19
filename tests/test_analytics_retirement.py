"""Retirement consumes accepted work on the singular coordinator."""

from threading import Event

import pytest
from test_rodex_analytics import FakeAnalyticsTask, _config, _pending_config

import rodex.analytics as analytics
from rodex.analytics import SharedAnalyticsCoordinator


def test_retirement_preserves_completion_inside_debounce_window(tmp_path):
    config = _config(tmp_path)
    worker = FakeAnalyticsTask()
    clock = [0.0]
    coordinator = SharedAnalyticsCoordinator(worker_factory=lambda _: worker, monotonic=lambda: clock[0])
    coordinator.reserve(_pending_config(config))
    coordinator.activate(config)
    runtime_id = str(config.runtime_id)
    coordinator._reconcile(runtime_id)
    event = {"method": "turn/completed", "params": {"threadId": str(config.codex_session_id)}}
    coordinator.observe_protocol_event(runtime_id, event)
    coordinator.retire(runtime_id)
    selected, retired, _timeout = coordinator._select_work()
    assert selected == runtime_id
    assert not retired
    coordinator._reconcile(selected)
    assert worker.events == [event]
    assert worker.batches[-1].full_reconcile


def test_retirement_retries_append_and_reports_exhaustion(tmp_path):
    config = _config(tmp_path)
    worker = FakeAnalyticsTask(result="pending_append")
    clock = [0.0]
    coordinator = SharedAnalyticsCoordinator(worker_factory=lambda _: worker, monotonic=lambda: clock[0])
    coordinator.reserve(_pending_config(config))
    coordinator.activate(config)
    runtime_id = str(config.runtime_id)
    coordinator._reconcile(runtime_id)
    coordinator.retire(runtime_id)
    selected, retired, _ = coordinator._select_work()
    assert selected == runtime_id and not retired
    coordinator._reconcile(selected)
    clock[0] = 100
    selected, retired, _ = coordinator._select_work()
    assert selected is None
    assert retired
    for retirement in retired:
        coordinator._finish_retirement(*retirement)
    outcome = coordinator.retirement_outcome(runtime_id)
    assert outcome.state == "incomplete"
    assert outcome.diagnostic_code == "analytics_retirement_incomplete"


def test_late_append_converges_before_retirement_and_seals_new_events(tmp_path):
    config = _config(tmp_path)
    worker = FakeAnalyticsTask(result="pending_append")
    clock = [0.0]
    coordinator = SharedAnalyticsCoordinator(worker_factory=lambda _: worker, monotonic=lambda: clock[0])
    runtime_id = str(config.runtime_id)
    coordinator.reserve(_pending_config(config))
    coordinator.activate(config)
    event = {"method": "turn/completed", "params": {"threadId": str(config.codex_session_id)}}
    coordinator.observe_protocol_event(runtime_id, event)
    coordinator.retire(runtime_id)
    coordinator.observe_protocol_event(runtime_id, event)
    coordinator._reconcile(runtime_id)
    assert worker.events == [event]
    clock[0] = 0.6
    worker.result = "up_to_date"
    coordinator._reconcile(runtime_id)
    selected, retired, _ = coordinator._select_work()
    assert selected is None
    for retirement in retired:
        coordinator._finish_retirement(*retirement)
    assert coordinator.retirement_outcome(runtime_id).state == "complete"
    assert coordinator.retirement_outcome(runtime_id).health_persisted


def test_inactive_retirement_never_constructs_analyzer(tmp_path):
    config = _config(tmp_path)
    coordinator = SharedAnalyticsCoordinator(worker_factory=lambda _: pytest.fail("inactive analyzer created"))
    runtime_id = str(config.runtime_id)
    coordinator.reserve(_pending_config(config))
    coordinator.retire(runtime_id)
    _, retired, _ = coordinator._select_work()
    for retirement in retired:
        coordinator._finish_retirement(*retirement)
    assert coordinator.retirement_outcome(runtime_id).state == "inactive"


def test_blocked_poll_outlives_close_budget_without_losing_its_owner(tmp_path, monkeypatch):
    config = _config(tmp_path)
    worker = FakeAnalyticsTask()
    entered, release = Event(), Event()
    original = worker.poll_once

    def blocked(batch):
        entered.set()
        assert release.wait(3)
        return original(batch)

    worker.poll_once = blocked
    monkeypatch.setattr(analytics, "ANALYTICS_CLOSE_WAIT_SECONDS", 0.02)
    coordinator = SharedAnalyticsCoordinator(worker_factory=lambda _: worker)
    coordinator.reserve(_pending_config(config))
    coordinator.activate(config)
    coordinator.start()
    thread = coordinator._thread
    try:
        assert entered.wait(1)
        coordinator.retire(str(config.runtime_id))
        assert coordinator.close() is False
        assert coordinator._thread is thread and thread.is_alive()
        assert coordinator.retirement_outcome(str(config.runtime_id)).state == "pending"
        with pytest.raises(RuntimeError):
            coordinator.start()
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert coordinator.close() is True
        assert coordinator.retirement_outcome(str(config.runtime_id)).state == "complete"
    finally:
        release.set()
        thread.join(3)


def test_unavailable_storage_keeps_incomplete_in_memory_outcome(tmp_path, caplog):
    config = _config(tmp_path)
    clock = [0.0]
    worker = FakeAnalyticsTask(result="clean_replay")

    def unavailable(_code=None):
        raise OSError("database unavailable")

    worker.mark_stopped = unavailable
    coordinator = SharedAnalyticsCoordinator(worker_factory=lambda _: worker, monotonic=lambda: clock[0])
    runtime_id = str(config.runtime_id)
    coordinator.reserve(_pending_config(config))
    coordinator.activate(config)
    coordinator._reconcile(runtime_id)
    coordinator.retire(runtime_id)
    clock[0] = 100
    _, retired, _ = coordinator._select_work()
    for retirement in retired:
        coordinator._finish_retirement(*retirement)
    outcome = coordinator.retirement_outcome(runtime_id)
    assert outcome.state == "incomplete"
    assert not outcome.health_persisted
    assert "analytics retirement" in caplog.text
    assert "health_persisted=False" in caplog.text


def test_global_close_cannot_claim_producers_have_stopped(tmp_path):
    config = _config(tmp_path)
    clock = [0.0]
    worker = FakeAnalyticsTask()
    coordinator = SharedAnalyticsCoordinator(worker_factory=lambda _: worker, monotonic=lambda: clock[0])
    runtime_id = str(config.runtime_id)
    coordinator.reserve(_pending_config(config))
    coordinator.activate(config)
    coordinator._reconcile(runtime_id)
    assert coordinator.close() is False
    clock[0] = 1.0
    coordinator._reconcile(runtime_id)
    assert not coordinator._runtimes[runtime_id].retirement_complete
    clock[0] = 100
    _, retired, _ = coordinator._select_work()
    for retirement in retired:
        coordinator._finish_retirement(*retirement)
    assert coordinator.retirement_outcome(runtime_id).state == "incomplete"


def test_quiescence_during_shutdown_poll_requires_a_new_final_generation(tmp_path):
    config = _config(tmp_path)
    clock = [0.0]
    worker = FakeAnalyticsTask()
    coordinator = SharedAnalyticsCoordinator(worker_factory=lambda _: worker, monotonic=lambda: clock[0])
    runtime_id = str(config.runtime_id)
    coordinator.reserve(_pending_config(config))
    coordinator.activate(config)
    coordinator._reconcile(runtime_id)
    assert coordinator.close() is False
    original_poll = worker.poll_once
    event = {"method": "turn/completed", "params": {"threadId": str(config.codex_session_id)}}

    def finish_producer_after_snapshot(batch):
        coordinator.observe_protocol_event(runtime_id, event)
        coordinator.retire(runtime_id)
        return original_poll(batch)

    worker.poll_once = finish_producer_after_snapshot
    clock[0] = 1.0
    coordinator._reconcile(runtime_id)
    assert not coordinator._runtimes[runtime_id].retirement_complete
    assert coordinator._runtimes[runtime_id].full_reconcile
    worker.poll_once = original_poll
    clock[0] = 2.0
    coordinator._reconcile(runtime_id)
    assert worker.events == [event]
    assert worker.batches[-1].full_reconcile
    assert coordinator._runtimes[runtime_id].retirement_complete


@pytest.mark.parametrize("fail_factory", [False, True])
def test_moved_catalog_health_failure_does_not_stop_other_catalog(tmp_path, monkeypatch, caplog, fail_factory):
    from dataclasses import replace

    from rodex_registry import RodexRuntimeId
    from rodex_sql import RodexDatabaseMovedError

    first = _config(tmp_path / "first")
    second = replace(_config(tmp_path / "second"), runtime_id=RodexRuntimeId.parse("0000000000000002"))
    observed = Event()
    good = FakeAnalyticsTask()
    bad = FakeAnalyticsTask()

    def moved(*_args, **_kwargs):
        raise RodexDatabaseMovedError(first.rodex_database_path, "storage changed")

    def poll(batch):
        observed.set()
        return "up_to_date"

    bad.poll_once = moved
    good.poll_once = poll
    monkeypatch.setattr(analytics, "_project_supervisor_health", moved)

    def factory(config):
        if config == first:
            return moved() if fail_factory else bad
        return good

    coordinator = SharedAnalyticsCoordinator(worker_factory=factory, max_start_attempts=1)
    for config in (first, second):
        coordinator.reserve(_pending_config(config))
        coordinator.activate(config)
    coordinator.start()
    try:
        assert observed.wait(2)
        assert coordinator._thread.is_alive()
        assert "analytics health unavailable" in caplog.text
    finally:
        for config in (first, second):
            coordinator.retire(str(config.runtime_id))
        coordinator.close()
