from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from pathlib import Path

import pytest

import rodex_registry.identity as identity_module
from rodex_registry import (
    RodexRegistryId,
    RodexSessionError,
    RodexSessionId,
    RodexSessionIdCollisionError,
    create_a_rodex_session,
    default_rodex_database_path,
    generate_an_unregistered_rodex_session_id_candidate,
    initialise_rodex_database,
    join_signed_bigints_into_a_codex_session_id,
    lookup_codex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_registry_id,
    lookup_rodex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_sessions_id_from_a_rodex_session_id,
    split_codex_session_id_into_signed_bigints,
)
from rodex_sql import RodexSQLError


def fetch_all(database: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(query).fetchall()


def seed_current_schema_generation(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE rodex_schema_generations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "schema_generation INTEGER NOT NULL CHECK (schema_generation >= 1), "
        "CHECK (id = 1))"
    )
    connection.execute(
        "INSERT INTO rodex_schema_generations (schema_generation) VALUES (14)"
    )


def codex_session_id(sequence: int) -> uuid.UUID:
    return uuid.UUID(int=(1 << 120) + sequence)


def test_initialise_creates_database_parent_directories(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "state" / "rodex.sqlite3"

    assert initialise_rodex_database(database) == database
    assert database.is_file()
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_each_database_has_one_stable_distinct_64_bit_registry_id(tmp_path: Path) -> None:
    first = initialise_rodex_database(tmp_path / "first.sqlite3")
    second = initialise_rodex_database(tmp_path / "second.sqlite3")

    first_registry_id = lookup_rodex_registry_id(first)

    assert isinstance(first_registry_id, RodexRegistryId)
    assert len(str(first_registry_id)) == 16
    assert lookup_rodex_registry_id(first) == first_registry_id
    assert lookup_rodex_registry_id(second) != first_registry_id


@pytest.mark.evolutionary_regression
def test_registry_identity_read_does_not_bootstrap_an_absent_database(
    tmp_path: Path,
) -> None:
    """Current evidence: registry creation belongs only to explicit write paths."""
    database = tmp_path / "absent" / "rodex.sqlite3"

    with pytest.raises(RodexSQLError, match="database does not exist"):
        lookup_rodex_registry_id(database)

    assert not database.parent.exists()
    assert not database.exists()


def test_registry_id_has_one_bigint_column_and_named_unique_index(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    columns = fetch_all(database, "PRAGMA table_info(rodex_registries)")
    indexes = fetch_all(database, "PRAGMA index_list(rodex_registries)")
    index_columns = fetch_all(
        database, "PRAGMA index_info(rodex_registries_registry_id_unique)"
    )

    assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
        ("id", "INTEGER", 0, 1),
        ("rodex_registry_id_signed_bigint", "BIGINT", 1, 0),
    ]
    assert {(row[1], row[2]) for row in indexes} == {
        ("rodex_registries_registry_id_unique", 1)
    }
    assert [row[2] for row in index_columns] == ["rodex_registry_id_signed_bigint"]


def test_initialise_repairs_an_existing_database_to_private_permissions(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    database.touch(mode=0o644)

    initialise_rodex_database(database)

    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_initialise_rejects_a_database_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch()
    database = tmp_path / "rodex.sqlite3"
    database.symlink_to(target)

    with pytest.raises(RodexSQLError, match="securely open database"):
        initialise_rodex_database(database)


def test_initialise_rejects_a_nonregular_database_path(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    os.mkfifo(database)

    with pytest.raises(RodexSQLError, match="not a regular file"):
        initialise_rodex_database(database)


def test_session_and_codex_identities_have_separate_canonical_rows(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    columns = fetch_all(database, "PRAGMA table_info(rodex_sessions)")

    assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
        ("id", "INTEGER", 0, 1),
        ("rodex_session_id_signed_bigint", "BIGINT", 1, 0),
        ("cool_names_id", "INTEGER", 1, 0),
        ("user_defined_cool_names_id", "INTEGER", 0, 0),
    ]
    foreign_keys = fetch_all(database, "PRAGMA foreign_key_list(rodex_sessions)")
    assert {(row[2], row[3], row[4]) for row in foreign_keys} == {
        ("cool_names", "cool_names_id", "id"),
        ("cool_names", "user_defined_cool_names_id", "id"),
    }
    assert columns[-1][4] == "NULL"
    assert [
        (row[1], row[2], row[3], row[5])
        for row in fetch_all(database, "PRAGMA table_info(codex_threads)")
    ] == [
        ("id", "INTEGER", 0, 1),
        ("codex_thread_public_id_signed_bigint_1", "BIGINT", 1, 0),
        ("codex_thread_public_id_signed_bigint_2", "BIGINT", 1, 0),
    ]


def test_identity_bigint_columns_reject_non_integer_storage(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=codex_session_id(1))
    identity_columns = (
        ("rodex_registries", "rodex_registry_id_signed_bigint"),
        ("rodex_sessions", "rodex_session_id_signed_bigint"),
        (
            "codex_threads",
            "codex_thread_public_id_signed_bigint_1",
        ),
        (
            "codex_threads",
            "codex_thread_public_id_signed_bigint_2",
        ),
    )

    with sqlite3.connect(database) as connection:
        for table_name, column_name in identity_columns:
            expected = (
                "thread identity is immutable"
                if table_name == "codex_threads"
                else "CHECK constraint failed"
            )
            with pytest.raises(sqlite3.IntegrityError, match=expected):
                connection.execute(f"UPDATE {table_name} SET {column_name} = 1.5")


def test_canonical_trace_vocabulary_rows_are_append_only(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    with sqlite3.connect(database) as connection:
        dimensions = (
            ("model_names", "name_of_the_model", "gpt-test", "model name"),
            (
                "reasoning_effort_names",
                "name_of_the_reasoning_effort",
                "xhigh-test",
                "reasoning-effort name",
            ),
            ("tool_names", "tool_name", "test_tool", "tool name"),
        )
        for table_name, column_name, value, diagnostic in dimensions:
            row_id = connection.execute(
                f"INSERT INTO {table_name} ({column_name}) VALUES (?) RETURNING id",
                (value,),
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError, match=diagnostic):
                connection.execute(
                    f"UPDATE {table_name} SET {column_name} = ? WHERE id = ?",
                    (f"changed-{value}", row_id),
                )
            with pytest.raises(sqlite3.IntegrityError, match=diagnostic):
                connection.execute(f"DELETE FROM {table_name} WHERE id = ?", (row_id,))


def test_codex_thread_membership_is_append_only(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=codex_session_id(1))

    with sqlite3.connect(database) as connection:
        membership_id = connection.execute(
            "SELECT id FROM rodex_sessions_codex_threads"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="membership is immutable"):
            connection.execute(
                "UPDATE rodex_sessions_codex_threads "
                "SET first_linked_at_utc = '2026-08-26T00:00:00Z' WHERE id = ?",
                (membership_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="membership is immutable"):
            connection.execute(
                "DELETE FROM rodex_sessions_codex_threads WHERE id = ?",
                (membership_id,),
            )


def test_identity_readers_do_not_coerce_corrupt_storage(tmp_path: Path) -> None:
    cases = (
        (
            "registry",
            "rodex_registries",
            "rodex_registry_id_signed_bigint",
            lookup_rodex_registry_id,
        ),
        (
            "session",
            "rodex_sessions",
            "rodex_session_id_signed_bigint",
            lambda path: lookup_rodex_session_id_from_a_rodex_sessions_id(1, path),
        ),
        (
            "codex",
            "codex_threads",
            "codex_thread_public_id_signed_bigint_1",
            lambda path: lookup_codex_session_id_from_a_rodex_sessions_id(1, path),
        ),
    )
    for case_name, table_name, column_name, reader in cases:
        database = tmp_path / f"{case_name}.sqlite3"
        create_a_rodex_session(database, codex_session_id=codex_session_id(1))
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            if table_name == "codex_threads":
                connection.execute("DROP TRIGGER codex_threads_reject_update")
            connection.execute(f"UPDATE {table_name} SET {column_name} = 1.5")

        with pytest.raises(ValueError, match="signed 64-bit"):
            reader(database)


def test_id_uses_sqlite_autoincrement(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    table_sql = fetch_all(
        database,
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rodex_sessions'",
    )[0][0]

    assert isinstance(table_sql, str)
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in table_sql.upper()


def test_session_id_has_one_named_unique_index(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    indexes = fetch_all(database, "PRAGMA index_list(rodex_sessions)")
    columns = fetch_all(
        database,
        "PRAGMA index_info(rodex_sessions_session_id_unique)",
    )

    assert {(row[1], row[2]) for row in indexes} == {
        ("rodex_sessions_session_id_unique", 1),
        ("rodex_sessions_cool_names_id_unique", 1),
        ("rodex_sessions_user_defined_cool_names_id_unique", 1),
    }
    assert [row[2] for row in columns] == ["rodex_session_id_signed_bigint"]


def test_initialisation_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    initialise_rodex_database(database)
    initialise_rodex_database(database)

    assert fetch_all(database, "SELECT COUNT(*) FROM rodex_sessions") == [(0,)]


def test_initialisation_rejects_an_incompatible_root_table(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    with sqlite3.connect(database) as connection:
        seed_current_schema_generation(connection)
        connection.execute("CREATE TABLE rodex_sessions (id INTEGER PRIMARY KEY)")

    with pytest.raises(RodexSessionError, match="schema mismatch"):
        initialise_rodex_database(database)


def test_initialisation_rejects_an_id_without_autoincrement(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    with sqlite3.connect(database) as connection:
        seed_current_schema_generation(connection)
        connection.execute(
            "CREATE TABLE rodex_sessions ("
            "id INTEGER PRIMARY KEY, "
            "rodex_session_id_signed_bigint BIGINT NOT NULL, "
            "cool_names_id INTEGER NOT NULL, "
            "user_defined_cool_names_id INTEGER DEFAULT NULL, "
            "FOREIGN KEY (cool_names_id) REFERENCES cool_names (id), "
            "FOREIGN KEY (user_defined_cool_names_id) REFERENCES cool_names (id))"
        )

    with pytest.raises(RodexSessionError, match="AUTOINCREMENT"):
        initialise_rodex_database(database)


def test_initialisation_rejects_missing_identity_type_constraints(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    with sqlite3.connect(database) as connection:
        seed_current_schema_generation(connection)
        connection.execute(
            "CREATE TABLE rodex_sessions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "rodex_session_id_signed_bigint BIGINT NOT NULL, "
            "cool_names_id INTEGER NOT NULL, "
            "user_defined_cool_names_id INTEGER DEFAULT NULL, "
            "FOREIGN KEY (cool_names_id) REFERENCES cool_names (id), "
            "FOREIGN KEY (user_defined_cool_names_id) REFERENCES cool_names (id))"
        )

    with pytest.raises(RodexSessionError, match="identity constraints mismatch"):
        initialise_rodex_database(database)


def test_initialisation_repairs_a_missing_unique_index(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    with sqlite3.connect(database) as connection:
        seed_current_schema_generation(connection)
        connection.execute(
            "CREATE TABLE rodex_sessions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "rodex_session_id_signed_bigint BIGINT NOT NULL "
            "CHECK (typeof(rodex_session_id_signed_bigint) = 'integer'), "
            "cool_names_id INTEGER NOT NULL, "
            "user_defined_cool_names_id INTEGER DEFAULT NULL, "
            "FOREIGN KEY (cool_names_id) REFERENCES cool_names (id), "
            "FOREIGN KEY (user_defined_cool_names_id) REFERENCES cool_names (id))"
        )

    initialise_rodex_database(database)

    assert fetch_all(
        database,
        "PRAGMA index_info(rodex_sessions_session_id_unique)",
    )


def test_codex_session_id_storage_helpers_preserve_the_codex_identity() -> None:
    original = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")

    stored = split_codex_session_id_into_signed_bigints(original)

    assert join_signed_bigints_into_a_codex_session_id(*stored) == original


def test_create_returns_the_internal_id_and_canonical_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    random_value = 0xFEDCBA9876543210
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: random_value)

    created = create_a_rodex_session(database, codex_session_id=codex_session_id(1))

    assert created.rodex_sessions_id == 1
    assert created.rodex_session_id == RodexSessionId(random_value)
    assert str(created.rodex_session_id) == "fedcba9876543210"
    assert created.codex_session_id == codex_session_id(1)


def test_create_stores_all_session_id_bits_as_one_sqlite_integer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: (1 << 64) - 1)

    create_a_rodex_session(database, codex_session_id=codex_session_id(1))

    assert fetch_all(
        database,
        "SELECT rodex_session_id_signed_bigint, "
        "typeof(rodex_session_id_signed_bigint) FROM rodex_sessions",
    ) == [(-1, "integer")]


def test_create_allocates_monotonically_increasing_internal_ids(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    first = create_a_rodex_session(database, codex_session_id=codex_session_id(1))
    second = create_a_rodex_session(database, codex_session_id=codex_session_id(2))
    third = create_a_rodex_session(database, codex_session_id=codex_session_id(3))

    assert [
        first.rodex_sessions_id,
        second.rodex_sessions_id,
        third.rodex_sessions_id,
    ] == [1, 2, 3]
    assert (
        len(
            {
                first.rodex_session_id,
                second.rodex_session_id,
                third.rodex_session_id,
            }
        )
        == 3
    )


def test_database_unique_index_rejects_duplicate_session_id(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    first = create_a_rodex_session(database, codex_session_id=codex_session_id(1))
    second = create_a_rodex_session(database, codex_session_id=codex_session_id(2))
    stored_session_id = fetch_all(
        database,
        "SELECT rodex_session_id_signed_bigint FROM rodex_sessions WHERE id = 1",
    )[0][0]
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE rodex_sessions SET rodex_session_id_signed_bigint = ? WHERE id = ?",
            (stored_session_id, second.rodex_sessions_id),
        )
    assert first.rodex_sessions_id == 1


def test_generated_session_id_succeeds_on_the_tenth_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    candidates = iter([100] * 10 + [200])
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: next(candidates))

    first = create_a_rodex_session(database, codex_session_id=codex_session_id(1))
    second = create_a_rodex_session(database, codex_session_id=codex_session_id(2))

    assert first.rodex_session_id.value == 100
    assert second.rodex_session_id.value == 200
    assert second.rodex_sessions_id == 2


def test_create_reports_ten_repeated_session_id_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: 100)
    create_a_rodex_session(database, codex_session_id=codex_session_id(1))

    with pytest.raises(RodexSessionIdCollisionError, match="10 attempts"):
        create_a_rodex_session(database, codex_session_id=codex_session_id(2))


def test_unrelated_integrity_error_is_not_misreported_as_session_id_collision(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER reject_session BEFORE INSERT ON rodex_sessions "
            "BEGIN SELECT RAISE(FAIL, 'forced unrelated integrity failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="unrelated integrity failure"):
        create_a_rodex_session(
            database,
            codex_session_id=codex_session_id(1),
            rodex_session_id=RodexSessionId(200),
        )


def test_pending_session_id_candidate_succeeds_on_the_tenth_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_id=codex_session_id(1),
        rodex_session_id=RodexSessionId(100),
    )
    candidates = iter([100] * 9 + [200])
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: next(candidates))

    candidate = generate_an_unregistered_rodex_session_id_candidate(database)

    assert candidate.value == 200
    assert lookup_rodex_sessions_id_from_a_rodex_session_id(candidate, database) is None


def test_pending_session_id_candidate_exhaustion_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_id=codex_session_id(1),
        rodex_session_id=RodexSessionId(100),
    )
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: 100)

    with pytest.raises(RodexSessionIdCollisionError, match="10 attempts"):
        generate_an_unregistered_rodex_session_id_candidate(database)


def test_lookup_id_finds_a_session_id_object(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database, codex_session_id=codex_session_id(1))

    assert (
        lookup_rodex_sessions_id_from_a_rodex_session_id(created.rodex_session_id, database)
        == created.rodex_sessions_id
    )


def test_lookup_id_finds_a_canonical_session_id_string(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database, codex_session_id=codex_session_id(1))

    assert (
        lookup_rodex_sessions_id_from_a_rodex_session_id(
            str(created.rodex_session_id), database
        )
        == created.rodex_sessions_id
    )


def test_lookup_id_returns_none_for_an_unknown_session_id(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    assert (
        lookup_rodex_sessions_id_from_a_rodex_session_id("000000000000002a", database)
        is None
    )


def test_lookup_session_id_finds_an_internal_id(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database, codex_session_id=codex_session_id(1))

    assert (
        lookup_rodex_session_id_from_a_rodex_sessions_id(
            created.rodex_sessions_id, database
        )
        == created.rodex_session_id
    )


def test_codex_session_id_is_looked_up_directly_from_the_root_session(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database, codex_session_id=codex_session_id(1))

    assert lookup_codex_session_id_from_a_rodex_sessions_id(
        created.rodex_sessions_id, database
    ) == codex_session_id(1)


def test_lookup_session_id_returns_none_for_an_unknown_id(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    assert lookup_rodex_session_id_from_a_rodex_sessions_id(999, database) is None


@pytest.mark.parametrize("bad_id", [0, -1, True, 1.5, "1"])
def test_lookup_session_id_rejects_invalid_internal_ids(
    tmp_path: Path, bad_id: object
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        lookup_rodex_session_id_from_a_rodex_sessions_id(bad_id, tmp_path / "db.sqlite3")  # type: ignore[arg-type]


def test_default_database_path_uses_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RODEX_DATABASE_PATH", raising=False)
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    assert default_rodex_database_path() == state_home / "rodex" / "rodex-v14.sqlite3"


def test_default_database_path_uses_home_state_directory_without_xdg_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RODEX_DATABASE_PATH", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    assert default_rodex_database_path() == (
        home / ".local" / "state" / "rodex" / "rodex-v14.sqlite3"
    )


@pytest.mark.evolutionary_regression
def test_incompatible_schema_generation_leaves_v10_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Current evidence: incompatible ALPHA schemas use a new durable filename.

    Supersede this guard only with an explicit migration or database-reset decision.
    """
    monkeypatch.delenv("RODEX_DATABASE_PATH", raising=False)
    state_home = tmp_path / "state"
    registry_directory = state_home / "rodex"
    registry_directory.mkdir(mode=0o700, parents=True)
    registry_directory.chmod(0o700)
    legacy_database = registry_directory / "rodex-v10.sqlite3"
    legacy_contents = b"legacy-v10-database-sentinel"
    legacy_database.write_bytes(legacy_contents)
    legacy_database.chmod(0o600)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    current_database = initialise_rodex_database()

    assert current_database == registry_directory / "rodex-v14.sqlite3"
    assert current_database.is_file()
    assert legacy_database.read_bytes() == legacy_contents


def test_nonempty_unmarked_database_is_rejected_before_v14_tables_are_created(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_sentinel VALUES ('untouched')")

    with pytest.raises(RodexSessionError, match="no schema-generation marker"):
        initialise_rodex_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM legacy_sentinel").fetchall() == [
            ("untouched",)
        ]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'rodex_sessions'"
            ).fetchone()
            is None
        )


def test_wrong_explicit_schema_generation_is_rejected_without_repair(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE rodex_schema_generations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "schema_generation INTEGER NOT NULL CHECK (schema_generation >= 1), "
            "CHECK (id = 1))"
        )
        connection.execute(
            "INSERT INTO rodex_schema_generations (schema_generation) VALUES (10)"
        )

    with pytest.raises(RodexSessionError, match="schema generation does not match"):
        initialise_rodex_database(database)

    assert fetch_all(
        database, "SELECT schema_generation FROM rodex_schema_generations"
    ) == [(10,)]


def test_default_database_path_honours_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("RODEX_DATABASE_PATH", str(configured))

    assert default_rodex_database_path() == configured
