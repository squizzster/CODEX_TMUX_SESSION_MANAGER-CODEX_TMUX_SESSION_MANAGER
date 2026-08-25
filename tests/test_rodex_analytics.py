from __future__ import annotations

import json
import os
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
    AnalyticsCalculation,
    AnalyticsRolloutWorker,
    AnalyticsSubprocessSupervisor,
    CodexProtocolAnalyticsAdapter,
    RodexAnalyticsError,
    _derive_verified_collaboration_projection,
    locate_verified_rollout,
)
from rodex.process_contracts import AnalyticsWorkerConfig
from rodex_registry import (
    RodexRegistryId,
    RodexRuntimeId,
    RodexSessionId,
    RodexSessionStatisticsSourceObservation,
    SessionStatisticsProjection,
    StatisticsNamedCount,
    TurnStatisticsProjection,
    create_a_rodex_session,
    list_rodex_session_statistics_sources,
    parse_session_statistics_snapshot,
    read_rodex_session_statistics,
    read_rodex_session_turn_statistics,
    record_a_rodex_session_runtime_resume,
)

RODEX_SESSION_ID = RodexSessionId.parse("1234567890abcdef")
CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
REPLACEMENT_CODEX_SESSION_ID = uuid.UUID(int=CODEX_SESSION_ID.int + 1)


class FakeAnalyticsAdapter:
    def __init__(self) -> None:
        self.analyses: list[tuple[tuple[bytes, ...], str]] = []
        self.fail = False
        self.coverage_state = "complete"
        self.on_analyze: Callable[[], None] | None = None

    def analyze_rollouts(self, paths: list[Path], user_id: str) -> AnalyticsCalculation:
        if self.fail:
            raise OSError("analytics unavailable")
        contents = tuple(path.read_bytes() for path in paths)
        self.analyses.append((contents, user_id))
        if self.on_analyze is not None:
            self.on_analyze()
        base = parse_session_statistics_snapshot(analyzer_snapshot())
        return AnalyticsCalculation(
            statistics_projection=replace(
                base,
                analyzer_event_count=len(paths),
                analyzer_source_count=len(paths),
                history_records_count=len(paths),
            ),
            coverage_state=self.coverage_state,
        )


class FakeWorkerProcess:
    def __init__(self, *, timeout_on_wait: bool = False) -> None:
        self.returncode: int | None = None
        self.timeout_on_wait = timeout_on_wait
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> int:
        assert timeout == 1
        self.wait_calls += 1
        if self.timeout_on_wait and not self.killed:
            raise subprocess.TimeoutExpired("analytics-worker", timeout)
        self.returncode = -9 if self.killed else 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True


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
            "payload": {"type": "task_started", "turn_id": "turn-test"},
        },
        {
            "timestamp": "2026-08-16T12:00:02Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-test",
                "model": "gpt-test",
                "effort": "xhigh",
            },
        },
        {
            "timestamp": "2026-08-16T12:00:03Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-test"},
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
) -> Path:
    path = root / "2026" / "08" / "16" / f"rollout-child-{child_thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    spawn = {
        "parent_thread_id": str(root_thread_id),
        "depth": 1,
        "agent_path": "/root/review",
        "agent_nickname": "Curie",
    }
    records = [
        {
            "timestamp": "2026-08-16T12:00:00.500000Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "session_id": str(root_thread_id),
                "id": str(child_thread_id),
                "forked_from_id": str(root_thread_id),
                "parent_thread_id": str(root_thread_id),
                "timestamp": "2026-08-16T12:00:00.500000Z",
                "source": {"subagent": {"thread_spawn": spawn}},
                "thread_source": "subagent",
                "agent_path": "/root/review",
                "agent_nickname": "Curie",
                "subagent_history_start_ordinal": 2,
            },
        },
        {"ordinal": 1, "type": "event_msg", "payload": {"inherited": True}},
        {"ordinal": 2, "type": "event_msg", "payload": {"inherited": True}},
        {"ordinal": 3, "type": "event_msg", "payload": {"child": True}},
    ]
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


def _create(
    config: AnalyticsWorkerConfig, codex_session_id: uuid.UUID = CODEX_SESSION_ID
) -> None:
    create_a_rodex_session(
        config.rodex_database_path,
        rodex_session_id=RODEX_SESSION_ID,
        codex_session_id=codex_session_id,
    )


