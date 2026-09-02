from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest

from rodex.analytics_analyzer import (
    AnalyticsAnalyzerSource,
    CodexProtocolAnalyticsAdapter,
    RodexAnalyticsError,
    StatefulCodexProtocolAnalyticsAdapter,
)

THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
TURN_A_ID = "00000000-0000-7000-8000-000000000001"
TURN_B_ID = "00000000-0000-7000-8000-000000000002"
CHILD_TURN_ID = "00000000-0000-7000-8000-000000000003"


def _content(records: list[dict[str, object]]) -> bytes:
    return b"".join(json.dumps(record).encode() + b"\n" for record in records)


def _records() -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-08-16T12:00:00Z",
            "type": "session_meta",
            "payload": {"id": str(THREAD_ID), "session_id": str(THREAD_ID)},
        },
        {
            "timestamp": "2026-08-16T12:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": TURN_A_ID},
        },
        {
            "timestamp": "2026-08-16T12:00:02Z",
            "type": "turn_context",
            "payload": {
                "turn_id": TURN_A_ID,
                "model": "gpt-test",
                "effort": "xhigh",
            },
        },
        {
            "timestamp": "2026-08-16T12:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call-a",
                "name": "exec_command",
            },
        },
        {
            "timestamp": "2026-08-16T12:00:04Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call-a"},
        },
        {
            "timestamp": "2026-08-16T12:00:05Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": TURN_A_ID,
                "duration_ms": 4_000,
            },
        },
    ]


def _source(
    full: bytes, appended: bytes, thread_id: uuid.UUID = THREAD_ID
) -> AnalyticsAnalyzerSource:
    return AnalyticsAnalyzerSource(
        codex_thread_id=thread_id,
        analyzer_content=full,
        appended_analyzer_content=appended,
    )


def test_stateful_analyzer_matches_full_replay_at_every_record_boundary() -> None:
    records = _records()
    full = _content(records)
    oracle = CodexProtocolAnalyticsAdapter().analyze_rollouts(
        [_source(full, full)], "test-user"
    )

    for split_at in range(1, len(records)):
        prefix = _content(records[:split_at])
        suffix = _content(records[split_at:])
        stateful = StatefulCodexProtocolAnalyticsAdapter()
        stateful.analyze_rollouts([_source(prefix, prefix)], "test-user")
        stateful.accept_batch()
        final = stateful.analyze_rollouts([_source(full, suffix)], "test-user")
        assert final == oracle


def test_incremental_projection_materializes_only_the_changed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_turn = _records()
    second_turn = [
        {
            "timestamp": "2026-08-16T12:01:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": TURN_B_ID},
        },
        {
            "timestamp": "2026-08-16T12:01:02Z",
            "type": "turn_context",
            "payload": {"turn_id": TURN_B_ID, "model": "gpt-test", "effort": "xhigh"},
        },
        {
            "timestamp": "2026-08-16T12:01:03Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": TURN_B_ID,
                "duration_ms": 2_000,
            },
        },
    ]
    prefix = _content(first_turn)
    suffix = _content(second_turn)
    full = prefix + suffix
    adapter = StatefulCodexProtocolAnalyticsAdapter()
    adapter.analyze_rollouts([_source(prefix, prefix)], "test-user")
    adapter.accept_batch()
    turn_reports = 0
    original_turn_report = adapter._analyzer._turn_statistical_report

    def count_turn_report(turn: object) -> object:
        nonlocal turn_reports
        turn_reports += 1
        return original_turn_report(turn)

    monkeypatch.setattr(adapter._analyzer, "_turn_statistical_report", count_turn_report)
    monkeypatch.setattr(
        adapter._analyzer,
        "report",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full report called")),
    )

    incremental = adapter.analyze_rollouts([_source(full, suffix)], "test-user")
    oracle = CodexProtocolAnalyticsAdapter().analyze_rollouts(
        [_source(full, full)], "test-user"
    )

    assert [
        turn.codex_turn_id for turn in incremental.statistics_projection.turn_statistics
    ] == [TURN_B_ID]
    assert turn_reports == 1
    assert (
        replace(
            incremental.statistics_projection,
            turn_statistics=oracle.statistics_projection.turn_statistics,
        )
        == oracle.statistics_projection
    )


