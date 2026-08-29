"""Canonical stdlib SQLite connection, lock, journal, and transaction policy."""

from __future__ import annotations

import fcntl
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Final

from .errors import RodexSQLError
from .private_database_path import (
    PrivateDatabaseBoundary,
    ValidatedDatabaseFile,
    normalise_rodex_database_path,
    open_private_database_boundary,
)
from .sqlite_identity import (
    _database_identity_was_admitted,
    require_database_identity,
    require_sqlite_main_path,
    validated_database_uri,
)

_DATABASE_LOCK_TIMEOUT_SECONDS: Final = 10.0


@contextmanager
def open_rodex_transaction(
    database_path: str | Path,
) -> Iterator[sqlite3.Connection]:
    """Open one immediate WAL transaction under the shared transition lock."""
    with _open_rodex_write_transaction(
        database_path,
        allow_initial_creation=False,
    ) as connection:
        yield connection


@contextmanager
def open_rodex_bootstrap_transaction(
    database_path: str | Path,
) -> Iterator[sqlite3.Connection]:
    """Open the sole transaction authorized to create initial database storage."""
    with _open_rodex_write_transaction(
        database_path,
        allow_initial_creation=True,
    ) as connection:
        yield connection


@contextmanager
def open_rodex_read_transaction(
    database_path: str | Path,
) -> Iterator[sqlite3.Connection]:
    """Open one WAL-aware read-only snapshot under the shared transition lock."""
    with (
        _open_transaction_storage(
            database_path,
            writable=False,
            allow_initial_creation=False,
        ) as opened,
        _open_transaction_connection(
            opened,
            read_only=True,
            begin_statement="BEGIN",
        ) as connection,
    ):
        yield connection


@contextmanager
def open_rodex_maintenance_lock(
    database_path: str | Path,
) -> Iterator[Path]:
    """Exclude connections for diagnostics; no live move/migration is supported."""
    path = normalise_rodex_database_path(database_path)
    with open_private_database_boundary(path, create=False) as boundary:
        _acquire_database_lock(boundary, exclusive=True)
        yield boundary.path


def require_active_rodex_transaction(connection: sqlite3.Connection) -> None:
    """Reject operations that are not enclosed by an active transaction."""
    if not connection.in_transaction:
        raise RodexSQLError("operation requires an active transaction opened by Rodex")


@contextmanager
def _open_transaction_connection(
    opened: ValidatedDatabaseFile,
    *,
    read_only: bool,
    begin_statement: str,
) -> Iterator[sqlite3.Connection]:
    require_database_identity(opened, stage="pre_connect")
    try:
        connection = _connect_validated_database(opened, read_only=read_only)
    except RodexSQLError:
        require_database_identity(opened, stage="connect_error")
        raise
    try:
        require_sqlite_main_path(connection, opened)
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            _ensure_wal_journal_mode(connection)
            connection.execute("PRAGMA synchronous = NORMAL")
        require_database_identity(opened, stage="pre_begin")
        connection.execute(begin_statement)
        try:
            yield connection
            require_database_identity(opened, stage="pre_commit")
        except sqlite3.Error:
            with suppress(sqlite3.Error):
                connection.rollback()
            require_database_identity(opened, stage="sqlite_error")
            raise
        except BaseException:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise
        else:
            connection.commit()
    except sqlite3.Error:
        with suppress(sqlite3.Error):
            connection.rollback()
        require_database_identity(opened, stage="sqlite_error")
        raise
    finally:
        connection.close()


@contextmanager
def _open_rodex_write_transaction(
    database_path: str | Path,
    *,
    allow_initial_creation: bool,
) -> Iterator[sqlite3.Connection]:
    with (
        _open_transaction_storage(
            database_path,
            writable=True,
            allow_initial_creation=allow_initial_creation,
        ) as opened,
        _open_transaction_connection(
            opened,
            read_only=False,
            begin_statement="BEGIN IMMEDIATE",
        ) as connection,
    ):
        yield connection


@contextmanager
def _open_transaction_storage(
    database_path: str | Path,
    *,
    writable: bool,
    allow_initial_creation: bool,
) -> Iterator[ValidatedDatabaseFile]:
    path = normalise_rodex_database_path(database_path)
    create = allow_initial_creation and not _database_identity_was_admitted(path)
    with open_private_database_boundary(
        path,
        create=create,
    ) as boundary:
        _acquire_database_lock(boundary, exclusive=False)
        with boundary.open_database(
            writable=writable,
            create=create,
        ) as opened:
            yield opened


def _connect_validated_database(
    opened: ValidatedDatabaseFile,
    *,
    read_only: bool,
) -> sqlite3.Connection:
    try:
        return sqlite3.connect(
            validated_database_uri(opened, read_only=read_only),
            timeout=10,
            isolation_level=None,
            uri=True,
        )
    except sqlite3.Error as error:
        raise RodexSQLError(
            f"could not open validated database {opened.path}: {error}"
        ) from error


def _acquire_database_lock(
    boundary: PrivateDatabaseBoundary,
    *,
    exclusive: bool,
) -> None:
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + _DATABASE_LOCK_TIMEOUT_SECONDS
    retry_delay = 0.001
    while True:
        try:
            fcntl.flock(
                boundary.transition_lock_descriptor,
                operation | fcntl.LOCK_NB,
            )
            return
        except BlockingIOError as error:
            if time.monotonic() >= deadline:
                mode = "maintenance" if not exclusive else "active connections"
                raise RodexSQLError(
                    f"timed out waiting for database lock at {boundary.path}; "
                    f"blocked by {mode}"
                ) from error
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2.0, 0.05)


def _ensure_wal_journal_mode(connection: sqlite3.Connection) -> None:
    """Perform the cold journal transition with bounded sleeping retries."""
    deadline = time.monotonic() + _DATABASE_LOCK_TIMEOUT_SECONDS
    retry_delay = 0.001
    while True:
        if connection.execute("PRAGMA journal_mode").fetchone() == ("wal",):
            return
        try:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).casefold() or time.monotonic() >= deadline:
                raise
        else:
            if journal_mode == ("wal",):
                return
            if time.monotonic() >= deadline:
                raise RodexSQLError(
                    f"could not enable WAL journal mode; SQLite returned {journal_mode!r}"
                )
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2.0, 0.05)
