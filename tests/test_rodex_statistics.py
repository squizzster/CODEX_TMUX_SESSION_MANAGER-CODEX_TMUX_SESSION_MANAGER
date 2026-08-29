from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest
from test_statistics_projection import _snapshot as analyzer_snapshot

import rodex_registry.statistics as statistics_module
from rodex.cli import RodexLaunchError, run
from rodex_registry import (
    COLLABORATION_MODEL_TOOL_NAMES,
    RodexRuntimeId,
    RodexSessionCodexThreadObservation,
    RodexSessionError,
    RodexSessionStatisticsConflictError,
    RodexSessionTurnStatisticsAmbiguousError,
    SessionStatisticsProjection,
    StatisticsNamedCount,
    StatisticsProjectionError,
    TurnStatisticsProjection,
    create_a_rodex_session,
    generate_an_unregistered_rodex_session_id_candidate,
    list_rodex_session_codex_threads,
    parse_session_statistics_snapshot,
    publish_rodex_session_statistics,
    read_rodex_analytics_checkpoint,
    read_rodex_session_codex_thread_summaries,
    read_rodex_session_statistics,
    read_rodex_session_turn_statistics,
    record_a_rodex_session_runtime_resume,
    record_rodex_session_analytics_worker_health,
    session_statistics_as_dict,
    split_codex_turn_id_into_signed_bigints,
    turn_statistics_as_dict,
)

CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
REPLACEMENT_CODEX_SESSION_ID = uuid.UUID(int=CODEX_SESSION_ID.int + 1)
TEST_TURN_NAMESPACE = uuid.UUID("00000000-0000-7000-8000-000000000000")


def _test_turn_id(label: str) -> str:
    try:
        return str(uuid.UUID(label))
    except ValueError:
        return str(uuid.uuid5(TEST_TURN_NAMESPACE, label))


def _columns(database: Path, table: str) -> list[str]:
    with sqlite3.connect(database) as connection:
        return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


def _index_columns(database: Path, index: str) -> list[str]:
    with sqlite3.connect(database) as connection:
        return [str(row[2]) for row in connection.execute(f"PRAGMA index_info({index})")]


def _observation(root: Path, codex_session_id: uuid.UUID, marker: str = "a"):
    path = (root / f"rollout-{codex_session_id}.jsonl").resolve()
    content = marker.encode()
    return RodexSessionCodexThreadObservation(
        codex_thread_id=codex_session_id,
        source_kind="root",
        parent_codex_thread_id=None,
        thread_depth=0,
        agent_path=None,
        agent_nickname=None,
        subagent_history_start_ordinal=None,
        spawning_codex_turn_id=None,
        first_linked_at_utc="2026-08-16T12:00:00Z",
        rollout_file_path=path,
        analyzed_size_bytes=len(content),
        analyzed_mtime_ns=123,
        analyzed_prefix_sha256=hashlib.sha256(content).hexdigest(),
        verified_at_utc="2026-08-16T12:00:00Z",
    )


def _child_observation(root: Path, child_thread_id: uuid.UUID):
    path = (root / f"rollout-{child_thread_id}.jsonl").resolve()
    content = b"child"
    return RodexSessionCodexThreadObservation(
        codex_thread_id=child_thread_id,
        source_kind="subagent",
        parent_codex_thread_id=CODEX_SESSION_ID,
        thread_depth=1,
        agent_path="/root/review",
        agent_nickname="Curie",
        subagent_history_start_ordinal=12,
        spawning_codex_turn_id=_test_turn_id("root"),
        first_linked_at_utc="2026-08-16T12:01:00Z",
        rollout_file_path=path,
        analyzed_size_bytes=len(content),
        analyzed_mtime_ns=456,
        analyzed_prefix_sha256=hashlib.sha256(content).hexdigest(),
        verified_at_utc="2026-08-16T12:01:01Z",
        history_inheritance_kind="inherited",
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
        codex_thread_id=codex_session_id,
        codex_turn_id=_test_turn_id(turn_id),
        outcome=outcome,
        terminal_at_utc=terminal_at,
        total_tokens=total_tokens,
    )


