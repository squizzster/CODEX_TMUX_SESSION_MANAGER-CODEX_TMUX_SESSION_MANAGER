from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import NoReturn

import pytest

import rodex.exact_turn_mutation as exact_turn_mutation_module
from rodex.analytics import AnalyticsRolloutWorker
from rodex.analytics_analyzer import AnalyticsAnalyzerSource, RodexAnalyticsError
from rodex.analytics_scheduler import AnalyticsDirtyBatch
from rodex.live_runtime import session_transition_lock
from rodex.process_contracts import AnalyticsWorkerConfig
from rodex.session_commands import execute_session_command
from rodex_registry import (
    RodexAnalyticsRegistry,
    RodexRuntimeId,
    RodexSessionId,
    create_a_rodex_session,
    lookup_rodex_registry_id,
    lookup_rodex_session_names,
    open_a_user_defined_cool_name_assignment,
    read_rodex_session_statistics,
)

CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
RODEX_SESSION_ID = RodexSessionId.parse("1234567890abcdef")
RUNTIME_ID = RodexRuntimeId.parse("0000000000000001")


class PermanentlyFailingAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.bytes_analyzed = 0

    def analyze_rollouts(
        self,
        sources: list[AnalyticsAnalyzerSource],
        _user_id: str,
    ) -> NoReturn:
        self.calls += 1
        self.bytes_analyzed += sum(len(source.analyzer_content) for source in sources)
        raise RodexAnalyticsError("permanent analyzer schema mismatch")

    def accept_batch(self) -> None:
        raise AssertionError("a permanently failed batch cannot be accepted")


def _analytics_fixture(tmp_path: Path) -> tuple[AnalyticsWorkerConfig, Path]:
    database = tmp_path / "rodex.sqlite3"
    sessions_root = tmp_path / "sessions"
    rollout = (
        sessions_root / "2026" / "08" / "29" / f"rollout-round1-{CODEX_SESSION_ID}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    records = [
        {
            "timestamp": "2026-08-29T12:00:00Z",
            "type": "session_meta",
            "payload": {
                "session_id": str(CODEX_SESSION_ID),
                "id": str(CODEX_SESSION_ID),
                "timestamp": "2026-08-29T12:00:00Z",
                "thread_source": "user",
            },
        },
        {
            "timestamp": "2026-08-29T12:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
    ]
    rollout.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    session = create_a_rodex_session(
        database,
        rodex_session_id=RODEX_SESSION_ID,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="round1-runtime",
        runtime_id=RUNTIME_ID,
    )
    registry_id = lookup_rodex_registry_id(database)
    assert registry_id is not None
    return (
        AnalyticsWorkerConfig(
            rodex_database_path=database,
            codex_sessions_root=sessions_root,
            rodex_session_id=RODEX_SESSION_ID,
            rodex_registry_id=registry_id,
            runtime_id=RUNTIME_ID,
            protocol_event_socket_path=tmp_path / "events.sock",
            rodex_sessions_id=session.rodex_sessions_id,
            codex_session_id=CODEX_SESSION_ID,
        ),
        rollout,
    )


def test_round1_permanent_analytics_error_is_parked_until_source_state_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, rollout = _analytics_fixture(tmp_path)
    adapter = PermanentlyFailingAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)
    dirty = AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))
    health_writes: list[dict[str, object]] = []
    real_record_health = RodexAnalyticsRegistry.record_health_transition
    source_reads = 0
    real_source_read = worker._source_reader.read

    def read_source(source: object) -> object:
        nonlocal source_reads
        source_reads += 1
        return real_source_read(source)  # type: ignore[arg-type]

    def record_health(
        registry: RodexAnalyticsRegistry,
        **values: object,
    ) -> object:
        health_writes.append(values)
        return real_record_health(registry, **values)  # type: ignore[arg-type]

    monkeypatch.setattr(RodexAnalyticsRegistry, "record_health_transition", record_health)
    monkeypatch.setattr(worker._source_reader, "read", read_source)

    assert worker.poll_once() == "clean_replay"
    initial_size = rollout.stat().st_size
    for _ in range(6):
        assert worker.poll_once(dirty) == "clean_replay"

    snapshot = read_rodex_session_statistics(
        config.rodex_sessions_id,
        config.rodex_database_path,
    )
    assert adapter.calls == 1
    assert adapter.bytes_analyzed <= initial_size
    assert source_reads == 1
    assert len(health_writes) == 1
    assert snapshot.worker is not None
    assert snapshot.worker.consecutive_failures == 1

    with rollout.open("a", encoding="utf-8") as output:
        output.write('{"type":"event_msg","payload":{"changed":true}}\n')
    changed_size = rollout.stat().st_size

    assert worker.poll_once(dirty) == "clean_replay"
    for _ in range(6):
        assert worker.poll_once(dirty) == "clean_replay"

    changed_snapshot = read_rodex_session_statistics(
        config.rodex_sessions_id,
        config.rodex_database_path,
    )
    assert adapter.calls == 2
    assert adapter.bytes_analyzed <= initial_size + changed_size
    assert source_reads == 2
    assert len(health_writes) == 2
    assert changed_snapshot.worker is not None
    assert changed_snapshot.worker.consecutive_failures == 2


