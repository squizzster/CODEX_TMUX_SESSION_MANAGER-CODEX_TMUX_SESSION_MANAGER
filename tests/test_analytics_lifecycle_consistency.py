"""Group 5: event attribution and accepted outcomes across durable boundaries."""

from dataclasses import replace
from itertools import product
from pathlib import Path

import pytest
from test_rodex_agent_trace import THREAD_ID, TURN_A_ID, TURN_B_ID, _content, _publish_trace
from test_rodex_analytics import _config, _create, _rollout

from rodex.agent_trace import StatefulAgentTraceNormalizer, normalize_rollout_trace
from rodex.analytics import AnalyticsRolloutWorker
from rodex_registry import (
    create_a_rodex_session,
    read_rodex_agent_trace,
    read_rodex_session_statistics,
    read_rodex_session_turn_statistics,
)
from rodex_registry.agent_trace_contract import RodexAgentTraceEvent, RodexAgentTracePublication
from rodex_sql import open_rodex_read_transaction

NOW = "2026-08-16T12:00:20Z"


def _record(kind, payload, second=0):
    return {"type": kind, "payload": payload, "timestamp": f"2026-08-16T12:00:{second:02d}Z"}


def _lifecycle(kind, turn_id, second=0):
    return _record("event_msg", {"type": kind, "turn_id": turn_id}, second)


@pytest.mark.parametrize(
    "stale_kind", ["task_complete", "turn_aborted", "agent_message", "turn_context", "response_item"]
)
@pytest.mark.parametrize("cuts", list(product((False, True), repeat=4)))
def test_stale_explicit_identity_never_steals_active_turn(stale_kind, cuts):
    stale = _lifecycle(stale_kind, TURN_A_ID)
    if stale_kind == "turn_context":
        stale = _record("turn_context", {"turn_id": TURN_A_ID})
    elif stale_kind == "response_item":
        stale = _record(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "internal_chat_message_metadata_passthrough": {"turn_id": TURN_A_ID},
            },
        )
    records = [
        _lifecycle("task_started", TURN_A_ID),
        _lifecycle("task_started", TURN_B_ID),
        stale,
        _record("event_msg", {"type": "agent_message", "message": "newer turn"}),
        _lifecycle("task_complete", TURN_B_ID),
    ]
    normalizer = StatefulAgentTraceNormalizer()
    events = []
    start = 0
    for end in [i + 1 for i, cut in enumerate(cuts) if cut] + [len(records)]:
        batch = normalizer.prepare(
            ((THREAD_ID, _content(*records[start:end]), start),),
            based_on_trace_publication_sequence=None,
            calculated_at_utc=NOW,
        )
        events.extend(batch.events)
        normalizer.accept_batch()
        start = end
    assert [event.codex_turn_id for event in events] == [TURN_A_ID, TURN_B_ID, TURN_A_ID, TURN_B_ID, TURN_B_ID]
    replay = normalize_rollout_trace(
        ((THREAD_ID, _content(*records)),), based_on_trace_publication_sequence=None, calculated_at_utc=NOW
    )
    assert tuple(events) == replay.events
    for split in range(len(records) + 1):
        warm = StatefulAgentTraceNormalizer()
        warm.warmup(((THREAD_ID, _content(*records[:split])),))
        suffix = warm.prepare(
            ((THREAD_ID, _content(*records[split:]), split),),
            based_on_trace_publication_sequence=1,
            calculated_at_utc=NOW,
        )
        assert suffix.events == replay.events[split:]


def test_unpublished_candidate_cannot_change_accepted_context():
    normalizer = StatefulAgentTraceNormalizer()
    normalizer.warmup(((THREAD_ID, _content(_record("turn_context", {"turn_id": TURN_A_ID}))),))
    normalizer.prepare(
        ((THREAD_ID, _content(_lifecycle("task_started", TURN_B_ID)), 1),),
        based_on_trace_publication_sequence=1,
        calculated_at_utc=NOW,
    )
    candidate = normalizer.prepare(
        ((THREAD_ID, _content(_record("event_msg", {"type": "agent_message"})), 1),),
        based_on_trace_publication_sequence=1,
        calculated_at_utc=NOW,
    )
    assert candidate.events[0].codex_turn_id == TURN_A_ID


