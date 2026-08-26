"""Typed, rollout-addressable agent trace persistence and read contracts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from rodex_sql import index_re_try_attempt_numbers, open_rodex_read_transaction

from .errors import RodexSessionStatisticsConflictError
from .execution import resolve_codex_thread_identity_in_transaction
from .identity import (
    CodexThreadId,
    join_signed_bigints_into_a_codex_item_id,
    join_signed_bigints_into_a_codex_thread_id,
    join_signed_bigints_into_a_codex_turn_id,
    parse_codex_thread_id,
    parse_codex_turn_id,
    split_codex_item_id_into_signed_bigints,
    split_codex_thread_id_into_signed_bigints,
    split_codex_turn_id_into_signed_bigints,
)
from .schema import (
    CODEX_THREADS_TABLE,
    MODEL_NAMES_TABLE,
    REASONING_EFFORT_NAMES_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_RATE_LIMIT_WINDOWS_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_TOKEN_USAGE_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
    RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE,
    RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE,
    RODEX_SESSIONS_CODEX_ITEMS_TABLE,
    RODEX_SESSIONS_CODEX_THREADS_TABLE,
    RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE,
    RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
    RODEX_SESSIONS_CODEX_TURN_STATES_TABLE,
    RODEX_SESSIONS_CODEX_TURNS_TABLE,
    TOOL_NAMES_TABLE,
    existing_rodex_database_path,
)
from .validation import (
    _normalise_required_text,
    _validate_session_id,
)

TRACE_EVENT_KINDS = frozenset(
    {
        "session_metadata",
        "turn_context",
        "turn_started",
        "turn_completed",
        "turn_aborted",
        "message",
        "tool_call",
        "command_execution",
        "subagent_activity",
        "token_usage",
        "rate_limit",
        "compaction",
        "unrecognized_record",
    }
)


@dataclass(frozen=True, slots=True)
class TraceMessage:
    item_id: str | None
    message_phase: str
    message_role: str
    content_block_count: int
    body_utf8_bytes: int
    body_capture_state: str


@dataclass(frozen=True, slots=True)
class TraceToolCall:
    item_id: str | None
    call_id: str | None
    tool_name: str
    tool_status: str | None
    request_utf8_bytes: int
    response_utf8_bytes: int
    payload_capture_state: str
    activity_kind: str


@dataclass(frozen=True, slots=True)
class TraceCommandExecution:
    item_id: str | None
    command_argument_count: int
    working_directory: str | None
    command_status: str | None
    duration_ms: int | None
    exit_code: int | None
    stdout_utf8_bytes: int
    stderr_utf8_bytes: int
    aggregated_output_utf8_bytes: int
    payload_capture_state: str


@dataclass(frozen=True, slots=True)
class TraceContext:
    model: str | None
    reasoning_effort: str | None
    working_directory: str | None
    sandbox_mode: str | None
    approval_policy: str | None
    permission_profile_type: str | None
    workspace_root_count: int


@dataclass(frozen=True, slots=True)
class TraceTokenUsage:
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None
    context_used_percent: float | None


@dataclass(frozen=True, slots=True)
class TraceRateLimitWindow:
    limit_id: str
    used_percent: float | None
    window_minutes: int | None
    resets_at_unix_seconds: int | None
    plan_type: str | None


@dataclass(frozen=True, slots=True)
class TraceRateLimits:
    windows: tuple[TraceRateLimitWindow, ...]


@dataclass(frozen=True, slots=True)
class TraceSubagentActivity:
    target_codex_thread_id: CodexThreadId | None
    activity_kind: str
    agent_path: str | None


type TraceDetail = (
    TraceMessage
    | TraceToolCall
    | TraceCommandExecution
    | TraceContext
    | TraceTokenUsage
    | TraceRateLimits
    | TraceSubagentActivity
    | None
)

_TRACE_DETAIL_TYPES: dict[str, type[object] | None] = {
    "session_metadata": None,
    "turn_context": TraceContext,
    "turn_started": None,
    "turn_completed": None,
    "turn_aborted": None,
    "message": TraceMessage,
    "tool_call": TraceToolCall,
    "command_execution": TraceCommandExecution,
    "subagent_activity": TraceSubagentActivity,
    "token_usage": TraceTokenUsage,
    "rate_limit": TraceRateLimits,
    "compaction": None,
    "unrecognized_record": None,
}


@dataclass(frozen=True, slots=True)
class RodexAgentTraceEvent:
    codex_thread_id: CodexThreadId
    codex_turn_id: str | None
    source_record_ordinal: int
    derived_event_ordinal: int
    event_kind: str
    event_time_utc: str | None
    detail: TraceDetail = None


@dataclass(frozen=True, slots=True)
class RodexAgentTracePublication:
    based_on_trace_publication_sequence: int | None
    trace_schema_version: str
    calculated_at_utc: str
    coverage_state: str
    events: tuple[RodexAgentTraceEvent, ...]


@dataclass(frozen=True, slots=True)
class RodexAgentTracePublishReceipt:
    trace_publication_sequence: int
    durable_event_count: int
    unrecognized_record_count: int


@dataclass(frozen=True, slots=True)
class RodexAgentTraceSnapshot:
    trace_publication_sequence: int | None
    trace_schema_version: str | None
    calculated_at_utc: str | None
    coverage_state: str | None
    durable_event_count: int
    unrecognized_record_count: int
    events: tuple[dict[str, Any], ...]


def publish_agent_trace_in_transaction(
    connection: sqlite3.Connection,
    session_id: int,
    publication: RodexAgentTracePublication,
    *,
    model_name_ids: dict[str, int],
    reasoning_effort_name_ids: dict[str, int],
) -> RodexAgentTracePublishReceipt:
    """Append a deduplicated trace batch and advance its independent CAS head."""
    _validate_session_id(session_id)
    current = connection.execute(
        f"SELECT trace_publication_sequence, durable_event_count, "
        "unrecognized_record_count, coverage_state FROM "
        f"{RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE} "
        "WHERE rodex_sessions_id = ?",
        (session_id,),
    ).fetchone()
    current_sequence = None if current is None else int(current[0])
    prior_event_count = 0 if current is None else int(current[1])
    prior_unrecognized_count = 0 if current is None else int(current[2])
    prior_coverage = None if current is None else str(current[3])
    if current_sequence != publication.based_on_trace_publication_sequence:
        raise RodexSessionStatisticsConflictError(
            "agent trace publication sequence changed during calculation"
        )
    sequence = 1 if current_sequence is None else current_sequence + 1
    schema_version = _normalise_required_text(
        publication.trace_schema_version, "trace_schema_version"
    )
    coverage = _normalise_required_text(publication.coverage_state, "coverage_state")
    if coverage not in {"complete", "gapped"}:
        raise ValueError(f"unsupported agent trace coverage state: {coverage}")
    seen_keys: set[tuple[CodexThreadId, int, int]] = set()
    tool_name_ids: dict[str, int] = {}
    activity_scope_ids: dict[tuple[int, int | None], int] = {}
    inserted_event_count = 0
    inserted_unrecognized_count = 0
    for event in publication.events:
        parsed_thread_id = parse_codex_thread_id(event.codex_thread_id)
        key = (
            parsed_thread_id,
            _nonnegative_integer(event.source_record_ordinal, "source_record_ordinal"),
            _nonnegative_integer(event.derived_event_ordinal, "derived_event_ordinal"),
        )
        if key in seen_keys:
            raise ValueError("agent trace batch contains a duplicate source event key")
        seen_keys.add(key)
        kind = _normalise_required_text(event.event_kind, "event_kind")
        if kind not in TRACE_EVENT_KINDS:
            raise ValueError(f"unsupported agent trace event kind: {kind}")
        detail_sha256 = _trace_detail_sha256(kind, event.detail)
        thread_row = connection.execute(
            f"SELECT memberships.id FROM {RODEX_SESSIONS_CODEX_THREADS_TABLE} "
            "AS memberships "
            f"JOIN {CODEX_THREADS_TABLE} AS identities "
            "ON identities.id = memberships.codex_threads_id "
            "WHERE memberships.rodex_sessions_id = ? "
            "AND identities.codex_thread_public_id_signed_bigint_1 = ? "
            "AND identities.codex_thread_public_id_signed_bigint_2 = ?",
            (session_id, *split_codex_thread_id_into_signed_bigints(parsed_thread_id)),
        ).fetchone()
        if thread_row is None:
            raise RodexSessionStatisticsConflictError(
                "agent trace event identifies an unregistered Codex thread"
            )
        thread_row_id = int(thread_row[0])
        turn_row_id = _resolve_optional_trace_turn_id(
            connection,
            session_id,
            thread_row_id,
            event.codex_turn_id,
            event_kind=kind,
            event_time_utc=event.event_time_utc,
        )
        scope_id = _resolve_or_insert_activity_scope(
            connection,
            session_id=session_id,
            thread_row_id=thread_row_id,
            turn_row_id=turn_row_id,
            cache=activity_scope_ids,
        )
        inserted: tuple[object, ...] | None = None
        stored: tuple[object, ...] | None = None
        trace_event_public_id: uuid.UUID | None = None
        for _attempt_number in index_re_try_attempt_numbers():
            trace_event_public_id = uuid.uuid4()
            public_halves = split_codex_thread_id_into_signed_bigints(trace_event_public_id)
            inserted = connection.execute(
                f"INSERT OR IGNORE INTO {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} "
                "(trace_event_public_id_signed_bigint_1, "
                "trace_event_public_id_signed_bigint_2, rodex_sessions_id, "
                "rodex_sessions_codex_threads_id, "
                "rodex_sessions_codex_activity_scopes_id, "
                "source_record_ordinal, derived_event_ordinal, "
                "first_trace_publication_sequence, event_kind, event_time_utc, "
                "detail_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "RETURNING id",
                (
                    *public_halves,
                    session_id,
                    thread_row_id,
                    scope_id,
                    key[1],
                    key[2],
                    sequence,
                    kind,
                    event.event_time_utc,
                    detail_sha256,
                ),
            ).fetchone()
            if inserted is not None:
                break
            stored = connection.execute(
                f"SELECT id, rodex_sessions_codex_activity_scopes_id, "
                "event_kind, event_time_utc, "
                "detail_sha256 "
                f"FROM {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} "
                "WHERE rodex_sessions_codex_threads_id = ? "
                "AND source_record_ordinal = ? AND derived_event_ordinal = ?",
                (thread_row_id, key[1], key[2]),
            ).fetchone()
            if stored is not None:
                break
        if inserted is None:
            expected = (scope_id, kind, event.event_time_utc, detail_sha256)
            if stored is None or tuple(stored[1:]) != expected:
                raise RodexSessionStatisticsConflictError(
                    "authenticated rollout event changed after trace publication"
                )
            continue
        assert trace_event_public_id is not None
        inserted_event_count += 1
        inserted_unrecognized_count += kind == "unrecognized_record"
        _insert_trace_detail(
            connection,
            session_id,
            int(inserted[0]),
            thread_row_id,
            scope_id,
            event.detail,
            model_name_ids=model_name_ids,
            reasoning_effort_name_ids=reasoning_effort_name_ids,
            tool_name_ids=tool_name_ids,
        )
    durable_event_count = prior_event_count + inserted_event_count
    unrecognized_count = prior_unrecognized_count + inserted_unrecognized_count
    if prior_coverage == "gapped" or unrecognized_count:
        coverage = "gapped"
    values = (
        sequence,
        schema_version,
        publication.calculated_at_utc,
        coverage,
        int(durable_event_count),
        int(unrecognized_count),
    )
    if current_sequence is None:
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE} "
            "(rodex_sessions_id, trace_publication_sequence, trace_schema_version, "
            "calculated_at_utc, coverage_state, durable_event_count, "
            "unrecognized_record_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, *values),
        )
    else:
        connection.execute(
            f"UPDATE {RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE} SET "
            "trace_publication_sequence = ?, trace_schema_version = ?, "
            "calculated_at_utc = ?, coverage_state = ?, durable_event_count = ?, "
            "unrecognized_record_count = ? WHERE rodex_sessions_id = ?",
            (*values, session_id),
        )
    return RodexAgentTracePublishReceipt(
        sequence, int(durable_event_count), int(unrecognized_count)
    )


def read_rodex_agent_trace(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    after_event_id: uuid.UUID | str | None = None,
    limit: int = 200,
) -> RodexAgentTraceSnapshot:
    """Read one transactionally consistent trace head and stable event page."""
    _validate_session_id(session_id)
    if after_event_id is None:
        after_public_id = None
    elif isinstance(after_event_id, uuid.UUID):
        after_public_id = after_event_id
    elif isinstance(after_event_id, str):
        after_public_id = uuid.UUID(after_event_id)
        if str(after_public_id) != after_event_id:
            raise ValueError("after_event_id must use canonical lowercase UUID text")
    else:
        raise TypeError("after_event_id must be a 128-bit ID or string")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
        raise ValueError("limit must be an integer between 1 and 10000")
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        publication = connection.execute(
            f"SELECT trace_publication_sequence, trace_schema_version, "
            "calculated_at_utc, coverage_state, durable_event_count, "
            "unrecognized_record_count FROM "
            f"{RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
        after_internal_id = 0
        if after_public_id is not None:
            after_row = connection.execute(
                f"SELECT id FROM {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} "
                "WHERE rodex_sessions_id = ? "
                "AND trace_event_public_id_signed_bigint_1 = ? "
                "AND trace_event_public_id_signed_bigint_2 = ?",
                (
                    session_id,
                    *split_codex_thread_id_into_signed_bigints(after_public_id),
                ),
            ).fetchone()
            if after_row is None:
                raise ValueError("after_event_id is not an event in this Rodex session")
            after_internal_id = int(after_row[0])
        rows = connection.execute(
            _TRACE_EVENT_SELECT + " WHERE events.rodex_sessions_id = ? AND events.id > ? "
            "ORDER BY events.id LIMIT ?",
            (session_id, after_internal_id, limit),
        ).fetchall()
        event_ids = tuple(int(row[0]) for row in rows)
        rate_rows = (
            []
            if not event_ids
            else connection.execute(
                f"SELECT rodex_sessions_agent_trace_events_id, window_ordinal, limit_id, "
                "used_percent, window_minutes, resets_at_unix_seconds, plan_type "
                f"FROM {RODEX_SESSIONS_AGENT_TRACE_RATE_LIMIT_WINDOWS_TABLE} WHERE "
                "rodex_sessions_agent_trace_events_id IN "
                f"({','.join('?' for _ in event_ids)}) "
                "ORDER BY rodex_sessions_agent_trace_events_id, window_ordinal",
                event_ids,
            ).fetchall()
        )
    rates: dict[int, list[dict[str, Any]]] = {}
    for row in rate_rows:
        rates.setdefault(int(row[0]), []).append(
            {
                "window_ordinal": int(row[1]),
                "limit_id": str(row[2]),
                "used_percent": None if row[3] is None else float(row[3]),
                "window_minutes": None if row[4] is None else int(row[4]),
                "resets_at_unix_seconds": None if row[5] is None else int(row[5]),
                "plan_type": None if row[6] is None else str(row[6]),
            }
        )
    events = tuple(
        _trace_event_row_as_dict(row, rates.get(int(row[0]), [])) for row in rows
    )
    return RodexAgentTraceSnapshot(
        trace_publication_sequence=None if publication is None else int(publication[0]),
        trace_schema_version=None if publication is None else str(publication[1]),
        calculated_at_utc=None if publication is None else str(publication[2]),
        coverage_state=None if publication is None else str(publication[3]),
        durable_event_count=0 if publication is None else int(publication[4]),
        unrecognized_record_count=0 if publication is None else int(publication[5]),
        events=events,
    )


def _resolve_optional_trace_turn_id(
    connection: sqlite3.Connection,
    session_id: int,
    thread_row_id: int,
    codex_turn_id: str | None,
    *,
    event_kind: str,
    event_time_utc: str | None,
) -> int | None:
    if codex_turn_id is None:
        return None
    turn_id = str(parse_codex_turn_id(codex_turn_id))
    turn_identity = split_codex_turn_id_into_signed_bigints(turn_id)
    row = connection.execute(
        f"SELECT id FROM {RODEX_SESSIONS_CODEX_TURNS_TABLE} "
        "WHERE rodex_sessions_codex_threads_id = ? "
        "AND codex_turn_id_signed_bigint_1 = ? "
        "AND codex_turn_id_signed_bigint_2 = ?",
        (thread_row_id, *turn_identity),
    ).fetchone()
    if row is None:
        for _attempt_number in index_re_try_attempt_numbers():
            public_id = uuid.uuid4()
            inserted = connection.execute(
                f"INSERT OR IGNORE INTO {RODEX_SESSIONS_CODEX_TURNS_TABLE} "
                "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
                "turn_public_id_signed_bigint_1, turn_public_id_signed_bigint_2, "
                "codex_turn_id_signed_bigint_1, codex_turn_id_signed_bigint_2) "
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    session_id,
                    thread_row_id,
                    *split_codex_thread_id_into_signed_bigints(public_id),
                    *turn_identity,
                ),
            ).fetchone()
            if inserted is not None:
                row = (inserted[0],)
                break
            row = connection.execute(
                f"SELECT id FROM {RODEX_SESSIONS_CODEX_TURNS_TABLE} "
                "WHERE rodex_sessions_codex_threads_id = ? "
                "AND codex_turn_id_signed_bigint_1 = ? "
                "AND codex_turn_id_signed_bigint_2 = ?",
                (thread_row_id, *turn_identity),
            ).fetchone()
            if row is not None:
                break
    if row is None:
        raise RodexSessionStatisticsConflictError("trace turn identity was not registered")
    turn_row_id = int(row[0])
    outcome = (
        "completed"
        if event_kind == "turn_completed"
        else "aborted"
        if event_kind == "turn_aborted"
        else "open"
    )
    connection.execute(
        f"INSERT OR IGNORE INTO {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} "
        "(rodex_sessions_id, rodex_sessions_codex_turns_id, started_at_utc, "
        "terminal_at_utc, outcome) VALUES (?, ?, ?, ?, ?)",
        (
            session_id,
            turn_row_id,
            event_time_utc if event_kind == "turn_started" else None,
            event_time_utc if outcome != "open" else None,
            outcome,
        ),
    )
    if event_kind == "turn_started":
        connection.execute(
            f"UPDATE {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} "
            "SET started_at_utc = COALESCE(started_at_utc, ?) "
            "WHERE rodex_sessions_codex_turns_id = ?",
            (event_time_utc, turn_row_id),
        )
    elif event_kind in {"turn_completed", "turn_aborted"}:
        connection.execute(
            f"UPDATE {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} SET "
            "terminal_at_utc = COALESCE(terminal_at_utc, ?), outcome = ? "
            "WHERE rodex_sessions_codex_turns_id = ?",
            (
                event_time_utc,
                "completed" if event_kind == "turn_completed" else "aborted",
                turn_row_id,
            ),
        )
    return turn_row_id


def _resolve_or_insert_activity_scope(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    thread_row_id: int,
    turn_row_id: int | None,
    cache: dict[tuple[int, int | None], int],
) -> int:
    key = (thread_row_id, turn_row_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    if turn_row_id is None:
        condition = (
            "rodex_sessions_codex_threads_id = ? AND rodex_sessions_codex_turns_id IS NULL"
        )
        parameters: tuple[object, ...] = (thread_row_id,)
    else:
        condition = "rodex_sessions_codex_turns_id = ?"
        parameters = (turn_row_id,)
    row = connection.execute(
        f"SELECT id FROM {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE} WHERE {condition}",
        parameters,
    ).fetchone()
    if row is None:
        row = connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE} "
            "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
            "rodex_sessions_codex_turns_id) VALUES (?, ?, ?) RETURNING id",
            (session_id, thread_row_id, turn_row_id),
        ).fetchone()
    if row is None:
        raise RodexSessionStatisticsConflictError(
            "Codex activity scope insertion returned no identity"
        )
    scope_id = int(row[0])
    cache[key] = scope_id
    return scope_id


def _insert_trace_detail(
    connection: sqlite3.Connection,
    session_id: int,
    event_id: int,
    thread_row_id: int,
    scope_id: int,
    detail: TraceDetail,
    *,
    model_name_ids: dict[str, int],
    reasoning_effort_name_ids: dict[str, int],
    tool_name_ids: dict[str, int],
) -> None:
    if detail is None:
        return
    item_id = (
        _resolve_or_insert_codex_item(
            connection,
            session_id=session_id,
            thread_row_id=thread_row_id,
            scope_id=scope_id,
            codex_item_id=detail.item_id,
        )
        if isinstance(detail, (TraceMessage, TraceToolCall, TraceCommandExecution))
        else None
    )
    if isinstance(detail, TraceMessage):
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE} "
            "(rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, "
            "rodex_sessions_agent_trace_events_id, "
            "rodex_sessions_codex_items_id, message_phase, "
            "message_role, content_block_count, body_utf8_bytes, body_capture_state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                scope_id,
                event_id,
                item_id,
                detail.message_phase,
                detail.message_role,
                detail.content_block_count,
                detail.body_utf8_bytes,
                detail.body_capture_state,
            ),
        )
    elif isinstance(detail, TraceToolCall):
        tool_call_id = _resolve_or_insert_tool_call(
            connection,
            session_id=session_id,
            thread_row_id=thread_row_id,
            scope_id=scope_id,
            event_id=event_id,
            item_id=item_id,
            detail=detail,
            tool_name_ids=tool_name_ids,
        )
        activity_kind = _normalise_required_text(detail.activity_kind, "activity_kind")
        if activity_kind not in {"request", "output", "status"}:
            raise ValueError(f"unsupported tool-call activity kind: {activity_kind}")
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE} "
            "(rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, "
            "rodex_sessions_agent_trace_events_id, "
            "rodex_sessions_codex_tool_calls_id, activity_kind, "
            "rodex_sessions_codex_items_id, "
            "tool_status, request_utf8_bytes, response_utf8_bytes, "
            "payload_capture_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                scope_id,
                event_id,
                tool_call_id,
                activity_kind,
                item_id,
                detail.tool_status,
                detail.request_utf8_bytes,
                detail.response_utf8_bytes,
                detail.payload_capture_state,
            ),
        )
    elif isinstance(detail, TraceCommandExecution):
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE} "
            "(rodex_sessions_id, rodex_sessions_codex_activity_scopes_id, "
            "rodex_sessions_agent_trace_events_id, "
            "rodex_sessions_codex_items_id, command_argument_count, "
            "working_directory, command_status, duration_ms, exit_code, "
            "stdout_utf8_bytes, stderr_utf8_bytes, aggregated_output_utf8_bytes, "
            "payload_capture_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                scope_id,
                event_id,
                item_id,
                detail.command_argument_count,
                detail.working_directory,
                detail.command_status,
                detail.duration_ms,
                detail.exit_code,
                detail.stdout_utf8_bytes,
                detail.stderr_utf8_bytes,
                detail.aggregated_output_utf8_bytes,
                detail.payload_capture_state,
            ),
        )
    elif isinstance(detail, TraceContext):
        model_id = _lookup_name(
            connection, MODEL_NAMES_TABLE, "name_of_the_model", detail.model, model_name_ids
        )
        effort_id = _lookup_name(
            connection,
            REASONING_EFFORT_NAMES_TABLE,
            "name_of_the_reasoning_effort",
            detail.reasoning_effort,
            reasoning_effort_name_ids,
        )
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_TABLE} "
            "(rodex_sessions_agent_trace_events_id, model_names_id, "
            "reasoning_effort_names_id, working_directory, sandbox_mode, "
            "approval_policy, permission_profile_type, workspace_root_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                model_id,
                effort_id,
                detail.working_directory,
                detail.sandbox_mode,
                detail.approval_policy,
                detail.permission_profile_type,
                detail.workspace_root_count,
            ),
        )
    elif isinstance(detail, TraceTokenUsage):
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_AGENT_TRACE_TOKEN_USAGE_TABLE} "
            "(rodex_sessions_agent_trace_events_id, input_tokens, "
            "cached_input_tokens, output_tokens, reasoning_output_tokens, "
            "total_tokens, context_used_percent) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                detail.input_tokens,
                detail.cached_input_tokens,
                detail.output_tokens,
                detail.reasoning_output_tokens,
                detail.total_tokens,
                detail.context_used_percent,
            ),
        )
    elif isinstance(detail, TraceRateLimits):
        connection.executemany(
            f"INSERT INTO {RODEX_SESSIONS_AGENT_TRACE_RATE_LIMIT_WINDOWS_TABLE} "
            "(rodex_sessions_agent_trace_events_id, window_ordinal, limit_id, "
            "used_percent, window_minutes, resets_at_unix_seconds, plan_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    event_id,
                    ordinal,
                    window.limit_id,
                    window.used_percent,
                    window.window_minutes,
                    window.resets_at_unix_seconds,
                    window.plan_type,
                )
                for ordinal, window in enumerate(detail.windows)
            ),
        )
    elif isinstance(detail, TraceSubagentActivity):
        target_codex_threads_id = (
            None
            if detail.target_codex_thread_id is None
            else resolve_codex_thread_identity_in_transaction(
                connection, detail.target_codex_thread_id
            )
        )
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE} "
            "(rodex_sessions_agent_trace_events_id, rodex_sessions_id, "
            "target_codex_threads_id, activity_kind, agent_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                event_id,
                session_id,
                target_codex_threads_id,
                detail.activity_kind,
                detail.agent_path,
            ),
        )
    else:
        raise TypeError(f"unsupported agent trace detail: {type(detail).__name__}")


def _resolve_or_insert_codex_item(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    thread_row_id: int,
    scope_id: int,
    codex_item_id: str | None,
) -> int | None:
    if codex_item_id is None:
        return None
    source_item_id = _normalise_required_text(codex_item_id, "codex_item_id")
    try:
        parsed_item_id = uuid.UUID(source_item_id)
    except ValueError:
        item_identity = None
        alias_digest = _sha256_signed_bigints(source_item_id)
    else:
        if str(parsed_item_id) != source_item_id:
            raise ValueError("Codex item UUID must use canonical lowercase UUID text")
        item_identity = split_codex_item_id_into_signed_bigints(parsed_item_id)
        alias_digest = None
    for _attempt_number in index_re_try_attempt_numbers():
        if item_identity is None:
            assert alias_digest is not None
            stored = connection.execute(
                "SELECT items.id, items.rodex_sessions_codex_activity_scopes_id, "
                "aliases.codex_item_alias "
                f"FROM {RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE} AS aliases "
                f"JOIN {RODEX_SESSIONS_CODEX_ITEMS_TABLE} AS items "
                "ON items.id = aliases.rodex_sessions_codex_items_id "
                "WHERE aliases.rodex_sessions_codex_threads_id = ? "
                "AND aliases.codex_item_alias_sha256_int_1 = ? "
                "AND aliases.codex_item_alias_sha256_int_2 = ? "
                "AND aliases.codex_item_alias_sha256_int_3 = ? "
                "AND aliases.codex_item_alias_sha256_int_4 = ?",
                (thread_row_id, *alias_digest),
            ).fetchone()
        else:
            stored = connection.execute(
                "SELECT id, rodex_sessions_codex_activity_scopes_id, NULL "
                f"FROM {RODEX_SESSIONS_CODEX_ITEMS_TABLE} "
                "WHERE rodex_sessions_codex_threads_id = ? "
                "AND codex_item_id_signed_bigint_1 = ? "
                "AND codex_item_id_signed_bigint_2 = ?",
                (thread_row_id, *item_identity),
            ).fetchone()
        if stored is not None:
            if stored[2] is not None and str(stored[2]) != source_item_id:
                raise RodexSessionStatisticsConflictError(
                    "Codex item alias digest collision"
                )
            if stored[1] != scope_id:
                raise RodexSessionStatisticsConflictError(
                    "canonical Codex item identity changed across trace events"
                )
            return int(stored[0])
        public_id = uuid.uuid4()
        identity_columns = (
            ""
            if item_identity is None
            else ", codex_item_id_signed_bigint_1, codex_item_id_signed_bigint_2"
        )
        identity_placeholders = "" if item_identity is None else ", ?, ?"
        inserted = connection.execute(
            f"INSERT OR IGNORE INTO {RODEX_SESSIONS_CODEX_ITEMS_TABLE} "
            "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
            "rodex_sessions_codex_activity_scopes_id, "
            "item_public_id_signed_bigint_1, "
            f"item_public_id_signed_bigint_2{identity_columns}) "
            f"VALUES (?, ?, ?, ?, ?{identity_placeholders}) RETURNING id",
            (
                session_id,
                thread_row_id,
                scope_id,
                *split_codex_thread_id_into_signed_bigints(public_id),
                *(() if item_identity is None else item_identity),
            ),
        ).fetchone()
        if inserted is not None:
            item_row_id = int(inserted[0])
            if alias_digest is not None:
                connection.execute(
                    f"INSERT INTO {RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE} "
                    "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
                    "rodex_sessions_codex_activity_scopes_id, "
                    "rodex_sessions_codex_items_id, "
                    "codex_item_alias_sha256_int_1, "
                    "codex_item_alias_sha256_int_2, "
                    "codex_item_alias_sha256_int_3, "
                    "codex_item_alias_sha256_int_4, codex_item_alias) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        thread_row_id,
                        scope_id,
                        item_row_id,
                        *alias_digest,
                        source_item_id,
                    ),
                )
            return item_row_id
    raise RodexSessionStatisticsConflictError("could not allocate a Codex item public ID")


def _resolve_or_insert_tool_call(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    thread_row_id: int,
    scope_id: int,
    event_id: int,
    item_id: int | None,
    detail: TraceToolCall,
    tool_name_ids: dict[str, int],
) -> int:
    if detail.activity_kind not in {"request", "output", "status"}:
        raise ValueError(f"unsupported tool-call activity kind: {detail.activity_kind}")
    matched_call_ids: set[int] = set()
    call_id = None
    call_id_digest = None
    if detail.call_id is not None:
        call_id = _normalise_required_text(detail.call_id, "call_id")
        call_id_digest = _sha256_signed_bigints(call_id)
        alias = connection.execute(
            "SELECT rodex_sessions_codex_tool_calls_id, codex_call_id "
            f"FROM {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE} "
            "WHERE rodex_sessions_codex_threads_id = ? AND alias_kind = 'call_id' "
            "AND codex_call_id_sha256_int_1 = ? "
            "AND codex_call_id_sha256_int_2 = ? "
            "AND codex_call_id_sha256_int_3 = ? "
            "AND codex_call_id_sha256_int_4 = ?",
            (thread_row_id, *call_id_digest),
        ).fetchone()
        if alias is not None:
            if str(alias[1]) != call_id:
                raise RodexSessionStatisticsConflictError(
                    "Codex tool-call ID digest collision"
                )
            matched_call_ids.add(int(alias[0]))
    if item_id is not None:
        alias = connection.execute(
            "SELECT rodex_sessions_codex_tool_calls_id "
            f"FROM {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE} "
            "WHERE alias_kind = 'item_id' AND rodex_sessions_codex_items_id = ?",
            (item_id,),
        ).fetchone()
        if alias is not None:
            matched_call_ids.add(int(alias[0]))
    if len(matched_call_ids) > 1:
        raise RodexSessionStatisticsConflictError(
            "tool-call aliases resolve to different canonical calls"
        )
    tool_names_id = (
        _lookup_name(
            connection,
            TOOL_NAMES_TABLE,
            "tool_name",
            detail.tool_name,
            tool_name_ids,
        )
        if detail.activity_kind == "request"
        else None
    )
    if matched_call_ids:
        tool_call_id = matched_call_ids.pop()
        stored = connection.execute(
            "SELECT rodex_sessions_codex_activity_scopes_id, tool_names_id "
            f"FROM {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE} WHERE id = ?",
            (tool_call_id,),
        ).fetchone()
        if stored is None or stored[0] != scope_id:
            raise RodexSessionStatisticsConflictError(
                "canonical tool-call identity changed across activity events"
            )
        if tool_names_id is not None:
            if stored[1] is None:
                connection.execute(
                    f"UPDATE {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE} "
                    "SET tool_names_id = ? WHERE id = ?",
                    (tool_names_id, tool_call_id),
                )
            elif int(stored[1]) != tool_names_id:
                raise RodexSessionStatisticsConflictError(
                    "canonical tool-call name changed after request verification"
                )
    else:
        for _attempt_number in index_re_try_attempt_numbers():
            public_id = uuid.uuid4()
            inserted = connection.execute(
                f"INSERT OR IGNORE INTO {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE} "
                "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
                "rodex_sessions_codex_activity_scopes_id, tool_names_id, "
                "tool_call_public_id_signed_bigint_1, "
                "tool_call_public_id_signed_bigint_2) VALUES (?, ?, ?, ?, ?, ?) "
                "RETURNING id",
                (
                    session_id,
                    thread_row_id,
                    scope_id,
                    tool_names_id,
                    *split_codex_thread_id_into_signed_bigints(public_id),
                ),
            ).fetchone()
            if inserted is not None:
                tool_call_id = int(inserted[0])
                break
        else:
            raise RodexSessionStatisticsConflictError(
                "could not allocate a tool-call public ID"
            )
    if call_id is not None and call_id_digest is not None:
        _insert_and_verify_tool_call_alias(
            connection,
            session_id=session_id,
            thread_row_id=thread_row_id,
            scope_id=scope_id,
            tool_call_id=tool_call_id,
            alias_kind="call_id",
            call_id=call_id,
            call_id_digest=call_id_digest,
        )
    if item_id is not None:
        _insert_and_verify_tool_call_alias(
            connection,
            session_id=session_id,
            thread_row_id=thread_row_id,
            scope_id=scope_id,
            tool_call_id=tool_call_id,
            alias_kind="item_id",
            item_id=item_id,
        )
    if call_id is None and item_id is None:
        _insert_and_verify_tool_call_alias(
            connection,
            session_id=session_id,
            thread_row_id=thread_row_id,
            scope_id=scope_id,
            tool_call_id=tool_call_id,
            alias_kind="source_event",
            event_id=event_id,
        )
    return tool_call_id


def _insert_and_verify_tool_call_alias(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    thread_row_id: int,
    scope_id: int,
    tool_call_id: int,
    alias_kind: str,
    call_id: str | None = None,
    call_id_digest: tuple[int, int, int, int] | None = None,
    item_id: int | None = None,
    event_id: int | None = None,
) -> None:
    hash_values: tuple[int | None, ...] = (
        (None, None, None, None) if call_id_digest is None else call_id_digest
    )
    connection.execute(
        f"INSERT OR IGNORE INTO {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE} "
        "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
        "rodex_sessions_codex_activity_scopes_id, "
        "rodex_sessions_codex_tool_calls_id, alias_kind, "
        "codex_call_id_sha256_int_1, codex_call_id_sha256_int_2, "
        "codex_call_id_sha256_int_3, codex_call_id_sha256_int_4, codex_call_id, "
        "rodex_sessions_codex_items_id, rodex_sessions_agent_trace_events_id, "
        "source_event_kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            thread_row_id,
            scope_id,
            tool_call_id,
            alias_kind,
            *hash_values,
            call_id,
            item_id,
            event_id,
            "tool_call" if alias_kind == "source_event" else None,
        ),
    )
    condition, parameters = (
        (
            "alias_kind = 'call_id' AND rodex_sessions_codex_threads_id = ? "
            "AND codex_call_id_sha256_int_1 = ? "
            "AND codex_call_id_sha256_int_2 = ? "
            "AND codex_call_id_sha256_int_3 = ? "
            "AND codex_call_id_sha256_int_4 = ?",
            (thread_row_id, *hash_values),
        )
        if alias_kind == "call_id"
        else (
            "alias_kind = 'item_id' AND rodex_sessions_codex_items_id = ?",
            (item_id,),
        )
        if alias_kind == "item_id"
        else (
            "alias_kind = 'source_event' AND rodex_sessions_agent_trace_events_id = ?",
            (event_id,),
        )
    )
    stored = connection.execute(
        "SELECT rodex_sessions_codex_tool_calls_id, codex_call_id "
        f"FROM {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE} WHERE {condition}",
        parameters,
    ).fetchone()
    if (
        stored is None
        or int(stored[0]) != tool_call_id
        or (alias_kind == "call_id" and str(stored[1]) != call_id)
    ):
        raise RodexSessionStatisticsConflictError(
            "canonical tool-call alias changed across trace events"
        )


def _sha256_signed_bigints(value: str) -> tuple[int, int, int, int]:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    pieces = tuple(
        int.from_bytes(digest[offset : offset + 8], "big", signed=True)
        for offset in range(0, 32, 8)
    )
    return pieces[0], pieces[1], pieces[2], pieces[3]


def _lookup_name(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    value: str | None,
    cache: dict[str, int],
) -> int | None:
    if value is None:
        return None
    if value in cache:
        return cache[value]
    row = connection.execute(
        f"SELECT id FROM {table_name} WHERE {column_name} = ?", (value,)
    ).fetchone()
    if row is None:
        row = connection.execute(
            f"INSERT INTO {table_name} ({column_name}) VALUES (?) RETURNING id", (value,)
        ).fetchone()
    assert row is not None
    cache[value] = int(row[0])
    return int(row[0])


def _nonnegative_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _trace_detail_sha256(kind: str, detail: TraceDetail) -> str:
    expected_type = _TRACE_DETAIL_TYPES[kind]
    if expected_type is None:
        if detail is not None:
            raise ValueError(f"agent trace {kind} event cannot have typed detail")
        value: object = None
        detail_type = "none"
    else:
        if not isinstance(detail, expected_type):
            raise ValueError(
                f"agent trace {kind} event requires {expected_type.__name__} detail"
            )
        value = asdict(detail)
        detail_type = expected_type.__name__
    canonical = json.dumps(
        {"detail_type": detail_type, "value": value},
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


_TRACE_EVENT_SELECT = f"""
SELECT events.id, events.trace_event_public_id_signed_bigint_1,
events.trace_event_public_id_signed_bigint_2,
source_ids.codex_thread_public_id_signed_bigint_1,
source_ids.codex_thread_public_id_signed_bigint_2,
turns.turn_public_id_signed_bigint_1, turns.turn_public_id_signed_bigint_2,
turns.codex_turn_id_signed_bigint_1, turns.codex_turn_id_signed_bigint_2,
events.source_record_ordinal, events.derived_event_ordinal,
events.first_trace_publication_sequence, events.event_kind, events.event_time_utc,
message_items.codex_item_id_signed_bigint_1,
message_items.codex_item_id_signed_bigint_2,
(SELECT item_aliases.codex_item_alias
    FROM {RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE} AS item_aliases
    WHERE item_aliases.rodex_sessions_codex_items_id = message_items.id
    ORDER BY item_aliases.id LIMIT 1),
