"""Regressions for the compaction-triggered full-history replay CPU incident."""

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from test_rodex_analytics import (
    CODEX_SESSION_ID,
    TURN_TEST_ID,
    FakeAnalyticsAdapter,
    _config,
    _create,
    _pending_config,
    _rollout,
    _subagent_rollout,
)

from rodex import analytics_source_reader
from rodex.analytics import AnalyticsRolloutWorker, SharedAnalyticsCoordinator
from rodex.analytics_recovery import AnalyticsRecoveryBudget
from rodex.analytics_scheduler import AnalyticsDirtyBatch
from rodex_registry import (
    RodexAnalyticsRegistry,
    list_rodex_session_codex_threads,
    read_rodex_agent_trace,
    read_rodex_session_statistics,
)


def _append(path: Path, record: dict) -> int:
    data = json.dumps(record).encode() + b"\n"
    with path.open("ab") as stream:
        stream.write(data)
    return len(data)


def test_recovery_budget_cost_bound_survives_exponential_floor_cap() -> None:
    budget = AnalyticsRecoveryBudget()
    for index in range(20):
        budget.failed(started_at=100.0, finished_at=120.0)
        assert budget.retry_at == 2100.0  # 20 seconds work + 1,980 idle: <= 1% duty.
        assert budget.failures == index + 1
    budget.accepted()
    budget.failed(started_at=10.0, finished_at=10.0)
    assert budget.retry_at == 40.0
    assert budget.failures == 1


@pytest.mark.parametrize("restart_before_append", [False, True])
def test_compaction_label_publishes_then_restart_and_suffix_stay_incremental(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restart_before_append: bool,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    worker = AnalyticsRolloutWorker(config)
    assert worker.poll_once() == "up_to_date"
    if restart_before_append:
        worker = AnalyticsRolloutWorker(config)
    _append(
        rollout,
        {
            "timestamp": "2026-08-16T12:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "compact"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "auto-compact-1"},
            },
        },
    )
    assert worker.poll_once() == "up_to_date"
    trace = read_rodex_agent_trace(1, config.rodex_database_path)
    synthetic = [event for event in trace.events if event["event_kind"] == "message"]
    assert len(synthetic) == 1
    assert synthetic[0]["codex_turn_id"] is None
    state = read_rodex_session_statistics(1, config.rodex_database_path)
    assert state.worker.consecutive_failures == 0
    assert state.statistics.statistics_publication_sequence == 2

    worker = AnalyticsRolloutWorker(config)  # The accepted prefix now contains compaction.
    assert worker.poll_once() == "up_to_date"
    captured = []
    original = analytics_source_reader._pread_exact
    monkeypatch.setattr(
        analytics_source_reader,
        "_pread_exact",
        lambda descriptor, offset, size: (captured.append((offset, size)), original(descriptor, offset, size))[1],
    )
    _append(rollout, {"type": "event_msg", "payload": {"type": "task_started", "turn_id": TURN_TEST_ID}})
    assert worker.poll_once(AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))) == "up_to_date"
    assert captured and all(offset > 0 for offset, _size in captured)
    assert worker.recovery_retry_at is None


@pytest.mark.parametrize("error_type", [ValueError, KeyError, sqlite3.IntegrityError])
def test_generic_failure_parks_unchanged_prefix_and_logs_no_private_exception_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _append(rollout, {"type": "future", "padding": "x" * (2 * 1024 * 1024)})
    _create(config)
    clock = [100.0]
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter, monotonic=lambda: clock[0])
    assert worker.poll_once() == "up_to_date"
    before = read_rodex_session_statistics(1, config.rodex_database_path).statistics
    _append(rollout, {"type": "future"})
    calls = []

    def fail(*args):
        calls.append(args)
        raise error_type("private-transcript-secret")

    monkeypatch.setattr(adapter, "analyze_rollouts", fail)
    assert worker.poll_once() == "clean_replay"
    assert worker.recovery_retry_at == 130.0
    for _ in range(100):
        assert worker.poll_once(AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))) == "recovery_wait"
    clock[0] = 130.0
    for _ in range(100):
        assert worker.poll_once(AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))) == "failure_parked"
    assert len(calls) == 1
    after = read_rodex_session_statistics(1, config.rodex_database_path)
    assert after.statistics == before
    assert after.worker.consecutive_failures == 1
    assert after.worker.next_retry_at_utc is None
    assert "private-transcript-secret" not in caplog.text
    assert f"exception={error_type.__name__}" in caplog.text
    assert caplog.text.count("analytics failure runtime=") == 1


