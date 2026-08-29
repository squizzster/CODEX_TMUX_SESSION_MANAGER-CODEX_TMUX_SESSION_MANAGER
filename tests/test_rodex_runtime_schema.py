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
    audit_rodex_database_integrity,
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
    split_codex_thread_id_into_signed_bigints,
    split_codex_turn_id_into_signed_bigints,
)
from rodex_sql import open_rodex_transaction

CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
REPLACEMENT_CODEX_SESSION_ID = uuid.UUID(int=CODEX_SESSION_ID.int + 1)


def fetch_all(database: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(query).fetchall()


def _insert_codex_thread_membership(
    connection: sqlite3.Connection,
    thread_id: uuid.UUID,
) -> int:
    identity_id = connection.execute(
        "INSERT INTO codex_threads "
        "(codex_thread_public_id_signed_bigint_1, "
        "codex_thread_public_id_signed_bigint_2) VALUES (?, ?) RETURNING id",
        split_codex_thread_id_into_signed_bigints(thread_id),
    ).fetchone()[0]
    return int(
        connection.execute(
            "INSERT INTO rodex_sessions_codex_threads "
            "(rodex_sessions_id, codex_threads_id, first_linked_at_utc) "
            "VALUES (1, ?, '2026-08-26T12:00:00Z') RETURNING id",
            (identity_id,),
        ).fetchone()[0]
    )


def _insert_codex_turn(
    connection: sqlite3.Connection,
    thread_membership_id: int,
    turn_id: uuid.UUID,
) -> int:
    return int(
        connection.execute(
            "INSERT INTO rodex_sessions_codex_turns "
            "(rodex_sessions_id, rodex_sessions_codex_threads_id, "
            "turn_public_id_signed_bigint_1, turn_public_id_signed_bigint_2, "
            "codex_turn_id_signed_bigint_1, codex_turn_id_signed_bigint_2) "
            "VALUES (1, ?, ?, ?, ?, ?) RETURNING id",
            (
                thread_membership_id,
                *split_codex_turn_id_into_signed_bigints(uuid.uuid4()),
                *split_codex_turn_id_into_signed_bigints(turn_id),
            ),
        ).fetchone()[0]
    )


def test_codex_identity_is_canonical_and_current_root_is_a_relationship(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    columns = fetch_all(database, "PRAGMA table_info(rodex_sessions)")

    assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
        ("id", "INTEGER", 0, 1),
        ("rodex_session_id_signed_bigint", "BIGINT", 1, 0),
        ("cool_names_id", "INTEGER", 1, 0),
        ("user_defined_cool_names_id", "INTEGER", 0, 0),
    ]
    assert [
        row[2]
        for row in fetch_all(
            database,
            "PRAGMA index_info(codex_threads_public_id_unique)",
        )
    ] == [
        "codex_thread_public_id_signed_bigint_1",
        "codex_thread_public_id_signed_bigint_2",
    ]
    assert fetch_all(
        database,
        "PRAGMA table_info(rodex_sessions_current_codex_threads)",
    )
    membership_columns = [
        row[1]
        for row in fetch_all(database, "PRAGMA table_info(rodex_sessions_codex_threads)")
    ]
    assert membership_columns == [
        "id",
        "rodex_sessions_id",
        "codex_threads_id",
        "first_linked_at_utc",
    ]
    assert "parent_rodex_sessions_codex_threads_id" not in membership_columns


def test_current_root_and_subagent_roles_are_exclusive_for_insert_and_update(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 10)
    sibling_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 11)
    with open_rodex_transaction(database) as connection:
        root_membership_id = int(
            connection.execute(
                "SELECT rodex_sessions_codex_threads_id "
                "FROM rodex_sessions_current_codex_threads"
            ).fetchone()[0]
        )
        child_membership_id = _insert_codex_thread_membership(connection, child_thread_id)
        sibling_membership_id = _insert_codex_thread_membership(
            connection, sibling_thread_id
        )
        spawning_turn_id = _insert_codex_turn(
            connection,
            root_membership_id,
            uuid.UUID("00000000-0000-7000-8000-000000000001"),
        )
        spawn_sql = (
            "INSERT INTO rodex_sessions_subagent_spawns "
            "(rodex_sessions_id, subagent_rodex_sessions_codex_threads_id, "
            "parent_rodex_sessions_codex_threads_id, "
            "spawning_rodex_sessions_codex_turns_id, agent_path, "
            "history_inheritance_kind) VALUES (1, ?, ?, ?, '/root/test', 'clean')"
        )

        with pytest.raises(sqlite3.IntegrityError, match="current Codex thread"):
            connection.execute(
                spawn_sql,
                (root_membership_id, child_membership_id, spawning_turn_id),
            )
        connection.execute(
            spawn_sql,
            (child_membership_id, root_membership_id, spawning_turn_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="current Codex thread"):
            connection.execute(
                "UPDATE rodex_sessions_current_codex_threads "
                "SET rodex_sessions_codex_threads_id = ?",
                (child_membership_id,),
            )

        connection.execute("DELETE FROM rodex_sessions_current_codex_threads")
        with pytest.raises(sqlite3.IntegrityError, match="current Codex thread"):
            connection.execute(
                "INSERT INTO rodex_sessions_current_codex_threads "
                "(rodex_sessions_id, rodex_sessions_codex_threads_id) VALUES (1, ?)",
                (child_membership_id,),
            )
        connection.execute(
            "INSERT INTO rodex_sessions_current_codex_threads "
            "(rodex_sessions_id, rodex_sessions_codex_threads_id) VALUES (1, ?)",
            (root_membership_id,),
        )
        connection.execute(
            spawn_sql,
            (sibling_membership_id, root_membership_id, spawning_turn_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="current Codex thread"):
            connection.execute(
                "UPDATE rodex_sessions_subagent_spawns "
                "SET subagent_rodex_sessions_codex_threads_id = ? "
                "WHERE subagent_rodex_sessions_codex_threads_id = ?",
                (root_membership_id, sibling_membership_id),
            )

        assert connection.execute(
            "SELECT rodex_sessions_codex_threads_id "
            "FROM rodex_sessions_current_codex_threads"
        ).fetchone() == (root_membership_id,)
        assert set(
            connection.execute(
                "SELECT subagent_rodex_sessions_codex_threads_id "
                "FROM rodex_sessions_subagent_spawns"
            ).fetchall()
        ) == {(child_membership_id,), (sibling_membership_id,)}


def test_integrity_audit_rejects_a_same_named_but_weakened_root_guard(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER rodex_current_codex_thread_reject_spawn_insert")
        connection.execute(
            "CREATE TRIGGER rodex_current_codex_thread_reject_spawn_insert "
            "BEFORE INSERT ON rodex_sessions_current_codex_threads "
            "BEGIN SELECT 1; END"
        )
    connection.close()

    assert initialise_rodex_database(database) == database
    with pytest.raises(RodexSessionError, match="definition mismatch"):
        audit_rodex_database_integrity(database)


def test_exact_foreign_key_verifier_preserves_composite_grouping() -> None:
    from rodex_registry import schema as schema_module

    with sqlite3.connect(":memory:") as connection:
        connection.execute(
            "CREATE TABLE parent (a INTEGER, b INTEGER, c INTEGER, UNIQUE (a, c))"
        )
        connection.execute(
            "CREATE TABLE malformed (x INTEGER, y INTEGER, z INTEGER, "
            "FOREIGN KEY (x, z) REFERENCES parent (a, c), "
            "FOREIGN KEY (y) REFERENCES parent (b))"
        )
        with pytest.raises(RodexSessionError, match="foreign keys mismatch"):
            schema_module._verify_exact_foreign_keys(
                connection,
                "malformed",
                ((("parent", "x", "a"), ("parent", "y", "b"), ("parent", "z", "c")),),
            )


def test_resume_cannot_promote_a_subagent_and_rolls_back_runtime_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path="/tmp/rodex/old.sock",
        tmux_session_name="automatic-beluga",
    )
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 20)
    with open_rodex_transaction(database) as connection:
        root_membership_id = int(
            connection.execute(
                "SELECT rodex_sessions_codex_threads_id "
                "FROM rodex_sessions_current_codex_threads"
            ).fetchone()[0]
        )
        child_membership_id = _insert_codex_thread_membership(connection, child_thread_id)
        spawning_turn_id = _insert_codex_turn(
            connection,
            root_membership_id,
            uuid.UUID("00000000-0000-7000-8000-000000000002"),
        )
        connection.execute(
            "INSERT INTO rodex_sessions_subagent_spawns "
            "(rodex_sessions_id, subagent_rodex_sessions_codex_threads_id, "
            "parent_rodex_sessions_codex_threads_id, "
            "spawning_rodex_sessions_codex_turns_id, agent_path, "
            "history_inheritance_kind) VALUES (1, ?, ?, ?, '/root/test', 'clean')",
            (child_membership_id, root_membership_id, spawning_turn_id),
        )

    with pytest.raises(RodexSessionError, match="cannot become the current root"):
        record_a_rodex_session_runtime_resume(
            session.rodex_sessions_id,
            "/tmp/rodex/new.sock",
            "automatic-beluga",
            database,
            codex_session_id=child_thread_id,
            runtime_id=RodexRuntimeId.generate(),
        )

    tmux_link = lookup_rodex_tmux_session(session.rodex_sessions_id, database)
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == "/tmp/rodex/old.sock"
    assert (
        lookup_codex_session_id_from_a_rodex_sessions_id(
            session.rodex_sessions_id, database
        )
        == CODEX_SESSION_ID
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
    connection.close()

    with pytest.raises(RodexSessionError, match="definition mismatch"):
        audit_rodex_database_integrity(database)

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
        accessed_at_utc=datetime(2030, 8, 15, 18, 30, tzinfo=UTC),
    )

    resumed = lookup_rodex_runtime_instance(session.rodex_sessions_id, database)
    assert resumed is not None
    assert resumed.id == initial.id
    assert resumed.runtime_id == second_runtime
    assert resumed.started_at_utc == "2030-08-15T18:30:00.000000Z"


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
        runtime_id=RodexRuntimeId.generate(),
        accessed_at_utc=datetime(2030, 8, 15, 18, 30, tzinfo=UTC),
    )

    assert updated.id == 1
    assert updated.tmux_server_socket_path == "/tmp/rodex/new.sock"
    log = lookup_rodex_session_log(session.rodex_sessions_id, database)
    assert log is not None
    assert log.last_accessed_at_utc == "2030-08-15T18:30:00.000000Z"


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
        runtime_id=RodexRuntimeId.generate(),
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
            runtime_id=RodexRuntimeId.generate(),
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
