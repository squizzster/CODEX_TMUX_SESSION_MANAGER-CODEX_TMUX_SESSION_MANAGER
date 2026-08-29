"""Authoritative relational statistics publication and read pipeline."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace

from rodex_sql import (
    index_re_try_attempt_numbers,
    normalise_rodex_database_path,
    open_rodex_read_transaction,
    open_rodex_transaction,
    select_or_insert_lookup_id,
)

from .agent_trace_contract import (
    RodexAgentTracePublication,
    prepare_agent_trace_publication,
)
from .agent_trace_writer import (
    publish_agent_trace_in_transaction,
)
from .errors import (
    RodexSessionError,
    RodexSessionStatisticsConflictError,
    RodexSessionStatisticsPublicationRaceError,
    RodexSessionTurnStatisticsAmbiguousError,
)
from .execution import (
    RodexSessionCodexThread,
    RodexSessionCodexThreadObservation,
    resolve_codex_thread_identity_in_transaction,
    validate_codex_thread_observation,
)
from .execution import (
    codex_thread_from_row as _codex_thread_from_row,
)
from .execution import (
    select_codex_threads_in_transaction as _select_codex_threads,
)
from .identity import (
    CodexSessionId,
    CodexThreadId,
    RodexAnalyticsIdentityFence,
    join_signed_bigints_into_a_codex_thread_id,
    join_signed_bigints_into_a_codex_turn_id,
    parse_codex_turn_id,
    split_codex_session_id_into_signed_bigints,
    split_codex_thread_id_into_signed_bigints,
    split_codex_turn_id_into_signed_bigints,
)
from .schema import (
    CODEX_THREADS_TABLE,
    MODEL_NAMES_TABLE,
    REASONING_EFFORT_NAMES_TABLE,
    RODEX_REGISTRIES_TABLE,
    RODEX_RUNTIME_INSTANCES_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE,
    RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
    RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE,
    RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE,
    RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE,
    RODEX_SESSIONS_CODEX_THREADS_TABLE,
    RODEX_SESSIONS_CODEX_TURN_STATES_TABLE,
    RODEX_SESSIONS_CODEX_TURNS_TABLE,
    RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE,
    RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE,
    RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE,
    RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE,
    RODEX_SESSIONS_STATISTICS_TABLE,
    RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE,
    RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE,
    RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE,
    RODEX_SESSIONS_TABLE,
    STATISTICS_COVERAGE_STATES,
    STATISTICS_WORKER_STATES,
    require_current_rodex_schema,
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


def _require_analytics_identity_fence(
    row: Sequence[object] | None,
    fence: RodexAnalyticsIdentityFence,
    *,
    operation: str,
) -> None:
    if row is None:
        raise RodexSessionError(f"Rodex session does not exist: {fence.rodex_sessions_id}")
    expected = (
        fence.rodex_registry_id.as_signed_bigint(),
        fence.rodex_session_id.as_signed_bigint(),
        *split_codex_session_id_into_signed_bigints(fence.codex_session_id),
        fence.runtime_id.as_signed_bigint(),
    )
    if tuple(row[:5]) != expected:
        raise RodexSessionStatisticsConflictError(
            f"analytics registry/session/runtime identity changed before {operation}"
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
class RodexSessionTurnStatistics:
    """Latest persisted statistics projection for one exact Codex turn."""

    id: int
    rodex_sessions_id: int
    rodex_sessions_codex_threads_id: int
    turn_public_id: uuid.UUID
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
class RodexSessionAnalyticsWorker:
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
    worker: RodexSessionAnalyticsWorker | None
    sources: tuple[RodexSessionCodexThread, ...]


@dataclass(frozen=True, slots=True)
class RodexAnalyticsStatisticsCheckpoint:
    """Only publication metadata needed by the live analytics worker."""

    statistics_publication_sequence: int
    statistics_projection_schema_version: str


@dataclass(frozen=True, slots=True)
class RodexAnalyticsTraceCheckpoint:
    """Independent agent-trace publication head loaded by the worker."""

    trace_publication_sequence: int
    trace_schema_version: str


@dataclass(frozen=True, slots=True)
class RodexAnalyticsPublishReceipt:
    """Small acknowledgement returned without re-reading the published projection."""

    statistics_id: int
    statistics_publication_sequence: int
    statistics_projection_schema_version: str
    trace_publication_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class RodexAnalyticsCheckpoint:
    """Narrow worker checkpoint loaded without materializing statistics."""

    statistics: RodexAnalyticsStatisticsCheckpoint | None
    worker: RodexSessionAnalyticsWorker | None
    sources: tuple[RodexSessionCodexThread, ...]
    trace: RodexAnalyticsTraceCheckpoint | None = None
    unresolved_activity_targets: tuple[CodexThreadId, ...] = ()


@dataclass(frozen=True, slots=True)
class RodexSessionCodexThreadSummary:
    """SQL-derived additive lifecycle and resource totals for one thread source."""

    source: RodexSessionCodexThread
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
    worker: RodexSessionAnalyticsWorker | None
    sources: tuple[RodexSessionCodexThread, ...]
    turn: RodexSessionTurnStatistics | None


def publish_rodex_session_statistics(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    expected_current_codex_session_id: CodexSessionId | str,
    identity_fence: RodexAnalyticsIdentityFence | None = None,
    based_on_statistics_publication_sequence: int | None,
    statistics_projection_schema_version: str,
    calculated_at_utc: str,
    coverage_state: str,
    statistics_projection: SessionStatisticsProjection,
    analyzed_sources: Sequence[RodexSessionCodexThreadObservation],
    changed_source_thread_ids: frozenset[CodexThreadId] | None = None,
    changed_turn_keys: frozenset[tuple[CodexThreadId, str]] | None = None,
    removed_turn_keys: frozenset[tuple[CodexThreadId, str]] = frozenset(),
    model_name_ids: dict[str, int] | None = None,
    reasoning_effort_name_ids: dict[str, int] | None = None,
    agent_trace_publication: RodexAgentTracePublication | None = None,
) -> RodexAnalyticsPublishReceipt:
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
    statistics_projection = validate_session_statistics_projection(
        statistics_projection,
        complete_turn_statistics=changed_turn_keys is None,
    )
    observations = tuple(
        validate_codex_thread_observation(item) for item in analyzed_sources
    )
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
    if changed_turn_keys is not None and changed_turn_keys != turn_keys:
        raise ValueError("changed_turn_keys must exactly identify the projected turn delta")
    if removed_turn_keys & turn_keys:
        raise ValueError("removed_turn_keys contains a projected turn")
    _validate_authoritative_collaboration_projection(
        statistics_projection,
        observations,
        complete_turn_statistics=changed_turn_keys is None,
    )
    prepared_agent_trace_publication = (
        None
        if agent_trace_publication is None
        else prepare_agent_trace_publication(agent_trace_publication)
    )

    path = normalise_rodex_database_path(database_path)
    with open_rodex_transaction(path) as connection:
        require_current_rodex_schema(connection)
        identity_row = connection.execute(
            f"SELECT registries.rodex_registry_id_signed_bigint, "
            "sessions.rodex_session_id_signed_bigint, "
            "current_ids.codex_thread_public_id_signed_bigint_1, "
            "current_ids.codex_thread_public_id_signed_bigint_2, "
            "runtimes.runtime_id_signed_bigint, "
            "statistics.statistics_publication_sequence "
            f"FROM {RODEX_SESSIONS_TABLE} AS sessions "
            f"JOIN {RODEX_REGISTRIES_TABLE} AS registries ON registries.id = 1 "
            f"JOIN {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} AS current "
            "ON current.rodex_sessions_id = sessions.id "
            f"JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS current_membership "
            "ON current_membership.id = current.rodex_sessions_codex_threads_id "
            f"JOIN {CODEX_THREADS_TABLE} AS current_ids "
            "ON current_ids.id = current_membership.codex_threads_id "
            f"LEFT JOIN {RODEX_RUNTIME_INSTANCES_TABLE} AS runtimes "
            "ON runtimes.rodex_sessions_id = sessions.id "
            f"LEFT JOIN {RODEX_SESSIONS_STATISTICS_TABLE} AS statistics "
            "ON statistics.rodex_sessions_id = sessions.id "
            "WHERE sessions.id = ?",
            (session_id,),
        ).fetchone()
        if identity_row is None:
            raise RodexSessionError(f"Rodex session does not exist: {session_id}")
        if identity_fence is not None:
            _require_analytics_identity_fence(
                identity_row,
                identity_fence,
                operation="statistics publication",
            )
        elif (int(identity_row[2]), int(identity_row[3])) != expected_halves:
            raise RodexSessionStatisticsConflictError(
                "current Codex session ID changed during statistics calculation"
            )
        previous_publication_sequence = (
            None if identity_row[5] is None else int(identity_row[5])
        )
        if previous_publication_sequence != based_on_statistics_publication_sequence:
            raise RodexSessionStatisticsPublicationRaceError(
                "statistics publication sequence changed during calculation"
            )
        new_publication_sequence = (
            1
            if previous_publication_sequence is None
            else previous_publication_sequence + 1
        )
        registered_rows = connection.execute(
            f"WITH RECURSIVE hierarchy(id) AS ("
            "SELECT current.rodex_sessions_codex_threads_id "
            f"FROM {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} AS current "
            "WHERE current.rodex_sessions_id = ? "
            "UNION ALL "
            "SELECT spawns.subagent_rodex_sessions_codex_threads_id "
            f"FROM {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} AS spawns "
            "JOIN hierarchy AS parent ON "
            "spawns.parent_rodex_sessions_codex_threads_id = parent.id) "
            f"SELECT sources.id, ids.codex_thread_public_id_signed_bigint_1, "
            "ids.codex_thread_public_id_signed_bigint_2, "
            "spawns.parent_rodex_sessions_codex_threads_id, "
            "spawns.agent_path, spawns.agent_nickname, "
            "CASE WHEN spawns.history_inheritance_kind = 'clean' THEN 0 "
            "ELSE spawns.inherited_history_start_ordinal END, "
            "spawning_turn.codex_turn_id_signed_bigint_1, "
            "spawning_turn.codex_turn_id_signed_bigint_2, "
            "spawns.history_inheritance_kind "
            f"FROM {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS sources "
            f"JOIN {CODEX_THREADS_TABLE} AS ids ON ids.id = sources.codex_threads_id "
            f"LEFT JOIN {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} AS spawns "
            "ON spawns.subagent_rodex_sessions_codex_threads_id = sources.id "
            f"LEFT JOIN {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS spawning_turn "
            "ON spawning_turn.id = spawns.spawning_rodex_sessions_codex_turns_id "
            "JOIN hierarchy ON hierarchy.id = sources.id "
            "WHERE sources.rodex_sessions_id = ?",
            (session_id, session_id),
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
        source_threads_to_write = (
            frozenset(item.codex_thread_id for item in observations)
            if changed_source_thread_ids is None
            else changed_source_thread_ids
        )
        if not source_threads_to_write.issubset(
            item.codex_thread_id for item in observations
        ):
            raise ValueError("changed_source_thread_ids contains an unobserved source")
        if not previously_registered.issubset(observed):
            raise RodexSessionStatisticsConflictError(
                "statistics omit a registered Codex thread source"
            )
        new_source_threads: set[CodexThreadId] = set()
        for item in sorted(observations, key=lambda observation: observation.thread_depth):
            thread_halves = split_codex_thread_id_into_signed_bigints(item.codex_thread_id)
            if item.source_kind == "subagent" and thread_halves == expected_halves:
                raise RodexSessionStatisticsConflictError(
                    "current Codex thread cannot be published as a subagent"
                )
            parent_source_id = (
                None
                if item.parent_codex_thread_id is None
                else source_ids.get(
                    split_codex_thread_id_into_signed_bigints(item.parent_codex_thread_id)
                )
            )
            if item.source_kind == "subagent" and parent_source_id is None:
                raise RodexSessionStatisticsConflictError(
                    "sub-agent Codex thread has no published parent thread"
                )
            existing = existing_by_thread.get(thread_halves)
            expected_metadata = (
                parent_source_id,
                item.agent_path,
                item.agent_nickname,
                item.subagent_history_start_ordinal,
                item.spawning_codex_turn_id,
                item.history_inheritance_kind,
            )
            if existing is None:
                if item.source_kind != "subagent":
                    raise RodexSessionStatisticsConflictError(
                        "statistics include an unregistered root thread source"
                    )
                codex_threads_id = resolve_codex_thread_identity_in_transaction(
                    connection, item.codex_thread_id
                )
                occupied = connection.execute(
                    f"SELECT rodex_sessions_id FROM {RODEX_SESSIONS_CODEX_THREADS_TABLE} "
                    "WHERE codex_threads_id = ?",
                    (codex_threads_id,),
                ).fetchone()
                if occupied is not None:
                    raise RodexSessionStatisticsConflictError(
                        "Codex thread identity already belongs to another membership"
                    )
                row = connection.execute(
                    f"INSERT INTO {RODEX_SESSIONS_CODEX_THREADS_TABLE} "
                    "(rodex_sessions_id, codex_threads_id, first_linked_at_utc) "
                    "VALUES (?, ?, ?) "
                    "RETURNING id",
                    (session_id, codex_threads_id, item.first_linked_at_utc),
                ).fetchone()
                if row is None:
                    raise RodexSessionError("Codex thread insertion returned no identity")
                source_ids[thread_halves] = int(row[0])
                new_source_threads.add(item.codex_thread_id)
                continue
            stored_metadata = (
                None if existing[3] is None else int(existing[3]),
                None if existing[4] is None else str(existing[4]),
                None if existing[5] is None else str(existing[5]),
                None if existing[6] is None else int(existing[6]),
                (
                    None
                    if existing[7] is None
                    else str(
                        join_signed_bigints_into_a_codex_turn_id(existing[7], existing[8])
                    )
                ),
                None if existing[9] is None else str(existing[9]),
            )
            if stored_metadata != expected_metadata:
                raise RodexSessionStatisticsConflictError(
                    "Codex thread hierarchy changed during calculation"
                )
        source_threads_to_write = source_threads_to_write.union(new_source_threads)
        turn_sources = {
            split_codex_thread_id_into_signed_bigints(item.codex_thread_id)
            for item in turns
        }
        if not turn_sources.issubset(observed):
            raise RodexSessionStatisticsConflictError(
                "turn statistics include a source outside the analyzed snapshot"
            )

        aggregate_values = (
            new_publication_sequence,
            schema_version,
            calculated,
            coverage,
            *SESSION_STATISTICS_SCALARS.write_values(statistics_projection),
        )
        if previous_publication_sequence is None:
            statistics_row = connection.execute(
                f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TABLE} "
                "(rodex_sessions_id, statistics_publication_sequence, "
                "statistics_projection_schema_version, calculated_at_utc, "
                f"coverage_state, {SESSION_STATISTICS_SCALARS.columns_sql}) "
                f"VALUES (?, ?, ?, ?, ?, "
                f"{SESSION_STATISTICS_SCALARS.placeholders_sql}) RETURNING id",
                (session_id, *aggregate_values),
            ).fetchone()
        else:
            aggregate_assignments = ", ".join(
                f"{column} = ?"
                for column in (
                    "statistics_publication_sequence",
                    "statistics_projection_schema_version",
                    "calculated_at_utc",
                    "coverage_state",
                    *SESSION_STATISTICS_SCALARS.columns,
                )
            )
            statistics_row = connection.execute(
                f"UPDATE {RODEX_SESSIONS_STATISTICS_TABLE} "
                f"SET {aggregate_assignments} WHERE rodex_sessions_id = ? RETURNING id",
                (*aggregate_values, session_id),
            ).fetchone()
        if statistics_row is None:
            raise RodexSessionError("statistics upsert returned no identity")
        _sync_session_projection_children(connection, session_id, statistics_projection)
        worker_row = _upsert_analytics_worker(
            connection,
            session_id,
            worker_state="up_to_date",
            diagnostic_code=None,
            last_attempted_at_utc=calculated,
            consecutive_failures=0,
            next_retry_at_utc=None,
        )
        worker_id = int(worker_row[0])
        for item in observations:
            if item.codex_thread_id not in source_threads_to_write:
                continue
            source_id = source_ids[
                split_codex_thread_id_into_signed_bigints(item.codex_thread_id)
            ]
            rollout_row = connection.execute(
                f"SELECT id, rollout_file_path FROM "
                f"{RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE} "
                "WHERE rodex_sessions_codex_threads_id = ?",
                (source_id,),
            ).fetchone()
            if rollout_row is None:
                rollout_row = connection.execute(
                    f"INSERT INTO {RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE} "
                    "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
                    "rollout_file_path, first_observed_at_utc) VALUES (?, ?, ?, ?) "
                    "RETURNING id, rollout_file_path",
                    (
                        session_id,
                        source_id,
                        str(item.rollout_file_path),
                        item.verified_at_utc,
                    ),
                ).fetchone()
                if rollout_row is None:
                    raise RodexSessionError("rollout source insertion returned no identity")
            elif str(rollout_row[1]) != str(item.rollout_file_path):
                raise RodexSessionStatisticsConflictError(
                    "registered Codex rollout path changed during publication"
                )
            checkpoint_values = (
                item.analyzed_size_bytes,
                item.analyzed_mtime_ns,
                item.analyzed_prefix_sha256,
                item.verified_at_utc,
            )
            updated = connection.execute(
                f"UPDATE {RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE} "
                "SET analyzed_size_bytes = ?, analyzed_mtime_ns = ?, "
                "analyzed_prefix_sha256 = ?, verified_at_utc = ? "
                "WHERE rodex_sessions_analytics_workers_id = ? "
                "AND rodex_sessions_codex_rollout_sources_id = ?",
                (*checkpoint_values, worker_id, int(rollout_row[0])),
            )
            if updated.rowcount == 0:
                connection.execute(
                    f"INSERT INTO "
                    f"{RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE} "
                    "(rodex_sessions_id, rodex_sessions_analytics_workers_id, "
                    "rodex_sessions_codex_rollout_sources_id, analyzed_size_bytes, "
                    "analyzed_mtime_ns, analyzed_prefix_sha256, verified_at_utc) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        worker_id,
                        int(rollout_row[0]),
                        *checkpoint_values,
                    ),
                )
        turns_by_key = {(item.codex_thread_id, item.codex_turn_id): item for item in turns}
        if changed_turn_keys is None:
            stored_turn_keys = _select_stored_turn_keys(
                connection,
                tuple(source_ids.values()),
            )
            turns_to_write = turns
            turns_to_remove = stored_turn_keys - turn_keys
        else:
            turns_to_write = tuple(turns_by_key[key] for key in changed_turn_keys)
            turns_to_remove = set(removed_turn_keys)
        model_name_ids = {} if model_name_ids is None else model_name_ids
        reasoning_effort_name_ids = (
            {} if reasoning_effort_name_ids is None else reasoning_effort_name_ids
        )
        turn_row_ids: dict[tuple[CodexThreadId, str], int] = {}
        for item in turns_to_write:
            source_halves = split_codex_thread_id_into_signed_bigints(item.codex_thread_id)
            source_id = source_ids[source_halves]
            turn_identity = split_codex_turn_id_into_signed_bigints(item.codex_turn_id)
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
                f"SELECT id FROM "
                f"{RODEX_SESSIONS_CODEX_TURNS_TABLE} "
                "WHERE rodex_sessions_codex_threads_id = ? "
                "AND codex_turn_id_signed_bigint_1 = ? "
                "AND codex_turn_id_signed_bigint_2 = ?",
                (source_id, *turn_identity),
            ).fetchone()
            turn_state_values = (
                item.started_at_utc,
                item.terminal_at_utc,
                item.outcome,
                model_names_id,
                reasoning_effort_names_id,
            )
            if existing is None:
                row = None
                for _attempt_number in index_re_try_attempt_numbers():
                    public_id = uuid.uuid4()
                    row = connection.execute(
                        f"INSERT OR IGNORE INTO {RODEX_SESSIONS_CODEX_TURNS_TABLE} "
                        "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
                        "turn_public_id_signed_bigint_1, "
                        "turn_public_id_signed_bigint_2, "
                        "codex_turn_id_signed_bigint_1, "
                        "codex_turn_id_signed_bigint_2) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "RETURNING id",
                        (
                            session_id,
                            source_id,
                            *split_codex_thread_id_into_signed_bigints(public_id),
                            *turn_identity,
                        ),
                    ).fetchone()
                    if row is not None:
                        break
                if row is None:
                    raise RodexSessionError(
                        "turn statistics insertion returned no identity"
                    )
                turn_row_id = int(row[0])
            else:
                turn_row_id = int(existing[0])
            turn_state_columns = (
                "started_at_utc",
                "terminal_at_utc",
                "outcome",
                "model_names_id",
                "reasoning_effort_names_id",
            )
            state = connection.execute(
                f"SELECT id FROM {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} "
                "WHERE rodex_sessions_codex_turns_id = ?",
                (turn_row_id,),
            ).fetchone()
            if state is None:
                connection.execute(
                    f"INSERT INTO {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} "
                    "(rodex_sessions_id, rodex_sessions_codex_turns_id, "
                    + ", ".join(turn_state_columns)
                    + ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (session_id, turn_row_id, *turn_state_values),
                )
            else:
                connection.execute(
                    f"UPDATE {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} SET "
                    + ", ".join(f"{column} = ?" for column in turn_state_columns)
                    + " WHERE id = ? AND ("
                    + " OR ".join(f"{column} IS NOT ?" for column in turn_state_columns)
                    + ")",
                    (*turn_state_values, int(state[0]), *turn_state_values),
                )
            turn_row_ids[(item.codex_thread_id, item.codex_turn_id)] = turn_row_id
            _upsert_turn_statistics_metrics(connection, session_id, turn_row_id, item)
            _sync_turn_named_counts(
                connection,
                session_id,
                turn_row_id,
                item.named_counts,
            )
        for turn_key in turns_to_remove:
            source_id = source_ids.get(
                split_codex_thread_id_into_signed_bigints(turn_key[0])
            )
            if source_id is None:
                raise RodexSessionStatisticsConflictError(
                    "removed turn identifies an unregistered source"
                )
            turn_row_id = _lookup_turn_row_id(
                connection, source_id, turn_key[1], required=False
            )
            if turn_row_id is None:
                continue
            connection.execute(
                f"DELETE FROM {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
                "WHERE rodex_sessions_codex_turns_id = ?",
                (turn_row_id,),
            )
            connection.execute(
                f"DELETE FROM {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} "
                "WHERE rodex_sessions_codex_turns_id = ? AND rodex_sessions_id = ?",
                (turn_row_id, session_id),
            )
        for subagent_source in observations:
            if subagent_source.codex_thread_id not in new_source_threads:
                continue
            parent_thread_id = subagent_source.parent_codex_thread_id
            if parent_thread_id is None:
                continue
            spawning_codex_turn_id = subagent_source.spawning_codex_turn_id
            assert spawning_codex_turn_id is not None
            spawning_turn_row_id = turn_row_ids.get(
                (parent_thread_id, spawning_codex_turn_id)
            )
            if spawning_turn_row_id is None:
                spawning_turn_row_id = _lookup_turn_row_id(
                    connection,
                    source_ids[split_codex_thread_id_into_signed_bigints(parent_thread_id)],
                    spawning_codex_turn_id,
                    required=True,
                )
            subagent_source_id = source_ids[
                split_codex_thread_id_into_signed_bigints(subagent_source.codex_thread_id)
            ]
            connection.execute(
                f"INSERT INTO {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} "
                "(rodex_sessions_id, "
                "subagent_rodex_sessions_codex_threads_id, "
                "parent_rodex_sessions_codex_threads_id, "
                "spawning_rodex_sessions_codex_turns_id, agent_path, "
                "agent_nickname, history_inheritance_kind, "
                "inherited_history_start_ordinal) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    subagent_source_id,
                    source_ids[split_codex_thread_id_into_signed_bigints(parent_thread_id)],
                    spawning_turn_row_id,
                    subagent_source.agent_path,
                    subagent_source.agent_nickname,
                    subagent_source.history_inheritance_kind,
                    (
                        subagent_source.subagent_history_start_ordinal
                        if subagent_source.history_inheritance_kind == "inherited"
                        else None
                    ),
                ),
            )
        trace_receipt = (
            None
            if prepared_agent_trace_publication is None
            else publish_agent_trace_in_transaction(
                connection,
                session_id,
                prepared_agent_trace_publication,
                model_name_ids=model_name_ids,
                reasoning_effort_name_ids=reasoning_effort_name_ids,
            )
        )
    return RodexAnalyticsPublishReceipt(
        statistics_id=int(statistics_row[0]),
        statistics_publication_sequence=new_publication_sequence,
        statistics_projection_schema_version=schema_version,
        trace_publication_sequence=(
            None if trace_receipt is None else trace_receipt.trace_publication_sequence
        ),
    )


def record_rodex_session_analytics_worker_health(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    expected_current_codex_session_id: CodexSessionId | str,
    identity_fence: RodexAnalyticsIdentityFence | None = None,
    worker_state: str,
    diagnostic_code: str | None,
    last_attempted_at_utc: str,
    consecutive_failures: int | None,
    increment_failure: bool = False,
    next_retry_at_utc: str | None = None,
) -> RodexSessionAnalyticsWorker:
    """Update only fail-open worker health, preserving all last-good statistics."""
    _validate_session_id(session_id)
    expected_halves = split_codex_session_id_into_signed_bigints(
        expected_current_codex_session_id
    )
    state = _normalise_required_text(worker_state, "worker_state")
    if state not in STATISTICS_WORKER_STATES:
        raise ValueError(f"unsupported analytics worker state: {state}")
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
    if consecutive_failures is None:
        if not increment_failure:
            raise ValueError(
                "consecutive_failures may be omitted only for an atomic increment"
            )
    elif (
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
    path = normalise_rodex_database_path(database_path)
    with open_rodex_transaction(path) as connection:
        require_current_rodex_schema(connection)
        identity_row = connection.execute(
            f"SELECT registries.rodex_registry_id_signed_bigint, "
            "sessions.rodex_session_id_signed_bigint, "
            "current_ids.codex_thread_public_id_signed_bigint_1, "
            "current_ids.codex_thread_public_id_signed_bigint_2, "
            "runtimes.runtime_id_signed_bigint, "
            "workers.consecutive_failures "
            f"FROM {RODEX_SESSIONS_TABLE} AS sessions "
            f"JOIN {RODEX_REGISTRIES_TABLE} AS registries ON registries.id = 1 "
            f"JOIN {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} AS current "
            "ON current.rodex_sessions_id = sessions.id "
            f"JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS current_membership "
            "ON current_membership.id = current.rodex_sessions_codex_threads_id "
            f"JOIN {CODEX_THREADS_TABLE} AS current_ids "
            "ON current_ids.id = current_membership.codex_threads_id "
            f"LEFT JOIN {RODEX_RUNTIME_INSTANCES_TABLE} AS runtimes "
            "ON runtimes.rodex_sessions_id = sessions.id "
            f"LEFT JOIN {RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE} AS workers "
            "ON workers.rodex_sessions_id = sessions.id "
            "WHERE sessions.id = ?",
            (session_id,),
        ).fetchone()
        if identity_row is None:
            raise RodexSessionError(f"Rodex session does not exist: {session_id}")
        if identity_fence is not None:
            _require_analytics_identity_fence(
                identity_row,
                identity_fence,
                operation="worker health publication",
            )
        elif (int(identity_row[2]), int(identity_row[3])) != expected_halves:
            raise RodexSessionStatisticsConflictError(
                "current Codex session ID changed before worker health publication"
            )
        if consecutive_failures is None:
            consecutive_failures = (
                0 if identity_row[5] is None else int(identity_row[5])
            ) + 1
        row = _upsert_analytics_worker(
            connection,
            session_id,
            worker_state=state,
            diagnostic_code=diagnostic,
            last_attempted_at_utc=attempted,
            consecutive_failures=consecutive_failures,
            next_retry_at_utc=next_retry,
        )
    return _statistics_worker_from_row(row)


def read_rodex_analytics_checkpoint(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    expected_current_codex_session_id: CodexSessionId | str,
    identity_fence: RodexAnalyticsIdentityFence | None = None,
) -> RodexAnalyticsCheckpoint:
    """Read only publication, health, and source cursor facts in one SELECT."""
    _validate_session_id(session_id)
    expected_halves = split_codex_session_id_into_signed_bigints(
        expected_current_codex_session_id
    )
    path = normalise_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        rows = connection.execute(
            f"WITH RECURSIVE hierarchy(id, parent_id, thread_depth) AS ("
            "SELECT current.rodex_sessions_codex_threads_id, NULL, 0 "
            f"FROM {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} AS current "
            "WHERE current.rodex_sessions_id = ? "
            "UNION ALL "
            "SELECT spawns.subagent_rodex_sessions_codex_threads_id, "
            "spawns.parent_rodex_sessions_codex_threads_id, "
            "parent.thread_depth + 1 "
            f"FROM {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} AS spawns "
            "JOIN hierarchy AS parent ON "
            "spawns.parent_rodex_sessions_codex_threads_id = parent.id) "
            "SELECT registries.rodex_registry_id_signed_bigint, "
            "sessions.rodex_session_id_signed_bigint, "
            "current_ids.codex_thread_public_id_signed_bigint_1, "
            "current_ids.codex_thread_public_id_signed_bigint_2, "
            "runtimes.runtime_id_signed_bigint, "
            "statistics.statistics_publication_sequence, "
            "statistics.statistics_projection_schema_version, "
            "workers.id, workers.rodex_sessions_id, workers.worker_state, "
            "workers.diagnostic_code, workers.last_attempted_at_utc, "
            "workers.consecutive_failures, workers.next_retry_at_utc, "
            "sources.id, sources.rodex_sessions_id, "
            "source_ids.codex_thread_public_id_signed_bigint_1, "
            "source_ids.codex_thread_public_id_signed_bigint_2, "
            "CASE WHEN hierarchy.parent_id IS NULL "
            "THEN 'root' ELSE 'subagent' END, "
            "hierarchy.parent_id, "
            "hierarchy.thread_depth, spawns.agent_path, spawns.agent_nickname, "
            "CASE WHEN spawns.history_inheritance_kind = 'clean' THEN 0 "
            "ELSE spawns.inherited_history_start_ordinal END, "
            "spawning_turn.codex_turn_id_signed_bigint_1, "
            "spawning_turn.codex_turn_id_signed_bigint_2, "
            "sources.first_linked_at_utc, rollouts.rollout_file_path, "
            "checkpoints.analyzed_size_bytes, checkpoints.analyzed_mtime_ns, "
            "checkpoints.analyzed_prefix_sha256, checkpoints.verified_at_utc, "
            "spawns.history_inheritance_kind "
            f"FROM {RODEX_SESSIONS_TABLE} AS sessions "
            f"JOIN {RODEX_REGISTRIES_TABLE} AS registries ON registries.id = 1 "
            f"JOIN {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} AS current "
            "ON current.rodex_sessions_id = sessions.id "
            f"JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS current_membership "
            "ON current_membership.id = current.rodex_sessions_codex_threads_id "
            f"JOIN {CODEX_THREADS_TABLE} AS current_ids "
            "ON current_ids.id = current_membership.codex_threads_id "
            f"LEFT JOIN {RODEX_RUNTIME_INSTANCES_TABLE} AS runtimes "
            "ON runtimes.rodex_sessions_id = sessions.id "
            f"LEFT JOIN {RODEX_SESSIONS_STATISTICS_TABLE} AS statistics "
            "ON statistics.rodex_sessions_id = sessions.id "
            f"LEFT JOIN {RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE} AS workers "
            "ON workers.rodex_sessions_id = sessions.id "
            "LEFT JOIN hierarchy ON TRUE "
            f"LEFT JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS sources "
            "ON sources.id = hierarchy.id "
            f"LEFT JOIN {CODEX_THREADS_TABLE} AS source_ids "
            "ON source_ids.id = sources.codex_threads_id "
            f"LEFT JOIN {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} AS spawns "
            "ON spawns.subagent_rodex_sessions_codex_threads_id = sources.id "
            f"LEFT JOIN {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS spawning_turn "
            "ON spawning_turn.id = spawns.spawning_rodex_sessions_codex_turns_id "
            f"LEFT JOIN {RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE} AS rollouts "
            "ON rollouts.rodex_sessions_codex_threads_id = sources.id "
            f"LEFT JOIN {RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE} "
            "AS checkpoints ON checkpoints.rodex_sessions_analytics_workers_id = "
            "workers.id AND checkpoints.rodex_sessions_codex_rollout_sources_id = "
            "rollouts.id "
            "WHERE sessions.id = ? ORDER BY sources.id",
            (session_id, session_id),
        ).fetchall()
        trace_row = connection.execute(
            f"SELECT trace_publication_sequence, trace_schema_version "
            f"FROM {RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
        unresolved_target_rows = connection.execute(
            "SELECT DISTINCT ids.codex_thread_public_id_signed_bigint_1, "
            "ids.codex_thread_public_id_signed_bigint_2 "
            f"FROM {RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE} AS activity "
            f"JOIN {CODEX_THREADS_TABLE} AS ids "
            "ON ids.id = activity.target_codex_threads_id "
            f"LEFT JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS membership "
            "ON membership.codex_threads_id = ids.id "
            "AND membership.rodex_sessions_id = ? "
            "WHERE activity.rodex_sessions_id = ? "
            "AND membership.id IS NULL",
            (session_id, session_id),
        ).fetchall()
    if not rows:
        raise RodexSessionError(f"Rodex session does not exist: {session_id}")
    if identity_fence is not None:
        _require_analytics_identity_fence(
            rows[0],
            identity_fence,
            operation="checkpoint read",
        )
    elif (int(rows[0][2]), int(rows[0][3])) != expected_halves:
        raise RodexSessionStatisticsConflictError(
            "current Codex session ID changed before checkpoint read"
        )
    statistics = (
        None
        if rows[0][5] is None
        else RodexAnalyticsStatisticsCheckpoint(
            statistics_publication_sequence=int(rows[0][5]),
            statistics_projection_schema_version=str(rows[0][6]),
        )
    )
    worker = None if rows[0][7] is None else _statistics_worker_from_row(rows[0][7:14])
    sources = tuple(
        _codex_thread_from_row(row[14:33]) for row in rows if row[14] is not None
    )
    trace = (
        None
        if trace_row is None
        else RodexAnalyticsTraceCheckpoint(int(trace_row[0]), str(trace_row[1]))
    )
    unresolved_targets = tuple(
        join_signed_bigints_into_a_codex_thread_id(row[0], row[1])
        for row in unresolved_target_rows
    )
    return RodexAnalyticsCheckpoint(statistics, worker, sources, trace, unresolved_targets)


def read_rodex_session_statistics(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionStatisticsView:
    """Read last-good statistics, worker health, and sources in one transaction."""
    _validate_session_id(session_id)
    path = normalise_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        statistics_row = _select_statistics(connection, session_id)
        distribution_rows = _select_statistics_distributions(connection, session_id)
        named_count_rows = [
            *_select_statistics_named_counts(connection, session_id),
            *_select_statistics_turn_lookup_counts(connection, session_id),
        ]
        audit_limit_rows = _select_statistics_audit_limits(connection, session_id)
        worker_row = _select_analytics_worker(connection, session_id)
        source_rows = _select_codex_threads(connection, session_id)
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
        sources=tuple(_codex_thread_from_row(row) for row in source_rows),
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
    turn_id = str(parse_codex_turn_id(codex_turn_id))
    turn_identity = split_codex_turn_id_into_signed_bigints(turn_id)
    source_halves = (
        None
        if codex_thread_id is None
        else split_codex_thread_id_into_signed_bigints(codex_thread_id)
    )
    path = normalise_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        statistics_row = _select_statistics(connection, session_id)
        distribution_rows = _select_statistics_distributions(connection, session_id)
        named_count_rows = [
            *_select_statistics_named_counts(connection, session_id),
            *_select_statistics_turn_lookup_counts(connection, session_id),
        ]
        audit_limit_rows = _select_statistics_audit_limits(connection, session_id)
        worker_row = _select_analytics_worker(connection, session_id)
        source_rows = _select_codex_threads(connection, session_id)
        turn_scalar_columns = ", ".join(
            f"metrics.{column}" for column in TURN_STATISTICS_SCALARS.columns
        )
        query = (
            f"SELECT turns.id, turns.rodex_sessions_id, "
            "turns.rodex_sessions_codex_threads_id, "
            "source_ids.codex_thread_public_id_signed_bigint_1, "
            "source_ids.codex_thread_public_id_signed_bigint_2, "
            "turns.turn_public_id_signed_bigint_1, "
            "turns.turn_public_id_signed_bigint_2, "
            "turns.codex_turn_id_signed_bigint_1, "
            "turns.codex_turn_id_signed_bigint_2, "
            "states.started_at_utc, states.terminal_at_utc, states.outcome, "
            "models.name_of_the_model, "
            "efforts.name_of_the_reasoning_effort, "
            f"(SELECT COUNT(*) FROM {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} "
            "AS spawns WHERE spawns.spawning_rodex_sessions_codex_turns_id = "
            "turns.id), "
            f"{turn_scalar_columns} "
            f"FROM {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS turns "
            f"JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS sources "
            "ON sources.id = turns.rodex_sessions_codex_threads_id "
            f"JOIN {CODEX_THREADS_TABLE} AS source_ids "
            "ON source_ids.id = sources.codex_threads_id "
            f"JOIN {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} AS metrics "
            "ON metrics.rodex_sessions_codex_turns_id = turns.id "
            f"JOIN {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} AS states "
            "ON states.rodex_sessions_codex_turns_id = turns.id "
            f"LEFT JOIN {MODEL_NAMES_TABLE} AS models "
            "ON models.id = states.model_names_id "
            f"LEFT JOIN {REASONING_EFFORT_NAMES_TABLE} AS efforts "
            "ON efforts.id = states.reasoning_effort_names_id "
            "WHERE turns.rodex_sessions_id = ? "
            "AND turns.codex_turn_id_signed_bigint_1 = ? "
            "AND turns.codex_turn_id_signed_bigint_2 = ?"
        )
        parameters: tuple[object, ...] = (session_id, *turn_identity)
        if source_halves is not None:
            query += (
                " AND source_ids.codex_thread_public_id_signed_bigint_1 = ? "
                "AND source_ids.codex_thread_public_id_signed_bigint_2 = ?"
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
        sources=tuple(_codex_thread_from_row(row) for row in source_rows),
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


def read_rodex_session_codex_thread_summaries(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    expected_statistics_publication_sequence: int,
) -> tuple[RodexSessionCodexThreadSummary, ...]:
    """Group current turn facts by their existing source-row foreign key."""
    _validate_session_id(session_id)
    _validate_positive_id(
        expected_statistics_publication_sequence,
        "expected_statistics_publication_sequence",
    )
    path = normalise_rodex_database_path(database_path)
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
        source_rows = _select_codex_threads(connection, session_id)
        active_source_ids = tuple(int(row[0]) for row in source_rows)
        if not active_source_ids:
            raise RodexSessionError("Rodex session has no current Codex thread")
        active_placeholders = ", ".join("?" for _ in active_source_ids)
        aggregate_rows = connection.execute(
            f"SELECT sources.id, COUNT(metrics.id), "
            "COALESCE(SUM(metrics.id IS NOT NULL AND states.outcome = 'completed'), 0), "
            "COALESCE(SUM(metrics.id IS NOT NULL AND states.outcome = 'aborted'), 0), "
            "COALESCE(SUM(metrics.id IS NOT NULL AND states.outcome = 'open'), 0), "
            "MIN(CASE WHEN metrics.id IS NOT NULL THEN states.started_at_utc END), "
            "MAX(CASE WHEN metrics.id IS NOT NULL THEN states.terminal_at_utc END), "
            "COALESCE(SUM(metrics.input_tokens), 0), "
            "COALESCE(SUM(metrics.cached_input_tokens), 0), "
            "COALESCE(SUM(metrics.cache_write_input_tokens), 0), "
            "COALESCE(SUM(metrics.output_tokens), 0), "
            "COALESCE(SUM(metrics.reasoning_output_tokens), 0), "
            "COALESCE(SUM(metrics.total_tokens), 0), "
            "COALESCE(SUM(metrics.commands_executed_count), 0), "
            "COALESCE(SUM(metrics.model_tool_requests_count), 0), "
            "COALESCE(SUM(metrics.file_change_operations_count), 0), "
            "COALESCE(SUM(metrics.file_change_occurrences_count), 0), "
            "COALESCE(SUM(metrics.web_operations_count), 0), "
            "COALESCE(SUM(metrics.web_queries_count), 0), "
            "COALESCE(SUM(metrics.web_result_records_count), 0), "
            "COALESCE(SUM(metrics.compactions_count), 0) "
            f"FROM {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS sources "
            f"LEFT JOIN {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS turns "
            "ON turns.rodex_sessions_codex_threads_id = sources.id "
            f"LEFT JOIN {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} AS states "
            "ON states.rodex_sessions_codex_turns_id = turns.id "
            f"LEFT JOIN {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} AS metrics "
            "ON metrics.rodex_sessions_codex_turns_id = turns.id "
            f"WHERE sources.id IN ({active_placeholders}) "
            "GROUP BY sources.id ORDER BY sources.id",
            active_source_ids,
        ).fetchall()
        count_rows = connection.execute(
            f"SELECT turns.rodex_sessions_codex_threads_id, counts.count_kind, "
            "counts.count_name, SUM(counts.occurrence_count) "
            f"FROM {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} AS counts "
            f"JOIN {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS turns "
            "ON turns.id = counts.rodex_sessions_codex_turns_id "
            "WHERE turns.rodex_sessions_id = ? "
            "GROUP BY turns.rodex_sessions_codex_threads_id, "
            "counts.count_kind, counts.count_name "
            "ORDER BY turns.rodex_sessions_codex_threads_id, "
            "counts.count_kind, counts.count_name",
            (session_id,),
        ).fetchall()
        spawned_subagent_rows = connection.execute(
            "SELECT spawns.parent_rodex_sessions_codex_threads_id, COUNT(*) "
            f"FROM {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} AS spawns "
            "WHERE spawns.rodex_sessions_id = ? "
            "GROUP BY spawns.parent_rodex_sessions_codex_threads_id",
            (session_id,),
        ).fetchall()
    sources = {
        source.id: source for source in (_codex_thread_from_row(row) for row in source_rows)
    }
    counts_by_source: dict[int, list[tuple[object, ...]]] = {}
    for row in count_rows:
        counts_by_source.setdefault(int(row[0]), []).append(row[1:])
    summaries = tuple(
        RodexSessionCodexThreadSummary(
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


def _sync_session_projection_children(
    connection: sqlite3.Connection,
    session_id: int,
    projection: SessionStatisticsProjection,
) -> None:
    """Synchronize bounded aggregate children without rewriting equal rows."""
    distributions = {item.distribution_kind: item for item in projection.distributions}
    stored_distributions = {
        str(row[0]): tuple(row[1:])
        for row in connection.execute(
            "SELECT distribution_kind, observation_count, total, median, p75, p90, "
            "p95, maximum FROM "
            f"{RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        )
    }
    connection.executemany(
        f"INSERT INTO {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE} "
        "(rodex_sessions_id, distribution_kind, observation_count, total, median, "
        "p75, p90, p95, maximum) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                session_id,
                item.distribution_kind,
                item.observation_count,
                item.total,
                item.median,
                item.p75,
                item.p90,
                item.p95,
                item.maximum,
            )
            for key, item in distributions.items()
            if key not in stored_distributions
        ),
    )
    connection.executemany(
        f"UPDATE {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE} SET "
        "observation_count = ?, total = ?, median = ?, p75 = ?, p90 = ?, p95 = ?, "
        "maximum = ? WHERE rodex_sessions_id = ? AND distribution_kind = ?",
        (
            (
                item.observation_count,
                item.total,
                item.median,
                item.p75,
                item.p90,
                item.p95,
                item.maximum,
                session_id,
                key,
            )
            for key, item in distributions.items()
            if key in stored_distributions
            and stored_distributions[key]
            != (
                item.observation_count,
                item.total,
                item.median,
                item.p75,
                item.p90,
                item.p95,
                item.maximum,
            )
        ),
    )
    connection.executemany(
        f"DELETE FROM {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE} "
        "WHERE rodex_sessions_id = ? AND distribution_kind = ?",
        ((session_id, key) for key in stored_distributions.keys() - distributions.keys()),
    )

    named_counts = {
        (item.count_kind, item.count_name): item
        for item in projection.named_counts
        if item.count_kind not in _DERIVED_SESSION_NAMED_COUNT_KINDS
    }
    stored_named_counts = {
        (str(row[0]), str(row[1])): int(row[2])
        for row in connection.execute(
            f"SELECT count_kind, count_name, occurrence_count FROM "
            f"{RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        )
    }
    connection.executemany(
        f"INSERT INTO {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE} "
        "(rodex_sessions_id, count_kind, count_name, occurrence_count) "
        "VALUES (?, ?, ?, ?)",
        (
            (session_id, item.count_kind, item.count_name, item.occurrence_count)
            for key, item in named_counts.items()
            if key not in stored_named_counts
        ),
    )
    connection.executemany(
        f"UPDATE {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE} "
        "SET occurrence_count = ? WHERE rodex_sessions_id = ? "
        "AND count_kind = ? AND count_name = ?",
        (
            (item.occurrence_count, session_id, *key)
            for key, item in named_counts.items()
            if key in stored_named_counts
            and stored_named_counts[key] != item.occurrence_count
        ),
    )
    connection.executemany(
        f"DELETE FROM {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE} "
        "WHERE rodex_sessions_id = ? AND count_kind = ? AND count_name = ?",
        (
            (session_id, count_kind, count_name)
            for count_kind, count_name in stored_named_counts.keys() - named_counts.keys()
        ),
    )

    audit_limits = dict(enumerate(projection.audit_limits))
    stored_audit_limits = {
        int(row[0]): str(row[1])
        for row in connection.execute(
            f"SELECT limit_ordinal, limitation FROM "
            f"{RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        )
    }
    connection.executemany(
        f"INSERT INTO {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE} "
        "(rodex_sessions_id, limit_ordinal, limitation) VALUES (?, ?, ?)",
        (
            (session_id, ordinal, limitation)
            for ordinal, limitation in audit_limits.items()
            if ordinal not in stored_audit_limits
        ),
    )
    connection.executemany(
        f"UPDATE {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE} SET limitation = ? "
        "WHERE rodex_sessions_id = ? AND limit_ordinal = ?",
        (
            (limitation, session_id, ordinal)
            for ordinal, limitation in audit_limits.items()
            if ordinal in stored_audit_limits and stored_audit_limits[ordinal] != limitation
        ),
    )
    connection.executemany(
        f"DELETE FROM {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE} "
        "WHERE rodex_sessions_id = ? AND limit_ordinal = ?",
        (
            (session_id, ordinal)
            for ordinal in stored_audit_limits.keys() - audit_limits.keys()
        ),
    )


def _upsert_turn_statistics_metrics(
    connection: sqlite3.Connection,
    session_id: int,
    turn_row_id: int,
    projection: TurnStatisticsProjection,
) -> None:
    """Replace only the current statistics facts for one stable Codex turn."""
    values = TURN_STATISTICS_SCALARS.write_values(projection)
    existing = connection.execute(
        f"SELECT id, {TURN_STATISTICS_SCALARS.columns_sql} "
        f"FROM {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} "
        "WHERE rodex_sessions_codex_turns_id = ?",
        (turn_row_id,),
    ).fetchone()
    if existing is None:
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} "
            "(rodex_sessions_id, rodex_sessions_codex_turns_id, "
            f"{TURN_STATISTICS_SCALARS.columns_sql}) VALUES (?, ?, "
            f"{TURN_STATISTICS_SCALARS.placeholders_sql})",
            (session_id, turn_row_id, *values),
        )
        return
    if tuple(existing[1:]) == values:
        return
    connection.execute(
        f"UPDATE {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} SET "
        + ", ".join(f"{column} = ?" for column in TURN_STATISTICS_SCALARS.columns)
        + " WHERE id = ?",
        (*values, int(existing[0])),
    )


def _sync_turn_named_counts(
    connection: sqlite3.Connection,
    session_id: int,
    turn_row_id: int,
    named_counts: Sequence[StatisticsNamedCount],
) -> None:
    desired = {
        (item.count_kind, item.count_name): item
        for item in named_counts
        if item.count_kind != "collaboration_tool"
    }
    stored = {
        (str(row[0]), str(row[1])): int(row[2])
        for row in connection.execute(
            f"SELECT count_kind, count_name, occurrence_count FROM "
            f"{RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
            "WHERE rodex_sessions_codex_turns_id = ?",
            (turn_row_id,),
        )
    }
    connection.executemany(
        f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
        "(rodex_sessions_id, rodex_sessions_codex_turns_id, count_kind, "
        "count_name, occurrence_count) VALUES (?, ?, ?, ?, ?)",
        (
            (
                session_id,
                turn_row_id,
                item.count_kind,
                item.count_name,
                item.occurrence_count,
            )
            for key, item in desired.items()
            if key not in stored
        ),
    )
    connection.executemany(
        f"UPDATE {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
        "SET occurrence_count = ? WHERE rodex_sessions_codex_turns_id = ? "
        "AND count_kind = ? AND count_name = ?",
        (
            (item.occurrence_count, turn_row_id, *key)
            for key, item in desired.items()
            if key in stored and stored[key] != item.occurrence_count
        ),
    )
    connection.executemany(
        f"DELETE FROM {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
        "WHERE rodex_sessions_codex_turns_id = ? "
        "AND count_kind = ? AND count_name = ?",
        (
            (turn_row_id, count_kind, count_name)
            for count_kind, count_name in stored.keys() - desired.keys()
        ),
    )


def _select_stored_turn_keys(
    connection: sqlite3.Connection,
    source_row_ids: Sequence[int],
) -> set[tuple[CodexThreadId, str]]:
    if not source_row_ids:
        return set()
    placeholders = ", ".join("?" for _ in source_row_ids)
    return {
        (
            join_signed_bigints_into_a_codex_thread_id(row[0], row[1]),
            str(join_signed_bigints_into_a_codex_turn_id(row[2], row[3])),
        )
        for row in connection.execute(
            "SELECT source_ids.codex_thread_public_id_signed_bigint_1, "
            "source_ids.codex_thread_public_id_signed_bigint_2, "
            "turns.codex_turn_id_signed_bigint_1, "
            "turns.codex_turn_id_signed_bigint_2 "
            f"FROM {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS turns "
            f"JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS sources "
            "ON sources.id = turns.rodex_sessions_codex_threads_id "
            f"JOIN {CODEX_THREADS_TABLE} AS source_ids "
            "ON source_ids.id = sources.codex_threads_id "
            f"JOIN {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} AS metrics "
            "ON metrics.rodex_sessions_codex_turns_id = turns.id "
            "WHERE turns.rodex_sessions_codex_threads_id "
            f"IN ({placeholders})",
            tuple(source_row_ids),
        )
    }


def _lookup_turn_row_id(
    connection: sqlite3.Connection,
    source_id: int,
    codex_turn_id: str,
    *,
    required: bool,
) -> int | None:
    row = connection.execute(
        f"SELECT id FROM {RODEX_SESSIONS_CODEX_TURNS_TABLE} "
        "WHERE rodex_sessions_codex_threads_id = ? "
        "AND codex_turn_id_signed_bigint_1 = ? "
        "AND codex_turn_id_signed_bigint_2 = ?",
        (source_id, *split_codex_turn_id_into_signed_bigints(codex_turn_id)),
    ).fetchone()
    if row is None:
        if required:
            raise RodexSessionStatisticsConflictError(
                "sub-agent spawning turn disappeared during publication"
            )
        return None
    return int(row[0])


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


def _validate_authoritative_collaboration_projection(
    projection: SessionStatisticsProjection,
    observations: Sequence[RodexSessionCodexThreadObservation],
    *,
    complete_turn_statistics: bool,
) -> None:
    expected_session_tools = _canonical_collaboration_count_map(projection.named_counts)
    if _named_count_map(projection.named_counts, "collaboration_tool") != (
        expected_session_tools
    ):
        raise ValueError(
            "session collaboration tools must derive from canonical model tools"
        )
    if projection.collaboration_operations_count != sum(expected_session_tools.values()):
        raise ValueError(
            "session collaboration operations must derive from canonical model tools"
        )
    verified_subagents = tuple(
        item for item in observations if item.parent_codex_thread_id is not None
    )
    if projection.collaboration_agents_started_count != len(verified_subagents):
        raise ValueError("session agents started must equal verified sub-agent sources")

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
    if complete_turn_statistics and observed_session_tools != expected_session_tools:
        raise ValueError(
            "session collaboration tools must equal exact-turn collaboration tools"
        )
    if complete_turn_statistics and observed_agent_count != len(verified_subagents):
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
                f"FROM {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS turns "
                f"JOIN {RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} AS states "
                "ON states.rodex_sessions_codex_turns_id = turns.id "
                f"JOIN {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} AS metrics "
                "ON metrics.rodex_sessions_codex_turns_id = turns.id "
                f"JOIN {table_name} AS names ON names.id = states.{foreign_key} "
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
        "WHERE rodex_sessions_codex_turns_id = ? "
        "ORDER BY count_kind, count_name",
        (turn_row_id,),
    ).fetchall()


def _select_analytics_worker(
    connection: sqlite3.Connection, session_id: int
) -> tuple[object, ...] | None:
    return connection.execute(
        f"SELECT id, rodex_sessions_id, worker_state, diagnostic_code, "
        "last_attempted_at_utc, consecutive_failures, next_retry_at_utc "
        f"FROM {RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE} "
        "WHERE rodex_sessions_id = ?",
        (session_id,),
    ).fetchone()


def _upsert_analytics_worker(
    connection: sqlite3.Connection,
    session_id: int,
    *,
    worker_state: str,
    diagnostic_code: str | None,
    last_attempted_at_utc: str,
    consecutive_failures: int,
    next_retry_at_utc: str | None,
) -> tuple[object, ...]:
    values = (
        worker_state,
        diagnostic_code,
        last_attempted_at_utc,
        consecutive_failures,
        next_retry_at_utc,
    )
    row = connection.execute(
        f"UPDATE {RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE} SET "
        "worker_state = ?, diagnostic_code = ?, last_attempted_at_utc = ?, "
        "consecutive_failures = ?, next_retry_at_utc = ? "
        "WHERE rodex_sessions_id = ? RETURNING id, rodex_sessions_id, worker_state, "
        "diagnostic_code, last_attempted_at_utc, consecutive_failures, "
        "next_retry_at_utc",
        (*values, session_id),
    ).fetchone()
    if row is not None:
        return row
    inserted = connection.execute(
        f"INSERT INTO {RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE} "
        "(rodex_sessions_id, worker_state, diagnostic_code, last_attempted_at_utc, "
        "consecutive_failures, next_retry_at_utc) VALUES (?, ?, ?, ?, ?, ?) "
        "RETURNING id, rodex_sessions_id, worker_state, diagnostic_code, "
        "last_attempted_at_utc, consecutive_failures, next_retry_at_utc",
        (
            session_id,
            *values,
        ),
    ).fetchone()
    if inserted is None:
        raise RodexSessionError(f"analytics worker disappeared: {session_id}")
    return inserted


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


def _turn_statistics_from_rows(
    row: tuple[object, ...],
    named_count_rows: Sequence[tuple[object, ...]],
) -> RodexSessionTurnStatistics:
    values = TURN_STATISTICS_SCALARS.read_values(row[15:])
    named_counts = _append_collaboration_view_from_model_tools(
        _named_counts_from_rows(named_count_rows)
    )
    projection = TurnStatisticsProjection(
        codex_thread_id=join_signed_bigints_into_a_codex_thread_id(row[3], row[4]),
        codex_turn_id=str(join_signed_bigints_into_a_codex_turn_id(row[7], row[8])),
        started_at_utc=None if row[9] is None else str(row[9]),
        terminal_at_utc=None if row[10] is None else str(row[10]),
        outcome=str(row[11]),
        model=None if row[12] is None else str(row[12]),
        reasoning_effort=None if row[13] is None else str(row[13]),
        **values,
        collaboration_operations_count=sum(
            _canonical_collaboration_count_map(named_counts).values()
        ),
        collaboration_agents_started_count=int(row[14]),
        named_counts=named_counts,
    )
    return RodexSessionTurnStatistics(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        rodex_sessions_codex_threads_id=int(row[2]),
        turn_public_id=join_signed_bigints_into_a_codex_turn_id(row[5], row[6]),
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
) -> RodexSessionAnalyticsWorker:
    return RodexSessionAnalyticsWorker(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        worker_state=str(row[2]),
        diagnostic_code=None if row[3] is None else str(row[3]),
        last_attempted_at_utc=str(row[4]),
        consecutive_failures=int(row[5]),
        next_retry_at_utc=None if row[6] is None else str(row[6]),
    )
