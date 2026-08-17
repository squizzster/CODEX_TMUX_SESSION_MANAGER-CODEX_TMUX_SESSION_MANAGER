from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rodex_registry import (
    RodexSessionError,
    RodexSessionsUserIdentity,
    create_a_rodex_session,
    initialise_rodex_database,
    lookup_codex_uuid_from_a_rodex_session_id,
    lookup_rodex_session_id_from_a_codex_uuid,
    lookup_rodex_session_log,
    lookup_rodex_tmux_session,
    record_a_rodex_session_runtime_resume,
)

CODEX_UUID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
REPLACEMENT_CODEX_UUID = uuid.UUID(int=CODEX_UUID.int + 1)


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
        ("rodex_session_identifier_signed_bigint", "BIGINT", 1, 0),
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

    assert session.rodex_session_identifier != CODEX_UUID
    tmux_link = lookup_rodex_tmux_session(session.rodex_sessions_id, database)
    assert session.codex_session_uuid == CODEX_UUID
    assert (
        lookup_codex_uuid_from_a_rodex_session_id(session.rodex_sessions_id, database)
        == CODEX_UUID
    )
    assert (
        lookup_rodex_session_id_from_a_codex_uuid(CODEX_UUID, database)
        == session.rodex_sessions_id
    )
    assert tmux_link is not None
    assert tmux_link.rodex_sessions_id == session.rodex_sessions_id
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
    session = create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)

    expected = (
        f"Codex session already belongs to Rodex {session.cool_name}.\n"
        f"Resume with: rodex {session.cool_name}"
    )
    with pytest.raises(RodexSessionError) as raised:
        create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)

    assert str(raised.value) == expected
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


def test_runtime_resume_replaces_endpoint_and_access_time_in_one_transaction(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        tmux_server_socket_path="/tmp/rodex/old.sock",
        tmux_session_name="automatic-beluga",
    )

    updated = record_a_rodex_session_runtime_resume(
        session.rodex_sessions_id,
        "/tmp/rodex/new.sock",
        "automatic-beluga",
        database,
        accessed_at_utc=datetime(2026, 8, 15, 18, 30, tzinfo=UTC),
    )

    assert updated.id == 1
    assert updated.tmux_server_socket_path == "/tmp/rodex/new.sock"
    log = lookup_rodex_session_log(session.rodex_sessions_id, database)
    assert log is not None
    assert log.last_accessed_at_utc == "2026-08-15T18:30:00.000000Z"


def test_runtime_recovery_atomically_relinks_the_codex_uuid_and_endpoint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        tmux_server_socket_path="/tmp/rodex/old.sock",
        tmux_session_name="automatic-beluga",
    )

    updated = record_a_rodex_session_runtime_resume(
        session.rodex_sessions_id,
        "/tmp/rodex/new.sock",
        "automatic-beluga",
        database,
        codex_session_uuid=REPLACEMENT_CODEX_UUID,
    )

    assert updated.tmux_server_socket_path == "/tmp/rodex/new.sock"
    assert (
        lookup_codex_uuid_from_a_rodex_session_id(session.rodex_sessions_id, database)
        == REPLACEMENT_CODEX_UUID
    )
    assert lookup_rodex_session_id_from_a_codex_uuid(CODEX_UUID, database) is None
    assert (
        lookup_rodex_session_id_from_a_codex_uuid(REPLACEMENT_CODEX_UUID, database)
        == session.rodex_sessions_id
    )


def test_runtime_resume_rolls_back_endpoint_when_access_log_update_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        tmux_server_socket_path="/tmp/rodex/old.sock",
        tmux_session_name="automatic-beluga",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER force_log_update_failure BEFORE UPDATE "
            "ON rodex_sessions_log BEGIN "
            "SELECT RAISE(ABORT, 'forced log update failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced log update failure"):
        record_a_rodex_session_runtime_resume(
            session.rodex_sessions_id,
            "/tmp/rodex/new.sock",
            "automatic-beluga",
            database,
            codex_session_uuid=REPLACEMENT_CODEX_UUID,
        )

    assert (
        lookup_codex_uuid_from_a_rodex_session_id(session.rodex_sessions_id, database)
        == CODEX_UUID
    )
    tmux_link = lookup_rodex_tmux_session(session.rodex_sessions_id, database)
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == "/tmp/rodex/old.sock"
