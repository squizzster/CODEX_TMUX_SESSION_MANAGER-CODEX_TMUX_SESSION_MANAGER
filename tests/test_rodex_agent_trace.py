from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from rodex.agent_trace import StatefulAgentTraceNormalizer, normalize_rollout_trace
from rodex.agent_trace_commands import _safe_event_body, execute_agent_trace_command
from rodex.errors import RodexLaunchError
from rodex_registry import (
    RodexRuntimeId,
    RodexSessionStatisticsConflictError,
    create_a_rodex_session,
    read_rodex_agent_trace,
    read_rodex_agent_trace_cursor,
    record_a_rodex_session_runtime_resume,
    split_codex_item_id_into_signed_bigints,
    split_codex_thread_id_into_signed_bigints,
    split_codex_turn_id_into_signed_bigints,
)
from rodex_registry.agent_trace_contract import (
    RodexAgentTraceEvent,
    RodexAgentTracePublication,
    TraceMessage,
    TraceSubagentActivity,
    TraceToolCall,
    prepare_agent_trace_publication,
)
from rodex_registry.agent_trace_writer import publish_agent_trace_in_transaction
from rodex_sql import open_rodex_transaction

THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
CHILD_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f83")
REPLACEMENT_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f84")
TURN_A_ID = "00000000-0000-7000-8000-00000000000a"
TURN_B_ID = "00000000-0000-7000-8000-00000000000b"
ITEM_A_ID = "00000000-0000-7000-8000-0000000000aa"


def _content(*records: dict[str, object]) -> bytes:
    return b"".join(json.dumps(record).encode() + b"\n" for record in records)


def _publish_trace(database: Path, publication: RodexAgentTracePublication) -> object:
    prepared = prepare_agent_trace_publication(publication)
    with open_rodex_transaction(database) as connection:
        return publish_agent_trace_in_transaction(
            connection,
            1,
            prepared,
            model_name_ids={},
            reasoning_effort_name_ids={},
        )


def test_trace_cursor_is_empty_then_tracks_the_latest_public_event(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    assert read_rodex_agent_trace_cursor(1, database) is None
    publication = RodexAgentTracePublication(
        None,
        "test-v1",
        "2026-08-26T12:00:00Z",
        "complete",
        (
            RodexAgentTraceEvent(THREAD_ID, None, 1, 0, "session_metadata", None),
            RodexAgentTraceEvent(THREAD_ID, None, 2, 0, "compaction", None),
        ),
    )
    _publish_trace(database, publication)

    snapshot = read_rodex_agent_trace(1, database)

    assert read_rodex_agent_trace_cursor(1, database) == uuid.UUID(
        snapshot.events[-1]["event_id"]
    )


def test_stateful_trace_links_later_item_batches_to_the_accepted_turn() -> None:
    normalizer = StatefulAgentTraceNormalizer()
    first = normalizer.prepare(
        (
            (
                THREAD_ID,
                _content(
                    {
                        "ordinal": 1,
                        "timestamp": "2026-08-26T12:00:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_started",
                            "turn_id": "00000000-0000-7000-8000-00000000000a",
                        },
                    },
                    {
                        "ordinal": 2,
                        "timestamp": "2026-08-26T12:00:01Z",
                        "type": "turn_context",
                        "payload": {
                            "turn_id": "00000000-0000-7000-8000-00000000000a",
                            "model": "gpt-test",
                            "effort": "xhigh",
                        },
                    },
                ),
            ),
        ),
        based_on_trace_publication_sequence=None,
        calculated_at_utc="2026-08-26T12:00:02Z",
    )
    assert {event.codex_turn_id for event in first.events} == {
        "00000000-0000-7000-8000-00000000000a"
    }
    normalizer.accept_batch()

    second = normalizer.prepare(
        (
            (
                THREAD_ID,
                _content(
                    {
                        "ordinal": 3,
                        "timestamp": "2026-08-26T12:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "CommandExecution",
                                "id": "item-command",
                                "command": ["secret-command", "--flag"],
                                "status": "completed",
                                "exit_code": 0,
                                "stdout": "done",
                            },
                        },
                    }
                ),
            ),
        ),
        based_on_trace_publication_sequence=1,
        calculated_at_utc="2026-08-26T12:00:04Z",
    )

    assert len(second.events) == 1
    assert second.events[0].codex_turn_id == "00000000-0000-7000-8000-00000000000a"
    assert second.events[0].event_kind == "command_execution"
    assert second.events[0].detail.command_argument_count == 2  # type: ignore[union-attr]


def test_stateful_trace_pairs_namespaced_function_request_and_later_output() -> None:
    normalizer = StatefulAgentTraceNormalizer()
    request = normalizer.prepare(
        (
            (
                THREAD_ID,
                _content(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "namespace": "collaboration",
                            "name": "spawn_agent",
                            "call_id": "call-a",
                            "arguments": '{"task_name":"/root/review"}',
                        },
                    }
                ),
            ),
        ),
        based_on_trace_publication_sequence=None,
        calculated_at_utc="2026-08-26T12:00:00Z",
    )
    normalizer.accept_batch()
    response = normalizer.prepare(
        (
            (
                THREAD_ID,
                _content(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-a",
                            "output": '{"task_name":"/root/review"}',
                        },
                    }
                ),
                1,
            ),
        ),
        based_on_trace_publication_sequence=1,
        calculated_at_utc="2026-08-26T12:00:01Z",
    )

    assert request.events[0].detail.tool_name == "collaboration.spawn_agent"  # type: ignore[union-attr]
    assert response.events[0].detail.tool_name == "collaboration.spawn_agent"  # type: ignore[union-attr]
    assert request.events[0].detail.request_utf8_bytes > 0  # type: ignore[union-attr]
    assert response.events[0].detail.response_utf8_bytes > 0  # type: ignore[union-attr]


def test_normalizer_derives_typed_message_usage_rate_limit_and_unknown_events() -> None:
    publication = normalize_rollout_trace(
        (
            (
                THREAD_ID,
                _content(
                    {
                        "ordinal": 0,
                        "type": "session_meta",
                        "payload": {"id": str(THREAD_ID)},
                    },
                    {
                        "ordinal": 1,
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "AgentMessage",
                                "id": "message-a",
                                "phase": "final_answer",
                                "content": [{"type": "Text", "text": "finished"}],
                            },
                        },
                    },
                    {
                        "ordinal": 2,
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "usage": {"total_tokens": 120},
                            "rate_limits": [
                                {
                                    "limit_id": "codex",
                                    "primary": {
                                        "used_percent": 28,
                                        "window_minutes": 10080,
                                        "resets_at": 2_000_000_000,
                                    },
                                    "plan_type": "pro",
                                }
                            ],
                        },
                    },
                    {"ordinal": 3, "type": "future_record", "payload": {}},
                ),
            ),
        ),
        based_on_trace_publication_sequence=None,
        calculated_at_utc="2026-08-26T12:00:04Z",
    )

    assert [event.event_kind for event in publication.events] == [
        "session_metadata",
        "message",
        "token_usage",
        "rate_limit",
        "unrecognized_record",
    ]
    assert publication.coverage_state == "gapped"


