"""Fast smoke test for the complete Rodex database workflow."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from rodex_functions import (
    RodexSessionsUserIdentity,
    create_a_rodex_session,
    lookup_id_from_a_rodex_uuid,
    lookup_rodex_session_log,
    lookup_rodex_uuid_from_an_id,
)


def test_full_rodex_database_regression(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    user = RodexSessionsUserIdentity(uid=1009, gid=1010, user_name="dna")

    first = create_a_rodex_session(
        database, codex_session_uuid=uuid.UUID(int=1), user_identity=user
    )
    second = create_a_rodex_session(
        database, codex_session_uuid=uuid.UUID(int=2), user_identity=user
    )

    assert [first.id, second.id] == [1, 2]
    assert first.rodex_uuid != second.rodex_uuid
    assert lookup_id_from_a_rodex_uuid(first.rodex_uuid, database) == first.id
    assert lookup_rodex_uuid_from_an_id(second.id, database) == second.rodex_uuid

    first_log = lookup_rodex_session_log(first.id, database)
    second_log = lookup_rodex_session_log(second.id, database)
    assert first_log is not None and second_log is not None
    assert first_log.rodex_sessions_users_id == second_log.rodex_sessions_users_id == 1

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id, uid, gid, user_name FROM rodex_sessions_users"
        ).fetchall() == [(1, 1009, 1010, "dna")]
        assert connection.execute(
            "SELECT rodex_sessions_id, rodex_sessions_users_id "
            "FROM rodex_sessions_log ORDER BY id"
        ).fetchall() == [(1, 1), (2, 1)]
        assert connection.execute(
            "SELECT cool_names_id FROM rodex_sessions ORDER BY id"
        ).fetchall() == [(1,), (2,)]
        assert connection.execute(
            "SELECT name, seq FROM sqlite_sequence ORDER BY name"
        ).fetchall() == [
            ("cool_names", 2),
            ("rodex_sessions", 2),
            ("rodex_sessions_log", 2),
            ("rodex_sessions_users", 1),
        ]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