@pytest.mark.parametrize(
    "terminal_kinds",
    [("turn_completed", "turn_aborted"), ("turn_aborted", "turn_completed"), ("turn_completed", "turn_completed")],
)
@pytest.mark.parametrize("first_timestamp", [None, "2026-08-16T12:00:01Z"])
def test_exact_coordinate_replay_cannot_mutate_lifecycle(tmp_path, terminal_kinds, first_timestamp):
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    events = (
        RodexAgentTraceEvent(THREAD_ID, TURN_A_ID, 0, 0, "turn_started", None),
        RodexAgentTraceEvent(THREAD_ID, TURN_A_ID, 1, 0, terminal_kinds[0], first_timestamp),
        RodexAgentTraceEvent(THREAD_ID, TURN_A_ID, 2, 0, terminal_kinds[1], NOW),
    )
    publication = RodexAgentTracePublication(None, "test-v1", NOW, "complete", events)
    _publish_trace(database, publication)

    def state():
        with open_rodex_read_transaction(database) as connection:
            return connection.execute("SELECT outcome, terminal_at_utc FROM rodex_sessions_codex_turn_states").fetchone()

    expected = state()
    assert expected[0] == terminal_kinds[0].removeprefix("turn_")
    assert (expected[1] is None) == (first_timestamp is None)
    for sequence, event in enumerate(reversed(events), 1):
        _publish_trace(database, replace(publication, based_on_trace_publication_sequence=sequence, events=(event,)))
        assert state() == expected
        assert read_rodex_agent_trace(1, database).durable_event_count == 3


@pytest.mark.parametrize(
    "terminal_kinds",
    [("task_complete", "turn_aborted"), ("turn_aborted", "task_complete"), ("task_complete", "task_complete")],
)
@pytest.mark.parametrize("missing_start", [False, True])
@pytest.mark.parametrize("missing_time", [False, True])
@pytest.mark.parametrize("split", [1, 2, 3, 4])
def test_aggregate_outcome_timestamp_and_duplicate_count_agree_after_restart(
    tmp_path: Path, terminal_kinds, missing_start, missing_time, split
):
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, THREAD_ID)
    records = [
        _record("session_meta", {"id": str(THREAD_ID), "session_id": str(THREAD_ID)}),
        _lifecycle("task_started", TURN_A_ID, 1),
        _lifecycle(terminal_kinds[0], TURN_A_ID, 2),
        _lifecycle(terminal_kinds[1], TURN_A_ID, 3),
    ]
    if missing_start:
        records[1] = _record("event_msg", {"type": "agent_message", "turn_id": TURN_A_ID}, 1)
    if missing_time:
        records[2].pop("timestamp")
    rollout.write_bytes(_content(*records[:split]))
    _create(config)
    worker = AnalyticsRolloutWorker(config)
    assert worker.poll_once() == "up_to_date"
    with rollout.open("ab") as output:
        output.write(_content(*records[split:]))
    assert worker.poll_once() == "up_to_date"
    stats = read_rodex_session_statistics(1, config.rodex_database_path).statistics.projection
    turn = read_rodex_session_turn_statistics(1, TURN_A_ID, config.rodex_database_path).turn
    if missing_start:
        assert turn is None
        assert stats.audit_terminal_events_without_start_ignored_count == 2
        assert stats.turns_completed_count == stats.turns_aborted_count == stats.turns_started_count == 0
    else:
        assert turn.outcome == ("completed" if terminal_kinds[0] == "task_complete" else "aborted")
        assert (turn.terminal_at_utc is None) == missing_time
        assert stats.turns_completed_count == (turn.outcome == "completed")
        assert stats.turns_aborted_count == (turn.outcome == "aborted")
        assert stats.turns_started_count == 1
        assert stats.audit_duplicate_terminals_ignored_count == 1
    before = read_rodex_agent_trace(1, config.rodex_database_path)
    assert len(before.events) == 4
    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"
    assert read_rodex_agent_trace(1, config.rodex_database_path) == before
    assert read_rodex_session_statistics(1, config.rodex_database_path).statistics.projection == stats


@pytest.mark.parametrize("terminal", ["task_complete", "turn_aborted"])
@pytest.mark.parametrize("started", [False, True])
def test_inferred_terminal_identity_is_evidence_without_lifecycle_authority(tmp_path, terminal, started):
    records = [
        _lifecycle("task_started", TURN_A_ID) if started else _record("turn_context", {"turn_id": TURN_A_ID}),
        _record("event_msg", {"type": terminal}),
        _record("event_msg", {"type": "agent_message", "message": "still active"}),
    ]
    trace = normalize_rollout_trace(
        ((THREAD_ID, _content(*records)),), based_on_trace_publication_sequence=None, calculated_at_utc=NOW
    )
    assert [event.codex_turn_id for event in trace.events] == [TURN_A_ID] * 3
    assert trace.events[1].lifecycle_eligible is False
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    _publish_trace(database, trace)
    with open_rodex_read_transaction(database) as connection:
        state = connection.execute("SELECT outcome, terminal_at_utc FROM rodex_sessions_codex_turn_states").fetchone()
    assert state == ("open", None)


