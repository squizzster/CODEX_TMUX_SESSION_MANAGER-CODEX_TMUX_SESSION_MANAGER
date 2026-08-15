"""Authoritative creation and lookup pipeline for Rodex session identities."""

from __future__ import annotations

import os
import pwd
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from cool_name.functions import (
    allocate_unique_cool_name,
    create_and_verify_cool_names_schema,
    lookup_cool_name,
    normalise_rodex_display_name,
    reserve_specific_cool_name,
)
from rodex_sql import default_rodex_database_path as _default_rodex_database_path
from rodex_sql import (
    normalise_rodex_database_path,
    open_rodex_transaction,
    select_lookup_id,
    select_or_insert_lookup_id,
)

RODEX_SESSIONS_TABLE: Final = "rodex_sessions"
RODEX_UUID_UNIQUE_INDEX: Final = "rodex_sessions_uuid_ints_unique"
RODEX_SESSIONS_USERS_TABLE: Final = "rodex_sessions_users"
RODEX_SESSIONS_USERS_UNIQUE_INDEX: Final = "rodex_sessions_users_uid_gid_user_name_unique"
RODEX_SESSIONS_LOG_TABLE: Final = "rodex_sessions_log"
RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_log_rodex_sessions_id_unique"
)
RODEX_CODEX_UUID_UNIQUE_INDEX: Final = "rodex_sessions_codex_session_uuid_ints_unique"
RODEX_TMUX_SESSIONS_TABLE: Final = "rodex_tmux_sessions"
RODEX_TMUX_SESSIONS_SESSION_UNIQUE_INDEX: Final = (
    "rodex_tmux_sessions_rodex_sessions_id_unique"
)
RODEX_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX: Final = "rodex_tmux_sessions_endpoint_unique"
RODEX_SESSIONS_COOL_NAMES_UNIQUE_INDEX: Final = "rodex_sessions_cool_names_id_unique"
RODEX_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX: Final = (
    "rodex_sessions_user_defined_cool_names_id_unique"
)
MAX_UUID_GENERATION_ATTEMPTS: Final = 8

_HALF_BITS: Final = 64
_HALF_MODULUS: Final = 1 << _HALF_BITS
_HALF_SIGN_BIT: Final = 1 << (_HALF_BITS - 1)
_SIGNED_BIGINT_MIN: Final = -_HALF_SIGN_BIT
_SIGNED_BIGINT_MAX: Final = _HALF_SIGN_BIT - 1

_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid_int_1 BIGINT NOT NULL,
    uuid_int_2 BIGINT NOT NULL,
    codex_session_uuid_int_1 BIGINT NOT NULL,
    codex_session_uuid_int_2 BIGINT NOT NULL,
    cool_names_id INTEGER NOT NULL,
    user_defined_cool_names_id INTEGER DEFAULT NULL,
    FOREIGN KEY (cool_names_id) REFERENCES cool_names (id),
    FOREIGN KEY (user_defined_cool_names_id) REFERENCES cool_names (id)
)
"""
_CREATE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_UUID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE} (uuid_int_1, uuid_int_2)
"""
_CREATE_CODEX_UUID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_CODEX_UUID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE}
    (codex_session_uuid_int_1, codex_session_uuid_int_2)
