"""Fail-open, aggregate-only analytics for managed Codex rollout files."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Any

from rodex_registry import (
    COLLABORATION_MODEL_TOOL_NAMES,
    CodexSessionId,
    CodexThreadId,
    RodexAnalyticsPublication,
    RodexAnalyticsRegistry,
    RodexAnalyticsStatisticsCheckpoint,
    RodexSessionStatisticsSource,
    RodexSessionStatisticsSourceObservation,
    SessionStatisticsProjection,
    StatisticsNamedCount,
    TurnStatisticsProjection,
    current_rodex_sessions_user_identity,
    parse_codex_thread_id,
)
from rodex_sql import RodexDatabaseNotFoundError

from .analytics_analyzer import (
    AnalyticsAnalyzerSource,
    AnalyticsBoundary,
    AnalyticsBoundaryFactory,
    RodexAnalyticsError,
    StatefulCodexProtocolAnalyticsAdapter,
)
from .analytics_scheduler import (
    AnalyticsEventScheduler,
    AnalyticsProtocolEventSubscriber,
)
from .analytics_source_catalog import AnalyticsSourceCatalog
from .analytics_source_reader import (
    AnalyticsAppendSource,
    AnalyticsSourceRead,
    AnalyticsSourceReader,
    AnalyticsSourceReadError,
    AuthenticatedRolloutPrefix,
    open_rollout_descriptor,
    resolve_rollout_path,
)
from .process_contracts import AnalyticsWorkerConfig

ANALYTICS_RESTART_DELAY_SECONDS = 2.0
STATISTICS_PROJECTION_SCHEMA_VERSION = "rodex-statistics-v7"


@dataclass(frozen=True, slots=True)
class VerifiedRollout:
    """An exact root or sub-agent rollout authenticated from its own metadata."""

    path: Path
    size_bytes: int
    modified_at_ns: int
    codex_thread_id: CodexThreadId
    source_kind: str
    parent_codex_thread_id: CodexThreadId | None
    thread_depth: int
    agent_path: str | None
    agent_nickname: str | None
    subagent_history_start_ordinal: int | None
    first_linked_at_utc: str


@dataclass(frozen=True, slots=True)
class VerifiedCollaborationProjection:
    """Canonical collaboration facts joined to authenticated source lineage."""

    statistics_projection: SessionStatisticsProjection
    analyzed_sources: tuple[RodexSessionStatisticsSourceObservation, ...]


@dataclass(frozen=True, slots=True)
class StableRolloutRead:
    """One in-memory complete-line prefix plus its exact source state."""

    analyzer_content: bytes
    appended_analyzer_content: bytes
    prepared_read: AnalyticsSourceRead
    observation: RodexSessionStatisticsSourceObservation
    authenticated_source: AuthenticatedRolloutPrefix


def _analyzer_sources(
    stable_reads: Sequence[StableRolloutRead],
) -> list[AnalyticsAnalyzerSource]:
    """Adapt authenticated reads into the worker's single analyzer boundary."""
    return [
        AnalyticsAnalyzerSource(
            codex_thread_id=item.observation.codex_thread_id,
            analyzer_content=item.analyzer_content,
            appended_analyzer_content=item.appended_analyzer_content,
        )
        for item in stable_reads
    ]


def _turns_by_key(
    turns: Sequence[TurnStatisticsProjection],
) -> dict[tuple[CodexThreadId, str], TurnStatisticsProjection]:
    return {(turn.codex_thread_id, turn.codex_turn_id): turn for turn in turns}


def _changed_source_thread_ids(
    prior_sources: Sequence[RodexSessionStatisticsSource],
    stable_reads: Sequence[StableRolloutRead],
) -> frozenset[CodexThreadId]:
    prior_by_thread = {source.codex_thread_id: source for source in prior_sources}
    return frozenset(
        read.observation.codex_thread_id
        for read in stable_reads
        if (
            (prior := prior_by_thread.get(read.observation.codex_thread_id)) is None
            or prior.rollout_file_path != str(read.observation.rollout_file_path)
            or prior.analyzed_size_bytes != read.observation.analyzed_size_bytes
            or prior.analyzed_mtime_ns != read.observation.analyzed_mtime_ns
            or prior.analyzed_prefix_sha256
            != read.observation.analyzed_prefix_sha256
        )
    )


