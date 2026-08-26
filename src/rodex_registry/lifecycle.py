"""Authoritative Rodex session identity and lifecycle pipeline."""

from __future__ import annotations

import os
import pwd
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from cool_name.functions import (
    allocate_unique_cool_name,
    lookup_cool_name,
    normalise_rodex_display_name,
    reserve_specific_cool_name,
)
from rodex_sql import (
    INDEX_RE_TRY_ATTEMPTS,
    index_re_try_attempt_numbers,
    open_rodex_read_transaction,
    open_rodex_transaction,
    select_lookup_id,
    select_or_insert_lookup_id,
)

from .errors import (
    RodexRuntimeIdCollisionError,
    RodexSessionError,
    RodexSessionIdCollisionError,
)
from .execution import register_codex_root_thread_in_transaction
from .identity import (
    CodexSessionId,
    RodexRuntimeId,
    RodexSessionId,
    join_signed_bigints_into_a_codex_session_id,
    parse_codex_session_id,
    parse_rodex_runtime_id,
    parse_rodex_session_id,
    split_codex_session_id_into_signed_bigints,
)
from .schema import (
    CODEX_THREADS_TABLE,
    RODEX_RUNTIME_INSTANCES_TABLE,
    RODEX_SESSIONS_CODEX_THREADS_TABLE,
    RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE,
    RODEX_SESSIONS_LOG_TABLE,
    RODEX_SESSIONS_TABLE,
    RODEX_SESSIONS_USERS_TABLE,
    RODEX_TMUX_SESSIONS_TABLE,
    existing_rodex_database_path,
    initialise_rodex_database,
)
from .validation import (
    _normalise_utc_datetime,
    _utc_now_timestamp,
    _validate_positive_id,
    _validate_session_id,
)


@dataclass(frozen=True, slots=True)
class RodexSession:
    """The public identity allocated to one Rodex launch."""

    rodex_sessions_id: int
    rodex_session_id: RodexSessionId
    codex_session_id: CodexSessionId
    cool_names_id: int
    cool_name: str


@dataclass(frozen=True, slots=True)
class RodexSessionsUserIdentity:
    """A POSIX user's natural lookup key."""

    uid: int
    gid: int
    user_name: str


@dataclass(frozen=True, slots=True)
class RodexSessionsUser:
    """A normalized POSIX user lookup row."""

    id: int
    uid: int
    gid: int
    user_name: str


@dataclass(frozen=True, slots=True)
class RodexSessionLog:
    """Creation provenance and most recent access for one Rodex session."""

    id: int
    rodex_sessions_id: int
    created_at_utc: str
    rodex_sessions_users_id: int
    last_accessed_at_utc: str


@dataclass(frozen=True, slots=True)
class RodexTmuxSession:
    """The tmux endpoint linked one-to-one with a Rodex session."""

    id: int
    rodex_sessions_id: int
    tmux_server_socket_path: str
    tmux_session_name: str


@dataclass(frozen=True, slots=True)
class RodexRuntimeInstance:
    """The exact current runtime incarnation linked to one Rodex session."""

    id: int
    rodex_sessions_id: int
    runtime_id: RodexRuntimeId
    started_at_utc: str


@dataclass(frozen=True, slots=True)
class RodexSessionNames:
    """The permanent and optional user-defined names for one session."""

    rodex_sessions_id: int
    cool_name: str
    user_defined_cool_name: str | None

    @property
    def display_name(self) -> str:
        """Return the user-defined name when present, otherwise the generated name."""
        return self.user_defined_cool_name or self.cool_name


@dataclass(frozen=True, slots=True)
class RodexSessionRuntime:
    """The persisted identities needed to identify one user's live runtime."""

    rodex_sessions_id: int
    cool_name: str
    user_defined_cool_name: str | None
    codex_session_id: CodexSessionId
    tmux_server_socket_path: str
    tmux_session_name: str

    @property
    def display_name(self) -> str:
        """Return the effective user-facing name for this runtime."""
        return self.user_defined_cool_name or self.cool_name


@dataclass(slots=True)
class RodexUserDefinedCoolNameAssignment:
    """One serialized database/tmux name transition prepared for the CLI."""

    names: RodexSessionNames
    tmux_session: RodexTmuxSession | None
    renamed_tmux_session_name: str | None = None


