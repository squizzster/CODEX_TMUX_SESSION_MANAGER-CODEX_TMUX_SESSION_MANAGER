"""Durable publication is independent of fallible resident acceptance."""

import json
from pathlib import Path

import pytest
from test_rodex_analytics import CODEX_SESSION_ID, _config, _create, _rollout
from test_rodex_analytics_analyzer import _canonical_token_records, _content, _token_vector

from rodex.agent_trace import StatefulAgentTraceNormalizer
from rodex.analytics import AnalyticsRolloutWorker
from rodex.analytics_analyzer import StatefulCodexProtocolAnalyticsAdapter
from rodex.analytics_scheduler import AnalyticsDirtyBatch
from rodex.analytics_source_reader import AnalyticsSourceReader
from rodex_registry import (
    RodexAnalyticsPublicationRetryableError,
    RodexAnalyticsRegistry,
    read_rodex_agent_trace,
    read_rodex_session_statistics,
    read_rodex_session_turn_statistics,
)

_ACCEPTANCE_METHODS = {
    "analyzer": (StatefulCodexProtocolAnalyticsAdapter, "accept_batch"),
    "trace": (StatefulAgentTraceNormalizer, "accept_batch"),
    "cursor": (AnalyticsSourceReader, "accept"),
}


def _append_turn(rollout: Path, number: int) -> None:
    turn_id = f"00000000-0000-7000-8000-{number:012d}"
    usage = _token_vector(7, 2, 0, 3, 1)
    cumulative = _token_vector(30 + 7 * (number - 1), 12 + 2 * (number - 1), 3, 5 + 3 * (number - 1), 3 + number - 1)
    records = (
        ("event_msg", {"type": "task_started", "turn_id": turn_id}),
        ("turn_context", {"turn_id": turn_id, "model": "gpt-test", "effort": "xhigh"}),
        (
            "token_usage_record",
            {
                "thread_id": str(CODEX_SESSION_ID),
                "turn_id": turn_id,
                "response_id": f"response-{number}",
                "usage": usage,
                "turn_token_usage": usage,
                "thread_token_usage": cumulative,
            },
        ),
        ("event_msg", {"type": "task_complete", "turn_id": turn_id}),
    )
    with rollout.open("a", encoding="utf-8") as output:
        for second, (kind, payload) in enumerate(records):
            output.write(
                json.dumps({"timestamp": f"2026-08-16T12:{number:02d}:{second:02d}Z", "type": kind, "payload": payload})
                + "\n"
            )


def _durable_projection(database: Path) -> tuple:
    view = read_rodex_session_statistics(1, database)
    trace = read_rodex_agent_trace(1, database)
    turn_ids = sorted({event["codex_turn_id"] for event in trace.events if event["codex_turn_id"] is not None})
    return (
        view.statistics,
        view.sources,
        tuple(read_rodex_session_turn_statistics(1, turn_id, database).turn for turn_id in turn_ids),
        trace,
    )


