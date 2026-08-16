"""Transaction and lookup-table primitives for Rodex SQLite databases."""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

SQLValue = int | float | str | bytes | None
_SQL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


class RodexSQLError(RuntimeError):
    """A Rodex SQL operation violated its transaction or lookup contract."""


def default_rodex_database_path() -> Path:
    """Resolve the durable database path for the current POSIX user."""
    configured = os.environ.get("RODEX_DATABASE_PATH")
    if configured:
        return Path(configured).expanduser().resolve()

    configured_state_home = os.environ.get("XDG_STATE_HOME")
    state_home = (
        Path(configured_state_home).expanduser()
        if configured_state_home
        else Path.home() / ".local" / "state"
    )
    return (state_home / "rodex" / "rodex.sqlite3").resolve()


def normalise_rodex_database_path(
    database_path: str | os.PathLike[str] | None,
) -> Path:
    """Resolve an explicit path or the current user's durable default."""
    if database_path is None:
        return default_rodex_database_path()
    return Path(database_path).expanduser().resolve()


@contextmanager
def open_rodex_transaction(
    database_path: str | os.PathLike[str],
) -> Iterator[sqlite3.Connection]:
    """Open one immediate transaction with foreign-key enforcement enabled."""
    path = Path(database_path).expanduser().resolve()
    database_exists = path.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    if not database_exists:
        path.chmod(0o600)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


def select_lookup_id(
    connection: sqlite3.Connection,
    table_name: str,
    lookup_values: Mapping[str, SQLValue],
) -> int | None:
    """Select a lookup row's integer id by its complete natural key."""
    _require_transaction(connection)
    columns = _validated_lookup_columns(table_name, lookup_values)
    predicate = " AND ".join(f"{column} = ?" for column in columns)
    row = connection.execute(
        f"SELECT id FROM {table_name} WHERE {predicate}",
        tuple(lookup_values[column] for column in columns),
    ).fetchone()
    return None if row is None else int(row[0])


def select_or_insert_lookup_id(
    connection: sqlite3.Connection,
    table_name: str,
    lookup_values: Mapping[str, SQLValue],
) -> int:
    """Select first, inserting a lookup row only when its natural key is absent."""
    existing_id = select_lookup_id(connection, table_name, lookup_values)
    if existing_id is not None:
        return existing_id

    columns = _validated_lookup_columns(table_name, lookup_values)
    placeholders = ", ".join("?" for _ in columns)
    cursor = connection.execute(
        f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})",
        tuple(lookup_values[column] for column in columns),
    )
    if cursor.lastrowid is None:
        raise RodexSQLError(f"SQLite did not return an id for lookup table {table_name}")
    return cursor.lastrowid


def _validated_lookup_columns(
    table_name: str, lookup_values: Mapping[str, SQLValue]
) -> tuple[str, ...]:
    if not _SQL_IDENTIFIER.fullmatch(table_name):
        raise ValueError(f"invalid SQL table identifier: {table_name!r}")
    columns = tuple(lookup_values)
    if not columns:
        raise ValueError("lookup_values must contain at least one field")
    invalid_columns = [
        column for column in columns if not _SQL_IDENTIFIER.fullmatch(column)
    ]
    if invalid_columns:
        raise ValueError(f"invalid SQL column identifier: {invalid_columns[0]!r}")
    return columns


def _require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RodexSQLError("lookup operations require an active transaction")