class AnalyticsRolloutWorker:
    """Watch verified rollouts and project aggregate statistics into Rodex SQLite."""

    def __init__(
        self,
        config: AnalyticsWorkerConfig,
        *,
        adapter_factory: AnalyticsBoundaryFactory = (StatefulCodexProtocolAnalyticsAdapter),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not config.is_activated:
            raise ValueError("analytics worker requires committed runtime identity")
        self._config = config
        self._adapter_factory = adapter_factory
        self._adapter: AnalyticsBoundary | None = None
        self._now = now
        assert config.rodex_sessions_id is not None
        assert config.codex_session_id is not None
        self._session_id = config.rodex_sessions_id
        self._expected_codex_session_id = config.codex_session_id
        self._registry: RodexAnalyticsRegistry | None = None
        self._source_catalog = AnalyticsSourceCatalog(config.codex_sessions_root)
        self._source_reader = AnalyticsSourceReader()
        self._verified_sources: dict[CodexThreadId, VerifiedRollout] = {}
        self._schedule_followup: Callable[[], None] | None = None
        self._last_health_transition: tuple[str, str | None] | None = None
        self._consecutive_failures = 0
        self._published_turns: dict[
            tuple[CodexThreadId, str], TurnStatisticsProjection
        ] | None = None

    def observe_protocol_event(self, event: Mapping[str, Any]) -> None:
        """Feed exact lifecycle identity metadata into bounded source resolution."""
        self._source_catalog.observe_protocol_event(event)

    def poll_once(self) -> str:
        """Perform one reconciliation; no analytics failure is allowed to escape."""
        expected_codex_session_id = self._expected_codex_session_id
        try:
            session_id = self._session_id
            codex_session_id = self._expected_codex_session_id
            registry = self._registry
            if registry is None:
                registry = RodexAnalyticsRegistry.open(
                    self._config.rodex_database_path,
                    session_id=session_id,
                    expected_codex_session_id=codex_session_id,
                )
                self._registry = registry
            view = registry.load_checkpoint()
            if view.worker is not None:
                self._last_health_transition = (
                    view.worker.worker_state,
                    view.worker.diagnostic_code,
                )
                self._consecutive_failures = view.worker.consecutive_failures
            stable_reads = self._read_registered_sources(view.sources)
            if stable_reads is None:
                self._project_health("catching_up", "rollout_not_found", codex_session_id)
                return "catching_up"
            if _view_matches_source_reads(view.statistics, view.sources, stable_reads):
                adapter = self._adapter
                if adapter is None:
                    adapter = self._adapter_factory()
                    self._adapter = adapter
                    warm_calculation = adapter.analyze_rollouts(
                        _analyzer_sources(stable_reads),
                        _current_analytics_user_id(),
                    )
                    warm_projection = _derive_verified_collaboration_projection(
                        warm_calculation.statistics_projection,
                        analyzed_sources=tuple(
                            item.observation for item in stable_reads
                        ),
                    )
                    self._published_turns = _turns_by_key(
                        warm_projection.statistics_projection.turn_statistics
                    )
                adapter.accept_batch()
                self._source_reader.accept([item.prepared_read for item in stable_reads])
                if view.worker is None or view.worker.worker_state != "up_to_date":
                    self._project_health("up_to_date", None, codex_session_id)
                return "up_to_date"
            adapter = self._adapter
            if adapter is None:
                adapter = self._adapter_factory()
                self._adapter = adapter
            calculation = adapter.analyze_rollouts(
                _analyzer_sources(stable_reads),
                _current_analytics_user_id(),
            )
            source_growth = tuple(
                self._source_reader.verify_captured_prefix(item.authenticated_source)
                for item in stable_reads
            )
            append_arrived_during_analysis = any(source_growth)
            verified_collaboration = _derive_verified_collaboration_projection(
                calculation.statistics_projection,
                analyzed_sources=tuple(item.observation for item in stable_reads),
            )
            current_turns = _turns_by_key(
                verified_collaboration.statistics_projection.turn_statistics
            )
            previous_turns = self._published_turns
            changed_turn_keys = (
                None
                if previous_turns is None
                else frozenset(
                    key
                    for key, turn in current_turns.items()
                    if previous_turns.get(key) != turn
                )
            )
            removed_turn_keys = (
                frozenset()
                if previous_turns is None
                else frozenset(previous_turns.keys() - current_turns.keys())
            )
            registry.publish(
                RodexAnalyticsPublication(
                    based_on_statistics_publication_sequence=(
                        None
                        if view.statistics is None
                        else view.statistics.statistics_publication_sequence
                    ),
                    statistics_projection_schema_version=(
                        STATISTICS_PROJECTION_SCHEMA_VERSION
                    ),
                    calculated_at_utc=self._timestamp(),
                    coverage_state=calculation.coverage_state,
                    statistics_projection=verified_collaboration.statistics_projection,
                    analyzed_sources=tuple(verified_collaboration.analyzed_sources),
                    changed_source_thread_ids=_changed_source_thread_ids(
                        view.sources, stable_reads
                    ),
                    changed_turn_keys=changed_turn_keys,
                    removed_turn_keys=removed_turn_keys,
                )
            )
            self._last_health_transition = ("up_to_date", None)
            self._consecutive_failures = 0
            self._published_turns = current_turns
            adapter.accept_batch()
            self._source_reader.accept([item.prepared_read for item in stable_reads])
            if append_arrived_during_analysis:
                if self._schedule_followup is not None:
                    self._schedule_followup()
                return "pending_append"
            return "up_to_date"
        except RodexDatabaseNotFoundError:
            return "catching_up"
        except Exception as error:
            self._project_health(
                "degraded",
                _diagnostic_code(error),
                expected_codex_session_id,
                failed=True,
            )
            self._adapter = None
            self._source_reader.require_clean_replay()
            return "degraded"

    def run_until_stopped(
        self,
        stop: Event,
        *,
        scheduler: AnalyticsEventScheduler | None = None,
        subscriber_factory: Callable[
            [Path, AnalyticsEventScheduler], AnalyticsProtocolEventSubscriber
        ] = AnalyticsProtocolEventSubscriber,
    ) -> None:
        active_scheduler = scheduler or AnalyticsEventScheduler(
            event_observer=self.observe_protocol_event
        )
        self._schedule_followup = active_scheduler.offer_dirty
        subscriber = subscriber_factory(
            self._config.protocol_event_socket_path,
            active_scheduler,
        )

        def stop_scheduler() -> None:
            stop.wait()
            active_scheduler.close()

        stop_watcher = Thread(
            target=stop_scheduler,
            name="rodex-analytics-stop-watcher",
            daemon=True,
        )
        subscriber.start()
        stop_watcher.start()
        try:
            active_scheduler.run(self.poll_once)
        finally:
            self._schedule_followup = None
            subscriber.close()

    def _read_registered_sources(
        self,
        sources: Sequence[RodexSessionStatisticsSource],
    ) -> list[StableRolloutRead] | None:
        verified_sources = _discover_verified_thread_rollouts(
            self._config.codex_sessions_root,
            sources,
            self._source_catalog,
            verified_cache=self._verified_sources,
        )
        if verified_sources is None:
            return None
        self._verified_sources = {
            verified.codex_thread_id: verified for verified in verified_sources
        }
        reads: list[StableRolloutRead] = []
        for verified in verified_sources:
            captured = self._source_reader.read(
                AnalyticsAppendSource(
                    path=verified.path,
                    codex_thread_id=verified.codex_thread_id,
                    source_kind=verified.source_kind,
                    subagent_history_start_ordinal=(
                        verified.subagent_history_start_ordinal
                    ),
                    allowed_root=self._config.codex_sessions_root,
                )
            )
            reads.append(_stable_rollout_read(verified, captured, self._timestamp()))
        return reads

    def _project_health(
        self,
        state: str,
        diagnostic_code: str | None,
        expected_codex_session_id: CodexSessionId | None,
        *,
        failed: bool = False,
    ) -> None:
        session_id = self._session_id
        if session_id is None or expected_codex_session_id is None:
            return
        try:
            registry = self._registry
            if registry is None:
                return
            transition = (state, diagnostic_code)
            if not failed and self._last_health_transition == transition:
                return
            now = self._now()
            registry.record_health_transition(
                worker_state=state,
                diagnostic_code=diagnostic_code,
                attempted_at_utc=now.isoformat(timespec="microseconds"),
                failed=failed,
                next_retry_at_utc=None,
                prior_consecutive_failures=self._consecutive_failures,
            )
            self._last_health_transition = transition
            self._consecutive_failures = self._consecutive_failures + 1 if failed else 0
        except Exception:
            return

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="microseconds")

    def mark_stopped(self) -> None:
        """Best-effort terminal health update for an orderly worker shutdown."""
        self._project_health("stopped", None, self._expected_codex_session_id)


