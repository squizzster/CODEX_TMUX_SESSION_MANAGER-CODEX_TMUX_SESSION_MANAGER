"""One end-to-end lock on the complete current Rodex database contract."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

import rodex_functions.sessions as session_module
from rodex_functions import (
    RodexSessionsUser,
    RodexSessionsUserIdentity,
    create_a_rodex_session,
    join_signed_bigints_into_a_rodex_uuid,
    lookup_id_from_a_rodex_uuid,
    lookup_or_create_rodex_sessions_user,
    lookup_rodex_session_log,
    lookup_rodex_sessions_user,
    lookup_rodex_uuid_from_an_id,
    record_a_rodex_session_access,
    split_a_rodex_uuid_into_signed_bigints,
)


def test_full_rodex_database_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    user_identity = RodexSessionsUserIdentity(uid=1009, gid=1010, user_name="dna")
    uuid_integers = iter(
        [
            0xFEDCBA98765432100123456789ABCDEF,
            0x0123456789ABCDEFFEDCBA9876543210,
        ]
    )
    created_timestamps = iter(
        [
            "2026-08-15T12:00:00.000001Z",
            "2026-08-15T12:00:01.000002Z",
        ]
    )
    monkeypatch.setattr(
        session_module.secrets, "randbits", lambda requested_bits: next(uuid_integers)
    )
    monkeypatch.setattr(
        session_module, "_utc_now_timestamp", lambda: next(created_timestamps)
    )

    first = create_a_rodex_session(database, user_identity=user_identity)
    second = create_a_rodex_session(database, user_identity=user_identity)
    repeated_user = lookup_or_create_rodex_sessions_user(1009, 1010, "dna", database)
    updated_second_log = record_a_rodex_session_access(
        second.id,
        database,
        accessed_at_utc=datetime(2026, 8, 15, 12, 30, tzinfo=UTC),
    )

    expected_first_uuid = uuid.UUID(int=0xFEDCBA98765432100123456789ABCDEF)
    expected_second_uuid = uuid.UUID(int=0x0123456789ABCDEFFEDCBA9876543210)
    assert (first.id, first.rodex_uuid) == (1, expected_first_uuid)
    assert (second.id, second.rodex_uuid) == (2, expected_second_uuid)
    assert repeated_user == RodexSessionsUser(1, 1009, 1010, "dna")
    assert lookup_rodex_sessions_user(1, database) == repeated_user
    assert lookup_id_from_a_rodex_uuid(expected_first_uuid, database) == 1
    assert lookup_id_from_a_rodex_uuid(str(expected_second_uuid), database) == 2
    assert lookup_rodex_uuid_from_an_id(1, database) == expected_first_uuid
    assert lookup_rodex_uuid_from_an_id(2, database) == expected_second_uuid
    assert updated_second_log.last_accessed_at_utc == "2026-08-15T12:30:00.000000Z"

    with sqlite3.connect(database) as connection:
        application_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert application_tables == {
            "rodex_sessions",
            "rodex_sessions_log",
            "rodex_sessions_users",
        }

        expected_columns = {
            "rodex_sessions": [
                ("id", "INTEGER", 0, 1),
                ("uuid_int_1", "BIGINT", 1, 0),
                ("uuid_int_2", "BIGINT", 1, 0),
            ],
            "rodex_sessions_users": [
                ("id", "INTEGER", 0, 1),
                ("uid", "INTEGER", 1, 0),
                ("gid", "INTEGER", 1, 0),
                ("user_name", "TEXT", 1, 0),
            ],
            "rodex_sessions_log": [
                ("id", "INTEGER", 0, 1),
                ("rodex_sessions_id", "INTEGER", 1, 0),
                ("created_at_utc", "TEXT", 1, 0),
                ("rodex_sessions_users_id", "INTEGER", 1, 0),
                ("last_accessed_at_utc", "TEXT", 1, 0),
            ],
        }
        for table_name, expected in expected_columns.items():
            columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
            assert [(row[1], row[2], row[3], row[5]) for row in columns] == expected
            definition = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()[0]
            assert "ID INTEGER PRIMARY KEY AUTOINCREMENT" in definition.upper()

        expected_indexes = {
            "rodex_sessions": {
                "rodex_sessions_uuid_ints_unique": ["uuid_int_1", "uuid_int_2"]
            },
            "rodex_sessions_users": {
                "rodex_sessions_users_uid_gid_user_name_unique": [
                    "uid",
                    "gid",
                    "user_name",
                ]
            },
            "rodex_sessions_log": {
                "rodex_sessions_log_rodex_sessions_id_unique": ["rodex_sessions_id"]
            },
        }
        for table_name, expected in expected_indexes.items():
            indexes = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
            assert {row[1] for row in indexes} == set(expected)
            assert all(row[2] == 1 for row in indexes)
            for index_name, expected_index_columns in expected.items():
                index_columns = connection.execute(
                    f"PRAGMA index_info({index_name})"
                ).fetchall()
                assert [row[2] for row in index_columns] == expected_index_columns

        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(rodex_sessions_log)"
        ).fetchall()
        assert {(row[2], row[3], row[4]) for row in foreign_keys} == {
            ("rodex_sessions", "rodex_sessions_id", "id"),
            ("rodex_sessions_users", "rodex_sessions_users_id", "id"),
        }

        assert connection.execute(
            "SELECT id, uid, gid, user_name FROM rodex_sessions_users"
        ).fetchall() == [(1, 1009, 1010, "dna")]

        stored_sessions = connection.execute(
            "SELECT id, uuid_int_1, uuid_int_2, typeof(id), "
            "typeof(uuid_int_1), typeof(uuid_int_2) "
            "FROM rodex_sessions ORDER BY id"
        ).fetchall()
        assert stored_sessions == [
            (
                1,
                *split_a_rodex_uuid_into_signed_bigints(expected_first_uuid),
                *(["integer"] * 3),
            ),
            (
                2,
                *split_a_rodex_uuid_into_signed_bigints(expected_second_uuid),
                *(["integer"] * 3),
            ),
        ]
        for _, uuid_int_1, uuid_int_2, *_ in stored_sessions:
            assert join_signed_bigints_into_a_rodex_uuid(uuid_int_1, uuid_int_2) in {
                expected_first_uuid,
                expected_second_uuid,
            }

        assert connection.execute(
            "SELECT id, rodex_sessions_id, created_at_utc, "
            "rodex_sessions_users_id, last_accessed_at_utc "
            "FROM rodex_sessions_log ORDER BY id"
        ).fetchall() == [
            (1, 1, "2026-08-15T12:00:00.000001Z", 1, "2026-08-15T12:00:00.000001Z"),
            (2, 2, "2026-08-15T12:00:01.000002Z", 1, "2026-08-15T12:30:00.000000Z"),
        ]
        first_log = lookup_rodex_session_log(first.id, database)
        assert first_log is not None and first_log.rodex_sessions_users_id == 1

        assert connection.execute(
            "SELECT name, seq FROM sqlite_sequence ORDER BY name"
        ).fetchall() == [
            ("rodex_sessions", 2),
            ("rodex_sessions_log", 2),
            ("rodex_sessions_users", 1),
        ]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