"""
_CREATE_SESSIONS_COOL_NAMES_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_COOL_NAMES_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE} (cool_names_id)
"""
_CREATE_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE} (user_defined_cool_names_id)
"""
_CREATE_USERS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_USERS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid INTEGER NOT NULL,
    gid INTEGER NOT NULL,
    user_name TEXT NOT NULL
)
"""
_CREATE_USERS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_USERS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_USERS_TABLE} (uid, gid, user_name)
"""
_CREATE_LOG_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_LOG_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    rodex_sessions_users_id INTEGER NOT NULL,
    last_accessed_at_utc TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id),
    FOREIGN KEY (rodex_sessions_users_id) REFERENCES {RODEX_SESSIONS_USERS_TABLE} (id)
)
"""
_CREATE_LOG_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_LOG_TABLE} (rodex_sessions_id)
"""
_CREATE_TMUX_SESSIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_TMUX_SESSIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    tmux_server_socket_path TEXT NOT NULL,
    tmux_session_name TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_TMUX_SESSIONS_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_TMUX_SESSIONS_SESSION_UNIQUE_INDEX}
ON {RODEX_TMUX_SESSIONS_TABLE} (rodex_sessions_id)
"""
_CREATE_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX}
ON {RODEX_TMUX_SESSIONS_TABLE} (tmux_server_socket_path, tmux_session_name)
"""


class RodexSessionError(RuntimeError):
    """The Rodex session registry could not satisfy its contract."""


class RodexSessionUUIDCollisionError(RodexSessionError):
    """Repeated secure UUID candidates collided with existing sessions."""


@dataclass(frozen=True, slots=True)
class RodexSession:
    """The public identity allocated to one Rodex launch."""

    id: int
    rodex_uuid: uuid.UUID
    codex_session_uuid: uuid.UUID
    cool_names_id: int
    cool_name: str

    @property
    def uuid_int_1(self) -> int:
        """Return the unsigned high 64 bits of the public UUID."""
        return self.rodex_uuid.int >> _HALF_BITS

    @property
    def uuid_int_2(self) -> int:
        """Return the unsigned low 64 bits of the public UUID."""
        return self.rodex_uuid.int & (_HALF_MODULUS - 1)


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
    codex_session_uuid: uuid.UUID
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


def default_rodex_database_path() -> Path:
    """Resolve the shared runtime database path for compatibility."""
    return _default_rodex_database_path()


def initialise_rodex_database(database_path: str | os.PathLike[str] | None = None) -> Path:
    """Create and verify the current Rodex schema in one transaction."""
    path = normalise_rodex_database_path(database_path)
    with open_rodex_transaction(path) as connection:
        create_and_verify_cool_names_schema(connection)
        connection.execute(_CREATE_TABLE)
        _verify_sessions_table(connection)
        connection.execute(_CREATE_UNIQUE_INDEX)
        connection.execute(_CREATE_CODEX_UUID_UNIQUE_INDEX)
        connection.execute(_CREATE_SESSIONS_COOL_NAMES_UNIQUE_INDEX)
        connection.execute(_CREATE_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX)
        _verify_sessions_unique_indexes(connection)
        connection.execute(_CREATE_USERS_TABLE)
        _verify_sessions_users_table(connection)
        connection.execute(_CREATE_USERS_UNIQUE_INDEX)
        _verify_sessions_users_unique_index(connection)
        connection.execute(_CREATE_LOG_TABLE)
        _verify_sessions_log_table(connection)
        connection.execute(_CREATE_LOG_SESSION_UNIQUE_INDEX)
        _verify_sessions_log_unique_index(connection)
        connection.execute(_CREATE_TMUX_SESSIONS_TABLE)
        _verify_tmux_sessions_table(connection)
        connection.execute(_CREATE_TMUX_SESSIONS_SESSION_UNIQUE_INDEX)
        connection.execute(_CREATE_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX)
        _verify_tmux_sessions_unique_indexes(connection)
    return path


def create_a_rodex_session(
    database_path: str | os.PathLike[str] | None = None,
    *,
    codex_session_uuid: uuid.UUID | str,
    user_identity: RodexSessionsUserIdentity | None = None,
    tmux_server_socket_path: str | os.PathLike[str] | None = None,
    tmux_session_name: str | None = None,
) -> RodexSession:
    """Atomically persist a session and any live Codex/tmux linkage."""
    path = initialise_rodex_database(database_path)
    identity = (
        current_rodex_sessions_user_identity()
        if user_identity is None
        else _validate_user_identity(user_identity)
    )
    parsed_codex_session_uuid = _parse_uuid(codex_session_uuid, "codex_session_uuid")
    codex_uuid_int_1, codex_uuid_int_2 = split_a_codex_uuid_into_signed_bigints(
        parsed_codex_session_uuid
    )
    tmux_link = _normalise_tmux_link(tmux_server_socket_path, tmux_session_name)
    created_at_utc = _utc_now_timestamp()
    with open_rodex_transaction(path) as connection:
        rodex_sessions_users_id = _lookup_or_insert_rodex_sessions_user_id(
            connection, identity
        )
        if (
            select_lookup_id(
                connection,
                RODEX_SESSIONS_TABLE,
                {
                    "codex_session_uuid_int_1": codex_uuid_int_1,
                    "codex_session_uuid_int_2": codex_uuid_int_2,
                },
            )
            is not None
        ):
            raise RodexSessionError(
                f"Codex session UUID already belongs to a Rodex session: "
                f"{parsed_codex_session_uuid}"
            )
        allocated_name = allocate_unique_cool_name(connection)
        for _ in range(MAX_UUID_GENERATION_ATTEMPTS):
            rodex_uuid = uuid.UUID(int=secrets.randbits(128))
            uuid_int_1, uuid_int_2 = split_a_rodex_uuid_into_signed_bigints(rodex_uuid)
            try:
                cursor = connection.execute(
                    f"INSERT INTO {RODEX_SESSIONS_TABLE} "
                    "(uuid_int_1, uuid_int_2, codex_session_uuid_int_1, "
                    "codex_session_uuid_int_2, cool_names_id) VALUES (?, ?, ?, ?, ?)",
                    (
                        uuid_int_1,
                        uuid_int_2,
                        codex_uuid_int_1,
                        codex_uuid_int_2,
                        allocated_name.id,
                    ),
                )
            except sqlite3.IntegrityError:
                continue
            if cursor.lastrowid is None:
                raise RodexSessionError("SQLite did not return a Rodex session id")
            session = RodexSession(
                id=cursor.lastrowid,
                rodex_uuid=rodex_uuid,
                codex_session_uuid=parsed_codex_session_uuid,
                cool_names_id=allocated_name.id,
                cool_name=allocated_name.cool_name,
            )
            connection.execute(
                f"INSERT INTO {RODEX_SESSIONS_LOG_TABLE} "
                "(rodex_sessions_id, created_at_utc, rodex_sessions_users_id, "
                "last_accessed_at_utc) VALUES (?, ?, ?, ?)",
                (
                    session.id,
                    created_at_utc,
                    rodex_sessions_users_id,
                    created_at_utc,
                ),
            )
            if tmux_link is not None:
                socket_path, session_name = tmux_link
                connection.execute(
                    f"INSERT INTO {RODEX_TMUX_SESSIONS_TABLE} "
                    "(rodex_sessions_id, tmux_server_socket_path, tmux_session_name) "
                    "VALUES (?, ?, ?)",
                    (session.id, socket_path, session_name),
                )
            return session
        raise RodexSessionUUIDCollisionError(
            "could not allocate a unique Rodex UUID after "
            f"{MAX_UUID_GENERATION_ATTEMPTS} attempts"
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
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
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


def lookup_id_from_a_rodex_uuid(
    rodex_uuid: uuid.UUID | str,
    database_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Return the internal integer id for a Rodex UUID, or ``None`` when absent."""
    path = initialise_rodex_database(database_path)
    uuid_int_1, uuid_int_2 = split_a_rodex_uuid_into_signed_bigints(rodex_uuid)
    with open_rodex_transaction(path) as connection:
        row = connection.execute(
            f"SELECT id FROM {RODEX_SESSIONS_TABLE} "
            "WHERE uuid_int_1 = ? AND uuid_int_2 = ?",
            (uuid_int_1, uuid_int_2),
        ).fetchone()
    return None if row is None else int(row[0])


