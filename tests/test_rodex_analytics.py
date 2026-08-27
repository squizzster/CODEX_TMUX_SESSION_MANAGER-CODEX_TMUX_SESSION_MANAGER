from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from test_statistics_projection import _snapshot as analyzer_snapshot

from rodex.analytics import (
    AnalyticsRolloutWorker,
    AnalyticsSubprocessSupervisor,
    _derive_verified_collaboration_projection,
    locate_verified_rollout,
)
from rodex.analytics_analyzer import (
    AnalyticsAnalyzerSource,
    AnalyticsCalculation,
    CodexProtocolAnalyticsAdapter,
    RodexAnalyticsError,
)
from rodex.analytics_scheduler import AnalyticsDirtyBatch
from rodex.process_contracts import AnalyticsWorkerConfig
from rodex_registry import (
    RodexAnalyticsPublication,
    RodexAnalyticsRegistry,
    RodexRegistryId,
    RodexRuntimeId,
    RodexSessionCodexThreadObservation,
    RodexSessionId,
    RodexSessionStatisticsConflictError,
    SessionStatisticsProjection,
    StatisticsNamedCount,
    TurnStatisticsProjection,
    create_a_rodex_session,
    list_rodex_session_codex_threads,
    lookup_rodex_registry_id,
    parse_session_statistics_snapshot,
    read_rodex_agent_trace,
    read_rodex_session_statistics,
    read_rodex_session_turn_statistics,
    record_a_rodex_session_runtime_resume,
)

RODEX_SESSION_ID = RodexSessionId.parse("1234567890abcdef")
CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
REPLACEMENT_CODEX_SESSION_ID = uuid.UUID(int=CODEX_SESSION_ID.int + 1)
REPLACEMENT_RUNTIME_ID = RodexRuntimeId.parse("0000000000000002")
TURN_TEST_ID = "00000000-0000-7000-8000-000000000001"
TURN_NEXT_ID = "00000000-0000-7000-8000-000000000002"
TURN_ONE_ID = "00000000-0000-7000-8000-000000000003"
TURN_SECOND_ID = "00000000-0000-7000-8000-000000000004"
CHILD_TURN_ID = "00000000-0000-7000-8000-000000000005"


class FakeAnalyticsAdapter:
    def __init__(self) -> None:
        self.analyses: list[tuple[tuple[bytes, ...], str]] = []
        self.appended_analyses: list[tuple[bytes, ...]] = []
        self.fail = False
        self.coverage_state = "complete"
        self.on_analyze: Callable[[], None] | None = None
        self.accepted_batches = 0
        self.source_ids: set[uuid.UUID] = set()

    def analyze_rollouts(
        self, sources: list[AnalyticsAnalyzerSource], user_id: str
    ) -> AnalyticsCalculation:
        if self.fail:
            raise OSError("analytics unavailable")
        captured = tuple(source.analyzer_content for source in sources)
        self.source_ids.update(source.codex_thread_id for source in sources)
        self.analyses.append((captured, user_id))
        self.appended_analyses.append(
            tuple(source.appended_analyzer_content for source in sources)
        )
        if self.on_analyze is not None:
            self.on_analyze()
        base = parse_session_statistics_snapshot(analyzer_snapshot())
        return AnalyticsCalculation(
            statistics_projection=replace(
                base,
                analyzer_event_count=len(self.source_ids),
                analyzer_source_count=len(self.source_ids),
                history_records_count=len(self.source_ids),
            ),
            coverage_state=self.coverage_state,
        )

    def accept_batch(self) -> None:
        self.accepted_batches += 1


class FakeWorkerProcess:
    def __init__(self, *, timeout_on_wait: bool = False) -> None:
        self.returncode: int | None = None
        self.timeout_on_wait = timeout_on_wait
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self.exited = Event()

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if not self.timeout_on_wait:
            self.returncode = -15
            self.exited.set()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired("analytics-worker", timeout)
        if self.timeout_on_wait and not self.killed:
            raise subprocess.TimeoutExpired("analytics-worker", timeout)
        assert self.returncode is not None
        return self.returncode

    def exit(self, returncode: int) -> None:
        self.returncode = returncode
        self.exited.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.exited.set()


def _rollout(root: Path, codex_session_id: uuid.UUID) -> Path:
    path = root / "2026" / "08" / "16" / f"rollout-example-{codex_session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-08-16T12:00:00Z",
            "type": "session_meta",
            "payload": {
                "session_id": str(codex_session_id),
                "id": str(codex_session_id),
                "timestamp": "2026-08-16T12:00:00Z",
                "thread_source": "user",
            },
        },
        {
            "timestamp": "2026-08-16T12:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": TURN_TEST_ID},
        },
        {
            "timestamp": "2026-08-16T12:00:02Z",
            "type": "turn_context",
            "payload": {
                "turn_id": TURN_TEST_ID,
                "model": "gpt-test",
                "effort": "xhigh",
            },
        },
        {
            "timestamp": "2026-08-16T12:00:03Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": TURN_TEST_ID},
        },
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


def _subagent_rollout(
    root: Path,
    root_thread_id: uuid.UUID,
    child_thread_id: uuid.UUID,
    *,
    parent_thread_id: uuid.UUID | None = None,
    depth: int = 1,
    linked_at_utc: str = "2026-08-16T12:00:00.500000Z",
    inherited_history: bool = True,
) -> Path:
    direct_parent_thread_id = parent_thread_id or root_thread_id
    path = root / "2026" / "08" / "16" / f"rollout-child-{child_thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    spawn = {
        "parent_thread_id": str(direct_parent_thread_id),
        "depth": depth,
        "agent_path": "/root/review",
        "agent_nickname": "Curie",
    }
    session_payload = {
        "session_id": str(root_thread_id),
        "id": str(child_thread_id),
        "parent_thread_id": str(direct_parent_thread_id),
        "timestamp": linked_at_utc,
        "source": {"subagent": {"thread_spawn": spawn}},
        "thread_source": "subagent",
        "agent_path": "/root/review",
        "agent_nickname": "Curie",
    }
    if inherited_history:
        session_payload.update(
            {
                "forked_from_id": str(direct_parent_thread_id),
                "subagent_history_start_ordinal": 2,
            }
        )
    records = [
        {
            "timestamp": linked_at_utc,
            "ordinal": 0,
            "type": "session_meta",
            "payload": session_payload,
        },
    ]
    records.extend(
        (
            {"ordinal": 1, "type": "event_msg", "payload": {"inherited": True}},
            {"ordinal": 2, "type": "event_msg", "payload": {"inherited": True}},
            {"ordinal": 3, "type": "event_msg", "payload": {"child": True}},
        )
        if inherited_history
        else ({"ordinal": 1, "type": "event_msg", "payload": {"child": True}},)
    )
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


def _config(tmp_path: Path) -> AnalyticsWorkerConfig:
    return AnalyticsWorkerConfig(
        rodex_database_path=tmp_path / "rodex.sqlite3",
        codex_sessions_root=tmp_path / "sessions",
        rodex_session_id=RODEX_SESSION_ID,
        rodex_registry_id=RodexRegistryId.parse("0000000000000001"),
        runtime_id=RodexRuntimeId.parse("0000000000000001"),
        protocol_event_socket_path=tmp_path / "events.sock",
        rodex_sessions_id=1,
        codex_session_id=CODEX_SESSION_ID,
    )


def _analyzer_source(content: bytes) -> AnalyticsAnalyzerSource:
    return AnalyticsAnalyzerSource(
        codex_thread_id=CODEX_SESSION_ID,
        analyzer_content=content,
        appended_analyzer_content=content,
    )


def _create(
    config: AnalyticsWorkerConfig, codex_session_id: uuid.UUID = CODEX_SESSION_ID
) -> None:
    create_a_rodex_session(
        config.rodex_database_path,
        rodex_session_id=RODEX_SESSION_ID,
        codex_session_id=codex_session_id,
        tmux_server_socket_path=config.rodex_database_path.parent / "tmux.sock",
        tmux_session_name="test-runtime",
        runtime_id=config.runtime_id,
    )
    registry_id = lookup_rodex_registry_id(config.rodex_database_path)
    assert registry_id is not None
    object.__setattr__(config, "rodex_registry_id", registry_id)