def test_normalizer_uses_authenticated_physical_ordinals_and_append_offset() -> None:
    publication = normalize_rollout_trace(
        (
            (
                THREAD_ID,
                _content(
                    {"type": "session_meta", "payload": {"id": str(THREAD_ID)}},
                    {"type": "compacted", "payload": {}},
                ),
                41,
            ),
        ),
        based_on_trace_publication_sequence=None,
        calculated_at_utc="2026-08-26T12:00:00Z",
    )

    assert [event.source_record_ordinal for event in publication.events] == [41, 42]
    assert [event.event_kind for event in publication.events] == [
        "session_metadata",
        "compaction",
    ]
    assert publication.coverage_state == "complete"


def test_normalizer_covers_canonical_message_custom_tool_and_subagent_shapes() -> None:
    publication = normalize_rollout_trace(
        (
            (
                THREAD_ID,
                _content(
                    {
                        "ordinal": 1,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "developer",
                            "content": "policy",
                        },
                    },
                    {
                        "ordinal": 2,
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "tool": "collaboration.spawn_agent",
                            "input": "request",
                        },
                    },
                    {
                        "ordinal": 3,
                        "type": "event_msg",
                        "payload": {
                            "type": "sub_agent_activity",
                            "agent_thread_id": str(CHILD_THREAD_ID),
                            "status": "completed",
                            "agent_path": "/root/review",
                        },
                    },
                ),
            ),
        ),
        based_on_trace_publication_sequence=None,
        calculated_at_utc="2026-08-26T12:00:00Z",
    )

    message, tool, activity = publication.events
    assert message.detail.message_role == "unknown"  # type: ignore[union-attr]
    assert message.detail.body_utf8_bytes == 6  # type: ignore[union-attr]
    assert tool.detail.tool_name == "collaboration.spawn_agent"  # type: ignore[union-attr]
    assert tool.detail.request_utf8_bytes == 7  # type: ignore[union-attr]
    assert activity.detail.target_codex_thread_id == CHILD_THREAD_ID  # type: ignore[union-attr]


def test_normalizer_retains_empty_tool_request_and_output_activity_kinds(
    tmp_path: Path,
) -> None:
    publication = normalize_rollout_trace(
        (
            (
                THREAD_ID,
                _content(
                    {
                        "ordinal": 1,
                        "type": "response_item",
                        "payload": {
                            "id": "item-1",
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "test_tool",
                            "arguments": "",
                        },
                    },
                    {
                        "ordinal": 2,
                        "type": "response_item",
                        "payload": {
                            "id": "item-2",
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "",
                        },
                    },
                ),
            ),
        ),
        based_on_trace_publication_sequence=None,
        calculated_at_utc="2026-08-26T12:00:00Z",
    )

    assert [event.detail.activity_kind for event in publication.events] == [  # type: ignore[union-attr]
        "request",
        "output",
    ]
    assert [event.detail.request_utf8_bytes for event in publication.events] == [  # type: ignore[union-attr]
        0,
        0,
    ]
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    _publish_trace(database, publication)
    assert [
        event["detail"]["activity_kind"]
        for event in read_rodex_agent_trace(1, database).events
    ] == ["request", "output"]


def test_trace_publication_is_deduplicated_typed_and_contains_no_bodies(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    content = _content(
        {
            "ordinal": 0,
            "timestamp": "2026-08-26T12:00:00Z",
            "type": "session_meta",
            "payload": {"id": str(THREAD_ID)},
        },
        {
            "ordinal": 1,
            "timestamp": "2026-08-26T12:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "command-a",
                    "command": ["super-secret-command"],
                    "stdout": "super-secret-output",
                    "status": "completed",
                    "exit_code": 0,
                },
            },
        },
    )
    first = normalize_rollout_trace(
        ((THREAD_ID, content),),
        based_on_trace_publication_sequence=None,
        calculated_at_utc="2026-08-26T12:00:02Z",
    )
    first_receipt = _publish_trace(database, first)
    replay = normalize_rollout_trace(
        ((THREAD_ID, content),),
        based_on_trace_publication_sequence=1,
        calculated_at_utc="2026-08-26T12:00:03Z",
    )
    second_receipt = _publish_trace(database, replay)

    assert first_receipt.durable_event_count == 2
    assert second_receipt.trace_publication_sequence == 2
    assert second_receipt.durable_event_count == 2
    assert b"super-secret-command" not in database.read_bytes()
    assert b"super-secret-output" not in database.read_bytes()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT command_argument_count, stdout_utf8_bytes, exit_code "
            "FROM rodex_sessions_agent_trace_command_executions"
        ).fetchone() == (1, 19, 0)
    snapshot = read_rodex_agent_trace(1, database)
    assert snapshot.trace_publication_sequence == 2
    assert [event["event_kind"] for event in snapshot.events] == [
        "session_metadata",
        "command_execution",
    ]
    command_event_id = snapshot.events[1]["event_id"]
    with (
        pytest.raises(sqlite3.IntegrityError),
        open_rodex_transaction(database) as connection,
    ):
        connection.execute(
            "INSERT INTO rodex_sessions_agent_trace_messages "
            "(rodex_sessions_agent_trace_events_id, message_phase, message_role, "
            "content_block_count, body_utf8_bytes, body_capture_state) "
            "VALUES (?, 'unknown', 'unknown', 0, 0, 'unavailable')",
            (command_event_id,),
        )


def test_trace_coverage_remains_gapped_while_unrecognized_events_are_durable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    _publish_trace(
        database,
        RodexAgentTracePublication(
            None,
            "test-v1",
            "2026-08-26T12:00:00Z",
            "gapped",
            (
                RodexAgentTraceEvent(
                    THREAD_ID, None, 1, 0, "unrecognized_record", None, None
                ),
            ),
        ),
    )

    _publish_trace(
        database,
        RodexAgentTracePublication(
            1,
            "test-v1",
            "2026-08-26T12:00:01Z",
            "complete",
            (RodexAgentTraceEvent(THREAD_ID, None, 2, 0, "session_metadata", None, None),),
        ),
    )

    snapshot = read_rodex_agent_trace(1, database)
    assert snapshot.coverage_state == "gapped"
    assert snapshot.unrecognized_record_count == 1


