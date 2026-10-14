from __future__ import annotations

import json
import uuid
from copy import deepcopy
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
ACCOUNTING_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


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


def _source(full: bytes, appended: bytes, thread_id: uuid.UUID = THREAD_ID) -> AnalyticsAnalyzerSource:
    return AnalyticsAnalyzerSource(
        codex_thread_id=thread_id,
        analyzer_content=full,
        appended_analyzer_content=appended,
    )


def _token_vector(
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _canonical_token_records() -> list[dict[str, object]]:
    first = _token_vector(10, 4, 1, 2, 1)
    compaction = _token_vector(20, 8, 2, 3, 2)
    cumulative = _token_vector(30, 12, 3, 5, 3)
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
            "type": "token_usage_record",
            "payload": {
                "thread_id": str(THREAD_ID),
                "turn_id": TURN_A_ID,
                "response_id": "resp-first",
                "usage": first,
                "turn_token_usage": first,
                "thread_token_usage": first,
            },
        },
        {
            "timestamp": "2026-08-16T12:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "turn_id": TURN_A_ID,
                "info": {
                    "last_token_usage": first,
                    "total_token_usage": first,
                    "model_context_window": 100,
                },
            },
        },
        {
            "timestamp": "2026-08-16T12:00:04Z",
            "type": "token_usage_record",
            "payload": {
                "thread_id": str(THREAD_ID),
                "turn_id": TURN_A_ID,
                "response_id": "resp-compaction",
                "usage": compaction,
                "turn_token_usage": cumulative,
                "thread_token_usage": cumulative,
            },
        },
        {
            "timestamp": "2026-08-16T12:00:05Z",
            "type": "compacted",
            "payload": {"turn_id": TURN_A_ID},
        },
        {
            "timestamp": "2026-08-16T12:00:06Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": TURN_A_ID},
        },
    ]


def test_stateful_analyzer_matches_full_replay_at_every_record_boundary() -> None:
    records = _records()
    full = _content(records)
    oracle = CodexProtocolAnalyticsAdapter().analyze_rollouts([_source(full, full)], "test-user")

    for split_at in range(1, len(records)):
        prefix = _content(records[:split_at])
        suffix = _content(records[split_at:])
        stateful = StatefulCodexProtocolAnalyticsAdapter()
        stateful.analyze_rollouts([_source(prefix, prefix)], "test-user")
        stateful.accept_batch()
        final = stateful.analyze_rollouts([_source(full, suffix)], "test-user")
        assert final == oracle


def test_canonical_response_usage_counts_mirror_once_and_includes_compaction() -> None:
    records = _canonical_token_records()
    content = _content(records)

    full = CodexProtocolAnalyticsAdapter().analyze_rollouts([_source(content, content)], "test-user")
    stateful = StatefulCodexProtocolAnalyticsAdapter().analyze_rollouts(
        [_source(content, content)],
        "test-user",
    )

    assert stateful == full
    projection = stateful.statistics_projection
    assert (
        projection.input_tokens,
        projection.cached_input_tokens,
        projection.cache_write_input_tokens,
        projection.output_tokens,
        projection.reasoning_output_tokens,
        projection.total_tokens,
    ) == (30, 12, 3, 5, 3, 35)
    assert projection.turn_statistics[0].total_tokens == 35
    assert projection.audit_new_event_type_warnings_count == 0
    assert projection.audit_token_method.startswith("unique response usage")


def test_canonical_response_usage_matches_full_replay_across_appends() -> None:
    records = _canonical_token_records()
    full_content = _content(records)
    oracle = CodexProtocolAnalyticsAdapter().analyze_rollouts(
        [_source(full_content, full_content)],
        "test-user",
    )

    for split_at in range(1, len(records)):
        prefix = _content(records[:split_at])
        suffix = _content(records[split_at:])
        stateful = StatefulCodexProtocolAnalyticsAdapter()
        stateful.analyze_rollouts([_source(prefix, prefix)], "test-user")
        stateful.accept_batch()
        assert stateful.analyze_rollouts([_source(full_content, suffix)], "test-user") == oracle