class AnalyticsSubprocessSupervisor:
    """Own an optional worker with one bounded restart for this runtime."""

    def __init__(
        self,
        config: AnalyticsWorkerConfig,
        *,
        python_executable: str = sys.executable,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        restart_delay_seconds: float = ANALYTICS_RESTART_DELAY_SECONDS,
        max_start_attempts: int = 2,
    ) -> None:
        if max_start_attempts < 1:
            raise ValueError("analytics supervisor requires a start attempt")
        if restart_delay_seconds < 0:
            raise ValueError("analytics restart delay cannot be negative")
        self._config = config
        self._python_executable = python_executable
        self._popen = popen
        self._monotonic = monotonic
        self._restart_delay_seconds = restart_delay_seconds
        self._max_start_attempts = max_start_attempts
        self._start_attempts = 0
        self._exhausted = False
        self._process: subprocess.Popen[bytes] | None = None
        self._next_start_at = 0.0

    def poll(self) -> None:
        if self._exhausted:
            return
        now = self._monotonic()
        process = self._process
        if process is not None and process.poll() is None:
            return
        if process is not None:
            self._process = None
            self._next_start_at = now + self._restart_delay_seconds
            retry_scheduled = self._start_attempts < self._max_start_attempts
            _project_supervisor_health(
                self._config,
                "analytics_worker_exited",
                retry_scheduled=retry_scheduled,
                retry_delay_seconds=self._restart_delay_seconds,
            )
            if not retry_scheduled:
                self._exhausted = True
                return
        if now < self._next_start_at:
            return
        self._start_attempts += 1
        try:
            self._process = self._popen(
                self._config.command(self._python_executable),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            self._next_start_at = now + self._restart_delay_seconds
            retry_scheduled = self._start_attempts < self._max_start_attempts
            _project_supervisor_health(
                self._config,
                "analytics_worker_start_failed",
                retry_scheduled=retry_scheduled,
                retry_delay_seconds=self._restart_delay_seconds,
            )
            if not retry_scheduled:
                self._exhausted = True

    def close(self) -> None:
        self._exhausted = True
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=1)
        except (OSError, subprocess.SubprocessError):
            with suppress(OSError, subprocess.SubprocessError):
                process.kill()
                process.wait(timeout=1)