def test_trace_coverage_preserves_a_prior_gap_without_unrecognized_events(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    _publish_trace(
        database,
        RodexAgentTracePublication(
            None,
            "test-v1",
            "2026-08-26T12:00:00Z",
            "gapped",
            (RodexAgentTraceEvent(THREAD_ID, None, 1, 0, "session_metadata", None, None),),
        ),
    )

    _publish_trace(
        database,
        RodexAgentTracePublication(
            1,
            "test-v1",
            "2026-08-26T12:00:01Z",
            "complete",
            (RodexAgentTraceEvent(THREAD_ID, None, 2, 0, "compaction", None, None),),
        ),
    )

    snapshot = read_rodex_agent_trace(1, database)
    assert snapshot.coverage_state == "gapped"
    assert snapshot.unrecognized_record_count == 0


def test_tool_request_and_output_share_one_canonical_public_tool_call(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    request_event = RodexAgentTraceEvent(
        THREAD_ID,
        None,
        1,
        0,
        "tool_call",
        None,
        TraceToolCall(
            "request-item",
            "call-1",
            "collaboration.spawn_agent",
            None,
            12,
            0,
            "rollout_reference",
            "request",
        ),
    )
    output_event = RodexAgentTraceEvent(
        THREAD_ID,
        None,
        2,
        0,
        "tool_call",
        None,
        TraceToolCall(
            "request-item",
            None,
            "function_call_output",
            "completed",
            0,
            24,
            "rollout_reference",
            "output",
        ),
    )
    request_publication = RodexAgentTracePublication(
        None,
        "test-v1",
        "2026-08-26T12:00:00Z",
        "complete",
        (request_event,),
    )
    output_publication = RodexAgentTracePublication(
        1,
        "test-v1",
        "2026-08-26T12:00:01Z",
        "complete",
        (output_event,),
    )

    _publish_trace(database, request_publication)
    _publish_trace(database, output_publication)

    events = read_rodex_agent_trace(1, database).events
    assert [event["detail"]["activity_kind"] for event in events] == [
        "request",
        "output",
    ]
    assert events[0]["detail"]["tool_call_id"] == events[1]["detail"]["tool_call_id"]
    assert events[0]["detail"]["item_id"] == events[1]["detail"]["item_id"]
    assert events[0]["detail"]["item_id"] is None
    assert events[0]["detail"]["item_alias"] == "request-item"
    assert events[1]["detail"]["item_alias"] == "request-item"
    assert events[0]["detail"]["item_public_id"] == events[1]["detail"]["item_public_id"]
    assert uuid.UUID(events[0]["detail"]["item_public_id"])
    assert {event["detail"]["tool_name"] for event in events} == {
        "collaboration.spawn_agent"
    }
    assert uuid.UUID(events[0]["event_id"])
    assert uuid.UUID(events[1]["event_id"])
    assert read_rodex_agent_trace(
        1, database, after_event_id=events[0]["event_id"]
    ).events == (events[1],)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_codex_tool_calls"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_agent_trace_tool_call_activities"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_codex_items"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT codex_item_alias FROM rodex_sessions_codex_item_aliases"
        ).fetchone() == ("request-item",)
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_codex_tool_call_aliases"
        ).fetchone() == (2,)
        replacement_name_id = connection.execute(
            "INSERT INTO tool_names (tool_name) VALUES ('replacement.tool') RETURNING id"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            connection.execute(
                "UPDATE rodex_sessions_codex_tool_calls SET tool_names_id = ?",
                (replacement_name_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            connection.execute("UPDATE rodex_sessions_codex_tool_calls SET id = id + 100")


def test_tool_request_can_verify_an_output_first_canonical_call_name(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    output = TraceToolCall(
        "request-item",
        None,
        "function_call_output",
        "completed",
        0,
        24,
        "rollout_reference",
        "output",
    )
    request = TraceToolCall(
        "request-item",
        "call-1",
        "collaboration.spawn_agent",
        None,
        12,
        0,
        "rollout_reference",
        "request",
    )

    _publish_trace(
        database,
        RodexAgentTracePublication(
            None,
            "test-v1",
            "2026-08-26T12:00:00Z",
            "complete",
            (RodexAgentTraceEvent(THREAD_ID, None, 1, 0, "tool_call", None, output),),
        ),
    )
    _publish_trace(
        database,
        RodexAgentTracePublication(
            1,
            "test-v1",
            "2026-08-26T12:00:01Z",
            "complete",
            (RodexAgentTraceEvent(THREAD_ID, None, 2, 0, "tool_call", None, request),),
        ),
    )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT names.tool_name FROM rodex_sessions_codex_tool_calls AS calls "
            "JOIN tool_names AS names ON names.id = calls.tool_names_id"
        ).fetchone() == ("collaboration.spawn_agent",)


def test_canonical_item_uuid_has_distinct_semantic_and_public_identities(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    message = TraceMessage(
        ITEM_A_ID,
        "commentary",
        "assistant",
        1,
        4,
        "rollout_reference",
    )
    _publish_trace(
        database,
        RodexAgentTracePublication(
            None,
            "test-v1",
            "2026-08-26T12:00:00Z",
            "complete",
            (RodexAgentTraceEvent(THREAD_ID, TURN_A_ID, 1, 0, "message", None, message),),
        ),
    )

    detail = read_rodex_agent_trace(1, database).events[0]["detail"]
    assert detail["item_id"] == ITEM_A_ID
    assert detail["item_alias"] is None
    assert detail["item_public_id"] != ITEM_A_ID
    assert uuid.UUID(detail["item_public_id"])
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT codex_item_id_signed_bigint_1, codex_item_id_signed_bigint_2, "
            "item_public_id_signed_bigint_1, item_public_id_signed_bigint_2 "
            "FROM rodex_sessions_codex_items"
        ).fetchone()
        assert row is not None
        assert row[:2] == split_codex_item_id_into_signed_bigints(uuid.UUID(ITEM_A_ID))
        assert row[:2] != row[2:]
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_codex_item_aliases"
        ).fetchone() == (0,)

    noncanonical = RodexAgentTracePublication(
        1,
        "test-v1",
        "2026-08-26T12:00:01Z",
        "complete",
        (
            RodexAgentTraceEvent(
                THREAD_ID,
                TURN_A_ID,
                2,
                0,
                "message",
                None,
                TraceMessage(
                    ITEM_A_ID.upper(),
                    "commentary",
                    "assistant",
                    1,
                    4,
                    "rollout_reference",
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="canonical lowercase UUID text"):
        _publish_trace(database, noncanonical)
    assert read_rodex_agent_trace(1, database).trace_publication_sequence == 1


def test_activity_scope_fences_every_typed_dependency_and_is_immutable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    events = tuple(
        RodexAgentTraceEvent(
            THREAD_ID,
            turn_id,
            ordinal,
            0,
            "message",
            None,
            TraceMessage(
                f"message-{ordinal}",
                "commentary",
                "assistant",
                1,
                ordinal,
                "rollout_reference",
            ),
        )
        for ordinal, turn_id in enumerate((TURN_A_ID, TURN_B_ID), start=1)
    )
    _publish_trace(
        database,
        RodexAgentTracePublication(
            None,
            "test-v1",
            "2026-08-26T12:00:00Z",
            "complete",
            events,
        ),
    )

    with open_rodex_transaction(database) as connection:
        rows = connection.execute(
            "SELECT events.id, events.rodex_sessions_codex_activity_scopes_id, "
            "messages.id, messages.rodex_sessions_codex_items_id "
            "FROM rodex_sessions_agent_trace_events AS events "
            "JOIN rodex_sessions_agent_trace_messages AS messages "
            "ON messages.rodex_sessions_agent_trace_events_id = events.id "
            "ORDER BY events.source_record_ordinal"
        ).fetchall()
        assert len(rows) == 2
        first_event_id, first_scope_id, first_message_id, first_item_id = rows[0]
        _, second_scope_id, _, second_item_id = rows[1]
        assert first_scope_id != second_scope_id

        with pytest.raises(sqlite3.IntegrityError, match="trace detail is immutable"):
            connection.execute(
                "UPDATE rodex_sessions_agent_trace_messages "
                "SET rodex_sessions_codex_activity_scopes_id = ? WHERE id = ?",
                (second_scope_id, first_message_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="trace detail is immutable"):
            connection.execute(
                "UPDATE rodex_sessions_agent_trace_messages "
                "SET rodex_sessions_codex_items_id = ? WHERE id = ?",
                (second_item_id, first_message_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="scope identity is immutable"):
            connection.execute(
                "UPDATE rodex_sessions_codex_activity_scopes "
                "SET rodex_sessions_codex_turns_id = NULL WHERE id = ?",
                (first_scope_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="event provenance is immutable"):
            connection.execute(
                "UPDATE rodex_sessions_agent_trace_events "
                "SET event_time_utc = '2026-08-26T12:00:09Z' WHERE id = ?",
                (first_event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="item identity is immutable"):
            connection.execute(
                "UPDATE rodex_sessions_codex_items "
                "SET item_public_id_signed_bigint_1 = 99 WHERE id = ?",
                (first_item_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="trace detail is immutable"):
            connection.execute(
                "DELETE FROM rodex_sessions_agent_trace_messages WHERE id = ?",
                (first_message_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="event provenance is immutable"):
            connection.execute(
                "DELETE FROM rodex_sessions_agent_trace_events WHERE id = ?",
                (first_event_id,),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="item alias identity is immutable"
        ):
            connection.execute(
                "UPDATE rodex_sessions_codex_item_aliases "
                "SET rodex_sessions_codex_items_id = ? "
                "WHERE rodex_sessions_codex_items_id = ?",
                (second_item_id, first_item_id),
            )


def test_trace_append_advances_persisted_counts_without_recounting_the_ledger(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    detail = TraceMessage(None, "commentary", "assistant", 1, 4, "rollout_reference")
    _publish_trace(
        database,
        RodexAgentTracePublication(
            None,
            "test-v1",
            "2026-08-26T12:00:00Z",
            "complete",
            (RodexAgentTraceEvent(THREAD_ID, None, 1, 0, "message", None, detail),),
        ),
    )
    statements: list[str] = []
    prepared = prepare_agent_trace_publication(
        RodexAgentTracePublication(
            1,
            "test-v1",
            "2026-08-26T12:00:01Z",
            "complete",
            (RodexAgentTraceEvent(THREAD_ID, None, 2, 0, "message", None, detail),),
        )
    )
    with open_rodex_transaction(database) as connection:
        connection.set_trace_callback(statements.append)
        receipt = publish_agent_trace_in_transaction(
            connection,
            1,
            prepared,
            model_name_ids={},
            reasoning_effort_name_ids={},
        )
        connection.set_trace_callback(None)

    assert receipt.durable_event_count == 2
    normalized_sql = [" ".join(statement.upper().split()) for statement in statements]
    assert not any("SELECT COUNT(*)" in statement for statement in normalized_sql)
    assert not any("SUM(EVENT_KIND" in statement for statement in normalized_sql)
    assert not any(
        "INSERT INTO RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES" in statement
        for statement in normalized_sql
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_codex_activity_scopes"
        ).fetchone() == (1,)


def test_trace_rejects_mutation_at_an_already_published_rollout_coordinate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    original = RodexAgentTracePublication(
        None,
        "test-v1",
        "2026-08-26T12:00:00Z",
        "complete",
        (
            RodexAgentTraceEvent(
                THREAD_ID,
                None,
                1,
                0,
                "message",
                "2026-08-26T12:00:00Z",
                TraceMessage(None, "commentary", "assistant", 1, 4, "rollout_reference"),
            ),
        ),
    )
    _publish_trace(database, original)
    changed = RodexAgentTracePublication(
        1,
        "test-v1",
        "2026-08-26T12:00:01Z",
        "complete",
        (
            RodexAgentTraceEvent(
                THREAD_ID,
                None,
                1,
                0,
                "message",
                "2026-08-26T12:00:00Z",
                TraceMessage(None, "commentary", "assistant", 1, 5, "rollout_reference"),
            ),
        ),
    )

    with pytest.raises(
        RodexSessionStatisticsConflictError, match="changed after trace publication"
    ):
        _publish_trace(database, changed)
    assert read_rodex_agent_trace(1, database).trace_publication_sequence == 1


def test_trace_requires_the_exact_typed_detail_for_each_event_kind(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    publication = RodexAgentTracePublication(
        None,
        "test-v1",
        "2026-08-26T12:00:00Z",
        "complete",
        (RodexAgentTraceEvent(THREAD_ID, None, 1, 0, "message", None),),
    )

    with pytest.raises(ValueError, match="requires TraceMessage detail"):
        _publish_trace(database, publication)


def test_subagent_activity_retains_a_128_bit_target_before_target_registration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    publication = RodexAgentTracePublication(
        None,
        "test-v1",
        "2026-08-26T12:00:00Z",
        "complete",
        (
            RodexAgentTraceEvent(
                THREAD_ID,
                None,
                1,
                0,
                "subagent_activity",
                None,
                TraceSubagentActivity(CHILD_THREAD_ID, "started", "/root/review"),
            ),
        ),
    )

    _publish_trace(database, publication)

    detail = read_rodex_agent_trace(1, database).events[0]["detail"]
    assert detail["target_codex_thread_id"] == str(CHILD_THREAD_ID)
    with sqlite3.connect(database) as connection:
        halves = connection.execute(
            "SELECT identities.codex_thread_public_id_signed_bigint_1, "
            "identities.codex_thread_public_id_signed_bigint_2 "
            "FROM rodex_sessions_agent_trace_subagent_activities AS activity "
            "JOIN codex_threads AS identities "
            "ON identities.id = activity.target_codex_threads_id"
        ).fetchone()
    assert halves == split_codex_thread_id_into_signed_bigints(CHILD_THREAD_ID)


def test_subagent_activity_event_foreign_key_fences_its_session(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    create_a_rodex_session(database, codex_session_id=uuid.UUID(int=THREAD_ID.int + 2))
    publication = RodexAgentTracePublication(
        None,
        "test-v1",
        "2026-08-26T12:00:00Z",
        "complete",
        (
            RodexAgentTraceEvent(
                THREAD_ID,
                None,
                1,
                0,
                "subagent_activity",
                None,
                TraceSubagentActivity(CHILD_THREAD_ID, "started", "/root/review"),
            ),
        ),
    )
    _publish_trace(database, publication)

    with (
        pytest.raises(sqlite3.IntegrityError),
        open_rodex_transaction(database) as connection,
    ):
        connection.execute(
            "UPDATE rodex_sessions_agent_trace_subagent_activities "
            "SET rodex_sessions_id = 2"
        )


def test_initial_and_followup_requests_are_distinct_and_link_distinct_agent_turns(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    child_turn_ids = (
        uuid.UUID("00000000-0000-7000-8000-0000000000c1"),
        uuid.UUID("00000000-0000-7000-8000-0000000000c2"),
    )
    with open_rodex_transaction(database) as connection:
        child_identity_id = connection.execute(
            "INSERT INTO codex_threads "
            "(codex_thread_public_id_signed_bigint_1, "
            "codex_thread_public_id_signed_bigint_2) VALUES (?, ?) RETURNING id",
            split_codex_thread_id_into_signed_bigints(CHILD_THREAD_ID),
        ).fetchone()[0]
        child_membership_id = connection.execute(
            "INSERT INTO rodex_sessions_codex_threads "
            "(rodex_sessions_id, codex_threads_id, first_linked_at_utc) "
            "VALUES (1, ?, '2026-08-27T03:25:30Z') RETURNING id",
            (child_identity_id,),
        ).fetchone()[0]
        for ordinal, (turn_id, started, terminal, outcome) in enumerate(
            (
                (
                    child_turn_ids[0],
                    "2026-08-27T03:25:30.982Z",
                    "2026-08-27T03:25:41.223Z",
                    "completed",
                ),
                (
                    child_turn_ids[1],
                    "2026-08-27T03:27:04.580Z",
                    None,
                    "open",
                ),
            ),
            start=1,
        ):
            public_id = uuid.UUID(int=0x100 + ordinal)
            turn_row_id = connection.execute(
                "INSERT INTO rodex_sessions_codex_turns "
                "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
                "turn_public_id_signed_bigint_1, turn_public_id_signed_bigint_2, "
                "codex_turn_id_signed_bigint_1, codex_turn_id_signed_bigint_2) "
                "VALUES (1, ?, ?, ?, ?, ?) RETURNING id",
                (
                    child_membership_id,
                    *split_codex_thread_id_into_signed_bigints(public_id),
                    *split_codex_turn_id_into_signed_bigints(turn_id),
                ),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO rodex_sessions_codex_turn_states "
                "(rodex_sessions_id, rodex_sessions_codex_turns_id, "
                "started_at_utc, terminal_at_utc, outcome) "
                "VALUES (1, ?, ?, ?, ?)",
                (turn_row_id, started, terminal, outcome),
            )
    parent_turn_ids = (
        "00000000-0000-7000-8000-0000000000a1",
        "00000000-0000-7000-8000-0000000000a2",
    )
    publication = normalize_rollout_trace(
        (
            (
                THREAD_ID,
                _content(
                    {
                        "ordinal": 1,
                        "timestamp": "2026-08-27T03:25:27.142Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": parent_turn_ids[0]},
                    },
                    {
                        "ordinal": 2,
                        "timestamp": "2026-08-27T03:25:27.537Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "UserMessage",
                                "id": "00000000-0000-7000-8000-0000000000d1",
                                "content": [{"type": "text", "text": "first ever word"}],
                            },
                        },
                    },
                    {
                        "ordinal": 3,
                        "timestamp": "2026-08-27T03:25:30.924Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "namespace": "collaboration",
                            "name": "spawn_agent",
                            "call_id": "call-first-word",
                            "arguments": "encrypted",
                        },
                    },
                    {
                        "ordinal": 4,
                        "timestamp": "2026-08-27T03:25:30.962Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "SubAgentActivity",
                                "id": "call-first-word",
                                "kind": "started",
                                "agent_thread_id": str(CHILD_THREAD_ID),
                                "agent_path": "/root/first_human_word",
                            },
                        },
                    },
                    {
                        "ordinal": 5,
                        "timestamp": "2026-08-27T03:25:44.433Z",
                        "type": "event_msg",
                        "payload": {"type": "task_complete"},
                    },
                    {
                        "ordinal": 6,
                        "timestamp": "2026-08-27T03:26:52.374Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": parent_turn_ids[1]},
                    },
                    {
                        "ordinal": 7,
                        "timestamp": "2026-08-27T03:26:52.391Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "UserMessage",
                                "id": "00000000-0000-7000-8000-0000000000d2",
                                "content": [
                                    {"type": "text", "text": "stock market Iran Trump"}
                                ],
                            },
                        },
                    },
                    {
                        "ordinal": 8,
                        "timestamp": "2026-08-27T03:27:04.500Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "namespace": "collaboration",
                            "name": "followup_task",
                            "call_id": "call-stock-market",
                            "arguments": "encrypted",
                        },
                    },
                    {
                        "ordinal": 9,
                        "timestamp": "2026-08-27T03:27:04.578Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "SubAgentActivity",
                                "id": "call-stock-market",
                                "kind": "interacted",
                                "agent_thread_id": str(CHILD_THREAD_ID),
                                "agent_path": "/root/first_human_word",
                            },
                        },
                    },
                ),
            ),
        ),
        based_on_trace_publication_sequence=None,
        calculated_at_utc="2026-08-27T03:27:05Z",
    )

    _publish_trace(database, publication)

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT requests.id, requests.agent_request_public_id_signed_bigint_1, "
            "requests.agent_request_public_id_signed_bigint_2, "
            "messages.rodex_sessions_agent_trace_events_id, "
            "target_turns.target_rodex_sessions_codex_turns_id "
            "FROM rodex_sessions_agent_requests AS requests "
            "JOIN rodex_sessions_agent_trace_messages AS messages "
            "ON messages.id = "
            "requests.parent_rodex_sessions_agent_trace_messages_id "
            "JOIN rodex_sessions_agent_request_target_turns AS target_turns "
            "ON target_turns.rodex_sessions_agent_requests_id = requests.id "
            "ORDER BY requests.id"
        ).fetchall()
        request_public_ids = [
            uuid.UUID(int=((first % (1 << 64)) << 64) | (second % (1 << 64)))
            for _, first, second, _, _ in rows
        ]
    assert len(rows) == 2
    assert request_public_ids[0] != request_public_ids[1]
    assert [
        event["source_record_ordinal"]
        for event in read_rodex_agent_trace(1, database).events
        if event["event_kind"] == "message"
    ] == [2, 7]
    request_events = [
        event
        for event in read_rodex_agent_trace(1, database).events
        if event["event_kind"] == "subagent_activity"
    ]
    turn_requests = [event["detail"]["turn_request"] for event in request_events]
    assert [request["request_id"] for request in turn_requests] == [
        str(request_public_ids[0]),
        str(request_public_ids[1]),
    ]
    assert [request["request_kind"] for request in turn_requests] == [
        "initial",
        "follow_up",
    ]
    assert [
        event["detail"]["collaboration_invocation"]["tool_name"] for event in request_events
    ] == [
        "collaboration.spawn_agent",
        "collaboration.followup_task",
    ]
    assert [event["detail"]["target_codex_thread_id"] for event in request_events] == [
        str(CHILD_THREAD_ID),
        str(CHILD_THREAD_ID),
    ]
    assert [request["target_codex_turn_id"] for request in turn_requests] == [
        str(child_turn_ids[0]),
        str(child_turn_ids[1]),
    ]
    assert [request["target_turn_outcome"] for request in turn_requests] == [
        "completed",
        "open",
    ]


def test_send_message_activity_exposes_invocation_without_a_turn_request(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    parent_turn_id = "00000000-0000-7000-8000-0000000000d0"
    call_id = "call_fnfkboC80cNGCck6HG8rT3Z5"
    arguments = '{"target":"/root/review","message":"gAAAA_TEST_TOKEN"}'
    publication = normalize_rollout_trace(
        (
            (
                THREAD_ID,
                _content(
                    {
                        "ordinal": 1,
                        "timestamp": "2026-08-27T23:54:00Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": parent_turn_id},
                    },
                    {
                        "ordinal": 2,
                        "timestamp": "2026-08-27T23:54:14.419Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "namespace": "collaboration",
                            "name": "send_message",
                            "call_id": call_id,
                            "arguments": arguments,
                        },
                    },
                    {
                        "ordinal": 3,
                        "timestamp": "2026-08-27T23:54:14.421Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "SubAgentActivity",
                                "id": call_id,
                                "kind": "interacted",
                                "agent_thread_id": str(CHILD_THREAD_ID),
                                "agent_path": "/root/review",
                            },
                        },
                    },
                ),
            ),
        ),
        based_on_trace_publication_sequence=None,
        calculated_at_utc="2026-08-27T23:54:15Z",
    )

    _publish_trace(database, publication)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_agent_requests"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_agent_request_target_turns"
        ).fetchone() == (0,)
    detail = next(
        event["detail"]
        for event in read_rodex_agent_trace(1, database).events
        if event["event_kind"] == "subagent_activity"
    )
    assert detail == {
        "target_codex_thread_id": str(CHILD_THREAD_ID),
        "activity_kind": "interacted",
        "agent_path": "/root/review",
        "collaboration_invocation": {
            "tool_call_id": detail["collaboration_invocation"]["tool_call_id"],
            "source_call_id": call_id,
            "tool_name": "collaboration.send_message",
            "arguments_utf8_bytes": len(arguments.encode()),
            "arguments_capture_state": "encrypted",
        },
        "turn_request": None,
    }


