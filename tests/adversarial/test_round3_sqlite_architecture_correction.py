from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

import rodex.analytics as analytics_module
import rodex.machine_commands as machine_commands_module
import rodex.session_commands as session_commands_module
import rodex_sql.sqlite_identity as sqlite_identity_module
import rodex_sql.transactions as transactions_module
from rodex_sql import (
    RodexDatabaseMovedError,
    RodexDatabaseNotFoundError,
    RodexSQLError,
    open_rodex_bootstrap_transaction,
    open_rodex_read_transaction,
    open_rodex_transaction,
)
from rodex_sql.private_database_path import open_private_database_boundary


def _marker_database(path: Path) -> None:
    with open_rodex_bootstrap_transaction(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('stable')")


def _rows(path: Path) -> list[tuple[str]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT value FROM marker ORDER BY rowid").fetchall()


@pytest.mark.parametrize(
    "stage",
    ["pre_connect", "post_connect", "pre_begin", "pre_commit"],
)
def test_round3_move_spanning_every_transaction_boundary_is_rejected_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    database = tmp_path / "registry.sqlite3"
    moved = tmp_path / "moved.sqlite3"
    _marker_database(database)
    real_identity = transactions_module.require_database_identity
    real_main_path = transactions_module.require_sqlite_main_path
    moved_once = False

    def maybe_move_identity(opened: object, *, stage: str) -> None:
        nonlocal moved_once
        if not moved_once and stage == target_stage:
            moved_once = True
            database.replace(moved)
        real_identity(opened, stage=stage)

    def maybe_move_main(connection: object, opened: object) -> None:
        nonlocal moved_once
        if not moved_once and stage == "post_connect":
            moved_once = True
            database.replace(moved)
        real_main_path(connection, opened)

    target_stage = stage
    monkeypatch.setattr(
        transactions_module,
        "require_database_identity",
        maybe_move_identity,
    )
    monkeypatch.setattr(
        transactions_module,
        "require_sqlite_main_path",
        maybe_move_main,
    )

    with (
        pytest.raises(
            RodexDatabaseMovedError,
            match=r"database_moved: .*please restart Rodex",
        ),
        open_rodex_transaction(database) as connection,
    ):
        connection.execute("INSERT INTO marker VALUES ('uncommitted')")

    assert moved_once
    assert _rows(moved) == [("stable",)]


def test_round3_valid_replacement_is_rejected_on_the_next_transaction(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    original = tmp_path / "original.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    _marker_database(database)
    shutil.copyfile(database, replacement)
    replacement.chmod(0o600)
    database.replace(original)
    replacement.replace(database)

    with (
        pytest.raises(RodexDatabaseMovedError, match="identity mismatch"),
        open_rodex_read_transaction(database),
    ):
        pass

    assert _rows(original) == [("stable",)]


def test_round3_bootstrap_does_not_recreate_previously_admitted_missing_storage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    moved = tmp_path / "moved.sqlite3"
    _marker_database(database)
    database.replace(moved)

    with (
        pytest.raises(RodexDatabaseNotFoundError, match="database does not exist"),
        open_rodex_bootstrap_transaction(database),
    ):
        pass

    assert not database.exists()
    assert _rows(moved) == [("stable",)]


def test_round3_transactions_leave_no_watcher_resources(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite3"
    probe = """
import json
import os
import sys
from pathlib import Path
from threading import enumerate as enumerate_threads

from rodex_sql import (
    open_rodex_bootstrap_transaction,
    open_rodex_read_transaction,
    open_rodex_transaction,
)

database = Path(sys.argv[1])
threads_before = sorted((thread.name, thread.daemon) for thread in enumerate_threads())
with open_rodex_bootstrap_transaction(database) as connection:
    connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
with open_rodex_transaction(database) as connection:
    connection.execute("INSERT INTO marker VALUES ('stable')")
with open_rodex_read_transaction(database) as connection:
    assert connection.execute("SELECT value FROM marker").fetchone() == ("stable",)

targets = []
for descriptor in os.listdir("/proc/self/fd"):
    try:
        targets.append(os.readlink(f"/proc/self/fd/{descriptor}"))
    except OSError:
        pass
print(json.dumps({
    "threads_before": threads_before,
    "threads_after": sorted(
        (thread.name, thread.daemon) for thread in enumerate_threads()
    ),
    "fd_targets": targets,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, os.fspath(database)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["threads_after"] == result["threads_before"]
    assert not any("inotify" in target for target in result["fd_targets"])
    assert not any(os.fspath(tmp_path) in target for target in result["fd_targets"])


def test_round3_missing_proc_descriptor_path_fails_with_storage_identity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "registry.sqlite3"
    with (
        open_private_database_boundary(database, create=True) as boundary,
        boundary.open_database(writable=True, create=True) as opened,
    ):
        monkeypatch.setattr(sqlite_identity_module.Path, "exists", lambda _path: False)
        with pytest.raises(RodexDatabaseMovedError, match="/proc/self/fd is unavailable"):
            sqlite_identity_module.validated_database_uri(opened, read_only=False)


def test_round3_connect_error_revalidates_changed_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "registry.sqlite3"
    moved = tmp_path / "moved.sqlite3"
    _marker_database(database)

    def fail_connect(*_args: object, **_kwargs: object) -> None:
        database.replace(moved)
        raise RodexSQLError("synthetic connect failure")

    monkeypatch.setattr(
        transactions_module,
        "_connect_validated_database",
        fail_connect,
    )

    with (
        pytest.raises(RodexDatabaseMovedError, match="connect_error identity check failed"),
        open_rodex_read_transaction(database),
    ):
        pass


def test_round3_sqlite_error_revalidates_changed_storage(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite3"
    moved = tmp_path / "moved.sqlite3"
    _marker_database(database)

    with (
        pytest.raises(RodexDatabaseMovedError, match="sqlite_error identity check failed"),
        open_rodex_transaction(database),
    ):
        database.replace(moved)
        raise sqlite3.OperationalError("synthetic SQLite failure")


def test_round3_storage_identity_error_is_not_downgraded_by_best_effort_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "registry.sqlite3"
    moved = RodexDatabaseMovedError(database, "test replacement")
    monkeypatch.setattr(
        session_commands_module,
        "record_a_rodex_session_access",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(moved),
    )
    with pytest.raises(RodexDatabaseMovedError) as session_error:
        session_commands_module._record_access_best_effort(1, database)
    assert session_error.value is moved
    assert machine_commands_module._machine_error_classification(moved) == (
        "database_moved",
        False,
        1,
    )

    class MovedRegistry:
        def record_health_transition(self, **_kwargs: object) -> None:
            raise moved

    worker = object.__new__(analytics_module.AnalyticsRolloutWorker)
    worker._session_id = 1
    worker._expected_codex_session_id = object()
    worker._registry = MovedRegistry()
    worker._last_health_transition = None
    worker._last_failure_health_fingerprint = None
    worker._consecutive_failures = 0
    worker._now = lambda: datetime.now(UTC)
    with pytest.raises(RodexDatabaseMovedError) as analytics_error:
        worker._project_health("degraded", "test", object())
    assert analytics_error.value is moved
