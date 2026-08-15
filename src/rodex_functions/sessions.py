"""Authoritative creation and lookup pipeline for Rodex session identities."""

from __future__ import annotations

import os
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

RODEX_SESSIONS_TABLE: Final = "rodex_sessions"
RODEX_UUID_UNIQUE_INDEX: Final = "rodex_sessions_uuid_ints_unique"
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
    return path


def create_a_rodex_session(
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSession:
    """Allocate and persist a cryptographically secure 128-bit session identity."""
    path = initialise_rodex_database(database_path)
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
            return RodexSession(id=cursor.lastrowid, rodex_uuid=rodex_uuid)
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