def test_request_commits_before_target_registration_then_links_only_its_target_turn(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    parent_turn_id = "00000000-0000-7000-8000-0000000000e1"
    target_turn_id = "00000000-0000-7000-8000-0000000000e2"
    later_target_turn_id = "00000000-0000-7000-8000-0000000000e3"
    first_publication = RodexAgentTracePublication(
        None,
        "test-v2",
        "2026-08-27T03:25:31Z",
        "complete",
        (
            RodexAgentTraceEvent(
                THREAD_ID,
                parent_turn_id,
                1,
                0,
                "turn_started",
                "2026-08-27T03:25:27Z",
            ),
            RodexAgentTraceEvent(
                THREAD_ID,
                parent_turn_id,
                2,
                0,
                "message",
                "2026-08-27T03:25:28Z",
                TraceMessage(
                    "00000000-0000-7000-8000-0000000000f1",
                    "unknown",
                    "user",
                    1,
                    16,
                    "rollout_reference",
                ),
            ),
            RodexAgentTraceEvent(
                THREAD_ID,
                parent_turn_id,
                3,
                0,
                "tool_call",
                "2026-08-27T03:25:30Z",
                TraceToolCall(
                    None,
                    "call-late-target",
                    "collaboration.spawn_agent",
                    "function_call",
                    100,
                    0,
                    "encrypted",
                    "request",
                ),
            ),
            RodexAgentTraceEvent(
                THREAD_ID,
                parent_turn_id,
                4,
                0,
                "subagent_activity",
                "2026-08-27T03:25:30.962Z",
                TraceSubagentActivity(
                    CHILD_THREAD_ID,
                    "started",
                    "/root/review",
                    "call-late-target",
                ),
            ),
        ),
    )

    _publish_trace(database, first_publication)

    with sqlite3.connect(database) as connection:
        request_row = connection.execute(
            "SELECT id FROM rodex_sessions_agent_requests"
        ).fetchone()
        assert request_row is not None
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM rodex_sessions_agent_request_target_turns"
            ).fetchone()[0]
            == 0
        )
    with open_rodex_transaction(database) as connection:
        wrong_identity_id = connection.execute(
            "INSERT INTO codex_threads "
            "(codex_thread_public_id_signed_bigint_1, "
            "codex_thread_public_id_signed_bigint_2) VALUES (?, ?) RETURNING id",
            split_codex_thread_id_into_signed_bigints(REPLACEMENT_THREAD_ID),
        ).fetchone()[0]
        wrong_membership_id = connection.execute(
            "INSERT INTO rodex_sessions_codex_threads "
            "(rodex_sessions_id, codex_threads_id, first_linked_at_utc) "
            "VALUES (1, ?, '2026-08-27T03:25:31Z') RETURNING id",
            (wrong_identity_id,),
        ).fetchone()[0]
        wrong_turn_row = connection.execute(
            "INSERT INTO rodex_sessions_codex_turns "
            "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
            "turn_public_id_signed_bigint_1, turn_public_id_signed_bigint_2, "
            "codex_turn_id_signed_bigint_1, codex_turn_id_signed_bigint_2) "
            "VALUES (1, ?, 0, 900, 0, 901) RETURNING id",
            (wrong_membership_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO rodex_sessions_codex_turn_states "
            "(rodex_sessions_id, rodex_sessions_codex_turns_id, "
            "started_at_utc, outcome) VALUES "
            "(1, ?, '2026-08-27T03:25:31Z', 'open')",
            (wrong_turn_row,),
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="agent request target turn is inconsistent"
        ):
            connection.execute(
                "INSERT INTO rodex_sessions_agent_request_target_turns "
                "(rodex_sessions_id, rodex_sessions_agent_requests_id, "
                "target_rodex_sessions_codex_turns_id) VALUES (1, ?, ?)",
                (request_row[0], wrong_turn_row),
            )
        child_identity_id = connection.execute(
            "SELECT id FROM codex_threads WHERE "
            "codex_thread_public_id_signed_bigint_1 = ? AND "
            "codex_thread_public_id_signed_bigint_2 = ?",
            split_codex_thread_id_into_signed_bigints(CHILD_THREAD_ID),
        ).fetchone()[0]
        child_membership_id = connection.execute(
            "INSERT INTO rodex_sessions_codex_threads "
            "(rodex_sessions_id, codex_threads_id, first_linked_at_utc) "
            "VALUES (1, ?, '2026-08-27T03:25:31Z') RETURNING id",
            (child_identity_id,),
        ).fetchone()[0]
        target_turn_rows = []
        for public_id, codex_turn_id, started_at in (
            (
                uuid.UUID(int=902),
                target_turn_id,
                "2026-08-27T03:25:31.100Z",
            ),
            (
                uuid.UUID(int=903),
                later_target_turn_id,
                "2026-08-27T03:25:31.200Z",
            ),
        ):
            turn_row = connection.execute(
                "INSERT INTO rodex_sessions_codex_turns "
                "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
                "turn_public_id_signed_bigint_1, turn_public_id_signed_bigint_2, "
                "codex_turn_id_signed_bigint_1, codex_turn_id_signed_bigint_2) "
                "VALUES (1, ?, ?, ?, ?, ?) RETURNING id",
                (
                    child_membership_id,
                    *split_codex_thread_id_into_signed_bigints(public_id),
                    *split_codex_turn_id_into_signed_bigints(codex_turn_id),
                ),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO rodex_sessions_codex_turn_states "
                "(rodex_sessions_id, rodex_sessions_codex_turns_id, "
                "started_at_utc, outcome) VALUES (1, ?, ?, 'open')",
                (turn_row, started_at),
            )
            target_turn_rows.append(turn_row)
        with pytest.raises(
            sqlite3.IntegrityError, match="agent request target turn is inconsistent"
        ):
            connection.execute(
                "INSERT INTO rodex_sessions_agent_request_target_turns "
                "(rodex_sessions_id, rodex_sessions_agent_requests_id, "
                "target_rodex_sessions_codex_turns_id) VALUES (1, ?, ?)",
                (request_row[0], target_turn_rows[1]),
            )
    second_publication = RodexAgentTracePublication(
        1,
        "test-v2",
        "2026-08-27T03:25:32Z",
        "complete",
        (
            RodexAgentTraceEvent(
                CHILD_THREAD_ID,
                target_turn_id,
                1,
                0,
                "turn_started",
                "2026-08-27T03:25:31.100Z",
            ),
        ),
    )

    _publish_trace(database, second_publication)

    request_detail = next(
        event["detail"]
        for event in read_rodex_agent_trace(1, database).events
        if event["event_kind"] == "subagent_activity"
    )
    assert request_detail["turn_request"]["target_codex_turn_id"] == target_turn_id
    assert (
        request_detail["turn_request"]["target_turn_association_kind"]
        == "next_observed_turn"
    )


def test_agent_request_parent_is_latest_user_message_before_the_tool_request(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=THREAD_ID)
    parent_turn_id = "00000000-0000-7000-8000-0000000000f0"
    publication = normalize_rollout_trace(
        (
            (
                THREAD_ID,
                _content(
                    {
                        "ordinal": 1,
                        "timestamp": "2026-08-27T04:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": parent_turn_id},
                    },
                    {
                        "ordinal": 2,
                        "timestamp": "2026-08-27T04:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "UserMessage",
                                "id": "00000000-0000-7000-8000-0000000000f1",
                                "content": [{"type": "text", "text": "actual request"}],
                            },
                        },
                    },
                    {
                        "ordinal": 3,
                        "timestamp": "2026-08-27T04:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "namespace": "collaboration",
                            "name": "spawn_agent",
                            "call_id": "call-parent-order",
                            "arguments": "encrypted",
                        },
                    },
                    {
                        "ordinal": 4,
                        "timestamp": "2026-08-27T04:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "UserMessage",
                                "id": "00000000-0000-7000-8000-0000000000f2",
                                "content": [
                                    {"type": "text", "text": "arrived after invocation"}
                                ],
                            },
                        },
                    },
                    {
                        "ordinal": 5,
                        "timestamp": "2026-08-27T04:00:04Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "SubAgentActivity",
                                "id": "call-parent-order",
                                "kind": "started",
                                "agent_thread_id": str(CHILD_THREAD_ID),
                                "agent_path": "/root/parent-order",
                            },
                        },
                    },
                ),
            ),
        ),
        based_on_trace_publication_sequence=None,
        calculated_at_utc="2026-08-27T04:00:05Z",
    )

    _publish_trace(database, publication)

    with sqlite3.connect(database) as connection:
        parent_ordinal = connection.execute(
            "SELECT parent_event.source_record_ordinal "
            "FROM rodex_sessions_agent_requests AS requests "
            "JOIN rodex_sessions_agent_trace_messages AS parent_message "
            "ON parent_message.id = "
            "requests.parent_rodex_sessions_agent_trace_messages_id "
            "JOIN rodex_sessions_agent_trace_events AS parent_event "
            "ON parent_event.id = parent_message.rodex_sessions_agent_trace_events_id"
        ).fetchone()[0]

    assert parent_ordinal == 2


