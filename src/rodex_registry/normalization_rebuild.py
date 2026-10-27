"""Verified v5-to-v6 reconstruction of derived analytics ownership.

Only this transition may repair published scope projections. Source coordinates,
public identities, typed facts and accepted source digests are never rewritten.
Repaired ownership/lifecycle/checkpoint values and withdrawn request provenance
remain immutable transition evidence. Metrics are recomputed in the same transaction
as both publication heads, without retaining a separate per-metric change history.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace

from rodex_sql import require_active_rodex_transaction

from . import schema as catalog
from .agent_request_reconciliation import _insert_agent_request_from_activity, _reconcile_agent_request_target_turns
from .agent_trace_contract import PreparedAgentTracePublication
from .agent_trace_writer import _resolve_optional_trace_turn_id, _resolve_or_insert_activity_scope
from .errors import RodexSessionStatisticsConflictError, RodexSessionStatisticsPublicationRaceError
from .execution import RodexSessionCodexThread, RodexSessionCodexThreadObservation, validate_codex_thread_observation
from .identity import CodexThreadId, join_signed_bigints_into_a_codex_thread_id
from .normalization_schema import (
    CURRENT_TRACE_VERSION,
    NORMALIZATION_CHANGES_TABLE,
    NORMALIZATION_SOURCES_TABLE,
    NORMALIZATION_TRANSITIONS_TABLE,
    PREVIOUS_TRACE_VERSION,
)
from .statistics_projection import TurnStatisticsProjection, parse_turn_statistics_snapshot, turn_statistics_as_dict
from .turn_statistics_writer import sync_turn_named_counts, upsert_turn_statistics_metrics

_SCOPE = "rodex_sessions_codex_activity_scopes_id"
_EVENT = "rodex_sessions_agent_trace_events_id"
_ITEM = "rodex_sessions_codex_items_id"
_CALL = "rodex_sessions_codex_tool_calls_id"
_SATELLITES = (
    catalog.RODEX_SESSIONS_AGENT_TRACE_MESSAGES_TABLE,
    catalog.RODEX_SESSIONS_AGENT_TRACE_TOOL_CALLS_TABLE,
    catalog.RODEX_SESSIONS_AGENT_TRACE_COMMAND_EXECUTIONS_TABLE,
    catalog.RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE,
)


@dataclass(frozen=True, slots=True)
class AnalyticsNormalizationRebuild:
    """Authenticated replay, including historical roots, offered with both heads."""

    previous_sources: tuple[RodexSessionCodexThread, ...]
    replayed_sources: tuple[RodexSessionCodexThreadObservation, ...]
    turn_statistics: tuple[TurnStatisticsProjection, ...]


def validate_normalization_rebuild(rebuild: AnalyticsNormalizationRebuild) -> AnalyticsNormalizationRebuild:
    """Apply ordinary projection/identity contracts before taking the writer lock."""
    if not isinstance(rebuild, AnalyticsNormalizationRebuild) or not all(
        isinstance(value, tuple)
        for value in (rebuild.previous_sources, rebuild.replayed_sources, rebuild.turn_statistics)
    ):
        raise TypeError("normalization rebuild requires immutable typed replay evidence")
    sources = tuple(validate_codex_thread_observation(source) for source in rebuild.replayed_sources)
    by_thread = {source.codex_thread_id: source for source in sources}
    if len(by_thread) != len(sources):
        raise ValueError("normalization replay contains duplicate source identities")
    previous_ids = [source.codex_thread_id for source in rebuild.previous_sources]
    if len(set(previous_ids)) != len(previous_ids) or not set(previous_ids).issubset(by_thread):
        raise ValueError("normalization replay must include each prior source exactly once")
    for source in sources:
        if source.parent_codex_thread_id is not None:
            parent = by_thread.get(source.parent_codex_thread_id)
            if parent is None or parent.thread_depth + 1 != source.thread_depth:
                raise ValueError("normalization replay has an incomplete parent closure")
    turns = tuple(
        parse_turn_statistics_snapshot(
            {
                "session_id": str(turn.codex_thread_id),
                "turn_id": turn.codex_turn_id,
                "started_at": turn.started_at_utc,
                "terminal_at": turn.terminal_at_utc,
                "outcome": turn.outcome,
                **turn_statistics_as_dict(turn),
            }
        )
        for turn in rebuild.turn_statistics
    )
    if len({(turn.codex_thread_id, turn.codex_turn_id) for turn in turns}) != len(turns):
        raise ValueError("normalization replay contains duplicate turn identities")
    if not {turn.codex_thread_id for turn in turns}.issubset(by_thread):
        raise ValueError("normalization turn belongs to an unverified source")
    return replace(rebuild, replayed_sources=sources, turn_statistics=turns)


class _RepairAudit:
    def __init__(self, connection: sqlite3.Connection, transition_id: int) -> None:
        self.connection = connection
        self.transition_id = transition_id

    def remember(self, table: str, row_id: int, column: str, previous: object, rebuilt: object) -> None:
        if previous == rebuilt:
            return
        self.connection.execute(
            f"INSERT INTO {NORMALIZATION_CHANGES_TABLE} "
            "(normalization_transitions_id, relation_name, relation_row_id, column_name, "
            "previous_integer, rebuilt_integer, previous_text, rebuilt_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.transition_id,
                table,
                row_id,
                column,
                previous if isinstance(previous, int) else None,
                rebuilt if isinstance(rebuilt, int) else None,
                previous if isinstance(previous, str) else None,
                rebuilt if isinstance(rebuilt, str) else None,
            ),
        )

    def update(self, table: str, row_id: int, **values: object) -> None:
        columns = tuple(values)
        row = self.connection.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise RodexSessionStatisticsConflictError("normalization repair lost a projection row")
        if tuple(row) == tuple(values.values()):
            return
        for column, previous in zip(columns, row, strict=True):
            self.remember(table, row_id, column, previous, values[column])
        self.connection.execute(
            f"UPDATE {table} SET {', '.join(f'{column} = ?' for column in columns)} WHERE id = ?",
            (*values.values(), row_id),
        )

    def withdraw(self, table: str, session_id: int) -> list[dict[str, object]]:
        cursor = self.connection.execute(f"SELECT * FROM {table} WHERE rodex_sessions_id = ?", (session_id,))
        columns = [entry[0] for entry in cursor.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        for row in rows:
            for column, value in row.items():
                self.remember(table, int(row["id"]), column, value, None)
        self.connection.execute(f"DELETE FROM {table} WHERE rodex_sessions_id = ?", (session_id,))
        return rows


@contextmanager
def _projection_repair_guards(connection: sqlite3.Connection) -> Iterator[None]:
    """Suspend only canonical projection guards; all other guards remain live."""
    guarded = {
        catalog.RODEX_SESSIONS_AGENT_TRACE_EVENTS_IMMUTABLE_TRIGGER: catalog._CREATE_AGENT_TRACE_EVENTS_IMMUTABLE_TRIGGER,
        catalog.RODEX_SESSIONS_CODEX_ITEMS_IMMUTABLE_TRIGGER: catalog._CREATE_CODEX_ITEMS_IMMUTABLE_TRIGGER,
        catalog.RODEX_SESSIONS_CODEX_TOOL_CALLS_IDENTITY_IMMUTABLE_TRIGGER: (
            catalog._CREATE_CODEX_TOOL_CALLS_IDENTITY_IMMUTABLE_TRIGGER
        ),
    }
    append_only = {
        **dict.fromkeys(_SATELLITES, "published agent trace detail is immutable"),
        catalog.RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE: "Codex item alias identity is immutable",
        catalog.RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE: "Codex tool-call alias identity is immutable",
        catalog.RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE: "subagent spawn provenance is immutable",
        catalog.RODEX_SESSIONS_AGENT_REQUESTS_TABLE: "agent request identity and provenance are immutable",
        catalog.RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE: "agent request target-turn association is immutable",
    }
    for table, diagnostic in append_only.items():
        operations = (
            ("UPDATE", "DELETE")
            if table
            in {catalog.RODEX_SESSIONS_AGENT_REQUESTS_TABLE, catalog.RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE}
            else ("UPDATE",)
        )
        for operation in operations:
            name = f"{table}_reject_{operation.lower()}"
            guarded[name] = f"""