def _collaboration_source(
    tmp_path: Path,
    thread_id: uuid.UUID,
    *,
    linked_at_utc: str,
    parent_thread_id: uuid.UUID | None = None,
    depth: int = 0,
) -> RodexSessionCodexThreadObservation:
    is_subagent = parent_thread_id is not None
    return RodexSessionCodexThreadObservation(
        codex_thread_id=thread_id,
        source_kind="subagent" if is_subagent else "root",
        parent_codex_thread_id=parent_thread_id,
        thread_depth=depth,
        agent_path=f"/root/agent-{depth}" if is_subagent else None,
        agent_nickname=f"Agent-{depth}" if is_subagent else None,
        subagent_history_start_ordinal=0 if is_subagent else None,
        spawning_codex_turn_id=None,
        first_linked_at_utc=linked_at_utc,
        rollout_file_path=(tmp_path / f"{thread_id}.jsonl").resolve(),
        analyzed_size_bytes=1,
        analyzed_mtime_ns=1,
        analyzed_prefix_sha256="0" * 64,
        verified_at_utc=linked_at_utc,
        history_inheritance_kind="clean" if is_subagent else None,
    )


def _collaboration_turn(
    thread_id: uuid.UUID,
    turn_id: str,
    *,
    started_at_utc: str,
    terminal_at_utc: str | None,
    model_tools: tuple[tuple[str, int], ...] = (),
) -> TurnStatisticsProjection:
    base = parse_session_statistics_snapshot(analyzer_snapshot()).turn_statistics[0]
    legacy_collaboration_count = (StatisticsNamedCount("collaboration_tool", "wait", 2),)
    return replace(
        base,
        codex_thread_id=thread_id,
        codex_turn_id=turn_id,
        started_at_utc=started_at_utc,
        terminal_at_utc=terminal_at_utc,
        outcome="open" if terminal_at_utc is None else "completed",
        collaboration_operations_count=2,
        collaboration_agents_started_count=0,
        named_counts=tuple(
            item
            for item in base.named_counts
            if item.count_kind not in {"model_tool", "collaboration_tool"}
        )
        + tuple(
            StatisticsNamedCount("model_tool", tool_name, count)
            for tool_name, count in model_tools
        )
        + legacy_collaboration_count,
    )


def _collaboration_projection(
    turns: tuple[TurnStatisticsProjection, ...],
) -> SessionStatisticsProjection:
    base = parse_session_statistics_snapshot(analyzer_snapshot())
    model_tools: dict[str, int] = {}
    for turn in turns:
        for item in turn.named_counts:
            if item.count_kind == "model_tool":
                model_tools[item.count_name] = (
                    model_tools.get(item.count_name, 0) + item.occurrence_count
                )
    return replace(
        base,
        named_counts=tuple(
            item
            for item in base.named_counts
            if item.count_kind not in {"model_tool", "collaboration_tool"}
        )
        + tuple(
            StatisticsNamedCount("model_tool", tool_name, count)
            for tool_name, count in sorted(model_tools.items())
        )
        + (StatisticsNamedCount("collaboration_tool", "wait", 2),),
        turn_statistics=turns,
    )


def test_verified_collaboration_replaces_legacy_spawning_turn_counts(
    tmp_path: Path,
) -> None:
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    raw_turn = _collaboration_turn(
        CODEX_SESSION_ID,
        "spawning-turn",
        started_at_utc="2026-08-16T12:00:00Z",
        terminal_at_utc="2026-08-16T12:00:01Z",
        model_tools=(("spawn_agent", 1), ("list_agents", 1), ("wait_agent", 2)),
    )
    verified = _derive_verified_collaboration_projection(
        _collaboration_projection((raw_turn,)),
        analyzed_sources=(
            _collaboration_source(
                tmp_path,
                CODEX_SESSION_ID,
                linked_at_utc="2026-08-16T12:00:00Z",
            ),
            _collaboration_source(
                tmp_path,
                child_thread_id,
                parent_thread_id=CODEX_SESSION_ID,
                depth=1,
                linked_at_utc="2026-08-16T12:00:00.500000Z",
            ),
        ),
    )

    projection = verified.statistics_projection
    assert projection.collaboration_operations_count == 4
    assert projection.collaboration_agents_started_count == 1
    assert projection.turn_statistics[0].collaboration_operations_count == 4
    assert projection.turn_statistics[0].collaboration_agents_started_count == 1
    assert {
        item.count_name: item.occurrence_count
        for item in projection.turn_statistics[0].named_counts
        if item.count_kind == "collaboration_tool"
    } == {"spawn_agent": 1, "list_agents": 1, "wait_agent": 2}
    assert verified.analyzed_sources[1].spawning_codex_turn_id == "spawning-turn"


def test_verified_collaboration_owns_multiple_and_nested_subagents(
    tmp_path: Path,
) -> None:
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    sibling_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 101)
    grandchild_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 102)
    root_turn = _collaboration_turn(
        CODEX_SESSION_ID,
        "root-spawn",
        started_at_utc="2026-08-16T12:00:00Z",
        terminal_at_utc=None,
        model_tools=(("spawn_agent", 2),),
    )
    child_turn = _collaboration_turn(
        child_thread_id,
        "child-spawn",
        started_at_utc="2026-08-16T12:01:00Z",
        terminal_at_utc="2026-08-16T12:02:00Z",
        model_tools=(("spawn_agent", 1),),
    )
    verified = _derive_verified_collaboration_projection(
        _collaboration_projection((root_turn, child_turn)),
        analyzed_sources=(
            _collaboration_source(
                tmp_path,
                CODEX_SESSION_ID,
                linked_at_utc="2026-08-16T12:00:00Z",
            ),
            _collaboration_source(
                tmp_path,
                child_thread_id,
                parent_thread_id=CODEX_SESSION_ID,
                depth=1,
                linked_at_utc="2026-08-16T12:00:01Z",
            ),
            _collaboration_source(
                tmp_path,
                sibling_thread_id,
                parent_thread_id=CODEX_SESSION_ID,
                depth=1,
                linked_at_utc="2026-08-16T12:00:02Z",
            ),
            _collaboration_source(
                tmp_path,
                grandchild_thread_id,
                parent_thread_id=child_thread_id,
                depth=2,
                linked_at_utc="2026-08-16T12:01:30Z",
            ),
        ),
    )

    assert verified.statistics_projection.collaboration_agents_started_count == 3
    assert [
        turn.collaboration_agents_started_count
        for turn in verified.statistics_projection.turn_statistics
    ] == [2, 1]
    assert [source.spawning_codex_turn_id for source in verified.analyzed_sources[1:]] == [
        "root-spawn",
        "root-spawn",
        "child-spawn",
    ]


@pytest.mark.parametrize("terminal_at_utc", ["2026-08-16T12:00:01Z", None])
def test_verified_collaboration_rejects_missing_or_ambiguous_turn_ownership(
    tmp_path: Path,
    terminal_at_utc: str | None,
) -> None:
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    turns = (
        _collaboration_turn(
            CODEX_SESSION_ID,
            "first",
            started_at_utc="2026-08-16T12:00:00Z",
            terminal_at_utc=terminal_at_utc,
            model_tools=(("spawn_agent", 1),),
        ),
    )
    if terminal_at_utc is None:
        turns += (
            _collaboration_turn(
                CODEX_SESSION_ID,
                "overlap",
                started_at_utc="2026-08-16T12:00:00Z",
                terminal_at_utc=None,
            ),
        )
    sources = (
        _collaboration_source(
            tmp_path,
            CODEX_SESSION_ID,
            linked_at_utc="2026-08-16T12:00:00Z",
        ),
        _collaboration_source(
            tmp_path,
            child_thread_id,
            parent_thread_id=CODEX_SESSION_ID,
            depth=1,
            linked_at_utc="2026-08-16T12:00:02Z",
        ),
    )

    with pytest.raises(
        RodexAnalyticsError,
        match="must belong to exactly one direct-parent turn",
    ):
        _derive_verified_collaboration_projection(
            _collaboration_projection(turns), analyzed_sources=sources
        )


def test_verified_collaboration_rejects_session_and_turn_tool_disagreement(
    tmp_path: Path,
) -> None:
    turn = _collaboration_turn(
        CODEX_SESSION_ID,
        "turn",
        started_at_utc="2026-08-16T12:00:00Z",
        terminal_at_utc="2026-08-16T12:00:01Z",
        model_tools=(("wait_agent", 1),),
    )
    projection = _collaboration_projection((turn,))
    projection = replace(
        projection,
        named_counts=tuple(
            item
            for item in projection.named_counts
            if not (item.count_kind == "model_tool" and item.count_name == "wait_agent")
        ),
    )

    with pytest.raises(
        RodexAnalyticsError,
        match="aggregate collaboration tools disagree",
    ):
        _derive_verified_collaboration_projection(
            projection,
            analyzed_sources=(
                _collaboration_source(
                    tmp_path,
                    CODEX_SESSION_ID,
                    linked_at_utc="2026-08-16T12:00:00Z",
                ),
            ),
        )