@pytest.mark.parametrize("repeated_record", [2, 4])
@pytest.mark.parametrize("mirror_type", ["token_count", "token_usage"])
def test_duplicate_canonical_response_identity_does_not_add_tokens_twice(repeated_record: int, mirror_type: str) -> None:
    records = _canonical_token_records()
    records.append(deepcopy(records[repeated_record]))
    mirror = deepcopy(records[3])
    mirror["payload"]["type"] = mirror_type
    records.append(mirror)
    content = _content(records)
    oracle = CodexProtocolAnalyticsAdapter().analyze_rollouts([_source(content, content)], "test-user")
    for projection in (oracle.statistics_projection, oracle.statistics_projection.turn_statistics[0]):
        assert tuple(getattr(projection, name) for name in ACCOUNTING_TOKEN_FIELDS) == (30, 12, 3, 5, 3, 35)
    for split_at in range(len(records)):
        stateful = StatefulCodexProtocolAnalyticsAdapter()
        if split_at:
            prefix = _content(records[:split_at])
            stateful.analyze_rollouts([_source(prefix, prefix)], "test-user")
            stateful.accept_batch()
        calculation = stateful.analyze_rollouts([_source(content, _content(records[split_at:]))], "test-user")
        assert calculation == oracle


def _canonical_records_with_conflicting_duplicate(component: str, token_field: str | None) -> list[dict[str, object]]:
    records = _canonical_token_records()
    duplicate = deepcopy(records[2])
    payload = duplicate["payload"]
    if component == "turn_id":
        payload[component] = TURN_B_ID
    else:
        # The in-memory fixture shares equal vector dictionaries. Replace just
        # one vector so a cumulative-only conflict cannot also change usage.
        payload[component] = {**payload[component], token_field: payload[component][token_field] + 1}
    for unchanged in {"turn_id", "usage", "thread_token_usage"} - {component}:
        assert payload[unchanged] == records[2]["payload"][unchanged]
    # Repeat the older response after a later canonical total. The conflict must
    # be checked against that response, not only against the current total.
    return [*records, duplicate]


def _require_accounting_conflict(adapter, full: bytes, appended: bytes) -> None:
    with pytest.raises(RodexAnalyticsError) as caught:
        adapter.analyze_rollouts([_source(full, appended)], "test-user")
    error = caught.value.__cause__ or caught.value
    assert isinstance(error, RodexAnalyticsError)
    assert str(error) == "token usage response identity has conflicting values"


@pytest.mark.parametrize(
    ("component", "token_field"),
    [("turn_id", None)]
    + [(component, name) for component in ("usage", "thread_token_usage") for name in ACCOUNTING_TOKEN_FIELDS],
)
def test_conflicting_canonical_response_is_rejected_in_replay_and_every_append_split(
    component: str, token_field: str | None
) -> None:
    records = _canonical_records_with_conflicting_duplicate(component, token_field)
    full = _content(records)
    _require_accounting_conflict(CodexProtocolAnalyticsAdapter(), full, full)
    _require_accounting_conflict(StatefulCodexProtocolAnalyticsAdapter(), full, full)
    for split_at in range(1, len(records)):
        prefix = _content(records[:split_at])
        for accepted in (False, True):
            stateful = StatefulCodexProtocolAnalyticsAdapter()
            stateful.analyze_rollouts([_source(prefix, prefix)], "test-user")
            if accepted:
                stateful.accept_batch()
            offered = _content(records[split_at:]) if accepted else full
            _require_accounting_conflict(stateful, full, offered)
            with pytest.raises(RodexAnalyticsError, match="requires clean restart"):
                stateful.analyze_rollouts([_source(full, b"")], "test-user")


def test_canonical_duplicate_compares_normalized_vectors_not_optional_zero_spelling() -> None:
    records = _canonical_token_records()[:3]
    payload = records[2]["payload"]
    for component in ("usage", "turn_token_usage", "thread_token_usage"):
        payload[component] = _token_vector(10, 0, 0, 2, 0)
    duplicate = deepcopy(records[2])
    for component in ("usage", "thread_token_usage"):
        duplicate["payload"][component] = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    prefix = _content(records)
    full = prefix + _content([duplicate])
    oracle = CodexProtocolAnalyticsAdapter().analyze_rollouts([_source(full, full)], "test-user")
    assert tuple(getattr(oracle.statistics_projection, name) for name in ACCOUNTING_TOKEN_FIELDS) == (10, 0, 0, 2, 0, 12)
    for split in (False, True):
        stateful = StatefulCodexProtocolAnalyticsAdapter()
        if split:
            stateful.analyze_rollouts([_source(prefix, prefix)], "test-user")
            stateful.accept_batch()
        assert stateful.analyze_rollouts([_source(full, _content([duplicate]) if split else full)], "test-user") == oracle


