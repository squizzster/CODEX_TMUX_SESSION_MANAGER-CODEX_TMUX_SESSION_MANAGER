from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rodex_registry import (
    RodexRuntimeId,
    RodexRuntimeIdCollisionError,
    RodexSessionError,
    RodexSessionsUserIdentity,
    create_a_rodex_session,
    generate_an_unregistered_rodex_runtime_id_candidate,
    initialise_rodex_database,
    lookup_codex_session_id_from_a_rodex_sessions_id,
    lookup_owned_rodex_sessions_id_from_a_codex_session_id,
    lookup_rodex_runtime_instance,
    lookup_rodex_session_log,
    lookup_rodex_sessions_id_from_a_codex_session_id,
    lookup_rodex_tmux_session,
    record_a_rodex_session_runtime_resume,
)

CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
REPLACEMENT_CODEX_SESSION_ID = uuid.UUID(int=CODEX_SESSION_ID.int + 1)


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
        ("rodex_session_id_signed_bigint", "BIGINT", 1, 0),
        ("codex_session_id_signed_bigint_1", "BIGINT", 1, 0),
        ("codex_session_id_signed_bigint_2", "BIGINT", 1, 0),
        ("cool_names_id", "INTEGER", 1, 0),
        ("user_defined_cool_names_id", "INTEGER", 0, 0),
    ]
    assert [
        row[2]
        for row in fetch_all(
            database,
            "PRAGMA index_info(rodex_sessions_codex_session_id_unique)",
        )
    ] == ["codex_session_id_signed_bigint_1", "codex_session_id_signed_bigint_2"]
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


def test_runtime_instance_table_identifies_one_exact_current_incarnation(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    columns = fetch_all(database, "PRAGMA table_info(rodex_runtime_instances)")
    indexes = fetch_all(database, "PRAGMA index_list(rodex_runtime_instances)")

    assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("runtime_id_signed_bigint", "BIGINT", 1, 0),
        ("started_at_utc", "TEXT", 1, 0),
    ]
    assert {(row[1], row[2]) for row in indexes} == {
        ("rodex_runtime_instances_runtime_id_unique", 1),
        ("rodex_runtime_instances_rodex_sessions_id_unique", 1),
    }
    assert [
        row[2]
        for row in fetch_all(
            database,
            "PRAGMA index_info(rodex_runtime_instances_runtime_id_unique)",
        )
    ] == ["runtime_id_signed_bigint"]


@pytest.mark.evolutionary_regression
def test_v4_runtime_uuid_schema_is_rejected_without_migration(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex-v4.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE rodex_runtime_instances")
        connection.execute(
            "CREATE TABLE rodex_runtime_instances ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "rodex_sessions_id INTEGER NOT NULL, "
            "runtime_identifier_signed_bigint_1 BIGINT NOT NULL, "
            "runtime_identifier_signed_bigint_2 BIGINT NOT NULL, "
            "started_at_utc TEXT NOT NULL, "
            "FOREIGN KEY (rodex_sessions_id) REFERENCES rodex_sessions (id))"
        )

    with pytest.raises(RodexSessionError, match="schema mismatch"):
        initialise_rodex_database(database)

    assert [
        row[1] for row in fetch_all(database, "PRAGMA table_info(rodex_runtime_instances)")
    ] == [
        "id",
        "rodex_sessions_id",
        "runtime_identifier_signed_bigint_1",
        "runtime_identifier_signed_bigint_2",
        "started_at_utc",
    ]


def test_create_and_resume_persist_the_exact_current_runtime_id(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    first_runtime = RodexRuntimeId.parse("0c01ee2ead7240e1")
    second_runtime = RodexRuntimeId.parse("e6877350da744e32")
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path="/tmp/rodex/tmux.sock",
        tmux_session_name="automatic-beluga",
        runtime_id=first_runtime,
    )

    initial = lookup_rodex_runtime_instance(session.rodex_sessions_id, database)
    assert initial is not None
    assert initial.runtime_id == first_runtime

    record_a_rodex_session_runtime_resume(
        session.rodex_sessions_id,
        "/tmp/rodex/tmux.sock",
        "automatic-beluga",
        database,
        runtime_id=second_runtime,
        accessed_at_utc=datetime(2026, 8, 15, 18, 30, tzinfo=UTC),
    )

    resumed = lookup_rodex_runtime_instance(session.rodex_sessions_id, database)
    assert resumed is not None
    assert resumed.id == initial.id
    assert resumed.runtime_id == second_runtime
    assert resumed.started_at_utc == "2026-08-15T18:30:00.000000Z"


def test_runtime_id_bigints_reject_non_integer_storage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path="/tmp/rodex/tmux.sock",
        tmux_session_name="automatic-beluga",
        runtime_id=RodexRuntimeId.parse("0c01ee2ead7240e1"),
    )

    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
    ):
        connection.execute(
            "UPDATE rodex_runtime_instances SET runtime_id_signed_bigint = 1.5 "
            "WHERE rodex_sessions_id = ?",
            (session.rodex_sessions_id,),
        )


def test_runtime_id_reader_does_not_coerce_corrupt_storage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path="/tmp/rodex/tmux.sock",
        tmux_session_name="automatic-beluga",
        runtime_id=RodexRuntimeId.parse("0c01ee2ead7240e1"),
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE rodex_runtime_instances "
            "SET runtime_id_signed_bigint = 1.5 "
            "WHERE rodex_sessions_id = ?",
            (session.rodex_sessions_id,),
        )

    with pytest.raises(ValueError, match="signed 64-bit"):
        lookup_rodex_runtime_instance(session.rodex_sessions_id, database)


