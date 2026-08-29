"""Natural-key lookup operations for active Rodex transactions."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping

from .errors import RodexSQLError
from .transactions import require_active_rodex_transaction

SQLValue = int | float | str | bytes | None
_SQL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def select_lookup_id(
    connection: sqlite3.Connection,
    table_name: str,
    lookup_values: Mapping[str, SQLValue],
) -> int | None:
    """Select a lookup row's integer id by its complete natural key."""
    require_active_rodex_transaction(connection)
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
    return int(cursor.lastrowid)


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