def lookup_rodex_uuid_from_an_id(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> uuid.UUID | None:
    """Return the public Rodex UUID for an internal id, or ``None`` when absent."""
    _validate_positive_id(session_id, "session_id")
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        row = connection.execute(
            f"SELECT uuid_int_1, uuid_int_2 FROM {RODEX_SESSIONS_TABLE} WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return join_signed_bigints_into_a_rodex_uuid(int(row[0]), int(row[1]))


def lookup_rodex_session_log(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionLog | None:
    """Return the one log row belonging to a session, or ``None`` when absent."""
    _validate_session_id(session_id)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
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
    accessed_at_utc: datetime | None = None,
) -> RodexTmuxSession:
    """Atomically replace a resumed session's tmux endpoint and access time."""
    _validate_session_id(session_id)
    tmux_link = _normalise_tmux_link(
        tmux_server_socket_path,
        tmux_session_name,
    )
    if tmux_link is None:  # Both arguments are required by this public contract.
        raise ValueError("a resumed session requires a tmux endpoint")
    socket_path, session_name = tmux_link
    timestamp = _normalise_utc_datetime(accessed_at_utc)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        tmux_cursor = connection.execute(
            f"UPDATE {RODEX_TMUX_SESSIONS_TABLE} "
            "SET tmux_server_socket_path = ?, tmux_session_name = ? "
            "WHERE rodex_sessions_id = ?",
            (socket_path, session_name, session_id),
        )
        if tmux_cursor.rowcount != 1:
            raise RodexSessionError(f"Rodex tmux session does not exist: {session_id}")
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


def lookup_codex_uuid_from_a_rodex_session_id(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> uuid.UUID | None:
    """Return the Codex UUID stored on one Rodex session."""
    _validate_session_id(session_id)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        row = connection.execute(
            f"SELECT codex_session_uuid_int_1, codex_session_uuid_int_2 "
            f"FROM {RODEX_SESSIONS_TABLE} WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return join_signed_bigints_into_a_codex_uuid(int(row[0]), int(row[1]))


def lookup_rodex_tmux_session(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexTmuxSession | None:
    """Return the tmux endpoint linked to one Rodex session."""
    _validate_session_id(session_id)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        return _select_rodex_tmux_session(connection, session_id)


def lookup_rodex_session_id_from_a_cool_name(
    cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Resolve a permanent or user-defined cool name through integer identities."""
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
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


def lookup_owned_rodex_session_id_from_a_cool_name(
    cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    user_identity: RodexSessionsUserIdentity | None = None,
) -> int | None:
    """Resolve a name only when its session belongs to the selected POSIX user."""
    identity = _resolve_user_identity(user_identity)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
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
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
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
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
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
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        user_id = select_lookup_id(
            connection,
            RODEX_SESSIONS_USERS_TABLE,
            {"uid": identity.uid, "gid": identity.gid, "user_name": identity.user_name},
        )
        if user_id is None:
            return []
        rows = connection.execute(
            f"SELECT sessions.id, permanent.cool_name, user_defined.cool_name, "
            "sessions.codex_session_uuid_int_1, "
            "sessions.codex_session_uuid_int_2, tmux.tmux_server_socket_path, "
            "tmux.tmux_session_name "
            f"FROM {RODEX_SESSIONS_TABLE} AS sessions "
            f"JOIN {RODEX_SESSIONS_LOG_TABLE} AS log "
            "ON log.rodex_sessions_id = sessions.id "
            f"JOIN {RODEX_TMUX_SESSIONS_TABLE} AS tmux "
            "ON tmux.rodex_sessions_id = sessions.id "
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
            codex_session_uuid=join_signed_bigints_into_a_codex_uuid(
                int(row[3]), int(row[4])
            ),
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


def lookup_rodex_session_id_from_a_codex_uuid(
    codex_session_uuid: uuid.UUID | str,
    database_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Return the Rodex id linked to a Codex thread UUID."""
    uuid_int_1, uuid_int_2 = split_a_codex_uuid_into_signed_bigints(codex_session_uuid)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        row = connection.execute(
            f"SELECT id FROM {RODEX_SESSIONS_TABLE} "
            "WHERE codex_session_uuid_int_1 = ? "
            "AND codex_session_uuid_int_2 = ?",
            (uuid_int_1, uuid_int_2),
        ).fetchone()
    return None if row is None else int(row[0])


def split_a_rodex_uuid_into_signed_bigints(
    rodex_uuid: uuid.UUID | str,
) -> tuple[int, int]:
    """Map all 128 UUID bits into SQLite's two signed 64-bit integer values."""
    parsed = _parse_uuid(rodex_uuid, "rodex_uuid")
    return _split_uuid_into_signed_bigints(parsed)


def split_a_codex_uuid_into_signed_bigints(
    codex_session_uuid: uuid.UUID | str,
) -> tuple[int, int]:
    """Map all 128 Codex UUID bits into its two signed storage integers."""
    parsed = _parse_uuid(codex_session_uuid, "codex_session_uuid")
    return _split_uuid_into_signed_bigints(parsed)


def _split_uuid_into_signed_bigints(parsed: uuid.UUID) -> tuple[int, int]:
    high_unsigned = parsed.int >> _HALF_BITS
    low_unsigned = parsed.int & (_HALF_MODULUS - 1)
    return _unsigned_half_to_signed(high_unsigned), _unsigned_half_to_signed(low_unsigned)


def join_signed_bigints_into_a_rodex_uuid(uuid_int_1: int, uuid_int_2: int) -> uuid.UUID:
    """Reverse the SQLite signed representation into the original 128-bit UUID."""
    high_unsigned = _signed_half_to_unsigned(uuid_int_1)
    low_unsigned = _signed_half_to_unsigned(uuid_int_2)
    return uuid.UUID(int=(high_unsigned << _HALF_BITS) | low_unsigned)


def join_signed_bigints_into_a_codex_uuid(
    codex_session_uuid_int_1: int,
    codex_session_uuid_int_2: int,
) -> uuid.UUID:
    """Reverse the Codex storage integers into its original 128-bit UUID."""
    high_unsigned = _signed_half_to_unsigned(codex_session_uuid_int_1)
    low_unsigned = _signed_half_to_unsigned(codex_session_uuid_int_2)
    return uuid.UUID(int=(high_unsigned << _HALF_BITS) | low_unsigned)


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


def _verify_sessions_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(f"PRAGMA table_info({RODEX_SESSIONS_TABLE})").fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("uuid_int_1", "BIGINT", 1, 0),
        ("uuid_int_2", "BIGINT", 1, 0),
        ("codex_session_uuid_int_1", "BIGINT", 1, 0),
        ("codex_session_uuid_int_2", "BIGINT", 1, 0),
        ("cool_names_id", "INTEGER", 1, 0),
        ("user_defined_cool_names_id", "INTEGER", 0, 0),
    ]
    if observed != expected:
        raise RodexSessionError(f"{RODEX_SESSIONS_TABLE} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_SESSIONS_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{RODEX_SESSIONS_TABLE} id must use AUTOINCREMENT")
    if columns[-1][4] != "NULL":
        raise RodexSessionError(
            f"{RODEX_SESSIONS_TABLE}.user_defined_cool_names_id must default to NULL"
        )
    foreign_keys = connection.execute(
        f"PRAGMA foreign_key_list({RODEX_SESSIONS_TABLE})"
    ).fetchall()
    observed_foreign_keys = {(row[2], row[3], row[4]) for row in foreign_keys}
    expected_foreign_keys = {
        ("cool_names", "cool_names_id", "id"),
        ("cool_names", "user_defined_cool_names_id", "id"),
    }
    if observed_foreign_keys != expected_foreign_keys:
        raise RodexSessionError(
            f"{RODEX_SESSIONS_TABLE} foreign keys mismatch: {observed_foreign_keys!r}"
        )


def _verify_sessions_unique_indexes(connection: sqlite3.Connection) -> None:
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_UUID_UNIQUE_INDEX,
        ["uuid_int_1", "uuid_int_2"],
    )
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_CODEX_UUID_UNIQUE_INDEX,
        ["codex_session_uuid_int_1", "codex_session_uuid_int_2"],
    )
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_SESSIONS_COOL_NAMES_UNIQUE_INDEX,
        ["cool_names_id"],
    )
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX,
        ["user_defined_cool_names_id"],
    )


def _verify_sessions_users_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(
        f"PRAGMA table_info({RODEX_SESSIONS_USERS_TABLE})"
    ).fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("uid", "INTEGER", 1, 0),
        ("gid", "INTEGER", 1, 0),
        ("user_name", "TEXT", 1, 0),
    ]
    if observed != expected:
        raise RodexSessionError(
            f"{RODEX_SESSIONS_USERS_TABLE} schema mismatch: {observed!r}"
        )
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_SESSIONS_USERS_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{RODEX_SESSIONS_USERS_TABLE} id must use AUTOINCREMENT")


