from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import rodex_sql.transactions as transactions_module
from rodex_sql import (
    INDEX_RE_TRY_ATTEMPTS,
    RodexDatabaseNotFoundError,
    RodexDatabaseNotInitializedError,
    RodexSQLError,
    index_re_try_attempt_numbers,
    open_rodex_bootstrap_transaction,
    open_rodex_read_transaction,
    open_rodex_transaction,
    require_active_rodex_transaction,
    select_lookup_id,
    select_or_insert_lookup_id,
    subscribe_rodex_database_terminal,
)


def test_index_re_try_policy_has_exactly_ten_finite_attempts() -> None:
    assert INDEX_RE_TRY_ATTEMPTS == 10
    assert tuple(index_re_try_attempt_numbers()) == tuple(range(1, 11))


def create_lookup_table(database: Path) -> None:
    with open_rodex_bootstrap_transaction(database) as connection:
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


@pytest.mark.evolutionary_regression
def test_read_transaction_cannot_create_database_storage(tmp_path: Path) -> None:
    """Current evidence: a read path must never bootstrap durable state."""
    database = tmp_path / "absent" / "database.sqlite3"

    with (
        pytest.raises(RodexDatabaseNotFoundError, match="database does not exist"),
        open_rodex_read_transaction(database),
    ):
        pass

    assert not database.parent.exists()
    assert not database.exists()


@pytest.mark.evolutionary_regression
def test_write_transaction_cannot_create_database_storage(tmp_path: Path) -> None:
    database = tmp_path / "absent" / "database.sqlite3"

    with (
        pytest.raises(RodexDatabaseNotFoundError, match="database does not exist"),
        open_rodex_transaction(database),
    ):
        pass

    assert not database.parent.exists()
    assert not database.exists()


def test_bootstrap_transaction_is_the_explicit_creation_entry(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "database.sqlite3"

    with open_rodex_bootstrap_transaction(database) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")

    assert database.is_file()


def test_existing_transaction_does_not_initialize_an_unadmitted_file(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database.sqlite3"
    database.touch(mode=0o600)

    with (
        pytest.raises(RodexDatabaseNotInitializedError, match="transition lock"),
        open_rodex_transaction(database),
    ):
        pass

    assert not any(
        path.name.endswith(".rodex-transition.lock") for path in tmp_path.iterdir()
    )


def test_terminal_subscription_uses_the_existing_only_boundary(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nested" / "database.sqlite3"
    callbacks: list[tuple[Path, str]] = []

    with pytest.raises(RodexDatabaseNotFoundError, match="database does not exist"):
        subscribe_rodex_database_terminal(
            database,
            lambda path, reason: callbacks.append((path, reason)),
        )
    assert not database.parent.exists()

    with open_rodex_bootstrap_transaction(database):
        pass
    unsubscribe = subscribe_rodex_database_terminal(
        database,
        lambda path, reason: callbacks.append((path, reason)),
    )
    unsubscribe()
    assert callbacks == []


def test_terminal_subscription_admits_before_guard_lookup_and_subscription(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "database.sqlite3"
    lifecycle: list[str] = []

    class FakeReadTransaction:
        def __enter__(self) -> object:
            lifecycle.append("read-enter")
            return object()

        def __exit__(self, *_exc: object) -> None:
            lifecycle.append("read-exit")

    class FakeGuard:
        def require_available(self, stage: str) -> None:
            assert stage == "terminal_subscription"
            lifecycle.append("guard-require")

        def subscribe_terminal(self, _callback: object) -> object:
            lifecycle.append("guard-subscribe")

            def unsubscribe() -> None:
                lifecycle.append("guard-unsubscribe")

            return unsubscribe

    def open_read(path: Path) -> FakeReadTransaction:
        assert path == database
        lifecycle.append("read-open")
        return FakeReadTransaction()

    def known_guard(path: Path) -> FakeGuard:
        assert path == database
        lifecycle.append("guard-lookup")
        return FakeGuard()

    monkeypatch.setattr(transactions_module, "open_rodex_read_transaction", open_read)
    monkeypatch.setattr(
        transactions_module,
        "_known_database_location_guard",
        known_guard,
    )

    unsubscribe = subscribe_rodex_database_terminal(database, lambda *_args: None)

    assert lifecycle == [
        "read-open",
        "read-enter",
        "guard-lookup",
        "guard-require",
        "guard-subscribe",
        "read-exit",
    ]
    unsubscribe()
    assert lifecycle[-1] == "guard-unsubscribe"


@pytest.mark.evolutionary_regression
def test_read_transaction_rejects_sql_writes(tmp_path: Path) -> None:
    """Current evidence: every declared read transaction is SQLite read-only."""
    database = tmp_path / "database.sqlite3"
    create_lookup_table(database)

    with open_rodex_read_transaction(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM example_lookup").fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO example_lookup (code) VALUES ('forbidden')")

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


def test_canonical_transaction_invariant_rejects_before_sql() -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.set_authorizer(
            lambda *_args: pytest.fail("transaction guard attempted SQL")
        )
        with pytest.raises(RodexSQLError, match="active transaction"):
            require_active_rodex_transaction(connection)


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