@pytest.mark.parametrize("participant", _ACCEPTANCE_METHODS)
@pytest.mark.parametrize("after_mutation", [False, True])
@pytest.mark.parametrize("initial_publication", [False, True])
@pytest.mark.parametrize("append_before_recovery", [False, True])
@pytest.mark.parametrize("publication_lock_retry", [False, True])
def test_committed_publication_recovers_acceptance_without_republishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    participant: str,
    after_mutation: bool,
    initial_publication: bool,
    append_before_recovery: bool,
    publication_lock_retry: bool,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    rollout.write_bytes(_content(_canonical_token_records()))
    _create(config)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    published_turns = 1
    if not initial_publication:
        assert worker.poll_once() == "up_to_date"
        _append_turn(rollout, 2)
        published_turns = 2
    owner, method_name = _ACCEPTANCE_METHODS[participant]
    original_accept = getattr(owner, method_name)
    failed = False

    def fail_acceptance_once(self, *args):
        nonlocal failed
        if failed:
            return original_accept(self, *args)
        failed = True
        if after_mutation:
            original_accept(self, *args)
        raise RuntimeError("private-acceptance-exception-body")

    monkeypatch.setattr(owner, method_name, fail_acceptance_once)
    if publication_lock_retry:
        original_publish = RodexAnalyticsRegistry.publish

        def locked(*_args):
            raise RodexAnalyticsPublicationRetryableError("lock before commit")

        monkeypatch.setattr(RodexAnalyticsRegistry, "publish", locked)
        assert worker.poll_once() == "publication_retry"
        assert not failed
        monkeypatch.setattr(RodexAnalyticsRegistry, "publish", original_publish)
    assert worker.poll_once() == "clean_replay"
    committed = _durable_projection(config.rodex_database_path)
    assert committed[0].statistics_publication_sequence == published_turns
    assert committed[0].projection.total_tokens == 35 + 10 * (published_turns - 1)
    assert worker._parked_failure is None
    assert worker.recovery_retry_at == 130.0
    assert worker.poll_once() == "recovery_wait"
    assert "private-acceptance-exception-body" not in caplog.text
    assert "phase=resident_acceptance" in caplog.text
    if append_before_recovery:
        _append_turn(rollout, published_turns + 1)
    clock[0] = worker.recovery_retry_at
    assert worker.poll_once() == "up_to_date"
    assert worker.recovery_retry_at is None
    if not append_before_recovery:
        # Full statistics, turns, trace facts/head and source checkpoints stay exact.
        assert _durable_projection(config.rodex_database_path) == committed
        _append_turn(rollout, published_turns + 1)
        assert worker.poll_once(AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))) == "up_to_date"
    final = _durable_projection(config.rodex_database_path)
    assert final[0].statistics_publication_sequence == published_turns + 1
    assert final[0].projection.total_tokens == 35 + 10 * published_turns
    assert len(final[2]) == published_turns + 1
    assert len(final[3].events) == len(committed[3].events) + 4
    assert worker.poll_once() == "up_to_date"
    assert _durable_projection(config.rodex_database_path) == final
    assert read_rodex_session_statistics(1, config.rodex_database_path).worker.consecutive_failures == 0


@pytest.mark.parametrize("initial_publication", [False, True])
@pytest.mark.parametrize("append_before_recovery", [False, True])
def test_lost_publication_acknowledgement_reloads_durable_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_publication: bool, append_before_recovery: bool
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    rollout.write_bytes(_content(_canonical_token_records()))
    _create(config)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    published_turns = 1
    if not initial_publication:
        assert worker.poll_once() == "up_to_date"
        _append_turn(rollout, 2)
        published_turns = 2
    original_publish = RodexAnalyticsRegistry.publish

    def lose_ack(registry, publication):
        original_publish(registry, publication)
        raise OSError("publication acknowledgement lost")

    monkeypatch.setattr(RodexAnalyticsRegistry, "publish", lose_ack)
    assert worker.poll_once() == "clean_replay"
    committed = _durable_projection(config.rodex_database_path)
    assert worker._parked_failure is None
    if append_before_recovery:
        _append_turn(rollout, published_turns + 1)
    monkeypatch.setattr(RodexAnalyticsRegistry, "publish", original_publish)
    clock[0] = worker.recovery_retry_at
    assert worker.poll_once() == "up_to_date"
    recovered = _durable_projection(config.rodex_database_path)
    if append_before_recovery:
        assert recovered[0].statistics_publication_sequence == published_turns + 1
        assert recovered[0].projection.total_tokens == 35 + 10 * published_turns
        assert len(recovered[3].events) == len(committed[3].events) + 4
    else:
        assert recovered == committed


@pytest.mark.parametrize("participant", ["analyzer", "trace", "cursor", "baseline-analyzer", "unchanged-cursor"])
@pytest.mark.parametrize("after_mutation", [False, True])
def test_acceptance_without_new_publication_retries_unchanged_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, participant: str, after_mutation: bool
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    assert worker.poll_once() == "up_to_date"
    committed = _durable_projection(config.rodex_database_path)
    if participant != "unchanged-cursor":
        worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    owner, method_name = _ACCEPTANCE_METHODS[participant.removeprefix("baseline-").removeprefix("unchanged-")]
    original_accept = getattr(owner, method_name)
    calls = 0

    def fail_once(self, *args):
        nonlocal calls
        calls += 1
        # Cold adapter accepts the durable baseline, then its empty suffix.
        if calls != (2 if participant == "analyzer" else 1):
            return original_accept(self, *args)
        if after_mutation:
            original_accept(self, *args)
        raise RuntimeError("resident acceptance interrupted")

    def unexpected_publication(*_args):
        pytest.fail("unchanged durable input must not publish again")

    monkeypatch.setattr(owner, method_name, fail_once)
    monkeypatch.setattr(RodexAnalyticsRegistry, "publish", unexpected_publication)
    assert worker.poll_once() == "clean_replay"
    assert worker._parked_failure is None
    assert worker.recovery_retry_at == 130.0
    clock[0] = worker.recovery_retry_at
    assert worker.poll_once() == "up_to_date"
    assert _durable_projection(config.rodex_database_path) == committed