def test_canonical_response_identity_is_local_to_its_source_thread() -> None:
    records = _canonical_token_records()
    records.append(deepcopy(records[2]))
    other_thread = uuid.UUID(int=THREAD_ID.int + 100)
    other_records = deepcopy(records)
    other_records[0]["payload"] = {"id": str(other_thread), "session_id": str(other_thread)}
    for record in other_records:
        if record["type"] == "token_usage_record":
            payload = record["payload"]
            payload["thread_id"] = str(other_thread)
            for component in ("usage", "turn_token_usage", "thread_token_usage"):
                payload[component] = {name: value * 2 for name, value in payload[component].items()}
    sources = [
        _source(_content(records), _content(records)),
        _source(_content(other_records), _content(other_records), other_thread),
    ]
    oracle = CodexProtocolAnalyticsAdapter().analyze_rollouts(sources, "test-user")
    assert tuple(getattr(oracle.statistics_projection, name) for name in ACCOUNTING_TOKEN_FIELDS) == (
        90,
        36,
        9,
        15,
        9,
        105,
    )
    assert sorted(turn.total_tokens for turn in oracle.statistics_projection.turn_statistics) == [35, 70]
    assert StatefulCodexProtocolAnalyticsAdapter().analyze_rollouts(sources, "test-user") == oracle
    stateful = StatefulCodexProtocolAnalyticsAdapter()
    stateful.analyze_rollouts(
        [
            _source(_content(records[:3]), _content(records[:3])),
            _source(_content(other_records[:3]), _content(other_records[:3]), other_thread),
        ],
        "test-user",
    )
    stateful.accept_batch()
    assert (
        stateful.analyze_rollouts(
            [
                _source(_content(records), _content(records[3:])),
                _source(_content(other_records), _content(other_records[3:]), other_thread),
            ],
            "test-user",
        )
        == oracle
    )


@pytest.mark.parametrize("reset", [False, True])
@pytest.mark.parametrize("snapshot_type", ["token_count", "token_usage"])
def test_legacy_cumulative_token_snapshots_remain_supported(reset: bool, snapshot_type: str) -> None:
    first = _token_vector(10, 4, 1, 2, 1)
    cumulative = _token_vector(15, 6, 1, 4, 2)
    records = [
        {
            "type": "session_meta",
            "payload": {"id": str(THREAD_ID), "session_id": str(THREAD_ID)},
        },
        {
            "type": "event_msg",
            "payload": {"type": snapshot_type, "info": {"total_token_usage": first}},
        },
        {
            "type": "event_msg",
            "payload": {"type": snapshot_type, "info": {"total_token_usage": cumulative}},
        },
    ]
    if reset:
        records.append(
            {
                "type": "event_msg",
                "payload": {"type": snapshot_type, "info": {"total_token_usage": _token_vector(5, 1, 0, 1, 0)}},
            }
        )
    content = _content(records)

    full = CodexProtocolAnalyticsAdapter().analyze_rollouts([_source(content, content)], "test-user")
    stateful = StatefulCodexProtocolAnalyticsAdapter().analyze_rollouts(
        [_source(content, content)],
        "test-user",
    )

    assert stateful == full
    expected = (20, 7, 1, 5, 2, 25) if reset else (15, 6, 1, 4, 2, 19)
    assert tuple(getattr(stateful.statistics_projection, name) for name in ACCOUNTING_TOKEN_FIELDS) == expected
    for split_at in range(1, len(records)):
        prefix = _content(records[:split_at])
        adapter = StatefulCodexProtocolAnalyticsAdapter()
        adapter.analyze_rollouts([_source(prefix, prefix)], "test-user")
        adapter.accept_batch()
        assert adapter.analyze_rollouts([_source(content, _content(records[split_at:]))], "test-user") == full


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
    oracle = CodexProtocolAnalyticsAdapter().analyze_rollouts([_source(full, full)], "test-user")

    assert [turn.codex_turn_id for turn in incremental.statistics_projection.turn_statistics] == [TURN_B_ID]
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
    oracle = CodexProtocolAnalyticsAdapter().analyze_rollouts([_source(full, full)], "test-user")

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
    stateful = StatefulCodexProtocolAnalyticsAdapter().analyze_rollouts(sources, "test-user")

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
        adapter.analyze_rollouts([_source(_content(records), _content(records))], "test-user")

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
