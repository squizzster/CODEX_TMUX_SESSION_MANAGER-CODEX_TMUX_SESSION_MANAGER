"""Authoritative allocation and lookup pipeline for human-friendly names."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import coolname

from rodex_sql import (
    normalise_rodex_database_path,
    open_rodex_transaction,
    select_lookup_id,
)

COOL_NAMES_TABLE: Final = "cool_names"
COOL_NAMES_MD5_INTS_UNIQUE_INDEX: Final = "cool_names_md5_ints_unique"
ATTEMPTS_PER_WORD_COUNT: Final = 5
RODEX_RESERVED_WORDS: Final = frozenset(
    {
        "a",
        "alias",
        "app-server",
        "apply",
        "archive",
        "cloud",
        "completion",
        "create",
        "debug",
        "delete",
        "detach",
        "doctor",
        "e",
        "exec",
        "exec-server",
        "execpolicy",
        "features",
        "fork",
        "help",
        "login",
        "logout",
        "mcp",
        "mcp-server",
        "plugin",
        "remote-control",
        "responses-api-proxy",
        "resume",
        "review",
        "running",
        "sandbox",
        "send",
        "sessions",
        "stdio-to-uds",
        "tail",
        "unarchive",
        "update",
        "wait",
    }
)
_SAFE_RODEX_DISPLAY_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")

_HALF_BITS: Final = 64
_HALF_MODULUS: Final = 1 << _HALF_BITS
_HALF_SIGN_BIT: Final = 1 << (_HALF_BITS - 1)

_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {COOL_NAMES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cool_name_md5_int_1 BIGINT NOT NULL,
    cool_name_md5_int_2 BIGINT NOT NULL,
    cool_name TEXT NOT NULL
)
"""
_CREATE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {COOL_NAMES_MD5_INTS_UNIQUE_INDEX}
ON {COOL_NAMES_TABLE} (cool_name_md5_int_1, cool_name_md5_int_2)
"""

NameGenerator = Callable[[int], str]


class CoolNameError(RuntimeError):
    """The cool-name registry could not satisfy its contract."""


class CoolNameGenerationError(CoolNameError):
    """All configured cool-name candidates already exist."""


class ReservedCoolNameError(CoolNameError):
    """A requested cool name conflicts with Rodex command vocabulary."""


@dataclass(frozen=True, slots=True)
class CoolName:
    """One allocated cool-name lookup row."""

    id: int
    cool_name: str


def initialise_cool_names_database(
    database_path: str | os.PathLike[str] | None = None,
) -> Path:
    """Create and verify the cool-name schema in one transaction."""
    path = normalise_rodex_database_path(database_path)
    with open_rodex_transaction(path) as connection:
        create_and_verify_cool_names_schema(connection)
    return path


def get_unique_new_cool_name(
    database_path: str | os.PathLike[str] | None = None,
    *,
    name_generator: NameGenerator | None = None,
) -> str:
    """Allocate a unique two-word name, falling back to three words after five hits."""
    path = normalise_rodex_database_path(database_path)
    with open_rodex_transaction(path) as connection:
        return allocate_unique_cool_name(
            connection, name_generator=name_generator
        ).cool_name


def allocate_unique_cool_name(
    connection: sqlite3.Connection,
    *,
    name_generator: NameGenerator | None = None,
) -> CoolName:
    """Allocate and return a lookup row inside the caller's transaction."""
    create_and_verify_cool_names_schema(connection)
    generate_name = coolname.generate_slug if name_generator is None else name_generator
    for word_count in (2, 3):
        for _ in range(ATTEMPTS_PER_WORD_COUNT):
            try:
                cool_name = normalise_rodex_display_name(generate_name(word_count))
            except CoolNameError:
                continue
            md5_int_1, md5_int_2 = _cool_name_md5_signed_ints(cool_name)
            lookup_values = {
                "cool_name_md5_int_1": md5_int_1,
                "cool_name_md5_int_2": md5_int_2,
            }
            if select_lookup_id(connection, COOL_NAMES_TABLE, lookup_values) is not None:
                continue
            cursor = connection.execute(
                f"INSERT INTO {COOL_NAMES_TABLE} "
                "(cool_name_md5_int_1, cool_name_md5_int_2, cool_name) "
                "VALUES (?, ?, ?)",
                (md5_int_1, md5_int_2, cool_name),
            )
            if cursor.lastrowid is None:
                raise CoolNameError("SQLite did not return a cool-name id")
            return CoolName(id=cursor.lastrowid, cool_name=cool_name)
    raise CoolNameGenerationError(
        "could not allocate a unique cool name after five two-word and "
        "five three-word attempts"
    )


