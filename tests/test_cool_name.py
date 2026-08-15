from __future__ import annotations

import hashlib
import sqlite3
import uuid
from pathlib import Path

import pytest

from cool_name import (
    CoolNameGenerationError,
    get_unique_id_from_cool_name,
    get_unique_new_cool_name,
    initialise_cool_names_database,
)
from rodex_functions import create_a_rodex_session, initialise_rodex_database


def fetch_all(database: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(query).fetchall()


def signed_64(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


def test_cool_names_schema_uses_two_indexed_md5_integers_and_unindexed_text(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")

    columns = fetch_all(database, "PRAGMA table_info(cool_names)")
    indexes = fetch_all(database, "PRAGMA index_list(cool_names)")
    index_columns = fetch_all(database, "PRAGMA index_info(cool_names_md5_ints_unique)")

    assert [(row[1], row[2], row[3], row[5]) for row in columns] == [
        ("id", "INTEGER", 0, 1),
        ("cool_name_md5_int_1", "BIGINT", 1, 0),
        ("cool_name_md5_int_2", "BIGINT", 1, 0),
        ("cool_name", "TEXT", 1, 0),
    ]
    assert [(row[1], row[2]) for row in indexes] == [("cool_names_md5_ints_unique", 1)]
    assert [row[2] for row in index_columns] == [
        "cool_name_md5_int_1",
        "cool_name_md5_int_2",
    ]
    assert "cool_name" not in {row[2] for row in index_columns}


def test_new_cool_name_stores_the_full_md5_as_two_signed_integers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    allocated = get_unique_new_cool_name(
        database, name_generator=lambda _word_count: "alpha-beta"
    )
    digest = hashlib.md5(b"alpha-beta", usedforsecurity=False).digest()
    expected = (
        signed_64(int.from_bytes(digest[:8], byteorder="big")),
        signed_64(int.from_bytes(digest[8:], byteorder="big")),
    )

    stored = fetch_all(
        database,
        "SELECT cool_name_md5_int_1, cool_name_md5_int_2, cool_name FROM cool_names",
    )

    assert allocated == "alpha-beta"
    assert stored == [(expected[0], expected[1], "alpha-beta")]
    assert get_unique_id_from_cool_name("alpha-beta", database) == 1


def test_absent_cool_name_returns_none(tmp_path: Path) -> None:
    database = initialise_cool_names_database(tmp_path / "rodex.sqlite3")

    assert get_unique_id_from_cool_name("not-allocated", database) is None


def test_five_blocked_two_word_names_fall_back_to_three_words_without_id_gaps(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    get_unique_new_cool_name(database, name_generator=lambda _word_count: "blocked-name")
    requested_word_counts: list[int] = []

    def generate_name(word_count: int) -> str:
        requested_word_counts.append(word_count)
        return "blocked-name" if word_count == 2 else "fresh-three-name"

    allocated = get_unique_new_cool_name(database, name_generator=generate_name)

    assert allocated == "fresh-three-name"
    assert requested_word_counts == [2, 2, 2, 2, 2, 3]
    assert fetch_all(database, "SELECT id, cool_name FROM cool_names ORDER BY id") == [
        (1, "blocked-name"),
        (2, "fresh-three-name"),
    ]


def test_ten_blocked_names_fail_after_exactly_five_attempts_at_each_size(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    get_unique_new_cool_name(database, name_generator=lambda _word_count: "blocked-name")
    requested_word_counts: list[int] = []

    def generate_blocked_name(word_count: int) -> str:
        requested_word_counts.append(word_count)
        return "blocked-name"

    with pytest.raises(CoolNameGenerationError, match="five two-word"):
        get_unique_new_cool_name(database, name_generator=generate_blocked_name)

    assert requested_word_counts == [2, 2, 2, 2, 2, 3, 3, 3, 3, 3]
    assert fetch_all(database, "SELECT id, cool_name FROM cool_names") == [
        (1, "blocked-name")
    ]


def test_md5_integer_pair_unique_index_rejects_duplicates(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    get_unique_new_cool_name(database, name_generator=lambda _word_count: "alpha-beta")
    stored_ints = fetch_all(
        database,
        "SELECT cool_name_md5_int_1, cool_name_md5_int_2 FROM cool_names",
    )[0]

    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO cool_names "
            "(cool_name_md5_int_1, cool_name_md5_int_2, cool_name) "
            "VALUES (?, ?, ?)",
            (*stored_ints, "different-text"),
        )


def test_session_creation_allocates_and_owns_one_cool_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda word_count: "precise-schema" if word_count == 2 else "unused-name-here",
    )

    session = create_a_rodex_session(
        database,
        codex_session_uuid=uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
    )

    assert session.cool_name == "precise-schema"
    assert session.cool_names_id == 1
    assert fetch_all(database, "SELECT cool_names_id FROM rodex_sessions") == [(1,)]
    assert fetch_all(database, "SELECT id, cool_name FROM cool_names") == [
        (1, "precise-schema")
    ]
