"""Authoritative creation and lookup pipeline for Rodex session identities."""

from __future__ import annotations

import getpass
import os
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

RODEX_SESSIONS_TABLE: Final = "rodex_sessions"
RODEX_UUID_UNIQUE_INDEX: Final = "rodex_sessions_uuid_ints_unique"
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
_CREATE_LOG_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_LOG_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    created_by_user TEXT NOT NULL,
    last_accessed_at_utc TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
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
class RodexSessionLog:
    """Creation provenance and most recent access for one Rodex session."""

    id: int
    rodex_sessions_id: int
    created_at_utc: str
    created_by_user: str
    last_accessed_at_utc: str


def default_rodex_database_path() -> Path:
    """Resolve the runtime database path for the current Rodex workspace."""
    configured = os.environ.get("RODEX_DATABASE_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.cwd() / ".rodex" / "rodex.sqlite3").resolve()


def initialise_rodex_database(database_path: str | os.PathLike[str] | None = None) -> Path:
    """Create the sealed session table and unique index when absent."""
    path = _normalise_database_path(database_path)
    with _connect(path) as connection:
        connection.execute(_CREATE_TABLE)
        _verify_sealed_table(connection)
        connection.execute(_CREATE_UNIQUE_INDEX)
        _verify_sealed_unique_index(connection)
        connection.execute(_CREATE_LOG_TABLE)
        _verify_sessions_log_table(connection)
        connection.execute(_CREATE_LOG_SESSION_UNIQUE_INDEX)
        _verify_sessions_log_unique_index(connection)
    return path


def create_a_rodex_session(
    database_path: str | os.PathLike[str] | None = None,
    *,
    created_by_user: str | None = None,
) -> RodexSession:
    """Allocate and persist a cryptographically secure 128-bit session identity."""
    path = initialise_rodex_database(database_path)
    user = _normalise_created_by_user(created_by_user)
    created_at_utc = _utc_now_timestamp()
    with _connect(path) as connection:
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
                "(rodex_sessions_id, created_at_utc, created_by_user, "
                "last_accessed_at_utc) VALUES (?, ?, ?, ?)",
                (session.id, created_at_utc, user, created_at_utc),
            )
            return session
    raise RodexSessionUUIDCollisionError(
        "could not allocate a unique Rodex UUID after "
        f"{MAX_UUID_GENERATION_ATTEMPTS} attempts"
    )


def lookup_id_from_a_rodex_uuid(
    rodex_uuid: uuid.UUID | str,
    database_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Return the internal integer id for a Rodex UUID, or ``None`` when absent."""
    path = initialise_rodex_database(database_path)
    uuid_int_1, uuid_int_2 = split_a_rodex_uuid_into_signed_bigints(rodex_uuid)
    with _connect(path) as connection:
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
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id < 1:
        raise ValueError("session_id must be a positive integer")
    path = initialise_rodex_database(database_path)
    with _connect(path) as connection:
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
    with _connect(path) as connection:
        row = connection.execute(
            f"SELECT id, rodex_sessions_id, created_at_utc, created_by_user, "
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
    with _connect(path) as connection:
        cursor = connection.execute(
            f"UPDATE {RODEX_SESSIONS_LOG_TABLE} SET last_accessed_at_utc = ? "
            "WHERE rodex_sessions_id = ?",
            (timestamp, session_id),
        )
        if cursor.rowcount != 1:
            raise RodexSessionError(f"Rodex session log does not exist: {session_id}")
        row = connection.execute(
            f"SELECT id, rodex_sessions_id, created_at_utc, created_by_user, "
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


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            yield connection
    finally:
        connection.close()


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


def _verify_sessions_log_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(
        f"PRAGMA table_info({RODEX_SESSIONS_LOG_TABLE})"
    ).fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("created_at_utc", "TEXT", 1, 0),
        ("created_by_user", "TEXT", 1, 0),
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


def _normalise_created_by_user(created_by_user: str | None) -> str:
    user = getpass.getuser() if created_by_user is None else created_by_user
    if not isinstance(user, str) or not user.strip():
        raise ValueError("created_by_user must be a non-empty string")
    return user.strip()


def _utc_now_timestamp() -> str:
    return _normalise_utc_datetime(datetime.now(UTC))


def _normalise_utc_datetime(value: datetime | None) -> str:
    instant = datetime.now(UTC) if value is None else value
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("UTC timestamps must be timezone-aware")
    return instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_session_id(session_id: int) -> None:
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id < 1:
        raise ValueError("session_id must be a positive integer")


def _session_log_from_row(row: tuple[object, ...]) -> RodexSessionLog:
    return RodexSessionLog(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        created_at_utc=str(row[2]),
        created_by_user=str(row[3]),
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
