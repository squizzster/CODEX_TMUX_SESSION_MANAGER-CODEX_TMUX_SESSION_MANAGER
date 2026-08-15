from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from rodex_functions import (
    RodexSessionsUserIdentity,
    create_a_rodex_session,
    initialise_rodex_database,
    lookup_rodex_codex_session,
    lookup_rodex_session_id_from_a_codex_uuid,
    lookup_rodex_tmux_session,
)

CODEX_UUID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")


def fetch_all(database: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(query).fetchall()


def test_codex_matchmaking_table_has_explicit_distinct_identity_fields(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    columns = fetch_all(database, "PRAGMA table_info(rodex_codex_sessions)")

    assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("codex_session_uuid_int_1", "BIGINT", 1, 0),
        ("codex_session_uuid_int_2", "BIGINT", 1, 0),
    ]
    assert fetch_all(database, "PRAGMA foreign_key_list(rodex_codex_sessions)")[0][2:5] == (
        "rodex_sessions",
        "rodex_sessions_id",
        "id",
    )
    assert [
        row[2]
        for row in fetch_all(
            database, "PRAGMA index_info(rodex_codex_sessions_uuid_ints_unique)"
        )
    ] == ["codex_session_uuid_int_1", "codex_session_uuid_int_2"]


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
    codex_link = lookup_rodex_codex_session(session.id, database)
    tmux_link = lookup_rodex_tmux_session(session.id, database)
    assert codex_link is not None
    assert codex_link.codex_session_uuid == CODEX_UUID
    assert lookup_rodex_session_id_from_a_codex_uuid(CODEX_UUID, database) == session.id
    assert tmux_link is not None
    assert tmux_link.rodex_sessions_id == session.id
    assert tmux_link.tmux_session_name == "rodex-example"


def test_runtime_link_fields_are_all_required_together(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        create_a_rodex_session(tmp_path / "rodex.sqlite3", codex_session_uuid=CODEX_UUID)


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
        "rodex_codex_sessions",
        "rodex_tmux_sessions",
    ):
        assert fetch_all(database, f"SELECT COUNT(*) FROM {table}") == [(0,)]