def test_include_body_adapter_is_allowlisted_and_redacts_hidden_or_encrypted_data() -> None:
    unknown = _safe_event_body(
        "unrecognized_record",
        {"payload": {"type": "future_reasoning", "content": "must-not-leak"}},
    )
    reasoning = _safe_event_body(
        "message",
        {"payload": {"type": "reasoning", "content": "must-not-leak"}},
    )
    nested_reasoning = _safe_event_body(
        "message",
        {
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "content": [
                        {"type": "Text", "text": "visible"},
                        {"type": "reasoning", "text": "must-not-leak"},
                    ],
                },
            }
        },
    )
    tool = _safe_event_body(
        "tool_call",
        {
            "payload": {
                "type": "custom_tool_call",
                "tool": "example",
                "input": {"secret": "gAAAA-encrypted"},
            }
        },
    )
    embedded = _safe_event_body(
        "message",
        {
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "content": "prefix gAAAA-encrypted suffix",
                },
            }
        },
    )

    assert unknown == {"capture_state": "redacted", "reason": "metadata_only_event"}
    assert reasoning == {"capture_state": "redacted", "reason": "hidden_reasoning"}
    assert nested_reasoning["value"]["content"][1] == {
        "capture_state": "redacted",
        "reason": "hidden_reasoning",
    }
    assert "must-not-leak" not in json.dumps(nested_reasoning)
    assert tool["value"]["input"]["secret"] == "<encrypted>"
    assert embedded["value"]["content"] == "<encrypted>"


