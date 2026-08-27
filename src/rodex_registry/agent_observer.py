"""Bounded read projection for the developer-facing live agent observer."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

from rodex_sql import open_rodex_read_transaction

from .identity import (
    CodexThreadId,
    join_signed_bigints_into_a_codex_thread_id,
    join_signed_bigints_into_a_codex_turn_id,
    parse_codex_thread_id,
    parse_codex_turn_id,
    split_codex_thread_id_into_signed_bigints,
    split_codex_turn_id_into_signed_bigints,
)
from .schema import (
    CODEX_THREADS_TABLE,
    MODEL_NAMES_TABLE,
    REASONING_EFFORT_NAMES_TABLE,
    RODEX_SESSIONS_CODEX_THREADS_TABLE,
    RODEX_SESSIONS_CODEX_TURN_STATES_TABLE,
    RODEX_SESSIONS_CODEX_TURNS_TABLE,
    RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE,
    RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE,
    existing_rodex_database_path,
)
from .validation import _validate_session_id


@dataclass(frozen=True, slots=True)
class RodexAgentObserverTurnEvidence:
    """Human-relevant facts for one exact observed agent turn."""

    codex_thread_id: CodexThreadId
    codex_turn_id: str
    agent_path: str
    agent_nickname: str | None
    history_inheritance_kind: str
    inherited_history_start_ordinal: int | None
    outcome: str
    model: str | None
    reasoning_effort: str | None
    actions_completed_count: int | None
    commands_executed_count: int | None
    file_change_operations_count: int | None
    web_operations_count: int | None
    web_queries_count: int | None
    web_result_records_count: int | None
    compactions_count: int | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None


def read_rodex_agent_observer_turn_evidence(
    session_id: int,
    turn_keys: Sequence[tuple[CodexThreadId | str, str]],
    database_path: str | os.PathLike[str] | None = None,
) -> tuple[RodexAgentObserverTurnEvidence, ...]:
    """Read only the exact tracked agent turns in one indexed transaction."""
    _validate_session_id(session_id)
    if not turn_keys:
        return ()
    canonical_keys: list[tuple[CodexThreadId, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_thread_id, raw_turn_id in turn_keys:
        thread_id = parse_codex_thread_id(raw_thread_id)
        turn_id = str(parse_codex_turn_id(raw_turn_id))
        key = (str(thread_id), turn_id)
        if key in seen:
            continue
        seen.add(key)
        canonical_keys.append((thread_id, turn_id))

    values_sql = ", ".join("(?, ?, ?, ?, ?)" for _key in canonical_keys)
    parameters: list[object] = []
    for ordinal, (thread_id, turn_id) in enumerate(canonical_keys):
        parameters.extend(
            (
                ordinal,
                *split_codex_thread_id_into_signed_bigints(thread_id),
                *split_codex_turn_id_into_signed_bigints(turn_id),
            )
        )
    parameters.append(session_id)
    query = f"""
        WITH requested(
            request_ordinal,
            thread_id_1,
            thread_id_2,
            turn_id_1,
            turn_id_2
        ) AS (VALUES {values_sql})
        SELECT
            requested.request_ordinal,
            thread_ids.codex_thread_public_id_signed_bigint_1,
            thread_ids.codex_thread_public_id_signed_bigint_2,
            turns.codex_turn_id_signed_bigint_1,
            turns.codex_turn_id_signed_bigint_2,
            spawns.agent_path,
            spawns.agent_nickname,
            spawns.history_inheritance_kind,
            spawns.inherited_history_start_ordinal,
            states.outcome,
            models.name_of_the_model,
            efforts.name_of_the_reasoning_effort,
            metrics.model_tool_outputs_paired_count,
            metrics.commands_executed_count,
            metrics.file_change_operations_count,
            metrics.web_operations_count,
            metrics.web_queries_count,
            metrics.web_result_records_count,
            metrics.compactions_count,
            metrics.input_tokens,
            metrics.cached_input_tokens,
            metrics.output_tokens,
            metrics.reasoning_output_tokens,
            metrics.total_tokens
        FROM requested
        JOIN {CODEX_THREADS_TABLE} AS thread_ids
            ON thread_ids.codex_thread_public_id_signed_bigint_1 =
                requested.thread_id_1
            AND thread_ids.codex_thread_public_id_signed_bigint_2 =
                requested.thread_id_2
        JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS memberships
            ON memberships.rodex_sessions_id = ?
            AND memberships.codex_threads_id = thread_ids.id
        JOIN {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS turns
            ON turns.rodex_sessions_codex_threads_id = memberships.id
            AND turns.codex_turn_id_signed_bigint_1 = requested.turn_id_1
            AND turns.codex_turn_id_signed_bigint_2 = requested.turn_id_2
        JOIN {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} AS states
            ON states.rodex_sessions_codex_turns_id = turns.id
        JOIN {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} AS spawns
            ON spawns.subagent_rodex_sessions_codex_threads_id = memberships.id
        LEFT JOIN {MODEL_NAMES_TABLE} AS models
            ON models.id = states.model_names_id
        LEFT JOIN {REASONING_EFFORT_NAMES_TABLE} AS efforts
            ON efforts.id = states.reasoning_effort_names_id
        LEFT JOIN {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} AS metrics
            ON metrics.rodex_sessions_codex_turns_id = turns.id
        ORDER BY requested.request_ordinal
    """
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        rows = connection.execute(query, tuple(parameters)).fetchall()
    return tuple(_turn_evidence_from_row(row) for row in rows)


def _turn_evidence_from_row(
    row: tuple[object, ...],
) -> RodexAgentObserverTurnEvidence:
    return RodexAgentObserverTurnEvidence(
        codex_thread_id=join_signed_bigints_into_a_codex_thread_id(row[1], row[2]),
        codex_turn_id=str(join_signed_bigints_into_a_codex_turn_id(row[3], row[4])),
        agent_path=str(row[5]),
        agent_nickname=None if row[6] is None else str(row[6]),
        history_inheritance_kind=str(row[7]),
        inherited_history_start_ordinal=(None if row[8] is None else int(row[8])),
        outcome=str(row[9]),
        model=None if row[10] is None else str(row[10]),
        reasoning_effort=None if row[11] is None else str(row[11]),
        actions_completed_count=_optional_int(row[12]),
        commands_executed_count=_optional_int(row[13]),
        file_change_operations_count=_optional_int(row[14]),
        web_operations_count=_optional_int(row[15]),
        web_queries_count=_optional_int(row[16]),
        web_result_records_count=_optional_int(row[17]),
        compactions_count=_optional_int(row[18]),
        input_tokens=_optional_int(row[19]),
        cached_input_tokens=_optional_int(row[20]),
        output_tokens=_optional_int(row[21]),
        reasoning_output_tokens=_optional_int(row[22]),
        total_tokens=_optional_int(row[23]),
    )


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)
