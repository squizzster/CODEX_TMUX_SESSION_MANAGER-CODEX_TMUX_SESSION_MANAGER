"""Canonical stdlib SQLite connection, lock, journal, and transaction policy."""

from __future__ import annotations

import atexit
import fcntl
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
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
_WAL_AUTOCHECKPOINT_PAGES: Final = 1_000
_WAL_JOURNAL_SIZE_LIMIT_BYTES: Final = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _WalStorageIdentity:
    """Exact storage identity retained by one process-local WAL owner."""

    parent: tuple[int, int]
    transition_lock: tuple[int, int]
    database: tuple[int, int]


@dataclass(slots=True)
class _ProcessWalLifetimeOwner:
    """Keep validated storage and one idle WAL connection process-local."""

    process_id: int
    database_path: Path
    storage_identity: _WalStorageIdentity
    database_descriptor: int
    database_state: os.stat_result
    connection: sqlite3.Connection | None = None

    def close(self) -> None:
        """Close SQLite before releasing its separately validated descriptor."""
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        os.close(self.database_descriptor)


_PROCESS_WAL_LIFETIME_LOCK = Lock()
_PROCESS_WAL_LIFETIME_OWNER: _ProcessWalLifetimeOwner | None = None


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
            _configure_wal_write_connection(connection)
            _retain_process_wal_lifetime(opened)
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
    allow_initial_creation: bool,
) -> Iterator[ValidatedDatabaseFile]:
    path = normalise_rodex_database_path(database_path)
    create = allow_initial_creation and not _database_identity_was_admitted(path)
    with open_private_database_boundary(
        path,
        create=create,
    ) as boundary:
        _acquire_database_lock(boundary, exclusive=False)
        opened = _retain_process_database_storage(
            boundary,
            create=create,
        )
        yield opened


def _connect_validated_database(
    opened: ValidatedDatabaseFile,
    *,
    read_only: bool,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    try:
        return sqlite3.connect(
            validated_database_uri(opened, read_only=read_only),
            timeout=10,
            isolation_level=None,
            uri=True,
            check_same_thread=check_same_thread,
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


def _configure_wal_write_connection(connection: sqlite3.Connection) -> None:
    """Apply the one bounded durability and checkpoint policy to every writer."""
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute(f"PRAGMA wal_autocheckpoint = {_WAL_AUTOCHECKPOINT_PAGES}")
    connection.execute(f"PRAGMA journal_size_limit = {_WAL_JOURNAL_SIZE_LIMIT_BYTES}")


def _retain_process_wal_lifetime(opened: ValidatedDatabaseFile) -> None:
    """Retain one idle connection so sparse commits reuse their WAL sidecars."""
    process_id = os.getpid()
    storage_identity = _wal_storage_identity(opened)
    with _PROCESS_WAL_LIFETIME_LOCK:
        current = _PROCESS_WAL_LIFETIME_OWNER
        if current is None or (
            current.process_id != process_id
            or current.database_path != opened.path
            or current.storage_identity != storage_identity
            or current.database_descriptor != opened.descriptor
        ):
            raise RodexSQLError(
                "process-local SQLite storage owner changed before WAL retention"
            )
        if current.connection is not None:
            return
        current.connection = _open_process_wal_lifetime_connection(opened)


def _retain_process_database_storage(
    boundary: PrivateDatabaseBoundary,
    *,
    create: bool,
) -> ValidatedDatabaseFile:
    """Borrow one process-local main-file descriptor across all connections."""
    global _PROCESS_WAL_LIFETIME_OWNER

    process_id = os.getpid()
    path = boundary.path
    with _PROCESS_WAL_LIFETIME_LOCK:
        current = _PROCESS_WAL_LIFETIME_OWNER
        if current is not None and current.process_id != process_id:
            raise RodexSQLError(
                "inherited SQLite storage owner reached an unsupported process boundary"
            )
        if (
            current is not None
            and current.database_path == path
            and current.storage_identity.parent
            == (boundary.parent_state.st_dev, boundary.parent_state.st_ino)
            and current.storage_identity.transition_lock
            == (
                boundary.transition_lock_state.st_dev,
                boundary.transition_lock_state.st_ino,
            )
        ):
            opened = ValidatedDatabaseFile(
                boundary=boundary,
                descriptor=current.database_descriptor,
                state=current.database_state,
            )
            return opened

        if current is not None:
            current.close()
            _PROCESS_WAL_LIFETIME_OWNER = None

        # The retained descriptor is always writable because a later write must
        # reuse it without opening and releasing another descriptor on this file.
        opened = boundary.open_database(writable=True, create=create)
        try:
            _PROCESS_WAL_LIFETIME_OWNER = _ProcessWalLifetimeOwner(
                process_id=process_id,
                database_path=path,
                storage_identity=_wal_storage_identity(opened),
                database_descriptor=opened.descriptor,
                database_state=opened.state,
            )
        except BaseException:
            opened.close()
            raise
        return opened


def _open_process_wal_lifetime_connection(
    opened: ValidatedDatabaseFile,
) -> sqlite3.Connection:
    """Open the validated idle connection owned by this process's WAL lifetime."""
    connection = _connect_validated_database(
        opened,
        read_only=False,
        check_same_thread=False,
    )
    try:
        require_sqlite_main_path(connection, opened)
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA journal_mode").fetchone() != ("wal",):
            raise RodexSQLError("process WAL lifetime opened outside WAL journal mode")
        _configure_wal_write_connection(connection)
        require_database_identity(opened, stage="wal_lifetime")
        return connection
    except BaseException:
        connection.close()
        raise


def _wal_storage_identity(opened: ValidatedDatabaseFile) -> _WalStorageIdentity:
    boundary = opened.boundary
    return _WalStorageIdentity(
        parent=(boundary.parent_state.st_dev, boundary.parent_state.st_ino),
        transition_lock=(
            boundary.transition_lock_state.st_dev,
            boundary.transition_lock_state.st_ino,
        ),
        database=(opened.state.st_dev, opened.state.st_ino),
    )


def _close_process_wal_lifetime_owner() -> None:
    """Checkpoint and release this process's sole idle WAL lifetime owner."""
    global _PROCESS_WAL_LIFETIME_OWNER

    with _PROCESS_WAL_LIFETIME_LOCK:
        owner = _PROCESS_WAL_LIFETIME_OWNER
        _PROCESS_WAL_LIFETIME_OWNER = None
        if owner is not None:
            owner.close()


def _prepare_process_wal_lifetime_fork() -> None:
    """Close parent-owned SQLite state before a child can inherit it."""
    global _PROCESS_WAL_LIFETIME_OWNER

    _PROCESS_WAL_LIFETIME_LOCK.acquire()
    owner = _PROCESS_WAL_LIFETIME_OWNER
    _PROCESS_WAL_LIFETIME_OWNER = None
    if owner is not None:
        owner.close()


def _finish_process_wal_lifetime_fork_in_parent() -> None:
    _PROCESS_WAL_LIFETIME_LOCK.release()


def _finish_process_wal_lifetime_fork_in_child() -> None:
    """Release synchronization; the parent closed SQLite before the fork."""
    _PROCESS_WAL_LIFETIME_LOCK.release()


os.register_at_fork(
    before=_prepare_process_wal_lifetime_fork,
    after_in_parent=_finish_process_wal_lifetime_fork_in_parent,
    after_in_child=_finish_process_wal_lifetime_fork_in_child,
)
atexit.register(_close_process_wal_lifetime_owner)