def get_unique_id_from_cool_name(
    cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Return the integer id found through a cool name's two MD5 integer fields."""
    normalised_name = _normalise_cool_name(cool_name)
    md5_int_1, md5_int_2 = _cool_name_md5_signed_ints(normalised_name)
    path = normalise_rodex_database_path(database_path)
    with open_rodex_transaction(path) as connection:
        create_and_verify_cool_names_schema(connection)
        return _lookup_cool_name_id_from_md5_ints(connection, md5_int_1, md5_int_2)


def reserve_specific_cool_name(
    connection: sqlite3.Connection,
    cool_name: str,
) -> CoolName:
    """Select an exact available name first, inserting only when absent."""
    if not connection.in_transaction:
        raise CoolNameError("cool-name allocation requires an active transaction")
    create_and_verify_cool_names_schema(connection)
    normalised_name = normalise_rodex_display_name(cool_name)
    md5_int_1, md5_int_2 = _cool_name_md5_signed_ints(normalised_name)
    existing_id = _lookup_cool_name_id_from_md5_ints(connection, md5_int_1, md5_int_2)
    if existing_id is not None:
        row = connection.execute(
            f"SELECT cool_name FROM {COOL_NAMES_TABLE} WHERE id = ?",
            (existing_id,),
        ).fetchone()
        if row is None or str(row[0]) != normalised_name:
            raise CoolNameError("derived cool-name identity is already occupied")
        return CoolName(id=existing_id, cool_name=normalised_name)
    cursor = connection.execute(
        f"INSERT INTO {COOL_NAMES_TABLE} "
        "(cool_name_md5_int_1, cool_name_md5_int_2, cool_name) VALUES (?, ?, ?)",
        (md5_int_1, md5_int_2, normalised_name),
    )
    if cursor.lastrowid is None:
        raise CoolNameError("SQLite did not return a cool-name id")
    return CoolName(id=cursor.lastrowid, cool_name=normalised_name)


def lookup_cool_name(
    connection: sqlite3.Connection,
    cool_name: str,
) -> CoolName | None:
    """Resolve one name through its integer MD5 identity in an active transaction."""
    if not connection.in_transaction:
        raise CoolNameError("cool-name lookup requires an active transaction")
    normalised_name = _normalise_cool_name(cool_name)
    md5_int_1, md5_int_2 = _cool_name_md5_signed_ints(normalised_name)
    cool_names_id = _lookup_cool_name_id_from_md5_ints(connection, md5_int_1, md5_int_2)
    if cool_names_id is None:
        return None
    return CoolName(id=cool_names_id, cool_name=normalised_name)


def is_reserved_rodex_name(cool_name: str) -> bool:
    """Return whether a complete name is reserved for a Rodex command."""
    return _normalise_cool_name(cool_name).casefold() in RODEX_RESERVED_WORDS


def normalise_rodex_display_name(cool_name: str) -> str:
    """Validate the portable subset used for user-facing tmux session names."""
    try:
        normalised_name = _normalise_cool_name(cool_name)
    except (TypeError, ValueError) as error:
        raise CoolNameError(str(error)) from error
    if not _SAFE_RODEX_DISPLAY_NAME.fullmatch(normalised_name):
        raise CoolNameError(
            "Rodex names must be 1-80 ASCII letters, digits, underscores, or "
            "hyphens and must start with a letter or digit"
        )
    if is_reserved_rodex_name(normalised_name):
        raise ReservedCoolNameError(f"Rodex name is reserved: {normalised_name}")
    return normalised_name


def create_and_verify_cool_names_schema(connection: sqlite3.Connection) -> None:
    """Create and validate the table within the caller's active transaction."""
    if not connection.in_transaction:
        raise CoolNameError("cool-name schema changes require an active transaction")
    connection.execute(_CREATE_TABLE)
    _verify_table(connection)
    connection.execute(_CREATE_UNIQUE_INDEX)
    _verify_indexes(connection)


def _cool_name_md5_signed_ints(cool_name: str) -> tuple[int, int]:
    digest = hashlib.md5(cool_name.encode("utf-8"), usedforsecurity=False).digest()
    high_unsigned = int.from_bytes(digest[:8], byteorder="big")
    low_unsigned = int.from_bytes(digest[8:], byteorder="big")
    return _unsigned_half_to_signed(high_unsigned), _unsigned_half_to_signed(low_unsigned)


def _lookup_cool_name_id_from_md5_ints(
    connection: sqlite3.Connection,
    md5_int_1: int,
    md5_int_2: int,
) -> int | None:
    return select_lookup_id(
        connection,
        COOL_NAMES_TABLE,
        {
            "cool_name_md5_int_1": md5_int_1,
            "cool_name_md5_int_2": md5_int_2,
        },
    )


def _unsigned_half_to_signed(value: int) -> int:
    return value - _HALF_MODULUS if value >= _HALF_SIGN_BIT else value


def _normalise_cool_name(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("cool_name must be a string")
    normalised = value.strip()
    if not normalised:
        raise ValueError("cool_name must be non-empty")
    return normalised


def _verify_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(f"PRAGMA table_info({COOL_NAMES_TABLE})").fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("cool_name_md5_int_1", "BIGINT", 1, 0),
        ("cool_name_md5_int_2", "BIGINT", 1, 0),
        ("cool_name", "TEXT", 1, 0),
    ]
    if observed != expected:
        raise CoolNameError(f"{COOL_NAMES_TABLE} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (COOL_NAMES_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise CoolNameError(f"{COOL_NAMES_TABLE} id must use AUTOINCREMENT")


def _verify_indexes(connection: sqlite3.Connection) -> None:
    indexes = connection.execute(f"PRAGMA index_list({COOL_NAMES_TABLE})").fetchall()
    matching = [row for row in indexes if row[1] == COOL_NAMES_MD5_INTS_UNIQUE_INDEX]
    columns = connection.execute(
        f"PRAGMA index_info({COOL_NAMES_MD5_INTS_UNIQUE_INDEX})"
    ).fetchall()
    if (
        len(matching) != 1
        or matching[0][2] != 1
        or [row[2] for row in columns] != ["cool_name_md5_int_1", "cool_name_md5_int_2"]
    ):
        raise CoolNameError(f"unique index is missing: {COOL_NAMES_MD5_INTS_UNIQUE_INDEX}")
    indexed_columns = {
        row[2]
        for index in indexes
        for row in connection.execute(f"PRAGMA index_info({index[1]})").fetchall()
    }
    if "cool_name" in indexed_columns:
        raise CoolNameError("cool_name must remain an unindexed payload field")