def _verify_sessions_users_unique_index(connection: sqlite3.Connection) -> None:
    indexes = connection.execute(
        f"PRAGMA index_list({RODEX_SESSIONS_USERS_TABLE})"
    ).fetchall()
    matching_indexes = [
        row for row in indexes if row[1] == RODEX_SESSIONS_USERS_UNIQUE_INDEX
    ]
    index_columns = connection.execute(
        f"PRAGMA index_info({RODEX_SESSIONS_USERS_UNIQUE_INDEX})"
    ).fetchall()
    if (
        len(matching_indexes) != 1
        or matching_indexes[0][2] != 1
        or [row[2] for row in index_columns] != ["uid", "gid", "user_name"]
    ):
        raise RodexSessionError(
            "Rodex sessions users unique index is missing: "
            f"{RODEX_SESSIONS_USERS_UNIQUE_INDEX}"
        )


def _verify_sessions_log_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(
        f"PRAGMA table_info({RODEX_SESSIONS_LOG_TABLE})"
    ).fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("created_at_utc", "TEXT", 1, 0),
        ("rodex_sessions_users_id", "INTEGER", 1, 0),
        ("last_accessed_at_utc", "TEXT", 1, 0),
    ]
    if observed != expected:
        raise RodexSessionError(f"{RODEX_SESSIONS_LOG_TABLE} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_SESSIONS_LOG_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{RODEX_SESSIONS_LOG_TABLE} id must use AUTOINCREMENT")
    foreign_keys = connection.execute(
        f"PRAGMA foreign_key_list({RODEX_SESSIONS_LOG_TABLE})"
    ).fetchall()
    observed_foreign_keys = {(row[2], row[3], row[4]) for row in foreign_keys}
    expected_foreign_keys = {
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
        (RODEX_SESSIONS_USERS_TABLE, "rodex_sessions_users_id", "id"),
    }
    if observed_foreign_keys != expected_foreign_keys:
        raise RodexSessionError(
            f"{RODEX_SESSIONS_LOG_TABLE} foreign keys mismatch: {observed_foreign_keys!r}"
        )


