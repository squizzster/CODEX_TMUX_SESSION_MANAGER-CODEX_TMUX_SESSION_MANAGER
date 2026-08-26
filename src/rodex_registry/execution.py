"""Shared execution-lineage contracts independent of derived analytics."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from rodex_sql import open_rodex_read_transaction

from .errors import RodexSessionError
from .identity import (
    CodexSessionId,
    CodexThreadId,
    join_signed_bigints_into_a_codex_thread_id,
    join_signed_bigints_into_a_codex_turn_id,
    parse_codex_thread_id,
    parse_codex_turn_id,
    split_codex_thread_id_into_signed_bigints,
)
from .schema import (
    CODEX_THREADS_TABLE,
    RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE,
    RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE,
    RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE,
    RODEX_SESSIONS_CODEX_THREADS_TABLE,
    RODEX_SESSIONS_CODEX_TURNS_TABLE,
    RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE,
    RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE,
    existing_rodex_database_path,
)
from .validation import (
    _normalise_required_text,
    _normalise_utc_timestamp_text,
    _validate_session_id,
)


@dataclass(frozen=True, slots=True)
class RodexSessionCodexThread:
    """One stable Codex thread plus its current lineage and rollout checkpoint view."""

    id: int
    rodex_sessions_id: int
    codex_thread_id: CodexThreadId
    source_kind: str
    parent_rodex_sessions_codex_threads_id: int | None
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
    history_inheritance_kind: str | None = None


@dataclass(frozen=True, slots=True)
class RodexSessionCodexThreadObservation:
    """Authenticated rollout bytes and lineage used by one calculation."""

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
    history_inheritance_kind: str | None = None


def list_rodex_session_codex_threads(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> tuple[RodexSessionCodexThread, ...]:
    """Read execution lineage and rollout checkpoints without derived statistics."""
    _validate_session_id(session_id)
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        rows = select_codex_threads_in_transaction(connection, session_id)
    return tuple(codex_thread_from_row(row) for row in rows)


def select_codex_threads_in_transaction(
    connection: sqlite3.Connection, session_id: int
) -> list[tuple[object, ...]]:
    """Select every rooted thread and its current worker checkpoint in one query."""
    rows = connection.execute(
        f"WITH RECURSIVE hierarchy(id, parent_id, thread_depth) AS ("
        "SELECT current.rodex_sessions_codex_threads_id, NULL, 0 "
        f"FROM {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} AS current "
        "WHERE current.rodex_sessions_id = ? "
        "UNION ALL "
        "SELECT spawns.subagent_rodex_sessions_codex_threads_id, "
        "spawns.parent_rodex_sessions_codex_threads_id, parent.thread_depth + 1 "
        f"FROM {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} AS spawns "
        "JOIN hierarchy AS parent ON "
        "spawns.parent_rodex_sessions_codex_threads_id = parent.id) "
        f"SELECT sources.id, sources.rodex_sessions_id, "
        "identities.codex_thread_public_id_signed_bigint_1, "
        "identities.codex_thread_public_id_signed_bigint_2, "
        "CASE WHEN hierarchy.parent_id IS NULL "
        "THEN 'root' ELSE 'subagent' END, "
        "hierarchy.parent_id, hierarchy.thread_depth, "
        "spawns.agent_path, spawns.agent_nickname, "
        "CASE WHEN spawns.history_inheritance_kind = 'clean' THEN 0 "
        "ELSE spawns.inherited_history_start_ordinal END, "
        "spawning_turn.codex_turn_id_signed_bigint_1, "
        "spawning_turn.codex_turn_id_signed_bigint_2, "
        "sources.first_linked_at_utc, rollouts.rollout_file_path, "
        "checkpoints.analyzed_size_bytes, checkpoints.analyzed_mtime_ns, "
        "checkpoints.analyzed_prefix_sha256, checkpoints.verified_at_utc, "
        "spawns.history_inheritance_kind "
        f"FROM {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS sources "
        "JOIN hierarchy ON hierarchy.id = sources.id "
        f"JOIN {CODEX_THREADS_TABLE} AS identities "
        "ON identities.id = sources.codex_threads_id "
        f"LEFT JOIN {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} AS spawns "
        "ON spawns.subagent_rodex_sessions_codex_threads_id = sources.id "
        f"LEFT JOIN {RODEX_SESSIONS_CODEX_TURNS_TABLE} AS spawning_turn "
        "ON spawning_turn.id = spawns.spawning_rodex_sessions_codex_turns_id "
        f"LEFT JOIN {RODEX_SESSIONS_CODEX_ROLLOUT_SOURCES_TABLE} AS rollouts "
        "ON rollouts.rodex_sessions_codex_threads_id = sources.id "
        f"LEFT JOIN {RODEX_SESSIONS_ANALYTICS_WORKERS_TABLE} AS workers "
        "ON workers.rodex_sessions_id = sources.rodex_sessions_id "
        f"LEFT JOIN {RODEX_SESSIONS_ANALYTICS_WORKER_THREAD_CHECKPOINTS_TABLE} "
        "AS checkpoints ON checkpoints.rodex_sessions_analytics_workers_id = "
        "workers.id AND checkpoints.rodex_sessions_codex_rollout_sources_id = "
        "rollouts.id ORDER BY sources.id",
        (session_id,),
    ).fetchall()
    current = connection.execute(
        f"SELECT 1 FROM {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} "
        "WHERE rodex_sessions_id = ?",
        (session_id,),
    ).fetchone()
    if current is not None and not rows:
        raise RodexSessionError("current Codex thread hierarchy is not rooted")
    return rows


def codex_thread_from_row(row: tuple[object, ...]) -> RodexSessionCodexThread:
    """Adapt the shared execution-lineage row contract."""
    return RodexSessionCodexThread(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        codex_thread_id=join_signed_bigints_into_a_codex_thread_id(row[2], row[3]),
        source_kind=str(row[4]),
        parent_rodex_sessions_codex_threads_id=(None if row[5] is None else int(row[5])),
        thread_depth=int(row[6]),
        agent_path=None if row[7] is None else str(row[7]),
        agent_nickname=None if row[8] is None else str(row[8]),
        subagent_history_start_ordinal=None if row[9] is None else int(row[9]),
        spawning_codex_turn_id=(
            None
            if row[10] is None
            else str(join_signed_bigints_into_a_codex_turn_id(row[10], row[11]))
        ),
        first_linked_at_utc=str(row[12]),
        rollout_file_path=None if row[13] is None else str(row[13]),
        analyzed_size_bytes=None if row[14] is None else int(row[14]),
        analyzed_mtime_ns=None if row[15] is None else int(row[15]),
        analyzed_prefix_sha256=None if row[16] is None else str(row[16]),
        verified_at_utc=None if row[17] is None else str(row[17]),
        history_inheritance_kind=None if row[18] is None else str(row[18]),
    )


def register_codex_root_thread_in_transaction(
    connection: sqlite3.Connection,
    session_id: int,
    codex_session_id: CodexSessionId,
    first_linked_at_utc: str,
) -> int:
    """Register one root Codex thread inside its owning lifecycle transaction."""
    codex_threads_id = resolve_codex_thread_identity_in_transaction(
        connection, codex_session_id
    )
    row = connection.execute(
        f"SELECT id, rodex_sessions_id FROM {RODEX_SESSIONS_CODEX_THREADS_TABLE} "
        "WHERE codex_threads_id = ?",
        (codex_threads_id,),
    ).fetchone()
    if row is None:
        row = connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_CODEX_THREADS_TABLE} "
            "(rodex_sessions_id, codex_threads_id, first_linked_at_utc) "
            "VALUES (?, ?, ?) RETURNING id, rodex_sessions_id",
            (session_id, codex_threads_id, first_linked_at_utc),
        ).fetchone()
    if row is None:
        raise RodexSessionError("Codex thread membership insertion returned no identity")
    if int(row[1]) != session_id:
        raise RodexSessionError(
            "Codex history already belongs to another Rodex execution lineage: "
            f"{codex_session_id}"
        )
    membership_id = int(row[0])
    if (
        connection.execute(
            f"SELECT 1 FROM {RODEX_SESSIONS_SUBAGENT_SPAWNS_TABLE} "
            "WHERE subagent_rodex_sessions_codex_threads_id = ?",
            (membership_id,),
        ).fetchone()
        is not None
    ):
        raise RodexSessionError("a subagent Codex thread cannot become the current root")
    connection.execute(
        f"INSERT INTO {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} "
        "(rodex_sessions_id, rodex_sessions_codex_threads_id) VALUES (?, ?) "
        "ON CONFLICT(rodex_sessions_id) DO UPDATE SET "
        "rodex_sessions_codex_threads_id = excluded.rodex_sessions_codex_threads_id",
        (session_id, membership_id),
    )
    return membership_id


def resolve_codex_thread_identity_in_transaction(
    connection: sqlite3.Connection,
    codex_thread_id: CodexThreadId,
) -> int:
    """Resolve or insert the sole canonical row for one Codex-owned UUID."""
    parsed_thread_id = parse_codex_thread_id(codex_thread_id)
    stored_codex_thread_id = split_codex_thread_id_into_signed_bigints(parsed_thread_id)
    identity_row = connection.execute(
        f"SELECT id FROM {CODEX_THREADS_TABLE} "
        "WHERE codex_thread_public_id_signed_bigint_1 = ? "
        "AND codex_thread_public_id_signed_bigint_2 = ?",
        stored_codex_thread_id,
    ).fetchone()
    if identity_row is None:
        identity_row = connection.execute(
            f"INSERT INTO {CODEX_THREADS_TABLE} "
            "(codex_thread_public_id_signed_bigint_1, "
            "codex_thread_public_id_signed_bigint_2) VALUES (?, ?) RETURNING id",
            stored_codex_thread_id,
        ).fetchone()
    if identity_row is None:
        raise RodexSessionError("Codex thread identity insertion returned no identity")
    return int(identity_row[0])


def validate_codex_thread_observation(
    observation: RodexSessionCodexThreadObservation,
) -> RodexSessionCodexThreadObservation:
    """Validate one exact worker observation at the shared lineage boundary."""
    if not isinstance(observation, RodexSessionCodexThreadObservation):
        raise TypeError(
            "analyzed_sources must contain RodexSessionCodexThreadObservation values"
        )
    codex_thread_id = parse_codex_thread_id(observation.codex_thread_id)
    source_kind = _normalise_required_text(observation.source_kind, "source_kind")
    if source_kind not in {"root", "subagent"}:
        raise ValueError(f"unsupported Codex thread kind: {source_kind}")
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
        else str(parse_codex_turn_id(observation.spawning_codex_turn_id))
    )
    history_inheritance_kind = observation.history_inheritance_kind
    if history_inheritance_kind not in {None, "clean", "inherited"}:
        raise ValueError("history_inheritance_kind must be clean or inherited")
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
                    history_inheritance_kind,
                )
            )
            or observation.thread_depth != 0
        ):
            raise ValueError("root Codex thread has sub-agent metadata")
    elif (
        parent_codex_thread_id is None
        or observation.thread_depth == 0
        or agent_path is None
        or cutoff is None
        or spawning_codex_turn_id is None
        or history_inheritance_kind is None
    ):
        raise ValueError("sub-agent Codex thread metadata is incomplete")
    if source_kind == "subagent" and (
        (history_inheritance_kind == "clean" and cutoff != 0)
        or (history_inheritance_kind == "inherited" and cutoff is None)
    ):
        raise ValueError("sub-agent history provenance disagrees with its cutoff")
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
    return RodexSessionCodexThreadObservation(
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
        history_inheritance_kind=history_inheritance_kind,
    )
