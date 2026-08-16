from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

import rodex_functions.sessions as sessions_module
from rodex.cli import RodexLaunchError, run
from rodex_functions import (
    RodexSessionError,
    RodexSessionStatisticsConflictError,
    RodexSessionStatisticsSourceObservation,
    RodexSessionTurnStatisticsAmbiguousError,
    RodexSessionTurnStatisticsObservation,
    create_a_rodex_session,
    generate_an_unregistered_rodex_uuid_candidate,
    list_rodex_session_statistics_sources,
    publish_rodex_session_statistics,
    read_rodex_session_statistics,
    read_rodex_session_turn_statistics,
    record_a_rodex_session_runtime_resume,
    record_rodex_session_statistics_worker_health,
)

CODEX_UUID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
REPLACEMENT_UUID = uuid.UUID(int=CODEX_UUID.int + 1)


def _columns(database: Path, table: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(f"PRAGMA table_info({table})").fetchall()


def _observation(root: Path, codex_uuid: uuid.UUID, marker: str = "a"):
    path = (root / f"rollout-{codex_uuid}.jsonl").resolve()
    content = marker.encode()
    return RodexSessionStatisticsSourceObservation(
        codex_session_uuid=codex_uuid,
        rollout_file_path=path,
        analyzed_size_bytes=len(content),
        analyzed_mtime_ns=123,
        analyzed_prefix_sha256=hashlib.sha256(content).hexdigest(),
        verified_at_utc="2026-08-16T12:00:00Z",
    )


def _turn(
    turn_id: str,
    *,
    codex_uuid: uuid.UUID = CODEX_UUID,
    outcome: str = "completed",
    total_tokens: int = 10,
    started_at: str | None = "2026-08-16T12:00:00Z",
    terminal_at: str | None = "2026-08-16T12:00:01Z",
) -> RodexSessionTurnStatisticsObservation:
    return RodexSessionTurnStatisticsObservation(
        codex_session_uuid=codex_uuid,
        codex_turn_id=turn_id,
        started_at_utc=started_at,
        terminal_at_utc=terminal_at,
        outcome=outcome,
        turn_statistics={
            "must_have_basic_stats": {"token_usage": {"total_tokens": total_tokens}},
            "recommended_insight_stats": {"hands_on": True},
            "raw_events": [{"prompt": "must-not-persist"}],
        },
    )


def _publish(
    database: Path,
    source_root: Path,
    *,
    based_on: int | None = None,
    aggregate: dict[str, object] | None = None,
    turns: list[RodexSessionTurnStatisticsObservation] | None = None,
):
    return publish_rodex_session_statistics(
        1,
        database,
        expected_current_codex_uuid=CODEX_UUID,
        based_on_statistics_revision=based_on,
        statistics_projection_schema_version="aggregate-v1",
        calculated_at_utc="2026-08-16T12:00:00Z",
        coverage_state="complete",
        aggregate_statistics=aggregate
        or {
            "event_count": 4,
            "source_count": 1,
            "must_have_basic_stats": {"turns": {"started": 3}},
            "recommended_insight_stats": {},
            "audit": {"privacy": "aggregate-only"},
        },
        analyzed_sources=[_observation(source_root, CODEX_UUID)],
        turn_statistics=[] if turns is None else turns,
    )


def _snapshot(database: Path, source_root: Path) -> str:
    created = create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)
    _publish(database, source_root, turns=[_turn("turn-exact", total_tokens=42)])
    return created.cool_name