def _verify_sessions_log_unique_index(connection: sqlite3.Connection) -> None:
    indexes = connection.execute(
        f"PRAGMA index_list({RODEX_SESSIONS_LOG_TABLE})"
    ).fetchall()
    matching_indexes = [
        row for row in indexes if row[1] == RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX
    ]
    index_columns = connection.execute(
        f"PRAGMA index_info({RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX})"
    ).fetchall()
    if (
        len(matching_indexes) != 1
        or matching_indexes[0][2] != 1
        or [row[2] for row in index_columns] != ["rodex_sessions_id"]
    ):
        raise RodexSessionError(
            "Rodex sessions log unique index is missing: "
            f"{RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX}"
        )


def _verify_tmux_sessions_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("tmux_server_socket_path", "TEXT", 1, 0),
            ("tmux_session_name", "TEXT", 1, 0),
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )


def _verify_tmux_sessions_unique_indexes(connection: sqlite3.Connection) -> None:
    _verify_unique_index(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        RODEX_TMUX_SESSIONS_SESSION_UNIQUE_INDEX,
        ["rodex_sessions_id"],
    )
    _verify_unique_index(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        RODEX_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX,
        ["tmux_server_socket_path", "tmux_session_name"],
    )


def _verify_table_columns(
    connection: sqlite3.Connection,
    table_name: str,
    expected: list[tuple[str, str, int, int]],
) -> None:
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    if observed != expected:
        raise RodexSessionError(f"{table_name} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{table_name} id must use AUTOINCREMENT")


