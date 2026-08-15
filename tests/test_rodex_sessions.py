from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

import rodex_functions.sessions as session_module
from rodex_functions import (
    RodexSessionError,
    RodexSessionUUIDCollisionError,
    create_a_rodex_session,
    default_rodex_database_path,
    initialise_rodex_database,
    join_signed_bigints_into_a_rodex_uuid,
    lookup_id_from_a_rodex_uuid,
    lookup_rodex_uuid_from_an_id,
    split_a_rodex_uuid_into_signed_bigints,
)


def fetch_all(database: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(query).fetchall()


def test_initialise_creates_database_parent_directories(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "state" / "rodex.sqlite3"

    assert initialise_rodex_database(database) == database
    assert database.is_file()


def test_rodex_sessions_table_has_exactly_the_three_sealed_columns(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    columns = fetch_all(database, "PRAGMA table_info(rodex_sessions)")

    assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
        ("id", "INTEGER", 0, 1),
        ("uuid_int_1", "BIGINT", 1, 0),
        ("uuid_int_2", "BIGINT", 1, 0),
    ]


def test_id_uses_sqlite_autoincrement(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    table_sql = fetch_all(
        database,
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rodex_sessions'",
    )[0][0]

    assert isinstance(table_sql, str)
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in table_sql.upper()


def test_uuid_halves_have_a_named_unique_index(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    indexes = fetch_all(database, "PRAGMA index_list(rodex_sessions)")
    columns = fetch_all(database, "PRAGMA index_info(rodex_sessions_uuid_ints_unique)")

    assert [(row[1], row[2]) for row in indexes] == [("rodex_sessions_uuid_ints_unique", 1)]
    assert [row[2] for row in columns] == ["uuid_int_1", "uuid_int_2"]


def test_initialisation_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    initialise_rodex_database(database)
    initialise_rodex_database(database)

    assert fetch_all(database, "SELECT COUNT(*) FROM rodex_sessions") == [(0,)]


def test_initialisation_rejects_a_changed_sealed_table(tmp_path: Path) -> None:
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
            "id INTEGER PRIMARY KEY, uuid_int_1 BIGINT NOT NULL, "
            "uuid_int_2 BIGINT NOT NULL)"
        )

    with pytest.raises(RodexSessionError, match="AUTOINCREMENT"):
        initialise_rodex_database(database)


def test_initialisation_repairs_a_missing_unique_index(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE rodex_sessions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "uuid_int_1 BIGINT NOT NULL, uuid_int_2 BIGINT NOT NULL)"
        )

    initialise_rodex_database(database)

    assert fetch_all(database, "PRAGMA index_info(rodex_sessions_uuid_ints_unique)")


@pytest.mark.parametrize(
    "uuid_integer",
    [
        0,
        1,
        (1 << 63) - 1,
        1 << 63,
        (1 << 64) - 1,
        1 << 64,
        1 << 127,
        (1 << 128) - 1,
        0x0123456789ABCDEFFEDCBA9876543210,
    ],
)
def test_uuid_split_and_join_preserves_every_bit(uuid_integer: int) -> None:
    original = uuid.UUID(int=uuid_integer)

    stored = split_a_rodex_uuid_into_signed_bigints(original)

    assert join_signed_bigints_into_a_rodex_uuid(*stored) == original
    assert all(-(1 << 63) <= half < (1 << 63) for half in stored)


def test_split_accepts_the_hyphenated_string_form() -> None:
    value = "01234567-89ab-cdef-fedc-ba9876543210"

    assert split_a_rodex_uuid_into_signed_bigints(value) == (
        0x0123456789ABCDEF,
        -0x0123456789ABCDF0,
    )


@pytest.mark.parametrize("bad_half", [-(1 << 63) - 1, 1 << 63])
def test_join_rejects_values_outside_sqlite_bigint_range(bad_half: int) -> None:
    with pytest.raises(ValueError, match="signed 64-bit"):
        join_signed_bigints_into_a_rodex_uuid(bad_half, 0)


@pytest.mark.parametrize("bad_half", [True, 1.5, "1"])
def test_join_rejects_non_integer_halves(bad_half: object) -> None:
    with pytest.raises(TypeError, match="integers"):
        join_signed_bigints_into_a_rodex_uuid(bad_half, 0)  # type: ignore[arg-type]


def test_create_returns_the_auto_increment_id_and_secure_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    random_value = 0xFEDCBA98765432100123456789ABCDEF
    monkeypatch.setattr(session_module.secrets, "randbits", lambda bits: random_value)

    created = create_a_rodex_session(database)

    assert created.id == 1
    assert created.rodex_uuid.int == random_value
    assert created.uuid_int_1 == 0xFEDCBA9876543210
    assert created.uuid_int_2 == 0x0123456789ABCDEF


def test_create_stores_signed_bigints_without_losing_uuid_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(session_module.secrets, "randbits", lambda bits: (1 << 128) - 1)

    create_a_rodex_session(database)

    assert fetch_all(database, "SELECT uuid_int_1, uuid_int_2 FROM rodex_sessions") == [
        (-1, -1)
    ]


def test_create_allocates_monotonically_increasing_internal_ids(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    first = create_a_rodex_session(database)
    second = create_a_rodex_session(database)
    third = create_a_rodex_session(database)

    assert [first.id, second.id, third.id] == [1, 2, 3]
    assert len({first.rodex_uuid, second.rodex_uuid, third.rodex_uuid}) == 3


def test_database_unique_index_rejects_duplicate_uuid_halves(tmp_path: Path) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO rodex_sessions (uuid_int_1, uuid_int_2) VALUES (10, 20)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO rodex_sessions (uuid_int_1, uuid_int_2) VALUES (10, 20)"
            )


def test_create_retries_after_a_uuid_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    candidates = iter([100, 100, 200])
    monkeypatch.setattr(session_module.secrets, "randbits", lambda bits: next(candidates))

    first = create_a_rodex_session(database)
    second = create_a_rodex_session(database)

    assert first.rodex_uuid.int == 100
    assert second.rodex_uuid.int == 200
    assert second.id == 2


def test_create_reports_repeated_uuid_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(session_module.secrets, "randbits", lambda bits: 100)
    create_a_rodex_session(database)

    with pytest.raises(RodexSessionUUIDCollisionError, match="8 attempts"):
        create_a_rodex_session(database)


def test_lookup_id_finds_a_uuid_object(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database)

    assert lookup_id_from_a_rodex_uuid(created.rodex_uuid, database) == created.id


def test_lookup_id_finds_a_uuid_string(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database)

    assert lookup_id_from_a_rodex_uuid(str(created.rodex_uuid), database) == created.id


def test_lookup_id_returns_none_for_an_unknown_uuid(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    assert lookup_id_from_a_rodex_uuid(uuid.UUID(int=42), database) is None


def test_lookup_uuid_finds_an_internal_id(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database)

    assert lookup_rodex_uuid_from_an_id(created.id, database) == created.rodex_uuid


def test_lookup_uuid_returns_none_for_an_unknown_id(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"

    assert lookup_rodex_uuid_from_an_id(999, database) is None


@pytest.mark.parametrize("bad_id", [0, -1, True, 1.5, "1"])
def test_lookup_uuid_rejects_invalid_internal_ids(tmp_path: Path, bad_id: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        lookup_rodex_uuid_from_an_id(bad_id, tmp_path / "db.sqlite3")  # type: ignore[arg-type]


def test_default_database_path_uses_current_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RODEX_DATABASE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    assert default_rodex_database_path() == tmp_path / ".rodex" / "rodex.sqlite3"


def test_default_database_path_honours_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("RODEX_DATABASE_PATH", str(configured))

    assert default_rodex_database_path() == configured