def test_statistics_schema_is_constrained_and_sources_register_on_create(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)

    assert [
        (row[1], row[2].upper(), row[3], row[5])
        for row in _columns(database, "rodex_sessions_statistics")
    ] == [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("statistics_revision", "INTEGER", 1, 0),
        ("statistics_projection_schema_version", "TEXT", 1, 0),
        ("calculated_at_utc", "TEXT", 1, 0),
        ("coverage_state", "TEXT", 1, 0),
        ("aggregate_statistics_json", "TEXT", 1, 0),
    ]
    assert [
        (row[1], row[2].upper(), row[3], row[5])
        for row in _columns(database, "rodex_sessions_statistics_workers")
    ] == [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("worker_state", "TEXT", 1, 0),
        ("diagnostic_code", "TEXT", 0, 0),
        ("last_attempted_at_utc", "TEXT", 1, 0),
        ("consecutive_failures", "INTEGER", 1, 0),
        ("next_retry_at_utc", "TEXT", 0, 0),
    ]
    assert [
        (row[1], row[2].upper(), row[3], row[5])
        for row in _columns(database, "rodex_sessions_statistics_sources")
    ] == [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("codex_session_uuid_int_1", "BIGINT", 1, 0),
        ("codex_session_uuid_int_2", "BIGINT", 1, 0),
        ("first_linked_at_utc", "TEXT", 1, 0),
        ("rollout_file_path", "TEXT", 0, 0),
        ("analyzed_size_bytes", "INTEGER", 0, 0),
        ("analyzed_mtime_ns", "INTEGER", 0, 0),
        ("analyzed_prefix_sha256", "TEXT", 0, 0),
        ("verified_at_utc", "TEXT", 0, 0),
        ("included_statistics_revision", "INTEGER", 0, 0),
    ]
    assert [
        (row[1], row[2].upper(), row[3], row[5])
        for row in _columns(database, "rodex_sessions_statistics_turns")
    ] == [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("rodex_sessions_statistics_sources_id", "INTEGER", 1, 0),
        ("codex_turn_id_sha256_int_1", "BIGINT", 1, 0),
        ("codex_turn_id_sha256_int_2", "BIGINT", 1, 0),
        ("codex_turn_id_sha256_int_3", "BIGINT", 1, 0),
        ("codex_turn_id_sha256_int_4", "BIGINT", 1, 0),
        ("codex_turn_id", "TEXT", 1, 0),
        ("included_statistics_revision", "INTEGER", 1, 0),
        ("started_at_utc", "TEXT", 0, 0),
        ("terminal_at_utc", "TEXT", 0, 0),
        ("outcome", "TEXT", 1, 0),
        ("turn_statistics_json", "TEXT", 1, 0),
    ]
    source = list_rodex_session_statistics_sources(1, database)[0]
    assert source.codex_session_uuid == CODEX_UUID
    assert source.rollout_file_path is None
    with sqlite3.connect(database) as connection:
        unique = connection.execute(
            "PRAGMA index_info(rodex_sessions_statistics_sources_codex_uuid_unique)"
        ).fetchall()
        by_session = connection.execute(
            "PRAGMA index_info(rodex_sessions_statistics_sources_session)"
        ).fetchall()
    assert [row[2] for row in unique] == [
        "codex_session_uuid_int_1",
        "codex_session_uuid_int_2",
    ]
    assert [row[2] for row in by_session] == ["rodex_sessions_id"]


def test_publish_is_monotonic_atomic_and_whitelists_aggregate_fields(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)
    first = _publish(
        database,
        tmp_path,
        aggregate={
            "event_count": 1,
            "source_count": 1,
            "must_have_basic_stats": {"turns": 1},
            "raw_events": [{"prompt": "must-not-persist"}],
            "protocol_id": "temporary-id",
        },
    )
    second = _publish(database, tmp_path, based_on=1)

    assert first.statistics_revision == 1
    assert second.statistics_revision == 2
    view = read_rodex_session_statistics(1, database)
    assert view.statistics == second
    assert view.worker is not None
    assert view.worker.worker_state == "up_to_date"
    assert view.statistics.aggregate_statistics == {
        "audit": {"privacy": "aggregate-only"},
        "event_count": 4,
        "must_have_basic_stats": {"turns": {"started": 3}},
        "recommended_insight_stats": {},
        "source_count": 1,
    }
    assert view.sources[0].included_statistics_revision == 2
    assert view.sources[0].rollout_file_path == str(
        (tmp_path / f"rollout-{CODEX_UUID}.jsonl").resolve()
    )
    assert b"must-not-persist" not in database.read_bytes()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_statistics"
        ).fetchone() == (1,)


def test_health_failure_preserves_last_good_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)
    snapshot = _publish(database, tmp_path)

    health = record_rodex_session_statistics_worker_health(
        1,
        database,
        expected_current_codex_uuid=CODEX_UUID,
        worker_state="degraded",
        diagnostic_code="analytics_io_error",
        last_attempted_at_utc="2026-08-16T12:01:00Z",
        consecutive_failures=2,
        next_retry_at_utc="2026-08-16T12:01:02Z",
    )

    view = read_rodex_session_statistics(1, database)
    assert view.statistics == snapshot
    assert view.worker == health
    assert view.worker.consecutive_failures == 2