def _collaboration_source(
    tmp_path: Path,
    thread_id: uuid.UUID,
    *,
    linked_at_utc: str,
    parent_thread_id: uuid.UUID | None = None,
    depth: int = 0,
) -> RodexSessionStatisticsSourceObservation:
    is_subagent = parent_thread_id is not None
    return RodexSessionStatisticsSourceObservation(
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

    assert state == "catching_up"
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
        def run(self, reconcile: Callable[[], object]) -> None:
            lifecycle.append(f"reconcile:{reconcile()}")

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
    source = list_rodex_session_statistics_sources(1, config.rodex_database_path)[0]
    assert source.codex_thread_id == CODEX_SESSION_ID
    assert source.rollout_file_path == str(rollout.resolve())
    assert source.analyzed_prefix_sha256 is not None


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
    assert child.parent_rodex_sessions_statistics_sources_id == root.id
    assert child.agent_path == "/root/review"
    assert child.subagent_history_start_ordinal == 2
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 1
    assert view.worker is not None
    assert view.worker.worker_state == "up_to_date"
    assert view.statistics.projection.audit_privacy
    assert b'"type":"session_meta"' not in config.rodex_database_path.read_bytes()


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
            '"payload":{"type":"task_started","turn_id":"turn-1"}}\n'
        )
    assert worker.poll_once() == "up_to_date"

    assert len(adapter.analyses) == 2
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 2


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
    source = list_rodex_session_statistics_sources(1, config.rodex_database_path)[0]
    assert source.analyzed_size_bytes is not None
    assert source.analyzed_size_bytes < rollout.stat().st_size


def test_same_size_rewrite_with_restored_mtime_is_reauthenticated(
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

    assert worker.poll_once() == "up_to_date"
    assert len(adapter.analyses) == 2
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 2


def test_worker_does_not_adopt_a_replacement_codex_identity(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    replacement = _rollout(config.codex_sessions_root, REPLACEMENT_CODEX_SESSION_ID)
    create_a_rodex_session(
        config.rodex_database_path,
        rodex_session_id=RODEX_SESSION_ID,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
    )
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)
    assert worker.poll_once() == "up_to_date"
    record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        config.rodex_database_path,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
    )

    assert worker.poll_once() == "degraded"

    assert adapter.analyses[-1][0] == (first.read_bytes(), replacement.read_bytes())
    sources = list_rodex_session_statistics_sources(1, config.rodex_database_path)
    assert [source.codex_thread_id for source in sources] == [
        CODEX_SESSION_ID,
        REPLACEMENT_CODEX_SESSION_ID,
    ]
    assert sources[0].included_statistics_publication_sequence == 1
    assert sources[1].included_statistics_publication_sequence is None


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

    assert worker.poll_once() == "degraded"

    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_publication_sequence == 1
    assert view.statistics.projection.audit_privacy
    assert view.worker is not None
    assert view.worker.worker_state == "degraded"
    assert view.worker.consecutive_failures == 1
    assert view.worker.next_retry_at_utc is not None


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
        "rodex.analytics.importlib.import_module",
        lambda _name: SimpleNamespace(CodexProtocolLibrary=DriftedLibrary),
    )
    state = AnalyticsRolloutWorker(
        config, adapter_factory=CodexProtocolAnalyticsAdapter
    ).poll_once()
    after = read_rodex_session_statistics(1, config.rodex_database_path)

    assert state == "degraded"
    assert after.statistics == before
    assert after.worker is not None and after.worker.worker_state == "degraded"
    assert after.worker.diagnostic_code == "analytics_error"


def test_source_change_during_analysis_does_not_publish(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_SESSION_ID)
    _create(config)
    adapter = FakeAnalyticsAdapter()
    adapter.on_analyze = lambda: rollout.write_text(
        rollout.read_text(encoding="utf-8") + "{}\n", encoding="utf-8"
    )

    state = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter).poll_once()

    assert state == "degraded"
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is None
    assert view.sources[0].rollout_file_path is None
    assert view.worker is not None
    assert view.worker.worker_state == "degraded"


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
    )
    adapter = FakeAnalyticsAdapter()
    adapter.on_analyze = lambda: record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        config.rodex_database_path,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
    )

    state = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter).poll_once()

    assert state == "degraded"
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is None
    assert view.worker is not None
    assert view.worker.worker_state == "catching_up"
    assert view.worker.diagnostic_code is None


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