@pytest.mark.evolutionary_regression
def test_worker_waits_for_unregistered_identity_without_opening_analyzer(
    tmp_path: Path,
) -> None:
    """Current evidence: a pre-registration worker waits without creating storage.

    Deliberately supersede this guard if runtime launch ordering later removes that state.
    """
    config = _config(tmp_path)
    adapters: list[FakeAnalyticsAdapter] = []

    def create_adapter() -> FakeAnalyticsAdapter:
        adapter = FakeAnalyticsAdapter()
        adapters.append(adapter)
        return adapter

    state = AnalyticsRolloutWorker(config, adapter_factory=create_adapter).poll_once()

    assert state == "catching_up"
    assert adapters == []
    assert not config.rodex_database_path.exists()


def test_worker_with_the_wrong_session_id_cannot_publish_for_an_existing_session(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _create(config)
    wrong_config = replace(
        config,
        rodex_session_id=RodexSessionId(RODEX_SESSION_ID.value + 1),
    )
    adapters: list[FakeAnalyticsAdapter] = []

    state = AnalyticsRolloutWorker(
        wrong_config,
        adapter_factory=lambda: adapters.append(FakeAnalyticsAdapter()) or adapters[-1],
    ).poll_once()

    assert state == "clean_replay"
    assert adapters == []
    assert read_rodex_session_statistics(1, config.rodex_database_path).statistics is None


def test_worker_runs_one_startup_reconciliation_then_uses_the_event_scheduler(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _create(config)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    worker = AnalyticsRolloutWorker(config, adapter_factory=FakeAnalyticsAdapter)
    lifecycle: list[str] = []

    class RecordingScheduler:
        def offer_dirty(self, _thread_id: uuid.UUID) -> None:
            lifecycle.append("scheduler-dirty")

        def run(self, reconcile: Callable[[AnalyticsDirtyBatch], object]) -> None:
            lifecycle.append(
                f"reconcile:{reconcile(AnalyticsDirtyBatch(frozenset(), True))}"
            )

        def close(self) -> None:
            lifecycle.append("scheduler-close")

    class RecordingSubscriber:
        def start(self) -> None:
            lifecycle.append("subscriber-start")

        def close(self) -> None:
            lifecycle.append("subscriber-close")

    scheduler = RecordingScheduler()

    def subscriber_factory(path: Path, supplied_scheduler: object) -> RecordingSubscriber:
        assert path == config.protocol_event_socket_path
        assert supplied_scheduler is scheduler
        return RecordingSubscriber()

    worker.run_until_stopped(  # type: ignore[arg-type]
        Event(),
        scheduler=scheduler,
        subscriber_factory=subscriber_factory,  # type: ignore[arg-type]
    )

    assert lifecycle == [
        "subscriber-start",
        "reconcile:up_to_date",
        "subscriber-close",
    ]


def test_worker_backfills_verified_rollout_and_projects_only_aggregates(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)

    assert worker.poll_once() == "up_to_date"

    assert adapter.analyses[0][0] == (rollout.read_bytes(),)
    assert adapter.analyses[0][1].startswith("posix:")
    source = list_rodex_session_codex_threads(1, config.rodex_database_path)[0]
    assert source.codex_thread_id == CODEX_SESSION_ID
    assert source.rollout_file_path == str(rollout.resolve())
    assert source.analyzed_prefix_sha256 is not None
    trace = read_rodex_agent_trace(1, config.rodex_database_path)
    assert trace.coverage_state == "complete"
    assert [event["source_record_ordinal"] for event in trace.events] == [0, 1, 2, 3]
    assert [event["event_kind"] for event in trace.events] == [
        "session_metadata",
        "turn_started",
        "turn_context",
        "turn_completed",
    ]


def test_worker_discovers_subagent_and_removes_inherited_parent_history(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    _subagent_rollout(config.codex_sessions_root, CODEX_SESSION_ID, child_thread_id)
    _create(config)
    adapter = FakeAnalyticsAdapter()

    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)
    worker.observe_protocol_event(
        {
            "method": "thread/started",
            "params": {
                "thread": {
                    "id": str(child_thread_id),
                    "createdAt": "2026-08-16T12:00:00.500000Z",
                }
            },
        }
    )

    assert worker.poll_once() == "up_to_date"

    analyzed = adapter.analyses[0][0]
    assert len(analyzed) == 2
    child_records = [json.loads(line) for line in analyzed[1].splitlines()]
    assert [record.get("ordinal") for record in child_records] == [0, 3]
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.projection.collaboration_agents_started_count == 1
    root, child = sorted(view.sources, key=lambda source: source.thread_depth)
    assert child.codex_thread_id == child_thread_id
    assert child.parent_rodex_sessions_codex_threads_id == root.id
    assert child.agent_path == "/root/review"
    assert child.subagent_history_start_ordinal == 2
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 1
    assert view.worker is not None
    assert view.worker.worker_state == "up_to_date"
    assert view.statistics.projection.audit_privacy
    assert b'"type":"session_meta"' not in config.rodex_database_path.read_bytes()


def test_worker_notifies_the_observer_only_after_durable_trace_publication(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    notifications: list[tuple[Path, int, bool]] = []
    worker = AnalyticsRolloutWorker(
        config,
        adapter_factory=FakeAnalyticsAdapter,
        trace_publication_notifier=lambda path, sequence, caught_up: notifications.append(
            (path, sequence, caught_up)
        ),
    )

    assert worker.poll_once() == "up_to_date"

    trace = read_rodex_agent_trace(1, config.rodex_database_path)
    assert notifications == [
        (config.protocol_event_socket_path, trace.trace_publication_sequence, True)
    ]

    assert worker.poll_once() == "up_to_date"
    assert len(notifications) == 1


def test_cold_worker_bounded_scan_recovers_child_without_uuid_bearing_event(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    _subagent_rollout(config.codex_sessions_root, CODEX_SESSION_ID, child_thread_id)
    unrelated_root = uuid.UUID(int=CODEX_SESSION_ID.int + 200)
    unrelated_child = uuid.UUID(int=CODEX_SESSION_ID.int + 300)
    _subagent_rollout(config.codex_sessions_root, unrelated_root, unrelated_child)
    _create(config)
    worker = AnalyticsRolloutWorker(config, adapter_factory=FakeAnalyticsAdapter)

    assert worker.poll_once() == "up_to_date"

    sources = list_rodex_session_codex_threads(1, config.rodex_database_path)
    assert {source.codex_thread_id for source in sources} == {
        CODEX_SESSION_ID,
        child_thread_id,
    }
    assert worker._session_tree_bootstrap_complete


def test_cold_worker_exactly_follows_a_clean_subagent_activity_target(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    root_rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    with root_rollout.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": "2026-08-16T12:00:04Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "sub_agent_activity",
                        "agent_thread_id": str(child_thread_id),
                        "status": "started",
                        "agent_path": "/root/review",
                    },
                }
            )
            + "\n"
        )
    _subagent_rollout(
        config.codex_sessions_root,
        CODEX_SESSION_ID,
        child_thread_id,
        inherited_history=False,
    )
    _create(config)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)

    assert worker.poll_once() == "up_to_date"

    child_records = [json.loads(line) for line in adapter.analyses[0][0][1].splitlines()]
    assert [record.get("ordinal") for record in child_records] == [0, 1]
    _, child = sorted(
        list_rodex_session_codex_threads(1, config.rodex_database_path),
        key=lambda source: source.thread_depth,
    )
    assert child.codex_thread_id == child_thread_id
    assert child.subagent_history_start_ordinal == 0


def test_cold_restart_recovers_an_unresolved_activity_target_without_scanning(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    root_rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    with root_rollout.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": "2026-08-16T12:00:04Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "sub_agent_activity",
                        "agent_thread_id": str(child_thread_id),
                        "status": "started",
                    },
                }
            )
            + "\n"
        )
    _create(config)
    first_worker = AnalyticsRolloutWorker(config, adapter_factory=FakeAnalyticsAdapter)

    assert first_worker.poll_once() == "pending_append"
    assert first_worker.poll_once(AnalyticsDirtyBatch(frozenset())) == "catching_up"

    _subagent_rollout(
        config.codex_sessions_root,
        CODEX_SESSION_ID,
        child_thread_id,
        inherited_history=False,
    )
    restarted = AnalyticsRolloutWorker(config, adapter_factory=FakeAnalyticsAdapter)

    assert restarted.poll_once() == "up_to_date"
    assert [
        source.codex_thread_id
        for source in list_rodex_session_codex_threads(1, config.rodex_database_path)
    ] == [CODEX_SESSION_ID, child_thread_id]


