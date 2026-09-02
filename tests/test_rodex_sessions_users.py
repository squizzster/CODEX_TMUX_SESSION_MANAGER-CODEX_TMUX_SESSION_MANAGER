from __future__ import annotations

import os
import pwd
import sqlite3
import uuid
from pathlib import Path

import pytest

from rodex_registry import (
    RodexSessionError,
    RodexSessionsUser,
    RodexSessionsUserIdentity,
    create_a_rodex_session,
    current_rodex_sessions_user_identity,
    initialise_rodex_database,
    lookup_or_create_rodex_sessions_user,
    lookup_rodex_session_log,
    lookup_rodex_sessions_user,
)

CODEX_SESSION_ID_1 = uuid.UUID(int=(1 << 120) + 1)
CODEX_SESSION_ID_2 = uuid.UUID(int=(1 << 120) + 2)


def fetch_all(database: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(query).fetchall()


def test_sessions_users_has_the_requested_lookup_schema(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    columns = fetch_all(database, "PRAGMA table_info(rodex_sessions_users)")

    assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
        ("id", "INTEGER", 0, 1),
        ("uid", "INTEGER", 1, 0),
        ("gid", "INTEGER", 1, 0),
        ("user_name", "TEXT", 1, 0),
    ]


def test_sessions_users_id_uses_autoincrement(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    definition = fetch_all(
        database,
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'rodex_sessions_users'",
    )[0][0]

    assert isinstance(definition, str)
    assert "ID INTEGER PRIMARY KEY AUTOINCREMENT" in definition.upper()


def test_sessions_users_has_the_exact_composite_unique_index(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    indexes = fetch_all(database, "PRAGMA index_list(rodex_sessions_users)")
    columns = fetch_all(
        database,
        "PRAGMA index_info(rodex_sessions_users_uid_gid_user_name_unique)",
    )

    assert [(row[1], row[2]) for row in indexes] == [
        ("rodex_sessions_users_uid_gid_user_name_unique", 1)
    ]
    assert [row[2] for row in columns] == ["uid", "gid", "user_name"]


def test_repeated_user_lookup_returns_existing_id_without_a_gap(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    first = lookup_or_create_rodex_sessions_user(1009, 1010, "dna", database)
    repeated = lookup_or_create_rodex_sessions_user(1009, 1010, "dna", database)
    second = lookup_or_create_rodex_sessions_user(2009, 2010, "other", database)

    assert first == repeated == RodexSessionsUser(1, 1009, 1010, "dna")
    assert second.id == 2
    assert fetch_all(
        database,
        "SELECT name, seq FROM sqlite_sequence WHERE name = 'rodex_sessions_users'",
    ) == [("rodex_sessions_users", 2)]


@pytest.mark.parametrize(
    ("uid", "gid", "user_name"),
    [(1009, 1010, "DNA"), (1009, 1011, "dna"), (1010, 1010, "dna")],
)
def test_each_natural_key_field_participates_in_uniqueness(
    tmp_path: Path, uid: int, gid: int, user_name: str
) -> None:
    database = tmp_path / "rodex.sqlite3"
    lookup_or_create_rodex_sessions_user(1009, 1010, "dna", database)

    different = lookup_or_create_rodex_sessions_user(uid, gid, user_name, database)

    assert different.id == 2


def test_current_identity_comes_from_posix_uid_gid_and_password_database() -> None:
    identity = current_rodex_sessions_user_identity()

    assert identity == RodexSessionsUserIdentity(
        uid=os.getuid(),
        gid=os.getgid(),
        user_name=pwd.getpwuid(os.getuid()).pw_name,
    )
    assert identity.uid == os.getuid()
    assert identity.gid == os.getgid()


def test_windows_is_explicitly_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rodex_registry.lifecycle.sys.platform", "win32")

    with pytest.raises(RodexSessionError, match="Linux"):
        current_rodex_sessions_user_identity()


def test_new_session_log_references_the_normalized_user_lookup(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    identity = RodexSessionsUserIdentity(1009, 1010, "dna")

    first = create_a_rodex_session(
        database, codex_session_id=CODEX_SESSION_ID_1, user_identity=identity
    )
    second = create_a_rodex_session(
        database, codex_session_id=CODEX_SESSION_ID_2, user_identity=identity
    )

    first_log = lookup_rodex_session_log(first.rodex_sessions_id, database)
    second_log = lookup_rodex_session_log(second.rodex_sessions_id, database)
    assert first_log is not None and second_log is not None
    assert first_log.rodex_sessions_users_id == 1
    assert second_log.rodex_sessions_users_id == 1
    assert fetch_all(database, "SELECT COUNT(*) FROM rodex_sessions_users") == [(1,)]


def test_user_can_be_looked_up_by_internal_id(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = lookup_or_create_rodex_sessions_user(1009, 1010, "dna", database)

    assert lookup_rodex_sessions_user(created.id, database) == created
    assert lookup_rodex_sessions_user(999, database) is None


@pytest.mark.parametrize(
    "identity",
    [
        RodexSessionsUserIdentity(-1, 1, "user"),
        RodexSessionsUserIdentity(1, -1, "user"),
        RodexSessionsUserIdentity(1, 1, ""),
    ],
)
def test_invalid_posix_user_identity_is_rejected(
    tmp_path: Path, identity: RodexSessionsUserIdentity
) -> None:
    with pytest.raises(ValueError):
        create_a_rodex_session(
            tmp_path / "rodex.sqlite3",
            codex_session_id=CODEX_SESSION_ID_1,
            user_identity=identity,
        )


def test_user_session_and_log_insertions_are_one_transaction(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER force_log_failure BEFORE INSERT ON rodex_sessions_log "
            "BEGIN SELECT RAISE(ABORT, 'forced log failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced log failure"):
        create_a_rodex_session(
            database,
            codex_session_id=CODEX_SESSION_ID_1,
            user_identity=RodexSessionsUserIdentity(1009, 1010, "dna"),
        )

    assert fetch_all(database, "SELECT COUNT(*) FROM rodex_sessions_users") == [(0,)]
    assert fetch_all(database, "SELECT COUNT(*) FROM rodex_sessions") == [(0,)]
    assert fetch_all(database, "SELECT COUNT(*) FROM rodex_sessions_log") == [(0,)]