CREATE TRIGGER IF NOT EXISTS {name}
BEFORE {operation} ON {table}
BEGIN
    SELECT RAISE(ABORT, '{diagnostic}');
END
"""
    for name, statement in guarded.items():
        catalog._verify_schema_object_definition_exact(connection, "trigger", name, statement)
    connection.execute("PRAGMA defer_foreign_keys = ON")
    for name in guarded:
        connection.execute(f"DROP TRIGGER {name}")
    try:
        yield
    finally:
        for name, statement in guarded.items():
            connection.execute(statement)
            catalog._verify_schema_object_definition_exact(connection, "trigger", name, statement)
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RodexSessionStatisticsConflictError("normalization rebuild failed foreign-key verification")


def rebuild_normalization_in_transaction(
    connection: sqlite3.Connection,
    session_id: int,
    trace: PreparedAgentTracePublication,
    rebuild: AnalyticsNormalizationRebuild,
) -> None:
    """Verify replay and repair complete derived ownership before ordinary append."""
    require_active_rodex_transaction(connection)
    head = connection.execute(
        "SELECT trace_publication_sequence, trace_schema_version "
        f"FROM {catalog.RODEX_SESSIONS_AGENT_TRACE_PUBLICATIONS_TABLE} "
        "WHERE rodex_sessions_id = ?",
        (session_id,),
    ).fetchone()
    if head is None or head[0] != trace.based_on_trace_publication_sequence:
        raise RodexSessionStatisticsPublicationRaceError("normalization rebuild trace head changed")
    if head[1] != PREVIOUS_TRACE_VERSION or trace.trace_schema_version != CURRENT_TRACE_VERSION:
        raise RodexSessionStatisticsConflictError("unsupported analytics normalization transition")
    catalog.ensure_normalization_schema(connection, install=True)
    memberships = {
        join_signed_bigints_into_a_codex_thread_id(high, low): row_id
        for row_id, high, low in connection.execute(
            "SELECT membership.id, ids.codex_thread_public_id_signed_bigint_1, "
            "ids.codex_thread_public_id_signed_bigint_2 "
            f"FROM {catalog.RODEX_SESSIONS_CODEX_THREADS_TABLE} AS membership "
            f"JOIN {catalog.CODEX_THREADS_TABLE} AS ids ON ids.id = membership.codex_threads_id "
            "WHERE membership.rodex_sessions_id = ?",
            (session_id,),
        )
    }
    source_evidence = _verify_replayed_sources(connection, session_id, rebuild, memberships)
    prepared_by_key = {
        (memberships[event.codex_thread_id], *event.source_key[1:]): event
        for event in trace.events
        if event.codex_thread_id in memberships
    }
    stored_events = connection.execute(
        f"SELECT id, rodex_sessions_codex_threads_id, source_record_ordinal, derived_event_ordinal, "
        f"event_kind, event_time_utc, detail_sha256, {_SCOPE} "
        f"FROM {catalog.RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE} WHERE rodex_sessions_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    for row in stored_events:
        candidate = prepared_by_key.get(tuple(row[1:4]))
        if candidate is None or tuple(row[4:7]) != (
            candidate.event_kind,
            candidate.event.event_time_utc,
            candidate.detail_sha256,
        ):
            raise RodexSessionStatisticsConflictError("normalization replay did not verify every immutable source event")
    inserted = connection.execute(
        f"INSERT INTO {NORMALIZATION_TRANSITIONS_TABLE} "
        "(rodex_sessions_id, previous_trace_schema_version, rebuilt_trace_schema_version, "
        "previous_trace_publication_sequence, rebuilt_trace_publication_sequence, verified_at_utc, "
        "verified_source_count, verified_event_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            head[1],
            trace.trace_schema_version,
            head[0],
            head[0] + 1,
            trace.calculated_at_utc,
            len(source_evidence),
            len(stored_events),
        ),
    )
    audit = _RepairAudit(connection, int(inserted.lastrowid))
    for evidence in source_evidence:
        connection.execute(
            f"INSERT INTO {NORMALIZATION_SOURCES_TABLE} (normalization_transitions_id, "
            "rodex_sessions_codex_rollout_sources_id, accepted_size_bytes, accepted_prefix_sha256, "
            "replayed_size_bytes, replayed_prefix_sha256) VALUES (?, ?, ?, ?, ?, ?)",
            (audit.transition_id, *evidence),
        )
    scope_cache: dict[tuple[int, int | None], int] = {}
    event_scopes: dict[int, int] = {}
    for row in stored_events:
        event = prepared_by_key[tuple(row[1:4])].event
        turn_row_id = _resolve_optional_trace_turn_id(connection, session_id, row[1], event.codex_turn_id)
        event_scopes[row[0]] = _resolve_or_insert_activity_scope(
            connection, session_id=session_id, thread_row_id=row[1], turn_row_id=turn_row_id, cache=scope_cache
        )
    with _projection_repair_guards(connection):
        _repair_scopes(connection, session_id, event_scopes, audit)
        _repair_spawn_turns(connection, session_id, rebuild, memberships, audit)
        _repair_turn_states(connection, session_id, rebuild, memberships, audit)
        _repair_requests(connection, session_id, audit)
    for source in rebuild.replayed_sources:
        membership_id = memberships.get(source.codex_thread_id)
        if membership_id is None:
            continue
        checkpoint = connection.execute(
            "SELECT checkpoints.id "
            f"FROM {catalog.RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE} AS checkpoints "
            f"JOIN {catalog.RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE} AS sources "
            "ON sources.id = checkpoints.rodex_sessions_codex_rollout_sources_id "
            "WHERE sources.rodex_sessions_codex_threads_id = ?",
            (membership_id,),
        ).fetchone()
        if checkpoint is not None:
            audit.update(
                catalog.RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE,
                checkpoint[0],
                analyzed_size_bytes=source.analyzed_size_bytes,
                analyzed_mtime_ns=source.analyzed_mtime_ns,
                analyzed_prefix_sha256=source.analyzed_prefix_sha256,
                verified_at_utc=source.verified_at_utc,
            )
    # The ordinary writer compares all replayed coordinates after this repair and
    # advances the v6 head only together with new metrics and source checkpoints.


def _verify_replayed_sources(
    connection: sqlite3.Connection,
    session_id: int,
    rebuild: AnalyticsNormalizationRebuild,
    memberships: Mapping[CodexThreadId, int],
) -> list[tuple[int, int, str, int, str]]:
    previous = {source.codex_thread_id: source for source in rebuild.previous_sources}
    replayed = {source.codex_thread_id: source for source in rebuild.replayed_sources}
    rows = connection.execute(
        f"SELECT sources.id, sources.rodex_sessions_codex_threads_id, sources.rollout_file_path, "
        "checkpoints.analyzed_size_bytes, checkpoints.analyzed_prefix_sha256 "
        f"FROM {catalog.RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE} AS sources "
        f"JOIN {catalog.RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE} AS checkpoints "
        "ON checkpoints.rodex_sessions_codex_rollout_sources_id = sources.id "
        "WHERE sources.rodex_sessions_id = ?",
        (session_id,),
    ).fetchall()
    by_membership = {row_id: thread_id for thread_id, row_id in memberships.items()}
    evidence = []
    for source_id, membership_id, path, size, digest in rows:
        thread_id = by_membership[membership_id]
        prior, candidate = previous.get(thread_id), replayed.get(thread_id)
        if (
            prior is None
            or candidate is None
            or (prior.id, prior.rollout_file_path, prior.analyzed_size_bytes, prior.analyzed_prefix_sha256)
            != (membership_id, path, size, digest)
        ):
            raise RodexSessionStatisticsPublicationRaceError("normalization source checkpoint changed")
        if str(candidate.rollout_file_path) != path or candidate.analyzed_size_bytes < size:
            raise RodexSessionStatisticsConflictError("normalization replay omitted an authenticated source prefix")
        evidence.append((source_id, size, digest, candidate.analyzed_size_bytes, candidate.analyzed_prefix_sha256))
    if not evidence:
        raise RodexSessionStatisticsConflictError("normalization rebuild has no authenticated source checkpoints")
    return evidence


def _repair_scopes(
    connection: sqlite3.Connection, session_id: int, event_scopes: Mapping[int, int], audit: _RepairAudit
) -> None:
    item_scopes: dict[int, set[int]] = {}
    call_scopes: dict[int, set[int]] = {}
    for table in _SATELLITES:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        extra = [column for column in (_ITEM, _CALL) if column in columns]
        rows = connection.execute(
            f"SELECT id, {_EVENT}{''.join(', ' + column for column in extra)} FROM {table} WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchall()
        for row in rows:
            scope_id = event_scopes[row[1]]
            for column, value in zip(extra, row[2:], strict=True):
                if value is not None:
                    (item_scopes if column == _ITEM else call_scopes).setdefault(value, set()).add(scope_id)
            audit.update(table, row[0], **{_SCOPE: scope_id})
    for owners, table, alias_table, reference in (
        (item_scopes, catalog.RODEX_SESSIONS_CODEX_ITEMS_TABLE, catalog.RODEX_SESSIONS_CODEX_ITEM_ALIASES_TABLE, _ITEM),
        (
            call_scopes,
            catalog.RODEX_SESSIONS_CODEX_TOOL_CALLS_TABLE,
            catalog.RODEX_SESSIONS_CODEX_TOOL_CALL_ALIASES_TABLE,
            _CALL,
        ),
    ):
        for row_id, scopes in owners.items():
            if len(scopes) != 1:
                raise RodexSessionStatisticsConflictError(
                    "normalization replay gives one canonical item/call multiple scopes"
                )
            scope_id = next(iter(scopes))
            audit.update(table, row_id, **{_SCOPE: scope_id})
            for (alias_id,) in connection.execute(f"SELECT id FROM {alias_table} WHERE {reference} = ?", (row_id,)):
                audit.update(alias_table, alias_id, **{_SCOPE: scope_id})
    for event_id, scope_id in event_scopes.items():
        audit.update(catalog.RODEX_SESSIONS_AGENT_TRACE_EVENTS_TABLE, event_id, **{_SCOPE: scope_id})


def _repair_spawn_turns(
    connection: sqlite3.Connection,
    session_id: int,
    rebuild: AnalyticsNormalizationRebuild,
    memberships: Mapping[CodexThreadId, int],
    audit: _RepairAudit,
) -> None:
    for source in rebuild.replayed_sources:
        if source.parent_codex_thread_id is None or source.codex_thread_id not in memberships:
            continue
        row = connection.execute(
            f"SELECT id FROM {catalog.RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} "
            "WHERE subagent_rodex_sessions_codex_threads_id = ?",
            (memberships[source.codex_thread_id],),
        ).fetchone()
        if row is None:
            continue
        turn_id = _resolve_optional_trace_turn_id(
            connection, session_id, memberships[source.parent_codex_thread_id], source.spawning_codex_turn_id
        )
        audit.update(catalog.RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE, row[0], spawning_rodex_sessions_codex_turns_id=turn_id)


def _repair_turn_states(
    connection: sqlite3.Connection,
    session_id: int,
    rebuild: AnalyticsNormalizationRebuild,
    memberships: Mapping[CodexThreadId, int],
    audit: _RepairAudit,
) -> None:
    # Clear lifecycle projections for trace-only identities too; they must not
    # retain a terminal that the authoritative analyzer rejected for no start.
    states = connection.execute(
        f"SELECT id FROM {catalog.RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} WHERE rodex_sessions_id = ?", (session_id,)
    ).fetchall()
    for (state_id,) in states:
        audit.update(
            catalog.RODEX_SESSIONS_CODEX_TURN_STATES_TABLE,
            state_id,
            started_at_utc=None,
            terminal_at_utc=None,
            outcome="open",
        )
    for turn in rebuild.turn_statistics:
        membership = memberships.get(turn.codex_thread_id)
        if membership is None:
            continue  # A newly discovered member is inserted by ordinary publication.
        turn_id = _resolve_optional_trace_turn_id(connection, session_id, membership, turn.codex_turn_id)
        state = connection.execute(
            f"SELECT id FROM {catalog.RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} WHERE rodex_sessions_codex_turns_id = ?",
            (turn_id,),
        ).fetchone()
        if state is None:
            inserted = connection.execute(
                f"INSERT INTO {catalog.RODEX_SESSIONS_CODEX_TURN_STATES_TABLE} "
                "(rodex_sessions_id, rodex_sessions_codex_turns_id, outcome) VALUES (?, ?, 'open')",
                (session_id, turn_id),
            )
            state = (inserted.lastrowid,)
        audit.update(
            catalog.RODEX_SESSIONS_CODEX_TURN_STATES_TABLE,
            state[0],
            started_at_utc=turn.started_at_utc,
            terminal_at_utc=turn.terminal_at_utc,
            outcome=turn.outcome,
        )
        upsert_turn_statistics_metrics(connection, session_id, turn_id, turn)
        sync_turn_named_counts(connection, session_id, turn_id, turn.named_counts)


def _repair_requests(connection: sqlite3.Connection, session_id: int, audit: _RepairAudit) -> None:
    # Preserve complete original identity/provenance before rebuilding the derived
    # parent-message and FIFO associations. A request lacking a verified new parent
    # remains explicitly withdrawn in this ledger; it is never silently reassigned.
    audit.withdraw(catalog.RODEX_SESSIONS_AGENT_REQUEST_TARGET_TURNS_TABLE, session_id)
    previous = audit.withdraw(catalog.RODEX_SESSIONS_AGENT_REQUESTS_TABLE, session_id)
    by_activity = {row["rodex_sessions_agent_trace_subagent_activities_id"]: row for row in previous}
    targets = set()
    rows = connection.execute(
        f"SELECT id, {_SCOPE}, target_codex_threads_id, {_CALL}, activity_kind "
        f"FROM {catalog.RODEX_SESSIONS_AGENT_TRACE_SUBAGENT_ACTIVITIES_TABLE} "
        "WHERE rodex_sessions_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()
    for activity_id, scope_id, target, call_id, kind in rows:
        if target is None or call_id is None:
            continue
        old = by_activity.get(activity_id)
        preserved = (
            None
            if old is None
            else (
                old["id"],
                old["agent_request_public_id_signed_bigint_1"],
                old["agent_request_public_id_signed_bigint_2"],
            )
        )
        _insert_agent_request_from_activity(
            connection,
            session_id=session_id,
            scope_id=scope_id,
            subagent_activity_id=activity_id,
            target_codex_threads_id=target,
            collaboration_tool_call_id=call_id,
            activity_kind=kind,
            preserved_identity=preserved,
        )
        targets.add(target)
    for target in targets:
        _reconcile_agent_request_target_turns(connection, session_id=session_id, target_codex_threads_id=target)
