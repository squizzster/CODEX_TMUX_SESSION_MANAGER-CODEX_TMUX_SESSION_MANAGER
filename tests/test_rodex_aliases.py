from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from cool_name import RODEX_RESERVED_WORDS, CoolNameError, ReservedCoolNameError
from rodex_functions import (
    RodexSessionError,
    RodexSessionsUserIdentity,
    assign_a_user_defined_cool_name,
    create_a_rodex_session,
    list_rodex_session_runtimes_for_a_user,
    lookup_rodex_session_id_from_a_cool_name,
    lookup_rodex_session_names,
)

DNA = RodexSessionsUserIdentity(1009, 1010, "dna")
OTHER_USER = RodexSessionsUserIdentity(2001, 2002, "other")


def _cool_names(database: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT id, cool_name FROM cool_names ORDER BY id"
        ).fetchall()


def _create_session(
    database: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cool_name: str,
    codex_int: int,
    owner: RodexSessionsUserIdentity = DNA,
) -> int:
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: cool_name
    )
    return create_a_rodex_session(
        database,
        codex_session_uuid=uuid.UUID(int=codex_int),
        user_identity=owner,
        tmux_server_socket_path=f"/tmp/{cool_name}.sock",
        tmux_session_name=cool_name,
    ).id


def test_alias_is_one_owned_integer_identity_and_force_replaces_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session_id = _create_session(
        database, monkeypatch, cool_name="black-sawfly", codex_int=1
    )

    assigned = assign_a_user_defined_cool_name(
        "black-sawfly", "user_defined_field", database, user_identity=DNA
    )

    assert assigned.user_defined_cool_name == "user_defined_field"
    assert lookup_rodex_session_id_from_a_cool_name("user_defined_field", database) == (
        session_id
    )
    with pytest.raises(RodexSessionError, match="use -f or --force"):
        assign_a_user_defined_cool_name(
            "black-sawfly", "replacement", database, user_identity=DNA
        )
    assert _cool_names(database) == [(1, "black-sawfly"), (2, "user_defined_field")]

    replaced = assign_a_user_defined_cool_name(
        "user_defined_field",
        "replacement",
        database,
        force=True,
        user_identity=DNA,
    )

    assert replaced.user_defined_cool_name == "replacement"
    assert lookup_rodex_session_id_from_a_cool_name("user_defined_field", database) is None
    assert lookup_rodex_session_id_from_a_cool_name("black-sawfly", database) == session_id
    assert lookup_rodex_session_id_from_a_cool_name("replacement", database) == session_id
    assert lookup_rodex_session_names(session_id, database) == replaced
    assert _cool_names(database) == [
        (1, "black-sawfly"),
        (2, "user_defined_field"),
        (3, "replacement"),
    ]


@pytest.mark.parametrize(
    "reserved_name",
    [*sorted(RODEX_RESERVED_WORDS), "Alias", "EXEC", "RUNNING"],
)
def test_reserved_aliases_are_rejected_without_consuming_an_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reserved_name: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    _create_session(database, monkeypatch, cool_name="black-sawfly", codex_int=1)

    with pytest.raises(ReservedCoolNameError, match="reserved"):
        assign_a_user_defined_cool_name(
            "black-sawfly", reserved_name, database, user_identity=DNA
        )

    assert _cool_names(database) == [(1, "black-sawfly")]


@pytest.mark.parametrize(
    "invalid_name",
    ["", "two words", "tmux:window", "has.period", "x" * 81],
)
def test_aliases_must_be_portable_tmux_session_names_without_consuming_an_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_name: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    _create_session(database, monkeypatch, cool_name="black-sawfly", codex_int=1)

    with pytest.raises(CoolNameError, match=r"cool_name|Rodex names"):
        assign_a_user_defined_cool_name(
            "black-sawfly", invalid_name, database, user_identity=DNA
        )

    assert _cool_names(database) == [(1, "black-sawfly")]


def test_alias_ownership_and_cross_session_uniqueness_are_enforced_transactionally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    first_id = _create_session(database, monkeypatch, cool_name="black-sawfly", codex_int=1)
    _create_session(
        database,
        monkeypatch,
        cool_name="silver-otter",
        codex_int=2,
        owner=OTHER_USER,
    )

    with pytest.raises(RodexSessionError, match="not owned"):
        assign_a_user_defined_cool_name(
            "black-sawfly", "unauthorised", database, user_identity=OTHER_USER
        )
    with pytest.raises(RodexSessionError, match="another session"):
        assign_a_user_defined_cool_name(
            "black-sawfly", "silver-otter", database, user_identity=DNA
        )

    assert lookup_rodex_session_names(first_id, database).user_defined_cool_name is None  # type: ignore[union-attr]
    assert _cool_names(database) == [(1, "black-sawfly"), (2, "silver-otter")]


def test_runtime_listing_is_filtered_by_the_complete_posix_user_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session_id = _create_session(
        database, monkeypatch, cool_name="black-sawfly", codex_int=1
    )
    _create_session(
        database,
        monkeypatch,
        cool_name="silver-otter",
        codex_int=2,
        owner=OTHER_USER,
    )
    assign_a_user_defined_cool_name("black-sawfly", "work", database, user_identity=DNA)

    runtimes = list_rodex_session_runtimes_for_a_user(database, user_identity=DNA)

    assert len(runtimes) == 1
    assert runtimes[0].rodex_sessions_id == session_id
    assert runtimes[0].cool_name == "black-sawfly"
    assert runtimes[0].user_defined_cool_name == "work"
    assert runtimes[0].display_name == "work"
    assert runtimes[0].codex_session_uuid == uuid.UUID(int=1)
