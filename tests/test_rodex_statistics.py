from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from test_statistics_projection import _snapshot as analyzer_snapshot

import rodex_registry.statistics as statistics_module
from rodex.cli import RodexLaunchError, run
from rodex_registry import (
    RodexSessionError,
    RodexSessionStatisticsConflictError,
    RodexSessionStatisticsSourceObservation,
    RodexSessionTurnStatisticsAmbiguousError,
    SessionStatisticsProjection,
    StatisticsProjectionError,
    TurnStatisticsProjection,
    create_a_rodex_session,
    generate_an_unregistered_rodex_session_id_candidate,
    list_rodex_session_statistics_sources,
    parse_session_statistics_snapshot,
    publish_rodex_session_statistics,
    read_rodex_session_statistics,
    read_rodex_session_turn_statistics,
    record_a_rodex_session_runtime_resume,
    record_rodex_session_statistics_worker_health,
    session_statistics_as_dict,
    turn_statistics_as_dict,
)

CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
REPLACEMENT_CODEX_SESSION_ID = uuid.UUID(int=CODEX_SESSION_ID.int + 1)


def _columns(database: Path, table: str) -> list[str]:
    with sqlite3.connect(database) as connection:
        return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


def _index_columns(database: Path, index: str) -> list[str]:
    with sqlite3.connect(database) as connection:
        return [str(row[2]) for row in connection.execute(f"PRAGMA index_info({index})")]


def _observation(root: Path, codex_session_id: uuid.UUID, marker: str = "a"):
    path = (root / f"rollout-{codex_session_id}.jsonl").resolve()
    content = marker.encode()
    return RodexSessionStatisticsSourceObservation(
        codex_session_id=codex_session_id,
        rollout_file_path=path,
        analyzed_size_bytes=len(content),
        analyzed_mtime_ns=123,
        analyzed_prefix_sha256=hashlib.sha256(content).hexdigest(),
        verified_at_utc="2026-08-16T12:00:00Z",
    )


def _base_projection() -> SessionStatisticsProjection:
    return parse_session_statistics_snapshot(analyzer_snapshot())


def _turn(
    turn_id: str,
    *,
    codex_session_id: uuid.UUID = CODEX_SESSION_ID,
    outcome: str = "completed",
    total_tokens: int = 10,
    terminal_at: str | None = "2026-08-16T12:00:01.000000Z",
) -> TurnStatisticsProjection:
    base = _base_projection().turn_statistics[0]
    return replace(
        base,
        codex_session_id=codex_session_id,
        codex_turn_id=turn_id,
        outcome=outcome,
        terminal_at_utc=terminal_at,
        total_tokens=total_tokens,
    )


def _projection(
    turns: tuple[TurnStatisticsProjection, ...] | None = None,
) -> SessionStatisticsProjection:
    base = _base_projection()
    selected = base.turn_statistics if turns is None else turns
    completed = sum(turn.outcome == "completed" for turn in selected)
    aborted = sum(turn.outcome == "aborted" for turn in selected)
    open_turns = sum(turn.outcome == "open" for turn in selected)
    workspace_turns = sum(turn.workspace_digest is not None for turn in selected)
    hour_turns = sum(turn.local_start_hour is not None for turn in selected)
    return replace(
        base,
        turns_started_count=len(selected),
        turns_completed_count=completed,
        turns_aborted_count=aborted,
        turns_open_count=open_turns,
        typical_turns_count=len(selected),
        hands_on_turn_count=sum(turn.hands_on for turn in selected),
        workspace_tagged_turn_count=workspace_turns,
        turns_in_busiest_workspace_count=workspace_turns,
        turns_with_local_hour_count=hour_turns,
        busiest_local_hour=(selected[0].local_start_hour if hour_turns else None),
        turns_in_busiest_local_hour_count=hour_turns,
        turn_statistics=selected,
    )


def _publish(
    database: Path,
    source_root: Path,
    *,
    based_on: int | None = None,
    projection: SessionStatisticsProjection | None = None,
    expected_codex_session_id: uuid.UUID = CODEX_SESSION_ID,
    sources: tuple[uuid.UUID, ...] = (CODEX_SESSION_ID,),
):
    supplied_projection = _projection(()) if projection is None else projection
    return publish_rodex_session_statistics(
        1,
        database,
        expected_current_codex_session_id=expected_codex_session_id,
        based_on_statistics_revision=based_on,
        statistics_projection_schema_version="rodex-statistics-v3",
        calculated_at_utc="2026-08-16T12:00:00Z",
        coverage_state="complete",
        statistics_projection=replace(
            supplied_projection,
            analyzer_source_count=len(sources),
            history_sessions_count=len(sources),
        ),
        analyzed_sources=[
            _observation(source_root, source, str(index))
            for index, source in enumerate(sources)
        ],
    )


