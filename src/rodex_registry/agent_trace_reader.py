"""Bounded read-only projections for the durable agent trace."""

from __future__ import annotations

import os
import uuid
from typing import Any

from rodex_sql import normalise_rodex_database_path, open_rodex_read_transaction

from .agent_trace_contract import RodexAgentTraceSnapshot
from .identity import (
    join_signed_bigints_into_a_codex_item_id,
    join_signed_bigints_into_a_codex_thread_id,
    join_signed_bigints_into_a_codex_turn_id,
    split_codex_thread_id_into_signed_bigints,
)
from .schema import (
    CODEX_THREADS_TABLE,
    MODEL_NAMES_TABLE,
    REASONING_EFFORT_NAMES_TABLE,
    RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE,
    RODEX_SESSIONS_AGENT_REQUESTS_TABLE,
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
)
from .validation import _validate_session_id


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
    path = normalise_rodex_database_path(database_path)
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


def read_rodex_agent_trace_cursor(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> uuid.UUID | None:
    """Return the latest durable event identity using the session-order index."""
    _validate_session_id(session_id)
    path = normalise_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        row = connection.execute(
            f"SELECT trace_event_public_id_signed_bigint_1, "
            "trace_event_public_id_signed_bigint_2 FROM "
            f"{RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} "
            "WHERE rodex_sessions_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return join_signed_bigints_into_a_codex_thread_id(row[0], row[1])


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
activities.activity_kind, activities.agent_path,
agent_requests.agent_request_public_id_signed_bigint_1,
agent_requests.agent_request_public_id_signed_bigint_2,
CASE invocation_tool_names.tool_name
    WHEN 'collaboration.spawn_agent' THEN 'initial'
    WHEN 'collaboration.followup_task' THEN 'follow_up'
END,
parent_request_items.item_public_id_signed_bigint_1,
parent_request_items.item_public_id_signed_bigint_2,
parent_request_events.trace_event_public_id_signed_bigint_1,
parent_request_events.trace_event_public_id_signed_bigint_2,
target_turns.turn_public_id_signed_bigint_1,
target_turns.turn_public_id_signed_bigint_2,
target_turns.codex_turn_id_signed_bigint_1,
target_turns.codex_turn_id_signed_bigint_2,
target_turn_states.outcome,
request_target_turns.association_kind,
invocation_tool_calls.tool_call_public_id_signed_bigint_1,
invocation_tool_calls.tool_call_public_id_signed_bigint_2,
(SELECT aliases.codex_call_id
    FROM {RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE} AS aliases
    WHERE aliases.rodex_sessions_codex_tool_calls_id = invocation_tool_calls.id
        AND aliases.alias_kind = 'call_id'
    ORDER BY aliases.id LIMIT 1),
invocation_tool_names.tool_name,
(SELECT request.request_utf8_bytes
    FROM {RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE} AS request
    WHERE request.rodex_sessions_codex_tool_calls_id = invocation_tool_calls.id
        AND request.activity_kind = 'request'
    ORDER BY request.id LIMIT 1),
(SELECT request.payload_capture_state
    FROM {RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE} AS request
    WHERE request.rodex_sessions_codex_tool_calls_id = invocation_tool_calls.id
        AND request.activity_kind = 'request'
    ORDER BY request.id LIMIT 1)
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
LEFT JOIN {RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE} AS invocation_tool_calls
    ON invocation_tool_calls.id = activities.rodex_sessions_codex_tool_calls_id
LEFT JOIN {TOOL_NAMES_TABLE} AS invocation_tool_names
    ON invocation_tool_names.id = invocation_tool_calls.tool_names_id
LEFT JOIN {RODEX_SESSIONS_AGENT_REQUESTS_TABLE} AS agent_requests
    ON agent_requests.rodex_sessions_agent_trace_subagent_activities_id = activities.id
LEFT JOIN {RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE} AS parent_request_messages
    ON parent_request_messages.id =
        agent_requests.parent_rodex_sessions_agent_trace_messages_id
LEFT JOIN {RODEX_SESSIONS_CODEX_ITEMS_TABLE} AS parent_request_items
    ON parent_request_items.id =
        parent_request_messages.rodex_sessions_codex_items_id
LEFT JOIN {RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} AS parent_request_events
    ON parent_request_events.id =
        parent_request_messages.rodex_sessions_agent_trace_events_id
LEFT JOIN {RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE} AS request_target_turns
    ON request_target_turns.rodex_sessions_agent_requests_id = agent_requests.id
LEFT JOIN {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS target_turns
    ON target_turns.id = request_target_turns.target_rodex_sessions_codex_turns_id
LEFT JOIN {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} AS target_turn_states
    ON target_turn_states.rodex_sessions_codex_turns_id = target_turns.id
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
        invocation = None
        if row[82] is not None and row[83] is not None:
            invocation = {
                "tool_call_id": _optional_public_id(row[82], row[83]),
                "source_call_id": row[84],
                "tool_name": row[85],
                "arguments_utf8_bytes": row[86],
                "arguments_capture_state": row[87],
            }
        turn_request = None
        if row[69] is not None and row[70] is not None:
            turn_request = {
                "request_id": _optional_public_id(row[69], row[70]),
                "request_kind": row[71],
                "root_request_item_public_id": _optional_public_id(row[72], row[73]),
                "root_request_message_event_id": _optional_public_id(row[74], row[75]),
                "target_turn_id": _optional_public_id(row[76], row[77]),
                "target_codex_turn_id": _optional_codex_turn_id(row[78], row[79]),
                "target_turn_outcome": row[80],
                "target_turn_association_kind": row[81],
            }
        event["detail"] = {
            "target_codex_thread_id": target_id,
            "activity_kind": row[67],
            "agent_path": row[68],
            "collaboration_invocation": invocation,
            "turn_request": turn_request,
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