message_items.item_public_id_signed_bigint_1,
message_items.item_public_id_signed_bigint_2,
messages.message_phase, messages.message_role,
messages.content_block_count, messages.body_utf8_bytes, messages.body_capture_state,
tool_calls.tool_call_public_id_signed_bigint_1,
tool_calls.tool_call_public_id_signed_bigint_2,
(SELECT aliases.codex_call_id
    FROM {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE} AS aliases
    WHERE aliases.rodex_sessions_codex_tool_calls_id = tool_calls.id
        AND aliases.alias_kind = 'call_id'
    ORDER BY aliases.id LIMIT 1),
tool_items.codex_item_id_signed_bigint_1,
tool_items.codex_item_id_signed_bigint_2,
(SELECT item_aliases.codex_item_alias
    FROM {RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE} AS item_aliases
    WHERE item_aliases.rodex_sessions_codex_items_id = tool_items.id
    ORDER BY item_aliases.id LIMIT 1),
tool_items.item_public_id_signed_bigint_1,
tool_items.item_public_id_signed_bigint_2,
tool_names.tool_name, tools.tool_status,
tools.request_utf8_bytes, tools.response_utf8_bytes, tools.payload_capture_state,
tools.activity_kind,
command_items.codex_item_id_signed_bigint_1,
command_items.codex_item_id_signed_bigint_2,
(SELECT item_aliases.codex_item_alias
    FROM {RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE} AS item_aliases
    WHERE item_aliases.rodex_sessions_codex_items_id = command_items.id
    ORDER BY item_aliases.id LIMIT 1),