def _projection(
    turns: tuple[TurnStatisticsProjection, ...] | None = None,
) -> SessionStatisticsProjection:
    base = _base_projection()
    selected_input = base.turn_statistics if turns is None else turns
    selected = tuple(_canonical_turn_collaboration(turn) for turn in selected_input)
    completed = sum(turn.outcome == "completed" for turn in selected)
    aborted = sum(turn.outcome == "aborted" for turn in selected)
    open_turns = sum(turn.outcome == "open" for turn in selected)
    workspace_counts = Counter(
        turn.workspace_digest for turn in selected if turn.workspace_digest is not None
    )
    workspace_turns = sum(workspace_counts.values())
    distinct_workspaces = len(workspace_counts)
    hour_turns = sum(turn.local_start_hour is not None for turn in selected)
    lookup_counts = (
        *(
            StatisticsNamedCount("model", name, count)
            for name, count in sorted(
                Counter(turn.model for turn in selected if turn.model is not None).items()
            )
        ),
        *(
            StatisticsNamedCount("reasoning_effort", name, count)
            for name, count in sorted(
                Counter(
                    turn.reasoning_effort
                    for turn in selected
                    if turn.reasoning_effort is not None
                ).items()
            )
        ),
    )
    model_tool_counts = Counter(
        item.count_name
        for turn in selected
        for item in turn.named_counts
        if item.count_kind == "model_tool"
        for _occurrence in range(item.occurrence_count)
    )
    canonical_model_tool_counts = tuple(
        StatisticsNamedCount("model_tool", name, count)
        for name, count in sorted(model_tool_counts.items())
    )
    collaboration_tool_counts = tuple(
        StatisticsNamedCount("collaboration_tool", name, count)
        for name, count in sorted(model_tool_counts.items())
        if name in COLLABORATION_MODEL_TOOL_NAMES
    )
    return replace(
        base,
        turns_started_count=len(selected),
        turns_completed_count=completed,
        turns_aborted_count=aborted,
        turns_open_count=open_turns,
        typical_turns_count=len(selected),
        hands_on_turn_count=sum(turn.hands_on for turn in selected),
        distinct_workspaces_count=distinct_workspaces,
        workspace_tagged_turn_count=workspace_turns,
        turns_in_busiest_workspace_count=max(workspace_counts.values(), default=0),
        turns_with_local_hour_count=hour_turns,
        busiest_local_hour=(selected[0].local_start_hour if hour_turns else None),
        turns_in_busiest_local_hour_count=hour_turns,
        collaboration_operations_count=sum(
            item.occurrence_count for item in collaboration_tool_counts
        ),
        collaboration_agents_started_count=0,
        named_counts=tuple(
            count
            for count in base.named_counts
            if count.count_kind
            not in {
                "model",
                "reasoning_effort",
                "model_tool",
                "collaboration_tool",
            }
        )
        + lookup_counts
        + canonical_model_tool_counts
        + collaboration_tool_counts,
        turn_statistics=selected,
    )


def _canonical_turn_collaboration(
    turn: TurnStatisticsProjection,
) -> TurnStatisticsProjection:
    collaboration_counts = tuple(
        StatisticsNamedCount("collaboration_tool", item.count_name, item.occurrence_count)
        for item in turn.named_counts
        if item.count_kind == "model_tool"
        and item.count_name in COLLABORATION_MODEL_TOOL_NAMES
    )
    return replace(
        turn,
        collaboration_operations_count=sum(
            item.occurrence_count for item in collaboration_counts
        ),
        collaboration_agents_started_count=0,
        named_counts=tuple(
            item for item in turn.named_counts if item.count_kind != "collaboration_tool"
        )
        + collaboration_counts,
    )


def _publish(
    database: Path,
    source_root: Path,
    *,
    based_on: int | None = None,
    projection: SessionStatisticsProjection | None = None,
    expected_codex_session_id: uuid.UUID = CODEX_SESSION_ID,
    sources: tuple[uuid.UUID, ...] = (CODEX_SESSION_ID,),
    observations: tuple[RodexSessionCodexThreadObservation, ...] | None = None,
    changed_source_thread_ids: frozenset[uuid.UUID] | None = None,
    changed_turn_keys: frozenset[tuple[uuid.UUID, str]] | None = None,
    removed_turn_keys: frozenset[tuple[uuid.UUID, str]] = frozenset(),
):
    supplied_projection = _projection(()) if projection is None else projection
    selected_observations = (
        tuple(
            _observation(source_root, source, str(index))
            for index, source in enumerate(sources)
        )
        if observations is None
        else observations
    )
    return publish_rodex_session_statistics(
        1,
        database,
        expected_current_codex_session_id=expected_codex_session_id,
        based_on_statistics_publication_sequence=based_on,
        statistics_projection_schema_version="rodex-statistics-v4",
        calculated_at_utc="2026-08-16T12:00:00Z",
        coverage_state="complete",
        statistics_projection=replace(
            supplied_projection,
            analyzer_source_count=len(selected_observations),
            history_sessions_count=len(selected_observations),
        ),
        analyzed_sources=selected_observations,
        changed_source_thread_ids=changed_source_thread_ids,
        changed_turn_keys=changed_turn_keys,
        removed_turn_keys=removed_turn_keys,
    )


