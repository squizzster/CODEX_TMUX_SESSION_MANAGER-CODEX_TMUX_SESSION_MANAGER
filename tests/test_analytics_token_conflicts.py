"""Canonical accounting conflicts cannot advance durable analytics evidence."""

from pathlib import Path

import pytest
from test_analytics_publication_acceptance import _append_turn, _durable_projection
from test_rodex_analytics import CODEX_SESSION_ID, _config, _create, _rollout
from test_rodex_analytics_analyzer import (
    _canonical_records_with_conflicting_duplicate,
    _canonical_token_records,
    _content,
)

import rodex.analytics_analyzer as analyzer_module
from rodex.analytics import AnalyticsRolloutWorker
from rodex.analytics_scheduler import AnalyticsDirtyBatch
from rodex_registry import read_rodex_session_statistics


@pytest.mark.parametrize("cold_worker", [False, True])
def test_cumulative_conflict_preserves_durable_prefix_then_accepts_corrected_unpublished_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cold_worker: bool
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    records = _canonical_token_records()
    prefix = _content(records)
    rollout.write_bytes(prefix)
    _create(config)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    assert worker.poll_once() == "up_to_date"
    committed = _durable_projection(config.rodex_database_path)
    assert committed[0].projection.total_tokens == 35
    with rollout.open("ab") as output:
        output.write(_content(_canonical_records_with_conflicting_duplicate("thread_token_usage", "input_tokens")[-1:]))
    if cold_worker:
        worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    assert worker.poll_once() == "clean_replay"
    assert _durable_projection(config.rodex_database_path) == committed
    assert worker._parked_failure is not None
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.worker.worker_state == "degraded"
    assert view.worker.diagnostic_code == "analytics_error"
    assert view.worker.consecutive_failures == 1
    assert worker.poll_once() == "recovery_wait"
    clock[0] = worker.recovery_retry_at
    with monkeypatch.context() as parked:
        parked.setattr(
            analyzer_module.StatefulCodexProtocolAnalyticsAdapter,
            "analyze_rollouts",
            lambda *_args: pytest.fail("unchanged conflicting source must stay parked"),
        )
        for _ in range(3):
            assert worker.poll_once(AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))) == "failure_parked"
    assert _durable_projection(config.rodex_database_path) == committed

    # The fixture corrects only unaccepted bytes, never the authenticated prefix.
    # This is not a production rollout-rewriting or catalog-repair mechanism.
    rollout.write_bytes(prefix + _content([records[2]]))
    assert worker.poll_once() == "up_to_date"
    repaired = _durable_projection(config.rodex_database_path)
    assert repaired[0].statistics_publication_sequence == 2
    assert repaired[0].projection.total_tokens == 35
    assert len(repaired[3].events) == len(committed[3].events) + 1
    _append_turn(rollout, 2)
    assert worker.poll_once() == "up_to_date"
    final = _durable_projection(config.rodex_database_path)
    assert final[0].statistics_publication_sequence == 3
    assert final[0].projection.total_tokens == 45
    assert worker.poll_once() == "up_to_date"
    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"
    assert _durable_projection(config.rodex_database_path) == final


@pytest.mark.parametrize("append_before_replay", [False, True])
def test_historically_accepted_cumulative_conflict_degrades_without_rewriting_durable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, append_before_replay: bool
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    records = _canonical_records_with_conflicting_duplicate("thread_token_usage", "input_tokens")
    rollout.write_bytes(_content(records))
    _create(config)
    original_adapt = analyzer_module._adapt_token_accounting_record

    def prior_acceptance(record, thread_id, state):
        # Reproduce the old acceptance defect for this fixture's known duplicate.
        # Source bytes/checkpoints still contain the conflicting cumulative vector.
        if record == records[-1]:
            record = {
                **record,
                "payload": {**record["payload"], "thread_token_usage": records[2]["payload"]["thread_token_usage"]},
            }
        return original_adapt(record, thread_id, state)

    with monkeypatch.context() as prior_implementation:
        prior_implementation.setattr(analyzer_module, "_adapt_token_accounting_record", prior_acceptance)
        assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"
    committed = _durable_projection(config.rodex_database_path)
    assert committed[0].projection.total_tokens == 35
    if append_before_replay:
        _append_turn(rollout, 2)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    assert worker.poll_once() == "clean_replay"
    assert _durable_projection(config.rodex_database_path) == committed
    assert worker._parked_failure is not None
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.worker.diagnostic_code == "analytics_error"
    assert view.worker.worker_state == "degraded"
    assert "phase=source_processing" in caplog.text
    assert "analytics_acceptance_failed" not in caplog.text
    clock[0] = worker.recovery_retry_at
    assert worker.poll_once() == "failure_parked"
    assert _durable_projection(config.rodex_database_path) == committed