command_items.item_public_id_signed_bigint_1,
command_items.item_public_id_signed_bigint_2,
commands.command_argument_count, commands.working_directory,
commands.command_status, commands.duration_ms, commands.exit_code,
commands.stdout_utf8_bytes, commands.stderr_utf8_bytes,
commands.aggregated_output_utf8_bytes, commands.payload_capture_state,
models.name_of_the_model, efforts.name_of_the_reasoning_effort,
contexts.working_directory, contexts.sandbox_mode, contexts.approval_policy,
contexts.permission_profile_type, contexts.workspace_root_count,
usage.input_tokens, usage.cached_input_tokens, usage.output_tokens,
usage.reasoning_output_tokens, usage.total_tokens, usage.context_used_percent,
target_ids.codex_thread_public_id_signed_bigint_1,
target_ids.codex_thread_public_id_signed_bigint_2,
activities.activity_kind, activities.agent_path
FROM {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} AS events
JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS sources
    ON sources.id = events.rodex_sessions_codex_threads_id
JOIN {CODEX_THREADS_TABLE} AS source_ids ON source_ids.id = sources.codex_threads_id
JOIN {RODEX_SESSIONS_CODEX_ACTIVITY_SCOPES_TABLE} AS scopes
    ON scopes.id = events.rodex_sessions_codex_activity_scopes_id
