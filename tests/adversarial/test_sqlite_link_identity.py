"""Hard-link admission and lifetime fences for the canonical SQLite pathname."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, closing
from pathlib import Path

import pytest

import rodex_sql.sqlite_identity as identity_module
import rodex_sql.transactions as transactions_module
from rodex_sql import (
    RodexDatabaseMovedError,
    RodexSQLError,
    open_rodex_bootstrap_transaction,
    open_rodex_read_transaction,
    open_rodex_transaction,
)
from rodex_sql.private_database_path import ValidatedDatabaseFile, open_private_database_boundary

TransactionOpener = Callable[[Path], AbstractContextManager[sqlite3.Connection]]
TRANSACTION_OPENERS = [open_rodex_bootstrap_transaction, open_rodex_read_transaction, open_rodex_transaction]
TRANSACTION_NAMES = ["bootstrap", "read", "write"]


@pytest.fixture(autouse=True)
def _isolate_storage_owner() -> Iterator[None]:
    transactions_module._close_process_wal_lifetime_owner()
    yield
    transactions_module._close_process_wal_lifetime_owner()


def _marker_database(database: Path) -> None:
    with open_rodex_bootstrap_transaction(database) as connection:
        connection.execute("CREATE TABLE marker (value INTEGER NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (101)")


@pytest.mark.parametrize("opener", TRANSACTION_OPENERS, ids=TRANSACTION_NAMES)
@pytest.mark.parametrize("requested_name", ["canonical", "alias"])
@pytest.mark.parametrize("initialized", [False, True], ids=["empty", "existing"])
def test_preexisting_links_are_rejected_before_sqlite_connect_without_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opener: TransactionOpener,
    requested_name: str,
    initialized: bool,
) -> None:
    database = tmp_path / "canonical.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    database.touch(mode=0o600)
    if initialized:
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE marker (value INTEGER NOT NULL)")
            connection.execute("INSERT INTO marker VALUES (101)")
            connection.commit()
    os.link(database, alias)
    requested = database if requested_name == "canonical" else alias
    # Provision only the coordination lock, without admitting either database name.
    with open_private_database_boundary(requested, create=True):
        pass
    original_contents = database.read_bytes()

    def unexpected_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        pytest.fail("multiply linked storage reached SQLite")

    monkeypatch.setattr(transactions_module, "_connect_validated_database", unexpected_connect)
    with pytest.raises(RodexSQLError, match="exactly one hard link"), opener(requested):
        pytest.fail("multiply linked storage admitted")

    assert database.stat().st_nlink == alias.stat().st_nlink == 2
    assert database.read_bytes() == alias.read_bytes() == original_contents
    assert not list(tmp_path.glob("*-wal"))
    assert not list(tmp_path.glob("*-shm"))


@pytest.mark.parametrize("writable", [False, True], ids=["read", "write"])
@pytest.mark.parametrize("create", [False, True], ids=["existing", "bootstrap"])
def test_descriptor_admission_rechecks_links_added_after_parent_admission(
    tmp_path: Path, writable: bool, create: bool
) -> None:
    database = tmp_path / "canonical.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    database.touch(mode=0o600)
    with open_private_database_boundary(database, create=True) as boundary:
        os.link(database, alias)
        with (
            pytest.raises(RodexSQLError, match="exactly one hard link"),
            boundary.open_database(writable=writable, create=create),
        ):
            pytest.fail("multiply linked descriptor admitted")
    assert database.stat().st_nlink == alias.stat().st_nlink == 2


def test_identity_fence_uses_fresh_descriptor_link_count(tmp_path: Path) -> None:
    database = tmp_path / "canonical.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    with (
        open_private_database_boundary(database, create=True) as boundary,
        boundary.open_database(writable=True, create=True) as opened,
    ):
        identity_module.require_database_identity(opened, stage="admission")
        os.link(database, alias)
        assert opened.state.st_nlink == 1
        assert os.fstat(opened.descriptor).st_nlink == 2
        with pytest.raises(RodexDatabaseMovedError, match=r"fresh_check database descriptor .*exactly one hard link"):
            identity_module.require_database_identity(opened, stage="fresh_check")
    assert database.stat().st_nlink == alias.stat().st_nlink == 2


@pytest.mark.parametrize("opener", TRANSACTION_OPENERS, ids=TRANSACTION_NAMES)
def test_idle_owner_cannot_authorize_a_later_transaction_after_link_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: TransactionOpener
) -> None:
    database = tmp_path / "canonical.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    _marker_database(database)
    owner = transactions_module._PROCESS_STORAGE_OWNERS[database]
    assert owner.database_state.st_nlink == 1
    os.link(database, alias)

    def unexpected_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        pytest.fail("linked retained storage reached SQLite")

    monkeypatch.setattr(transactions_module, "_connect_validated_database", unexpected_connect)
    with pytest.raises(RodexSQLError, match="exactly one hard link"), opener(database):
        pytest.fail("linked retained storage admitted")
    assert owner.borrowers == 0
    assert owner.database_state.st_nlink == 1
    assert os.fstat(owner.database_descriptor).st_nlink == 2
    assert database.stat().st_nlink == alias.stat().st_nlink == 2


FENCE_CASES = [
    pytest.param(opener, stage, id=f"{name}-{stage}")
    for opener, name in zip(TRANSACTION_OPENERS, TRANSACTION_NAMES, strict=True)
    for stage in (
        "storage_borrow",
        "pre_connect",
        "post_connect",
        "pre_begin",
        "pre_commit",
        "wal_lifetime",
        "connect_error",
        "sqlite_error",
    )
    if stage != "wal_lifetime" or opener is not open_rodex_read_transaction
]


@pytest.mark.parametrize(("opener", "target_stage"), FENCE_CASES)
def test_link_creation_at_each_identity_fence_rejects_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opener: TransactionOpener,
    target_stage: str,
) -> None:
    database = tmp_path / "canonical.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    _marker_database(database)
    if target_stage == "wal_lifetime":
        transactions_module._close_process_wal_lifetime_owner()
    real_identity = identity_module.require_database_identity
    real_connect = transactions_module._connect_validated_database
    statements: list[str] = []
    connection_attempts = 0
    linked = False

    def link_before_validation(opened: ValidatedDatabaseFile, *, stage: str) -> None:
        nonlocal linked
        if stage == target_stage and not linked:
            os.link(database, alias)
            linked = True
        real_identity(opened, stage=stage)

    def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal connection_attempts
        connection_attempts += 1
        if target_stage == "connect_error":
            raise RodexSQLError("injected connection failure")
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(identity_module, "require_database_identity", link_before_validation)
    monkeypatch.setattr(transactions_module, "require_database_identity", link_before_validation)
    monkeypatch.setattr(transactions_module, "_connect_validated_database", traced_connect)

    with (
        pytest.raises(RodexDatabaseMovedError, match=f"{target_stage} .*exactly one hard link"),
        opener(database) as connection,
    ):
        if opener is not open_rodex_read_transaction:
            connection.execute("INSERT INTO marker VALUES (202)")
        else:
            assert connection.execute("SELECT value FROM marker").fetchall() == [(101,)]
        if target_stage == "sqlite_error":
            raise sqlite3.OperationalError("injected statement failure")

    assert linked
    assert database.stat().st_nlink == alias.stat().st_nlink == 2
    assert "COMMIT" not in statements
    if target_stage in {"storage_borrow", "pre_connect"}:
        assert connection_attempts == 0
    if target_stage in {"pre_commit", "sqlite_error"}:
        assert "ROLLBACK" in statements
    else:
        assert not any(statement.startswith("BEGIN") for statement in statements)
    if target_stage in {"post_connect", "connect_error"}:
        assert statements == []

    # Observe rollback through the canonical name only; do not open an alias or
    # remove either name to make the storage appear safe to Rodex again.
    with closing(sqlite3.connect(database)) as observer:
        assert observer.execute("SELECT value FROM marker").fetchall() == [(101,)]
    assert database.stat().st_nlink == alias.stat().st_nlink == 2
    assert transactions_module._PROCESS_STORAGE_OWNERS[database].borrowers == 0


def test_fresh_bootstrap_rolls_back_ddl_when_a_link_appears_before_commit(tmp_path: Path) -> None:
    database = tmp_path / "canonical.sqlite3"
    alias = tmp_path / "alias.sqlite3"
    with (
        pytest.raises(RodexDatabaseMovedError, match=r"pre_commit .*exactly one hard link"),
        open_rodex_bootstrap_transaction(database) as connection,
    ):
        connection.execute("CREATE TABLE uncommitted (value INTEGER)")
        connection.execute("INSERT INTO uncommitted VALUES (202)")
        os.link(database, alias)

    with closing(sqlite3.connect(database)) as observer:
        assert observer.execute("SELECT name FROM sqlite_master WHERE name = 'uncommitted'").fetchall() == []
    assert database.stat().st_nlink == alias.stat().st_nlink == 2