def default_codex_sessions_root() -> Path:
    configured = os.environ.get("RODEX_CODEX_SESSIONS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    codex_root = os.environ.get("CODEX_HOME")
    root = Path(codex_root).expanduser() if codex_root else Path.home() / ".codex"
    return (root / "sessions").resolve()


def locate_verified_rollout(
    root: Path,
    codex_session_id: CodexSessionId,
    *,
    first_linked_at_utc: str | None = None,
    source_catalog: AnalyticsSourceCatalog | None = None,
) -> VerifiedRollout | None:
    """Find the rollout whose metadata confirms the exact Codex session ID."""
    catalog = source_catalog or AnalyticsSourceCatalog(root)
    verified = [
        source
        for path in catalog.candidate_paths(
            codex_session_id, first_linked_at_utc=first_linked_at_utc
        )
        if (source := _verify_root_rollout(path, codex_session_id, allowed_root=root))
        is not None
    ]
    if len(verified) > 1:
        raise RodexAnalyticsError(
            f"multiple rollout files declare Codex identity {codex_session_id}"
        )
    if not verified:
        return None
    catalog.remember_resolved_path(codex_session_id, verified[0].path)
    return verified[0]


def analytics_worker_main(arguments: list[str] | None = None) -> int:
    config = AnalyticsWorkerConfig.parse(arguments)
    _lower_process_priority()
    stop = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)
    worker = AnalyticsRolloutWorker(config)
    worker.run_until_stopped(stop)
    worker.mark_stopped()
    return 0


