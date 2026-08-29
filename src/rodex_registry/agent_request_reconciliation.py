"""Canonical request provenance and FIFO target-turn reconciliation."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid

from rodex_sql import index_re_try_attempt_numbers

from .errors import RodexSessionStatisticsConflictError
from .identity import split_codex_thread_id_into_signed_bigints
from .schema import (
    RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE,
    RODEX_SESSIONS_AGENT_REQUESTS_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
    RODEX_SESSIONS_CODEX_THREADS_TABLE,
    RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE,
    RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
    RODEX_SESSIONS_CODEX_TURN_STATES_TABLE,
    RODEX_SESSIONS_CODEX_TURNS_TABLE,
    TOOL_NAMES_TABLE,
)


def _resolve_existing_tool_call_by_call_id(
    connection: sqlite3.Connection,
    *,
    thread_row_id: int,
    scope_id: int,
    call_id: str | None,
) -> int | None:
    """Resolve only an already published exact call alias; never infer a match."""
    if call_id is None:
        return None
    source_call_id = call_id
    row = connection.execute(
        "SELECT aliases.rodex_sessions_codex_tool_calls_id, "
        "aliases.codex_call_id, calls.rodex_sessions_codex_activity_scopes_id "
        f"FROM {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE} AS aliases "
        f"JOIN {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE} AS calls "
        "ON calls.id = aliases.rodex_sessions_codex_tool_calls_id "
        "WHERE aliases.rodex_sessions_codex_threads_id = ? "
        "AND aliases.alias_kind = 'call_id' "
        "AND aliases.codex_call_id_sha256_int_1 = ? "
        "AND aliases.codex_call_id_sha256_int_2 = ? "
        "AND aliases.codex_call_id_sha256_int_3 = ? "
        "AND aliases.codex_call_id_sha256_int_4 = ?",
        (thread_row_id, *_sha256_signed_bigints(source_call_id)),
    ).fetchone()
    if row is None:
        return None
    if str(row[1]) != source_call_id:
        raise RodexSessionStatisticsConflictError(
            "Codex collaboration call ID digest collision"
        )
    if int(row[2]) != scope_id:
        raise RodexSessionStatisticsConflictError(
            "sub-agent activity call belongs to a different parent turn"
        )
    return int(row[0])


def _insert_agent_request_from_activity(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    scope_id: int,
    subagent_activity_id: int,
    target_codex_threads_id: int,
    collaboration_tool_call_id: int,
    activity_kind: str,
) -> bool:
    """Create one canonical request from exact message/call/activity provenance."""
    tool_activity = connection.execute(
        "SELECT activities.id, names.tool_name, "
        "events.source_record_ordinal, events.derived_event_ordinal "
        f"FROM {RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE} AS activities "
        f"JOIN {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE} AS calls "
        "ON calls.id = activities.rodex_sessions_codex_tool_calls_id "
        f"JOIN {TOOL_NAMES_TABLE} AS names ON names.id = calls.tool_names_id "
        f"JOIN {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} AS events "
        "ON events.id = activities.rodex_sessions_agent_trace_events_id "
        "WHERE activities.rodex_sessions_id = ? "
        "AND activities.rodex_sessions_codex_activity_scopes_id = ? "
        "AND activities.rodex_sessions_codex_tool_calls_id = ? "
        "AND activities.activity_kind = 'request'",
        (session_id, scope_id, collaboration_tool_call_id),
    ).fetchone()
    if tool_activity is None:
        return False
    expected_activity_kind = {
        "collaboration.spawn_agent": "started",
        "collaboration.followup_task": "interacted",
    }.get(str(tool_activity[1]))
    if expected_activity_kind is None or activity_kind != expected_activity_kind:
        return False
    parent_message = connection.execute(
        "SELECT messages.id "
        f"FROM {RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE} AS messages "
        f"JOIN {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} AS events "
        "ON events.id = messages.rodex_sessions_agent_trace_events_id "
        "WHERE messages.rodex_sessions_id = ? "
        "AND messages.rodex_sessions_codex_activity_scopes_id = ? "
        "AND messages.message_role = 'user' "
        "AND messages.body_capture_state = 'rollout_reference' "
        "AND messages.rodex_sessions_codex_items_id IS NOT NULL "
        "AND (events.source_record_ordinal < ? OR "
        "(events.source_record_ordinal = ? "
        "AND events.derived_event_ordinal < ?)) "
        "ORDER BY events.source_record_ordinal DESC, "
        "events.derived_event_ordinal DESC, events.id DESC LIMIT 1",
        (
            session_id,
            scope_id,
            int(tool_activity[2]),
            int(tool_activity[2]),
            int(tool_activity[3]),
        ),
    ).fetchone()
    if parent_message is None:
        return False
    for _attempt_number in index_re_try_attempt_numbers():
        public_id = uuid.uuid4()
        inserted = connection.execute(
            f"INSERT OR IGNORE INTO {RODEX_SESSIONS_AGENT_REQUESTS_TABLE} "
            "(agent_request_public_id_signed_bigint_1, "
            "agent_request_public_id_signed_bigint_2, rodex_sessions_id, "
            "rodex_sessions_codex_activity_scopes_id, "
            "parent_rodex_sessions_agent_trace_messages_id, "
            "rodex_sessions_agent_trace_tool_call_activities_id, "
            "rodex_sessions_agent_trace_subagent_activities_id, "
            "target_codex_threads_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "RETURNING id",
            (
                *split_codex_thread_id_into_signed_bigints(public_id),
                session_id,
                scope_id,
                int(parent_message[0]),
                int(tool_activity[0]),
                subagent_activity_id,
                target_codex_threads_id,
            ),
        ).fetchone()
        if inserted is not None:
            return True
        stored = connection.execute(
            f"SELECT id FROM {RODEX_SESSIONS_AGENT_REQUESTS_TABLE} "
            "WHERE rodex_sessions_agent_trace_subagent_activities_id = ?",
            (subagent_activity_id,),
        ).fetchone()
        if stored is not None:
            return False
    raise RodexSessionStatisticsConflictError(
        "could not allocate an agent request public ID"
    )


def _reconcile_agent_request_target_turns(
    connection: sqlite3.Connection,
    *,
    session_id: int,
    target_codex_threads_id: int,
) -> None:
    """FIFO-link unmatched requests to later distinct turns on one exact agent."""
    pending = connection.execute(
        "SELECT requests.id, request_events.event_time_utc "
        f"FROM {RODEX_SESSIONS_AGENT_REQUESTS_TABLE} AS requests "
        f"JOIN {RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE} AS activities "
        "ON activities.id = "
        "requests.rodex_sessions_agent_trace_subagent_activities_id "
        f"JOIN {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} AS request_events "
        "ON request_events.id = activities.rodex_sessions_agent_trace_events_id "
        f"LEFT JOIN {RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE} AS linked "
        "ON linked.rodex_sessions_agent_requests_id = requests.id "
        "WHERE requests.rodex_sessions_id = ? "
        "AND requests.target_codex_threads_id = ? "
        "AND request_events.event_time_utc IS NOT NULL "
        "AND linked.id IS NULL "
        "ORDER BY julianday(request_events.event_time_utc), "
        "request_events.id, requests.id",
        (session_id, target_codex_threads_id),
    ).fetchall()
    for request_id, request_time in pending:
        target_turn = connection.execute(
            "SELECT turns.id "
            f"FROM {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS membership "
            f"JOIN {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS turns "
            "ON turns.rodex_sessions_codex_threads_id = membership.id "
            f"JOIN {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} AS states "
            "ON states.rodex_sessions_codex_turns_id = turns.id "
            f"LEFT JOIN {RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE} AS linked "
            "ON linked.target_rodex_sessions_codex_turns_id = turns.id "
            "WHERE membership.rodex_sessions_id = ? "
            "AND membership.codex_threads_id = ? "
            "AND states.started_at_utc IS NOT NULL "
            "AND julianday(states.started_at_utc) >= julianday(?) "
            "AND linked.id IS NULL "
            "ORDER BY julianday(states.started_at_utc), turns.id LIMIT 1",
            (session_id, target_codex_threads_id, request_time),
        ).fetchone()
        if target_turn is None:
            return
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE} "
            "(rodex_sessions_id, rodex_sessions_agent_requests_id, "
            "target_rodex_sessions_codex_turns_id) VALUES (?, ?, ?)",
            (session_id, int(request_id), int(target_turn[0])),
        )


def _sha256_signed_bigints(value: str) -> tuple[int, int, int, int]:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    pieces = tuple(
        int.from_bytes(digest[offset : offset + 8], "big", signed=True)
        for offset in range(0, 32, 8)
    )
    return pieces[0], pieces[1], pieces[2], pieces[3]
