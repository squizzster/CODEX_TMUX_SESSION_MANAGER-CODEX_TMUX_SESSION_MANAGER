from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from pathlib import Path

import pytest

import rodex_registry.identity as identity_module
from rodex_registry import (
    RodexSessionError,
    RodexSessionIdentifier,
    RodexSessionIdentifierCollisionError,
    create_a_rodex_session,
    default_rodex_database_path,
    generate_an_unregistered_rodex_session_identifier_candidate,
    initialise_rodex_database,
    join_signed_bigints_into_a_codex_session_uuid,
    lookup_codex_uuid_from_a_rodex_session_id,
    lookup_id_from_a_rodex_session_identifier,
    lookup_rodex_registry_uuid,
    lookup_rodex_session_identifier_from_an_id,
    split_codex_session_uuid_into_signed_bigints,
)
from rodex_sql import RodexSQLError


def fetch_all(database: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(query).fetchall()


def codex_uuid(sequence: int) -> uuid.UUID:
    return uuid.UUID(int=(1 << 120) + sequence)


def test_initialise_creates_database_parent_directories(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "state" / "rodex.sqlite3"

    assert initialise_rodex_database(database) == database
    assert database.is_file()
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_each_database_has_one_stable_distinct_registry_identity(tmp_path: Path) -> None:
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"

    first_uuid = lookup_rodex_registry_uuid(first)

    assert lookup_rodex_registry_uuid(first) == first_uuid
    assert lookup_rodex_registry_uuid(second) != first_uuid


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


def test_rodex_sessions_table_has_the_complete_root_identity(tmp_path: Path) -> None:
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
    foreign_keys = fetch_all(database, "PRAGMA foreign_key_list(rodex_sessions)")
    assert {(row[2], row[3], row[4]) for row in foreign_keys} == {
        ("cool_names", "cool_names_id", "id"),
        ("cool_names", "user_defined_cool_names_id", "id"),
    }
    assert columns[-1][4] == "NULL"


def test_id_uses_sqlite_autoincrement(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    table_sql = fetch_all(
        database,
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rodex_sessions'",
    )[0][0]

    assert isinstance(table_sql, str)
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in table_sql.upper()


def test_session_identifier_has_one_named_unique_index(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    indexes = fetch_all(database, "PRAGMA index_list(rodex_sessions)")
    columns = fetch_all(
        database,
        "PRAGMA index_info(rodex_sessions_session_identifier_unique)",
    )

    assert {(row[1], row[2]) for row in indexes} == {
        ("rodex_sessions_session_identifier_unique", 1),
        ("rodex_sessions_codex_session_uuid_ints_unique", 1),
        ("rodex_sessions_cool_names_id_unique", 1),
        ("rodex_sessions_user_defined_cool_names_id_unique", 1),
    }
    assert [row[2] for row in columns] == ["rodex_session_identifier_signed_bigint"]


def test_initialisation_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    initialise_rodex_database(database)
    initialise_rodex_database(database)

    assert fetch_all(database, "SELECT COUNT(*) FROM rodex_sessions") == [(0,)]


def test_initialisation_rejects_an_incompatible_root_table(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE rodex_sessions (id INTEGER PRIMARY KEY)")

    with pytest.raises(RodexSessionError, match="schema mismatch"):
        initialise_rodex_database(database)


def test_initialisation_rejects_an_id_without_autoincrement(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE rodex_sessions ("
            "id INTEGER PRIMARY KEY, "
            "rodex_session_identifier_signed_bigint BIGINT NOT NULL, "
            "codex_session_uuid_int_1 BIGINT NOT NULL, "
            "codex_session_uuid_int_2 BIGINT NOT NULL, cool_names_id INTEGER NOT NULL, "
            "user_defined_cool_names_id INTEGER DEFAULT NULL, "
            "FOREIGN KEY (cool_names_id) REFERENCES cool_names (id), "
            "FOREIGN KEY (user_defined_cool_names_id) REFERENCES cool_names (id))"
        )

    with pytest.raises(RodexSessionError, match="AUTOINCREMENT"):
        initialise_rodex_database(database)


def test_initialisation_repairs_a_missing_unique_index(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE rodex_sessions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "rodex_session_identifier_signed_bigint BIGINT NOT NULL, "
            "codex_session_uuid_int_1 BIGINT NOT NULL, "
            "codex_session_uuid_int_2 BIGINT NOT NULL, cool_names_id INTEGER NOT NULL, "
            "user_defined_cool_names_id INTEGER DEFAULT NULL, "
            "FOREIGN KEY (cool_names_id) REFERENCES cool_names (id), "
            "FOREIGN KEY (user_defined_cool_names_id) REFERENCES cool_names (id))"
        )

    initialise_rodex_database(database)

    assert fetch_all(
        database,
        "PRAGMA index_info(rodex_sessions_session_identifier_unique)",
    )


def test_codex_uuid_storage_helpers_preserve_the_codex_identity() -> None:
    original = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")

    stored = split_codex_session_uuid_into_signed_bigints(original)

    assert join_signed_bigints_into_a_codex_session_uuid(*stored) == original


def test_create_returns_the_internal_id_and_canonical_session_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    random_value = 0xFEDCBA9876543210
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: random_value)

    created = create_a_rodex_session(database, codex_session_uuid=codex_uuid(1))

    assert created.rodex_sessions_id == 1
    assert created.rodex_session_identifier == RodexSessionIdentifier(random_value)
    assert str(created.rodex_session_identifier) == "fedcba9876543210"
    assert created.codex_session_uuid == codex_uuid(1)


def test_create_stores_all_identifier_bits_as_one_sqlite_integer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: (1 << 64) - 1)

    create_a_rodex_session(database, codex_session_uuid=codex_uuid(1))

    assert fetch_all(
        database,
        "SELECT rodex_session_identifier_signed_bigint, "
        "typeof(rodex_session_identifier_signed_bigint) FROM rodex_sessions",
    ) == [(-1, "integer")]


def test_create_allocates_monotonically_increasing_internal_ids(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    first = create_a_rodex_session(database, codex_session_uuid=codex_uuid(1))
    second = create_a_rodex_session(database, codex_session_uuid=codex_uuid(2))
    third = create_a_rodex_session(database, codex_session_uuid=codex_uuid(3))

    assert [
        first.rodex_sessions_id,
        second.rodex_sessions_id,
        third.rodex_sessions_id,
    ] == [1, 2, 3]
    assert (
        len(
            {
                first.rodex_session_identifier,
                second.rodex_session_identifier,
                third.rodex_session_identifier,
            }
        )
        == 3
    )


def test_database_unique_index_rejects_duplicate_session_identifier(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    first = create_a_rodex_session(database, codex_session_uuid=codex_uuid(1))
    second = create_a_rodex_session(database, codex_session_uuid=codex_uuid(2))
    stored_identifier = fetch_all(
        database,
        "SELECT rodex_session_identifier_signed_bigint FROM rodex_sessions WHERE id = 1",
    )[0][0]
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE rodex_sessions "
            "SET rodex_session_identifier_signed_bigint = ? WHERE id = ?",
            (stored_identifier, second.rodex_sessions_id),
        )
    assert first.rodex_sessions_id == 1


def test_generated_session_identifier_succeeds_on_the_tenth_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    candidates = iter([100] * 10 + [200])
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: next(candidates))

    first = create_a_rodex_session(database, codex_session_uuid=codex_uuid(1))
    second = create_a_rodex_session(database, codex_session_uuid=codex_uuid(2))

    assert first.rodex_session_identifier.value == 100
    assert second.rodex_session_identifier.value == 200
    assert second.rodex_sessions_id == 2


def test_create_reports_ten_repeated_identifier_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: 100)
    create_a_rodex_session(database, codex_session_uuid=codex_uuid(1))

    with pytest.raises(RodexSessionIdentifierCollisionError, match="10 attempts"):
        create_a_rodex_session(database, codex_session_uuid=codex_uuid(2))


def test_unrelated_integrity_error_is_not_misreported_as_identifier_collision(
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
            codex_session_uuid=codex_uuid(1),
            rodex_session_identifier=RodexSessionIdentifier(200),
        )


def test_pending_identifier_candidate_succeeds_on_the_tenth_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_uuid=codex_uuid(1),
        rodex_session_identifier=RodexSessionIdentifier(100),
    )
    candidates = iter([100] * 9 + [200])
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: next(candidates))

    candidate = generate_an_unregistered_rodex_session_identifier_candidate(database)

    assert candidate.value == 200
    assert lookup_id_from_a_rodex_session_identifier(candidate, database) is None


def test_pending_identifier_candidate_exhaustion_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_uuid=codex_uuid(1),
        rodex_session_identifier=RodexSessionIdentifier(100),
    )
    monkeypatch.setattr(identity_module.secrets, "randbits", lambda bits: 100)

    with pytest.raises(RodexSessionIdentifierCollisionError, match="10 attempts"):
        generate_an_unregistered_rodex_session_identifier_candidate(database)