def test_continuous_appends_cannot_reset_failure_budget_and_success_restores_suffix_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    clock = [100.0]
    healthy = [False]
    constructions = []

    class Adapter(FakeAnalyticsAdapter):
        def analyze_rollouts(self, sources, user_id):
            if not healthy[0]:
                raise ValueError("deterministic")
            return super().analyze_rollouts(sources, user_id)

    def factory():
        adapter = Adapter()
        constructions.append(adapter)
        return adapter

    worker = AnalyticsRolloutWorker(config, adapter_factory=factory, monotonic=lambda: clock[0])
    assert worker.poll_once() == "clean_replay"
    captured_bytes = [0]
    original = analytics_source_reader._pread_exact

    def measured(descriptor, offset, size):
        captured_bytes[0] += size
        return original(descriptor, offset, size)

    monkeypatch.setattr(analytics_source_reader, "_pread_exact", measured)
    for delay in (30.0, 60.0, 120.0):
        deadline = worker.recovery_retry_at
        assert deadline == clock[0] + delay
        for _ in range(100):
            _append(rollout, {"type": "future"})
            assert worker.poll_once(AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))) == "recovery_wait"
        assert captured_bytes[0] == 0
        clock[0] = deadline
        assert worker.poll_once() == "clean_replay"
        assert captured_bytes[0] > 0
        captured_bytes[0] = 0
    assert len(constructions) == 4
    healthy[0] = True
    _append(rollout, {"type": "future", "repair": True})
    clock[0] = worker.recovery_retry_at
    assert worker.poll_once() == "up_to_date"
    assert worker._recovery.failures == 0
    captured_bytes[0] = 0
    added = _append(rollout, {"type": "future", "after_recovery": True})
    assert worker.poll_once(AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))) == "up_to_date"
    assert captured_bytes[0] == added
    assert len(constructions) == 5


def test_failed_partial_read_is_guarded_before_all_sources_are_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    original = worker._source_reader.read
    calls = []

    def fail(source):
        calls.append(source)
        original(source)
        clock[0] += 2.0
        raise OSError("read after previous expensive sources")

    monkeypatch.setattr(worker._source_reader, "read", fail)
    assert worker.poll_once() == "clean_replay"
    assert worker._parked_failure is None
    assert worker.recovery_retry_at == 300.0
    for _ in range(100):
        assert worker.poll_once() == "recovery_wait"
    assert len(calls) == 1
    monkeypatch.setattr(worker._source_reader, "read", original)
    clock[0] = 300.0
    assert worker.poll_once() == "up_to_date"


def test_sqlite_environment_error_retries_unchanged_input_at_recovery_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    original = RodexAnalyticsRegistry.publish

    def fail(*_args):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(RodexAnalyticsRegistry, "publish", fail)
    assert worker.poll_once() == "clean_replay"
    assert worker._parked_failure is None
    monkeypatch.setattr(RodexAnalyticsRegistry, "publish", original)
    clock[0] = worker.recovery_retry_at
    assert worker.poll_once() == "up_to_date"


@pytest.mark.parametrize("child_during_cooldown", [False, True])
def test_recovery_keeps_latest_accepted_child_and_new_child_dirty_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_during_cooldown: bool,
) -> None:
    config = _config(tmp_path)
    root = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    assert worker.poll_once() == "up_to_date"
    child_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)

    def introduce_child():
        _subagent_rollout(
            config.codex_sessions_root, CODEX_SESSION_ID, child_id, linked_at_utc="2026-08-16T12:00:01.500000Z"
        )
        worker.observe_protocol_event(
            {
                "method": "thread/started",
                "params": {
                    "thread": {
                        "id": str(child_id),
                        "createdAt": "2026-08-16T12:00:01.500000Z",
                    }
                },
            }
        )

    if not child_during_cooldown:
        introduce_child()
        assert worker.poll_once(AnalyticsDirtyBatch(frozenset({child_id}))) == "up_to_date"
    original = RodexAnalyticsRegistry.publish

    def fail(*_args):
        raise OSError("transient publication IO failure")

    monkeypatch.setattr(RodexAnalyticsRegistry, "publish", fail)
    _append(root, {"type": "event_msg", "payload": {"type": "agent_message", "message": "after acceptance"}})
    assert worker.poll_once(AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))) == "clean_replay"
    if child_during_cooldown:
        introduce_child()
        assert worker.poll_once(AnalyticsDirtyBatch(frozenset({child_id}))) == "recovery_wait"
    monkeypatch.setattr(RodexAnalyticsRegistry, "publish", original)
    clock[0] = worker.recovery_retry_at
    result = worker.poll_once(AnalyticsDirtyBatch(frozenset()))
    if result == "catching_up":
        result = worker.poll_once(AnalyticsDirtyBatch(frozenset()))
    assert result == "up_to_date"
    sources = list_rodex_session_codex_threads(1, config.rodex_database_path)
    assert {source.codex_thread_id for source in sources} == {CODEX_SESSION_ID, child_id}
    assert next(source for source in sources if source.codex_thread_id == child_id).spawning_codex_turn_id == TURN_TEST_ID
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.worker.consecutive_failures == 0
    assert view.statistics.projection.collaboration_agents_started_count == 1