def _verify_single_foreign_key(
    connection: sqlite3.Connection,
    table_name: str,
    expected: tuple[str, str, str],
) -> None:
    foreign_keys = connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
    observed = {(row[2], row[3], row[4]) for row in foreign_keys}
    if observed != {expected}:
        raise RodexSessionError(f"{table_name} foreign keys mismatch: {observed!r}")


def _verify_unique_index(
    connection: sqlite3.Connection,
    table_name: str,
    index_name: str,
    expected_columns: list[str],
) -> None:
    indexes = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    matching = [row for row in indexes if row[1] == index_name]
    columns = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
    if (
        len(matching) != 1
        or matching[0][2] != 1
        or [row[2] for row in columns] != expected_columns
    ):
        raise RodexSessionError(f"unique index is missing: {index_name}")


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
                "use -f or --force to replace it"
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


def _utc_now_timestamp() -> str:
    return _normalise_utc_datetime(datetime.now(UTC))


def _normalise_utc_datetime(value: datetime | None) -> str:
    instant = datetime.now(UTC) if value is None else value
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("UTC timestamps must be timezone-aware")
    return instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_session_id(session_id: int) -> None:
    _validate_positive_id(session_id, "session_id")


def _validate_positive_id(value: int, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _session_log_from_row(row: tuple[object, ...]) -> RodexSessionLog:
    return RodexSessionLog(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        created_at_utc=str(row[2]),
        rodex_sessions_users_id=int(row[3]),
        last_accessed_at_utc=str(row[4]),
    )


def _parse_uuid(value: uuid.UUID | str, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a uuid.UUID or string")
    return uuid.UUID(value)


def _unsigned_half_to_signed(value: int) -> int:
    return value - _HALF_MODULUS if value >= _HALF_SIGN_BIT else value


def _signed_half_to_unsigned(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("UUID halves must be integers")
    if not _SIGNED_BIGINT_MIN <= value <= _SIGNED_BIGINT_MAX:
        raise ValueError("UUID halves must fit a signed 64-bit SQLite integer")
    return value + _HALF_MODULUS if value < 0 else value