def _verify_root_rollout(
    path: str | Path,
    expected: CodexThreadId,
    *,
    allowed_root: Path | None = None,
) -> VerifiedRollout | None:
    try:
        source_path = resolve_rollout_path(path, allowed_root=allowed_root)
        descriptor = open_rollout_descriptor(source_path)
        with os.fdopen(descriptor, encoding="utf-8") as records:
            state = os.fstat(records.fileno())
            for _, line in zip(range(32), records, strict=False):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if (
                    not isinstance(payload, dict)
                    or payload.get("id") != str(expected)
                    or payload.get("session_id") != str(expected)
                    or payload.get("thread_source") == "subagent"
                ):
                    return None
                timestamp = payload.get("timestamp") or record.get("timestamp")
                if not isinstance(timestamp, str) or not timestamp:
                    return None
                return VerifiedRollout(
                    source_path,
                    state.st_size,
                    state.st_mtime_ns,
                    parse_codex_thread_id(expected),
                    "root",
                    None,
                    0,
                    None,
                    None,
                    None,
                    timestamp,
                )
    except (OSError, UnicodeError, AnalyticsSourceReadError, RodexAnalyticsError):
        return None
    return None


def _verify_subagent_rollout(
    path: str | Path,
    root_thread_ids: frozenset[CodexThreadId],
    *,
    allowed_root: Path,
) -> VerifiedRollout | None:
    """Authenticate the self-describing hierarchy metadata of one child rollout."""
    try:
        source_path = resolve_rollout_path(path, allowed_root=allowed_root)
        descriptor = open_rollout_descriptor(source_path)
        with os.fdopen(descriptor, encoding="utf-8") as records:
            state = os.fstat(records.fileno())
            line = next(records)
        record = json.loads(line)
        payload = record.get("payload") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or record.get("type") != "session_meta"
            or not isinstance(payload, dict)
            or payload.get("thread_source") != "subagent"
        ):
            return None
        root_thread_id = parse_codex_thread_id(payload.get("session_id"))
        if root_thread_id not in root_thread_ids:
            return None
        thread_id = parse_codex_thread_id(payload.get("id"))
        parent_thread_id = parse_codex_thread_id(payload.get("parent_thread_id"))
        if payload.get("forked_from_id") != str(parent_thread_id):
            return None
        source = payload.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        if not isinstance(spawn, dict):
            return None
        depth = spawn.get("depth")
        agent_path = payload.get("agent_path")
        nickname = payload.get("agent_nickname")
        cutoff = payload.get("subagent_history_start_ordinal")
        timestamp = payload.get("timestamp") or record.get("timestamp")
        if (
            not isinstance(depth, int)
            or isinstance(depth, bool)
            or depth <= 0
            or not isinstance(agent_path, str)
            or not agent_path
            or (nickname is not None and (not isinstance(nickname, str) or not nickname))
            or not isinstance(cutoff, int)
            or isinstance(cutoff, bool)
            or cutoff < 0
            or not isinstance(timestamp, str)
            or not timestamp
            or spawn.get("parent_thread_id") != str(parent_thread_id)
            or spawn.get("agent_path") != agent_path
            or spawn.get("agent_nickname") != nickname
        ):
            return None
        return VerifiedRollout(
            source_path,
            state.st_size,
            state.st_mtime_ns,
            thread_id,
            "subagent",
            parent_thread_id,
            depth,
            agent_path,
            nickname,
            cutoff,
            timestamp,
        )
    except (
        OSError,
        StopIteration,
        TypeError,
        UnicodeError,
        ValueError,
        AnalyticsSourceReadError,
        RodexAnalyticsError,
    ):
        return None


