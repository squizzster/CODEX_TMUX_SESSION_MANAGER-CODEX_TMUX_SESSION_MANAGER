from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from rodex_functions import (
    RodexSessionError,
    RodexSessionsUserIdentity,
    create_a_rodex_session,
    initialise_rodex_database,
    lookup_codex_uuid_from_a_rodex_session_id,
    lookup_rodex_session_id_from_a_codex_uuid,
    lookup_rodex_tmux_session,
)

CODEX_UUID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")


def fetch_all(database: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(query).fetchall()


def test_codex_identity_is_stored_directly_on_the_root_session(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    columns = fetch_all(database, "PRAGMA table_info(rodex_sessions)")

    assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
        ("id", "INTEGER", 0, 1),
        ("uuid_int_1", "BIGINT", 1, 0),
        ("uuid_int_2", "BIGINT", 1, 0),
        ("codex_session_uuid_int_1", "BIGINT", 1, 0),
        ("codex_session_uuid_int_2", "BIGINT", 1, 0),
        ("cool_names_id", "INTEGER", 1, 0),
        ("user_defined_cool_names_id", "INTEGER", 0, 0),
    ]
    assert [
        row[2]
        for row in fetch_all(
            database,
            "PRAGMA index_info(rodex_sessions_codex_session_uuid_ints_unique)",
        )
    ] == ["codex_session_uuid_int_1", "codex_session_uuid_int_2"]
    assert (
        fetch_all(
            database,
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'rodex_codex_sessions'",
        )
        == []
    )


def test_tmux_sessions_table_has_its_own_id_and_two_unique_keys(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    columns = fetch_all(database, "PRAGMA table_info(rodex_tmux_sessions)")

    assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("tmux_server_socket_path", "TEXT", 1, 0),
        ("tmux_session_name", "TEXT", 1, 0),
    ]
    indexes = fetch_all(database, "PRAGMA index_list(rodex_tmux_sessions)")
    assert {(row[1], row[2]) for row in indexes} == {
        ("rodex_tmux_sessions_rodex_sessions_id_unique", 1),
        ("rodex_tmux_sessions_endpoint_unique", 1),
    }


def test_one_transaction_matches_rodex_codex_and_tmux_without_mixing_ids(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        user_identity=RodexSessionsUserIdentity(1009, 1010, "dna"),
        codex_session_uuid=CODEX_UUID,
        tmux_server_socket_path="/run/user/1009/rodex/tmux.sock",
        tmux_session_name="rodex-example",
    )

    assert session.rodex_uuid != CODEX_UUID
    tmux_link = lookup_rodex_tmux_session(session.id, database)
    assert session.codex_session_uuid == CODEX_UUID
    assert lookup_codex_uuid_from_a_rodex_session_id(session.id, database) == CODEX_UUID
    assert lookup_rodex_session_id_from_a_codex_uuid(CODEX_UUID, database) == session.id
    assert tmux_link is not None
    assert tmux_link.rodex_sessions_id == session.id
    assert tmux_link.tmux_session_name == "rodex-example"


def test_runtime_link_fields_are_all_required_together(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        create_a_rodex_session(
            tmp_path / "rodex.sqlite3",
            codex_session_uuid=CODEX_UUID,
            tmux_session_name="rodex-example",
        )


def test_codex_uuid_unique_index_rejects_a_second_rodex_owner(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)

    with pytest.raises(RodexSessionError, match="already belongs"):
        create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)

    assert fetch_all(database, "SELECT COUNT(*) FROM rodex_sessions") == [(1,)]
    assert fetch_all(database, "SELECT COUNT(*) FROM cool_names") == [(1,)]


def test_runtime_matchmaking_rows_rollback_with_the_session(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER force_tmux_failure BEFORE INSERT ON rodex_tmux_sessions "
            "BEGIN SELECT RAISE(ABORT, 'forced tmux failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced tmux failure"):
        create_a_rodex_session(
            database,
            user_identity=RodexSessionsUserIdentity(1009, 1010, "dna"),
            codex_session_uuid=CODEX_UUID,
            tmux_server_socket_path="/tmp/rodex/tmux.sock",
            tmux_session_name="rodex-example",
        )

    for table in (
        "rodex_sessions_users",
        "rodex_sessions",
        "rodex_sessions_log",
        "rodex_tmux_sessions",
        "cool_names",
    ):
        assert fetch_all(database, f"SELECT COUNT(*) FROM {table}") == [(0,)]
