"""Verified repair of v5 publications, including silently persisted wrong scopes."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from test_analytics_lifecycle_consistency import _lifecycle, _record
from test_rodex_agent_trace import THREAD_ID, TURN_A_ID, TURN_B_ID, _content
from test_rodex_analytics import _config, _create, _rollout

import rodex.agent_trace as trace_module
import rodex.analytics as analytics_module
from rodex.analytics import AnalyticsRolloutWorker
from rodex_registry import (
    RodexAnalyticsRegistry,
    audit_rodex_database_integrity,
    read_rodex_agent_trace,
    read_rodex_session_statistics,
    read_rodex_session_turn_statistics,
)
from rodex_registry.normalization_schema import NORMALIZATION_CHANGES_TABLE, NORMALIZATION_TRANSITIONS_TABLE
from rodex_sql import open_rodex_read_transaction, open_rodex_transaction


def _legacy_normalizer(normalize):
    def legacy(sources, **kwargs):
        publication = normalize(sources, **kwargs)
        identities = {}
        for source in sources:
            thread_id, content = source[:2]
            active = None
            for ordinal, line in enumerate(content.splitlines()):
                record = json.loads(line)
                payload = record.get("payload", {})
                explicit = (
                    payload.get("internal_chat_message_metadata_passthrough", {})
                    if record.get("type") == "response_item"
                    else payload
                )
                active = explicit.get("turn_id") or active
                identities[(thread_id, record.get("ordinal", ordinal))] = active
                if payload.get("type") in {"task_complete", "turn_aborted"}:
                    active = None
        return replace(
            publication,
            events=tuple(
                replace(event, codex_turn_id=identities[(event.codex_thread_id, event.source_record_ordinal)])
                for event in publication.events
            ),
        )

    return legacy


def _wrong_scope_records():
    return [
        _record("session_meta", {"id": str(THREAD_ID), "session_id": str(THREAD_ID)}),
        _lifecycle("task_started", TURN_A_ID, 1),
        _record("response_item", {"type": "message", "id": "user-a", "role": "user", "content": "old"}, 2),
        _lifecycle("task_started", TURN_B_ID, 3),
        _lifecycle("agent_message", TURN_A_ID, 4),
        _record("response_item", {"type": "message", "id": "user-b", "role": "user", "content": "new"}, 5),
        _record(
            "response_item", {"type": "function_call", "call_id": "call-b", "id": "item-b", "name": "exec_command"}, 6
        ),
        _record("response_item", {"type": "function_call_output", "call_id": "call-b", "output": "done"}, 7),
        _lifecycle("task_complete", TURN_B_ID, 8),
        _lifecycle("turn_aborted", TURN_B_ID, 9),
    ]


def _publish_legacy(config, monkeypatch):
    with monkeypatch.context() as old:
        old.setattr(trace_module, "AGENT_TRACE_SCHEMA_VERSION", "rodex-agent-trace-v5")
        old.setattr(analytics_module, "AGENT_TRACE_SCHEMA_VERSION", "rodex-agent-trace-v5")
        old.setattr(analytics_module, "STATISTICS_PROJECTION_SCHEMA_VERSION", "rodex-statistics-v10")
        old.setattr(trace_module, "normalize_rollout_trace", _legacy_normalizer(trace_module.normalize_rollout_trace))
        assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"
    # Reproduce B06's original later-observation overwrite without changing source
    # coordinates or any of the immutable typed facts.
    with open_rodex_transaction(config.rodex_database_path) as connection:
        connection.execute(
            "UPDATE rodex_sessions_codex_turn_states SET outcome = 'aborted' WHERE terminal_at_utc IS NOT NULL"
        )


def _stored_identity(database):
    with open_rodex_read_transaction(database) as connection:
        return {
            "events": connection.execute(
                "SELECT id, trace_event_public_id_signed_bigint_1, trace_event_public_id_signed_bigint_2, "
                "rodex_sessions_codex_threads_id, source_record_ordinal, derived_event_ordinal, "
                "event_kind, event_time_utc, detail_sha256, first_trace_publication_sequence "
                "FROM rodex_sessions_agent_trace_events ORDER BY id"
            ).fetchall(),
            "turns": connection.execute("SELECT * FROM rodex_sessions_codex_turns ORDER BY id").fetchall(),
            "items": connection.execute(
                "SELECT id, item_public_id_signed_bigint_1, item_public_id_signed_bigint_2 "
                "FROM rodex_sessions_codex_items ORDER BY id"
            ).fetchall(),
            "calls": connection.execute(
                "SELECT id, tool_call_public_id_signed_bigint_1, tool_call_public_id_signed_bigint_2 "
                "FROM rodex_sessions_codex_tool_calls ORDER BY id"
            ).fetchall(),
        }


def _transition_count(database):
    with open_rodex_read_transaction(database) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {NORMALIZATION_TRANSITIONS_TABLE}").fetchone()[0]


@pytest.mark.parametrize("lost_ack", [False, True])
@pytest.mark.parametrize("append_before_repair", [False, True])
def test_generation_rebuild_repairs_scopes_aliases_outcomes_and_preserves_identities(
    tmp_path: Path, monkeypatch, lost_ack, append_before_repair
):
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, THREAD_ID)
    rollout.write_bytes(_content(*_wrong_scope_records()))
    _create(config)
    _publish_legacy(config, monkeypatch)
    before = _stored_identity(config.rodex_database_path)
    assert read_rodex_agent_trace(1, config.rodex_database_path).events[5]["codex_turn_id"] == TURN_A_ID
    if append_before_repair:
        with rollout.open("ab") as output:
            output.write(
                _content(
                    _record(
                        "response_item",
                        {
                            "type": "function_call_output",
                            "call_id": "call-b",
                            "output": "later",
                            "internal_chat_message_metadata_passthrough": {"turn_id": TURN_B_ID},
                        },
                        10,
                    )
                )
            )
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    if lost_ack:
        original = RodexAnalyticsRegistry.publish

        def lose_ack(registry, publication):
            original(registry, publication)
            raise OSError("lost commit acknowledgement")

        with monkeypatch.context() as transient:
            transient.setattr(RodexAnalyticsRegistry, "publish", lose_ack)
            assert worker.poll_once() == "clean_replay"
        clock[0] = worker.recovery_retry_at
    assert worker.poll_once() == "up_to_date"
    snapshot = read_rodex_agent_trace(1, config.rodex_database_path)
    assert snapshot.trace_schema_version == "rodex-agent-trace-v6"
    assert snapshot.trace_publication_sequence == 2
    assert [event["codex_turn_id"] for event in snapshot.events[5:8]] == [TURN_B_ID] * 3
    after = _stored_identity(config.rodex_database_path)
    assert after["events"][: len(before["events"])] == before["events"]
    for identity in ("turns", "items", "calls"):
        assert after[identity] == before[identity]
    stats = read_rodex_session_statistics(1, config.rodex_database_path).statistics
    turn = read_rodex_session_turn_statistics(1, TURN_B_ID, config.rodex_database_path).turn
    assert stats.statistics_projection_schema_version == "rodex-statistics-v11"
    assert turn.outcome == "completed"
    assert turn.terminal_at_utc == "2026-08-16T12:00:08.000000Z"
    assert stats.projection.turns_completed_count == 1
    assert stats.projection.turns_aborted_count == 0
    assert stats.projection.audit_duplicate_terminals_ignored_count == 1
    assert _transition_count(config.rodex_database_path) == 1
    with open_rodex_read_transaction(config.rodex_database_path) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {NORMALIZATION_CHANGES_TABLE}").fetchone()[0] > 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    audit_rodex_database_integrity(config.rodex_database_path)
    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"
    assert read_rodex_agent_trace(1, config.rodex_database_path) == snapshot


@pytest.mark.parametrize("fault", ["scope_repair", "after_rebuild", "changed_prefix", "missing_source"])
def test_rebuild_failure_preserves_v5_and_rolls_back_all_repair_state(tmp_path, monkeypatch, fault):
    import rodex_registry.normalization_rebuild as rebuild_module
    import rodex_registry.statistics as statistics_module

    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, THREAD_ID)
    rollout.write_bytes(_content(*_wrong_scope_records()))
    _create(config)
    _publish_legacy(config, monkeypatch)
    before = read_rodex_agent_trace(1, config.rodex_database_path)
    identities = _stored_identity(config.rodex_database_path)
    stats = read_rodex_session_statistics(1, config.rodex_database_path).statistics
    if fault == "changed_prefix":
        rollout.write_bytes(rollout.read_bytes().replace(b'"new"', b'"bad"'))
    elif fault == "missing_source":
        rollout.unlink()
    else:
        owner, method = (
            (rebuild_module, "_repair_spawn_turns")
            if fault == "scope_repair"
            else (statistics_module, "_sync_session_projection_children")
        )

        def fail(*args, **kwargs):
            raise OSError("injected rebuild interruption")

        monkeypatch.setattr(owner, method, fail)
    result = AnalyticsRolloutWorker(config).poll_once()
    assert result in {"clean_replay", "catching_up"}
    assert read_rodex_agent_trace(1, config.rodex_database_path) == before
    assert _stored_identity(config.rodex_database_path) == identities
    assert read_rodex_session_statistics(1, config.rodex_database_path).statistics == stats
    assert _transition_count(config.rodex_database_path) == 0
    audit_rodex_database_integrity(config.rodex_database_path)


def _remove_extension(database):
    from rodex_registry import normalization_schema

    with open_rodex_transaction(database) as connection:
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND "
            "(name LIKE 'rodex_analytics_normalization_%' OR name LIKE 'rodex_agent_trace_normalization_%')"
        ).fetchall():
            connection.execute(f'DROP TRIGGER "{name}"')
        for table in reversed(tuple(normalization_schema._TABLES)):
            connection.execute(f"DROP TABLE {table}")


def _extension_catalog(database):
    with open_rodex_read_transaction(database) as connection:
        return connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'rodex_analytics_normalization_%' "
            "OR name LIKE 'rodex_agent_trace_normalization_%' ORDER BY type, name"
        ).fetchall()


@pytest.mark.parametrize("rollback", [False, True])
def test_real_pre_extension_catalog_is_installed_atomically(tmp_path, monkeypatch, rollback):
    import rodex_registry.normalization_rebuild as rebuild_module

    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, THREAD_ID)
    rollout.write_bytes(_content(*_wrong_scope_records()))
    _create(config)
    _publish_legacy(config, monkeypatch)
    _remove_extension(config.rodex_database_path)
    assert _extension_catalog(config.rodex_database_path) == []
    audit_rodex_database_integrity(config.rodex_database_path)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    if rollback:
        with monkeypatch.context() as interrupted:

            def fail(*args):
                raise OSError("interruption after extension installation and scope changes")

            interrupted.setattr(rebuild_module, "_repair_spawn_turns", fail)
            assert worker.poll_once() == "clean_replay"
        assert _extension_catalog(config.rodex_database_path) == []
        assert read_rodex_agent_trace(1, config.rodex_database_path).trace_schema_version == "rodex-agent-trace-v5"
        audit_rodex_database_integrity(config.rodex_database_path)
        clock[0] = worker.recovery_retry_at
    assert worker.poll_once() == "up_to_date"
    assert _transition_count(config.rodex_database_path) == 1
    audit_rodex_database_integrity(config.rodex_database_path)
    with (
        open_rodex_transaction(config.rodex_database_path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="downgraded"),
    ):
        connection.execute(
            "UPDATE rodex_sessions_agent_trace_publications SET trace_schema_version = 'rodex-agent-trace-v5'"
        )


@pytest.mark.parametrize("eligible_parent", [False, True])
@pytest.mark.parametrize("inherited_child", [False, True])
def test_rebuild_repairs_spawn_and_request_ancestry_without_losing_canonical_identity(
    tmp_path, monkeypatch, eligible_parent, inherited_child
):
    from test_rodex_agent_trace import CHILD_THREAD_ID
    from test_rodex_analytics import _subagent_rollout

    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, THREAD_ID)
    records = _wrong_scope_records()
    records[4], records[5] = records[5], records[4]
    records[4]["payload"]["role"] = "user" if eligible_parent else "assistant"
    records[6]["payload"]["name"] = "collaboration.spawn_agent"
    records.insert(
        8,
        _record(
            "event_msg",
            {
                "type": "item_completed",
                "item": {
                    "type": "SubAgentActivity",
                    "id": "call-b",
                    "kind": "started",
                    "agent_thread_id": str(CHILD_THREAD_ID),
                    "agent_path": "/root/review",
                },
            },
            8,
        ),
    )
    rollout.write_bytes(_content(*records))
    child_path = _subagent_rollout(
        config.codex_sessions_root,
        THREAD_ID,
        CHILD_THREAD_ID,
        linked_at_utc="2026-08-16T12:00:08Z",
        inherited_history=inherited_child,
    )
    child_records = [json.loads(line) for line in child_path.read_bytes().splitlines()]
    # Explicit physical coordinates after an inherited prefix must survive rebuild.
    child_records[-1] = {**_lifecycle("task_started", TURN_A_ID, 10), "ordinal": child_records[-1]["ordinal"]}
    child_path.write_bytes(_content(*child_records))
    _create(config)
    _publish_legacy(config, monkeypatch)
    with open_rodex_read_transaction(config.rodex_database_path) as connection:
        before_request = connection.execute("SELECT * FROM rodex_sessions_agent_requests").fetchone()
        assert before_request is not None
        before_parent = before_request[5]
    before_identity = _stored_identity(config.rodex_database_path)
    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"
    assert _stored_identity(config.rodex_database_path) == before_identity
    with open_rodex_read_transaction(config.rodex_database_path) as connection:
        after_request = connection.execute("SELECT * FROM rodex_sessions_agent_requests").fetchone()
        if eligible_parent:
            assert after_request is not None
            assert after_request[:4] == before_request[:4]
            assert after_request[5] != before_parent
        else:
            assert after_request is None
            archived_identity = connection.execute(
                f"SELECT previous_integer FROM {NORMALIZATION_CHANGES_TABLE} "
                "WHERE relation_name = 'rodex_sessions_agent_requests' AND column_name = 'id'"
            ).fetchall()
            assert archived_identity == [(before_request[0],)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        spawning_turn = connection.execute(
            "SELECT turns.codex_turn_id_signed_bigint_2 FROM rodex_sessions_subagent_spawns AS spawns "
            "JOIN rodex_sessions_codex_turns AS turns ON turns.id = spawns.spawning_rodex_sessions_codex_turns_id"
        ).fetchone()[0]
        assert spawning_turn & ((1 << 64) - 1) == int(TURN_B_ID.replace("-", "")[-16:], 16)
    audit_rodex_database_integrity(config.rodex_database_path)


def test_historical_roots_and_children_rebuild_without_entering_current_aggregate(tmp_path, monkeypatch):
    from test_rodex_agent_trace import CHILD_THREAD_ID, REPLACEMENT_THREAD_ID
    from test_rodex_analytics import _subagent_rollout

    from rodex.analytics_scheduler import AnalyticsDirtyBatch
    from rodex_registry import RodexRuntimeId, record_a_rodex_session_runtime_resume

    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, THREAD_ID)
    records = _wrong_scope_records()
    records.insert(
        8,
        _record(
            "event_msg", {"type": "sub_agent_activity", "kind": "started", "agent_thread_id": str(CHILD_THREAD_ID)}, 8
        ),
    )
    rollout.write_bytes(_content(*records))
    _subagent_rollout(
        config.codex_sessions_root,
        THREAD_ID,
        CHILD_THREAD_ID,
        linked_at_utc="2026-08-16T12:00:08Z",
        inherited_history=False,
    )
    _create(config)
    _publish_legacy(config, monkeypatch)
    with rollout.open("ab") as output:
        output.write(
            _content(
                _record(
                    "response_item",
                    {
                        "type": "function_call_output",
                        "call_id": "call-b",
                        "output": "historical append",
                        "internal_chat_message_metadata_passthrough": {"turn_id": TURN_B_ID},
                    },
                    11,
                )
            )
        )
        output.write(
            _content(
                _record(
                    "event_msg",
                    {
                        "type": "sub_agent_activity",
                        "kind": "started",
                        "agent_thread_id": "01a00654-f2bc-7a30-834a-a5f886a65f99",
                        "turn_id": TURN_B_ID,
                    },
                    12,
                )
            )
        )
    runtime_id = RodexRuntimeId.generate()
    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        config.rodex_database_path,
        codex_session_id=REPLACEMENT_THREAD_ID,
        runtime_id=runtime_id,
        expected_previous_runtime_id=config.runtime_id,
    )
    new_config = replace(config, runtime_id=runtime_id, codex_session_id=REPLACEMENT_THREAD_ID)
    _rollout(config.codex_sessions_root, REPLACEMENT_THREAD_ID)
    worker = AnalyticsRolloutWorker(new_config)
    assert worker.poll_once() == "up_to_date"
    assert set(worker._verified_sources) == {REPLACEMENT_THREAD_ID}
    assert not worker._pending_resolution_thread_ids
    stats = read_rodex_session_statistics(1, config.rodex_database_path).statistics.projection
    assert stats.analyzer_source_count == stats.turns_started_count == stats.turns_completed_count == 1
    assert stats.collaboration_agents_started_count == 0
    trace = read_rodex_agent_trace(1, config.rodex_database_path)
    assert trace.events[5]["codex_turn_id"] == TURN_B_ID
    with open_rodex_read_transaction(config.rodex_database_path) as connection:
        size = connection.execute(
            "SELECT checkpoints.analyzed_size_bytes "
            "FROM rodex_sessions_analytics_worker_thread_checkpoints AS checkpoints "
            "JOIN rodex_sessions_codex_rollout_sources AS sources "
            "ON sources.id = checkpoints.rodex_sessions_codex_rollout_sources_id WHERE sources.rollout_file_path = ?",
            (str(rollout),),
        ).fetchone()[0]
        assert size == rollout.stat().st_size
    assert worker.poll_once(AnalyticsDirtyBatch(frozenset({REPLACEMENT_THREAD_ID}))) == "awaiting_append"
    assert read_rodex_session_statistics(1, config.rodex_database_path).statistics.projection == stats
    assert AnalyticsRolloutWorker(new_config).poll_once() == "up_to_date"
    assert read_rodex_agent_trace(1, config.rodex_database_path) == trace
    audit_rodex_database_integrity(config.rodex_database_path)


def test_old_generation_parked_worker_recovers_without_source_growth(tmp_path, monkeypatch):
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, THREAD_ID)
    rollout.write_bytes(_content(*_wrong_scope_records()))
    _create(config)
    _publish_legacy(config, monkeypatch)
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: 100.0)
    worker._parked_failure = analytics_module._AnalyticsFailureFingerprint(
        diagnostic_code="analytics_publication_conflict",
        diagnostic_detail="old normalization conflict",
        sources=(),
        normalization_generation="rodex-agent-trace-v5",
    )
    worker._recovery.retry_at = 1000.0
    assert worker.poll_once() == "up_to_date"
    assert worker._parked_failure is None
    assert worker.recovery_retry_at is None
    assert _transition_count(config.rodex_database_path) == 1


def test_rebuild_compare_and_set_race_discards_candidate_then_recovers(tmp_path, monkeypatch):
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, THREAD_ID)
    rollout.write_bytes(_content(*_wrong_scope_records()))
    _create(config)
    _publish_legacy(config, monkeypatch)
    original = RodexAnalyticsRegistry.publish

    def race(registry, publication):
        with open_rodex_transaction(config.rodex_database_path) as connection:
            connection.execute("UPDATE rodex_sessions_agent_trace_publications SET trace_publication_sequence = 2")
        return original(registry, publication)

    worker = AnalyticsRolloutWorker(config)
    with monkeypatch.context() as competing:
        competing.setattr(RodexAnalyticsRegistry, "publish", race)
        assert worker.poll_once() == "clean_replay"
    assert _transition_count(config.rodex_database_path) == 0
    assert worker.poll_once() == "up_to_date"
    assert _transition_count(config.rodex_database_path) == 1
    assert read_rodex_agent_trace(1, config.rodex_database_path).trace_publication_sequence == 3


def test_parked_rebuild_recovers_when_another_worker_advances_the_generation(tmp_path, monkeypatch):
    from rodex_registry import RodexSessionStatisticsConflictError

    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, THREAD_ID)
    rollout.write_bytes(_content(*_wrong_scope_records()))
    _create(config)
    _publish_legacy(config, monkeypatch)
    clock = [100.0]
    worker = AnalyticsRolloutWorker(config, monotonic=lambda: clock[0])
    with monkeypatch.context() as failing:

        def conflict(*args):
            raise RodexSessionStatisticsConflictError("injected old projection conflict")

        failing.setattr(RodexAnalyticsRegistry, "publish", conflict)
        assert worker.poll_once() == "clean_replay"
    clock[0] = worker.recovery_retry_at
    assert worker.poll_once() == "failure_parked"
    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"
    assert worker.poll_once() == "up_to_date"
    assert worker._parked_failure is None
    assert _transition_count(config.rodex_database_path) == 1


def test_old_writer_head_downgrade_rolls_back_its_earlier_statistics_mutations(tmp_path):
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, THREAD_ID)
    _create(config)
    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"
    before = read_rodex_session_statistics(1, config.rodex_database_path).statistics
    with (
        pytest.raises(sqlite3.IntegrityError, match="downgraded"),
        open_rodex_transaction(config.rodex_database_path) as connection,
    ):
        connection.execute("UPDATE rodex_sessions_statistics SET turns_completed_count = 0, turns_aborted_count = 1")
        connection.execute("UPDATE rodex_sessions_codex_turn_states SET outcome = 'aborted'")
        connection.execute(
            "UPDATE rodex_sessions_agent_trace_publications SET trace_schema_version = 'rodex-agent-trace-v5'"
        )
    assert read_rodex_session_statistics(1, config.rodex_database_path).statistics == before