def test_response_metadata_and_context_initialization_share_replay_statistics_scope():
    from rodex.analytics_analyzer import (
        AnalyticsAnalyzerSource,
        CodexProtocolAnalyticsAdapter,
        StatefulCodexProtocolAnalyticsAdapter,
    )

    records = [
        _record("session_meta", {"id": str(THREAD_ID), "session_id": str(THREAD_ID)}),
        _lifecycle("task_started", TURN_A_ID, 1),
        _lifecycle("task_started", TURN_B_ID, 2),
        _record(
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "old-explicit",
                "internal_chat_message_metadata_passthrough": {"turn_id": TURN_A_ID},
            },
            3,
        ),
        _record("response_item", {"type": "function_call", "name": "exec_command", "call_id": "new-inferred"}, 4),
        _record("event_msg", {"type": "task_complete"}, 5),
        _record("response_item", {"type": "function_call", "name": "exec_command", "call_id": "still-new"}, 6),
    ]
    content = _content(*records)
    source = AnalyticsAnalyzerSource(THREAD_ID, content, content)
    for adapter in (CodexProtocolAnalyticsAdapter(), StatefulCodexProtocolAnalyticsAdapter()):
        projection = adapter.analyze_rollouts((source,), "test").statistics_projection
        turns = {turn.codex_turn_id: turn for turn in projection.turn_statistics}
        assert turns[TURN_A_ID].model_tool_requests_count == 1
        assert turns[TURN_B_ID].model_tool_requests_count == 2
        assert projection.turns_open_count == 2
        assert projection.audit_duplicate_terminals_ignored_count == 0


def test_duplicate_source_coordinate_precedes_context_and_aggregate_mutation(tmp_path):
    records = [
        _record("session_meta", {"id": str(THREAD_ID), "session_id": str(THREAD_ID)}),
        _lifecycle("task_started", TURN_A_ID, 1),
        _lifecycle("task_complete", TURN_A_ID, 2),
        _lifecycle("task_started", TURN_B_ID, 3),
    ]
    for ordinal, record in enumerate(records):
        record["ordinal"] = ordinal
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, THREAD_ID)
    rollout.write_bytes(_content(*records))
    _create(config)
    worker = AnalyticsRolloutWorker(config)
    assert worker.poll_once() == "up_to_date"
    before = read_rodex_session_statistics(1, config.rodex_database_path).statistics.projection
    with rollout.open("ab") as output:
        output.write(_content(records[1], records[2]))
    assert worker.poll_once() == "up_to_date"
    after = read_rodex_session_statistics(1, config.rodex_database_path).statistics.projection
    assert after == before
    assert read_rodex_agent_trace(1, config.rodex_database_path).durable_event_count == 4
    message = _record("event_msg", {"type": "agent_message", "message": "B still owns context"}, 5)
    message["ordinal"] = 6
    with rollout.open("ab") as output:
        output.write(_content(message))
    assert worker.poll_once() == "up_to_date"
    trace = read_rodex_agent_trace(1, config.rodex_database_path)
    assert trace.events[-1]["codex_turn_id"] == TURN_B_ID
    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"
    assert read_rodex_agent_trace(1, config.rodex_database_path) == trace


def test_conflicting_source_coordinate_cannot_publish_or_accept_context(tmp_path):
    records = [
        _record("session_meta", {"id": str(THREAD_ID), "session_id": str(THREAD_ID)}),
        _lifecycle("task_started", TURN_A_ID, 1),
    ]
    for ordinal, record in enumerate(records):
        record["ordinal"] = ordinal
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, THREAD_ID)
    rollout.write_bytes(_content(*records))
    _create(config)
    worker = AnalyticsRolloutWorker(config)
    assert worker.poll_once() == "up_to_date"
    before = read_rodex_agent_trace(1, config.rodex_database_path)
    with rollout.open("ab") as output:
        output.write(_content({**_lifecycle("task_started", TURN_B_ID, 2), "ordinal": 1}))
    assert worker.poll_once() == "clean_replay"
    assert read_rodex_agent_trace(1, config.rodex_database_path) == before


def test_inferred_start_is_not_proof_for_a_later_explicit_terminal(tmp_path):
    records = [
        _record("turn_context", {"turn_id": TURN_A_ID}),
        _record("event_msg", {"type": "task_started"}),
        _lifecycle("task_complete", TURN_A_ID),
        _record("event_msg", {"type": "agent_message"}),
    ]
    publication = normalize_rollout_trace(
        ((THREAD_ID, _content(*records)),), based_on_trace_publication_sequence=None, calculated_at_utc=NOW
    )
    assert publication.events[1].lifecycle_eligible is False
    assert publication.events[2].lifecycle_eligible is False
    assert publication.events[-1].codex_turn_id == TURN_A_ID
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    _publish_trace(database, publication)
    with open_rodex_read_transaction(database) as connection:
        assert connection.execute("SELECT outcome FROM rodex_sessions_codex_turn_states").fetchone() == ("open",)