def test_round1_parked_failure_retries_only_failed_health_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _rollout = _analytics_fixture(tmp_path)
    adapter = PermanentlyFailingAdapter()
    clock = [datetime(2026, 8, 29, 12, 0, tzinfo=UTC)]
    monotonic_clock = [0.0]
    worker = AnalyticsRolloutWorker(
        config,
        adapter_factory=lambda: adapter,
        now=lambda: clock[0],
        monotonic=lambda: monotonic_clock[0],
    )
    dirty = AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))
    health_attempts = 0
    successful_health_writes = 0
    source_reads = 0
    real_record_health = RodexAnalyticsRegistry.record_health_transition
    real_source_read = worker._source_reader.read

    def read_source(source: object) -> object:
        nonlocal source_reads
        source_reads += 1
        return real_source_read(source)  # type: ignore[arg-type]

    def fail_once_then_record_health(
        registry: RodexAnalyticsRegistry,
        **values: object,
    ) -> object:
        nonlocal health_attempts, successful_health_writes
        health_attempts += 1
        if health_attempts == 1:
            raise sqlite3.OperationalError("injected health write failure")
        successful_health_writes += 1
        return real_record_health(registry, **values)  # type: ignore[arg-type]

    monkeypatch.setattr(worker._source_reader, "read", read_source)
    monkeypatch.setattr(
        RodexAnalyticsRegistry,
        "record_health_transition",
        fail_once_then_record_health,
    )

    assert worker.poll_once() == "clean_replay"
    for _ in range(10):
        assert worker.poll_once(dirty) == "clean_replay"
    assert health_attempts == 1

    monotonic_clock[0] += 2
    assert worker.poll_once(dirty) == "clean_replay"
    for _ in range(10):
        assert worker.poll_once(dirty) == "clean_replay"

    snapshot = read_rodex_session_statistics(
        config.rodex_sessions_id,
        config.rodex_database_path,
    )
    assert adapter.calls == 1
    assert source_reads == 1
    assert health_attempts == 2
    assert successful_health_writes == 1
    assert snapshot.worker is not None
    assert snapshot.worker.worker_state == "degraded"
    assert snapshot.worker.consecutive_failures == 1


def test_round1_alias_does_not_hold_sqlite_writer_during_external_transition(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="round1-runtime",
    )
    yielded = Event()
    release_transition = Event()
    errors: list[BaseException] = []

    def hold_external_transition() -> None:
        try:
            with open_a_user_defined_cool_name_assignment(
                session.cool_name,
                "round1-alias",
                database,
            ):
                yielded.set()
                assert release_transition.wait(2)
        except BaseException as error:
            errors.append(error)

    alias_thread = Thread(target=hold_external_transition)
    alias_thread.start()
    assert yielded.wait(2)

    contender = sqlite3.connect(database, timeout=0, isolation_level=None)
    writer_acquired = False
    try:
        try:
            contender.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            pass
        else:
            writer_acquired = True
            contender.rollback()
    finally:
        contender.close()
        release_transition.set()
        alias_thread.join(2)

    assert not alias_thread.is_alive()
    assert errors == []
    assert writer_acquired


def test_round1_alias_participates_in_the_open_resume_transition_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="round1-runtime",
    )
    transition_attempted = Event()
    finished = Event()
    errors: list[BaseException] = []
    real_assignment = exact_turn_mutation_module.open_a_user_defined_cool_name_assignment

    def observed_assignment(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        transition_attempted.set()
        return real_assignment(*args, **kwargs)

    monkeypatch.setattr(
        exact_turn_mutation_module,
        "open_a_user_defined_cool_name_assignment",
        observed_assignment,
    )

    class DeadRuntimeLauncher:
        def session_exists(self, _runtime: object) -> bool:
            return False

    def assign_alias() -> None:
        try:
            execute_session_command(
                ["_alias", session.cool_name, "round1-converged"],
                database,
                DeadRuntimeLauncher(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
            )
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    with session_transition_lock(database, session.rodex_sessions_id):
        alias_thread = Thread(target=assign_alias)
        alias_thread.start()
        attempted_while_locked = transition_attempted.wait(0.25)

    alias_thread.join(2)
    assert not alias_thread.is_alive()
    assert finished.is_set()
    assert errors == []
    assert not attempted_while_locked
    names = lookup_rodex_session_names(session.rodex_sessions_id, database)
    assert names is not None and names.display_name == "round1-converged"
