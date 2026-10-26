from __future__ import annotations

import os
import pwd
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import rodex_registry.lifecycle as session_module
from rodex_registry import (
    RodexSessionError,
    RodexSessionsUserIdentity,
    create_a_rodex_session,
    initialise_rodex_database,
    lookup_rodex_session_log,
    lookup_rodex_sessions_user,
    record_a_rodex_session_access,
)

ALICE = RodexSessionsUserIdentity(uid=1001, gid=1002, user_name="alice")
BOB = RodexSessionsUserIdentity(uid=2001, gid=2002, user_name="bob")
CODEX_SESSION_ID_1 = uuid.UUID(int=(1 << 120) + 1)
CODEX_SESSION_ID_2 = uuid.UUID(int=(1 << 120) + 2)


def fetch_all(database: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(query).fetchall()


def test_sessions_log_uses_the_exact_plural_table_name(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    tables = fetch_all(
        database,
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
    )

    assert ("rodex_sessions_log",) in tables
    assert ("rodex_session_log",) not in tables


def test_sessions_log_has_the_requested_columns_and_types(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    columns = fetch_all(database, "PRAGMA table_info(rodex_sessions_log)")

    assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("created_at_utc", "TEXT", 1, 0),
        ("rodex_sessions_users_id", "INTEGER", 1, 0),
        ("last_accessed_at_utc", "TEXT", 1, 0),
    ]


def test_sessions_log_id_uses_autoincrement(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    definition = fetch_all(
        database,
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rodex_sessions_log'",
    )[0][0]

    assert isinstance(definition, str)
    assert "ID INTEGER PRIMARY KEY AUTOINCREMENT" in definition.upper()


def test_sessions_log_connecting_field_follows_table_name_lookup_field_rule(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    foreign_keys = fetch_all(database, "PRAGMA foreign_key_list(rodex_sessions_log)")

    assert {(row[2], row[3], row[4]) for row in foreign_keys} == {
        ("rodex_sessions", "rodex_sessions_id", "id"),
        ("rodex_sessions_users", "rodex_sessions_users_id", "id"),
    }


def test_sessions_log_has_named_unique_index_on_rodex_sessions_id(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    indexes = fetch_all(database, "PRAGMA index_list(rodex_sessions_log)")
    columns = fetch_all(
        database,
        "PRAGMA index_info(rodex_sessions_log_rodex_sessions_id_unique)",
    )

    assert [(row[1], row[2]) for row in indexes] == [("rodex_sessions_log_rodex_sessions_id_unique", 1)]
    assert [row[2] for row in columns] == ["rodex_sessions_id"]


def test_creating_a_session_also_creates_its_one_log_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "rodex.sqlite3"
    timestamp = "2026-08-15T12:34:56.123456Z"
    monkeypatch.setattr(session_module, "_utc_now_timestamp", lambda: timestamp)

    session = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID_1, user_identity=ALICE)

    assert fetch_all(
        database,
        "SELECT id, rodex_sessions_id, created_at_utc, rodex_sessions_users_id, "
        "last_accessed_at_utc FROM rodex_sessions_log",
    ) == [(1, session.rodex_sessions_id, timestamp, 1, timestamp)]


def test_session_user_defaults_to_the_posix_operating_system_identity(
    tmp_path: Path, executing_user: RodexSessionsUserIdentity
) -> None:
    database = tmp_path / "rodex.sqlite3"

    session = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID_1)

    assert database.stat().st_uid == executing_user.uid
    log = lookup_rodex_session_log(session.rodex_sessions_id, database)
    assert log is not None
    assert lookup_rodex_sessions_user(log.rodex_sessions_users_id, database) == (
        session_module.RodexSessionsUser(
            id=1, uid=executing_user.uid, gid=executing_user.gid, user_name=executing_user.user_name
        )
    )


def test_synthetic_session_identity_does_not_change_storage_identity_or_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executing_user: RodexSessionsUserIdentity,
    foreign_user: RodexSessionsUserIdentity,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    real_identity_functions = os.getuid, os.getgid, pwd.getpwuid
    with monkeypatch.context() as identity_patch:
        identity_patch.setattr(session_module, "current_rodex_sessions_user_identity", lambda: foreign_user)

        synthetic = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID_1)

        assert (os.getuid, os.getgid, pwd.getpwuid) == real_identity_functions
        assert os.getuid() == database.stat().st_uid == executing_user.uid
        log = lookup_rodex_session_log(synthetic.rodex_sessions_id, database)
        assert log is not None
        assert lookup_rodex_sessions_user(log.rodex_sessions_users_id, database) == session_module.RodexSessionsUser(
            id=1, uid=foreign_user.uid, gid=foreign_user.gid, user_name=foreign_user.user_name
        )

    actual = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID_2)
    log = lookup_rodex_session_log(actual.rodex_sessions_id, database)
    assert log is not None
    assert session_module.current_rodex_sessions_user_identity() == executing_user
    assert lookup_rodex_sessions_user(log.rodex_sessions_users_id, database) == session_module.RodexSessionsUser(
        id=2, uid=executing_user.uid, gid=executing_user.gid, user_name=executing_user.user_name
    )


@pytest.mark.parametrize("invalid_user", ["", "   "])
def test_user_name_must_not_be_empty(tmp_path: Path, invalid_user: str) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        create_a_rodex_session(
            tmp_path / "rodex.sqlite3",
            codex_session_id=CODEX_SESSION_ID_1,
            user_identity=RodexSessionsUserIdentity(1, 1, invalid_user),
        )


def test_log_ids_auto_increment_independently_from_session_ids(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    first = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID_1, user_identity=ALICE)
    second = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID_2, user_identity=BOB)

    assert fetch_all(
        database,
        "SELECT id, rodex_sessions_id FROM rodex_sessions_log ORDER BY id",
    ) == [(1, first.rodex_sessions_id), (2, second.rodex_sessions_id)]
    assert fetch_all(
        database,
        "SELECT name, seq FROM sqlite_sequence WHERE name = 'rodex_sessions_log'",
    ) == [("rodex_sessions_log", 2)]