def test_live_batch_loads_checkpoint_once_and_reads_only_its_exact_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    child_rollout = _subagent_rollout(
        config.codex_sessions_root,
        CODEX_SESSION_ID,
        child_thread_id,
    )
    _create(config)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)
    worker.observe_protocol_event(
        {
            "method": "thread/started",
            "params": {
                "thread": {
                    "id": str(child_thread_id),
                    "createdAt": "2026-08-16T12:00:00.500000Z",
                }
            },
        }
    )
    checkpoint_loads = 0
    original_load_checkpoint = RodexAnalyticsRegistry.load_checkpoint

    def count_checkpoint_load(registry: RodexAnalyticsRegistry) -> object:
        nonlocal checkpoint_loads
        checkpoint_loads += 1
        return original_load_checkpoint(registry)

    monkeypatch.setattr(
        RodexAnalyticsRegistry,
        "load_checkpoint",
        count_checkpoint_load,
    )
    assert worker.poll_once(AnalyticsDirtyBatch(frozenset(), True)) == "up_to_date"

    read_paths: list[Path] = []
    original_read = worker._source_reader.read

    def record_read(source: object) -> object:
        read_paths.append(source.path)  # type: ignore[attr-defined]
        return original_read(source)  # type: ignore[arg-type]

    monkeypatch.setattr(worker._source_reader, "read", record_read)
    addition = b'{"ordinal":4,"type":"event_msg","payload":{"child":true}}\n'
    with child_rollout.open("ab") as output:
        output.write(addition)

    assert (
        worker.poll_once(AnalyticsDirtyBatch(frozenset({child_thread_id}))) == "up_to_date"
    )

    assert checkpoint_loads == 1
    assert read_paths == [child_rollout.resolve()]
    assert adapter.appended_analyses[-1] == (addition,)


def test_mixed_batch_publishes_resolved_source_and_retains_unresolved_identity(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    root_rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)
    assert worker.poll_once() == "up_to_date"
    missing_child_id = uuid.UUID(int=CODEX_SESSION_ID.int + 200)
    worker.observe_protocol_event(
        {
            "method": "thread/started",
            "params": {
                "thread": {
                    "id": str(missing_child_id),
                    "createdAt": "2026-08-16T12:00:01.500000Z",
                }
            },
        }
    )
    root_addition = (
        '{"timestamp":"2026-08-16T12:01:00Z","type":"event_msg",'
        f'"payload":{{"type":"task_started","turn_id":"{TURN_NEXT_ID}"}}}}\n'
    ).encode()
    with root_rollout.open("ab") as output:
        output.write(root_addition)

    state = worker.poll_once(
        AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID, missing_child_id}))
    )

    assert state == "catching_up"
    assert adapter.appended_analyses[-1] == (root_addition,)
    statistics = read_rodex_session_statistics(1, config.rodex_database_path).statistics
    assert statistics is not None
    assert statistics.statistics_publication_sequence == 2
    assert worker.poll_once(AnalyticsDirtyBatch(frozenset())) == "catching_up"
    assert len(adapter.analyses) == 2
    _subagent_rollout(
        config.codex_sessions_root,
        CODEX_SESSION_ID,
        missing_child_id,
    )

    assert (
        worker.poll_once(AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))) == "up_to_date"
    )
    assert missing_child_id in adapter.source_ids


def test_new_child_batch_reads_its_exact_parent_dependency_without_full_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rodex import analytics as analytics_module

    config = _config(tmp_path)
    root_rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    worker = AnalyticsRolloutWorker(config)
    assert worker.poll_once() == "up_to_date"
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    child_rollout = _subagent_rollout(
        config.codex_sessions_root,
        CODEX_SESSION_ID,
        child_thread_id,
        linked_at_utc="2026-08-16T12:00:01.500000Z",
    )
    worker.observe_protocol_event(
        {
            "method": "thread/started",
            "params": {
                "thread": {
                    "id": str(child_thread_id),
                    "createdAt": "2026-08-16T12:00:01.500000Z",
                }
            },
        }
    )
    read_paths: list[Path] = []
    original_read = worker._source_reader.read

    def record_read(source: object) -> object:
        read_paths.append(source.path)  # type: ignore[attr-defined]
        return original_read(source)  # type: ignore[arg-type]

    monkeypatch.setattr(worker._source_reader, "read", record_read)
    assert worker._adapter is not None
    monkeypatch.setattr(
        worker._adapter._analyzer,  # type: ignore[attr-defined]
        "report",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full report called")),
    )
    errors: list[Exception] = []

    def remember_error(error: Exception) -> str:
        errors.append(error)
        return "captured_error"

    monkeypatch.setattr(analytics_module, "_diagnostic_code", remember_error)

    state = worker.poll_once(AnalyticsDirtyBatch(frozenset({child_thread_id})))

    assert state == "up_to_date", repr(errors)

    assert read_paths == [root_rollout.resolve(), child_rollout.resolve()]
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 2
    assert view.statistics.projection.collaboration_agents_started_count == 1
    assert len(view.sources) == 2
    exact = read_rodex_session_turn_statistics(1, TURN_TEST_ID, config.rodex_database_path)
    assert exact.turn is not None
    assert exact.turn.projection.collaboration_agents_started_count == 1


def test_new_historical_child_resolves_against_a_non_latest_resident_parent_turn(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    root_rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    with root_rollout.open("a", encoding="utf-8") as output:
        output.writelines(
            json.dumps(record) + "\n"
            for record in (
                {
                    "timestamp": "2026-08-16T12:01:01Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": TURN_SECOND_ID},
                },
                {
                    "timestamp": "2026-08-16T12:01:02Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": TURN_SECOND_ID,
                        "model": "gpt-test",
                        "effort": "xhigh",
                    },
                },
                {
                    "timestamp": "2026-08-16T12:01:03Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": TURN_SECOND_ID},
                },
            )
        )
    _create(config)
    worker = AnalyticsRolloutWorker(config)
    assert worker.poll_once() == "up_to_date"

    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    _subagent_rollout(
        config.codex_sessions_root,
        CODEX_SESSION_ID,
        child_thread_id,
        linked_at_utc="2026-08-16T12:00:02.500000Z",
    )
    worker.observe_protocol_event(
        {
            "method": "thread/started",
            "params": {
                "thread": {
                    "id": str(child_thread_id),
                    "createdAt": "2026-08-16T12:00:02.500000Z",
                }
            },
        }
    )

    assert (
        worker.poll_once(AnalyticsDirtyBatch(frozenset({child_thread_id}))) == "up_to_date"
    )
    child = next(
        source
        for source in list_rodex_session_codex_threads(1, config.rodex_database_path)
        if source.codex_thread_id == child_thread_id
    )
    assert child.spawning_codex_turn_id == TURN_TEST_ID


def test_same_burst_nested_children_resolve_in_parent_first_topology(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    worker = AnalyticsRolloutWorker(config)
    assert worker.poll_once() == "up_to_date"
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 1000)
    grandchild_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 500)
    assert sorted({child_thread_id, grandchild_thread_id}, key=str) == [
        grandchild_thread_id,
        child_thread_id,
    ]
    child_rollout = _subagent_rollout(
        config.codex_sessions_root,
        CODEX_SESSION_ID,
        child_thread_id,
        linked_at_utc="2026-08-16T12:00:01.500000Z",
    )
    with child_rollout.open("a", encoding="utf-8") as output:
        output.write(
            json.dumps(
                {
                    "timestamp": "2026-08-16T12:00:01.700000Z",
                    "ordinal": 4,
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": CHILD_TURN_ID},
                }
            )
            + "\n"
        )
        output.write(
            json.dumps(
                {
                    "timestamp": "2026-08-16T12:00:02.500000Z",
                    "ordinal": 5,
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": CHILD_TURN_ID},
                }
            )
            + "\n"
        )
    _subagent_rollout(
        config.codex_sessions_root,
        CODEX_SESSION_ID,
        grandchild_thread_id,
        parent_thread_id=child_thread_id,
        depth=2,
        linked_at_utc="2026-08-16T12:00:02Z",
    )
    for thread_id in (child_thread_id, grandchild_thread_id):
        worker.observe_protocol_event(
            {
                "method": "thread/started",
                "params": {
                    "thread": {
                        "id": str(thread_id),
                        "createdAt": "2026-08-16T12:00:02Z",
                    }
                },
            }
        )

    state = worker.poll_once(
        AnalyticsDirtyBatch(frozenset({child_thread_id, grandchild_thread_id}))
    )

    assert state == "up_to_date"
    assert worker._requires_full_reconcile is False
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 2
    assert len(view.sources) == 3


