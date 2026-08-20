"""Authoritative relational statistics publication and read pipeline."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from rodex_sql import (
    open_rodex_read_transaction,
    open_rodex_transaction,
    select_or_insert_lookup_id,
)

from .errors import (
    RodexSessionError,
    RodexSessionStatisticsConflictError,
    RodexSessionTurnStatisticsAmbiguousError,
)
from .identity import (
    CodexSessionId,
    CodexThreadId,
    join_signed_bigints_into_a_codex_thread_id,
    parse_codex_thread_id,
    split_codex_session_id_into_signed_bigints,
    split_codex_thread_id_into_signed_bigints,
)
from .schema import (
    MODEL_NAMES_TABLE,
    REASONING_EFFORT_NAMES_TABLE,
    RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE,
    RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE,
    RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE,
    RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
    RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE,
    RODEX_SESSIONS_STATISTICS_TABLE,
    RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE,
    RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
    RODEX_SESSIONS_STATISTICS_WORKERS_TABLE,
    RODEX_SESSIONS_TABLE,
    STATISTICS_COVERAGE_STATES,
    STATISTICS_WORKER_STATES,
    existing_rodex_database_path,
    initialise_rodex_database,
)
from .statistics_fields import SESSION_STATISTICS_SCALARS, TURN_STATISTICS_SCALARS
from .statistics_projection import (
    COLLABORATION_MODEL_TOOL_NAMES,
    SessionStatisticsProjection,
    StatisticsDistribution,
    StatisticsNamedCount,
    TurnStatisticsProjection,
    validate_session_statistics_projection,
)
from .validation import (
    _normalise_required_text,
    _normalise_utc_timestamp_text,
    _validate_positive_id,
    _validate_session_id,
)

_DERIVED_SESSION_NAMED_COUNT_KINDS = frozenset(
    {"model", "reasoning_effort", "collaboration_tool"}
)


@dataclass(frozen=True, slots=True)
class RodexSessionStatistics:
    """Latest fully relational analyzer projection for one Rodex session."""

    id: int
    rodex_sessions_id: int
    statistics_publication_sequence: int
    statistics_projection_schema_version: str
    calculated_at_utc: str
    coverage_state: str
    projection: SessionStatisticsProjection


@dataclass(frozen=True, slots=True)
class RodexSessionStatisticsSource:
    """One exact root or sub-agent thread rollout and its analyzed provenance."""

    id: int
    rodex_sessions_id: int
    codex_thread_id: CodexThreadId
    source_kind: str
    parent_rodex_sessions_statistics_sources_id: int | None
    thread_depth: int
    agent_path: str | None
    agent_nickname: str | None
    subagent_history_start_ordinal: int | None
    spawning_codex_turn_id: str | None
    first_linked_at_utc: str
    rollout_file_path: str | None
    analyzed_size_bytes: int | None
    analyzed_mtime_ns: int | None
    analyzed_prefix_sha256: str | None
    verified_at_utc: str | None
    included_statistics_publication_sequence: int | None


@dataclass(frozen=True, slots=True)
class RodexSessionStatisticsSourceObservation:
    """Exact thread identity, hierarchy, and bytes used for one calculation."""

    codex_thread_id: CodexThreadId
    source_kind: str
    parent_codex_thread_id: CodexThreadId | None
    thread_depth: int
    agent_path: str | None
    agent_nickname: str | None
    subagent_history_start_ordinal: int | None
    spawning_codex_turn_id: str | None
    first_linked_at_utc: str
    rollout_file_path: Path
    analyzed_size_bytes: int
    analyzed_mtime_ns: int
    analyzed_prefix_sha256: str
    verified_at_utc: str


@dataclass(frozen=True, slots=True)
class RodexSessionTurnStatistics:
    """Latest persisted statistics projection for one exact Codex turn."""

    id: int
    rodex_sessions_id: int
    rodex_sessions_statistics_sources_id: int
    included_statistics_publication_sequence: int
    projection: TurnStatisticsProjection

    @property
    def codex_thread_id(self) -> CodexThreadId:
        return self.projection.codex_thread_id

    @property
    def codex_turn_id(self) -> str:
        return self.projection.codex_turn_id

    @property
    def started_at_utc(self) -> str | None:
        return self.projection.started_at_utc

    @property
    def terminal_at_utc(self) -> str | None:
        return self.projection.terminal_at_utc

    @property
    def outcome(self) -> str:
        return self.projection.outcome


@dataclass(frozen=True, slots=True)
class RodexSessionStatisticsWorker:
    """Independent health of one fail-open analytics worker."""

    id: int
    rodex_sessions_id: int
    worker_state: str
    diagnostic_code: str | None
    last_attempted_at_utc: str
    consecutive_failures: int
    next_retry_at_utc: str | None


@dataclass(frozen=True, slots=True)
class RodexSessionStatisticsView:
    """One transactionally consistent statistics, health, and provenance read."""

    statistics: RodexSessionStatistics | None
    worker: RodexSessionStatisticsWorker | None
    sources: tuple[RodexSessionStatisticsSource, ...]


@dataclass(frozen=True, slots=True)
class RodexSessionStatisticsSourceSummary:
    """SQL-derived additive lifecycle and resource totals for one thread source."""

    source: RodexSessionStatisticsSource
    turns_started_count: int
    turns_completed_count: int
    turns_aborted_count: int
    turns_open_count: int
    first_turn_started_at_utc: str | None
    last_turn_terminal_at_utc: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    commands_executed_count: int
    model_tool_requests_count: int
    file_change_operations_count: int
    file_change_occurrences_count: int
    web_operations_count: int
    web_queries_count: int
    web_result_records_count: int
    collaboration_operations_count: int
    collaboration_agents_started_count: int
    compactions_count: int
    named_counts: tuple[StatisticsNamedCount, ...]


@dataclass(frozen=True, slots=True)
class RodexSessionTurnStatisticsView:
    """One transactionally consistent exact-turn and parent statistics read."""

    statistics: RodexSessionStatistics | None
    worker: RodexSessionStatisticsWorker | None
    sources: tuple[RodexSessionStatisticsSource, ...]
    turn: RodexSessionTurnStatistics | None


def publish_rodex_session_statistics(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    expected_current_codex_session_id: CodexSessionId | str,
    based_on_statistics_publication_sequence: int | None,
    statistics_projection_schema_version: str,
    calculated_at_utc: str,
    coverage_state: str,
    statistics_projection: SessionStatisticsProjection,
    analyzed_sources: Sequence[RodexSessionStatisticsSourceObservation],
) -> RodexSessionStatistics:
    """Atomically publish one fenced session projection, turns, and sources."""
    _validate_session_id(session_id)
    expected_halves = split_codex_session_id_into_signed_bigints(
        expected_current_codex_session_id
    )
    if based_on_statistics_publication_sequence is not None:
        _validate_positive_id(
            based_on_statistics_publication_sequence,
            "based_on_statistics_publication_sequence",
        )
    schema_version = _normalise_required_text(
        statistics_projection_schema_version,
        "statistics_projection_schema_version",
    )
    calculated = _normalise_utc_timestamp_text(calculated_at_utc)
    coverage = _normalise_required_text(coverage_state, "coverage_state")
    if coverage not in STATISTICS_COVERAGE_STATES:
        raise ValueError(f"unsupported statistics coverage state: {coverage}")
    statistics_projection = validate_session_statistics_projection(statistics_projection)
    observations = tuple(_validate_source_observation(item) for item in analyzed_sources)
    if len({item.codex_thread_id for item in observations}) != len(observations):
        raise ValueError("analyzed_sources contains a duplicate Codex thread ID")
    observations_by_thread = {item.codex_thread_id: item for item in observations}
    for item in observations:
        if item.parent_codex_thread_id is None:
            continue
        parent = observations_by_thread.get(item.parent_codex_thread_id)
        if parent is None or item.thread_depth != parent.thread_depth + 1:
            raise ValueError(
                "sub-agent source depth must follow its observed parent thread"
            )
    if statistics_projection.analyzer_source_count != len(observations):
        raise ValueError(
            "analyzer source count must equal authenticated source observations"
        )
    turns = statistics_projection.turn_statistics
    turn_keys = {(item.codex_thread_id, item.codex_turn_id) for item in turns}
    if len(turn_keys) != len(turns):
        raise ValueError("turn_statistics contains a duplicate source and turn ID")
    _validate_authoritative_collaboration_projection(statistics_projection, observations)

    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        identity_row = connection.execute(
            f"SELECT codex_session_id_signed_bigint_1, codex_session_id_signed_bigint_2 "
            f"FROM {RODEX_SESSIONS_TABLE} WHERE id = ?",
            (session_id,),
        ).fetchone()
        if identity_row is None:
            raise RodexSessionError(f"Rodex session does not exist: {session_id}")
        if (int(identity_row[0]), int(identity_row[1])) != expected_halves:
            raise RodexSessionStatisticsConflictError(
                "current Codex session ID changed during statistics calculation"
            )
        previous_row = connection.execute(
            f"SELECT statistics_publication_sequence "
            f"FROM {RODEX_SESSIONS_STATISTICS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
        previous_publication_sequence = (
            None if previous_row is None else int(previous_row[0])
        )
        if previous_publication_sequence != based_on_statistics_publication_sequence:
            raise RodexSessionStatisticsConflictError(
                "statistics publication sequence changed during calculation"
            )
        new_publication_sequence = (
            1
            if previous_publication_sequence is None
            else previous_publication_sequence + 1
        )
        registered_rows = connection.execute(
            f"SELECT sources.id, sources.codex_thread_id_signed_bigint_1, "
            "sources.codex_thread_id_signed_bigint_2, "
            "sources.parent_rodex_sessions_statistics_sources_id, "
            "sources.agent_path, sources.agent_nickname, "
            "sources.subagent_history_start_ordinal, "
            "spawning_turn.codex_turn_id "
            f"FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} AS sources "
            f"LEFT JOIN {RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE} AS spawns "
            "ON spawns.subagent_rodex_sessions_statistics_sources_id = sources.id "
            f"LEFT JOIN {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} AS spawning_turn "
            "ON spawning_turn.id = spawns.spawning_rodex_sessions_statistics_turns_id "
            "WHERE sources.rodex_sessions_id = ?",
            (session_id,),
        ).fetchall()
        existing_by_thread = {(int(row[1]), int(row[2])): row for row in registered_rows}
        source_ids = {
            thread_halves: int(row[0]) for thread_halves, row in existing_by_thread.items()
        }
        previously_registered = frozenset(existing_by_thread)
        observed = frozenset(
            split_codex_thread_id_into_signed_bigints(item.codex_thread_id)
            for item in observations
        )
        if not previously_registered.issubset(observed):
            raise RodexSessionStatisticsConflictError(
                "statistics omit a registered Codex thread source"
            )
        for item in sorted(observations, key=lambda observation: observation.thread_depth):
            thread_halves = split_codex_thread_id_into_signed_bigints(item.codex_thread_id)
            parent_source_id = (
                None
                if item.parent_codex_thread_id is None
                else source_ids.get(
                    split_codex_thread_id_into_signed_bigints(item.parent_codex_thread_id)
                )
            )
            if item.source_kind == "subagent" and parent_source_id is None:
                raise RodexSessionStatisticsConflictError(
                    "sub-agent statistics source has no published parent thread"
                )
            existing = existing_by_thread.get(thread_halves)
            expected_metadata = (
                parent_source_id,
                item.agent_path,
                item.agent_nickname,
                item.subagent_history_start_ordinal,
                item.spawning_codex_turn_id,
            )
            if existing is None:
                if item.source_kind != "subagent":
                    raise RodexSessionStatisticsConflictError(
                        "statistics include an unregistered root thread source"
                    )
                row = connection.execute(
                    f"INSERT INTO {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
                    "(rodex_sessions_id, codex_thread_id_signed_bigint_1, "
                    "codex_thread_id_signed_bigint_2, "
                    "parent_rodex_sessions_statistics_sources_id, agent_path, "
                    "agent_nickname, subagent_history_start_ordinal, "
                    "first_linked_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "RETURNING id",
                    (
                        session_id,
                        *thread_halves,
                        parent_source_id,
                        item.agent_path,
                        item.agent_nickname,
                        item.subagent_history_start_ordinal,
                        item.first_linked_at_utc,
                    ),
                ).fetchone()
                if row is None:
                    raise RodexSessionError(
                        "statistics source insertion returned no identity"
                    )
                source_ids[thread_halves] = int(row[0])
                continue
            stored_metadata = (
                None if existing[3] is None else int(existing[3]),
                None if existing[4] is None else str(existing[4]),
                None if existing[5] is None else str(existing[5]),
                None if existing[6] is None else int(existing[6]),
                None if existing[7] is None else str(existing[7]),
            )
            if stored_metadata != expected_metadata:
                raise RodexSessionStatisticsConflictError(
                    "statistics source hierarchy changed during calculation"
                )
        turn_sources = {
            split_codex_thread_id_into_signed_bigints(item.codex_thread_id)
            for item in turns
        }
        if not turn_sources.issubset(observed):
            raise RodexSessionStatisticsConflictError(
                "turn statistics include a source outside the analyzed snapshot"
            )

        connection.execute(
            f"DELETE FROM {RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        )
        connection.execute(
            f"UPDATE {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
            "SET included_statistics_publication_sequence = NULL "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        )
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TABLE} "
            "(rodex_sessions_id, statistics_publication_sequence, "
            "statistics_projection_schema_version, calculated_at_utc, "
            f"coverage_state, {SESSION_STATISTICS_SCALARS.columns_sql}) "
            f"VALUES (?, ?, ?, ?, ?, {SESSION_STATISTICS_SCALARS.placeholders_sql}) "
            "ON CONFLICT(rodex_sessions_id) DO UPDATE SET "
            "statistics_publication_sequence = "
            "excluded.statistics_publication_sequence, "
            "statistics_projection_schema_version = "
            "excluded.statistics_projection_schema_version, "
            "calculated_at_utc = excluded.calculated_at_utc, "
            "coverage_state = excluded.coverage_state, "
            f"{SESSION_STATISTICS_SCALARS.excluded_updates_sql}",
            (
                session_id,
                new_publication_sequence,
                schema_version,
                calculated,
                coverage,
                *SESSION_STATISTICS_SCALARS.write_values(statistics_projection),
            ),
        )
        for table in (
            RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE,
            RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE,
            RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE,
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE rodex_sessions_id = ?", (session_id,)
            )
        connection.executemany(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE} "
            "(rodex_sessions_id, included_statistics_publication_sequence, "
            "distribution_kind, "
            "observation_count, total, median, p75, p90, p95, maximum) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    session_id,
                    new_publication_sequence,
                    item.distribution_kind,
                    item.observation_count,
                    item.total,
                    item.median,
                    item.p75,
                    item.p90,
                    item.p95,
                    item.maximum,
                )
                for item in statistics_projection.distributions
            ),
        )
        connection.executemany(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE} "
            "(rodex_sessions_id, included_statistics_publication_sequence, count_kind, "
            "count_name, occurrence_count) VALUES (?, ?, ?, ?, ?)",
            (
                (
                    session_id,
                    new_publication_sequence,
                    item.count_kind,
                    item.count_name,
                    item.occurrence_count,
                )
                for item in statistics_projection.named_counts
                if item.count_kind not in _DERIVED_SESSION_NAMED_COUNT_KINDS
            ),
        )
        connection.executemany(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE} "
            "(rodex_sessions_id, included_statistics_publication_sequence, "
            "limit_ordinal, "
            "limitation) VALUES (?, ?, ?, ?)",
            (
                (session_id, new_publication_sequence, ordinal, limitation)
                for ordinal, limitation in enumerate(statistics_projection.audit_limits)
            ),
        )
        for item in observations:
            cursor = connection.execute(
                f"UPDATE {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} SET "
                "rollout_file_path = ?, analyzed_size_bytes = ?, "
                "analyzed_mtime_ns = ?, analyzed_prefix_sha256 = ?, "
                "verified_at_utc = ?, included_statistics_publication_sequence = ? "
                "WHERE rodex_sessions_id = ? AND codex_thread_id_signed_bigint_1 = ? "
                "AND codex_thread_id_signed_bigint_2 = ?",
                (
                    str(item.rollout_file_path),
                    item.analyzed_size_bytes,
                    item.analyzed_mtime_ns,
                    item.analyzed_prefix_sha256,
                    item.verified_at_utc,
                    new_publication_sequence,
                    session_id,
                    *split_codex_thread_id_into_signed_bigints(item.codex_thread_id),
                ),
            )
            if cursor.rowcount != 1:
                raise RodexSessionStatisticsConflictError(
                    "registered statistics source changed during publication"
                )
        connection.execute(
            f"DELETE FROM {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        )
        model_name_ids: dict[str, int] = {}
        reasoning_effort_name_ids: dict[str, int] = {}
        turn_row_ids: dict[tuple[CodexThreadId, str], int] = {}
        for item in turns:
            source_halves = split_codex_thread_id_into_signed_bigints(item.codex_thread_id)
            source_id = source_ids[source_halves]
            turn_hash = _turn_id_sha256_signed_bigints(item.codex_turn_id)
            model_names_id = _lookup_or_insert_cached_name_id(
                connection,
                model_name_ids,
                MODEL_NAMES_TABLE,
                "name_of_the_model",
                item.model,
            )
            reasoning_effort_names_id = _lookup_or_insert_cached_name_id(
                connection,
                reasoning_effort_name_ids,
                REASONING_EFFORT_NAMES_TABLE,
                "name_of_the_reasoning_effort",
                item.reasoning_effort,
            )
            existing = connection.execute(
                f"SELECT id, codex_turn_id FROM "
                f"{RODEX_SESSIONS_STATISTICS_TURNS_TABLE} "
                "WHERE rodex_sessions_statistics_sources_id = ? "
                "AND codex_turn_id_sha256_int_1 = ? "
                "AND codex_turn_id_sha256_int_2 = ? "
                "AND codex_turn_id_sha256_int_3 = ? "
                "AND codex_turn_id_sha256_int_4 = ?",
                (source_id, *turn_hash),
            ).fetchone()
            if existing is not None and str(existing[1]) != item.codex_turn_id:
                raise RodexSessionStatisticsConflictError(
                    "turn ID digest collision during statistics publication"
                )
            row = connection.execute(
                f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} "
                "(rodex_sessions_id, rodex_sessions_statistics_sources_id, "
                "codex_turn_id_sha256_int_1, codex_turn_id_sha256_int_2, "
                "codex_turn_id_sha256_int_3, codex_turn_id_sha256_int_4, "
                "codex_turn_id, included_statistics_publication_sequence, "
                "started_at_utc, "
                "terminal_at_utc, outcome, model_names_id, "
                "reasoning_effort_names_id, "
                f"{TURN_STATISTICS_SCALARS.columns_sql}) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                f"{TURN_STATISTICS_SCALARS.placeholders_sql}) "
                "ON CONFLICT(rodex_sessions_statistics_sources_id, "
                "codex_turn_id_sha256_int_1, codex_turn_id_sha256_int_2, "
                "codex_turn_id_sha256_int_3, codex_turn_id_sha256_int_4) "
                "DO UPDATE SET included_statistics_publication_sequence = "
                "excluded.included_statistics_publication_sequence, "
                "started_at_utc = excluded.started_at_utc, "
                "terminal_at_utc = excluded.terminal_at_utc, "
                "outcome = excluded.outcome, "
                "model_names_id = excluded.model_names_id, "
                "reasoning_effort_names_id = excluded.reasoning_effort_names_id, "
                f"{TURN_STATISTICS_SCALARS.excluded_updates_sql} "
                "RETURNING id",
                (
                    session_id,
                    source_id,
                    *turn_hash,
                    item.codex_turn_id,
                    new_publication_sequence,
                    item.started_at_utc,
                    item.terminal_at_utc,
                    item.outcome,
                    model_names_id,
                    reasoning_effort_names_id,
                    *TURN_STATISTICS_SCALARS.write_values(item),
                ),
            ).fetchone()
            if row is None:
                raise RodexSessionError("turn statistics upsert returned no identity")
            turn_row_id = int(row[0])
            turn_row_ids[(item.codex_thread_id, item.codex_turn_id)] = turn_row_id
            connection.executemany(
                f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
                "(rodex_sessions_id, rodex_sessions_statistics_turns_id, "
                "included_statistics_publication_sequence, count_kind, count_name, "
                "occurrence_count) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        session_id,
                        turn_row_id,
                        new_publication_sequence,
                        count.count_kind,
                        count.count_name,
                        count.occurrence_count,
                    )
                    for count in item.named_counts
                    if count.count_kind != "collaboration_tool"
                ),
            )
        for subagent_source in observations:
            parent_thread_id = subagent_source.parent_codex_thread_id
            if parent_thread_id is None:
                continue
            spawning_codex_turn_id = subagent_source.spawning_codex_turn_id
            assert spawning_codex_turn_id is not None
            spawning_turn_row_id = turn_row_ids.get(
                (parent_thread_id, spawning_codex_turn_id)
            )
            if spawning_turn_row_id is None:
                raise RodexSessionStatisticsConflictError(
                    "sub-agent spawning turn disappeared during publication"
                )
            subagent_source_id = source_ids[
                split_codex_thread_id_into_signed_bigints(
                    subagent_source.codex_thread_id
                )
            ]
            connection.execute(
                f"INSERT INTO {RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE} "
                "(rodex_sessions_id, "
                "subagent_rodex_sessions_statistics_sources_id, "
                "parent_rodex_sessions_statistics_sources_id, "
                "spawning_rodex_sessions_statistics_turns_id, "
                "included_statistics_publication_sequence) VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    subagent_source_id,
                    source_ids[
                        split_codex_thread_id_into_signed_bigints(parent_thread_id)
                    ],
                    spawning_turn_row_id,
                    new_publication_sequence,
                ),
            )
        connection.execute(
            f"DELETE FROM {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} "
            "WHERE rodex_sessions_id = ? "
            "AND included_statistics_publication_sequence != ?",
            (session_id, new_publication_sequence),
        )
        _upsert_statistics_worker(
            connection,
            session_id,
            worker_state="up_to_date",
            diagnostic_code=None,
            last_attempted_at_utc=calculated,
            consecutive_failures=0,
            next_retry_at_utc=None,
        )
    published = lookup_rodex_session_statistics(session_id, path)
    if published is None:
        raise RodexSessionError(f"Rodex statistics disappeared: {session_id}")
    return published


def record_rodex_session_statistics_worker_health(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    expected_current_codex_session_id: CodexSessionId | str,
    worker_state: str,
    diagnostic_code: str | None,
    last_attempted_at_utc: str,
    consecutive_failures: int,
    next_retry_at_utc: str | None = None,
) -> RodexSessionStatisticsWorker:
    """Update only fail-open worker health, preserving all last-good statistics."""
    _validate_session_id(session_id)
    expected_halves = split_codex_session_id_into_signed_bigints(
        expected_current_codex_session_id
    )
    state = _normalise_required_text(worker_state, "worker_state")
    if state not in STATISTICS_WORKER_STATES:
        raise ValueError(f"unsupported statistics worker state: {state}")
    diagnostic = (
        None
        if diagnostic_code is None
        else _normalise_required_text(diagnostic_code, "diagnostic_code")
    )
    if diagnostic is not None and (
        len(diagnostic) > 64
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in diagnostic
        )
    ):
        raise ValueError(
            "diagnostic_code must contain 1-64 lowercase ASCII letters, digits, "
            "or underscores"
        )
    attempted = _normalise_utc_timestamp_text(last_attempted_at_utc)
    if (
        not isinstance(consecutive_failures, int)
        or isinstance(consecutive_failures, bool)
        or consecutive_failures < 0
    ):
        raise ValueError("consecutive_failures must be a non-negative integer")
    next_retry = (
        None
        if next_retry_at_utc is None
        else _normalise_utc_timestamp_text(next_retry_at_utc)
    )
    if state == "up_to_date" and (
        diagnostic is not None or consecutive_failures != 0 or next_retry is not None
    ):
        raise ValueError(
            "up_to_date worker health cannot include diagnostics, failures, or retry"
        )
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        identity_row = connection.execute(
            f"SELECT codex_session_id_signed_bigint_1, codex_session_id_signed_bigint_2 "
            f"FROM {RODEX_SESSIONS_TABLE} WHERE id = ?",
            (session_id,),
        ).fetchone()
        if identity_row is None:
            raise RodexSessionError(f"Rodex session does not exist: {session_id}")
        if (int(identity_row[0]), int(identity_row[1])) != expected_halves:
            raise RodexSessionStatisticsConflictError(
                "current Codex session ID changed before worker health publication"
            )
        _upsert_statistics_worker(
            connection,
            session_id,
            worker_state=state,
            diagnostic_code=diagnostic,
            last_attempted_at_utc=attempted,
            consecutive_failures=consecutive_failures,
            next_retry_at_utc=next_retry,
        )
        row = _select_statistics_worker(connection, session_id)
    if row is None:
        raise RodexSessionError(f"Rodex statistics worker disappeared: {session_id}")
    return _statistics_worker_from_row(row)


def read_rodex_session_statistics(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionStatisticsView:
    """Read last-good statistics, worker health, and sources in one transaction."""
    _validate_session_id(session_id)
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        statistics_row = _select_statistics(connection, session_id)
        distribution_rows = _select_statistics_distributions(connection, session_id)
        named_count_rows = [
            *_select_statistics_named_counts(connection, session_id),
            *_select_statistics_turn_lookup_counts(connection, session_id),
        ]
        audit_limit_rows = _select_statistics_audit_limits(connection, session_id)
        worker_row = _select_statistics_worker(connection, session_id)
        source_rows = _select_statistics_sources(connection, session_id)
    verified_subagent_count = sum(row[10] is not None for row in source_rows)
    return RodexSessionStatisticsView(
        statistics=(
            None
            if statistics_row is None
            else _session_statistics_from_rows(
                statistics_row,
                distribution_rows,
                named_count_rows,
                audit_limit_rows,
                verified_subagent_count,
            )
        ),
        worker=(None if worker_row is None else _statistics_worker_from_row(worker_row)),
        sources=tuple(_statistics_source_from_row(row) for row in source_rows),
    )


def read_rodex_session_turn_statistics(
    session_id: int,
    codex_turn_id: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    codex_thread_id: CodexThreadId | str | None = None,
) -> RodexSessionTurnStatisticsView:
    """Read one exact turn and its parent freshness in one transaction."""
    _validate_session_id(session_id)
    turn_id = _normalise_required_text(codex_turn_id, "codex_turn_id")
    turn_hash = _turn_id_sha256_signed_bigints(turn_id)
    source_halves = (
        None
        if codex_thread_id is None
        else split_codex_thread_id_into_signed_bigints(codex_thread_id)
    )
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        statistics_row = _select_statistics(connection, session_id)
        distribution_rows = _select_statistics_distributions(connection, session_id)
        named_count_rows = [
            *_select_statistics_named_counts(connection, session_id),
            *_select_statistics_turn_lookup_counts(connection, session_id),
        ]
        audit_limit_rows = _select_statistics_audit_limits(connection, session_id)
        worker_row = _select_statistics_worker(connection, session_id)
        source_rows = _select_statistics_sources(connection, session_id)
        turn_scalar_columns = ", ".join(
            f"turns.{column}" for column in TURN_STATISTICS_SCALARS.columns
        )
        query = (
            f"SELECT turns.id, turns.rodex_sessions_id, "
            "turns.rodex_sessions_statistics_sources_id, "
            "sources.codex_thread_id_signed_bigint_1, "
            "sources.codex_thread_id_signed_bigint_2, turns.codex_turn_id, "
            "turns.included_statistics_publication_sequence, turns.started_at_utc, "
            "turns.terminal_at_utc, turns.outcome, "
            "models.name_of_the_model, "
            "efforts.name_of_the_reasoning_effort, "
            f"(SELECT COUNT(*) FROM {RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE} "
            "AS spawns WHERE spawns.spawning_rodex_sessions_statistics_turns_id = "
            "turns.id), "
            f"{turn_scalar_columns} "
            f"FROM {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} AS turns "
            f"JOIN {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} AS sources "
            "ON sources.id = turns.rodex_sessions_statistics_sources_id "
            f"LEFT JOIN {MODEL_NAMES_TABLE} AS models "
            "ON models.id = turns.model_names_id "
            f"LEFT JOIN {REASONING_EFFORT_NAMES_TABLE} AS efforts "
            "ON efforts.id = turns.reasoning_effort_names_id "
            "WHERE turns.rodex_sessions_id = ? "
            "AND turns.codex_turn_id_sha256_int_1 = ? "
            "AND turns.codex_turn_id_sha256_int_2 = ? "
            "AND turns.codex_turn_id_sha256_int_3 = ? "
            "AND turns.codex_turn_id_sha256_int_4 = ? "
            "AND turns.codex_turn_id = ?"
        )
        parameters: tuple[object, ...] = (session_id, *turn_hash, turn_id)
        if source_halves is not None:
            query += (
                " AND sources.codex_thread_id_signed_bigint_1 = ? "
                "AND sources.codex_thread_id_signed_bigint_2 = ?"
            )
            parameters += source_halves
        turn_rows = connection.execute(query + " ORDER BY turns.id", parameters).fetchall()
        turn_named_count_rows = (
            []
            if len(turn_rows) != 1
            else _select_turn_statistics_named_counts(connection, int(turn_rows[0][0]))
        )
    if len(turn_rows) > 1:
        raise RodexSessionTurnStatisticsAmbiguousError(
            "turn ID exists in multiple Codex threads; qualify it with a thread ID"
        )
    verified_subagent_count = sum(row[10] is not None for row in source_rows)
    return RodexSessionTurnStatisticsView(
        statistics=(
            None
            if statistics_row is None
            else _session_statistics_from_rows(
                statistics_row,
                distribution_rows,
                named_count_rows,
                audit_limit_rows,
                verified_subagent_count,
            )
        ),
        worker=(None if worker_row is None else _statistics_worker_from_row(worker_row)),
        sources=tuple(_statistics_source_from_row(row) for row in source_rows),
        turn=(
            None
            if not turn_rows
            else _turn_statistics_from_rows(turn_rows[0], turn_named_count_rows)
        ),
    )


def lookup_rodex_session_statistics(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionStatistics | None:
    """Return the latest successful aggregate-only statistics projection."""
    return read_rodex_session_statistics(session_id, database_path).statistics


def list_rodex_session_statistics_sources(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> tuple[RodexSessionStatisticsSource, ...]:
    """List every Codex thread source registered to one Rodex statistics lineage."""
    return read_rodex_session_statistics(session_id, database_path).sources


def read_rodex_session_statistics_source_summaries(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    expected_statistics_publication_sequence: int,
) -> tuple[RodexSessionStatisticsSourceSummary, ...]:
    """Group current turn facts by their existing source-row foreign key."""
    _validate_session_id(session_id)
    _validate_positive_id(
        expected_statistics_publication_sequence,
        "expected_statistics_publication_sequence",
    )
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        publication_row = connection.execute(
            f"SELECT statistics_publication_sequence "
            f"FROM {RODEX_SESSIONS_STATISTICS_TABLE} WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
        publication_sequence = None if publication_row is None else int(publication_row[0])
        if publication_sequence != expected_statistics_publication_sequence:
            raise RodexSessionStatisticsConflictError(
                "statistics publication changed before source-summary read"
            )
        source_rows = _select_statistics_sources(connection, session_id)
        aggregate_rows = connection.execute(
            f"SELECT sources.id, COUNT(turns.id), "
            "COALESCE(SUM(turns.outcome = 'completed'), 0), "
            "COALESCE(SUM(turns.outcome = 'aborted'), 0), "
            "COALESCE(SUM(turns.outcome = 'open'), 0), "
            "MIN(turns.started_at_utc), MAX(turns.terminal_at_utc), "
            "COALESCE(SUM(turns.input_tokens), 0), "
            "COALESCE(SUM(turns.cached_input_tokens), 0), "
            "COALESCE(SUM(turns.cache_write_input_tokens), 0), "
            "COALESCE(SUM(turns.output_tokens), 0), "
            "COALESCE(SUM(turns.reasoning_output_tokens), 0), "
            "COALESCE(SUM(turns.total_tokens), 0), "
            "COALESCE(SUM(turns.commands_executed_count), 0), "
            "COALESCE(SUM(turns.model_tool_requests_count), 0), "
            "COALESCE(SUM(turns.file_change_operations_count), 0), "
            "COALESCE(SUM(turns.file_change_occurrences_count), 0), "
            "COALESCE(SUM(turns.web_operations_count), 0), "
            "COALESCE(SUM(turns.web_queries_count), 0), "
            "COALESCE(SUM(turns.web_result_records_count), 0), "
            "COALESCE(SUM(turns.compactions_count), 0) "
            f"FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} AS sources "
            f"LEFT JOIN {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} AS turns "
            "ON turns.rodex_sessions_statistics_sources_id = sources.id "
            "WHERE sources.rodex_sessions_id = ? GROUP BY sources.id ORDER BY sources.id",
            (session_id,),
        ).fetchall()
        count_rows = connection.execute(
            f"SELECT turns.rodex_sessions_statistics_sources_id, counts.count_kind, "
            "counts.count_name, SUM(counts.occurrence_count) "
            f"FROM {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} AS counts "
            f"JOIN {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} AS turns "
            "ON turns.id = counts.rodex_sessions_statistics_turns_id "
            "WHERE turns.rodex_sessions_id = ? "
            "GROUP BY turns.rodex_sessions_statistics_sources_id, "
            "counts.count_kind, counts.count_name "
            "ORDER BY turns.rodex_sessions_statistics_sources_id, "
            "counts.count_kind, counts.count_name",
            (session_id,),
        ).fetchall()
        spawned_subagent_rows = connection.execute(
            f"SELECT child.parent_rodex_sessions_statistics_sources_id, COUNT(*) "
            f"FROM {RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE} AS spawns "
            f"JOIN {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} AS child "
            "ON child.id = spawns.subagent_rodex_sessions_statistics_sources_id "
            "WHERE spawns.rodex_sessions_id = ? "
            "GROUP BY child.parent_rodex_sessions_statistics_sources_id",
            (session_id,),
        ).fetchall()
    sources = {
        source.id: source
        for source in (_statistics_source_from_row(row) for row in source_rows)
    }
    counts_by_source: dict[int, list[tuple[object, ...]]] = {}
    for row in count_rows:
        counts_by_source.setdefault(int(row[0]), []).append(row[1:])
    summaries = tuple(
        RodexSessionStatisticsSourceSummary(
            source=sources[int(row[0])],
            turns_started_count=int(row[1]),
            turns_completed_count=int(row[2]),
            turns_aborted_count=int(row[3]),
            turns_open_count=int(row[4]),
            first_turn_started_at_utc=None if row[5] is None else str(row[5]),
            last_turn_terminal_at_utc=None if row[6] is None else str(row[6]),
            input_tokens=int(row[7]),
            cached_input_tokens=int(row[8]),
            cache_write_input_tokens=int(row[9]),
            output_tokens=int(row[10]),
            reasoning_output_tokens=int(row[11]),
            total_tokens=int(row[12]),
            commands_executed_count=int(row[13]),
            model_tool_requests_count=int(row[14]),
            file_change_operations_count=int(row[15]),
            file_change_occurrences_count=int(row[16]),
            web_operations_count=int(row[17]),
            web_queries_count=int(row[18]),
            web_result_records_count=int(row[19]),
            collaboration_operations_count=0,
            collaboration_agents_started_count=0,
            compactions_count=int(row[20]),
            named_counts=_named_counts_from_rows(counts_by_source.get(int(row[0]), ())),
        )
        for row in aggregate_rows
    )
    child_counts = {
        int(parent_source_id): int(child_count)
        for parent_source_id, child_count in spawned_subagent_rows
    }
    return tuple(
        replace(
            summary,
            collaboration_operations_count=sum(
                count.occurrence_count
                for count in summary.named_counts
                if count.count_kind == "model_tool"
                and count.count_name in COLLABORATION_MODEL_TOOL_NAMES
            ),
            collaboration_agents_started_count=child_counts.get(summary.source.id, 0),
        )
        for summary in summaries
    )


def register_codex_root_statistics_source_in_transaction(
    connection: sqlite3.Connection,
    session_id: int,
    codex_session_id: CodexSessionId,
    first_linked_at_utc: str,
) -> None:
    """Register one root Codex thread inside its owning lifecycle transaction."""
    stored_codex_thread_id = split_codex_thread_id_into_signed_bigints(codex_session_id)
    row = connection.execute(
        f"SELECT rodex_sessions_id FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
        "WHERE codex_thread_id_signed_bigint_1 = ? "
        "AND codex_thread_id_signed_bigint_2 = ?",
        stored_codex_thread_id,
    ).fetchone()
    if row is None:
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
            "(rodex_sessions_id, codex_thread_id_signed_bigint_1, "
            "codex_thread_id_signed_bigint_2, first_linked_at_utc) "
            "VALUES (?, ?, ?, ?)",
            (session_id, *stored_codex_thread_id, first_linked_at_utc),
        )
        return
    if int(row[0]) != session_id:
        raise RodexSessionError(
            "Codex history already belongs to another Rodex statistics lineage: "
            f"{codex_session_id}"
        )


def _turn_id_sha256_signed_bigints(turn_id: str) -> tuple[int, int, int, int]:
    normalized = _normalise_required_text(turn_id, "codex_turn_id")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    pieces = tuple(
        int.from_bytes(digest[offset : offset + 8], "big", signed=True)
        for offset in range(0, 32, 8)
    )
    return pieces[0], pieces[1], pieces[2], pieces[3]


def _lookup_or_insert_cached_name_id(
    connection: sqlite3.Connection,
    cache: dict[str, int],
    table_name: str,
    name_column: str,
    name: str | None,
) -> int | None:
    """Resolve one append-only lookup name once within a publication transaction."""
    if name is None:
        return None
    cached = cache.get(name)
    if cached is not None:
        return cached
    lookup_id = select_or_insert_lookup_id(
        connection,
        table_name,
        {name_column: name},
    )
    cache[name] = lookup_id
    return lookup_id


def _validate_source_observation(
    observation: RodexSessionStatisticsSourceObservation,
) -> RodexSessionStatisticsSourceObservation:
    if not isinstance(observation, RodexSessionStatisticsSourceObservation):
        raise TypeError(
            "analyzed_sources must contain RodexSessionStatisticsSourceObservation values"
        )
    codex_thread_id = parse_codex_thread_id(observation.codex_thread_id)
    source_kind = _normalise_required_text(observation.source_kind, "source_kind")
    if source_kind not in {"root", "subagent"}:
        raise ValueError(f"unsupported statistics source kind: {source_kind}")
    if (
        not isinstance(observation.thread_depth, int)
        or isinstance(observation.thread_depth, bool)
        or observation.thread_depth < 0
    ):
        raise ValueError("thread_depth must be a non-negative integer")
    parent_codex_thread_id = (
        None
        if observation.parent_codex_thread_id is None
        else parse_codex_thread_id(observation.parent_codex_thread_id)
    )
    agent_path = (
        None
        if observation.agent_path is None
        else _normalise_required_text(observation.agent_path, "agent_path")
    )
    agent_nickname = (
        None
        if observation.agent_nickname is None
        else _normalise_required_text(observation.agent_nickname, "agent_nickname")
    )
    cutoff = observation.subagent_history_start_ordinal
    if cutoff is not None and (
        not isinstance(cutoff, int) or isinstance(cutoff, bool) or cutoff < 0
    ):
        raise ValueError("subagent_history_start_ordinal must be non-negative")
    spawning_codex_turn_id = (
        None
        if observation.spawning_codex_turn_id is None
        else _normalise_required_text(
            observation.spawning_codex_turn_id,
            "spawning_codex_turn_id",
        )
    )
    if source_kind == "root":
        if (
            any(
                value is not None
                for value in (
                    parent_codex_thread_id,
                    agent_path,
                    agent_nickname,
                    cutoff,
                    spawning_codex_turn_id,
                )
            )
            or observation.thread_depth != 0
        ):
            raise ValueError("root statistics source has sub-agent metadata")
    elif (
        parent_codex_thread_id is None
        or observation.thread_depth == 0
        or agent_path is None
        or cutoff is None
        or spawning_codex_turn_id is None
    ):
        raise ValueError("sub-agent statistics source metadata is incomplete")
    source_path = observation.rollout_file_path.expanduser().resolve()
    if not source_path.is_absolute():
        raise ValueError("rollout_file_path must resolve to an absolute path")
    for value, field_name in (
        (observation.analyzed_size_bytes, "analyzed_size_bytes"),
        (observation.analyzed_mtime_ns, "analyzed_mtime_ns"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
    digest = _normalise_required_text(
        observation.analyzed_prefix_sha256, "analyzed_prefix_sha256"
    ).lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("analyzed_prefix_sha256 must be 64 lowercase hexadecimal digits")
    return RodexSessionStatisticsSourceObservation(
        codex_thread_id=codex_thread_id,
        source_kind=source_kind,
        parent_codex_thread_id=parent_codex_thread_id,
        thread_depth=observation.thread_depth,
        agent_path=agent_path,
        agent_nickname=agent_nickname,
        subagent_history_start_ordinal=cutoff,
        spawning_codex_turn_id=spawning_codex_turn_id,
        first_linked_at_utc=_normalise_utc_timestamp_text(observation.first_linked_at_utc),
        rollout_file_path=source_path,
        analyzed_size_bytes=observation.analyzed_size_bytes,
        analyzed_mtime_ns=observation.analyzed_mtime_ns,
        analyzed_prefix_sha256=digest,
        verified_at_utc=_normalise_utc_timestamp_text(observation.verified_at_utc),
    )


def _validate_authoritative_collaboration_projection(
    projection: SessionStatisticsProjection,
    observations: Sequence[RodexSessionStatisticsSourceObservation],
) -> None:
    expected_session_tools = _canonical_collaboration_count_map(
        projection.named_counts
    )
    if _named_count_map(projection.named_counts, "collaboration_tool") != (
        expected_session_tools
    ):
        raise ValueError(
            "session collaboration tools must derive from canonical model tools"
        )
    if projection.collaboration_operations_count != sum(
        expected_session_tools.values()
    ):
        raise ValueError(
            "session collaboration operations must derive from canonical model tools"
        )
    verified_subagents = tuple(
        item for item in observations if item.parent_codex_thread_id is not None
    )
    if projection.collaboration_agents_started_count != len(verified_subagents):
        raise ValueError(
            "session agents started must equal verified sub-agent sources"
        )

    expected_agents_by_turn: dict[tuple[CodexThreadId, str], int] = {}
    for subagent in verified_subagents:
        assert subagent.parent_codex_thread_id is not None
        assert subagent.spawning_codex_turn_id is not None
        spawning_turn_key = (
            subagent.parent_codex_thread_id,
            subagent.spawning_codex_turn_id,
        )
        expected_agents_by_turn[spawning_turn_key] = (
            expected_agents_by_turn.get(spawning_turn_key, 0) + 1
        )

    observed_session_tools: dict[str, int] = {}
    observed_agent_count = 0
    for turn in projection.turn_statistics:
        expected_turn_tools = _canonical_collaboration_count_map(turn.named_counts)
        if _named_count_map(turn.named_counts, "collaboration_tool") != (
            expected_turn_tools
        ):
            raise ValueError(
                "turn collaboration tools must derive from canonical model tools"
            )
        if turn.collaboration_operations_count != sum(expected_turn_tools.values()):
            raise ValueError(
                "turn collaboration operations must derive from canonical model tools"
            )
        expected_agents_started = expected_agents_by_turn.get(
            (turn.codex_thread_id, turn.codex_turn_id), 0
        )
        if turn.collaboration_agents_started_count != expected_agents_started:
            raise ValueError(
                "turn agents started must equal verified sub-agent spawn relations"
            )
        observed_agent_count += expected_agents_started
        for tool_name, occurrence_count in expected_turn_tools.items():
            observed_session_tools[tool_name] = (
                observed_session_tools.get(tool_name, 0) + occurrence_count
            )
    if observed_session_tools != expected_session_tools:
        raise ValueError(
            "session collaboration tools must equal exact-turn collaboration tools"
        )
    if observed_agent_count != len(verified_subagents):
        raise ValueError(
            "every verified sub-agent must belong to one published spawning turn"
        )


def _canonical_collaboration_count_map(
    named_counts: Sequence[StatisticsNamedCount],
) -> dict[str, int]:
    return {
        item.count_name: item.occurrence_count
        for item in named_counts
        if item.count_kind == "model_tool"
        and item.count_name in COLLABORATION_MODEL_TOOL_NAMES
    }


def _named_count_map(
    named_counts: Sequence[StatisticsNamedCount],
    count_kind: str,
) -> dict[str, int]:
    return {
        item.count_name: item.occurrence_count
        for item in named_counts
        if item.count_kind == count_kind
    }


def _append_collaboration_view_from_model_tools(
    named_counts: Sequence[StatisticsNamedCount],
) -> tuple[StatisticsNamedCount, ...]:
    stored_counts = tuple(
        item for item in named_counts if item.count_kind != "collaboration_tool"
    )
    collaboration_counts = tuple(
        StatisticsNamedCount(
            count_kind="collaboration_tool",
            count_name=tool_name,
            occurrence_count=occurrence_count,
        )
        for tool_name, occurrence_count in _canonical_collaboration_count_map(
            stored_counts
        ).items()
    )
    return stored_counts + collaboration_counts


def _select_statistics(
    connection: sqlite3.Connection, session_id: int
) -> tuple[object, ...] | None:
    return connection.execute(
        f"SELECT id, rodex_sessions_id, statistics_publication_sequence, "
        "statistics_projection_schema_version, calculated_at_utc, coverage_state, "
        f"{SESSION_STATISTICS_SCALARS.columns_sql} "
        f"FROM {RODEX_SESSIONS_STATISTICS_TABLE} "
        "WHERE rodex_sessions_id = ?",
        (session_id,),
    ).fetchone()


def _select_statistics_distributions(
    connection: sqlite3.Connection, session_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT distribution_kind, observation_count, total, median, p75, p90, "
        f"p95, maximum FROM {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE} "
        "WHERE rodex_sessions_id = ? ORDER BY distribution_kind",
        (session_id,),
    ).fetchall()


def _select_statistics_named_counts(
    connection: sqlite3.Connection, session_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT count_kind, count_name, occurrence_count "
        f"FROM {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE} "
        "WHERE rodex_sessions_id = ? ORDER BY count_kind, count_name",
        (session_id,),
    ).fetchall()


def _select_statistics_turn_lookup_counts(
    connection: sqlite3.Connection,
    session_id: int,
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for count_kind, table_name, foreign_key, name_column in (
        ("model", MODEL_NAMES_TABLE, "model_names_id", "name_of_the_model"),
        (
            "reasoning_effort",
            REASONING_EFFORT_NAMES_TABLE,
            "reasoning_effort_names_id",
            "name_of_the_reasoning_effort",
        ),
    ):
        rows.extend(
            connection.execute(
                f"SELECT ?, names.{name_column}, COUNT(*) "
                f"FROM {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} AS turns "
                f"JOIN {table_name} AS names ON names.id = turns.{foreign_key} "
                "WHERE turns.rodex_sessions_id = ? "
                f"GROUP BY names.id, names.{name_column} "
                f"ORDER BY names.{name_column}",
                (count_kind, session_id),
            ).fetchall()
        )
    return rows


def _select_statistics_audit_limits(
    connection: sqlite3.Connection, session_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT limit_ordinal, limitation "
        f"FROM {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE} "
        "WHERE rodex_sessions_id = ? ORDER BY limit_ordinal",
        (session_id,),
    ).fetchall()


def _select_turn_statistics_named_counts(
    connection: sqlite3.Connection, turn_row_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT count_kind, count_name, occurrence_count "
        f"FROM {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
        "WHERE rodex_sessions_statistics_turns_id = ? "
        "ORDER BY count_kind, count_name",
        (turn_row_id,),
    ).fetchall()


def _select_statistics_worker(
    connection: sqlite3.Connection, session_id: int
) -> tuple[object, ...] | None:
    return connection.execute(
        f"SELECT id, rodex_sessions_id, worker_state, diagnostic_code, "
        "last_attempted_at_utc, consecutive_failures, next_retry_at_utc "
        f"FROM {RODEX_SESSIONS_STATISTICS_WORKERS_TABLE} "
        "WHERE rodex_sessions_id = ?",
        (session_id,),
    ).fetchone()


def _select_statistics_sources(
    connection: sqlite3.Connection, session_id: int
) -> list[tuple[object, ...]]:
    rows = connection.execute(
        f"WITH RECURSIVE hierarchy(id, thread_depth) AS ("
        f"SELECT id, 0 FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
        "WHERE rodex_sessions_id = ? "
        "AND parent_rodex_sessions_statistics_sources_id IS NULL "
        "UNION ALL "
        f"SELECT child.id, parent.thread_depth + 1 "
        f"FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} AS child "
        "JOIN hierarchy AS parent "
        "ON child.parent_rodex_sessions_statistics_sources_id = parent.id) "
        f"SELECT sources.id, sources.rodex_sessions_id, "
        "sources.codex_thread_id_signed_bigint_1, "
        "sources.codex_thread_id_signed_bigint_2, "
        "CASE WHEN sources.parent_rodex_sessions_statistics_sources_id IS NULL "
        "THEN 'root' ELSE 'subagent' END, "
        "sources.parent_rodex_sessions_statistics_sources_id, hierarchy.thread_depth, "
        "sources.agent_path, sources.agent_nickname, "
        "sources.subagent_history_start_ordinal, spawning_turn.codex_turn_id, "
        "sources.first_linked_at_utc, "
        "sources.rollout_file_path, sources.analyzed_size_bytes, "
        "sources.analyzed_mtime_ns, sources.analyzed_prefix_sha256, "
        "sources.verified_at_utc, "
        "sources.included_statistics_publication_sequence "
        f"FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} AS sources "
        "JOIN hierarchy ON hierarchy.id = sources.id "
        f"LEFT JOIN {RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE} AS spawns "
        "ON spawns.subagent_rodex_sessions_statistics_sources_id = sources.id "
        f"LEFT JOIN {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} AS spawning_turn "
        "ON spawning_turn.id = spawns.spawning_rodex_sessions_statistics_turns_id "
        "ORDER BY sources.id",
        (session_id,),
    ).fetchall()
    expected_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()[0]
    )
    if len(rows) != expected_count:
        raise RodexSessionError("stored statistics source hierarchy is not rooted")
    return rows


def _upsert_statistics_worker(
    connection: sqlite3.Connection,
    session_id: int,
    *,
    worker_state: str,
    diagnostic_code: str | None,
    last_attempted_at_utc: str,
    consecutive_failures: int,
    next_retry_at_utc: str | None,
) -> None:
    connection.execute(
        f"INSERT INTO {RODEX_SESSIONS_STATISTICS_WORKERS_TABLE} "
        "(rodex_sessions_id, worker_state, diagnostic_code, last_attempted_at_utc, "
        "consecutive_failures, next_retry_at_utc) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(rodex_sessions_id) DO UPDATE SET "
        "worker_state = excluded.worker_state, "
        "diagnostic_code = excluded.diagnostic_code, "
        "last_attempted_at_utc = excluded.last_attempted_at_utc, "
        "consecutive_failures = excluded.consecutive_failures, "
        "next_retry_at_utc = excluded.next_retry_at_utc",
        (
            session_id,
            worker_state,
            diagnostic_code,
            last_attempted_at_utc,
            consecutive_failures,
            next_retry_at_utc,
        ),
    )


def _session_statistics_from_rows(
    row: tuple[object, ...],
    distribution_rows: Sequence[tuple[object, ...]],
    named_count_rows: Sequence[tuple[object, ...]],
    audit_limit_rows: Sequence[tuple[object, ...]],
    verified_subagent_count: int,
) -> RodexSessionStatistics:
    scalar_values = SESSION_STATISTICS_SCALARS.read_values(row[6:])
    distributions = tuple(
        StatisticsDistribution(
            distribution_kind=str(item[0]),
            observation_count=int(item[1]),
            total=int(item[2]),
            median=None if item[3] is None else float(item[3]),
            p75=None if item[4] is None else int(item[4]),
            p90=None if item[5] is None else int(item[5]),
            p95=None if item[6] is None else int(item[6]),
            maximum=None if item[7] is None else int(item[7]),
        )
        for item in distribution_rows
    )
    named_counts = _append_collaboration_view_from_model_tools(
        _named_counts_from_rows(named_count_rows)
    )
    expected_ordinals = tuple(range(len(audit_limit_rows)))
    actual_ordinals = tuple(int(item[0]) for item in audit_limit_rows)
    if actual_ordinals != expected_ordinals:
        raise RodexSessionError("stored statistics audit limits are not contiguous")
    projection = SessionStatisticsProjection(
        **scalar_values,
        collaboration_operations_count=sum(
            _canonical_collaboration_count_map(named_counts).values()
        ),
        collaboration_agents_started_count=verified_subagent_count,
        distributions=distributions,
        named_counts=named_counts,
        audit_limits=tuple(str(item[1]) for item in audit_limit_rows),
        turn_statistics=(),
    )
    return RodexSessionStatistics(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        statistics_publication_sequence=int(row[2]),
        statistics_projection_schema_version=str(row[3]),
        calculated_at_utc=str(row[4]),
        coverage_state=str(row[5]),
        projection=projection,
    )


def _statistics_source_from_row(
    row: tuple[object, ...],
) -> RodexSessionStatisticsSource:
    return RodexSessionStatisticsSource(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        codex_thread_id=join_signed_bigints_into_a_codex_thread_id(row[2], row[3]),
        source_kind=str(row[4]),
        parent_rodex_sessions_statistics_sources_id=(
            None if row[5] is None else int(row[5])
        ),
        thread_depth=int(row[6]),
        agent_path=None if row[7] is None else str(row[7]),
        agent_nickname=None if row[8] is None else str(row[8]),
        subagent_history_start_ordinal=None if row[9] is None else int(row[9]),
        spawning_codex_turn_id=None if row[10] is None else str(row[10]),
        first_linked_at_utc=str(row[11]),
        rollout_file_path=None if row[12] is None else str(row[12]),
        analyzed_size_bytes=None if row[13] is None else int(row[13]),
        analyzed_mtime_ns=None if row[14] is None else int(row[14]),
        analyzed_prefix_sha256=None if row[15] is None else str(row[15]),
        verified_at_utc=None if row[16] is None else str(row[16]),
        included_statistics_publication_sequence=(
            None if row[17] is None else int(row[17])
        ),
    )


def _turn_statistics_from_rows(
    row: tuple[object, ...],
    named_count_rows: Sequence[tuple[object, ...]],
) -> RodexSessionTurnStatistics:
    values = TURN_STATISTICS_SCALARS.read_values(row[13:])
    named_counts = _append_collaboration_view_from_model_tools(
        _named_counts_from_rows(named_count_rows)
    )
    projection = TurnStatisticsProjection(
        codex_thread_id=join_signed_bigints_into_a_codex_thread_id(row[3], row[4]),
        codex_turn_id=str(row[5]),
        started_at_utc=None if row[7] is None else str(row[7]),
        terminal_at_utc=None if row[8] is None else str(row[8]),
        outcome=str(row[9]),
        model=None if row[10] is None else str(row[10]),
        reasoning_effort=None if row[11] is None else str(row[11]),
        **values,
        collaboration_operations_count=sum(
            _canonical_collaboration_count_map(named_counts).values()
        ),
        collaboration_agents_started_count=int(row[12]),
        named_counts=named_counts,
    )
    return RodexSessionTurnStatistics(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        rodex_sessions_statistics_sources_id=int(row[2]),
        included_statistics_publication_sequence=int(row[6]),
        projection=projection,
    )


def _named_counts_from_rows(
    rows: Sequence[tuple[object, ...]],
) -> tuple[StatisticsNamedCount, ...]:
    return tuple(
        StatisticsNamedCount(
            count_kind=str(row[0]),
            count_name=str(row[1]),
            occurrence_count=int(row[2]),
        )
        for row in rows
    )


def _statistics_worker_from_row(
    row: tuple[object, ...],
) -> RodexSessionStatisticsWorker:
    return RodexSessionStatisticsWorker(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        worker_state=str(row[2]),
        diagnostic_code=None if row[3] is None else str(row[3]),
        last_attempted_at_utc=str(row[4]),
        consecutive_failures=int(row[5]),
        next_retry_at_utc=None if row[6] is None else str(row[6]),
    )
