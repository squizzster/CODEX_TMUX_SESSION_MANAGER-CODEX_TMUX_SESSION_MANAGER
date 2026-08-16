from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from rodex.analytics import (
    AnalyticsCalculation,
    AnalyticsRolloutWorker,
    AnalyticsSubprocessSupervisor,
    AnalyticsWorkerConfig,
    CodexProtocolAnalyticsAdapter,
    RodexAnalyticsError,
    locate_verified_rollout,
)
from rodex_functions import (
    create_a_rodex_session,
    list_rodex_session_statistics_sources,
    read_rodex_session_statistics,
    record_a_rodex_session_runtime_resume,
)

RODEX_UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")
CODEX_UUID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
REPLACEMENT_CODEX_UUID = uuid.UUID(int=CODEX_UUID.int + 1)


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
        return AnalyticsCalculation(
            aggregate_statistics={
                "event_count": len(paths),
                "source_count": len(paths),
                "must_have_basic_stats": {"turns": {"started": len(paths)}},
                "recommended_insight_stats": {},
                "audit": {"privacy": "aggregate-only"},
                "protocol_id": "temporary-must-not-persist",
            },
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


def _rollout(root: Path, codex_uuid: uuid.UUID) -> Path:
    path = root / "2026" / "08" / "16" / f"rollout-example-{codex_uuid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-16T12:00:00Z",
                "type": "session_meta",
                "payload": {"id": str(codex_uuid)},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path) -> AnalyticsWorkerConfig:
    return AnalyticsWorkerConfig(
        rodex_database_path=tmp_path / "rodex.sqlite3",
        codex_sessions_root=tmp_path / "sessions",
        rodex_uuid=RODEX_UUID,
    )


def _create(config: AnalyticsWorkerConfig, codex_uuid: uuid.UUID = CODEX_UUID) -> None:
    create_a_rodex_session(
        config.rodex_database_path,
        rodex_session_uuid=RODEX_UUID,
        codex_session_uuid=codex_uuid,
    )


def test_worker_waits_for_unregistered_identity_without_opening_analyzer(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    adapters: list[FakeAnalyticsAdapter] = []

    def create_adapter() -> FakeAnalyticsAdapter:
        adapter = FakeAnalyticsAdapter()
        adapters.append(adapter)
        return adapter

    state = AnalyticsRolloutWorker(config, adapter_factory=create_adapter).poll_once()

    assert state == "catching_up"
    assert adapters == []


def test_worker_backfills_verified_rollout_and_projects_only_aggregates(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_UUID)
    _create(config)
    adapter = FakeAnalyticsAdapter()
    worker = AnalyticsRolloutWorker(config, adapter_factory=lambda: adapter)

    assert worker.poll_once() == "up_to_date"

    assert adapter.analyses[0][0] == (rollout.read_bytes(),)
    assert adapter.analyses[0][1].startswith("posix:")
    source = list_rodex_session_statistics_sources(1, config.rodex_database_path)[0]
    assert source.codex_session_uuid == CODEX_UUID
    assert source.rollout_file_path == str(rollout.resolve())
    assert source.analyzed_prefix_sha256 is not None
    view = read_rodex_session_statistics(1, config.rodex_database_path)
    assert view.statistics is not None
    assert view.statistics.statistics_revision == 1
    assert view.worker is not None
    assert view.worker.worker_state == "up_to_date"
    assert view.statistics.aggregate_statistics["audit"] == {"privacy": "aggregate-only"}
    assert "protocol_id" not in view.statistics.aggregate_statistics
    assert b'"type":"session_meta"' not in config.rodex_database_path.read_bytes()


def test_unchanged_rollout_does_not_recalculate_but_append_does(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_UUID)
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
    assert view.statistics.statistics_revision == 2


def test_worker_analyzes_only_through_final_complete_newline(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_UUID)
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
    rollout = _rollout(config.codex_sessions_root, CODEX_UUID)
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
    assert view.statistics.statistics_revision == 2


def test_replacement_retains_old_source_and_analyzes_full_history(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first = _rollout(config.codex_sessions_root, CODEX_UUID)
    replacement = _rollout(config.codex_sessions_root, REPLACEMENT_CODEX_UUID)
    create_a_rodex_session(
        config.rodex_database_path,
        rodex_session_uuid=RODEX_UUID,
        codex_session_uuid=CODEX_UUID,
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
        codex_session_uuid=REPLACEMENT_CODEX_UUID,
    )

    assert worker.poll_once() == "up_to_date"

    assert adapter.analyses[-1][0] == (first.read_bytes(), replacement.read_bytes())
    sources = list_rodex_session_statistics_sources(1, config.rodex_database_path)
    assert [source.codex_session_uuid for source in sources] == [
        CODEX_UUID,
        REPLACEMENT_CODEX_UUID,
    ]
    assert all(source.included_statistics_revision == 2 for source in sources)


def test_analyzer_failure_preserves_last_good_aggregate_and_increments_health(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_UUID)
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
    assert view.statistics.statistics_revision == 1
    assert view.statistics.aggregate_statistics["audit"] == {"privacy": "aggregate-only"}
    assert view.worker is not None
    assert view.worker.worker_state == "degraded"
    assert view.worker.consecutive_failures == 1
    assert view.worker.next_retry_at_utc is not None


def test_source_change_during_analysis_does_not_publish(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rollout = _rollout(config.codex_sessions_root, CODEX_UUID)
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
    _rollout(config.codex_sessions_root, CODEX_UUID)
    _rollout(config.codex_sessions_root, REPLACEMENT_CODEX_UUID)
    create_a_rodex_session(
        config.rodex_database_path,
        rodex_session_uuid=RODEX_UUID,
        codex_session_uuid=CODEX_UUID,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="first",
    )
    adapter = FakeAnalyticsAdapter()
    adapter.on_analyze = lambda: record_a_rodex_session_runtime_resume(
        1,
        tmp_path / "tmux.sock",
        "replacement",
        config.rodex_database_path,
        codex_session_uuid=REPLACEMENT_CODEX_UUID,
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
    _rollout(config.codex_sessions_root, CODEX_UUID)
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
    _rollout(root, REPLACEMENT_CODEX_UUID).rename(
        root / "2026" / "08" / "16" / f"rollout-forged-{CODEX_UUID}.jsonl"
    )

    assert locate_verified_rollout(root, CODEX_UUID) is None


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
    rollout = _rollout(tmp_path, CODEX_UUID)

    calculation = CodexProtocolAnalyticsAdapter().analyze_rollouts([rollout], "test-user")

    assert calculation.coverage_state == "complete"
    assert calculation.aggregate_statistics["source_count"] == 1
    assert "protocol_id" not in calculation.aggregate_statistics
    assert "user_id" not in calculation.aggregate_statistics
    assert "revision" not in calculation.aggregate_statistics


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

        def get_stats(self, _protocol_id: str) -> object:
            return SimpleNamespace(
                status="ok",
                value={
                    "event_count": 1,
                    "source_count": 1,
                    "must_have_basic_stats": {},
                    "protocol_id": "must-be-dropped",
                },
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
        assert "protocol_id" not in calculation.aggregate_statistics
    assert closed == [True]