def test_schema_is_relational_queryable_and_contains_no_json_columns(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)

    table_names = (
        "rodex_sessions_statistics",
        "rodex_sessions_statistics_distributions",
        "rodex_sessions_statistics_named_counts",
        "rodex_sessions_statistics_audit_limits",
        "rodex_sessions_statistics_sources",
        "rodex_sessions_statistics_turns",
        "rodex_sessions_statistics_turn_named_counts",
        "rodex_sessions_statistics_workers",
    )
    all_columns = {table: _columns(database, table) for table in table_names}
    assert not any(
        "json" in column.lower() for columns in all_columns.values() for column in columns
    )
    assert "total_tokens" in all_columns["rodex_sessions_statistics"]
    assert "total_tokens" in all_columns["rodex_sessions_statistics_turns"]
    assert all_columns["rodex_sessions_statistics_distributions"][3:6] == [
        "distribution_kind",
        "observation_count",
        "total",
    ]
    assert all_columns["rodex_sessions_statistics_named_counts"][-3:] == [
        "count_kind",
        "count_name",
        "occurrence_count",
    ]
    assert _index_columns(
        database, "rodex_sessions_statistics_distributions_kind_unique"
    ) == ["rodex_sessions_id", "distribution_kind"]
    assert _index_columns(
        database, "rodex_sessions_statistics_named_counts_key_unique"
    ) == ["rodex_sessions_id", "count_kind", "count_name"]
    assert _index_columns(
        database, "rodex_sessions_statistics_audit_limits_ordinal_unique"
    ) == ["rodex_sessions_id", "limit_ordinal"]
    assert _index_columns(
        database, "rodex_sessions_statistics_turn_named_counts_key_unique"
    ) == ["rodex_sessions_statistics_turns_id", "count_kind", "count_name"]
    assert _index_columns(
        database, "rodex_sessions_statistics_turns_session_id_revision_unique"
    ) == ["rodex_sessions_id", "id", "included_statistics_revision"]
    assert _index_columns(
        database, "rodex_sessions_statistics_turn_named_counts_session_kind"
    ) == ["rodex_sessions_id", "count_kind", "count_name"]