def _discover_verified_thread_rollouts(
    root: Path,
    registered_sources: Sequence[RodexSessionStatisticsSource],
    source_catalog: AnalyticsSourceCatalog,
    *,
    verified_cache: Mapping[CodexThreadId, VerifiedRollout] | None = None,
) -> list[VerifiedRollout] | None:
    """Discover the authenticated descendant closure for registered root history."""
    registered = {source.codex_thread_id: source for source in registered_sources}
    root_sources = tuple(
        source for source in registered_sources if source.source_kind == "root"
    )
    if not root_sources:
        return None
    cached = {} if verified_cache is None else verified_cache
    closure: dict[CodexThreadId, VerifiedRollout] = {}
    for root_source in root_sources:
        root_verified = cached.get(root_source.codex_thread_id)
        if root_verified is not None and (
            root_verified.source_kind != "root"
            or (
                root_source.rollout_file_path is not None
                and root_verified.path != Path(root_source.rollout_file_path)
            )
        ):
            root_verified = None
        if root_verified is None and root_source.rollout_file_path is not None:
            root_verified = _verify_root_rollout(
                root_source.rollout_file_path,
                root_source.codex_thread_id,
                allowed_root=root,
            )
        if root_verified is None:
            root_verified = locate_verified_rollout(
                root,
                root_source.codex_thread_id,
                first_linked_at_utc=root_source.first_linked_at_utc,
                source_catalog=source_catalog,
            )
        if root_verified is None:
            return None
        closure[root_source.codex_thread_id] = replace(
            root_verified, first_linked_at_utc=root_source.first_linked_at_utc
        )
    candidates: dict[CodexThreadId, VerifiedRollout] = {}
    root_thread_ids = frozenset(source.codex_thread_id for source in root_sources)
    candidate_thread_ids = set(source_catalog.candidate_thread_ids())
    candidate_thread_ids.update(registered)
    candidate_thread_ids.difference_update(root_thread_ids)
    for thread_id in candidate_thread_ids:
        cached_candidate = cached.get(thread_id)
        if cached_candidate is not None and cached_candidate.source_kind == "subagent":
            candidates[thread_id] = cached_candidate
            continue
        registered_source = registered.get(thread_id)
        known_path = (
            None
            if registered_source is None or registered_source.rollout_file_path is None
            else Path(registered_source.rollout_file_path)
        )
        paths = (
            (known_path,)
            if known_path is not None
            else source_catalog.candidate_paths(
                thread_id,
                first_linked_at_utc=(
                    None
                    if registered_source is None
                    else registered_source.first_linked_at_utc
                ),
            )
        )
        verified_for_thread = [
            candidate
            for path in paths
            if (
                candidate := _verify_subagent_rollout(
                    path, root_thread_ids, allowed_root=root
                )
            )
            is not None
            and candidate.codex_thread_id == thread_id
        ]
        if len(verified_for_thread) > 1:
            raise RodexAnalyticsError(
                f"multiple rollout files declare Codex thread {thread_id}"
            )
        if verified_for_thread:
            candidate = verified_for_thread[0]
            candidates[thread_id] = candidate
            source_catalog.remember_resolved_path(thread_id, candidate.path)
    pending = dict(candidates)
    while pending:
        added = False
        for thread_id, candidate in tuple(pending.items()):
            parent = closure.get(candidate.parent_codex_thread_id)
            if parent is None:
                continue
            if candidate.thread_depth != parent.thread_depth + 1:
                raise RodexAnalyticsError(
                    f"sub-agent thread depth disagrees with parent: {thread_id}"
                )
            closure[thread_id] = candidate
            del pending[thread_id]
            added = True
        if not added:
            break

    if not set(registered).issubset(closure):
        return None
    for thread_id, source in registered.items():
        candidate = closure[thread_id]
        candidate_metadata = (
            candidate.source_kind,
            candidate.parent_codex_thread_id,
            candidate.thread_depth,
            candidate.agent_path,
            candidate.agent_nickname,
            candidate.subagent_history_start_ordinal,
        )
        parent_source = (
            None
            if source.parent_rodex_sessions_statistics_sources_id is None
            else next(
                (
                    item
                    for item in registered_sources
                    if item.id == source.parent_rodex_sessions_statistics_sources_id
                ),
                None,
            )
        )
        stored_metadata = (
            source.source_kind,
            None if parent_source is None else parent_source.codex_thread_id,
            source.thread_depth,
            source.agent_path,
            source.agent_nickname,
            source.subagent_history_start_ordinal,
        )
        if candidate_metadata != stored_metadata:
            raise RodexAnalyticsError(
                f"stored hierarchy disagrees with rollout thread {thread_id}"
            )
        closure[thread_id] = replace(
            candidate, first_linked_at_utc=source.first_linked_at_utc
        )
    return sorted(
        closure.values(), key=lambda item: (item.thread_depth, str(item.codex_thread_id))
    )