def test_unique_index_rejects_a_second_log_for_the_same_session(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID_1, user_identity=ALICE)

    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"),
    ):
        connection.execute(
            "INSERT INTO rodex_sessions_log "
            "(rodex_sessions_id, created_at_utc, rodex_sessions_users_id, "
            "last_accessed_at_utc) VALUES (?, ?, ?, ?)",
            (session.rodex_sessions_id, "now", 1, "now"),
        )


def test_foreign_key_rejects_a_log_for_an_unknown_session(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            connection.execute(
                "INSERT INTO rodex_sessions_log "
                "(rodex_sessions_id, created_at_utc, rodex_sessions_users_id, "
                "last_accessed_at_utc) VALUES (999, 'now', 999, 'now')"
            )


def test_lookup_returns_the_log_for_a_session(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID_1, user_identity=ALICE)

    log = lookup_rodex_session_log(session.rodex_sessions_id, database)

    assert log is not None
    assert log.id == 1
    assert log.rodex_sessions_id == session.rodex_sessions_id
    assert log.rodex_sessions_users_id == 1


def test_lookup_returns_none_when_a_session_has_no_log(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    assert lookup_rodex_session_log(999, database) is None


def test_record_access_changes_only_the_last_access_timestamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = "2026-08-15T10:00:00.000000Z"
    monkeypatch.setattr(session_module, "_utc_now_timestamp", lambda: created)
    session = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID_1, user_identity=ALICE)
    accessed = datetime(2026, 8, 15, 11, 30, tzinfo=UTC)

    updated = record_a_rodex_session_access(session.rodex_sessions_id, database, accessed_at_utc=accessed)

    assert updated.created_at_utc == created
    assert updated.rodex_sessions_users_id == 1
    assert updated.last_accessed_at_utc == "2026-08-15T11:30:00.000000Z"


def test_access_timestamp_is_converted_to_utc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        session_module,
        "_utc_now_timestamp",
        lambda: "2026-08-15T10:00:00.000000Z",
    )
    session = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID_1)
    plus_two = timezone(timedelta(hours=2))

    updated = record_a_rodex_session_access(
        session.rodex_sessions_id,
        database,
        accessed_at_utc=datetime(2026, 8, 15, 14, 0, tzinfo=plus_two),
    )

    assert updated.last_accessed_at_utc == "2026-08-15T12:00:00.000000Z"


def test_access_rejects_a_naive_datetime(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID_1)

    with pytest.raises(ValueError, match="timezone-aware"):
        record_a_rodex_session_access(
            session.rodex_sessions_id,
            database,
            accessed_at_utc=datetime(2026, 8, 15, 12, 0),
        )


def test_access_reports_a_missing_session_log(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    with pytest.raises(RodexSessionError, match="does not exist"):
        record_a_rodex_session_access(999, database)


@pytest.mark.parametrize("bad_id", [0, -1, True])
def test_log_helpers_reject_invalid_session_ids(tmp_path: Path, bad_id: object) -> None:
    database = tmp_path / "rodex.sqlite3"

    with pytest.raises(ValueError, match="positive integer"):
        lookup_rodex_session_log(bad_id, database)  # type: ignore[arg-type]