def test_full_projection_round_trips_through_relational_rows(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    projection = _projection((_turn("turn-exact", total_tokens=42),))

    published = _publish(database, tmp_path, projection=projection)
    view = read_rodex_session_statistics(1, database)
    exact = read_rodex_session_turn_statistics(1, "turn-exact", database).turn

    assert published.statistics_revision == 1
    assert view.statistics is not None
    assert session_statistics_as_dict(
        view.statistics.projection
    ) == session_statistics_as_dict(projection)
    assert exact is not None
    assert turn_statistics_as_dict(exact.projection) == turn_statistics_as_dict(
        projection.turn_statistics[0]
    )
    assert view.worker is not None and view.worker.worker_state == "up_to_date"
    assert view.sources[0].included_statistics_revision == 1


def test_turn_identity_bigints_reject_non_integer_storage(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    _publish(database, tmp_path, projection=_projection((_turn("turn-exact"),)))

    with sqlite3.connect(database) as connection:
        for part_number in range(1, 5):
            column_name = f"codex_turn_id_sha256_int_{part_number}"
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                connection.execute(
                    f"UPDATE rodex_sessions_statistics_turns SET {column_name} = 1.5"
                )


def test_statistics_reader_does_not_coerce_corrupt_source_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE rodex_sessions_statistics_sources "
            "SET codex_session_id_signed_bigint_1 = 1.5"
        )

    with pytest.raises(ValueError, match="signed 64-bit"):
        read_rodex_session_statistics(1, database)


def test_sql_can_sum_group_and_filter_base_statistics(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    projection = _projection(
        (
            _turn("turn-a", total_tokens=40),
            _turn("turn-b", total_tokens=60, outcome="aborted"),
        )
    )
    _publish(database, tmp_path, projection=projection)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT SUM(total_tokens) FROM rodex_sessions_statistics_turns"
        ).fetchone() == (100,)
        assert connection.execute(
            "SELECT outcome, SUM(total_tokens) FROM rodex_sessions_statistics_turns "
            "GROUP BY outcome ORDER BY outcome"
        ).fetchall() == [("aborted", 60), ("completed", 40)]
        tool_total = connection.execute(
            "SELECT SUM(occurrence_count) "
            "FROM rodex_sessions_statistics_turn_named_counts "
            "WHERE count_kind = 'model_tool'"
        ).fetchone()[0]
        assert tool_total == 4


def test_revision_mark_and_sweep_preserves_identity_and_replaces_children(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    _publish(database, tmp_path, projection=_projection((_turn("a"), _turn("b"))))
    first_a = read_rodex_session_turn_statistics(1, "a", database).turn
    assert first_a is not None

    second_projection = replace(
        _projection((_turn("a", total_tokens=99), _turn("c"))),
        named_counts=tuple(
            count
            for count in _base_projection().named_counts
            if not (count.count_kind == "model" and count.count_name == "gpt-5")
        ),
    )
    _publish(database, tmp_path, based_on=1, projection=second_projection)

    second_a = read_rodex_session_turn_statistics(1, "a", database).turn
    assert second_a is not None and second_a.id == first_a.id
    assert second_a.projection.total_tokens == 99
    assert second_a.included_statistics_revision == 2
    assert read_rodex_session_turn_statistics(1, "b", database).turn is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_statistics_named_counts "
            "WHERE count_kind = 'model' AND count_name = 'gpt-5'"
        ).fetchone() == (0,)


def test_publication_failure_rolls_back_every_relational_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    before = _publish(database, tmp_path, projection=_projection((_turn("stable"),)))
    before_bytes = database.read_bytes()

    def fail_health(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("forced publication failure")

    monkeypatch.setattr(statistics_module, "_upsert_statistics_worker", fail_health)
    with pytest.raises(RuntimeError, match="forced publication failure"):
        _publish(
            database,
            tmp_path,
            based_on=before.statistics_revision,
            projection=_projection((_turn("stable", total_tokens=999), _turn("new"))),
        )

    current = read_rodex_session_statistics(1, database).statistics
    stable = read_rodex_session_turn_statistics(1, "stable", database).turn
    assert current == before
    assert stable is not None and stable.projection.total_tokens == 10
    assert read_rodex_session_turn_statistics(1, "new", database).turn is None
    assert database.read_bytes() == before_bytes


def test_directly_constructed_incomplete_projection_cannot_publish(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    invalid = replace(_projection(()), distributions=())

    with pytest.raises(StatisticsProjectionError, match="structurally incomplete"):
        _publish(database, tmp_path, projection=invalid)

    assert read_rodex_session_statistics(1, database).statistics is None


def test_same_turn_id_in_two_sources_requires_exact_source(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
    )
    _publish(database, tmp_path, projection=_projection((_turn("shared"),)))
    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        database,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
    )
    projection = _projection(
        (
            _turn("shared", codex_session_id=CODEX_SESSION_ID, total_tokens=10),
            _turn("shared", codex_session_id=REPLACEMENT_CODEX_SESSION_ID, total_tokens=20),
        )
    )
    _publish(
        database,
        tmp_path,
        based_on=1,
        projection=projection,
        expected_codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
        sources=(CODEX_SESSION_ID, REPLACEMENT_CODEX_SESSION_ID),
    )

    with pytest.raises(RodexSessionTurnStatisticsAmbiguousError):
        read_rodex_session_turn_statistics(1, "shared", database)
    exact = read_rodex_session_turn_statistics(
        1, "shared", database, codex_session_id=REPLACEMENT_CODEX_SESSION_ID
    ).turn
    assert exact is not None and exact.projection.total_tokens == 20


def test_unanalyzed_source_and_digest_collision_are_atomic_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
    )
    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        database,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
    )
    with pytest.raises(RodexSessionStatisticsConflictError, match="omit"):
        _publish(
            database,
            tmp_path,
            projection=_projection((_turn("old", codex_session_id=CODEX_SESSION_ID),)),
            expected_codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
            sources=(REPLACEMENT_CODEX_SESSION_ID,),
        )
    assert read_rodex_session_statistics(1, database).statistics is None

    monkeypatch.setattr(
        statistics_module,
        "_turn_id_sha256_signed_bigints",
        lambda _value: (1, 2, 3, 4),
    )
    with pytest.raises(RodexSessionStatisticsConflictError, match="digest collision"):
        _publish(
            database,
            tmp_path,
            projection=_projection(
                (
                    _turn("first", codex_session_id=REPLACEMENT_CODEX_SESSION_ID),
                    _turn("second", codex_session_id=REPLACEMENT_CODEX_SESSION_ID),
                )
            ),
            expected_codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
            sources=(CODEX_SESSION_ID, REPLACEMENT_CODEX_SESSION_ID),
        )