def create_a_rodex_session(
    database_path: str | os.PathLike[str] | None = None,
    *,
    codex_session_id: CodexSessionId | str,
    user_defined_cool_name: str | None = None,
    rodex_session_id: RodexSessionId | str | None = None,
    user_identity: RodexSessionsUserIdentity | None = None,
    tmux_server_socket_path: str | os.PathLike[str] | None = None,
    tmux_session_name: str | None = None,
    runtime_id: RodexRuntimeId | str | None = None,
) -> RodexSession:
    """Atomically persist a session and any live Codex/tmux linkage."""
    path = initialise_rodex_database(database_path)
    identity = (
        current_rodex_sessions_user_identity()
        if user_identity is None
        else _validate_user_identity(user_identity)
    )
    parsed_codex_session_id = parse_codex_session_id(codex_session_id)
    codex_session_id_signed_bigint_1, codex_session_id_signed_bigint_2 = (
        split_codex_session_id_into_signed_bigints(parsed_codex_session_id)
    )
    tmux_link = _normalise_tmux_link(tmux_server_socket_path, tmux_session_name)
    parsed_runtime_id = None if runtime_id is None else parse_rodex_runtime_id(runtime_id)
    if parsed_runtime_id is not None and tmux_link is None:
        raise ValueError("a runtime ID requires a tmux endpoint")
    created_at_utc = _utc_now_timestamp()
    with open_rodex_transaction(path) as connection:
        rodex_sessions_users_id = _lookup_or_insert_rodex_sessions_user_id(
            connection, identity
        )
        existing_row = connection.execute(
            f"SELECT memberships.rodex_sessions_id FROM {CODEX_THREADS_TABLE} AS ids "
            f"JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS memberships "
            "ON memberships.codex_threads_id = ids.id "
            "WHERE ids.codex_thread_public_id_signed_bigint_1 = ? "
            "AND ids.codex_thread_public_id_signed_bigint_2 = ?",
            (codex_session_id_signed_bigint_1, codex_session_id_signed_bigint_2),
        ).fetchone()
        existing_session_id = None if existing_row is None else int(existing_row[0])
        if existing_session_id is not None:
            names_row = _select_rodex_session_names(connection, existing_session_id)
            if names_row is None:
                raise RodexSessionError(f"Rodex session disappeared: {existing_session_id}")
            display_name = _session_names_from_row(names_row).display_name
            raise RodexSessionError(
                f"Codex session already belongs to Rodex {display_name}.\n"
                f"Resume with: rodex {display_name}"
            )
        allocated_name = allocate_unique_cool_name(connection)
        preallocated_session_id = (
            None if rodex_session_id is None else parse_rodex_session_id(rodex_session_id)
        )
        candidates = (
            (preallocated_session_id,)
            if preallocated_session_id is not None
            else (
                RodexSessionId.generate()
                for _attempt_number in index_re_try_attempt_numbers()
            )
        )
        for rodex_session_id_candidate in candidates:
            stored_session_id = rodex_session_id_candidate.as_signed_bigint()
            try:
                cursor = connection.execute(
                    f"INSERT INTO {RODEX_SESSIONS_TABLE} "
                    "(rodex_session_id_signed_bigint, cool_names_id) VALUES (?, ?)",
                    (
                        stored_session_id,
                        allocated_name.id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                occupied = connection.execute(
                    f"SELECT 1 FROM {RODEX_SESSIONS_TABLE} "
                    "WHERE rodex_session_id_signed_bigint = ?",
                    (stored_session_id,),
                ).fetchone()
                if occupied is None:
                    raise
                if preallocated_session_id is not None:
                    raise RodexSessionIdCollisionError(
                        "preallocated Rodex session ID is already occupied: "
                        f"{preallocated_session_id}"
                    ) from error
                continue
            if cursor.lastrowid is None:
                raise RodexSessionError("SQLite did not return a Rodex session id")
            session = RodexSession(
                rodex_sessions_id=cursor.lastrowid,
                rodex_session_id=rodex_session_id_candidate,
                codex_session_id=parsed_codex_session_id,
                cool_names_id=allocated_name.id,
                cool_name=allocated_name.cool_name,
            )
            connection.execute(
                f"INSERT INTO {RODEX_SESSIONS_LOG_TABLE} "
                "(rodex_sessions_id, created_at_utc, rodex_sessions_users_id, "
                "last_accessed_at_utc) VALUES (?, ?, ?, ?)",
                (
                    session.rodex_sessions_id,
                    created_at_utc,
                    rodex_sessions_users_id,
                    created_at_utc,
                ),
            )
            register_codex_root_thread_in_transaction(
                connection,
                session.rodex_sessions_id,
                parsed_codex_session_id,
                created_at_utc,
            )
            if tmux_link is not None:
                socket_path, session_name = tmux_link
                connection.execute(
                    f"INSERT INTO {RODEX_TMUX_SESSIONS_TABLE} "
                    "(rodex_sessions_id, tmux_server_socket_path, tmux_session_name) "
                    "VALUES (?, ?, ?)",
                    (session.rodex_sessions_id, socket_path, session_name),
                )
            if parsed_runtime_id is not None:
                _record_rodex_runtime_instance(
                    connection,
                    session.rodex_sessions_id,
                    parsed_runtime_id,
                    created_at_utc,
                )
            if user_defined_cool_name is not None:
                _apply_user_defined_cool_name_assignment(
                    connection,
                    session.cool_name,
                    user_defined_cool_name,
                    identity,
                    force=False,
                    mutate=True,
                    renamed_tmux_session_name=None,
                )
            return session
        raise RodexSessionIdCollisionError(
            "could not allocate a unique Rodex session ID after "
            f"{INDEX_RE_TRY_ATTEMPTS} attempts"
        )


def generate_an_unregistered_rodex_session_id_candidate(
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionId:
    """Generate an unused but deliberately unreserved identity for a pending launch."""
    return _generate_an_unregistered_rodex_id_candidate(
        database_path,
        id_type=RodexSessionId,
        table_name=RODEX_SESSIONS_TABLE,
        column_name="rodex_session_id_signed_bigint",
        collision_error=RodexSessionIdCollisionError,
        domain_name="Rodex session",
    )


def generate_an_unregistered_rodex_runtime_id_candidate(
    database_path: str | os.PathLike[str] | None = None,
) -> RodexRuntimeId:
    """Generate one unused runtime ID for a pending runtime incarnation."""
    return _generate_an_unregistered_rodex_id_candidate(
        database_path,
        id_type=RodexRuntimeId,
        table_name=RODEX_RUNTIME_INSTANCES_TABLE,
        column_name="runtime_id_signed_bigint",
        collision_error=RodexRuntimeIdCollisionError,
        domain_name="Rodex runtime",
    )


def _generate_an_unregistered_rodex_id_candidate[
    GeneratedRodexId: (RodexSessionId, RodexRuntimeId)
](
    database_path: str | os.PathLike[str] | None,
    *,
    id_type: type[GeneratedRodexId],
    table_name: str,
    column_name: str,
    collision_error: type[RodexSessionError],
    domain_name: str,
) -> GeneratedRodexId:
    """Run the one bounded indexed-selection pipeline for Rodex-owned IDs."""
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        for _attempt_number in index_re_try_attempt_numbers():
            candidate = id_type.generate()
            row = connection.execute(
                f"SELECT 1 FROM {table_name} WHERE {column_name} = ?",
                (candidate.as_signed_bigint(),),
            ).fetchone()
            if row is None:
                return candidate
    raise collision_error(
        f"could not generate an unused {domain_name} ID candidate after "
        f"{INDEX_RE_TRY_ATTEMPTS} attempts"
    )


def current_rodex_sessions_user_identity() -> RodexSessionsUserIdentity:
    """Read the current effective POSIX UID, GID, and account name."""
    if os.name == "nt" or not hasattr(os, "getuid") or not hasattr(os, "getgid"):
        raise RodexSessionError("Rodex requires Linux or a compatible POSIX system")
    uid = os.getuid()
    gid = os.getgid()
    return RodexSessionsUserIdentity(
        uid=uid,
        gid=gid,
        user_name=pwd.getpwuid(uid).pw_name,
    )


def lookup_or_create_rodex_sessions_user(
    uid: int,
    gid: int,
    user_name: str,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionsUser:
    """Select a user lookup row first, inserting it only when absent."""
    identity = _validate_user_identity(
        RodexSessionsUserIdentity(uid=uid, gid=gid, user_name=user_name)
    )
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        user_id = _lookup_or_insert_rodex_sessions_user_id(connection, identity)
    return RodexSessionsUser(
        id=user_id,
        uid=identity.uid,
        gid=identity.gid,
        user_name=identity.user_name,
    )


def lookup_rodex_sessions_user(
    user_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionsUser | None:
    """Return one normalized user by internal id, or ``None`` when absent."""
    _validate_positive_id(user_id, "user_id")
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        row = connection.execute(
            f"SELECT id, uid, gid, user_name FROM {RODEX_SESSIONS_USERS_TABLE} "
            "WHERE id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return RodexSessionsUser(
        id=int(row[0]), uid=int(row[1]), gid=int(row[2]), user_name=str(row[3])
    )


def lookup_rodex_sessions_id_from_a_rodex_session_id(
    rodex_session_id: RodexSessionId | str,
    database_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Return the internal id for a Rodex session ID, or ``None``."""
    path = existing_rodex_database_path(database_path)
    parsed_session_id = parse_rodex_session_id(rodex_session_id)
    with open_rodex_read_transaction(path) as connection:
        row = connection.execute(
            f"SELECT id FROM {RODEX_SESSIONS_TABLE} "
            "WHERE rodex_session_id_signed_bigint = ?",
            (parsed_session_id.as_signed_bigint(),),
        ).fetchone()
    return None if row is None else int(row[0])


def lookup_rodex_session_id_from_a_rodex_sessions_id(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionId | None:
    """Return the public Rodex session ID for an internal ID, or ``None``."""
    _validate_positive_id(session_id, "session_id")
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        row = connection.execute(
            f"SELECT rodex_session_id_signed_bigint "
            f"FROM {RODEX_SESSIONS_TABLE} WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return RodexSessionId.from_signed_bigint(row[0])


def lookup_rodex_session_log(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionLog | None:
    """Return the one log row belonging to a session, or ``None`` when absent."""
    _validate_session_id(session_id)
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        row = connection.execute(
            f"SELECT id, rodex_sessions_id, created_at_utc, rodex_sessions_users_id, "
            f"last_accessed_at_utc FROM {RODEX_SESSIONS_LOG_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
    return None if row is None else _session_log_from_row(row)


def record_a_rodex_session_access(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    accessed_at_utc: datetime | None = None,
) -> RodexSessionLog:
    """Update and return the most recent access timestamp for a session."""
    _validate_session_id(session_id)
    timestamp = _normalise_utc_datetime(accessed_at_utc)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        cursor = connection.execute(
            f"UPDATE {RODEX_SESSIONS_LOG_TABLE} SET last_accessed_at_utc = ? "
            "WHERE rodex_sessions_id = ?",
            (timestamp, session_id),
        )
        if cursor.rowcount != 1:
            raise RodexSessionError(f"Rodex session log does not exist: {session_id}")
        row = connection.execute(
            f"SELECT id, rodex_sessions_id, created_at_utc, rodex_sessions_users_id, "
            f"last_accessed_at_utc FROM {RODEX_SESSIONS_LOG_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise RodexSessionError(f"Rodex session log disappeared: {session_id}")
    return _session_log_from_row(row)


def record_a_rodex_session_runtime_resume(
    session_id: int,
    tmux_server_socket_path: str | os.PathLike[str],
    tmux_session_name: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    codex_session_id: CodexSessionId | str | None = None,
    runtime_id: RodexRuntimeId | str | None = None,
    accessed_at_utc: datetime | None = None,
) -> RodexTmuxSession:
    """Atomically activate a runtime, optionally replacing an unsaved Codex session ID."""
    _validate_session_id(session_id)
    tmux_link = _normalise_tmux_link(
        tmux_server_socket_path,
        tmux_session_name,
    )
    if tmux_link is None:  # Both arguments are required by this public contract.
        raise ValueError("a resumed session requires a tmux endpoint")
    socket_path, session_name = tmux_link
    codex_session_id_halves = (
        None
        if codex_session_id is None
        else split_codex_session_id_into_signed_bigints(codex_session_id)
    )
    parsed_runtime_id = None if runtime_id is None else parse_rodex_runtime_id(runtime_id)
    timestamp = _normalise_utc_datetime(accessed_at_utc)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        if codex_session_id_halves is not None:
            parsed_codex_session_id = parse_codex_session_id(codex_session_id)
            if (
                connection.execute(
                    f"SELECT 1 FROM {RODEX_SESSIONS_TABLE} WHERE id = ?", (session_id,)
                ).fetchone()
                is None
            ):
                raise RodexSessionError(f"Rodex session does not exist: {session_id}")
            register_codex_root_thread_in_transaction(
                connection,
                session_id,
                parsed_codex_session_id,
                timestamp,
            )
        tmux_cursor = connection.execute(
            f"UPDATE {RODEX_TMUX_SESSIONS_TABLE} "
            "SET tmux_server_socket_path = ?, tmux_session_name = ? "
            "WHERE rodex_sessions_id = ?",
            (socket_path, session_name, session_id),
        )
        if tmux_cursor.rowcount != 1:
            raise RodexSessionError(f"Rodex tmux session does not exist: {session_id}")
        if parsed_runtime_id is not None:
            _record_rodex_runtime_instance(
                connection,
                session_id,
                parsed_runtime_id,
                timestamp,
            )
        log_cursor = connection.execute(
            f"UPDATE {RODEX_SESSIONS_LOG_TABLE} SET last_accessed_at_utc = ? "
            "WHERE rodex_sessions_id = ?",
            (timestamp, session_id),
        )
        if log_cursor.rowcount != 1:
            raise RodexSessionError(f"Rodex session log does not exist: {session_id}")
        row = connection.execute(
            f"SELECT id, rodex_sessions_id, tmux_server_socket_path, "
            f"tmux_session_name FROM {RODEX_TMUX_SESSIONS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise RodexSessionError(f"Rodex tmux session disappeared: {session_id}")
    return RodexTmuxSession(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        tmux_server_socket_path=str(row[2]),
        tmux_session_name=str(row[3]),
    )


def lookup_rodex_runtime_instance(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexRuntimeInstance | None:
    """Return the exact persisted current runtime incarnation, when recorded."""
    _validate_session_id(session_id)
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        row = connection.execute(
            f"SELECT id, rodex_sessions_id, runtime_id_signed_bigint, started_at_utc "
            f"FROM {RODEX_RUNTIME_INSTANCES_TABLE} WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return RodexRuntimeInstance(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        runtime_id=RodexRuntimeId.from_signed_bigint(row[2]),
        started_at_utc=str(row[3]),
    )


def _record_rodex_runtime_instance(
    connection: sqlite3.Connection,
    session_id: int,
    runtime_id: RodexRuntimeId,
    started_at_utc: str,
) -> None:
    """Persist one exact current incarnation through its indexed 64-bit ID."""
    stored_runtime_id = runtime_id.as_signed_bigint()
    try:
        connection.execute(
            f"INSERT INTO {RODEX_RUNTIME_INSTANCES_TABLE} "
            "(rodex_sessions_id, runtime_id_signed_bigint, started_at_utc) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(rodex_sessions_id) DO UPDATE SET "
            "runtime_id_signed_bigint = excluded.runtime_id_signed_bigint, "
            "started_at_utc = excluded.started_at_utc",
            (session_id, stored_runtime_id, started_at_utc),
        )
    except sqlite3.IntegrityError as error:
        occupied = connection.execute(
            f"SELECT rodex_sessions_id FROM {RODEX_RUNTIME_INSTANCES_TABLE} "
            "WHERE runtime_id_signed_bigint = ?",
            (stored_runtime_id,),
        ).fetchone()
        if occupied is None or int(occupied[0]) == session_id:
            raise
        raise RodexRuntimeIdCollisionError(
            f"preallocated Rodex runtime ID is already occupied: {runtime_id}"
        ) from error


def lookup_codex_session_id_from_a_rodex_sessions_id(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> CodexSessionId | None:
    """Return the Codex session ID stored on one Rodex session."""
    _validate_session_id(session_id)
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        row = connection.execute(
            "SELECT identities.codex_thread_public_id_signed_bigint_1, "
            "identities.codex_thread_public_id_signed_bigint_2 "
            f"FROM {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} AS current "
            f"JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS memberships "
            "ON memberships.id = current.rodex_sessions_codex_threads_id "
            f"JOIN {CODEX_THREADS_TABLE} AS identities "
            "ON identities.id = memberships.codex_threads_id "
            "WHERE current.rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return join_signed_bigints_into_a_codex_session_id(row[0], row[1])


def lookup_rodex_tmux_session(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexTmuxSession | None:
    """Return the tmux endpoint linked to one Rodex session."""
    _validate_session_id(session_id)
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        return _select_rodex_tmux_session(connection, session_id)


def lookup_rodex_sessions_id_from_a_cool_name(
    cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Resolve a permanent or user-defined cool name through integer identities."""
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        allocated_name = lookup_cool_name(connection, cool_name)
        if allocated_name is None:
            return None
        rows = connection.execute(
            f"SELECT id FROM {RODEX_SESSIONS_TABLE} "
            "WHERE cool_names_id = ? OR user_defined_cool_names_id = ? "
            "ORDER BY id LIMIT 2",
            (allocated_name.id, allocated_name.id),
        ).fetchall()
    if len(rows) > 1:
        raise RodexSessionError(f"cool name resolves to multiple sessions: {cool_name}")
    return None if not rows else int(rows[0][0])


def lookup_owned_rodex_sessions_id_from_a_cool_name(
    cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    user_identity: RodexSessionsUserIdentity | None = None,
) -> int | None:
    """Resolve a name only when its session belongs to the selected POSIX user."""
    identity = _resolve_user_identity(user_identity)
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        allocated_name = lookup_cool_name(connection, cool_name)
        if allocated_name is None:
            return None
        rows = _select_sessions_and_owners_by_cool_names_id(connection, allocated_name.id)
        if not rows:
            return None
        if len(rows) > 1:
            raise RodexSessionError(f"cool name resolves to multiple sessions: {cool_name}")
        user_id = _lookup_rodex_sessions_user_id(connection, identity)
        if user_id is None or int(rows[0][5]) != user_id:
            raise RodexSessionError(
                f"Rodex session is not owned by the current user: {cool_name}"
            )
        return int(rows[0][0])


def lookup_rodex_session_names(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionNames | None:
    """Return the permanent and optional user-defined names for one session."""
    _validate_session_id(session_id)
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        row = _select_rodex_session_names(connection, session_id)
    return None if row is None else _session_names_from_row(row)


def assign_a_user_defined_cool_name(
    session_cool_name: str,
    user_defined_cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
    user_identity: RodexSessionsUserIdentity | None = None,
    renamed_tmux_session_name: str | None = None,
) -> RodexSessionNames:
    """Atomically assign one name and an already-renamed live tmux endpoint."""
    if not isinstance(force, bool):
        raise TypeError("force must be a boolean")
    identity = _resolve_user_identity(user_identity)
    persisted_tmux_name = (
        None
        if renamed_tmux_session_name is None
        else _normalise_tmux_session_name(renamed_tmux_session_name)
    )
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        return _apply_user_defined_cool_name_assignment(
            connection,
            session_cool_name,
            user_defined_cool_name,
            identity,
            force=force,
            mutate=True,
            renamed_tmux_session_name=persisted_tmux_name,
        )


def validate_a_user_defined_cool_name_assignment(
    session_cool_name: str,
    user_defined_cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
    user_identity: RodexSessionsUserIdentity | None = None,
) -> RodexSessionNames:
    """Validate an assignment without inserting or updating any lookup row."""
    if not isinstance(force, bool):
        raise TypeError("force must be a boolean")
    identity = _resolve_user_identity(user_identity)
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        return _apply_user_defined_cool_name_assignment(
            connection,
            session_cool_name,
            user_defined_cool_name,
            identity,
            force=force,
            mutate=False,
            renamed_tmux_session_name=None,
        )


@contextmanager
def open_a_user_defined_cool_name_assignment(
    session_cool_name: str,
    user_defined_cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
    user_identity: RodexSessionsUserIdentity | None = None,
) -> Iterator[RodexUserDefinedCoolNameAssignment]:
    """Serialize validation, a caller's live rename, and the durable assignment."""
    if not isinstance(force, bool):
        raise TypeError("force must be a boolean")
    identity = _resolve_user_identity(user_identity)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        planned_names = _apply_user_defined_cool_name_assignment(
            connection,
            session_cool_name,
            user_defined_cool_name,
            identity,
            force=force,
            mutate=False,
            renamed_tmux_session_name=None,
        )
        transition = RodexUserDefinedCoolNameAssignment(
            names=planned_names,
            tmux_session=_select_rodex_tmux_session(
                connection, planned_names.rodex_sessions_id
            ),
        )
        yield transition
        persisted_tmux_name = (
            None
            if transition.renamed_tmux_session_name is None
            else _normalise_tmux_session_name(transition.renamed_tmux_session_name)
        )
        transition.names = _apply_user_defined_cool_name_assignment(
            connection,
            session_cool_name,
            user_defined_cool_name,
            identity,
            force=force,
            mutate=True,
            renamed_tmux_session_name=persisted_tmux_name,
        )


def list_rodex_session_runtimes_for_a_user(
    database_path: str | os.PathLike[str] | None = None,
    *,
    user_identity: RodexSessionsUserIdentity | None = None,
) -> list[RodexSessionRuntime]:
    """List persisted runtime identities owned by one POSIX user."""
    identity = (
        current_rodex_sessions_user_identity()
        if user_identity is None
        else _validate_user_identity(user_identity)
    )
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        user_id = select_lookup_id(
            connection,
            RODEX_SESSIONS_USERS_TABLE,
            {"uid": identity.uid, "gid": identity.gid, "user_name": identity.user_name},
        )
        if user_id is None:
            return []
        rows = connection.execute(
            f"SELECT sessions.id, permanent.cool_name, user_defined.cool_name, "
            "identities.codex_thread_public_id_signed_bigint_1, "
            "identities.codex_thread_public_id_signed_bigint_2, "
            "tmux.tmux_server_socket_path, "
            "tmux.tmux_session_name "
            f"FROM {RODEX_SESSIONS_TABLE} AS sessions "
            f"JOIN {RODEX_SESSIONS_LOG_TABLE} AS log "
            "ON log.rodex_sessions_id = sessions.id "
            f"JOIN {RODEX_TMUX_SESSIONS_TABLE} AS tmux "
            "ON tmux.rodex_sessions_id = sessions.id "
            f"JOIN {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} AS current "
            "ON current.rodex_sessions_id = sessions.id "
            f"JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS memberships "
            "ON memberships.id = current.rodex_sessions_codex_threads_id "
            f"JOIN {CODEX_THREADS_TABLE} AS identities "
            "ON identities.id = memberships.codex_threads_id "
            "JOIN cool_names AS permanent ON permanent.id = sessions.cool_names_id "
            "LEFT JOIN cool_names AS user_defined "
            "ON user_defined.id = sessions.user_defined_cool_names_id "
            "WHERE log.rodex_sessions_users_id = ? ORDER BY sessions.id",
            (user_id,),
        ).fetchall()
    return [
        RodexSessionRuntime(
            rodex_sessions_id=int(row[0]),
            cool_name=str(row[1]),
            user_defined_cool_name=None if row[2] is None else str(row[2]),
            codex_session_id=join_signed_bigints_into_a_codex_session_id(row[3], row[4]),
            tmux_server_socket_path=str(row[5]),
            tmux_session_name=str(row[6]),
        )
        for row in rows
    ]


def update_rodex_tmux_session_name(
    session_id: int,
    tmux_session_name: str,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexTmuxSession:
    """Record a renamed tmux endpoint for one Rodex session."""
    _validate_session_id(session_id)
    session_name = _normalise_tmux_session_name(tmux_session_name)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        cursor = connection.execute(
            f"UPDATE {RODEX_TMUX_SESSIONS_TABLE} SET tmux_session_name = ? "
            "WHERE rodex_sessions_id = ?",
            (session_name, session_id),
        )
        if cursor.rowcount != 1:
            raise RodexSessionError(f"Rodex tmux session does not exist: {session_id}")
        row = connection.execute(
            f"SELECT id, rodex_sessions_id, tmux_server_socket_path, "
            f"tmux_session_name FROM {RODEX_TMUX_SESSIONS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise RodexSessionError(f"Rodex tmux session disappeared: {session_id}")
    return RodexTmuxSession(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        tmux_server_socket_path=str(row[2]),
        tmux_session_name=str(row[3]),
    )


def lookup_rodex_sessions_id_from_a_codex_session_id(
    codex_session_id: CodexSessionId | str,
    database_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Return the internal Rodex session ID linked to a Codex session ID."""
    codex_session_id_part_1, codex_session_id_part_2 = (
        split_codex_session_id_into_signed_bigints(codex_session_id)
    )
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        row = connection.execute(
            f"SELECT current.rodex_sessions_id FROM {CODEX_THREADS_TABLE} AS ids "
            f"JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS memberships "
            "ON memberships.codex_threads_id = ids.id "
            f"JOIN {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} AS current "
            "ON current.rodex_sessions_codex_threads_id = memberships.id "
            "WHERE ids.codex_thread_public_id_signed_bigint_1 = ? "
            "AND ids.codex_thread_public_id_signed_bigint_2 = ?",
            (codex_session_id_part_1, codex_session_id_part_2),
        ).fetchone()
    return None if row is None else int(row[0])


def lookup_owned_rodex_sessions_id_from_a_codex_session_id(
    codex_session_id: CodexSessionId | str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    user_identity: RodexSessionsUserIdentity | None = None,
) -> int | None:
    """Resolve a Codex identity only when its Rodex session has the selected owner."""
    identity = _resolve_user_identity(user_identity)
    codex_session_id_part_1, codex_session_id_part_2 = (
        split_codex_session_id_into_signed_bigints(codex_session_id)
    )
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        row = connection.execute(
            f"SELECT sessions.id, log.rodex_sessions_users_id "
            f"FROM {CODEX_THREADS_TABLE} AS ids "
            f"JOIN {RODEX_SESSIONS_CODEX_THREADS_TABLE} AS memberships "
            "ON memberships.codex_threads_id = ids.id "
            f"JOIN {RODEX_SESSIONS_CURRENT_CODEX_THREADS_TABLE} AS current "
            "ON current.rodex_sessions_codex_threads_id = memberships.id "
            f"JOIN {RODEX_SESSIONS_TABLE} AS sessions "
            "ON sessions.id = current.rodex_sessions_id "
            f"JOIN {RODEX_SESSIONS_LOG_TABLE} AS log "
            "ON log.rodex_sessions_id = sessions.id "
            "WHERE ids.codex_thread_public_id_signed_bigint_1 = ? "
            "AND ids.codex_thread_public_id_signed_bigint_2 = ?",
            (codex_session_id_part_1, codex_session_id_part_2),
        ).fetchone()
        if row is None:
            return None
        user_id = select_lookup_id(
            connection,
            RODEX_SESSIONS_USERS_TABLE,
            {"uid": identity.uid, "gid": identity.gid, "user_name": identity.user_name},
        )
    if user_id is None or int(row[1]) != user_id:
        raise RodexSessionError(
            "Rodex session is not owned by the current user: "
            f"{parse_codex_session_id(codex_session_id)}"
        )
    return int(row[0])


def _normalise_tmux_link(
    tmux_server_socket_path: str | os.PathLike[str] | None,
    tmux_session_name: str | None,
) -> tuple[str, str] | None:
    values = (tmux_server_socket_path, tmux_session_name)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError(
            "tmux_server_socket_path and tmux_session_name must be provided together"
        )
    socket_path = os.fspath(tmux_server_socket_path)
    if not socket_path.strip():
        raise ValueError("tmux_server_socket_path must be non-empty")
    return socket_path, _normalise_tmux_session_name(tmux_session_name)


def _normalise_tmux_session_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("tmux_session_name must be a non-empty string")
    return value.strip()


def _lookup_or_insert_rodex_sessions_user_id(
    connection: sqlite3.Connection, identity: RodexSessionsUserIdentity
) -> int:
    return select_or_insert_lookup_id(
        connection,
        RODEX_SESSIONS_USERS_TABLE,
        {"uid": identity.uid, "gid": identity.gid, "user_name": identity.user_name},
    )


def _apply_user_defined_cool_name_assignment(
    connection: sqlite3.Connection,
    session_cool_name: str,
    user_defined_cool_name: str,
    identity: RodexSessionsUserIdentity,
    *,
    force: bool,
    mutate: bool,
    renamed_tmux_session_name: str | None,
) -> RodexSessionNames:
    normalised_alias = normalise_rodex_display_name(user_defined_cool_name)
    requested_session_name = lookup_cool_name(connection, session_cool_name)
    if requested_session_name is None:
        raise RodexSessionError(f"Rodex session does not exist: {session_cool_name}")
    session_rows = _select_sessions_and_owners_by_cool_names_id(
        connection, requested_session_name.id
    )
    if not session_rows:
        raise RodexSessionError(f"Rodex session does not exist: {session_cool_name}")
    if len(session_rows) > 1:
        raise RodexSessionError(
            f"cool name resolves to multiple sessions: {session_cool_name}"
        )
    session_row = session_rows[0]
    user_id = _lookup_rodex_sessions_user_id(connection, identity)
    if user_id is None or int(session_row[5]) != user_id:
        raise RodexSessionError(
            f"Rodex session is not owned by the current user: {session_cool_name}"
        )

    existing_alias_id = None if session_row[2] is None else int(session_row[2])
    candidate_alias = lookup_cool_name(connection, normalised_alias)
    if candidate_alias is None or existing_alias_id != candidate_alias.id:
        if existing_alias_id is not None and not force:
            raise RodexSessionError(
                f"Rodex session already has user-defined name {session_row[4]!r}; "
                "use --force to replace it"
            )
        if candidate_alias is not None:
            owners = _select_session_ids_by_cool_names_id(connection, candidate_alias.id)
            if any(int(owner[0]) != int(session_row[0]) for owner in owners):
                raise RodexSessionError(
                    f"Rodex name already belongs to another session: {normalised_alias}"
                )

    planned_names = RodexSessionNames(
        rodex_sessions_id=int(session_row[0]),
        cool_name=str(session_row[3]),
        user_defined_cool_name=normalised_alias,
    )
    if not mutate:
        return planned_names

    allocated_alias = reserve_specific_cool_name(connection, normalised_alias)
    owners = _select_session_ids_by_cool_names_id(connection, allocated_alias.id)
    if any(int(owner[0]) != int(session_row[0]) for owner in owners):
        raise RodexSessionError(
            f"Rodex name already belongs to another session: {normalised_alias}"
        )
    cursor = connection.execute(
        f"UPDATE {RODEX_SESSIONS_TABLE} SET user_defined_cool_names_id = ? WHERE id = ?",
        (allocated_alias.id, int(session_row[0])),
    )
    if cursor.rowcount != 1:
        raise RodexSessionError(f"Rodex session disappeared: {int(session_row[0])}")
    if renamed_tmux_session_name is not None:
        tmux_cursor = connection.execute(
            f"UPDATE {RODEX_TMUX_SESSIONS_TABLE} SET tmux_session_name = ? "
            "WHERE rodex_sessions_id = ?",
            (renamed_tmux_session_name, int(session_row[0])),
        )
        if tmux_cursor.rowcount != 1:
            raise RodexSessionError(
                f"Rodex tmux session does not exist: {int(session_row[0])}"
            )
    return planned_names


def _select_sessions_and_owners_by_cool_names_id(
    connection: sqlite3.Connection, cool_names_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT sessions.id, sessions.cool_names_id, "
        "sessions.user_defined_cool_names_id, permanent.cool_name, "
        "user_defined.cool_name, log.rodex_sessions_users_id "
        f"FROM {RODEX_SESSIONS_TABLE} AS sessions "
        "JOIN cool_names AS permanent ON permanent.id = sessions.cool_names_id "
        "LEFT JOIN cool_names AS user_defined "
        "ON user_defined.id = sessions.user_defined_cool_names_id "
        f"JOIN {RODEX_SESSIONS_LOG_TABLE} AS log "
        "ON log.rodex_sessions_id = sessions.id "
        "WHERE sessions.cool_names_id = ? "
        "OR sessions.user_defined_cool_names_id = ? ORDER BY sessions.id LIMIT 2",
        (cool_names_id, cool_names_id),
    ).fetchall()


def _select_session_ids_by_cool_names_id(
    connection: sqlite3.Connection, cool_names_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT id FROM {RODEX_SESSIONS_TABLE} "
        "WHERE cool_names_id = ? OR user_defined_cool_names_id = ? "
        "ORDER BY id LIMIT 2",
        (cool_names_id, cool_names_id),
    ).fetchall()


def _lookup_rodex_sessions_user_id(
    connection: sqlite3.Connection, identity: RodexSessionsUserIdentity
) -> int | None:
    return select_lookup_id(
        connection,
        RODEX_SESSIONS_USERS_TABLE,
        {"uid": identity.uid, "gid": identity.gid, "user_name": identity.user_name},
    )


def _resolve_user_identity(
    user_identity: RodexSessionsUserIdentity | None,
) -> RodexSessionsUserIdentity:
    return (
        current_rodex_sessions_user_identity()
        if user_identity is None
        else _validate_user_identity(user_identity)
    )


def _select_rodex_session_names(
    connection: sqlite3.Connection, session_id: int
) -> tuple[object, ...] | None:
    return connection.execute(
        f"SELECT sessions.id, permanent.cool_name, user_defined.cool_name "
        f"FROM {RODEX_SESSIONS_TABLE} AS sessions "
        "JOIN cool_names AS permanent ON permanent.id = sessions.cool_names_id "
        "LEFT JOIN cool_names AS user_defined "
        "ON user_defined.id = sessions.user_defined_cool_names_id "
        "WHERE sessions.id = ?",
        (session_id,),
    ).fetchone()


def _select_rodex_tmux_session(
    connection: sqlite3.Connection, session_id: int
) -> RodexTmuxSession | None:
    row = connection.execute(
        f"SELECT id, rodex_sessions_id, tmux_server_socket_path, tmux_session_name "
        f"FROM {RODEX_TMUX_SESSIONS_TABLE} WHERE rodex_sessions_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return RodexTmuxSession(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        tmux_server_socket_path=str(row[2]),
        tmux_session_name=str(row[3]),
    )


def _session_names_from_row(row: tuple[object, ...]) -> RodexSessionNames:
    return RodexSessionNames(
        rodex_sessions_id=int(row[0]),
        cool_name=str(row[1]),
        user_defined_cool_name=None if row[2] is None else str(row[2]),
    )


def _validate_user_identity(
    identity: RodexSessionsUserIdentity,
) -> RodexSessionsUserIdentity:
    if not isinstance(identity, RodexSessionsUserIdentity):
        raise TypeError("user_identity must be a RodexSessionsUserIdentity")
    if (
        not isinstance(identity.uid, int)
        or isinstance(identity.uid, bool)
        or identity.uid < 0
    ):
        raise ValueError("uid must be a non-negative integer")
    if (
        not isinstance(identity.gid, int)
        or isinstance(identity.gid, bool)
        or identity.gid < 0
    ):
        raise ValueError("gid must be a non-negative integer")
    if not isinstance(identity.user_name, str) or not identity.user_name.strip():
        raise ValueError("user_name must be a non-empty string")
    return RodexSessionsUserIdentity(
        uid=identity.uid,
        gid=identity.gid,
        user_name=identity.user_name.strip(),
    )


def _session_log_from_row(row: tuple[object, ...]) -> RodexSessionLog:
    return RodexSessionLog(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        created_at_utc=str(row[2]),
        rodex_sessions_users_id=int(row[3]),
        last_accessed_at_utc=str(row[4]),
    )