def test_schema_is_relational_queryable_and_contains_no_json_columns(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)

    table_names = (
        "model_names",
        "reasoning_effort_names",
        "rodex_sessions_statistics",
        "rodex_sessions_statistics_distributions",
        "rodex_sessions_statistics_named_counts",
        "rodex_sessions_statistics_audit_limits",
        "rodex_sessions_codex_threads",
        "rodex_sessions_codex_rollout_sources",
        "rodex_sessions_codex_turns",
        "rodex_sessions_codex_turn_states",
        "rodex_sessions_statistics_turn_metrics",
        "rodex_sessions_subagent_spawns",
        "rodex_sessions_statistics_turn_named_counts",
        "rodex_sessions_analytics_workers",
    )
    all_columns = {table: _columns(database, table) for table in table_names}
    assert not any(
        "json" in column.lower() for columns in all_columns.values() for column in columns
    )
    assert "total_tokens" in all_columns["rodex_sessions_statistics"]
    assert "total_tokens" not in all_columns["rodex_sessions_codex_turns"]
    assert "total_tokens" in all_columns["rodex_sessions_statistics_turn_metrics"]
    assert "statistics_publication_sequence" in all_columns["rodex_sessions_statistics"]
    assert "statistics_revision" not in all_columns["rodex_sessions_statistics"]
    for child_table in table_names[3:10]:
        assert "included_statistics_publication_sequence" not in all_columns[child_table]
        assert "included_statistics_revision" not in all_columns[child_table]
    for statistics_table in (
        "rodex_sessions_statistics",
        "rodex_sessions_codex_turns",
    ):
        assert "collaboration_operations_count" not in all_columns[statistics_table]
        assert "collaboration_agents_started_count" not in all_columns[statistics_table]
    assert all_columns["rodex_sessions_subagent_spawns"] == [
        "id",
        "rodex_sessions_id",
        "subagent_rodex_sessions_codex_threads_id",
        "parent_rodex_sessions_codex_threads_id",
        "spawning_rodex_sessions_codex_turns_id",
        "agent_path",
        "agent_nickname",
        "history_inheritance_kind",
        "inherited_history_start_ordinal",
    ]
    assert all_columns["model_names"] == ["id", "name_of_the_model"]
    assert all_columns["reasoning_effort_names"] == [
        "id",
        "name_of_the_reasoning_effort",
    ]
    assert "model" not in all_columns["rodex_sessions_codex_turns"]
    assert "reasoning_effort" not in all_columns["rodex_sessions_codex_turns"]
    assert "model_names_id" not in all_columns["rodex_sessions_codex_turns"]
    assert "reasoning_effort_names_id" not in all_columns["rodex_sessions_codex_turns"]
    assert "model_names_id" in all_columns["rodex_sessions_codex_turn_states"]
    assert "reasoning_effort_names_id" in all_columns["rodex_sessions_codex_turn_states"]
    assert _index_columns(database, "model_names_name_of_the_model_unique") == [
        "name_of_the_model"
    ]
    assert _index_columns(
        database,
        "reasoning_effort_names_name_of_the_reasoning_effort_unique",
    ) == ["name_of_the_reasoning_effort"]
    with sqlite3.connect(database) as connection:
        lookup_definitions = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name IN ('model_names', 'reasoning_effort_names')"
            )
        }
    assert all(
        "INTEGER PRIMARY KEY AUTOINCREMENT" in sql for sql in lookup_definitions.values()
    )
    assert all_columns["rodex_sessions_statistics_distributions"][2:5] == [
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
    ) == ["rodex_sessions_codex_turns_id", "count_kind", "count_name"]
    assert _index_columns(
        database,
        "rodex_sessions_codex_turns_session_id_unique",
    ) == ["rodex_sessions_id", "id"]
    assert _index_columns(
        database, "rodex_sessions_statistics_turn_named_counts_session_kind"
    ) == ["rodex_sessions_id", "count_kind", "count_name"]