def test_new_topology_is_promoted_only_after_its_source_batch_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    worker = AnalyticsRolloutWorker(config)
    assert worker.poll_once() == "up_to_date"
    child_thread_id = uuid.UUID(int=CODEX_SESSION_ID.int + 100)
    _subagent_rollout(
        config.codex_sessions_root,
        CODEX_SESSION_ID,
        child_thread_id,
        linked_at_utc="2026-08-16T12:00:01.500000Z",
    )
    worker.observe_protocol_event(
        {
            "method": "thread/started",
            "params": {
                "thread": {
                    "id": str(child_thread_id),
                    "createdAt": "2026-08-16T12:00:01.500000Z",
                }
            },
        }
    )
    original_read = worker._source_reader.read

    def fail_new_child_read(source: object) -> object:
        if source.codex_thread_id == child_thread_id:  # type: ignore[attr-defined]
            raise OSError("new child read failed")
        return original_read(source)  # type: ignore[arg-type]

    monkeypatch.setattr(worker._source_reader, "read", fail_new_child_read)

    assert (
        worker.poll_once(AnalyticsDirtyBatch(frozenset({child_thread_id})))
        == "clean_replay"
    )
    assert child_thread_id not in worker._verified_sources
    assert child_thread_id in worker._pending_resolution_thread_ids
    monkeypatch.setattr(worker._source_reader, "read", original_read)

    assert (
        worker.poll_once(AnalyticsDirtyBatch(frozenset({child_thread_id}))) == "up_to_date"
    )
    assert child_thread_id in worker._verified_sources


def test_unchanged_rollout_does_not_recalculate_but_append_does(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)

    assert worker.poll_once() == "up_to_date"
    assert worker.poll_once() == "up_to_date"
    with rollout.open("a", encoding="utf-8") as output:
        output.write(
            '{"timestamp":"2026-08-16T12:00:01Z","type":"event_msg",'
            f'"payload":{{"type":"task_started","turn_id":"{TURN_ONE_ID}"}}}}\n'
        )
    assert worker.poll_once() == "up_to_date"

    assert len(adapter.analyses) == 2
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 2


def test_worker_retains_one_analyzer_and_offers_only_accepted_suffix(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    created: list[FakeAnalyticsAdapter] = []

    def adapter_factory() -> FakeAnalyticsAdapter:
        adapter = FakeAnalyticsAdapter()
        created.append(adapter)
        return adapter

    worker = AnalyticsRolloutWorker(config, adapter_factory=adapter_factory)
    assert worker.poll_once() == "up_to_date"
    addition = b'{"timestamp":"2026-08-16T12:00:04Z","type":"future"}\n'
    with rollout.open("ab") as output:
        output.write(addition)
    assert worker.poll_once() == "up_to_date"

    assert len(created) == 1
    assert created[0].appended_analyses == [(created[0].analyses[0][0][0],), (addition,)]
    assert created[0].accepted_batches == 2


def test_restarted_worker_warms_state_once_then_consumes_only_suffix(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    first_adapter = FakeAnalyticsAdapter()
    assert (
        AnalyticsRolloutWorker(config, adapter_factory=lambda: first_adapter).poll_once()
        == "up_to_date"
    )
    accepted_baseline = rollout.read_bytes()
    restarted_adapter = FakeAnalyticsAdapter()
    restarted = AnalyticsRolloutWorker(config, adapter_factory=lambda: restarted_adapter)

    assert restarted.poll_once() == "up_to_date"
    addition = b'{"timestamp":"2026-08-16T12:00:04Z","type":"future"}\n'
    with rollout.open("ab") as output:
        output.write(addition)
    assert restarted.poll_once() == "up_to_date"

    assert restarted_adapter.appended_analyses == [
        (accepted_baseline,),
        (b"",),
        (addition,),
    ]
    assert restarted_adapter.accepted_batches == 3


def test_cold_restart_after_offline_append_publishes_only_trace_and_turn_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"
    with rollout.open("a", encoding="utf-8") as output:
        output.writelines(
            json.dumps(record) + "\n"
            for record in (
                {
                    "timestamp": "2026-08-16T12:01:01Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": TURN_SECOND_ID},
                },
                {
                    "timestamp": "2026-08-16T12:01:02Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": TURN_SECOND_ID,
                        "model": "gpt-test",
                        "effort": "xhigh",
                    },
                },
                {
                    "timestamp": "2026-08-16T12:01:03Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": TURN_SECOND_ID},
                },
            )
        )
    publications: list[RodexAnalyticsPublication] = []
    original_publish = RodexAnalyticsRegistry.publish

    def remember_publication(
        registry: RodexAnalyticsRegistry,
        publication: RodexAnalyticsPublication,
    ) -> object:
        publications.append(publication)
        return original_publish(registry, publication)

    monkeypatch.setattr(RodexAnalyticsRegistry, "publish", remember_publication)

    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"

    assert len(publications) == 1
    publication = publications[0]
    assert publication.changed_turn_keys == {(CODEX_SESSION_ID, TURN_SECOND_ID)}
    assert [
        turn.codex_turn_id for turn in publication.statistics_projection.turn_statistics
    ] == [TURN_SECOND_ID]
    assert publication.agent_trace_publication is not None
    assert len(publication.agent_trace_publication.events) == 3
    assert {
        event.codex_turn_id for event in publication.agent_trace_publication.events
    } == {TURN_SECOND_ID}


