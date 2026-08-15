from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rodex_sql import (
    RodexSQLError,
    open_rodex_transaction,
    select_lookup_id,
    select_or_insert_lookup_id,
)


def create_lookup_table(database: Path) -> None:
    with open_rodex_transaction(database) as connection:
        connection.execute(
            "CREATE TABLE example_lookup ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX example_lookup_code_unique ON example_lookup (code)"
        )


def test_transaction_commits_successful_work(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite3"

    create_lookup_table(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'example_lookup'"
        ).fetchone() == ("example_lookup",)


def test_transaction_rolls_back_all_work_on_failure(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite3"
    create_lookup_table(database)

    with (
        pytest.raises(RuntimeError, match="abort transaction"),
        open_rodex_transaction(database) as connection,
    ):
        select_or_insert_lookup_id(connection, "example_lookup", {"code": "one"})
        raise RuntimeError("abort transaction")

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM example_lookup").fetchone() == (0,)


def test_rolled_back_lookup_insert_does_not_leave_an_autoincrement_gap(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database.sqlite3"
    create_lookup_table(database)
    with (
        pytest.raises(RuntimeError),
        open_rodex_transaction(database) as connection,
    ):
        assert (
            select_or_insert_lookup_id(
                connection, "example_lookup", {"code": "rolled-back"}
            )
            == 1
        )
        raise RuntimeError

    with open_rodex_transaction(database) as connection:
        inserted_id = select_or_insert_lookup_id(
            connection, "example_lookup", {"code": "committed"}
        )

    assert inserted_id == 1


def test_lookup_selects_before_it_considers_inserting(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite3"
    create_lookup_table(database)
    with open_rodex_transaction(database) as connection:
        select_or_insert_lookup_id(connection, "example_lookup", {"code": "existing"})

    statements: list[str] = []
    with open_rodex_transaction(database) as connection:
        connection.set_trace_callback(statements.append)
        existing_id = select_or_insert_lookup_id(
            connection, "example_lookup", {"code": "existing"}
        )

    assert existing_id == 1
    assert statements[0].startswith("SELECT id FROM example_lookup")
    assert not any(statement.startswith("INSERT") for statement in statements)


def test_select_lookup_id_returns_none_for_an_absent_key(tmp_path: Path) -> None:
    database = tmp_path / "database.sqlite3"
    create_lookup_table(database)

    with open_rodex_transaction(database) as connection:
        assert select_lookup_id(connection, "example_lookup", {"code": "absent"}) is None


def test_lookup_operations_require_an_active_transaction() -> None:
    with (
        sqlite3.connect(":memory:") as connection,
        pytest.raises(RodexSQLError, match="active transaction"),
    ):
        select_lookup_id(connection, "example_lookup", {"code": "one"})


@pytest.mark.parametrize(
    ("table_name", "lookup_values"),
    [
        ("unsafe-table", {"code": "one"}),
        ("example_lookup", {}),
        ("example_lookup", {"unsafe-column": "one"}),
    ],
)
def test_lookup_rejects_unsafe_or_empty_identifiers(
    tmp_path: Path, table_name: str, lookup_values: dict[str, str]
) -> None:
    database = tmp_path / "database.sqlite3"
    create_lookup_table(database)

    with open_rodex_transaction(database) as connection, pytest.raises(ValueError):
        select_lookup_id(connection, table_name, lookup_values)
