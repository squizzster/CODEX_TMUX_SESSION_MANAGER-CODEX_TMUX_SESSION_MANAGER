"""Authoritative creation and lookup pipeline for Rodex session identities."""

from __future__ import annotations

import os
import pwd
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from rodex_sql import open_rodex_transaction, select_or_insert_lookup_id

RODEX_SESSIONS_TABLE: Final = "rodex_sessions"
RODEX_UUID_UNIQUE_INDEX: Final = "rodex_sessions_uuid_ints_unique"
RODEX_SESSIONS_USERS_TABLE: Final = "rodex_sessions_users"
RODEX_SESSIONS_USERS_UNIQUE_INDEX: Final = "rodex_sessions_users_uid_gid_user_name_unique"
RODEX_SESSIONS_LOG_TABLE: Final = "rodex_sessions_log"
RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_log_rodex_sessions_id_unique"
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
    uuid_int_2 BIGINT NOT NULL
)
"""
_CREATE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_UUID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE} (uuid_int_1, uuid_int_2)
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


class RodexSessionError(RuntimeError):
    """The Rodex session registry could not satisfy its contract."""


class RodexSessionUUIDCollisionError(RodexSessionError):
    """Repeated secure UUID candidates collided with existing sessions."""


@dataclass(frozen=True, slots=True)
class RodexSession:
    """The public identity allocated to one Rodex launch."""

    id: int
    rodex_uuid: uuid.UUID

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


def default_rodex_database_path() -> Path:
    """Resolve the runtime database path for the current Rodex workspace."""
    configured = os.environ.get("RODEX_DATABASE_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / ".rodex" / "rodex.sqlite3").resolve()


def initialise_rodex_database(database_path: str | os.PathLike[str] | None = None) -> Path:
    """Create and verify the current Rodex schema in one transaction."""
    path = _normalise_database_path(database_path)
    with open_rodex_transaction(path) as connection:
        connection.execute(_CREATE_TABLE)
        _verify_sealed_table(connection)
        connection.execute(_CREATE_UNIQUE_INDEX)
        _verify_sealed_unique_index(connection)
        connection.execute(_CREATE_USERS_TABLE)
        _verify_sessions_users_table(connection)
        connection.execute(_CREATE_USERS_UNIQUE_INDEX)
        _verify_sessions_users_unique_index(connection)
        connection.execute(_CREATE_LOG_TABLE)
        _verify_sessions_log_table(connection)
        connection.execute(_CREATE_LOG_SESSION_UNIQUE_INDEX)
        _verify_sessions_log_unique_index(connection)
    return path


def create_a_rodex_session(
    database_path: str | os.PathLike[str] | None = None,
    *,
    user_identity: RodexSessionsUserIdentity | None = None,
) -> RodexSession:
    """Atomically persist a secure session, its user lookup, and its log row."""
    path = initialise_rodex_database(database_path)
    identity = (
        current_rodex_sessions_user_identity()
        if user_identity is None
        else _validate_user_identity(user_identity)
    )
    created_at_utc = _utc_now_timestamp()
    with open_rodex_transaction(path) as connection:
        rodex_sessions_users_id = _lookup_or_insert_rodex_sessions_user_id(
            connection, identity
        )
        for _ in range(MAX_UUID_GENERATION_ATTEMPTS):
            rodex_uuid = uuid.UUID(int=secrets.randbits(128))
            uuid_int_1, uuid_int_2 = split_a_rodex_uuid_into_signed_bigints(rodex_uuid)
            try:
                cursor = connection.execute(
                    f"INSERT INTO {RODEX_SESSIONS_TABLE} (uuid_int_1, uuid_int_2) "
                    "VALUES (?, ?)",
                    (uuid_int_1, uuid_int_2),
                )
            except sqlite3.IntegrityError:
                continue
            if cursor.lastrowid is None:
                raise RodexSessionError("SQLite did not return a Rodex session id")
            session = RodexSession(id=cursor.lastrowid, rodex_uuid=rodex_uuid)
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


def split_a_rodex_uuid_into_signed_bigints(
    rodex_uuid: uuid.UUID | str,
) -> tuple[int, int]:
    """Map all 128 UUID bits into SQLite's two signed 64-bit integer values."""
    parsed = _parse_rodex_uuid(rodex_uuid)
    high_unsigned = parsed.int >> _HALF_BITS
    low_unsigned = parsed.int & (_HALF_MODULUS - 1)
    return _unsigned_half_to_signed(high_unsigned), _unsigned_half_to_signed(low_unsigned)


def join_signed_bigints_into_a_rodex_uuid(uuid_int_1: int, uuid_int_2: int) -> uuid.UUID:
    """Reverse the SQLite signed representation into the original 128-bit UUID."""
    high_unsigned = _signed_half_to_unsigned(uuid_int_1)
    low_unsigned = _signed_half_to_unsigned(uuid_int_2)
    return uuid.UUID(int=(high_unsigned << _HALF_BITS) | low_unsigned)


def _normalise_database_path(
    database_path: str | os.PathLike[str] | None,
) -> Path:
    return (
        default_rodex_database_path()
        if database_path is None
        else Path(database_path).expanduser().resolve()
    )


def _verify_sealed_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(f"PRAGMA table_info({RODEX_SESSIONS_TABLE})").fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("uuid_int_1", "BIGINT", 1, 0),
        ("uuid_int_2", "BIGINT", 1, 0),
    ]
    if observed != expected:
        raise RodexSessionError(
            f"sealed {RODEX_SESSIONS_TABLE} schema mismatch: {observed!r}"
        )
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_SESSIONS_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"sealed {RODEX_SESSIONS_TABLE} id must use AUTOINCREMENT")


def _verify_sealed_unique_index(connection: sqlite3.Connection) -> None:
    indexes = connection.execute(f"PRAGMA index_list({RODEX_SESSIONS_TABLE})").fetchall()
    matching_indexes = [row for row in indexes if row[1] == RODEX_UUID_UNIQUE_INDEX]
    index_columns = connection.execute(
        f"PRAGMA index_info({RODEX_UUID_UNIQUE_INDEX})"
    ).fetchall()
    if (
        len(matching_indexes) != 1
        or matching_indexes[0][2] != 1
        or [row[2] for row in index_columns] != ["uuid_int_1", "uuid_int_2"]
    ):
        raise RodexSessionError(
            f"sealed unique index is missing: {RODEX_UUID_UNIQUE_INDEX}"
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


def _lookup_or_insert_rodex_sessions_user_id(
    connection: sqlite3.Connection, identity: RodexSessionsUserIdentity
) -> int:
    return select_or_insert_lookup_id(
        connection,
        RODEX_SESSIONS_USERS_TABLE,
        {"uid": identity.uid, "gid": identity.gid, "user_name": identity.user_name},
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


def _parse_rodex_uuid(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        raise TypeError("rodex_uuid must be a uuid.UUID or string")
    return uuid.UUID(value)


def _unsigned_half_to_signed(value: int) -> int:
    return value - _HALF_MODULUS if value >= _HALF_SIGN_BIT else value


def _signed_half_to_unsigned(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("UUID halves must be integers")
    if not _SIGNED_BIGINT_MIN <= value <= _SIGNED_BIGINT_MAX:
        raise ValueError("UUID halves must fit a signed 64-bit SQLite integer")
    return value + _HALF_MODULUS if value < 0 else value