def test_turn_publication_is_revisioned_mark_and_sweep_with_stable_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)
    _publish(database, tmp_path, turns=[_turn("turn-a"), _turn("turn-b")])
    first_a = read_rodex_session_turn_statistics(1, "turn-a", database).turn
    first_b = read_rodex_session_turn_statistics(1, "turn-b", database).turn
    assert first_a is not None
    assert first_b is not None

    _publish(
        database,
        tmp_path,
        based_on=1,
        turns=[
            _turn("turn-a", total_tokens=99),
            _turn("turn-c", outcome="open", terminal_at=None),
        ],
    )

    second_a = read_rodex_session_turn_statistics(1, "turn-a", database).turn
    second_c = read_rodex_session_turn_statistics(1, "turn-c", database).turn
    assert second_a is not None
    assert second_c is not None
    assert second_a.id == first_a.id
    assert second_a.included_statistics_revision == 2
    assert second_a.turn_statistics["must_have_basic_stats"]["token_usage"] == {
        "total_tokens": 99
    }
    assert second_c.outcome == "open"
    assert second_c.terminal_at_utc is None
    assert read_rodex_session_turn_statistics(1, "turn-b", database).turn is None

    _publish(database, tmp_path, based_on=2, turns=[])
    assert read_rodex_session_turn_statistics(1, "turn-a", database).turn is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_statistics_turns"
        ).fetchone() == (0,)


def test_turn_publication_rolls_back_session_sources_turns_and_health_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)
    before = _publish(database, tmp_path, turns=[_turn("turn-a")])
    before_view = read_rodex_session_statistics(1, database)
    before_turn = read_rodex_session_turn_statistics(1, "turn-a", database).turn

    def fail_health(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("forced publication failure")

    monkeypatch.setattr(sessions_module, "_upsert_statistics_worker", fail_health)
    with pytest.raises(RuntimeError, match="forced publication failure"):
        _publish(
            database,
            tmp_path,
            based_on=before.statistics_revision,
            turns=[_turn("turn-a", total_tokens=999), _turn("turn-new")],
        )

    after_view = read_rodex_session_statistics(1, database)
    after_turn = read_rodex_session_turn_statistics(1, "turn-a", database).turn
    assert after_view == before_view
    assert after_turn == before_turn
    assert read_rodex_session_turn_statistics(1, "turn-new", database).turn is None


def test_same_turn_id_across_lineage_requires_source_qualification(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
    )
    _publish(database, tmp_path, turns=[_turn("shared")])
    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        database,
        codex_session_uuid=REPLACEMENT_UUID,
    )
    publish_rodex_session_statistics(
        1,
        database,
        expected_current_codex_uuid=REPLACEMENT_UUID,
        based_on_statistics_revision=1,
        statistics_projection_schema_version="rodex-statistics-v2",
        calculated_at_utc="2026-08-16T12:05:00Z",
        coverage_state="complete",
        aggregate_statistics={"event_count": 8},
        analyzed_sources=[
            _observation(tmp_path, CODEX_UUID, "a"),
            _observation(tmp_path, REPLACEMENT_UUID, "b"),
        ],
        turn_statistics=[
            _turn("shared", codex_uuid=CODEX_UUID, total_tokens=10),
            _turn("shared", codex_uuid=REPLACEMENT_UUID, total_tokens=20),
        ],
    )

    with pytest.raises(RodexSessionTurnStatisticsAmbiguousError):
        read_rodex_session_turn_statistics(1, "shared", database)
    exact = read_rodex_session_turn_statistics(
        1, "shared", database, codex_session_uuid=REPLACEMENT_UUID
    ).turn
    assert exact is not None
    assert exact.codex_session_uuid == REPLACEMENT_UUID
    assert exact.turn_statistics["must_have_basic_stats"]["token_usage"] == {
        "total_tokens": 20
    }
    with pytest.raises(RodexLaunchError, match="multiple Codex sources"):
        run(
            ["_stats", created.cool_name, "--turn", "shared", "--json"],
            database_path=database,
        )
    assert (
        run(
            [
                "_stats",
                created.cool_name,
                "--turn",
                "shared",
                "--source",
                str(REPLACEMENT_UUID),
                "--json",
            ],
            database_path=database,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["turn"]["codex_session_uuid"] == str(REPLACEMENT_UUID)


def test_turn_from_registered_but_unanalyzed_source_is_rejected_atomically(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
    )
    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        database,
        codex_session_uuid=REPLACEMENT_UUID,
    )
    with pytest.raises(RodexSessionStatisticsConflictError, match="outside"):
        publish_rodex_session_statistics(
            1,
            database,
            expected_current_codex_uuid=REPLACEMENT_UUID,
            based_on_statistics_revision=None,
            statistics_projection_schema_version="rodex-statistics-v2",
            calculated_at_utc="2026-08-16T12:05:00Z",
            coverage_state="complete",
            aggregate_statistics={},
            analyzed_sources=[_observation(tmp_path, REPLACEMENT_UUID)],
            turn_statistics=[_turn("old", codex_uuid=CODEX_UUID)],
        )
    assert read_rodex_session_statistics(1, database).statistics is None


def test_turn_digest_collision_cannot_overwrite_exact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)
    monkeypatch.setattr(
        sessions_module,
        "_turn_id_sha256_signed_bigints",
        lambda _turn_id: (1, 2, 3, 4),
    )
    with pytest.raises(RodexSessionStatisticsConflictError, match="digest collision"):
        _publish(database, tmp_path, turns=[_turn("first"), _turn("second")])
    assert read_rodex_session_statistics(1, database).statistics is None