def test_repeated_acceptance_failure_keeps_cost_budget_without_republication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    original_accept = StatefulCodexProtocolAnalyticsAdapter.accept_batch
    calls = 0

    def fail(_self):
        nonlocal calls
        calls += 1
        clock[0] += 1.0
        raise RuntimeError("resident acceptance unavailable")

    monkeypatch.setattr(StatefulCodexProtocolAnalyticsAdapter, "accept_batch", fail)
    for attempt in range(3):
        assert worker.poll_once() == "clean_replay"
        assert worker._parked_failure is None
        assert worker.recovery_retry_at == clock[0] + max(30.0 * 2**attempt, 99.0)
        for _ in range(10):
            assert worker.poll_once(AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))) == "recovery_wait"
        assert calls == attempt + 1
        clock[0] = worker.recovery_retry_at
    committed = _durable_projection(config.rodex_database_path)
    assert committed[0].statistics_publication_sequence == 1
    monkeypatch.setattr(StatefulCodexProtocolAnalyticsAdapter, "accept_batch", original_accept)
    assert worker.poll_once() == "up_to_date"
    assert worker.recovery_retry_at is None
    assert _durable_projection(config.rodex_database_path) == committed


@pytest.mark.parametrize("acknowledgement_lost", [False, True])
def test_recovery_authenticates_newly_committed_prefix_not_old_resident_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acknowledgement_lost: bool
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    assert worker.poll_once() == "up_to_date"
    old_prefix = rollout.read_bytes()
    _append_turn(rollout, 2)

    def fail_acceptance(_self):
        raise RuntimeError("stop before cursor acceptance")

    if acknowledgement_lost:
        original_publish = RodexAnalyticsRegistry.publish

        def lose_ack(registry, publication):
            original_publish(registry, publication)
            raise OSError("publication acknowledgement lost")

        monkeypatch.setattr(RodexAnalyticsRegistry, "publish", lose_ack)
    else:
        monkeypatch.setattr(StatefulCodexProtocolAnalyticsAdapter, "accept_batch", fail_acceptance)
    assert worker.poll_once() == "clean_replay"
    committed = _durable_projection(config.rodex_database_path)
    new_prefix = rollout.read_bytes()
    rewritten = old_prefix + new_prefix[len(old_prefix) :].replace(b"gpt-test", b"bad-test")
    assert rewritten != new_prefix and len(rewritten) == len(new_prefix)
    rollout.write_bytes(rewritten)
    _append_turn(rollout, 3)  # Growth must not hide mutation of the durable suffix.

    def unexpected_analysis(*_args):
        pytest.fail("durable prefix must be authenticated before analyzer warmup")

    monkeypatch.setattr(StatefulCodexProtocolAnalyticsAdapter, "analyze_rollouts", unexpected_analysis)
    clock[0] = worker.recovery_retry_at
    assert worker.poll_once() == "clean_replay"
    assert _durable_projection(config.rodex_database_path) == committed
    assert read_rodex_session_statistics(1, config.rodex_database_path).worker.diagnostic_code == "analytics_error"


def test_trace_warmup_data_failure_still_parks_by_source_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    calls = 0

    def invalid_source(*_args):
        nonlocal calls
        calls += 1
        raise ValueError("deterministic trace normalization failure")

    monkeypatch.setattr(StatefulAgentTraceNormalizer, "warmup", invalid_source)
    assert worker.poll_once() == "clean_replay"
    assert worker._parked_failure is not None
    clock[0] = worker.recovery_retry_at
    assert worker.poll_once() == "failure_parked"
    assert calls == 1