def test_include_bodies_resolves_an_authenticated_historical_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=THREAD_ID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
    )
    rollout = tmp_path / "historical-root.jsonl"
    content = _content(
        {
            "ordinal": 1,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "phase": "final_answer",
                    "content": "historic answer",
                },
            },
        }
    )
    rollout.write_bytes(content)
    with open_rodex_transaction(database) as connection:
        worker_id = connection.execute(
            "INSERT INTO rodex_sessions_analytics_workers "
            "(rodex_sessions_id, worker_state, last_attempted_at_utc, "
            "consecutive_failures) VALUES (1, 'up_to_date', ?, 0) RETURNING id",
            ("2026-08-26T12:00:00Z",),
        ).fetchone()[0]
        source_id = connection.execute(
            "INSERT INTO rodex_sessions_codex_rollout_sources "
            "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
            "rollout_file_path, first_observed_at_utc) VALUES (1, 1, ?, ?) "
            "RETURNING id",
            (str(rollout), "2026-08-26T12:00:00Z"),
        ).fetchone()[0]
        stat = rollout.stat()
        connection.execute(
            "INSERT INTO rodex_sessions_analytics_worker_thread_checkpoints "
            "(rodex_sessions_id, rodex_sessions_analytics_workers_id, "
            "rodex_sessions_codex_rollout_sources_id, analyzed_size_bytes, "
            "analyzed_mtime_ns, analyzed_prefix_sha256, verified_at_utc) "
            "VALUES (1, ?, ?, ?, ?, ?, ?)",
            (
                worker_id,
                source_id,
                len(content),
                stat.st_mtime_ns,
                hashlib.sha256(content).hexdigest(),
                "2026-08-26T12:00:00Z",
            ),
        )
    _publish_trace(
        database,
        RodexAgentTracePublication(
            None,
            "test-v1",
            "2026-08-26T12:00:00Z",
            "complete",
            (
                RodexAgentTraceEvent(
                    THREAD_ID,
                    None,
                    1,
                    0,
                    "message",
                    None,
                    TraceMessage(
                        None,
                        "final_answer",
                        "assistant",
                        1,
                        len("historic answer"),
                        "rollout_reference",
                    ),
                ),
            ),
        ),
    )
    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        database,
        codex_session_id=REPLACEMENT_THREAD_ID,
        runtime_id=RodexRuntimeId.generate(),
    )

    execute_agent_trace_command(
        ["_trace", session.cool_name, "--include-bodies", "--json"], database
    )

    trace = json.loads(capsys.readouterr().out)
    assert trace["events"][0]["body"]["value"]["content"] == "historic answer"


