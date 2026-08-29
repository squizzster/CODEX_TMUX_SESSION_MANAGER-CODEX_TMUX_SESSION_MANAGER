"""Prepared-only transactional writer for the durable agent trace."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid

from rodex_sql import (
    index_re_try_attempt_numbers,
    require_active_rodex_transaction,
    select_or_insert_lookup_id,
)

from .agent_request_reconciliation import (
    _insert_agent_request_from_activity,
    _reconcile_agent_request_target_turns,
    _resolve_existing_tool_call_by_call_id,
)
from .agent_trace_contract import (
    PreparedAgentTracePublication,
    RodexAgentTracePublishReceipt,
    TraceCommandExecution,
    TraceContext,
    TraceDetail,
    TraceMessage,
    TraceRateLimits,
    TraceSubagentActivity,
    TraceTokenUsage,
    TraceToolCall,
    require_contract_prepared_agent_trace_publication,
)
from .errors import (
    RodexSessionStatisticsConflictError,
    RodexSessionStatisticsPublicationRaceError,
)
from .execution import resolve_codex_thread_identity_in_transaction
from .identity import (
    CodexThreadId,
    join_signed_bigints_into_a_codex_thread_id,
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
)
from .validation import _validate_session_id


def publish_agent_trace_in_transaction(
    connection: sqlite3.Connection,
    session_id: int,
    publication: PreparedAgentTracePublication,
    *,
    model_name_ids: dict[str, int],
    reasoning_effort_name_ids: dict[str, int],
) -> RodexAgentTracePublishReceipt:
    """Append a deduplicated trace batch and advance its independent CAS head."""
    _validate_session_id(session_id)
    prepared = require_contract_prepared_agent_trace_publication(publication)
    require_active_rodex_transaction(connection)
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
    if current_sequence != prepared.based_on_trace_publication_sequence:
        raise RodexSessionStatisticsPublicationRaceError(
            "agent trace publication sequence changed during calculation"
        )
    sequence = 1 if current_sequence is None else current_sequence + 1
    thread_memberships = _trace_thread_memberships(
        connection,
        session_id,
        prepared.source_thread_ids,
    )
    schema_version = prepared.trace_schema_version
    coverage = prepared.coverage_state
    tool_name_ids: dict[str, int] = {}
    activity_scope_ids: dict[tuple[int, int | None], int] = {}
    inserted_event_count = 0
    inserted_unrecognized_count = 0
    reconciliation_target_ids: set[int] = set()
    for prepared_event in prepared.events:
        event = prepared_event.event
        key = prepared_event.source_key
        kind = prepared_event.event_kind
        detail_sha256 = prepared_event.detail_sha256
        thread_membership = thread_memberships.get(prepared_event.codex_thread_id)
        if thread_membership is None:
            raise RodexSessionStatisticsConflictError(
                "agent trace event identifies an unregistered Codex thread"
            )
        thread_row_id, codex_threads_id = thread_membership
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
        request_target_id = _insert_trace_detail(
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
        if request_target_id is not None:
            reconciliation_target_ids.add(request_target_id)
        if kind == "turn_started":
            reconciliation_target_ids.add(codex_threads_id)
    for target_codex_threads_id in reconciliation_target_ids:
        _reconcile_agent_request_target_turns(
            connection,
            session_id=session_id,
            target_codex_threads_id=target_codex_threads_id,
        )
    durable_event_count = prior_event_count + inserted_event_count
    unrecognized_count = prior_unrecognized_count + inserted_unrecognized_count
    if prior_coverage == "gapped" or unrecognized_count:
        coverage = "gapped"
    values = (
        sequence,
        schema_version,
        prepared.calculated_at_utc,
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
) -> int | None:
    if detail is None:
        return None
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
                detail.activity_kind,
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
        collaboration_tool_call_id = _resolve_existing_tool_call_by_call_id(
            connection,
            thread_row_id=thread_row_id,
            scope_id=scope_id,
            call_id=detail.collaboration_call_id,
        )
        inserted_activity = connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE} "
            "(rodex_sessions_agent_trace_events_id, rodex_sessions_id, "
            "rodex_sessions_codex_activity_scopes_id, target_codex_threads_id, "
            "rodex_sessions_codex_tool_calls_id, activity_kind, agent_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                event_id,
                session_id,
                scope_id,
                target_codex_threads_id,
                collaboration_tool_call_id,
                detail.activity_kind,
                detail.agent_path,
            ),
        ).fetchone()
        if inserted_activity is None:
            raise RodexSessionStatisticsConflictError(
                "sub-agent trace activity insertion returned no identity"
            )
        if target_codex_threads_id is None or collaboration_tool_call_id is None:
            return None
        inserted_request = _insert_agent_request_from_activity(
            connection,
            session_id=session_id,
            scope_id=scope_id,
            subagent_activity_id=int(inserted_activity[0]),
            target_codex_threads_id=target_codex_threads_id,
            collaboration_tool_call_id=collaboration_tool_call_id,
            activity_kind=detail.activity_kind,
        )
        return target_codex_threads_id if inserted_request else None
    else:
        raise TypeError(f"unsupported agent trace detail: {type(detail).__name__}")
    return None


def _trace_thread_memberships(
    connection: sqlite3.Connection,
    session_id: int,
    source_thread_ids: frozenset[CodexThreadId],
) -> dict[CodexThreadId, tuple[int, int]]:
    """Resolve only distinct source threads through bounded VALUES queries."""
    resolved: dict[CodexThreadId, tuple[int, int]] = {}
    identities = sorted(
        split_codex_thread_id_into_signed_bigints(thread_id)
        for thread_id in source_thread_ids
    )
    chunk_size = 400
    for offset in range(0, len(identities), chunk_size):
        chunk = identities[offset : offset + chunk_size]
        placeholders = ", ".join("(?, ?)" for _identity in chunk)
        parameters: list[object] = [session_id]
        for identity in chunk:
            parameters.extend(identity)
        rows = connection.execute(
            f"SELECT memberships.id, memberships.codex_threads_id, "
            "identities.codex_thread_public_id_signed_bigint_1, "
            "identities.codex_thread_public_id_signed_bigint_2 "
            f"FROM {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS memberships "
            f"JOIN {CODEX_THREADS_TABLE} AS identities "
            "ON identities.id = memberships.codex_threads_id "
            "WHERE memberships.rodex_sessions_id = ? "
            "AND (identities.codex_thread_public_id_signed_bigint_1, "
            "identities.codex_thread_public_id_signed_bigint_2) "
            f"IN (VALUES {placeholders})",
            parameters,
        ).fetchall()
        resolved.update(
            {
                join_signed_bigints_into_a_codex_thread_id(row[2], row[3]): (
                    int(row[0]),
                    int(row[1]),
                )
                for row in rows
            }
        )
    return resolved


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
    source_item_id = codex_item_id
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
    matched_call_ids: set[int] = set()
    call_id = None
    call_id_digest = None
    if detail.call_id is not None:
        call_id = detail.call_id
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
    resolved = select_or_insert_lookup_id(
        connection,
        table_name,
        {column_name: value},
    )
    cache[value] = resolved
    return resolved