def test_turn_mark_and_sweep_scales_beyond_sqlite_bind_limit(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    turns = tuple(_turn(f"turn-{index}") for index in range(1_100))
    projection = _projection(turns)
    _publish(database, tmp_path, projection=projection)
    _publish(database, tmp_path, based_on=1, projection=projection)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*), MIN(included_statistics_revision), "
            "MAX(included_statistics_revision) FROM rodex_sessions_statistics_turns"
        ).fetchone() == (1_100, 2, 2)


def test_stale_codex_session_id_and_revision_fences_preserve_last_good_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
    )
    first = _publish(database, tmp_path, projection=_projection((_turn("stable"),)))
    second = _publish(
        database,
        tmp_path,
        based_on=1,
        projection=_projection((_turn("stable", total_tokens=20),)),
    )
    with pytest.raises(RodexSessionStatisticsConflictError, match="revision changed"):
        _publish(database, tmp_path, based_on=1, projection=_projection((_turn("new"),)))
    assert read_rodex_session_statistics(1, database).statistics == second

    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        database,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
    )
    with pytest.raises(RodexSessionStatisticsConflictError, match="Codex session ID"):
        _publish(database, tmp_path, based_on=first.statistics_revision)


def test_health_is_separate_and_preserves_last_good_statistics(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    snapshot = _publish(database, tmp_path)
    health = record_rodex_session_statistics_worker_health(
        1,
        database,
        expected_current_codex_session_id=CODEX_SESSION_ID,
        worker_state="degraded",
        diagnostic_code="analytics_io_error",
        last_attempted_at_utc="2026-08-16T12:01:00Z",
        consecutive_failures=2,
        next_retry_at_utc="2026-08-16T12:01:02Z",
    )
    view = read_rodex_session_statistics(1, database)
    assert view.statistics == snapshot
    assert view.worker == health


def test_foreign_keys_and_checks_reject_detached_relational_facts(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    _publish(database, tmp_path, projection=_projection((_turn("turn-a"),)))

    statements = (
        "UPDATE rodex_sessions_statistics_sources SET included_statistics_revision = NULL",
        "UPDATE rodex_sessions_statistics_turns SET included_statistics_revision = 999",
        "UPDATE rodex_sessions_statistics_turns SET cached_input_tokens = input_tokens + 1",
        "UPDATE rodex_sessions_statistics_named_counts SET occurrence_count = 0",
    )
    for statement in statements:
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
                connection.commit()
            connection.rollback()
        finally:
            connection.close()


def test_cli_reconstructs_json_from_sql_without_runtime_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "rodex.sqlite3"
    created = create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    _publish(
        database, tmp_path, projection=_projection((_turn("turn-exact", total_tokens=42),))
    )
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda _name: (_ for _ in ()).throw(AssertionError("must not resolve tools")),
    )

    assert run(["_stats", created.cool_name, "--json"], database_path=database) == 0
    aggregate = json.loads(capsys.readouterr().out)
    assert (
        aggregate["statistics"]["must_have_basic_stats"]["token_usage"]["total_tokens"]
        == _base_projection().total_tokens
    )
    assert (
        run(
            ["_stats", created.cool_name, "--turn", "turn-exact", "--json"],
            database_path=database,
        )
        == 0
    )
    exact = json.loads(capsys.readouterr().out)
    assert exact["statistics"]["must_have_basic_stats"]["token_usage"]["total_tokens"] == 42
    with pytest.raises(RodexLaunchError, match="not present in the latest"):
        run(["_stats", created.cool_name, "--turn", "missing"], database_path=database)


def test_no_raw_or_redundant_json_is_persisted(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    _publish(database, tmp_path, projection=_projection((_turn("private"),)))
    database_bytes = database.read_bytes()
    assert b"must-not-persist" not in database_bytes
    assert b'"must_have_basic_stats"' not in database_bytes
    assert b'"recommended_insight_stats"' not in database_bytes


def test_historical_source_cannot_move_to_another_lineage(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
    )
    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        database,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
    )
    with pytest.raises(RodexSessionError, match="statistics lineage"):
        create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    assert [
        item.codex_session_id for item in list_rodex_session_statistics_sources(1, database)
    ] == [CODEX_SESSION_ID, REPLACEMENT_CODEX_SESSION_ID]


def test_unregistered_codex_session_id_becomes_persisted_identity(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    candidate = generate_an_unregistered_rodex_session_id_candidate(database)
    created = create_a_rodex_session(
        database,
        rodex_session_id=candidate,
        codex_session_id=CODEX_SESSION_ID,
    )
    assert created.rodex_session_id == candidate