def test_lookup_id_finds_an_identifier_object(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database, codex_session_uuid=codex_uuid(1))

    assert (
        lookup_id_from_a_rodex_session_identifier(
            created.rodex_session_identifier, database
        )
        == created.rodex_sessions_id
    )


def test_lookup_id_finds_a_canonical_identifier_string(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database, codex_session_uuid=codex_uuid(1))

    assert (
        lookup_id_from_a_rodex_session_identifier(
            str(created.rodex_session_identifier), database
        )
        == created.rodex_sessions_id
    )


def test_lookup_id_returns_none_for_an_unknown_identifier(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    assert lookup_id_from_a_rodex_session_identifier("000000000000002a", database) is None


def test_lookup_identifier_finds_an_internal_id(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database, codex_session_uuid=codex_uuid(1))

    assert (
        lookup_rodex_session_identifier_from_an_id(created.rodex_sessions_id, database)
        == created.rodex_session_identifier
    )


def test_codex_uuid_is_looked_up_directly_from_the_root_session(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database, codex_session_uuid=codex_uuid(1))

    assert lookup_codex_uuid_from_a_rodex_session_id(
        created.rodex_sessions_id, database
    ) == codex_uuid(1)


def test_lookup_identifier_returns_none_for_an_unknown_id(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    assert lookup_rodex_session_identifier_from_an_id(999, database) is None


@pytest.mark.parametrize("bad_id", [0, -1, True, 1.5, "1"])
def test_lookup_identifier_rejects_invalid_internal_ids(
    tmp_path: Path, bad_id: object
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        lookup_rodex_session_identifier_from_an_id(bad_id, tmp_path / "db.sqlite3")  # type: ignore[arg-type]


def test_default_database_path_uses_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RODEX_DATABASE_PATH", raising=False)
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    assert default_rodex_database_path() == state_home / "rodex" / "rodex-v2.sqlite3"


def test_v2_default_initialization_leaves_the_pre_alpha_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RODEX_DATABASE_PATH", raising=False)
    state_home = tmp_path / "state"
    registry_directory = state_home / "rodex"
    registry_directory.mkdir(mode=0o700, parents=True)
    old_database = registry_directory / "rodex.sqlite3"
    old_contents = b"pre-alpha registry must remain isolated"
    old_database.write_bytes(old_contents)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    current_database = initialise_rodex_database()

    assert current_database == registry_directory / "rodex-v2.sqlite3"
    assert current_database.is_file()
    assert old_database.read_bytes() == old_contents


def test_default_database_path_uses_home_state_directory_without_xdg_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RODEX_DATABASE_PATH", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    assert default_rodex_database_path() == (
        home / ".local" / "state" / "rodex" / "rodex-v2.sqlite3"
    )


def test_default_database_path_honours_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("RODEX_DATABASE_PATH", str(configured))

    assert default_rodex_database_path() == configured