def test_unaccepted_stateful_batch_extends_without_double_counting() -> None:
    records = _records()
    prefix = _content(records[:2])
    full = _content(records)
    adapter = StatefulCodexProtocolAnalyticsAdapter()

    first_attempt = adapter.analyze_rollouts([_source(prefix, prefix)], "test-user")
    retry_with_more = adapter.analyze_rollouts([_source(full, full)], "test-user")
    oracle = CodexProtocolAnalyticsAdapter().analyze_rollouts(
        [_source(full, full)], "test-user"
    )

    assert first_attempt.statistics_projection.history_records_count == 2
    assert retry_with_more == oracle
    assert retry_with_more.statistics_projection.history_records_count == len(records)


def test_stateful_analyzer_preserves_gapped_coverage_for_unknown_records() -> None:
    records = _records()
    content = _content(records) + b'{"type":"future_record","payload":{}}\n'
    adapter = StatefulCodexProtocolAnalyticsAdapter()

    calculation = adapter.analyze_rollouts([_source(content, content)], "test-user")

    assert calculation.coverage_state == "gapped"
    assert calculation.statistics_projection.audit_new_event_type_warnings_count == 1


def test_stateful_analyzer_matches_full_replay_for_multiple_threads() -> None:
    root_content = _content(_records())
    child_id = uuid.UUID(int=THREAD_ID.int + 100)
    child_content = _content(
        [
            {
                "timestamp": "2026-08-16T12:00:02Z",
                "type": "session_meta",
                "payload": {"id": str(child_id), "session_id": str(THREAD_ID)},
            },
            {
                "timestamp": "2026-08-16T12:00:03Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": CHILD_TURN_ID},
            },
            {
                "timestamp": "2026-08-16T12:00:04Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": CHILD_TURN_ID},
            },
        ]
    )
    sources = [
        _source(root_content, root_content),
        _source(child_content, child_content, child_id),
    ]

    oracle = CodexProtocolAnalyticsAdapter().analyze_rollouts(sources, "test-user")
    stateful = StatefulCodexProtocolAnalyticsAdapter().analyze_rollouts(
        sources, "test-user"
    )

    assert stateful == oracle


def test_stateful_analyzer_reuses_candidate_projection_on_publication_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _content(_records())
    adapter = StatefulCodexProtocolAnalyticsAdapter()
    first = adapter.analyze_rollouts([_source(content, content)], "test-user")

    def unexpected_report(*, source: str) -> object:
        raise AssertionError(source)

    monkeypatch.setattr(adapter._analyzer, "report", unexpected_report)

    assert adapter.analyze_rollouts([_source(content, content)], "test-user") is first


def test_stateful_analyzer_prevalidates_identity_before_mutating() -> None:
    records = _records()
    records[-1] = {
        "type": "session_meta",
        "payload": {"id": str(uuid.UUID(int=THREAD_ID.int + 1))},
    }
    adapter = StatefulCodexProtocolAnalyticsAdapter()

    with pytest.raises(RodexAnalyticsError, match="identity changed"):
        adapter.analyze_rollouts(
            [_source(_content(records), _content(records))], "test-user"
        )

    assert adapter._analyzer.records == 0


def test_stateful_analyzer_fails_closed_after_unexpected_mutation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = _content(_records())
    adapter = StatefulCodexProtocolAnalyticsAdapter()

    def fail_consume(*args: object) -> None:
        raise RuntimeError("analyzer dependency failure")

    monkeypatch.setattr(adapter._analyzer, "_consume", fail_consume)

    with pytest.raises(RodexAnalyticsError, match="failed while consuming"):
        adapter.analyze_rollouts([_source(content, content)], "test-user")
    with pytest.raises(RodexAnalyticsError, match="requires clean restart"):
        adapter.analyze_rollouts([_source(content, content)], "test-user")
