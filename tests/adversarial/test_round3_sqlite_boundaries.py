from __future__ import annotations

import errno
import importlib
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from threading import Event, Thread

import pytest

import rodex_sql.private_database_path as private_path_module
import rodex_sql.transactions as transactions_module
from rodex_sql import (
    RodexSQLError,
    open_rodex_bootstrap_transaction,
    open_rodex_maintenance_lock,
    open_rodex_read_transaction,
    open_rodex_transaction,
)

guard_module = importlib.import_module("rodex_sql.database_location_guard")
TransactionOpener = Callable[[Path], AbstractContextManager[sqlite3.Connection]]


def _marker_database(path: Path, marker: str = "validated") -> None:
    with open_rodex_bootstrap_transaction(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (?)", (marker,))


def _open_targets_below(root: Path) -> list[str]:
    targets: list[str] = []
    for descriptor_name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{descriptor_name}")
        except OSError:
            continue
        if str(root) in target:
            targets.append(target)
    return targets


def _descriptor_count() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.parametrize(
    "opener",
    [open_rodex_read_transaction, open_rodex_transaction],
    ids=["read", "write"],
)
def test_round3_one_validated_file_open_and_one_sqlite_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opener: TransactionOpener,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    real_os_open = private_path_module.os.open
    real_connect = transactions_module.sqlite3.connect
    file_opens = 0
    connections = 0

    def counted_os_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal file_opens
        if path == database.name and kwargs.get("dir_fd") is not None:
            file_opens += 1
        return real_os_open(path, *args, **kwargs)  # type: ignore[arg-type]

    def counted_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal connections
        connections += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(private_path_module.os, "open", counted_os_open)
    monkeypatch.setattr(transactions_module.sqlite3, "connect", counted_connect)

    with opener(database) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("validated",)

    assert file_opens == 1
    assert connections == 1


@pytest.mark.parametrize(
    ("opener", "statement_budget"),
    [(open_rodex_read_transaction, 8), (open_rodex_transaction, 10)],
    ids=["read", "write"],
)
def test_round3_identity_and_transaction_sql_budget_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opener: TransactionOpener,
    statement_budget: int,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    real_connect = transactions_module.sqlite3.connect
    statements: list[str] = []

    def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(transactions_module.sqlite3, "connect", traced_connect)

    with opener(database) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("validated",)

    assert len(statements) <= statement_budget
    assert sum("PRAGMA database_list" in statement for statement in statements) == 1
    assert statements.count("BEGIN") + statements.count("BEGIN IMMEDIATE") == 1
    assert statements.count("COMMIT") == 1


def test_round3_successful_reads_do_not_leak_database_descriptors(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)

    for _ in range(20):
        with open_rodex_read_transaction(database) as connection:
            assert connection.execute("SELECT value FROM marker").fetchone() == (
                "validated",
            )

    assert _open_targets_below(tmp_path) == []


def test_round3_database_requires_private_owned_immediate_parent(tmp_path: Path) -> None:
    public_parent = tmp_path / "public"
    public_parent.mkdir(mode=0o755)

    with (
        pytest.raises(RodexSQLError, match="private current-user directory"),
        open_rodex_bootstrap_transaction(public_parent / "registry.sqlite3"),
    ):
        pass

    public_parent.chmod(0o700)
    database = public_parent / "registry.sqlite3"
    with open_rodex_bootstrap_transaction(database):
        pass
    assert database.exists()


def test_round3_existing_public_database_and_transition_lock_are_not_repaired(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    with (
        private_path_module.open_private_database_boundary(
            database,
            create=True,
        ) as boundary,
        boundary.open_database(writable=True, create=True),
    ):
        pass
    database.chmod(0o644)

    with (
        pytest.raises(RodexSQLError, match="database is not private"),
        open_rodex_read_transaction(database),
    ):
        pass
    assert database.stat().st_mode & 0o777 == 0o644

    lock_database = tmp_path / "lock-registry.sqlite3"
    with (
        private_path_module.open_private_database_boundary(
            lock_database,
            create=True,
        ) as boundary,
        boundary.open_database(writable=True, create=True),
    ):
        pass
    lock = private_path_module.database_transition_lock_path(lock_database)
    lock.chmod(0o644)
    with (
        pytest.raises(RodexSQLError, match="transition lock is not private"),
        open_rodex_read_transaction(lock_database),
    ):
        pass
    assert lock.stat().st_mode & 0o777 == 0o644


def test_round3_readers_are_not_head_of_line_blocked_by_a_writer(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    writer_entered = Event()
    release_writer = Event()
    failures: list[BaseException] = []

    def hold_writer() -> None:
        try:
            with open_rodex_transaction(database) as connection:
                connection.execute("INSERT INTO marker VALUES ('pending')")
                writer_entered.set()
                assert release_writer.wait(2)
        except BaseException as error:
            failures.append(error)

    writer = Thread(target=hold_writer)
    writer.start()
    assert writer_entered.wait(2)
    try:
        with open_rodex_read_transaction(database) as connection:
            assert connection.execute("SELECT value FROM marker").fetchall() == [
                ("validated",)
            ]
    finally:
        release_writer.set()
        writer.join(2)
    assert not writer.is_alive()
    assert failures == []


def test_round3_exclusive_maintenance_lock_rejects_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    monkeypatch.setattr(transactions_module, "_DATABASE_LOCK_TIMEOUT_SECONDS", 0.0)

    with (
        open_rodex_maintenance_lock(database),
        pytest.raises(RodexSQLError, match="blocked by maintenance"),
        open_rodex_read_transaction(database),
    ):
        pass


def test_round3_external_process_maintenance_lock_rejects_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    lock_path = private_path_module.database_transition_lock_path(database)
    script = (
        "import fcntl,os,sys; "
        "fd=os.open(sys.argv[1], os.O_RDWR); "
        "fcntl.flock(fd, fcntl.LOCK_EX); "
        "print('ready', flush=True); sys.stdin.readline(); os.close(fd)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, os.fspath(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        monkeypatch.setattr(
            transactions_module,
            "_DATABASE_LOCK_TIMEOUT_SECONDS",
            0.0,
        )
        with (
            pytest.raises(RodexSQLError, match="blocked by maintenance"),
            open_rodex_read_transaction(database),
        ):
            pass
    finally:
        assert process.stdin is not None
        process.stdin.write("stop\n")
        process.stdin.flush()
        process.wait(timeout=2)

    with open_rodex_read_transaction(database):
        pass

    with (
        open_rodex_read_transaction(database),
        pytest.raises(RodexSQLError, match="blocked by active connections"),
        open_rodex_maintenance_lock(database),
    ):
        pass


def test_round3_audit_snapshot_reads_committed_external_wal_content(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    writer = sqlite3.connect(database, isolation_level=None)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO marker VALUES ('external-wal')")
        assert Path(f"{database}-wal").exists()
        with open_rodex_read_transaction(database) as connection:
            rows = connection.execute("SELECT value FROM marker ORDER BY rowid").fetchall()
            assert rows == [("validated",), ("external-wal",)]
    finally:
        writer.close()


def test_round3_audit_snapshot_reads_wal_committed_by_an_external_process(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    script = (
        "import sqlite3,sys; "
        "c=sqlite3.connect(sys.argv[1], isolation_level=None); "
        "c.execute('PRAGMA wal_autocheckpoint = 0'); "
        "c.execute(\"INSERT INTO marker VALUES ('external-process')\"); "
        "print('ready', flush=True); sys.stdin.readline(); c.close()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, os.fspath(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        assert Path(f"{database}-wal").exists()
        with open_rodex_read_transaction(database) as connection:
            rows = connection.execute("SELECT value FROM marker ORDER BY rowid").fetchall()
        assert rows == [("validated",), ("external-process",)]
    finally:
        assert process.stdin is not None
        process.stdin.write("stop\n")
        process.stdin.flush()
        process.wait(timeout=2)


@pytest.mark.parametrize("failing_open", [1, 2, 3], ids=["parent", "lock", "database"])
def test_round3_emfile_at_each_private_open_boundary_leaks_no_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_open: int,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    real_open = private_path_module.os.open
    calls = 0
    baseline = _descriptor_count()

    def fail_selected_open(*args: object, **kwargs: object) -> int:
        nonlocal calls
        calls += 1
        if calls == failing_open:
            raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(private_path_module.os, "open", fail_selected_open)
    with pytest.raises(RodexSQLError), open_rodex_read_transaction(database):
        pass
    assert _descriptor_count() == baseline


def test_round3_guard_manager_pipe_failure_closes_its_inotify_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _descriptor_count()

    def fail_pipe(_flags: int) -> tuple[int, int]:
        raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))

    monkeypatch.setattr(guard_module.os, "pipe2", fail_pipe)
    with pytest.raises(OSError, match="Too many open files"):
        guard_module._InotifyGuardManager()
    assert _descriptor_count() == baseline


def test_round3_sqlite_crash_boundary_is_explicit_for_the_configured_mode(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    with open_rodex_bootstrap_transaction(database) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        synchronous = connection.execute("PRAGMA synchronous").fetchone()

    assert journal_mode == ("wal",)
    assert synchronous == (1,)

    repository = Path(__file__).resolve().parents[2]
    documented_boundary = "\n".join(
        (repository / relative).read_text(encoding="utf-8").lower()
        for relative in ("README.md", "docs/SECURITY.md", "docs/SQL_SCHEMA.md")
    )
    assert "synchronous" in documented_boundary and "normal" in documented_boundary
    assert any(
        phrase in documented_boundary
        for phrase in ("power loss", "power failure", "operating-system crash", "os crash")
    )
    assert any(
        phrase in documented_boundary
        for phrase in ("may lose", "can lose", "not guaranteed", "durability boundary")
    )