def test_supervisor_start_failure_is_fail_open_and_health_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _create(config)

    def fail_start(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        raise OSError("cannot fork analytics")

    supervisor = AnalyticsSubprocessSupervisor(
        config, popen=fail_start, monotonic=lambda: 1.0
    )

    supervisor.poll()
    supervisor.close()

    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is None
    assert view.worker is not None
    assert view.worker.worker_state == "degraded"
    assert view.worker.diagnostic_code == "analytics_worker_start_failed"


def test_supervisor_restarts_only_after_backoff_and_closes_new_worker(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _create(config)
    clock = [1.0]
    first = FakeWorkerProcess()
    second = FakeWorkerProcess()
    pending = [first, second]
    commands: list[list[str]] = []

    def start(command: list[str], **_options: object) -> FakeWorkerProcess:
        commands.append(command)
        return pending.pop(0)

    supervisor = AnalyticsSubprocessSupervisor(
        config,
        popen=start,  # type: ignore[arg-type]
        monotonic=lambda: clock[0],
        restart_delay_seconds=2.0,
    )
    supervisor.poll()
    first.returncode = 7
    supervisor.poll()
    clock[0] = 2.9
    supervisor.poll()
    assert len(commands) == 1
    clock[0] = 3.0
    supervisor.poll()
    assert len(commands) == 2

    supervisor.close()

    assert second.terminated
    assert second.wait_calls == 1
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.worker is not None
    assert view.worker.worker_state == "degraded"
    assert view.worker.diagnostic_code == "analytics_worker_exited"


def test_supervisor_close_kills_and_reaps_a_worker_that_ignores_terminate(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    process = FakeWorkerProcess(timeout_on_wait=True)
    supervisor = AnalyticsSubprocessSupervisor(
        config,
        popen=lambda *_args, **_kwargs: process,  # type: ignore[arg-type]
    )
    supervisor.poll()

    supervisor.close()

    assert process.terminated
    assert process.killed
    assert process.wait_calls == 2


def test_real_adapter_uses_existing_in_memory_analyzer_api(tmp_path: Path) -> None:
    rollout = _rollout(tmp_path, CODEX_SESSION_ID)

    calculation = CodexProtocolAnalyticsAdapter().analyze_rollouts([rollout], "test-user")

    assert calculation.coverage_state == "complete"
    assert calculation.statistics_projection.analyzer_source_count == 1
    assert len(calculation.statistics_projection.turn_statistics) == 1
    assert (
        calculation.statistics_projection.turn_statistics[0].codex_thread_id
        == CODEX_SESSION_ID
    )
    assert calculation.statistics_projection.turn_statistics[0].codex_turn_id == "turn-test"
    assert calculation.statistics_projection.turn_statistics[0].model == "gpt-test"
    assert calculation.statistics_projection.turn_statistics[0].reasoning_effort == "xhigh"


def test_real_worker_publishes_exact_turn_projection_into_rodex_sql(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    _create(config)
    _rollout(config.codex_sessions_root, CODEX_SESSION_ID)

    assert AnalyticsRolloutWorker(config).poll_once() == "up_to_date"

    exact = read_rodex_session_turn_statistics(1, "turn-test", config.rodex_database_path)
    assert exact.statistics is not None
    assert exact.statistics.statistics_projection_schema_version == "rodex-statistics-v6"
    assert exact.worker is not None
    assert exact.worker.worker_state == "up_to_date"
    assert exact.turn is not None
    assert exact.turn.codex_thread_id == CODEX_SESSION_ID
    assert (
        exact.turn.included_statistics_publication_sequence
        == exact.statistics.statistics_publication_sequence
    )
    assert exact.turn.outcome == "completed"
    assert exact.turn.projection.model == "gpt-test"
    assert exact.turn.projection.reasoning_effort == "xhigh"


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
        "rodex.analytics.importlib.import_module",
        lambda _name: SimpleNamespace(CodexProtocolLibrary=FakeLibrary),
    )

    if raises:
        with pytest.raises(RodexAnalyticsError):
            CodexProtocolAnalyticsAdapter().analyze_rollouts(
                [tmp_path / "source.jsonl"], "test-user"
            )
    else:
        calculation = CodexProtocolAnalyticsAdapter().analyze_rollouts(
            [tmp_path / "source.jsonl"], "test-user"
        )
        assert calculation.coverage_state == expected_coverage
        assert calculation.statistics_projection.analyzer_source_count == 1
    assert closed == [True]
