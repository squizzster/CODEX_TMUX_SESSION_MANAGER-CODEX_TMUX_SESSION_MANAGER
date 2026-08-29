from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import enumerate as enumerate_threads

import pytest

import rodex.analytics as analytics_module
import rodex.machine_commands as machine_commands_module
import rodex.session_commands as session_commands_module
import rodex_sql.transactions as transactions_module
from rodex.cli import main
from rodex_sql import (
    RodexDatabaseMovedError,
    database_terminal_signal,
    open_rodex_bootstrap_transaction,
    open_rodex_read_transaction,
    open_rodex_transaction,
)
from rodex_sql.private_database_path import open_private_database_boundary

guard_module = importlib.import_module("rodex_sql.database_location_guard")


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
def test_round3_move_spanning_every_transaction_boundary_is_terminal_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    database = tmp_path / "registry.sqlite3"
    moved = tmp_path / "moved.sqlite3"
    _marker_database(database)
    guard = database_terminal_signal(database)
    real_identity = transactions_module.require_database_identity
    real_main_path = transactions_module.require_sqlite_main_path
    moved_once = False

    def maybe_move_identity(opened: object, guard: object, *, stage: str) -> None:
        nonlocal moved_once
        if not moved_once and stage == target_stage:
            moved_once = True
            database.replace(moved)
        real_identity(opened, guard, stage=stage)

    def maybe_move_main(connection: object, opened: object, guard: object) -> None:
        nonlocal moved_once
        if not moved_once and stage == "post_connect":
            moved_once = True
            database.replace(moved)
        real_main_path(connection, opened, guard)

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
    assert guard.terminal_event.is_set()


def test_round3_rapid_move_away_and_back_still_latches_terminal_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    away = tmp_path / "away.sqlite3"
    _marker_database(database)
    guard = database_terminal_signal(database)

    with (
        pytest.raises(RodexDatabaseMovedError, match="please restart Rodex"),
        open_rodex_read_transaction(database) as connection,
    ):
        assert connection.execute("SELECT value FROM marker").fetchone() == ("stable",)
        database.replace(away)
        away.replace(database)

    assert guard.terminal_event.is_set()
    assert guard.terminal_reason is not None
    with pytest.raises(RodexDatabaseMovedError), open_rodex_read_transaction(database):
        pass


def test_round3_parent_move_away_and_back_latches_the_database(tmp_path: Path) -> None:
    private_parent = tmp_path / "private"
    private_parent.mkdir(mode=0o700)
    database = private_parent / "registry.sqlite3"
    moved_parent = tmp_path / "moved-private"
    _marker_database(database)

    with (
        pytest.raises(RodexDatabaseMovedError, match="please restart Rodex"),
        open_rodex_read_transaction(database),
    ):
        private_parent.replace(moved_parent)
        moved_parent.replace(private_parent)


def test_round3_inotify_queue_overflow_is_permanently_terminal(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    guard = database_terminal_signal(database)

    guard._manager.inject_overflow_for_testing()

    assert guard.terminal_event.is_set()
    assert guard.terminal_reason == "database location guard queue overflowed"
    with (
        pytest.raises(RodexDatabaseMovedError, match="queue overflowed"),
        open_rodex_read_transaction(database),
    ):
        pass


def test_round3_location_guard_subscription_is_once_and_immediate_after_latch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    away = tmp_path / "away.sqlite3"
    _marker_database(database)
    guard = database_terminal_signal(database)
    notices: list[tuple[Path, str]] = []
    unsubscribe = guard.subscribe_terminal(
        lambda path, reason: notices.append((path, reason))
    )

    database.replace(away)
    with pytest.raises(RodexDatabaseMovedError):
        guard.require_available("test")
    assert len(notices) == 1

    late: list[tuple[Path, str]] = []
    guard.subscribe_terminal(lambda path, reason: late.append((path, reason)))
    assert late == [(database, guard.terminal_reason)]
    unsubscribe()


def test_round3_guard_establishment_performs_no_writes_or_polling(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite3"
    _marker_database(database)
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
    }

    guard = database_terminal_signal(database)

    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
    }
    assert not guard.terminal_event.is_set()
    assert after == before
    source = Path(guard_module.__file__).read_text(encoding="utf-8")
    assert "time.sleep" not in source
    assert "select.select" in source


def test_round3_sibling_guard_does_not_inherit_pre_registration_parent_events(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    _marker_database(first)
    with open_rodex_read_transaction(first):
        pass

    _marker_database(second)
    with open_rodex_read_transaction(second) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("stable",)

    assert not database_terminal_signal(first).terminal_event.is_set()
    assert not database_terminal_signal(second).terminal_event.is_set()


def test_round3_external_process_swap_back_latches_terminal_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.sqlite3"
    away = tmp_path / "away.sqlite3"
    _marker_database(database)
    guard = database_terminal_signal(database)

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,sys; os.rename(sys.argv[1],sys.argv[2]); "
            "os.rename(sys.argv[2],sys.argv[1])",
            os.fspath(database),
            os.fspath(away),
        ],
        check=True,
    )

    with pytest.raises(RodexDatabaseMovedError, match="please restart Rodex"):
        guard.require_available("external_swap")


def test_round3_known_location_is_never_recreated_after_move(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite3"
    away = tmp_path / "away.sqlite3"
    _marker_database(database)
    database_terminal_signal(database)
    database.replace(away)

    with (
        pytest.raises(RodexDatabaseMovedError, match="please restart Rodex"),
        open_rodex_bootstrap_transaction(database),
    ):
        pass
    with (
        pytest.raises(RodexDatabaseMovedError, match="please restart Rodex"),
        open_rodex_transaction(database),
    ):
        pass

    assert not database.exists()
    assert _rows(away) == [("stable",)]


def test_round3_guard_worker_and_descriptors_are_reclaimed(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite3"
    before_fds = len(os.listdir("/proc/self/fd"))
    before_workers = sum(
        thread.name == "rodex-database-location-guard" for thread in enumerate_threads()
    )

    manager = guard_module._InotifyGuardManager()
    try:
        with (
            open_private_database_boundary(
                database,
                create=True,
            ) as boundary,
            boundary.open_database(
                writable=True,
                create=True,
            ) as opened,
        ):
            manager.get_or_create(opened)

        assert len(os.listdir("/proc/self/fd")) == before_fds + 3
        assert (
            sum(
                thread.name == "rodex-database-location-guard"
                for thread in enumerate_threads()
            )
            == before_workers + 1
        )
    finally:
        manager.close()
    assert len(os.listdir("/proc/self/fd")) == before_fds
    assert (
        sum(
            thread.name == "rodex-database-location-guard" for thread in enumerate_threads()
        )
        == before_workers
    )


def test_round3_terminal_storage_error_is_not_downgraded_by_best_effort_paths(
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


def test_round3_cli_reports_terminal_storage_failure_with_restart_guidance(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "registry.sqlite3"
    away = tmp_path / "away.sqlite3"
    _marker_database(database)
    database_terminal_signal(database)
    database.replace(away)
    away.replace(database)
    monkeypatch.setenv("RODEX_DATABASE_PATH", os.fspath(database))
    monkeypatch.setattr(sys, "argv", ["rodex", "_stats-status", "unused"])

    with pytest.raises(SystemExit) as exit_error:
        main()

    captured = capsys.readouterr()
    assert exit_error.value.code == 1
    assert captured.out == ""
    assert "database_moved" in captured.err
    assert "please restart Rodex" in captured.err