def _derive_verified_collaboration_projection(
    projection: SessionStatisticsProjection,
    *,
    analyzed_sources: Sequence[RodexSessionStatisticsSourceObservation],
) -> VerifiedCollaborationProjection:
    """Derive exact-turn and team collaboration from authenticated source truth."""
    sources = tuple(analyzed_sources)
    sources_by_thread = {item.codex_thread_id: item for item in sources}
    if len(sources_by_thread) != len(sources):
        raise RodexAnalyticsError(
            "verified collaboration sources contain a duplicate thread"
        )

    spawning_turn_id_by_child_thread: dict[CodexThreadId, str] = {}
    for child in sources:
        parent_thread_id = child.parent_codex_thread_id
        if parent_thread_id is None:
            continue
        if parent_thread_id not in sources_by_thread:
            raise RodexAnalyticsError(
                f"verified sub-agent has no parent source: {child.codex_thread_id}"
            )
        linked_at = _collaboration_timestamp(
            child.first_linked_at_utc,
            f"sub-agent {child.codex_thread_id} first-linked time",
        )
        owners = [
            turn
            for turn in projection.turn_statistics
            if turn.codex_thread_id == parent_thread_id
            and turn.started_at_utc is not None
            and _collaboration_timestamp(
                turn.started_at_utc,
                f"turn {turn.codex_turn_id} start time",
            )
            <= linked_at
            and (
                turn.terminal_at_utc is None
                or linked_at
                <= _collaboration_timestamp(
                    turn.terminal_at_utc,
                    f"turn {turn.codex_turn_id} terminal time",
                )
            )
        ]
        if len(owners) != 1:
            raise RodexAnalyticsError(
                "verified sub-agent must belong to exactly one direct-parent turn: "
                f"{child.codex_thread_id}"
            )
        owner = owners[0]
        spawning_turn_id_by_child_thread[child.codex_thread_id] = owner.codex_turn_id

    children_started_by_turn: dict[tuple[CodexThreadId, str], int] = {}
    for child in sources:
        parent_thread_id = child.parent_codex_thread_id
        if parent_thread_id is None:
            continue
        spawning_turn_id = spawning_turn_id_by_child_thread[child.codex_thread_id]
        spawning_turn_key = (parent_thread_id, spawning_turn_id)
        children_started_by_turn[spawning_turn_key] = (
            children_started_by_turn.get(spawning_turn_key, 0) + 1
        )

    projected_turns = tuple(
        _derive_verified_turn_collaboration(
            turn,
            agents_started=children_started_by_turn.get(
                (turn.codex_thread_id, turn.codex_turn_id), 0
            ),
        )
        for turn in projection.turn_statistics
    )
    by_tool = _canonical_collaboration_counts(projection.named_counts)
    turn_by_tool: dict[str, int] = {}
    for turn in projected_turns:
        for item in turn.named_counts:
            if item.count_kind != "collaboration_tool":
                continue
            turn_by_tool[item.count_name] = (
                turn_by_tool.get(item.count_name, 0) + item.occurrence_count
            )
    aggregate_by_tool = {item.count_name: item.occurrence_count for item in by_tool}
    if turn_by_tool != aggregate_by_tool:
        raise RodexAnalyticsError(
            "aggregate collaboration tools disagree with exact-turn model tools"
        )
    descendant_count = sum(item.parent_codex_thread_id is not None for item in sources)
    if sum(children_started_by_turn.values()) != descendant_count:
        raise RodexAnalyticsError(
            "verified sub-agent count disagrees with exact-turn ownership"
        )
    named_counts = _replace_collaboration_counts(projection.named_counts, by_tool)
    return VerifiedCollaborationProjection(
        statistics_projection=replace(
            projection,
            collaboration_operations_count=sum(item.occurrence_count for item in by_tool),
            collaboration_agents_started_count=descendant_count,
            named_counts=named_counts,
            turn_statistics=projected_turns,
        ),
        analyzed_sources=tuple(
            replace(
                source,
                spawning_codex_turn_id=spawning_turn_id_by_child_thread.get(
                    source.codex_thread_id
                ),
            )
            for source in sources
        ),
    )


def _derive_verified_turn_collaboration(
    turn: TurnStatisticsProjection,
    *,
    agents_started: int,
) -> TurnStatisticsProjection:
    """Replace one analyzer turn's legacy collaboration view with canonical facts."""
    by_tool = _canonical_collaboration_counts(turn.named_counts)
    return replace(
        turn,
        collaboration_operations_count=sum(item.occurrence_count for item in by_tool),
        collaboration_agents_started_count=agents_started,
        named_counts=_replace_collaboration_counts(turn.named_counts, by_tool),
    )


def _canonical_collaboration_counts(
    named_counts: Sequence[StatisticsNamedCount],
) -> tuple[StatisticsNamedCount, ...]:
    return tuple(
        StatisticsNamedCount(
            count_kind="collaboration_tool",
            count_name=item.count_name,
            occurrence_count=item.occurrence_count,
        )
        for item in named_counts
        if item.count_kind == "model_tool"
        and item.count_name in COLLABORATION_MODEL_TOOL_NAMES
    )