def test_pending_runtime_id_candidate_succeeds_on_the_tenth_indexed_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path="/tmp/rodex/tmux.sock",
        tmux_session_name="automatic-beluga",
        runtime_id=RodexRuntimeId(100),
    )
    candidates = iter([RodexRuntimeId(100)] * 9 + [RodexRuntimeId(200)])
    monkeypatch.setattr(
        RodexRuntimeId,
        "generate",
        classmethod(lambda cls: next(candidates)),
    )

    candidate = generate_an_unregistered_rodex_runtime_id_candidate(database)

    assert candidate == RodexRuntimeId(200)


def test_pending_runtime_id_candidate_exhaustion_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path="/tmp/rodex/tmux.sock",
        tmux_session_name="automatic-beluga",
        runtime_id=RodexRuntimeId(100),
    )
    monkeypatch.setattr(
        RodexRuntimeId,
        "generate",
        classmethod(lambda cls: RodexRuntimeId(100)),
    )

    with pytest.raises(RodexRuntimeIdCollisionError, match="10 attempts"):
        generate_an_unregistered_rodex_runtime_id_candidate(database)


def test_one_transaction_matches_rodex_codex_and_tmux_without_mixing_ids(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        user_identity=RodexSessionsUserIdentity(1009, 1010, "dna"),
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path="/run/user/1009/rodex/tmux.sock",
        tmux_session_name="rodex-example",
    )

    assert session.rodex_session_id != CODEX_SESSION_ID
    tmux_link = lookup_rodex_tmux_session(session.rodex_sessions_id, database)
    assert session.codex_session_id == CODEX_SESSION_ID
    assert (
        lookup_codex_session_id_from_a_rodex_sessions_id(
            session.rodex_sessions_id, database
        )
        == CODEX_SESSION_ID
    )
    assert (
        lookup_rodex_sessions_id_from_a_codex_session_id(CODEX_SESSION_ID, database)
        == session.rodex_sessions_id
    )
    assert tmux_link is not None
    assert tmux_link.rodex_sessions_id == session.rodex_sessions_id
    assert tmux_link.tmux_session_name == "rodex-example"


def test_codex_identity_lookup_enforces_the_complete_posix_owner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    owner = RodexSessionsUserIdentity(1009, 1010, "dna")
    other_user = RodexSessionsUserIdentity(2001, 2002, "other")
    session = create_a_rodex_session(
        database,
        user_identity=owner,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path="/run/user/1009/rodex/tmux.sock",
        tmux_session_name="rodex-example",
    )

    assert (
        lookup_owned_rodex_sessions_id_from_a_codex_session_id(
            CODEX_SESSION_ID, database, user_identity=owner
        )
        == session.rodex_sessions_id
    )
    with pytest.raises(RodexSessionError, match="not owned"):
        lookup_owned_rodex_sessions_id_from_a_codex_session_id(
            CODEX_SESSION_ID, database, user_identity=other_user
        )
    assert (
        lookup_owned_rodex_sessions_id_from_a_codex_session_id(
            REPLACEMENT_CODEX_SESSION_ID, database, user_identity=owner
        )
        is None
    )


def test_runtime_link_fields_are_all_required_together(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        create_a_rodex_session(
            tmp_path / "rodex.sqlite3",
            codex_session_id=CODEX_SESSION_ID,
            tmux_session_name="rodex-example",
        )


def test_codex_session_id_unique_index_rejects_a_second_rodex_owner(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)

    expected = (
        f"Codex session already belongs to Rodex {session.cool_name}.\n"
        f"Resume with: rodex {session.cool_name}"
    )
    with pytest.raises(RodexSessionError) as raised:
        create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)

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
            codex_session_id=CODEX_SESSION_ID,
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
        codex_session_id=CODEX_SESSION_ID,
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


def test_runtime_recovery_atomically_relinks_the_codex_session_id_and_endpoint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path="/tmp/rodex/old.sock",
        tmux_session_name="automatic-beluga",
    )

    updated = record_a_rodex_session_runtime_resume(
        session.rodex_sessions_id,
        "/tmp/rodex/new.sock",
        "automatic-beluga",
        database,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
    )

    assert updated.tmux_server_socket_path == "/tmp/rodex/new.sock"
    assert (
        lookup_codex_session_id_from_a_rodex_sessions_id(
            session.rodex_sessions_id, database
        )
        == REPLACEMENT_CODEX_SESSION_ID
    )
    assert (
        lookup_rodex_sessions_id_from_a_codex_session_id(CODEX_SESSION_ID, database) is None
    )
    assert (
        lookup_rodex_sessions_id_from_a_codex_session_id(
            REPLACEMENT_CODEX_SESSION_ID, database
        )
        == session.rodex_sessions_id
    )


def test_runtime_resume_rolls_back_endpoint_when_access_log_update_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
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
            codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
        )

    assert (
        lookup_codex_session_id_from_a_rodex_sessions_id(
            session.rodex_sessions_id, database
        )
        == CODEX_SESSION_ID
    )
    tmux_link = lookup_rodex_tmux_session(session.rodex_sessions_id, database)
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == "/tmp/rodex/old.sock"