def test_turn_projection_is_privacy_filtered_and_supports_missing_timestamps(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)
    _publish(
        database,
        tmp_path,
        turns=[
            _turn(
                "aborted-no-time",
                outcome="aborted",
                started_at=None,
                terminal_at=None,
            )
        ],
    )
    turn = read_rodex_session_turn_statistics(1, "aborted-no-time", database).turn
    assert turn is not None
    assert turn.started_at_utc is None
    assert turn.terminal_at_utc is None
    assert set(turn.turn_statistics) == {
        "must_have_basic_stats",
        "recommended_insight_stats",
    }
    assert b"must-not-persist" not in database.read_bytes()


def test_turn_mark_and_sweep_scales_beyond_sqlite_bind_limit(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)
    turns = [_turn(f"turn-{index}") for index in range(1_100)]
    _publish(database, tmp_path, turns=turns)
    _publish(database, tmp_path, based_on=1, turns=turns)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*), MIN(included_statistics_revision), "
            "MAX(included_statistics_revision) FROM rodex_sessions_statistics_turns"
        ).fetchone() == (1_100, 2, 2)


def test_publish_and_health_reject_stale_identity_and_revision_fences(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
    )
    _publish(database, tmp_path)
    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        database,
        codex_session_uuid=REPLACEMENT_UUID,
    )

    with pytest.raises(RodexSessionStatisticsConflictError, match="Codex UUID"):
        _publish(database, tmp_path, based_on=1)
    with pytest.raises(RodexSessionStatisticsConflictError, match="Codex UUID"):
        record_rodex_session_statistics_worker_health(
            1,
            database,
            expected_current_codex_uuid=CODEX_UUID,
            worker_state="degraded",
            diagnostic_code="stale",
            last_attempted_at_utc="2026-08-16T12:02:00Z",
            consecutive_failures=1,
        )


def test_publish_rejects_a_stale_statistics_revision_fence(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)
    _publish(database, tmp_path, turns=[_turn("stable")])
    current = _publish(
        database, tmp_path, based_on=1, turns=[_turn("stable", total_tokens=20)]
    )
    current_turn = read_rodex_session_turn_statistics(1, "stable", database).turn

    with pytest.raises(RodexSessionStatisticsConflictError, match="revision changed"):
        _publish(
            database,
            tmp_path,
            based_on=1,
            turns=[_turn("stable", total_tokens=999), _turn("stale-new")],
        )

    assert read_rodex_session_statistics(1, database).statistics == current
    assert read_rodex_session_turn_statistics(1, "stable", database).turn == current_turn
    assert read_rodex_session_turn_statistics(1, "stale-new", database).turn is None


def test_publish_rolls_back_when_analyzed_source_is_not_registered(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)
    before = _publish(database, tmp_path)

    with pytest.raises(RodexSessionStatisticsConflictError, match="unregistered"):
        publish_rodex_session_statistics(
            1,
            database,
            expected_current_codex_uuid=CODEX_UUID,
            based_on_statistics_revision=1,
            statistics_projection_schema_version="aggregate-v1",
            calculated_at_utc="2026-08-16T12:03:00Z",
            coverage_state="gapped",
            aggregate_statistics={"event_count": 0},
            analyzed_sources=[_observation(tmp_path, REPLACEMENT_UUID)],
            turn_statistics=[],
        )

    after = read_rodex_session_statistics(1, database)
    assert after.statistics == before
    assert after.sources[0].included_statistics_revision == 1


def test_historical_codex_source_cannot_move_to_another_rodex_lineage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_uuid=CODEX_UUID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
    )
    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        database,
        codex_session_uuid=REPLACEMENT_UUID,
    )

    with pytest.raises(RodexSessionError, match="statistics lineage"):
        create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)

    assert [
        source.codex_session_uuid
        for source in list_rodex_session_statistics_sources(1, database)
    ] == [CODEX_UUID, REPLACEMENT_UUID]