def test_failed_parking_health_transition_retries_without_source_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    clock = [100.0]
    calls = []

    class Adapter(FakeAnalyticsAdapter):
        def analyze_rollouts(self, sources, _user_id):
            calls.append(sources)
            raise ValueError("deterministic")

    worker = AnalyticsRolloutWorker(config, adapter_factory=Adapter, monotonic=lambda: clock[0])
    coordinator = SharedAnalyticsCoordinator(worker_factory=lambda _config: worker, monotonic=lambda: clock[0])
    coordinator.reserve(_pending_config(config))
    coordinator.activate(config)
    runtime_id = str(config.runtime_id)
    coordinator._reconcile(runtime_id)
    original = RodexAnalyticsRegistry.record_health_transition
    writes = []

    def fail_once(registry, **values):
        writes.append(values)
        if len(writes) == 1:
            raise sqlite3.OperationalError("health persistence unavailable")
        return original(registry, **values)

    monkeypatch.setattr(RodexAnalyticsRegistry, "record_health_transition", fail_once)
    clock[0] = 130.0
    coordinator._reconcile(runtime_id)
    entry = coordinator._runtimes[runtime_id]
    assert entry.next_retry_at == 131.0
    assert len(writes) == 1
    clock[0] = 131.0
    coordinator._reconcile(runtime_id)
    assert entry.next_retry_at is None
    assert len(writes) == 2
    assert len(calls) == 1
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.worker.next_retry_at_utc is None
    assert view.worker.consecutive_failures == 1


def test_coordinator_keeps_repair_wake_beyond_catchup_window_and_retirement_cannot_bypass_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    clock = [100.0]
    calls = []
    healthy = [False]

    class Adapter(FakeAnalyticsAdapter):
        def analyze_rollouts(self, sources, user_id):
            calls.append(sources)
            if not healthy[0]:
                raise ValueError("failure")
            return super().analyze_rollouts(sources, user_id)

    worker = AnalyticsRolloutWorker(config, adapter_factory=Adapter, monotonic=lambda: clock[0])
    coordinator = SharedAnalyticsCoordinator(worker_factory=lambda _config: worker, monotonic=lambda: clock[0])
    coordinator.reserve(_pending_config(config))
    coordinator.activate(config)
    runtime_id = str(config.runtime_id)
    coordinator._reconcile(runtime_id)
    entry = coordinator._runtimes[runtime_id]
    assert entry.next_retry_at == 130.0
    healthy[0] = True
    _append(rollout, {"type": "future", "repair": True})
    event = {"method": "turn/completed", "params": {"threadId": str(CODEX_SESSION_ID)}}
    for _ in range(100):
        coordinator.observe_protocol_event(runtime_id, event)
    assert coordinator._entry_deadline(entry, 106.0) == 130.0
    clock[0] = 130.0
    coordinator._reconcile(runtime_id)  # The one scheduled wake, no second repair event.
    assert len(calls) == 2
    assert entry.next_retry_at is None
    assert worker.recovery_retry_at is None
    healthy[0] = False
    _append(rollout, {"type": "future", "fail_again": True})
    coordinator.observe_protocol_event(runtime_id, event)
    coordinator._reconcile(runtime_id)
    attempts_before_retirement = len(calls)
    coordinator.retire(runtime_id)
    coordinator._reconcile(runtime_id)
    assert len(calls) == attempts_before_retirement
    assert entry.next_retry_at == entry.retirement_deadline