def test_restarted_worker_rebuilds_active_trace_turn_before_suffix(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    lines = rollout.read_text(encoding="utf-8").splitlines(keepends=True)
    rollout.write_text("".join(lines[:-1]), encoding="utf-8")
    _create(config)
    first = AnalyticsRolloutWorker(config, adapter_factory=FakeAnalyticsAdapter)
    assert first.poll_once() == "up_to_date"

    restarted = AnalyticsRolloutWorker(config, adapter_factory=FakeAnalyticsAdapter)
    assert restarted.poll_once() == "up_to_date"
    addition = (
        b'{"timestamp":"2026-08-16T12:00:04Z","type":"event_msg",'
        b'"payload":{"type":"item_completed","item":{"type":"AgentMessage",'
        b'"content":[{"text":"done"}]}}}\n'
    )
    with rollout.open("ab") as output:
        output.write(addition)

    assert restarted.poll_once() == "up_to_date"

    trace = read_rodex_agent_trace(1, config.rodex_database_path)
    assert trace.events[-1]["event_kind"] == "message"
    assert trace.events[-1]["codex_turn_id"] == TURN_TEST_ID


def test_restarted_worker_rejects_rewritten_durable_prefix_before_reanalysis(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    first_adapter = FakeAnalyticsAdapter()
    assert (
        AnalyticsRolloutWorker(config, adapter_factory=lambda: first_adapter).poll_once()
        == "up_to_date"
    )
    original = rollout.read_bytes()
    rewritten = original.replace(b'"model": "gpt-test"', b'"model": "bad-test"')
    assert len(rewritten) == len(original)
    rollout.write_bytes(rewritten + b'{"type":"compacted","payload":{}}\n')
    restarted_adapter = FakeAnalyticsAdapter()

    state = AnalyticsRolloutWorker(
        config, adapter_factory=lambda: restarted_adapter
    ).poll_once()

    assert state == "clean_replay"
    assert restarted_adapter.analyses == []
    statistics = read_rodex_session_statistics(1, config.rodex_database_path).statistics
    assert statistics is not None
    assert statistics.statistics_publication_sequence == 1


@pytest.mark.parametrize(
    "checkpoint_mutation",
    (
        "DELETE FROM rodex_sessions_agent_trace_publications",
        "UPDATE rodex_sessions_agent_trace_publications "
        "SET trace_schema_version = 'incompatible-v0'",
    ),
)
def test_restarted_worker_rejects_non_atomic_or_incompatible_publication_heads(
    tmp_path: Path,
    checkpoint_mutation: str,
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    assert AnalyticsRolloutWorker(
        config, adapter_factory=FakeAnalyticsAdapter
    ).poll_once() == ("up_to_date")
    before = read_rodex_session_statistics(1, config.rodex_database_path)
    assert before.statistics is not None
    with sqlite3.connect(config.rodex_database_path) as connection:
        connection.execute(checkpoint_mutation)
    restarted_adapter = FakeAnalyticsAdapter()

    state = AnalyticsRolloutWorker(
        config,
        adapter_factory=lambda: restarted_adapter,
    ).poll_once()

    assert state == "clean_replay"
    assert restarted_adapter.analyses == []
    after = read_rodex_session_statistics(1, config.rodex_database_path)
    assert after.statistics is not None
    assert (
        after.statistics.statistics_publication_sequence
        == before.statistics.statistics_publication_sequence
    )


def test_dirty_wake_before_append_retries_without_sql_write_churn(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    worker = AnalyticsRolloutWorker(config, adapter_factory=FakeAnalyticsAdapter)
    assert worker.poll_once() == "up_to_date"
    dirty = AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))

    assert worker.poll_once(dirty) == "awaiting_append"
    assert worker.poll_once(AnalyticsDirtyBatch(frozenset())) == "awaiting_append"
    before = read_rodex_session_statistics(1, config.rodex_database_path).statistics
    assert before is not None and before.statistics_publication_sequence == 1

    with rollout.open("ab") as output:
        output.write(b'{"type":"compacted","payload":{}}\n')
    assert worker.poll_once(AnalyticsDirtyBatch(frozenset())) == "up_to_date"
    after = read_rodex_session_statistics(1, config.rodex_database_path).statistics
    assert after is not None and after.statistics_publication_sequence == 2


def test_stale_publication_head_reloads_sql_cursor_before_accepting_later_append(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    leading = AnalyticsRolloutWorker(config, adapter_factory=FakeAnalyticsAdapter)
    stale_adapters: list[FakeAnalyticsAdapter] = []

    def stale_adapter_factory() -> FakeAnalyticsAdapter:
        adapter = FakeAnalyticsAdapter()
        stale_adapters.append(adapter)
        return adapter

    stale = AnalyticsRolloutWorker(config, adapter_factory=stale_adapter_factory)
    assert leading.poll_once() == "up_to_date"
    assert stale.poll_once() == "up_to_date"
    assert CODEX_SESSION_ID in stale._verified_sources
    accepted_baseline = rollout.read_bytes()
    leader_append = b'{"type":"leader-append","payload":{}}\n'
    with rollout.open("ab") as output:
        output.write(leader_append)

    assert leading.poll_once() == "up_to_date"
    assert stale.poll_once() == "clean_replay"
    assert stale._verified_sources == {}
    later_append = b'{"type":"later-append","payload":{}}\n'
    with rollout.open("ab") as output:
        output.write(later_append)
    assert stale.poll_once() == "up_to_date"

    statistics = read_rodex_session_statistics(1, config.rodex_database_path).statistics
    assert statistics is not None
    assert statistics.statistics_publication_sequence == 3
    assert len(stale_adapters) == 2
    assert stale_adapters[-1].appended_analyses == [
        (accepted_baseline + leader_append,),
        (later_append,),
    ]


def test_worker_analyzes_only_through_final_complete_newline(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    with rollout.open("ab") as output:
        output.write(b'{"incomplete":true')
    _create(config)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)

    assert worker.poll_once() == "up_to_date"
    assert worker.poll_once() == "up_to_date"

    assert len(adapter.analyses) == 1
    assert adapter.analyses[0][0][0].endswith(b"\n")
    assert b"incomplete" not in adapter.analyses[0][0][0]
    source = list_rodex_session_codex_threads(1, config.rodex_database_path)[0]
    assert source.analyzed_size_bytes is not None
    assert source.analyzed_size_bytes < rollout.stat().st_size


def test_same_size_rewrite_with_restored_mtime_invalidates_append_cursor(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    with rollout.open("a", encoding="utf-8") as output:
        output.write('{"type":"event_msg","payload":{"marker":"one"}}\n')
    _create(config)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)
    assert worker.poll_once() == "up_to_date"
    original_stat = rollout.stat()
    original = rollout.read_text(encoding="utf-8")

    rollout.write_text(original.replace('"one"', '"two"'), encoding="utf-8")
    os.utime(
        rollout,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert worker.poll_once() == "clean_replay"
    assert len(adapter.analyses) == 1
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 1
    assert view.worker is not None
    assert view.worker.diagnostic_code == "analytics_error"


def test_worker_does_not_adopt_a_replacement_codex_identity(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    original_rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _rollout(config.codex_sessions_root, REPLACEMENT_CODEX_SESSION_ID)
    create_a_rodex_session(
        config.rodex_database_path,
        rodex_session_id=RODEX_SESSION_ID,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
        runtime_id=config.runtime_id,
    )
    registry_id = lookup_rodex_registry_id(config.rodex_database_path)
    assert registry_id is not None
    object.__setattr__(config, "rodex_registry_id", registry_id)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)
    assert worker.poll_once() == "up_to_date"
    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        config.rodex_database_path,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
        runtime_id=REPLACEMENT_RUNTIME_ID,
    )
    with original_rollout.open("a", encoding="utf-8") as output:
        output.write('{"type":"event_msg","payload":{"changed":true}}\n')

    assert worker.poll_once() == "clean_replay"

    assert len(adapter.analyses) == 2
    sources = list_rodex_session_codex_threads(1, config.rodex_database_path)
    assert [source.codex_thread_id for source in sources] == [REPLACEMENT_CODEX_SESSION_ID]
    with sqlite3.connect(config.rodex_database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM rodex_sessions_codex_threads WHERE rodex_sessions_id = 1"
        ).fetchone() == (2,)
    assert sources[0].verified_at_utc is None

    replacement_config = replace(
        config,
        runtime_id=REPLACEMENT_RUNTIME_ID,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
    )

    class ReplacementAnalyticsAdapter(FakeAnalyticsAdapter):
        def analyze_rollouts(
            self,
            sources: list[AnalyticsAnalyzerSource],
            user_id: str,
        ) -> AnalyticsCalculation:
            calculation = super().analyze_rollouts(sources, user_id)
            return replace(
                calculation,
                statistics_projection=replace(
                    calculation.statistics_projection,
                    turn_statistics=tuple(
                        replace(turn, codex_thread_id=REPLACEMENT_CODEX_SESSION_ID)
                        for turn in calculation.statistics_projection.turn_statistics
                    ),
                ),
            )

    replacement_adapter = ReplacementAnalyticsAdapter()
    replacement_worker = AnalyticsRolloutWorker(
        replacement_config,
        adapter_factory=lambda: replacement_adapter,
    )

    assert replacement_worker.poll_once() == "up_to_date"

    sources = list_rodex_session_codex_threads(1, config.rodex_database_path)
    assert [source.codex_thread_id for source in sources] == [REPLACEMENT_CODEX_SESSION_ID]
    assert sources[0].verified_at_utc is not None


def test_registry_fence_rejects_a_stale_runtime_for_every_analytics_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    publications: list[RodexAnalyticsPublication] = []
    original_publish = RodexAnalyticsRegistry.publish

    def remember_publication(
        registry: RodexAnalyticsRegistry,
        publication: RodexAnalyticsPublication,
    ) -> object:
        publications.append(publication)
        return original_publish(registry, publication)

    monkeypatch.setattr(RodexAnalyticsRegistry, "publish", remember_publication)
    worker = AnalyticsRolloutWorker(config, adapter_factory=FakeAnalyticsAdapter)
    assert worker.poll_once() == "up_to_date"
    assert worker._registry is not None
    stale_registry = worker._registry
    before = read_rodex_session_statistics(1, config.rodex_database_path)

    record_a_rodex_session_runtime_resume(
        1,
        config.rodex_database_path.parent / "tmux.sock",
        "replacement-runtime",
        config.rodex_database_path,
        runtime_id=REPLACEMENT_RUNTIME_ID,
    )

    with pytest.raises(RodexSessionStatisticsConflictError, match="identity changed"):
        stale_registry.load_checkpoint()
    with pytest.raises(RodexSessionStatisticsConflictError, match="identity changed"):
        stale_registry.publish(publications[0])
    with pytest.raises(RodexSessionStatisticsConflictError, match="identity changed"):
        stale_registry.record_health_transition(
            worker_state="degraded",
            diagnostic_code="stale_runtime",
            attempted_at_utc="2026-08-26T00:00:00+00:00",
            failed=True,
            prior_consecutive_failures=0,
        )

    after = read_rodex_session_statistics(1, config.rodex_database_path)
    assert after.statistics == before.statistics
    assert after.worker == before.worker
    resumed_registry = RodexAnalyticsRegistry.open(
        config.rodex_database_path,
        session_id=1,
        rodex_session_id=config.rodex_session_id,
        rodex_registry_id=config.rodex_registry_id,
        runtime_id=REPLACEMENT_RUNTIME_ID,
        expected_codex_session_id=config.codex_session_id,
    )
    assert resumed_registry.load_checkpoint().statistics is not None


@pytest.mark.parametrize(
    "fence_change",
    ["registry", "session", "runtime", "codex"],
)
def test_checkpoint_rejects_each_wrong_durable_identity(
    tmp_path: Path,
    fence_change: str,
) -> None:
    config = _config(tmp_path)
    _create(config)
    values: dict[str, object] = {
        "rodex_session_id": config.rodex_session_id,
        "rodex_registry_id": config.rodex_registry_id,
        "runtime_id": config.runtime_id,
        "expected_codex_session_id": config.codex_session_id,
    }
    parameter_name = {
        "registry": "rodex_registry_id",
        "session": "rodex_session_id",
        "runtime": "runtime_id",
        "codex": "expected_codex_session_id",
    }[fence_change]
    values[parameter_name] = {
        "registry": RodexRegistryId(config.rodex_registry_id.value + 1),
        "session": RodexSessionId(config.rodex_session_id.value + 1),
        "runtime": REPLACEMENT_RUNTIME_ID,
        "codex": REPLACEMENT_CODEX_SESSION_ID,
    }[fence_change]
    registry = RodexAnalyticsRegistry.open(
        config.rodex_database_path,
        session_id=1,
        **values,  # type: ignore[arg-type]
    )

    with pytest.raises(RodexSessionStatisticsConflictError, match="identity changed"):
        registry.load_checkpoint()


def test_analyzer_failure_preserves_last_good_aggregate_and_increments_health(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)
    assert worker.poll_once() == "up_to_date"
    with rollout.open("a", encoding="utf-8") as output:
        output.write("{}\n")
    adapter.fail = True

    assert worker.poll_once() == "clean_replay"

    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 1
    assert view.statistics.projection.audit_privacy
    assert view.worker is not None
    assert view.worker.worker_state == "degraded"
    assert view.worker.consecutive_failures == 1
    assert view.worker.next_retry_at_utc is None


def test_transient_publication_retry_reuses_the_prepared_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)
    original_read = worker._source_reader.read
    read_calls = 0

    def count_read(source: object) -> object:
        nonlocal read_calls
        read_calls += 1
        return original_read(source)  # type: ignore[arg-type]

    monkeypatch.setattr(worker._source_reader, "read", count_read)

    from rodex_registry import analytics_registry as registry_module

    original_publish_statistics = registry_module.publish_rodex_session_statistics
    lower_publish_calls = 0

    def lock_once(*args: object, **kwargs: object) -> object:
        nonlocal lower_publish_calls
        lower_publish_calls += 1
        if lower_publish_calls == 1:
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        return original_publish_statistics(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(registry_module, "publish_rodex_session_statistics", lock_once)
    original_registry_publish = RodexAnalyticsRegistry.publish
    publications: list[object] = []

    def record_publication(registry: RodexAnalyticsRegistry, publication: object) -> object:
        publications.append(publication)
        return original_registry_publish(registry, publication)  # type: ignore[arg-type]

    monkeypatch.setattr(RodexAnalyticsRegistry, "publish", record_publication)

    assert worker.poll_once() == "publication_retry"
    assert adapter.accepted_batches == 0
    assert worker.poll_once(AnalyticsDirtyBatch(frozenset())) == "up_to_date"

    assert publications[0] is publications[1]
    assert read_calls == 1
    assert len(adapter.analyses) == 1
    assert adapter.accepted_batches == 1
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 1


def test_prepared_retry_reauthenticates_source_before_sql_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)
    from rodex_registry import analytics_registry as registry_module

    lower_publish_calls = 0

    def lock_once(*args: object, **kwargs: object) -> object:
        nonlocal lower_publish_calls
        lower_publish_calls += 1
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(registry_module, "publish_rodex_session_statistics", lock_once)
    assert worker.poll_once() == "publication_retry"
    original = rollout.read_bytes()
    rewritten = original.replace(b'"model": "gpt-test"', b'"model": "bad-test"')
    assert len(rewritten) == len(original)
    rollout.write_bytes(rewritten)

    assert worker.poll_once(AnalyticsDirtyBatch(frozenset())) == "clean_replay"

    assert lower_publish_calls == 1
    assert len(adapter.analyses) == 1
    assert read_rodex_session_statistics(1, config.rodex_database_path).statistics is None


def test_failed_analysis_resets_resident_state_for_one_clean_replay(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    failed = FakeAnalyticsAdapter()
    recovered = FakeAnalyticsAdapter()
    adapters = iter((failed, recovered))
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: next(adapters))

    assert worker.poll_once() == "up_to_date"
    assert CODEX_SESSION_ID in worker._verified_sources
    addition = b'{"type":"event_msg","payload":{"changed":true}}\n'
    with rollout.open("ab") as output:
        output.write(addition)
    failed.fail = True
    assert worker.poll_once() == "clean_replay"
    assert worker._verified_sources == {}
    assert worker.poll_once() == "up_to_date"

    assert recovered.analyses[0][0] == (rollout.read_bytes(),)
    assert recovered.appended_analyses[0] == (addition,)


def test_analyzer_schema_drift_degrades_without_replacing_relational_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    first_worker = AnalyticsRolloutWorker(
        config, adapter_factory=lambda: FakeAnalyticsAdapter()
    )
    assert first_worker.poll_once() == "up_to_date"
    before = read_rodex_session_statistics(1, config.rodex_database_path).statistics
    assert before is not None
    with rollout.open("a", encoding="utf-8") as output:
        output.write('{"timestamp":"2026-08-16T12:00:03Z","type":"future"}\n')

    class DriftedLibrary:
        def create_new_codex_protocol_id(self, _user_id: str) -> object:
            return SimpleNamespace(status="ok", value="temporary")

        def load_file(self, _protocol_id: str, _path: Path) -> object:
            return SimpleNamespace(status="ok", value=True)

        def get_stats(
            self, _protocol_id: str, *, include_turn_statistics: bool = False
        ) -> object:
            assert include_turn_statistics
            snapshot = analyzer_snapshot()
            snapshot["recommended_insight_stats"].pop("hands_on_turn_count")
            return SimpleNamespace(status="ok", value=snapshot)

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "rodex.analytics_analyzer.importlib.import_module",
        lambda _name: SimpleNamespace(CodexProtocolLibrary=DriftedLibrary),
    )
    state = AnalyticsRolloutWorker(
        config, adapter_factory=CodexProtocolAnalyticsAdapter
    ).poll_once()
    after = read_rodex_session_statistics(1, config.rodex_database_path)

    assert state == "clean_replay"
    assert after.statistics == before
    assert after.worker is not None and after.worker.worker_state == "degraded"
    assert after.worker.diagnostic_code == "analytics_error"


def test_append_during_analysis_publishes_prefix_then_catches_up(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    adapter = FakeAnalyticsAdapter()

    def append_once() -> None:
        adapter.on_analyze = None
        with rollout.open("a", encoding="utf-8") as output:
            output.write("{}\n")

    adapter.on_analyze = append_once

    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)
    state = worker.poll_once()

    assert state == "pending_append"
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 1
    assert view.sources[0].rollout_file_path == str(rollout.resolve())
    assert view.worker is not None
    assert view.worker.worker_state == "up_to_date"

    assert worker.poll_once() == "up_to_date"
    caught_up = read_rodex_session_statistics(1, config.rodex_database_path)
    assert caught_up.statistics is not None
    assert caught_up.statistics.statistics_publication_sequence == 2


def test_stale_worker_cannot_publish_snapshot_or_health_after_replacement(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _rollout(config.codex_sessions_root, REPLACEMENT_CODEX_SESSION_ID)
    create_a_rodex_session(
        config.rodex_database_path,
        rodex_session_id=RODEX_SESSION_ID,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
        runtime_id=config.runtime_id,
    )
    registry_id = lookup_rodex_registry_id(config.rodex_database_path)
    assert registry_id is not None
    object.__setattr__(config, "rodex_registry_id", registry_id)
    adapter = FakeAnalyticsAdapter()
    adapter.on_analyze = lambda: record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        config.rodex_database_path,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
        runtime_id=REPLACEMENT_RUNTIME_ID,
    )

    state = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter).poll_once()

    assert state == "clean_replay"
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is None
    assert view.worker is None


