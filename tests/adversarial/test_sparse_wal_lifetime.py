from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

import rodex_sql.transactions as transactions_module
from rodex_sql import (
    open_rodex_bootstrap_transaction,
    open_rodex_read_transaction,
    open_rodex_transaction,
)


def _close_process_wal_owner() -> None:
    close = getattr(
        transactions_module,
        "_close_process_wal_lifetime_owner",
        None,
    )
    if close is not None:
        close()


@pytest.fixture(autouse=True)
def _isolate_process_wal_owner() -> Iterator[None]:
    _close_process_wal_owner()
    yield
    _close_process_wal_owner()


def _marker_database(path: Path) -> None:
    with open_rodex_bootstrap_transaction(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")


def _sidecar(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.name}{suffix}")


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


def test_sparse_writes_reuse_one_live_wal_inode_until_clean_close(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    wal = _sidecar(database, "-wal")
    shm = _sidecar(database, "-shm")

    assert wal.exists()
    assert shm.exists()
    wal_identity = (wal.stat().st_dev, wal.stat().st_ino)
    shm_identity = (shm.stat().st_dev, shm.stat().st_ino)

    for value in range(25):
        with open_rodex_transaction(database) as connection:
            connection.execute("INSERT INTO marker VALUES (?)", (str(value),))
        time.sleep(0.002)
        assert (wal.stat().st_dev, wal.stat().st_ino) == wal_identity
        assert (shm.stat().st_dev, shm.stat().st_ino) == shm_identity

    _close_process_wal_owner()

    assert not wal.exists()
    assert not shm.exists()


def test_transactions_reuse_one_process_local_validated_database_descriptor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    owner = transactions_module._PROCESS_WAL_LIFETIME_OWNER
    assert owner is not None
    descriptor = owner.database_descriptor
    identity = os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino

    for value in range(10):
        with open_rodex_transaction(database) as connection:
            connection.execute("INSERT INTO marker VALUES (?)", (str(value),))
        with open_rodex_read_transaction(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM marker").fetchone() == (
                value + 1,
            )
        current = transactions_module._PROCESS_WAL_LIFETIME_OWNER
        assert current is owner
        assert current.database_descriptor == descriptor
        assert (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) == identity


def test_other_process_close_cannot_remove_sidecars_while_owner_is_active(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    wal = _sidecar(database, "-wal")
    shm = _sidecar(database, "-shm")
    wal_identity = wal.stat().st_dev, wal.stat().st_ino
    shm_identity = shm.stat().st_dev, shm.stat().st_ino
    probe = """
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
try:
    assert connection.execute('SELECT COUNT(*) FROM marker').fetchone() == (0,)
finally:
    connection.close()
"""

    subprocess.run([sys.executable, "-c", probe, os.fspath(database)], check=True)

    assert (wal.stat().st_dev, wal.stat().st_ino) == wal_identity
    assert (shm.stat().st_dev, shm.stat().st_ino) == shm_identity


def test_sparse_write_syscalls_do_not_recreate_sidecars_per_commit(
    tmp_path: Path,
) -> None:
    strace = shutil.which("strace")
    if strace is None:
        pytest.skip("strace is required for the physical WAL lifecycle budget")
    database = tmp_path / "registry.sqlite3"
    trace = tmp_path / "sparse-writes.strace"
    _marker_database(database)
    _close_process_wal_owner()
    probe = """
import sys
import time
from pathlib import Path

import rodex_sql.transactions as transactions
from rodex_sql import open_rodex_transaction

database = Path(sys.argv[1])
for value in range(25):
    with open_rodex_transaction(database) as connection:
        connection.execute("INSERT INTO marker VALUES (?)", (str(value),))
    time.sleep(0.002)
close = getattr(transactions, "_close_process_wal_lifetime_owner", None)
if close is not None:
    close()
"""

    subprocess.run(
        [
            strace,
            "-qq",
            "-o",
            os.fspath(trace),
            "-e",
            "trace=fsync,pwrite64,ftruncate,unlink,unlinkat",
            sys.executable,
            "-c",
            probe,
            os.fspath(database),
        ],
        check=True,
    )

    calls = trace.read_text(encoding="utf-8").splitlines()
    fsync_calls = sum("fsync(" in call for call in calls)
    unlink_calls = sum("unlink(" in call or "unlinkat(" in call for call in calls)
    truncate_calls = sum("ftruncate(" in call for call in calls)
    assert fsync_calls <= 8, f"25 sparse commits issued {fsync_calls} fsync calls"
    assert unlink_calls <= 4, f"25 sparse commits issued {unlink_calls} sidecar unlinks"
    assert truncate_calls <= 4, (
        f"25 sparse commits issued {truncate_calls} sidecar truncations"
    )


def test_twenty_clients_retain_wal_until_the_last_clean_process_exit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    _close_process_wal_owner()
    wal = _sidecar(database, "-wal")
    shm = _sidecar(database, "-shm")
    probe = """
import sys
from pathlib import Path

from rodex_sql import open_rodex_transaction
from rodex_sql.transactions import _close_process_wal_lifetime_owner

database = Path(sys.argv[1])
value = sys.argv[2]
print("waiting", flush=True)
sys.stdin.readline()
with open_rodex_transaction(database) as connection:
    connection.execute("INSERT INTO marker VALUES (?)", (value,))
print("committed", flush=True)
sys.stdin.readline()
_close_process_wal_lifetime_owner()
"""
    clients = [
        subprocess.Popen(
            [sys.executable, "-c", probe, os.fspath(database), str(index)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        for index in range(20)
    ]
    try:
        for client in clients:
            assert client.stdout is not None
            assert client.stdout.readline().strip() == "waiting"
        for client in clients:
            assert client.stdin is not None
            client.stdin.write("commit\n")
            client.stdin.flush()
        for client in clients:
            assert client.stdout is not None
            assert client.stdout.readline().strip() == "committed"

        assert wal.exists()
        assert shm.exists()
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM marker").fetchone() == (20,)
        assert wal.exists()
        assert shm.exists()

        for client in clients:
            assert client.stdin is not None
            client.stdin.write("stop\n")
            client.stdin.flush()
        for client in clients:
            assert client.wait(timeout=5) == 0

        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute("SELECT COUNT(*) FROM marker").fetchone() == (20,)
        assert not wal.exists()
        assert not shm.exists()
    finally:
        for client in clients:
            if client.poll() is None:
                client.terminate()
            client.wait(timeout=5)


def test_process_wal_owner_is_closed_before_fork_and_recreated_per_process(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    parent_owner = transactions_module._PROCESS_WAL_LIFETIME_OWNER
    assert parent_owner is not None
    assert parent_owner.process_id == os.getpid()
    read_descriptor, write_descriptor = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        try:
            os.close(read_descriptor)
            inherited_owner = transactions_module._PROCESS_WAL_LIFETIME_OWNER
            with open_rodex_transaction(database) as connection:
                connection.execute("INSERT INTO marker VALUES ('child')")
            child_owner = transactions_module._PROCESS_WAL_LIFETIME_OWNER
            payload = json.dumps(
                {
                    "inherited_owner_absent": inherited_owner is None,
                    "child_owner_pid": (
                        None if child_owner is None else child_owner.process_id
                    ),
                    "child_pid": os.getpid(),
                }
            ).encode()
            os.write(write_descriptor, payload)
            _close_process_wal_owner()
        finally:
            os.close(write_descriptor)
            os._exit(0)

    os.close(write_descriptor)
    try:
        payload = json.loads(os.read(read_descriptor, 4096))
    finally:
        os.close(read_descriptor)
        waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert payload == {
        "inherited_owner_absent": True,
        "child_owner_pid": child_pid,
        "child_pid": child_pid,
    }
    assert transactions_module._PROCESS_WAL_LIFETIME_OWNER is None
    with open_rodex_transaction(database) as connection:
        connection.execute("INSERT INTO marker VALUES ('parent')")
    replacement_parent_owner = transactions_module._PROCESS_WAL_LIFETIME_OWNER
    assert replacement_parent_owner is not None
    assert replacement_parent_owner is not parent_owner
    assert replacement_parent_owner.process_id == os.getpid()


def test_ordinary_subprocess_launch_does_not_close_parent_wal_owner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    parent_owner = transactions_module._PROCESS_WAL_LIFETIME_OWNER
    assert parent_owner is not None
    descriptor = parent_owner.database_descriptor

    subprocess.run([sys.executable, "-c", "pass"], check=True)

    assert transactions_module._PROCESS_WAL_LIFETIME_OWNER is parent_owner
    assert os.fstat(descriptor).st_ino == parent_owner.database_state.st_ino


def test_wal_owner_closes_sqlite_before_validated_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[str, int | None]] = []

    class Connection:
        def close(self) -> None:
            events.append(("sqlite", None))

    owner = transactions_module._ProcessWalLifetimeOwner(
        process_id=os.getpid(),
        database_path=tmp_path / "registry.sqlite3",
        storage_identity=transactions_module._WalStorageIdentity(
            parent=(1, 2),
            transition_lock=(3, 4),
            database=(5, 6),
        ),
        database_descriptor=123,
        database_state=os.stat_result((0,) * 10),
        connection=Connection(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        transactions_module.os,
        "close",
        lambda descriptor: events.append(("descriptor", descriptor)),
    )

    owner.close()

    assert events == [("sqlite", None), ("descriptor", 123)]


def test_wal_owner_resource_and_growth_budgets_are_explicit(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)

    with open_rodex_transaction(database) as connection:
        assert connection.execute("PRAGMA wal_autocheckpoint").fetchone() == (
            transactions_module._WAL_AUTOCHECKPOINT_PAGES,
        )
        assert connection.execute("PRAGMA journal_size_limit").fetchone() == (
            transactions_module._WAL_JOURNAL_SIZE_LIMIT_BYTES,
        )
    for value in range(1_250):
        with open_rodex_transaction(database) as connection:
            connection.execute("INSERT INTO marker VALUES (?)", ("x" * 4096 + str(value),))

    owner = transactions_module._PROCESS_WAL_LIFETIME_OWNER
    assert owner is not None
    assert owner.process_id == os.getpid()
    assert owner.database_path == database
    assert len(_open_targets_below(tmp_path)) <= 5
    wal = _sidecar(database, "-wal")
    assert wal.stat().st_size <= transactions_module._WAL_JOURNAL_SIZE_LIMIT_BYTES

    _close_process_wal_owner()

    assert _open_targets_below(tmp_path) == []


def test_process_retains_only_its_current_database_wal_lifetime(tmp_path: Path) -> None:
    first_database = tmp_path / "first.sqlite3"
    second_database = tmp_path / "second.sqlite3"
    _marker_database(first_database)

    assert _sidecar(first_database, "-wal").exists()
    assert transactions_module._PROCESS_WAL_LIFETIME_OWNER is not None
    assert transactions_module._PROCESS_WAL_LIFETIME_OWNER.database_path == first_database

    _marker_database(second_database)

    assert not _sidecar(first_database, "-wal").exists()
    assert not _sidecar(first_database, "-shm").exists()
    assert _sidecar(second_database, "-wal").exists()
    assert transactions_module._PROCESS_WAL_LIFETIME_OWNER is not None
    assert transactions_module._PROCESS_WAL_LIFETIME_OWNER.database_path == second_database


def test_committed_wal_survives_process_crash_without_owner_cleanup(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    _close_process_wal_owner()
    probe = """
import os
import sys
from pathlib import Path

from rodex_sql import open_rodex_transaction

with open_rodex_transaction(Path(sys.argv[1])) as connection:
    connection.execute("INSERT INTO marker VALUES ('committed-before-crash')")
os._exit(23)
"""

    completed = subprocess.run(
        [sys.executable, "-c", probe, os.fspath(database)],
        check=False,
    )

    assert completed.returncode == 23
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT value FROM marker").fetchall() == [
            ("committed-before-crash",)
        ]