def test_full_projection_round_trips_through_relational_rows(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    projection = _projection((_turn("turn-exact", total_tokens=42),))

    published = _publish(database, tmp_path, projection=projection)
    view = read_rodex_session_statistics(1, database)
    exact = read_rodex_session_turn_statistics(
        1, _test_turn_id("turn-exact"), database
    ).turn

    assert published.statistics_publication_sequence == 1
    assert view.statistics is not None
    assert session_statistics_as_dict(
        view.statistics.projection
    ) == session_statistics_as_dict(projection)
    assert exact is not None
    assert turn_statistics_as_dict(exact.projection) == turn_statistics_as_dict(
        projection.turn_statistics[0]
    )
    assert view.worker is not None and view.worker.worker_state == "up_to_date"
    assert view.sources[0].verified_at_utc is not None


def test_turn_model_and_effort_use_cached_independent_lookup_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    projection = _projection(
        (
            replace(_turn("turn-a"), model="gpt-a", reasoning_effort="xhigh"),
            replace(_turn("turn-b"), model="gpt-a", reasoning_effort="xhigh"),
            replace(_turn("turn-c"), model=None, reasoning_effort="medium"),
        )
    )
    resolutions: list[tuple[str, str]] = []
    original_resolver = statistics_module.select_or_insert_lookup_id

    def recording_resolver(
        connection: sqlite3.Connection,
        table_name: str,
        lookup_values: dict[str, object],
    ) -> int:
        resolutions.append((table_name, str(next(iter(lookup_values.values())))))
        return original_resolver(connection, table_name, lookup_values)

    monkeypatch.setattr(
        statistics_module,
        "select_or_insert_lookup_id",
        recording_resolver,
    )

    _publish(database, tmp_path, projection=projection)

    assert resolutions == [
        ("model_names", "gpt-a"),
        ("reasoning_effort_names", "xhigh"),
        ("reasoning_effort_names", "medium"),
    ]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id, name_of_the_model FROM model_names"
        ).fetchall() == [(1, "gpt-a")]
        assert connection.execute(
            "SELECT id, name_of_the_reasoning_effort "
            "FROM reasoning_effort_names ORDER BY id"
        ).fetchall() == [(1, "xhigh"), (2, "medium")]
        assert connection.execute(
            "SELECT turns.codex_turn_id_signed_bigint_1, "
            "turns.codex_turn_id_signed_bigint_2, models.name_of_the_model, "
            "efforts.name_of_the_reasoning_effort "
            "FROM rodex_sessions_codex_turns AS turns "
            "JOIN rodex_sessions_codex_turn_states AS states "
            "ON states.rodex_sessions_codex_turns_id = turns.id "
            "LEFT JOIN model_names AS models ON models.id = states.model_names_id "
            "LEFT JOIN reasoning_effort_names AS efforts "
            "ON efforts.id = states.reasoning_effort_names_id "
            "ORDER BY turns.codex_turn_id_signed_bigint_1, "
            "turns.codex_turn_id_signed_bigint_2"
        ).fetchall() == [
            tuple((*split_codex_turn_id_into_signed_bigints(_test_turn_id(label)), *rest))
            for label, rest in sorted(
                (
                    ("turn-a", ("gpt-a", "xhigh")),
                    ("turn-b", ("gpt-a", "xhigh")),
                    ("turn-c", (None, "medium")),
                ),
                key=lambda item: split_codex_turn_id_into_signed_bigints(
                    _test_turn_id(item[0])
                ),
            )
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_statistics_named_counts "
            "WHERE count_kind IN ('model', 'reasoning_effort')"
        ).fetchone() == (0,)
    view = read_rodex_session_statistics(1, database)
    assert view.statistics is not None
    rollups = session_statistics_as_dict(view.statistics.projection)[
        "must_have_basic_stats"
    ]["workspaces_and_models"]
    assert rollups["models"] == {"gpt-a": 2}
    assert rollups["reasoning_efforts"] == {"medium": 1, "xhigh": 2}