@pytest.mark.parametrize(
    "statement, parameters",
    [
        (
            "INSERT INTO rodex_sessions_statistics "
            "(rodex_sessions_id, statistics_revision, "
            "statistics_projection_schema_version, calculated_at_utc, "
            "coverage_state, aggregate_statistics_json) VALUES (1, 0, 'v1', "
            "'2026-08-16T12:00:00Z', 'complete', '{}')",
            (),
        ),
        (
            "INSERT INTO rodex_sessions_statistics_workers "
            "(rodex_sessions_id, worker_state, diagnostic_code, "
            "last_attempted_at_utc, consecutive_failures, next_retry_at_utc) "
            "VALUES (1, 'up_to_date', 'bad', '2026-08-16T12:00:00Z', 1, NULL)",
            (),
        ),
        (
            "INSERT INTO rodex_sessions_statistics_workers "
            "(rodex_sessions_id, worker_state, diagnostic_code, "
            "last_attempted_at_utc, consecutive_failures, next_retry_at_utc) "
            "VALUES (1, 'degraded', 'free form detail', "
            "'2026-08-16T12:00:00Z', 1, NULL)",
            (),
        ),
        (
            "UPDATE rodex_sessions_statistics_sources "
            "SET included_statistics_revision = 1 WHERE rodex_sessions_id = 1",
            (),
        ),
    ],
)
def test_database_checks_reject_invalid_analytics_state(
    tmp_path: Path, statement: str, parameters: tuple[object, ...]
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement, parameters)


@pytest.mark.parametrize(
    "statement",
    [
        (
            "UPDATE rodex_sessions_statistics_sources "
            "SET included_statistics_revision = NULL WHERE rodex_sessions_id = 1"
        ),
        (
            "UPDATE rodex_sessions_statistics_turns "
            "SET included_statistics_revision = 999 WHERE rodex_sessions_id = 1"
        ),
        (
            "UPDATE rodex_sessions_statistics_turns "
            "SET outcome = 'open' WHERE rodex_sessions_id = 1"
        ),
        (
            "UPDATE rodex_sessions_statistics_turns "
            "SET turn_statistics_json = '[]' WHERE rodex_sessions_id = 1"
        ),
    ],
)
def test_database_constraints_reject_detached_or_invalid_turn_projections(
    tmp_path: Path, statement: str
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)
    _publish(database, tmp_path, turns=[_turn("turn-a")])

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)
            connection.commit()
        connection.rollback()


def test_worker_health_rejects_free_form_diagnostic_text(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)

    with pytest.raises(ValueError, match="diagnostic_code must contain"):
        record_rodex_session_statistics_worker_health(
            1,
            database,
            expected_current_codex_uuid=CODEX_UUID,
            worker_state="degraded",
            diagnostic_code="rollout failed: /sensitive/path",
            last_attempted_at_utc="2026-08-16T12:00:00Z",
            consecutive_failures=1,
        )


def test_stats_commands_read_database_before_executable_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session_name = _snapshot(database, tmp_path)
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda _name: (_ for _ in ()).throw(AssertionError("must not resolve tools")),
    )

    assert run(["_stats", session_name, "--json"], database_path=database) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["statistics_revision"] == 1
    assert payload["statistics"]["must_have_basic_stats"]["turns"]["started"] == 3

    assert (
        run(
            ["_stats", session_name, "--turn", "turn-exact", "--json"],
            database_path=database,
        )
        == 0
    )
    exact = json.loads(capsys.readouterr().out)
    assert exact["turn"]["turn_id"] == "turn-exact"
    assert exact["turn"]["codex_session_uuid"] == str(CODEX_UUID)
    assert exact["turn"]["included_statistics_revision"] == 1
    assert exact["statistics"]["must_have_basic_stats"]["token_usage"] == {
        "total_tokens": 42
    }

    assert run(["_stats-status", session_name], database_path=database) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["worker_state"] == "up_to_date"
    assert status["included_source_count"] == 1
    assert "statistics" not in status
    with pytest.raises(RodexLaunchError, match="not present in the latest"):
        run(
            ["_stats", session_name, "--turn", "missing"],
            database_path=database,
        )


def test_stats_status_distinguishes_no_snapshot_from_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database, codex_session_uuid=CODEX_UUID)

    assert run(["_stats-status", created.cool_name], database_path=database) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["statistics_revision"] is None
    assert status["worker_state"] == "not_started"
    with pytest.raises(RodexLaunchError, match="no analytics snapshot"):
        run(["_stats", created.cool_name], database_path=database)


def test_unregistered_uuid_candidate_becomes_the_persisted_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    candidate = generate_an_unregistered_rodex_uuid_candidate(database)

    created = create_a_rodex_session(
        database,
        rodex_session_uuid=candidate,
        codex_session_uuid=CODEX_UUID,
    )

    assert created.rodex_uuid == candidate