def _replace_collaboration_counts(
    named_counts: Sequence[StatisticsNamedCount],
    canonical_counts: Sequence[StatisticsNamedCount],
) -> tuple[StatisticsNamedCount, ...]:
    return tuple(
        item for item in named_counts if item.count_kind != "collaboration_tool"
    ) + tuple(canonical_counts)


def _collaboration_timestamp(value: str, description: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise RodexAnalyticsError(f"invalid {description}") from error
    if parsed.tzinfo is None:
        raise RodexAnalyticsError(f"invalid {description}")
    return parsed.astimezone(UTC)


def _stable_rollout_read(
    verified: VerifiedRollout,
    captured: AnalyticsSourceRead,
    verified_at_utc: str,
) -> StableRolloutRead:
    authenticated = captured.authenticated_source
    return StableRolloutRead(
        analyzer_content=captured.analyzer_content,
        appended_analyzer_content=captured.appended_analyzer_content,
        prepared_read=captured,
        observation=RodexSessionStatisticsSourceObservation(
            codex_thread_id=verified.codex_thread_id,
            source_kind=verified.source_kind,
            parent_codex_thread_id=verified.parent_codex_thread_id,
            thread_depth=verified.thread_depth,
            agent_path=verified.agent_path,
            agent_nickname=verified.agent_nickname,
            subagent_history_start_ordinal=(verified.subagent_history_start_ordinal),
            spawning_codex_turn_id=None,
            first_linked_at_utc=verified.first_linked_at_utc,
            rollout_file_path=authenticated.path,
            analyzed_size_bytes=authenticated.analyzed_size_bytes,
            analyzed_mtime_ns=authenticated.source_mtime_ns,
            analyzed_prefix_sha256=authenticated.analyzed_prefix_sha256,
            verified_at_utc=verified_at_utc,
        ),
        authenticated_source=authenticated,
    )


def _view_matches_source_reads(
    statistics: RodexAnalyticsStatisticsCheckpoint | None,
    sources: Sequence[RodexSessionStatisticsSource],
    reads: Sequence[StableRolloutRead],
) -> bool:
    if (
        statistics is None
        or statistics.statistics_projection_schema_version
        != STATISTICS_PROJECTION_SCHEMA_VERSION
        or len(sources) != len(reads)
    ):
        return False
    sources_by_thread = {source.codex_thread_id: source for source in sources}
    for read in reads:
        source = sources_by_thread.get(read.observation.codex_thread_id)
        if source is None or (
            source.rollout_file_path != str(read.observation.rollout_file_path)
            or source.analyzed_size_bytes != read.observation.analyzed_size_bytes
            or source.analyzed_prefix_sha256 != read.observation.analyzed_prefix_sha256
        ):
            return False
    return True


def _current_analytics_user_id() -> str:
    identity = current_rodex_sessions_user_identity()
    return f"posix:{identity.uid}:{identity.gid}:{identity.user_name}"


def _lower_process_priority() -> None:
    with suppress(OSError):
        os.nice(19)
    ionice = shutil.which("ionice")
    if ionice is not None:
        subprocess.run(
            [ionice, "-c", "3", "-p", str(os.getpid())],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _diagnostic_code(error: Exception) -> str:
    if isinstance(error, (RodexAnalyticsError, AnalyticsSourceReadError)):
        return "analytics_error"
    if isinstance(error, OSError):
        return "analytics_io_error"
    return "analytics_internal_error"


def _project_supervisor_health(
    config: AnalyticsWorkerConfig,
    code: str,
    *,
    retry_scheduled: bool,
    retry_delay_seconds: float,
) -> None:
    if not config.is_activated:
        return
    assert config.rodex_sessions_id is not None
    assert config.codex_session_id is not None
    try:
        registry = RodexAnalyticsRegistry.open(
            config.rodex_database_path,
            session_id=config.rodex_sessions_id,
            expected_codex_session_id=config.codex_session_id,
        )
        now = datetime.now(UTC)
        registry.record_health_transition(
            worker_state="degraded",
            diagnostic_code=code,
            attempted_at_utc=now.isoformat(timespec="microseconds"),
            failed=True,
            next_retry_at_utc=(
                (now + timedelta(seconds=retry_delay_seconds)).isoformat(
                    timespec="microseconds"
                )
                if retry_scheduled
                else None
            ),
        )
    except Exception:
        return