def test_canonical_turn_identity_cannot_be_mutated_in_place(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    _publish(database, tmp_path, projection=_projection((_turn("turn-exact"),)))

    with sqlite3.connect(database) as connection:
        for part_number in range(1, 3):
            column_name = f"codex_turn_id_signed_bigint_{part_number}"
            with pytest.raises(sqlite3.IntegrityError, match="turn identity is immutable"):
                connection.execute(
                    f"UPDATE rodex_sessions_codex_turns SET {column_name} = 1.5"
                )


def test_statistics_reader_does_not_coerce_corrupt_source_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("DROP TRIGGER codex_threads_reject_update")
        connection.execute(
            "UPDATE codex_threads SET codex_thread_public_id_signed_bigint_1 = 1.5"
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
            "SELECT SUM(total_tokens) FROM rodex_sessions_statistics_turn_metrics"
        ).fetchone() == (100,)
        assert connection.execute(
            "SELECT states.outcome, SUM(metrics.total_tokens) "
            "FROM rodex_sessions_codex_turns AS turns "
            "JOIN rodex_sessions_codex_turn_states AS states "
            "ON states.rodex_sessions_codex_turns_id = turns.id "
            "JOIN rodex_sessions_statistics_turn_metrics AS metrics "
            "ON metrics.rodex_sessions_codex_turns_id = turns.id "
            "GROUP BY states.outcome ORDER BY states.outcome"
        ).fetchall() == [("aborted", 60), ("completed", 40)]
        tool_total = connection.execute(
            "SELECT SUM(occurrence_count) "
            "FROM rodex_sessions_statistics_turn_named_counts "
            "WHERE count_kind = 'model_tool'"
        ).fetchone()[0]
        assert tool_total == 4


def test_source_id_groups_subagent_lifecycle_and_resource_totals(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    turns = (
        _turn("root", total_tokens=40),
        _turn("child-a", codex_session_id=child_thread_id, total_tokens=60),
        _turn(
            "child-b",
            codex_session_id=child_thread_id,
            total_tokens=20,
            outcome="aborted",
        ),
    )
    canonical_projection = _projection(turns)
    projection = replace(
        canonical_projection,
        analyzer_source_count=2,
        history_sessions_count=2,
        collaboration_agents_started_count=1,
        turn_statistics=(
            replace(
                canonical_projection.turn_statistics[0],
                collaboration_agents_started_count=1,
            ),
            *canonical_projection.turn_statistics[1:],
        ),
    )

    publish_rodex_session_statistics(
        1,
        database,
        expected_current_codex_session_id=CODEX_SESSION_ID,
        based_on_statistics_publication_sequence=None,
        statistics_projection_schema_version="rodex-statistics-v6",
        calculated_at_utc="2026-08-16T12:02:00Z",
        coverage_state="complete",
        statistics_projection=projection,
        analyzed_sources=(
            _observation(tmp_path, CODEX_SESSION_ID),
            _child_observation(tmp_path, child_thread_id),
        ),
    )

    root, child = read_rodex_session_codex_thread_summaries(
        1, database, expected_statistics_publication_sequence=1
    )
    assert child.source.parent_rodex_sessions_codex_threads_id == root.source.id
    assert child.turns_started_count == 2
    assert child.turns_completed_count == 1
    assert child.turns_aborted_count == 1
    assert child.total_tokens == 80
    assert child.web_queries_count == 2
    assert child.source.spawning_codex_turn_id == _test_turn_id("root")
    with sqlite3.connect(database) as connection:
        spawn = connection.execute(
            "SELECT child_id.codex_thread_public_id_signed_bigint_1, "
            "parent_id.codex_thread_public_id_signed_bigint_1, "
            "spawning_turn.codex_turn_id_signed_bigint_1, "
            "spawning_turn.codex_turn_id_signed_bigint_2 "
            "FROM rodex_sessions_subagent_spawns AS spawns "
            "JOIN rodex_sessions_codex_threads AS child "
            "ON child.id = spawns.subagent_rodex_sessions_codex_threads_id "
            "JOIN rodex_sessions_codex_threads AS parent "
            "ON parent.id = spawns.parent_rodex_sessions_codex_threads_id "
            "JOIN codex_threads AS child_id "
            "ON child_id.id = child.codex_threads_id "
            "JOIN codex_threads AS parent_id "
            "ON parent_id.id = parent.codex_threads_id "
            "JOIN rodex_sessions_codex_turns AS spawning_turn "
            "ON spawning_turn.id = "
            "spawns.spawning_rodex_sessions_codex_turns_id"
        ).fetchone()
        assert spawn is not None and spawn[2:] == split_codex_turn_id_into_signed_bigints(
            _test_turn_id("root")
        )
        spawn_id = connection.execute(
            "SELECT id FROM rodex_sessions_subagent_spawns"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="spawn provenance is immutable"):
            connection.execute(
                "UPDATE rodex_sessions_subagent_spawns "
                "SET agent_path = '/root/retargeted' WHERE id = ?",
                (spawn_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="spawn provenance is immutable"):
            connection.execute(
                "DELETE FROM rodex_sessions_subagent_spawns WHERE id = ?", (spawn_id,)
            )
        rollout_source_id = connection.execute(
            "SELECT id FROM rodex_sessions_codex_rollout_sources ORDER BY id LIMIT 1"
        ).fetchone()[0]
        with pytest.raises(
            sqlite3.IntegrityError, match="rollout-source provenance is immutable"
        ):
            connection.execute(
                "UPDATE rodex_sessions_codex_rollout_sources "
                "SET first_observed_at_utc = '2026-08-26T00:00:00Z' WHERE id = ?",
                (rollout_source_id,),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="rollout-source provenance is immutable"
        ):
            connection.execute(
                "DELETE FROM rodex_sessions_codex_rollout_sources WHERE id = ?",
                (rollout_source_id,),
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_statistics_named_counts "
            "WHERE count_kind = 'collaboration_tool'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_statistics_turn_named_counts "
            "WHERE count_kind = 'collaboration_tool'"
        ).fetchone() == (0,)
    with pytest.raises(
        RodexSessionStatisticsConflictError,
        match="omit a registered Codex thread source",
    ):
        publish_rodex_session_statistics(
            1,
            database,
            expected_current_codex_session_id=CODEX_SESSION_ID,
            based_on_statistics_publication_sequence=1,
            statistics_projection_schema_version="rodex-statistics-v6",
            calculated_at_utc="2026-08-16T12:03:00Z",
            coverage_state="complete",
            statistics_projection=replace(
                _projection((_turn("root"),)), analyzer_source_count=1
            ),
            analyzed_sources=(_observation(tmp_path, CODEX_SESSION_ID),),
        )


def test_exact_spawning_turn_is_derived_from_model_tools_and_spawn_relation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    base_turn = _turn("root")
    raw_turn = replace(
        base_turn,
        named_counts=(
            *(
                item
                for item in base_turn.named_counts
                if item.count_kind not in {"model_tool", "collaboration_tool"}
            ),
            StatisticsNamedCount("model_tool", "spawn_agent", 1),
            StatisticsNamedCount("model_tool", "list_agents", 1),
            StatisticsNamedCount("model_tool", "wait_agent", 2),
        ),
    )
    canonical_projection = _projection((raw_turn,))
    projection = replace(
        canonical_projection,
        analyzer_source_count=2,
        history_sessions_count=2,
        collaboration_agents_started_count=1,
        turn_statistics=(
            replace(
                canonical_projection.turn_statistics[0],
                collaboration_agents_started_count=1,
            ),
        ),
    )
    publish_rodex_session_statistics(
        1,
        database,
        expected_current_codex_session_id=CODEX_SESSION_ID,
        based_on_statistics_publication_sequence=None,
        statistics_projection_schema_version="rodex-statistics-v6",
        calculated_at_utc="2026-08-16T12:02:00Z",
        coverage_state="complete",
        statistics_projection=projection,
        analyzed_sources=(
            _observation(tmp_path, CODEX_SESSION_ID),
            _child_observation(tmp_path, child_thread_id),
        ),
    )

    exact = read_rodex_session_turn_statistics(1, _test_turn_id("root"), database)
    assert exact.turn is not None
    assert exact.turn.projection.collaboration_operations_count == 4
    assert exact.turn.projection.collaboration_agents_started_count == 1
    assert {
        item.count_name: item.occurrence_count
        for item in exact.turn.projection.named_counts
        if item.count_kind == "collaboration_tool"
    } == {"list_agents": 1, "spawn_agent": 1, "wait_agent": 2}


def test_delta_publication_preserves_identity_and_replaces_changed_children(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    _publish(database, tmp_path, projection=_projection((_turn("a"), _turn("b"))))
    first_a = read_rodex_session_turn_statistics(1, _test_turn_id("a"), database).turn
    assert first_a is not None

    changed_a = replace(
        _turn("a", total_tokens=99),
        model="gpt-next",
        reasoning_effort="medium",
    )
    second_projection = _projection((changed_a, _turn("c")))
    _publish(
        database,
        tmp_path,
        based_on=1,
        projection=second_projection,
        changed_source_thread_ids=frozenset(),
        changed_turn_keys=frozenset(
            {
                (CODEX_SESSION_ID, _test_turn_id("a")),
                (CODEX_SESSION_ID, _test_turn_id("c")),
            }
        ),
        removed_turn_keys=frozenset({(CODEX_SESSION_ID, _test_turn_id("b"))}),
    )

    second_a = read_rodex_session_turn_statistics(1, _test_turn_id("a"), database).turn
    assert second_a is not None and second_a.id == first_a.id
    assert second_a.turn_public_id == first_a.turn_public_id
    assert second_a.projection.total_tokens == 99
    assert second_a.projection.model == "gpt-next"
    assert second_a.projection.reasoning_effort == "medium"
    assert read_rodex_session_turn_statistics(1, _test_turn_id("b"), database).turn is None
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

    monkeypatch.setattr(statistics_module, "_upsert_analytics_worker", fail_health)
    changed = replace(
        _turn("stable", total_tokens=999),
        model="gpt-new",
        reasoning_effort="ultra",
    )
    with pytest.raises(RuntimeError, match="forced publication failure"):
        _publish(
            database,
            tmp_path,
            based_on=before.statistics_publication_sequence,
            projection=_projection((changed, _turn("new"))),
        )

    current = read_rodex_session_statistics(1, database).statistics
    stable = read_rodex_session_turn_statistics(1, _test_turn_id("stable"), database).turn
    assert current is not None
    assert current.id == before.statistics_id
    assert current.statistics_publication_sequence == before.statistics_publication_sequence
    assert stable is not None and stable.projection.total_tokens == 10
    assert (
        read_rodex_session_turn_statistics(1, _test_turn_id("new"), database).turn is None
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM model_names WHERE name_of_the_model = 'gpt-new'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM reasoning_effort_names "
            "WHERE name_of_the_reasoning_effort = 'ultra'"
        ).fetchone() == (0,)
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
    canonical_projection = _projection(
        (
            _turn("shared", codex_session_id=CODEX_SESSION_ID, total_tokens=10),
            _turn("shared", codex_session_id=REPLACEMENT_CODEX_SESSION_ID, total_tokens=20),
        )
    )
    projection = replace(
        canonical_projection,
        collaboration_agents_started_count=1,
        turn_statistics=(
            replace(
                canonical_projection.turn_statistics[0],
                collaboration_agents_started_count=1,
            ),
            canonical_projection.turn_statistics[1],
        ),
    )
    _publish(
        database,
        tmp_path,
        based_on=1,
        projection=projection,
        observations=(
            _observation(tmp_path, CODEX_SESSION_ID),
            replace(
                _child_observation(tmp_path, REPLACEMENT_CODEX_SESSION_ID),
                spawning_codex_turn_id=_test_turn_id("shared"),
            ),
        ),
    )

    with pytest.raises(RodexSessionTurnStatisticsAmbiguousError):
        read_rodex_session_turn_statistics(1, _test_turn_id("shared"), database)
    exact = read_rodex_session_turn_statistics(
        1,
        _test_turn_id("shared"),
        database,
        codex_thread_id=REPLACEMENT_CODEX_SESSION_ID,
    ).turn
    assert exact is not None and exact.projection.total_tokens == 20


def test_unanalyzed_source_is_an_atomic_conflict(tmp_path: Path) -> None:
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
        runtime_id=RodexRuntimeId.generate(),
    )
    with pytest.raises(RodexSessionStatisticsConflictError, match="outside"):
        _publish(
            database,
            tmp_path,
            projection=_projection((_turn("old", codex_session_id=CODEX_SESSION_ID),)),
            expected_codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
            sources=(REPLACEMENT_CODEX_SESSION_ID,),
        )
    assert read_rodex_session_statistics(1, database).statistics is None


def test_exact_turn_delta_does_not_touch_unchanged_large_history(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    turns = tuple(_turn(f"turn-{index}") for index in range(1_100))
    projection = _projection(turns)
    _publish(database, tmp_path, projection=projection)
    with sqlite3.connect(database) as connection:
        sequence_before = dict(connection.execute("SELECT name, seq FROM sqlite_sequence"))
        connection.executescript(
            "CREATE TABLE turn_write_audit(operation TEXT NOT NULL);"
            "CREATE TRIGGER audit_turn_insert "
            "AFTER INSERT ON rodex_sessions_statistics_turn_metrics BEGIN "
            "INSERT INTO turn_write_audit VALUES ('insert'); END;"
            "CREATE TRIGGER audit_turn_update "
            "AFTER UPDATE ON rodex_sessions_statistics_turn_metrics BEGIN "
            "INSERT INTO turn_write_audit VALUES ('update'); END;"
            "CREATE TRIGGER audit_turn_delete "
            "AFTER DELETE ON rodex_sessions_statistics_turn_metrics BEGIN "
            "INSERT INTO turn_write_audit VALUES ('delete'); END;"
        )
    changed_key = (CODEX_SESSION_ID, _test_turn_id("turn-550"))
    changed_turns = tuple(
        replace(turn, total_tokens=99)
        if turn.codex_turn_id == _test_turn_id("turn-550")
        else turn
        for turn in turns
    )
    complete_changed_projection = _projection(changed_turns)
    changed_projection = replace(
        complete_changed_projection,
        turn_statistics=tuple(
            turn
            for turn in complete_changed_projection.turn_statistics
            if turn.codex_turn_id == _test_turn_id("turn-550")
        ),
    )
    _publish(
        database,
        tmp_path,
        based_on=1,
        projection=changed_projection,
        changed_source_thread_ids=frozenset(),
        changed_turn_keys=frozenset({changed_key}),
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_codex_turns"
        ).fetchone() == (1_100,)
        assert connection.execute("SELECT operation FROM turn_write_audit").fetchall() == [
            ("update",)
        ]
        assert dict(connection.execute("SELECT name, seq FROM sqlite_sequence")) == (
            sequence_before
        )
        assert "rodex_sessions_statistics_session_publication_sequence_unique" not in {
            str(row[1])
            for row in connection.execute("PRAGMA index_list(rodex_sessions_statistics)")
        }


def test_stale_codex_session_id_and_publication_sequence_fences_preserve_rows(
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
    with pytest.raises(
        RodexSessionStatisticsConflictError,
        match="publication sequence changed",
    ):
        _publish(database, tmp_path, based_on=1, projection=_projection((_turn("new"),)))
    current = read_rodex_session_statistics(1, database).statistics
    assert current is not None
    assert current.id == second.statistics_id
    assert current.statistics_publication_sequence == 2

    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        database,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
        runtime_id=RodexRuntimeId.generate(),
    )
    with pytest.raises(RodexSessionStatisticsConflictError, match="Codex session ID"):
        _publish(database, tmp_path, based_on=first.statistics_publication_sequence)


def test_health_is_separate_and_preserves_last_good_statistics(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    snapshot = _publish(database, tmp_path)
    health = record_rodex_session_analytics_worker_health(
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
    assert view.statistics is not None
    assert view.statistics.id == snapshot.statistics_id
    assert view.statistics.statistics_publication_sequence == 1
    assert view.worker == health


def test_analytics_hot_writes_do_not_reinitialise_the_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)

    monkeypatch.delattr(
        statistics_module,
        "initialise_rodex_database",
        raising=False,
    )

    published = _publish(database, tmp_path)
    health = record_rodex_session_analytics_worker_health(
        1,
        database,
        expected_current_codex_session_id=CODEX_SESSION_ID,
        worker_state="degraded",
        diagnostic_code="analytics_io_error",
        last_attempted_at_utc="2026-08-16T12:01:00Z",
        consecutive_failures=1,
        next_retry_at_utc="2026-08-16T12:01:02Z",
    )

    assert published.statistics_publication_sequence == 1
    assert health.worker_state == "degraded"


def test_foreign_keys_and_checks_reject_detached_relational_facts(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    _publish(database, tmp_path, projection=_projection((_turn("turn-a"),)))

    statements = (
        "UPDATE rodex_sessions_statistics_turn_metrics "
        "SET cached_input_tokens = input_tokens + 1",
        "UPDATE rodex_sessions_codex_turn_states SET model_names_id = 999",
        "UPDATE rodex_sessions_codex_turn_states SET reasoning_effort_names_id = 999",
        "UPDATE rodex_sessions_statistics_named_counts SET occurrence_count = 0",
        "INSERT INTO model_names (name_of_the_model) VALUES ('   ')",
        "INSERT INTO reasoning_effort_names (name_of_the_reasoning_effort) VALUES ('')",
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
    assert aggregate["statistics_publication_sequence"] == 1
    assert "statistics_revision" not in aggregate
    assert (
        aggregate["statistics"]["must_have_basic_stats"]["token_usage"]["total_tokens"]
        == _base_projection().total_tokens
    )
    assert aggregate["statistics"]["must_have_basic_stats"]["workspaces_and_models"] == {
        "distinct_workspaces": 1,
        "models": {"gpt-test": 1},
        "reasoning_efforts": {"xhigh": 1},
    }
    assert aggregate["registered_thread_count"] == 1
    assert aggregate["analyzed_thread_count"] == 1
    assert aggregate["threads"][0]["codex_thread_id"] == str(CODEX_SESSION_ID)
    assert "rodex_sessions_codex_threads_id" not in aggregate["threads"][0]
    assert aggregate["threads"][0]["source_kind"] == "root"
    assert aggregate["threads"][0]["lifecycle"]["turns_started"] == 1
    assert aggregate["threads"][0]["token_usage"]["total_tokens"] == 42
    assert (
        run(
            [
                "_stats",
                created.cool_name,
                "--turn",
                _test_turn_id("turn-exact"),
                "--json",
            ],
            database_path=database,
        )
        == 0
    )
    exact = json.loads(capsys.readouterr().out)
    assert exact["statistics_publication_sequence"] == 1
    assert "included_statistics_publication_sequence" not in exact["turn"]
    assert exact["turn"]["codex_turn_id"] == _test_turn_id("turn-exact")
    assert exact["turn"]["turn_id"] != exact["turn"]["codex_turn_id"]
    uuid.UUID(exact["turn"]["turn_id"])
    assert not any(
        key.startswith("rodex_sessions") and key.endswith("_id") for key in exact["turn"]
    )
    assert exact["statistics"]["must_have_basic_stats"]["token_usage"]["total_tokens"] == 42
    assert exact["statistics"]["must_have_basic_stats"]["workspace_and_model"] == {
        "workspace_digest": "a" * 64,
        "model": "gpt-test",
        "reasoning_effort": "xhigh",
        "local_start_hour": 13,
    }
    with pytest.raises(RodexLaunchError, match="not present in the latest"):
        run(
            ["_stats", created.cool_name, "--turn", _test_turn_id("missing")],
            database_path=database,
        )
    with pytest.raises(RodexLaunchError, match="valid Codex turn ID"):
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
        runtime_id=RodexRuntimeId.generate(),
    )
    with pytest.raises(RodexSessionError, match="already belongs"):
        create_a_rodex_session(database, codex_session_id=CODEX_SESSION_ID)
    assert [
        item.codex_thread_id for item in list_rodex_session_codex_threads(1, database)
    ] == [REPLACEMENT_CODEX_SESSION_ID]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_codex_threads WHERE rodex_sessions_id = 1"
        ).fetchone() == (2,)
    checkpoint = read_rodex_analytics_checkpoint(
        1,
        database,
        expected_current_codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
    )
    assert [source.codex_thread_id for source in checkpoint.sources] == [
        REPLACEMENT_CODEX_SESSION_ID
    ]


def test_unregistered_codex_session_id_becomes_persisted_identity(tmp_path: Path) -> None:
    database = tmp_path / "rodex.sqlite3"
    candidate = generate_an_unregistered_rodex_session_id_candidate(database)
    created = create_a_rodex_session(
        database,
        rodex_session_id=candidate,
        codex_session_id=CODEX_SESSION_ID,
    )
    assert created.rodex_session_id == candidate