def test_partial_usable_analysis_publishes_gapped_coverage(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    adapter = FakeAnalyticsAdapter()
    adapter.coverage_state = "gapped"

    assert (
        AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter).poll_once()
        == "up_to_date"
    )

    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.coverage_state == "gapped"


def test_rollout_locator_rejects_filename_match_with_wrong_internal_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    _rollout(root, REPLACEMENT_CODEX_SESSION_ID).rename(
        root / "2026" / "08" / "16" / f"rollout-forged-{CODEX_SESSION_ID}.jsonl"
    )

    assert locate_verified_rollout(root, CODEX_SESSION_ID) is None


def test_rollout_locator_rejects_a_matching_symlink_that_escapes_the_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    outside = _rollout(tmp_path / "outside", CODEX_SESSION_ID)
    candidate = root / f"rollout-linked-{CODEX_SESSION_ID}.jsonl"
    candidate.symlink_to(outside)

    assert locate_verified_rollout(root, CODEX_SESSION_ID) is None


def test_rollout_locator_rejects_a_matching_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    candidate = root / f"rollout-pipe-{CODEX_SESSION_ID}.jsonl"
    os.mkfifo(candidate)

    assert locate_verified_rollout(root, CODEX_SESSION_ID) is None


def test_supervisor_start_failure_is_fail_open_and_health_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _create(config)
    monkeypatch.setattr(
        RodexAnalyticsRegistry,
        "load_checkpoint",
        lambda _registry: (_ for _ in ()).throw(
            AssertionError("supervisor health opened a read transaction")
        ),
    )

    def fail_start(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        raise OSError("cannot fork analytics")

    supervisor = AnalyticsSubprocessSupervisor(config, popen=fail_start)

    supervisor.start()
    supervisor.close()

    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is None
    assert view.worker is not None
    assert view.worker.worker_state == "degraded"
    assert view.worker.diagnostic_code == "analytics_worker_start_failed"


def test_supervisor_restarts_once_after_backoff_then_exhausts(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _create(config)
    first = FakeWorkerProcess()
    second = FakeWorkerProcess()
    pending = [first, second]
    commands: list[list[str]] = []
    second_started = Event()

    def start(command: list[str], **_options: object) -> FakeWorkerProcess:
        commands.append(command)
        process = pending.pop(0)
        if process is second:
            second_started.set()
        return process

    supervisor = AnalyticsSubprocessSupervisor(
        config,
        popen=start,  # type: ignore[arg-type]
        restart_delay_seconds=0.01,
    )
    supervisor.start()
    first.exit(7)
    assert second_started.wait(1)
    second.exit(7)
    assert supervisor.wait(1)

    assert len(commands) == 2

    supervisor.close()

    assert not second.terminated
    assert second.wait_calls == 1
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.worker is not None
    assert view.worker.worker_state == "degraded"
    assert view.worker.diagnostic_code == "analytics_worker_exited"
    assert view.worker.consecutive_failures == 2
    assert view.worker.next_retry_at_utc is None


def test_supervisor_bounds_repeated_start_failure_to_two_attempts(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _create(config)
    attempts = 0

    def fail_start(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal attempts
        attempts += 1
        raise OSError("cannot fork analytics")

    supervisor = AnalyticsSubprocessSupervisor(
        config,
        popen=fail_start,
        restart_delay_seconds=0,
    )

    supervisor.start()
    assert supervisor.wait(1)

    assert attempts == 2
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.worker is not None
    assert view.worker.consecutive_failures == 2
    assert view.worker.next_retry_at_utc is None


def test_supervisor_close_kills_and_reaps_a_worker_that_ignores_terminate(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    process = FakeWorkerProcess(timeout_on_wait=True)
    supervisor = AnalyticsSubprocessSupervisor(
        config,
        popen=lambda *_args, **_kwargs: process,  # type: ignore[arg-type]
    )
    supervisor.start()

    supervisor.close()

    assert process.terminated
    assert process.killed
    assert process.wait_calls == 3


def test_real_adapter_uses_existing_in_memory_analyzer_api(tmp_path: Path) -> None:
    rollout = _rollout(tmp_path, CODEX_SESSION_ID)

    calculation = CodexProtocolAnalyticsAdapter().analyze_rollouts(
        [_analyzer_source(rollout.read_bytes())], "test-user"
    )

    assert calculation.coverage_state == "complete"
    assert calculation.statistics_projection.analyzer_source_count == 1
    assert len(calculation.statistics_projection.turn_statistics) == 1
    assert (
        calculation.statistics_projection.turn_statistics[0].codex_thread_id
        == CODEX_SESSION_ID
    )
    assert (
        calculation.statistics_projection.turn_statistics[0].codex_turn_id == TURN_TEST_ID
    )
    assert calculation.statistics_projection.turn_statistics[0].model == "gpt-test"
    assert calculation.statistics_projection.turn_statistics[0].reasoning_effort == "xhigh"


def test_real_worker_publishes_exact_turn_projection_into_rodex_sql(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _create(config)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)

    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"

    exact = read_rodex_session_turn_statistics(1, TURN_TEST_ID, config.rodex_database_path)
    assert exact.statistics is not None
    assert exact.statistics.statistics_projection_schema_version == "rodex-statistics-v7"
    assert exact.worker is not None
    assert exact.worker.worker_state == "up_to_date"
    assert exact.turn is not None
    assert exact.turn.codex_thread_id == CODEX_SESSION_ID
    assert exact.turn.outcome == "completed"
    assert exact.turn.projection.model == "gpt-test"
    assert exact.turn.projection.reasoning_effort == "xhigh"


def test_real_worker_publishes_only_the_incrementally_changed_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rodex_registry import statistics as statistics_module

    config = _config(tmp_path)
    _create(config)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    publications: list[RodexAnalyticsPublication] = []
    lookup_resolutions: list[tuple[str, str]] = []
    original_publish = RodexAnalyticsRegistry.publish
    original_lookup = statistics_module.select_or_insert_lookup_id

    def record_lookup(
        connection: sqlite3.Connection,
        table_name: str,
        lookup_values: dict[str, object],
    ) -> int:
        lookup_resolutions.append((table_name, str(next(iter(lookup_values.values())))))
        return original_lookup(connection, table_name, lookup_values)

    def remember_publication(
        registry: RodexAnalyticsRegistry,
        publication: RodexAnalyticsPublication,
    ) -> object:
        publications.append(publication)
        return original_publish(registry, publication)

    monkeypatch.setattr(RodexAnalyticsRegistry, "publish", remember_publication)
    monkeypatch.setattr(
        statistics_module,
        "select_or_insert_lookup_id",
        record_lookup,
    )
    worker = AnalyticsRolloutWorker(config)
    assert worker.poll_once() == "up_to_date"

    class ResidentTurns(dict[tuple[uuid.UUID, str], TurnStatisticsProjection]):
        def __iter__(self) -> object:
            raise AssertionError("resident turn history was iterated")

        def items(self) -> object:
            raise AssertionError("resident turn history was scanned")

        def keys(self) -> object:
            raise AssertionError("resident turn keys were scanned")

        def values(self) -> object:
            raise AssertionError("resident turn values were scanned")

        def copy(self) -> object:
            raise AssertionError("resident turn history was copied")

    assert worker._published_turns is not None
    worker._published_turns = ResidentTurns(worker._published_turns)
    second_turn = [
        {
            "timestamp": "2026-08-16T12:01:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": TURN_SECOND_ID},
        },
        {
            "timestamp": "2026-08-16T12:01:02Z",
            "type": "turn_context",
            "payload": {
                "turn_id": TURN_SECOND_ID,
                "model": "gpt-test",
                "effort": "xhigh",
            },
        },
        {
            "timestamp": "2026-08-16T12:01:03Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": TURN_SECOND_ID},
        },
    ]
    with rollout.open("a", encoding="utf-8") as output:
        output.writelines(json.dumps(record) + "\n" for record in second_turn)

    assert (
        worker.poll_once(AnalyticsDirtyBatch(frozenset({CODEX_SESSION_ID}))) == "up_to_date"
    )

    assert len(publications) == 2
    incremental = publications[1]
    assert incremental.changed_turn_keys == {(CODEX_SESSION_ID, TURN_SECOND_ID)}
    assert [
        turn.codex_turn_id for turn in incremental.statistics_projection.turn_statistics
    ] == [TURN_SECOND_ID]
    assert lookup_resolutions == [
        ("model_names", "gpt-test"),
        ("reasoning_effort_names", "xhigh"),
    ]
    assert (
        read_rodex_session_turn_statistics(
            1,
            TURN_TEST_ID,
            config.rodex_database_path,
        ).turn
        is not None
    )
    assert (
        read_rodex_session_turn_statistics(
            1,
            TURN_SECOND_ID,
            config.rodex_database_path,
        ).turn
        is not None
    )


@pytest.mark.parametrize(
    "load_status, load_value, expected_coverage, raises",
    [
        ("warning", object(), "gapped", False),
        ("error", object(), "gapped", False),
        ("fatal", object(), None, True),
        ("error", None, None, True),
    ],
)
def test_adapter_maps_partial_values_but_rejects_fatal_or_valueless_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load_status: str,
    load_value: object | None,
    expected_coverage: str | None,
    raises: bool,
) -> None:
    closed: list[bool] = []

    class FakeLibrary:
        def create_new_codex_protocol_id(self, _user_id: str) -> object:
            return SimpleNamespace(status="ok", value="temporary")

        def load_file(self, _protocol_id: str, _path: Path) -> object:
            return SimpleNamespace(
                status=load_status,
                value=load_value,
                diagnostics=(),
            )

        def get_stats(
            self, _protocol_id: str, *, include_turn_statistics: bool = False
        ) -> object:
            assert include_turn_statistics
            snapshot = analyzer_snapshot()
            return SimpleNamespace(
                status="ok",
                value=snapshot,
            )

        def close(self) -> object:
            closed.append(True)
            return True

    monkeypatch.setattr(
        "rodex.analytics_analyzer.importlib.import_module",
        lambda _name: SimpleNamespace(CodexProtocolLibrary=FakeLibrary),
    )

    if raises:
        with pytest.raises(RodexAnalyticsError):
            CodexProtocolAnalyticsAdapter().analyze_rollouts(
                [_analyzer_source(b"{}\n")], "test-user"
            )
    else:
        calculation = CodexProtocolAnalyticsAdapter().analyze_rollouts(
            [_analyzer_source(b"{}\n")], "test-user"
        )
        assert calculation.coverage_state == expected_coverage
        assert calculation.statistics_projection.analyzer_source_count == 1
    assert closed == [True]
