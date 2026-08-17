"""Authoritative relational statistics publication and read pipeline."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rodex_sql import open_rodex_read_transaction, open_rodex_transaction

from .errors import (
    RodexSessionError,
    RodexSessionStatisticsConflictError,
    RodexSessionTurnStatisticsAmbiguousError,
)
from .identity import (
    CodexSessionId,
    join_signed_bigints_into_a_codex_session_id,
    parse_codex_session_id,
    split_codex_session_id_into_signed_bigints,
)
from .schema import (
    _SESSION_PROJECTION_SCALAR_COLUMNS,
    _TURN_DATABASE_SCALAR_COLUMNS,
    _TURN_PROJECTION_SCALAR_COLUMNS,
    RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE,
    RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE,
    RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE,
    RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
    RODEX_SESSIONS_STATISTICS_TABLE,
    RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE,
    RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
    RODEX_SESSIONS_STATISTICS_WORKERS_TABLE,
    RODEX_SESSIONS_TABLE,
    STATISTICS_COVERAGE_STATES,
    STATISTICS_WORKER_STATES,
    initialise_rodex_database,
)
from .statistics_projection import (
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


@dataclass(frozen=True, slots=True)
class RodexSessionStatistics:
    """Latest fully relational analyzer projection for one Rodex session."""

    id: int
    rodex_sessions_id: int
    statistics_revision: int
    statistics_projection_schema_version: str
    calculated_at_utc: str
    coverage_state: str
    projection: SessionStatisticsProjection


@dataclass(frozen=True, slots=True)
class RodexSessionStatisticsSource:
    """One Codex lineage source and its latest analyzed prefix provenance."""

    id: int
    rodex_sessions_id: int
    codex_session_id: CodexSessionId
    first_linked_at_utc: str
    rollout_file_path: str | None
    analyzed_size_bytes: int | None
    analyzed_mtime_ns: int | None
    analyzed_prefix_sha256: str | None
    verified_at_utc: str | None
    included_statistics_revision: int | None


@dataclass(frozen=True, slots=True)
class RodexSessionStatisticsSourceObservation:
    """Exact bytes and filesystem state used for one aggregate calculation."""

    codex_session_id: CodexSessionId
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
    included_statistics_revision: int
    projection: TurnStatisticsProjection

    @property
    def codex_session_id(self) -> CodexSessionId:
        return self.projection.codex_session_id

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
    based_on_statistics_revision: int | None,
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
    if based_on_statistics_revision is not None:
        _validate_positive_id(based_on_statistics_revision, "based_on_statistics_revision")
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
    if len({item.codex_session_id for item in observations}) != len(observations):
        raise ValueError("analyzed_sources contains a duplicate Codex session ID")
    if statistics_projection.analyzer_source_count != len(observations):
        raise ValueError(
            "analyzer source count must equal authenticated source observations"
        )
    turns = statistics_projection.turn_statistics
    turn_keys = {(item.codex_session_id, item.codex_turn_id) for item in turns}
    if len(turn_keys) != len(turns):
        raise ValueError("turn_statistics contains a duplicate source and turn ID")

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
            f"SELECT statistics_revision FROM {RODEX_SESSIONS_STATISTICS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
        previous_revision = None if previous_row is None else int(previous_row[0])
        if previous_revision != based_on_statistics_revision:
            raise RodexSessionStatisticsConflictError(
                "statistics revision changed during calculation"
            )
        new_revision = 1 if previous_revision is None else previous_revision + 1
        registered_rows = connection.execute(
            f"SELECT id, codex_session_id_signed_bigint_1, "
            "codex_session_id_signed_bigint_2 "
            f"FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchall()
        registered = {(int(row[1]), int(row[2])) for row in registered_rows}
        source_ids = {(int(row[1]), int(row[2])): int(row[0]) for row in registered_rows}
        observed = {
            split_codex_session_id_into_signed_bigints(item.codex_session_id)
            for item in observations
        }
        if not observed.issubset(registered):
            raise RodexSessionStatisticsConflictError(
                "statistics include an unregistered Codex source"
            )
        if observed != registered:
            raise RodexSessionStatisticsConflictError(
                "statistics omit a registered Codex lineage source"
            )
        turn_sources = {
            split_codex_session_id_into_signed_bigints(item.codex_session_id)
            for item in turns
        }
        if not turn_sources.issubset(observed):
            raise RodexSessionStatisticsConflictError(
                "turn statistics include a source outside the analyzed snapshot"
            )

        connection.execute(
            f"UPDATE {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
            "SET included_statistics_revision = NULL "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        )
        scalar_columns_sql = ", ".join(_SESSION_PROJECTION_SCALAR_COLUMNS)
        scalar_placeholders = ", ".join("?" for _ in _SESSION_PROJECTION_SCALAR_COLUMNS)
        scalar_updates = ", ".join(
            f"{column} = excluded.{column}" for column in _SESSION_PROJECTION_SCALAR_COLUMNS
        )
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TABLE} "
            "(rodex_sessions_id, statistics_revision, "
            "statistics_projection_schema_version, calculated_at_utc, "
            f"coverage_state, {scalar_columns_sql}) "
            f"VALUES (?, ?, ?, ?, ?, {scalar_placeholders}) "
            "ON CONFLICT(rodex_sessions_id) DO UPDATE SET "
            "statistics_revision = excluded.statistics_revision, "
            "statistics_projection_schema_version = "
            "excluded.statistics_projection_schema_version, "
            "calculated_at_utc = excluded.calculated_at_utc, "
            "coverage_state = excluded.coverage_state, "
            f"{scalar_updates}",
            (
                session_id,
                new_revision,
                schema_version,
                calculated,
                coverage,
                *(
                    getattr(statistics_projection, column)
                    for column in _SESSION_PROJECTION_SCALAR_COLUMNS
                ),
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
            "(rodex_sessions_id, included_statistics_revision, distribution_kind, "
            "observation_count, total, median, p75, p90, p95, maximum) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    session_id,
                    new_revision,
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
            "(rodex_sessions_id, included_statistics_revision, count_kind, "
            "count_name, occurrence_count) VALUES (?, ?, ?, ?, ?)",
            (
                (
                    session_id,
                    new_revision,
                    item.count_kind,
                    item.count_name,
                    item.occurrence_count,
                )
                for item in statistics_projection.named_counts
            ),
        )
        connection.executemany(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE} "
            "(rodex_sessions_id, included_statistics_revision, limit_ordinal, "
            "limitation) VALUES (?, ?, ?, ?)",
            (
                (session_id, new_revision, ordinal, limitation)
                for ordinal, limitation in enumerate(statistics_projection.audit_limits)
            ),
        )
        for item in observations:
            cursor = connection.execute(
                f"UPDATE {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} SET "
                "rollout_file_path = ?, analyzed_size_bytes = ?, "
                "analyzed_mtime_ns = ?, analyzed_prefix_sha256 = ?, "
                "verified_at_utc = ?, included_statistics_revision = ? "
                "WHERE rodex_sessions_id = ? AND codex_session_id_signed_bigint_1 = ? "
                "AND codex_session_id_signed_bigint_2 = ?",
                (
                    str(item.rollout_file_path),
                    item.analyzed_size_bytes,
                    item.analyzed_mtime_ns,
                    item.analyzed_prefix_sha256,
                    item.verified_at_utc,
                    new_revision,
                    session_id,
                    *split_codex_session_id_into_signed_bigints(item.codex_session_id),
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
        turn_scalar_columns_sql = ", ".join(_TURN_DATABASE_SCALAR_COLUMNS)
        turn_scalar_placeholders = ", ".join("?" for _ in _TURN_DATABASE_SCALAR_COLUMNS)
        turn_scalar_updates = ", ".join(
            f"{column} = excluded.{column}" for column in _TURN_DATABASE_SCALAR_COLUMNS
        )
        for item in turns:
            source_halves = split_codex_session_id_into_signed_bigints(
                item.codex_session_id
            )
            source_id = source_ids[source_halves]
            turn_hash = _turn_id_sha256_signed_bigints(item.codex_turn_id)
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
                "codex_turn_id, included_statistics_revision, started_at_utc, "
                f"terminal_at_utc, outcome, {turn_scalar_columns_sql}) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                f"{turn_scalar_placeholders}) "
                "ON CONFLICT(rodex_sessions_statistics_sources_id, "
                "codex_turn_id_sha256_int_1, codex_turn_id_sha256_int_2, "
                "codex_turn_id_sha256_int_3, codex_turn_id_sha256_int_4) "
                "DO UPDATE SET included_statistics_revision = "
                "excluded.included_statistics_revision, "
                "started_at_utc = excluded.started_at_utc, "
                "terminal_at_utc = excluded.terminal_at_utc, "
                "outcome = excluded.outcome, "
                f"{turn_scalar_updates} "
                "RETURNING id",
                (
                    session_id,
                    source_id,
                    *turn_hash,
                    item.codex_turn_id,
                    new_revision,
                    item.started_at_utc,
                    item.terminal_at_utc,
                    item.outcome,
                    *_turn_database_scalar_values(item),
                ),
            ).fetchone()
            if row is None:
                raise RodexSessionError("turn statistics upsert returned no identity")
            turn_row_id = int(row[0])
            connection.executemany(
                f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
                "(rodex_sessions_id, rodex_sessions_statistics_turns_id, "
                "included_statistics_revision, count_kind, count_name, "
                "occurrence_count) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        session_id,
                        turn_row_id,
                        new_revision,
                        count.count_kind,
                        count.count_name,
                        count.occurrence_count,
                    )
                    for count in item.named_counts
                ),
            )
        connection.execute(
            f"DELETE FROM {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} "
            "WHERE rodex_sessions_id = ? AND included_statistics_revision != ?",
            (session_id, new_revision),
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
    path = initialise_rodex_database(database_path)
    with open_rodex_read_transaction(path) as connection:
        statistics_row = _select_statistics(connection, session_id)
        distribution_rows = _select_statistics_distributions(connection, session_id)
        named_count_rows = _select_statistics_named_counts(connection, session_id)
        audit_limit_rows = _select_statistics_audit_limits(connection, session_id)
        worker_row = _select_statistics_worker(connection, session_id)
        source_rows = _select_statistics_sources(connection, session_id)
    return RodexSessionStatisticsView(
        statistics=(
            None
            if statistics_row is None
            else _session_statistics_from_rows(
                statistics_row,
                distribution_rows,
                named_count_rows,
                audit_limit_rows,
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
    codex_session_id: CodexSessionId | str | None = None,
) -> RodexSessionTurnStatisticsView:
    """Read one exact turn and its parent freshness in one transaction."""
    _validate_session_id(session_id)
    turn_id = _normalise_required_text(codex_turn_id, "codex_turn_id")
    turn_hash = _turn_id_sha256_signed_bigints(turn_id)
    source_halves = (
        None
        if codex_session_id is None
        else split_codex_session_id_into_signed_bigints(codex_session_id)
    )
    path = initialise_rodex_database(database_path)
    with open_rodex_read_transaction(path) as connection:
        statistics_row = _select_statistics(connection, session_id)
        distribution_rows = _select_statistics_distributions(connection, session_id)
        named_count_rows = _select_statistics_named_counts(connection, session_id)
        audit_limit_rows = _select_statistics_audit_limits(connection, session_id)
        worker_row = _select_statistics_worker(connection, session_id)
        source_rows = _select_statistics_sources(connection, session_id)
        turn_scalar_columns = ", ".join(
            f"turns.{column}" for column in _TURN_DATABASE_SCALAR_COLUMNS
        )
        query = (
            f"SELECT turns.id, turns.rodex_sessions_id, "
            "turns.rodex_sessions_statistics_sources_id, "
            "sources.codex_session_id_signed_bigint_1, "
            "sources.codex_session_id_signed_bigint_2, turns.codex_turn_id, "
            "turns.included_statistics_revision, turns.started_at_utc, "
            f"turns.terminal_at_utc, turns.outcome, {turn_scalar_columns} "
            f"FROM {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} AS turns "
            f"JOIN {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} AS sources "
            "ON sources.id = turns.rodex_sessions_statistics_sources_id "
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
                " AND sources.codex_session_id_signed_bigint_1 = ? "
                "AND sources.codex_session_id_signed_bigint_2 = ?"
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
            "turn ID exists in multiple Codex sources; qualify it with a Codex session ID"
        )
    return RodexSessionTurnStatisticsView(
        statistics=(
            None
            if statistics_row is None
            else _session_statistics_from_rows(
                statistics_row,
                distribution_rows,
                named_count_rows,
                audit_limit_rows,
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
    """List every Codex source registered to one Rodex statistics lineage."""
    return read_rodex_session_statistics(session_id, database_path).sources


def register_codex_statistics_source_in_transaction(
    connection: sqlite3.Connection,
    session_id: int,
    codex_session_id: CodexSessionId,
    first_linked_at_utc: str,
) -> None:
    """Register one Codex lineage inside its owning lifecycle transaction."""
    stored_codex_session_id = split_codex_session_id_into_signed_bigints(codex_session_id)
    row = connection.execute(
        f"SELECT rodex_sessions_id FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
        "WHERE codex_session_id_signed_bigint_1 = ? "
        "AND codex_session_id_signed_bigint_2 = ?",
        stored_codex_session_id,
    ).fetchone()
    if row is None:
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
            "(rodex_sessions_id, codex_session_id_signed_bigint_1, "
            "codex_session_id_signed_bigint_2, first_linked_at_utc) VALUES (?, ?, ?, ?)",
            (session_id, *stored_codex_session_id, first_linked_at_utc),
        )
        return
    if int(row[0]) != session_id:
        raise RodexSessionError(
            "Codex history already belongs to another Rodex statistics lineage: "
            f"{codex_session_id}"
        )


def _turn_database_scalar_values(
    projection: TurnStatisticsProjection,
) -> tuple[object, ...]:
    command_duration = projection.command_duration
    return (
        *(getattr(projection, name) for name in _TURN_PROJECTION_SCALAR_COLUMNS[:11]),
        command_duration.observation_count,
        command_duration.total,
        command_duration.median,
        command_duration.p75,
        command_duration.p90,
        command_duration.p95,
        command_duration.maximum,
        *(getattr(projection, name) for name in _TURN_PROJECTION_SCALAR_COLUMNS[11:]),
    )


def _turn_id_sha256_signed_bigints(turn_id: str) -> tuple[int, int, int, int]:
    normalized = _normalise_required_text(turn_id, "codex_turn_id")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    pieces = tuple(
        int.from_bytes(digest[offset : offset + 8], "big", signed=True)
        for offset in range(0, 32, 8)
    )
    return pieces[0], pieces[1], pieces[2], pieces[3]


def _validate_source_observation(
    observation: RodexSessionStatisticsSourceObservation,
) -> RodexSessionStatisticsSourceObservation:
    if not isinstance(observation, RodexSessionStatisticsSourceObservation):
        raise TypeError(
            "analyzed_sources must contain RodexSessionStatisticsSourceObservation values"
        )
    codex_session_id = parse_codex_session_id(observation.codex_session_id)
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
        codex_session_id=codex_session_id,
        rollout_file_path=source_path,
        analyzed_size_bytes=observation.analyzed_size_bytes,
        analyzed_mtime_ns=observation.analyzed_mtime_ns,
        analyzed_prefix_sha256=digest,
        verified_at_utc=_normalise_utc_timestamp_text(observation.verified_at_utc),
    )


def _select_statistics(
    connection: sqlite3.Connection, session_id: int
) -> tuple[object, ...] | None:
    scalar_columns = ", ".join(_SESSION_PROJECTION_SCALAR_COLUMNS)
    return connection.execute(
        f"SELECT id, rodex_sessions_id, statistics_revision, "
        "statistics_projection_schema_version, calculated_at_utc, coverage_state, "
        f"{scalar_columns} FROM {RODEX_SESSIONS_STATISTICS_TABLE} "
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
    return connection.execute(
        f"SELECT id, rodex_sessions_id, codex_session_id_signed_bigint_1, "
        "codex_session_id_signed_bigint_2, first_linked_at_utc, rollout_file_path, "
        "analyzed_size_bytes, analyzed_mtime_ns, analyzed_prefix_sha256, "
        "verified_at_utc, included_statistics_revision "
        f"FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
        "WHERE rodex_sessions_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()


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
) -> RodexSessionStatistics:
    scalar_values = dict(zip(_SESSION_PROJECTION_SCALAR_COLUMNS, row[6:], strict=True))
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
    named_counts = _named_counts_from_rows(named_count_rows)
    expected_ordinals = tuple(range(len(audit_limit_rows)))
    actual_ordinals = tuple(int(item[0]) for item in audit_limit_rows)
    if actual_ordinals != expected_ordinals:
        raise RodexSessionError("stored statistics audit limits are not contiguous")
    projection = SessionStatisticsProjection(
        **scalar_values,
        distributions=distributions,
        named_counts=named_counts,
        audit_limits=tuple(str(item[1]) for item in audit_limit_rows),
        turn_statistics=(),
    )
    return RodexSessionStatistics(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        statistics_revision=int(row[2]),
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
        codex_session_id=join_signed_bigints_into_a_codex_session_id(
            int(row[2]), int(row[3])
        ),
        first_linked_at_utc=str(row[4]),
        rollout_file_path=None if row[5] is None else str(row[5]),
        analyzed_size_bytes=None if row[6] is None else int(row[6]),
        analyzed_mtime_ns=None if row[7] is None else int(row[7]),
        analyzed_prefix_sha256=None if row[8] is None else str(row[8]),
        verified_at_utc=None if row[9] is None else str(row[9]),
        included_statistics_revision=None if row[10] is None else int(row[10]),
    )


def _turn_statistics_from_rows(
    row: tuple[object, ...],
    named_count_rows: Sequence[tuple[object, ...]],
) -> RodexSessionTurnStatistics:
    values = dict(zip(_TURN_DATABASE_SCALAR_COLUMNS, row[10:], strict=True))
    for key in (
        "hands_on",
        "completed_after_nonzero_command",
        "edited_then_verified",
        "web_research_followed_by_command_or_file_work",
    ):
        values[key] = bool(values[key])
    projection = TurnStatisticsProjection(
        codex_session_id=join_signed_bigints_into_a_codex_session_id(
            int(row[3]), int(row[4])
        ),
        codex_turn_id=str(row[5]),
        started_at_utc=None if row[7] is None else str(row[7]),
        terminal_at_utc=None if row[8] is None else str(row[8]),
        outcome=str(row[9]),
        **values,
        named_counts=_named_counts_from_rows(named_count_rows),
    )
    return RodexSessionTurnStatistics(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        rodex_sessions_statistics_sources_id=int(row[2]),
        included_statistics_revision=int(row[6]),
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