def test_agent_trace_commands_render_lineage_and_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=THREAD_ID)

    execute_agent_trace_command(["_agents", session.cool_name, "--json"], database)
    agents = json.loads(capsys.readouterr().out)
    assert agents["agent_count"] == 1
    assert agents["agents"][0]["codex_thread_id"] == str(THREAD_ID)

    with open_rodex_transaction(database) as connection:
        identity_id = connection.execute(
            "INSERT INTO codex_threads "
            "(codex_thread_public_id_signed_bigint_1, "
            "codex_thread_public_id_signed_bigint_2) VALUES (?, ?) RETURNING id",
            split_codex_thread_id_into_signed_bigints(CHILD_THREAD_ID),
        ).fetchone()[0]
        child_membership_id = connection.execute(
            "INSERT INTO rodex_sessions_codex_threads "
            "(rodex_sessions_id, codex_threads_id, first_linked_at_utc) "
            "VALUES (?, ?, ?) RETURNING id",
            (1, identity_id, "2026-08-26T12:00:00Z"),
        ).fetchone()[0]
        spawning_turn_id = connection.execute(
            "INSERT INTO rodex_sessions_codex_turns "
            "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
            "turn_public_id_signed_bigint_1, turn_public_id_signed_bigint_2, "
            "codex_turn_id_signed_bigint_1, codex_turn_id_signed_bigint_2) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (
                1,
                1,
                101,
                102,
                *split_codex_thread_id_into_signed_bigints(
                    uuid.UUID("00000000-0000-7000-8000-00000000000b")
                ),
            ),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO rodex_sessions_codex_turn_states "
            "(rodex_sessions_id, rodex_sessions_codex_turns_id, outcome) "
            "VALUES (1, ?, 'open')",
            (spawning_turn_id,),
        )
        connection.execute(
            "INSERT INTO rodex_sessions_subagent_spawns "
            "(rodex_sessions_id, subagent_rodex_sessions_codex_threads_id, "
            "parent_rodex_sessions_codex_threads_id, "
            "spawning_rodex_sessions_codex_turns_id, agent_path, "
            "history_inheritance_kind) VALUES (1, ?, 1, ?, '/root/review', 'clean')",
            (child_membership_id, spawning_turn_id),
        )
    execute_agent_trace_command(["_agents", session.cool_name, "--json"], database)
    agents = json.loads(capsys.readouterr().out)
    assert agents["agents"][1]["parent_codex_thread_id"] == str(THREAD_ID)

    execute_agent_trace_command(["_trace", session.cool_name, "--json"], database)
    trace = json.loads(capsys.readouterr().out)
    assert trace["trace_publication_sequence"] is None
    assert trace["events"] == []


def test_trace_cursor_requires_canonical_public_uuid_text(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=THREAD_ID)
    _publish_trace(
        database,
        RodexAgentTracePublication(
            None,
            "test-v1",
            "2026-08-26T12:00:00Z",
            "complete",
            (
                RodexAgentTraceEvent(
                    THREAD_ID,
                    None,
                    1,
                    0,
                    "message",
                    None,
                    TraceMessage(
                        None,
                        "commentary",
                        "assistant",
                        1,
                        4,
                        "rollout_reference",
                    ),
                ),
            ),
        ),
    )
    event_id = str(read_rodex_agent_trace(1, database).events[0]["event_id"])

    for noncanonical in (event_id.upper(), event_id.replace("-", "")):
        with pytest.raises(ValueError, match="canonical lowercase UUID text"):
            read_rodex_agent_trace(1, database, after_event_id=noncanonical)
        with pytest.raises(RodexLaunchError, match="usage: rodex _trace"):
            execute_agent_trace_command(
                ["_trace", session.cool_name, "--after", noncanonical], database
            )