LEFT JOIN {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS turns
    ON turns.id = scopes.rodex_sessions_codex_turns_id
LEFT JOIN {RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE} AS messages
    ON messages.rodex_sessions_agent_trace_events_id = events.id
LEFT JOIN {RODEX_SESSIONS_CODEX_ITEMS_TABLE} AS message_items
    ON message_items.id = messages.rodex_sessions_codex_items_id
LEFT JOIN {RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE} AS tools
    ON tools.rodex_sessions_agent_trace_events_id = events.id
LEFT JOIN {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE} AS tool_calls
    ON tool_calls.id = tools.rodex_sessions_codex_tool_calls_id
LEFT JOIN {TOOL_NAMES_TABLE} AS tool_names ON tool_names.id = tool_calls.tool_names_id
LEFT JOIN {RODEX_SESSIONS_CODEX_ITEMS_TABLE} AS tool_items
    ON tool_items.id = tools.rodex_sessions_codex_items_id
LEFT JOIN {RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE} AS commands
    ON commands.rodex_sessions_agent_trace_events_id = events.id
LEFT JOIN {RODEX_SESSIONS_CODEX_ITEMS_TABLE} AS command_items
    ON command_items.id = commands.rodex_sessions_codex_items_id
LEFT JOIN {RODEX_SESSIONS_AGENT_TRACE_CONTEXTS_TABLE} AS contexts
    ON contexts.rodex_sessions_agent_trace_events_id = events.id
LEFT JOIN {MODEL_NAMES_TABLE} AS models ON models.id = contexts.model_names_id
LEFT JOIN {REASONING_EFFORT_NAMES_TABLE} AS efforts
    ON efforts.id = contexts.reasoning_effort_names_id
LEFT JOIN {RODEX_SESSIONS_AGENT_TRACE_TOKEN_USAGE_TABLE} AS usage
    ON usage.rodex_sessions_agent_trace_events_id = events.id
LEFT JOIN {RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE} AS activities
    ON activities.rodex_sessions_agent_trace_events_id = events.id
LEFT JOIN {CODEX_THREADS_TABLE} AS target_ids
    ON target_ids.id = activities.target_codex_threads_id
"""


def _trace_event_row_as_dict(
    row: tuple[object, ...], rate_windows: list[dict[str, Any]]
) -> dict[str, Any]:
    event = {
        "event_id": str(join_signed_bigints_into_a_codex_thread_id(row[1], row[2])),
        "codex_thread_id": str(join_signed_bigints_into_a_codex_thread_id(row[3], row[4])),
        "turn_id": _optional_public_id(row[5], row[6]),
        "codex_turn_id": _optional_codex_turn_id(row[7], row[8]),
        "source_record_ordinal": int(row[9]),
        "derived_event_ordinal": int(row[10]),
        "first_trace_publication_sequence": int(row[11]),
        "event_kind": str(row[12]),
        "event_time_utc": None if row[13] is None else str(row[13]),
        "detail": None,
    }
    kind = event["event_kind"]
    if kind == "message":
        event["detail"] = {
            "item_id": _optional_codex_item_id(row[14], row[15]),
            "item_alias": row[16],
            "item_public_id": _optional_public_id(row[17], row[18]),
            "message_phase": row[19],
            "message_role": row[20],
            "content_block_count": row[21],
            "body_utf8_bytes": row[22],
            "body_capture_state": row[23],
        }
    elif kind == "tool_call":
        event["detail"] = {
            "tool_call_id": str(
                join_signed_bigints_into_a_codex_thread_id(row[24], row[25])
            ),
            "call_id": row[26],
            "item_id": _optional_codex_item_id(row[27], row[28]),
            "item_alias": row[29],
            "item_public_id": _optional_public_id(row[30], row[31]),
            "tool_name": row[32],
            "activity_kind": row[37],
            "tool_status": row[33],
            "request_utf8_bytes": row[34],
            "response_utf8_bytes": row[35],
            "payload_capture_state": row[36],
        }
    elif kind == "command_execution":
        event["detail"] = {
            "item_id": _optional_codex_item_id(row[38], row[39]),
            "item_alias": row[40],
            "item_public_id": _optional_public_id(row[41], row[42]),
            "command_argument_count": row[43],
            "working_directory": row[44],
            "command_status": row[45],
            "duration_ms": row[46],
            "exit_code": row[47],
            "stdout_utf8_bytes": row[48],
            "stderr_utf8_bytes": row[49],
            "aggregated_output_utf8_bytes": row[50],
            "payload_capture_state": row[51],
        }
    elif kind == "turn_context":
        event["detail"] = {
            "model": row[52],
            "reasoning_effort": row[53],
            "working_directory": row[54],
            "sandbox_mode": row[55],
            "approval_policy": row[56],
            "permission_profile_type": row[57],
            "workspace_root_count": row[58],
        }
    elif kind == "token_usage":
        event["detail"] = {
            "input_tokens": row[59],
            "cached_input_tokens": row[60],
            "output_tokens": row[61],
            "reasoning_output_tokens": row[62],
            "total_tokens": row[63],
            "context_used_percent": row[64],
        }
    elif kind == "rate_limit":
        event["detail"] = {"windows": rate_windows}
    elif kind == "subagent_activity":
        target_id = _optional_public_id(row[65], row[66])
        event["detail"] = {
            "target_codex_thread_id": target_id,
            "activity_kind": row[67],
            "agent_path": row[68],
        }
    return event


def _optional_public_id(first_half: object, second_half: object) -> str | None:
    if first_half is None or second_half is None:
        return None
    return str(join_signed_bigints_into_a_codex_thread_id(first_half, second_half))


def _optional_codex_turn_id(first_half: object, second_half: object) -> str | None:
    if first_half is None or second_half is None:
        return None
    return str(join_signed_bigints_into_a_codex_turn_id(first_half, second_half))


def _optional_codex_item_id(first_half: object, second_half: object) -> str | None:
    if first_half is None or second_half is None:
        return None
    return str(join_signed_bigints_into_a_codex_item_id(first_half, second_half))
