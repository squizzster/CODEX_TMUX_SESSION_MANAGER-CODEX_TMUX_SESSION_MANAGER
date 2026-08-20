"""Fail-open, aggregate-only analytics for managed Codex rollout files."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import signal
import stat as stat_module
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from rodex_registry import (
    COLLABORATION_MODEL_TOOL_NAMES,
    CodexSessionId,
    CodexThreadId,
    RodexSessionStatistics,
    RodexSessionStatisticsSource,
    RodexSessionStatisticsSourceObservation,
    SessionStatisticsProjection,
    StatisticsNamedCount,
    StatisticsProjectionError,
    TurnStatisticsProjection,
    current_rodex_sessions_user_identity,
    lookup_codex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_sessions_id_from_a_rodex_session_id,
    parse_codex_thread_id,
    parse_session_statistics_snapshot,
    publish_rodex_session_statistics,
    read_rodex_session_statistics,
    record_rodex_session_statistics_worker_health,
)
from rodex_sql import RodexDatabaseNotFoundError

from .process_contracts import AnalyticsWorkerConfig

ANALYTICS_POLL_INTERVAL_SECONDS = 0.5
ANALYTICS_RESTART_DELAY_SECONDS = 2.0
STATISTICS_PROJECTION_SCHEMA_VERSION = "rodex-statistics-v6"


class RodexAnalyticsError(RuntimeError):
    """The optional analytics subsystem could not satisfy a request."""


class _AnalyzerLibrary(Protocol):
    def create_new_codex_protocol_id(self, user_id: str) -> object: ...

    def load_file(self, protocol_id: str, path: Path) -> object: ...

    def get_stats(
        self, protocol_id: str, *, include_turn_statistics: bool = False
    ) -> object: ...

    def close(self) -> object: ...


class AnalyticsBoundary(Protocol):
    def analyze_rollouts(
        self, paths: Sequence[Path], user_id: str
    ) -> AnalyticsCalculation: ...


AnalyticsBoundaryFactory = Callable[[], AnalyticsBoundary]


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
class AnalyticsCalculation:
    """Usable session and turn projections from one analyzer calculation."""

    statistics_projection: SessionStatisticsProjection
    coverage_state: str


@dataclass(frozen=True, slots=True)
class VerifiedCollaborationProjection:
    """Canonical collaboration facts joined to authenticated source lineage."""

    statistics_projection: SessionStatisticsProjection
    analyzed_sources: tuple[RodexSessionStatisticsSourceObservation, ...]


@dataclass(frozen=True, slots=True)
class StableRolloutCopy:
    """A private complete-line prefix plus the exact source state it represents."""

    temporary_path: Path
    observation: RodexSessionStatisticsSourceObservation
    authenticated_source: AuthenticatedRolloutPrefix


@dataclass(frozen=True, slots=True)
class AuthenticatedRolloutPrefix:
    """Filesystem identity and digest of the current complete-record prefix."""

    path: Path
    source_device: int
    source_inode: int
    source_size_bytes: int
    source_mtime_ns: int
    source_ctime_ns: int
    analyzed_size_bytes: int
    analyzed_prefix_sha256: str


class CodexProtocolAnalyticsAdapter:
    """Small seam around one temporary in-memory analyzer calculation."""

    def analyze_rollouts(self, paths: Sequence[Path], user_id: str) -> AnalyticsCalculation:
        try:
            module = importlib.import_module("codex_protocol_log_analyzer")
            library: _AnalyzerLibrary = module.CodexProtocolLibrary()
        except Exception as error:
            raise RodexAnalyticsError(
                f"could not initialize Codex protocol analytics: {error}"
            ) from error
        try:
            protocol_id = _protocol_id(
                _operation_value(
                    library.create_new_codex_protocol_id(user_id),
                    "create temporary analytics dataset",
                )
            )
            coverage_state = "complete"
            for path in paths:
                loaded = library.load_file(protocol_id, path)
                _operation_value(
                    loaded,
                    "load verified Codex rollout",
                    allow_partial=True,
                )
                if getattr(loaded, "status", "ok") != "ok":
                    coverage_state = "gapped"
            stats_result = library.get_stats(protocol_id, include_turn_statistics=True)
            stats = _mapping_value(
                _operation_value(
                    stats_result,
                    "calculate aggregate statistics",
                    allow_partial=True,
                )
            )
            if getattr(stats_result, "status", "ok") != "ok":
                coverage_state = "gapped"
            try:
                projection = parse_session_statistics_snapshot(stats)
            except StatisticsProjectionError as error:
                raise RodexAnalyticsError(
                    f"analyzer statistics contract mismatch: {error}"
                ) from error
            return AnalyticsCalculation(
                statistics_projection=projection,
                coverage_state=coverage_state,
            )
        finally:
            with suppress(Exception):
                library.close()


class AnalyticsRolloutWorker:
    """Watch verified rollouts and project aggregate statistics into Rodex SQLite."""

    def __init__(
        self,
        config: AnalyticsWorkerConfig,
        *,
        adapter_factory: AnalyticsBoundaryFactory = CodexProtocolAnalyticsAdapter,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._adapter_factory = adapter_factory
        self._now = now
        self._session_id: int | None = None
        self._expected_codex_session_id: CodexSessionId | None = None
        self._source_authentication: dict[CodexThreadId, AuthenticatedRolloutPrefix] = {}

    def poll_once(self) -> str:
        """Perform one reconciliation; no analytics failure is allowed to escape."""
        expected_codex_session_id: CodexSessionId | None = None
        try:
            session_id = lookup_rodex_sessions_id_from_a_rodex_session_id(
                self._config.rodex_session_id, self._config.rodex_database_path
            )
            self._session_id = session_id
            if session_id is None:
                return "catching_up"
            codex_session_id = lookup_codex_session_id_from_a_rodex_sessions_id(
                session_id, self._config.rodex_database_path
            )
            expected_codex_session_id = codex_session_id
            if codex_session_id is None:
                return "catching_up"
            self._expected_codex_session_id = codex_session_id
            view = read_rodex_session_statistics(
                session_id, self._config.rodex_database_path
            )
            if self._view_matches_live_sources(view.statistics, view.sources):
                if view.worker is None or view.worker.worker_state != "up_to_date":
                    self._project_health("up_to_date", None, codex_session_id)
                return "up_to_date"
            self._project_health("catching_up", None, codex_session_id)
            with tempfile.TemporaryDirectory(prefix="rodex-analytics-") as temporary:
                stable_copies = self._copy_registered_sources(view.sources, Path(temporary))
                if stable_copies is None:
                    self._project_health(
                        "catching_up", "rollout_not_found", codex_session_id
                    )
                    return "catching_up"
                calculation = self._adapter_factory().analyze_rollouts(
                    [item.temporary_path for item in stable_copies],
                    _current_analytics_user_id(),
                )
                authenticated_sources = [
                    _verify_source_unchanged(item) for item in stable_copies
                ]
                verified_collaboration = _derive_verified_collaboration_projection(
                    calculation.statistics_projection,
                    analyzed_sources=tuple(item.observation for item in stable_copies),
                )
                publish_rodex_session_statistics(
                    session_id,
                    self._config.rodex_database_path,
                    expected_current_codex_session_id=codex_session_id,
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
                    statistics_projection=(verified_collaboration.statistics_projection),
                    analyzed_sources=verified_collaboration.analyzed_sources,
                )
                self._source_authentication = {
                    item.observation.codex_thread_id: authenticated
                    for item, authenticated in zip(
                        stable_copies, authenticated_sources, strict=True
                    )
                }
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
            return "degraded"

    def run_until_stopped(
        self,
        stop: Event,
        *,
        poll_interval_seconds: float = ANALYTICS_POLL_INTERVAL_SECONDS,
    ) -> None:
        while not stop.is_set():
            state = self.poll_once()
            stop.wait(
                ANALYTICS_RESTART_DELAY_SECONDS
                if state == "degraded"
                else poll_interval_seconds
            )

    def _copy_registered_sources(
        self,
        sources: Sequence[RodexSessionStatisticsSource],
        temporary_root: Path,
    ) -> list[StableRolloutCopy] | None:
        verified_sources = _discover_verified_thread_rollouts(
            self._config.codex_sessions_root,
            sources,
        )
        if verified_sources is None:
            return None
        copies: list[StableRolloutCopy] = []
        for index, verified in enumerate(verified_sources):
            copies.append(
                _copy_complete_rollout_prefix(
                    verified,
                    temporary_root / f"source-{index}.jsonl",
                    self._timestamp(),
                )
            )
        return copies

    def _view_matches_live_sources(
        self,
        statistics: RodexSessionStatistics | None,
        sources: Sequence[RodexSessionStatisticsSource],
    ) -> bool:
        if (
            statistics is None
            or statistics.statistics_projection_schema_version
            != STATISTICS_PROJECTION_SCHEMA_VERSION
            or not sources
        ):
            return False
        publication_sequence = statistics.statistics_publication_sequence
        for source in sources:
            if (
                source.included_statistics_publication_sequence != publication_sequence
                or source.rollout_file_path is None
                or source.analyzed_size_bytes is None
                or source.analyzed_mtime_ns is None
                or source.analyzed_prefix_sha256 is None
            ):
                return False
            path = Path(source.rollout_file_path)
            try:
                stat = path.stat()
            except OSError:
                return False
            authenticated = self._source_authentication.get(source.codex_thread_id)
            if authenticated is None or (
                authenticated.path,
                authenticated.source_device,
                authenticated.source_inode,
                authenticated.source_size_bytes,
                authenticated.source_mtime_ns,
                authenticated.source_ctime_ns,
            ) != (
                path,
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            ):
                try:
                    authenticated, _ = _authenticate_rollout_prefix(
                        path,
                        source.codex_thread_id,
                        allowed_root=self._config.codex_sessions_root,
                    )
                except RodexAnalyticsError:
                    return False
                self._source_authentication[source.codex_thread_id] = authenticated
            if (
                authenticated.analyzed_size_bytes != source.analyzed_size_bytes
                or authenticated.analyzed_prefix_sha256 != source.analyzed_prefix_sha256
            ):
                return False
        return True

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
            view = read_rodex_session_statistics(
                session_id, self._config.rodex_database_path
            )
            prior_failures = 0 if view.worker is None else view.worker.consecutive_failures
            failures = prior_failures + 1 if failed else 0
            now = self._now()
            record_rodex_session_statistics_worker_health(
                session_id,
                self._config.rodex_database_path,
                expected_current_codex_session_id=expected_codex_session_id,
                worker_state=state,
                diagnostic_code=diagnostic_code,
                last_attempted_at_utc=now.isoformat(timespec="microseconds"),
                consecutive_failures=failures,
                next_retry_at_utc=(
                    (now + timedelta(seconds=ANALYTICS_RESTART_DELAY_SECONDS)).isoformat(
                        timespec="microseconds"
                    )
                    if failed
                    else None
                ),
            )
        except Exception:
            return

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="microseconds")

    def mark_stopped(self) -> None:
        """Best-effort terminal health update for an orderly worker shutdown."""
        session_id = self._session_id
        if session_id is None:
            return
        self._project_health("stopped", None, self._expected_codex_session_id)


class AnalyticsSubprocessSupervisor:
    """Own and restart an optional worker without affecting the session host."""

    def __init__(
        self,
        config: AnalyticsWorkerConfig,
        *,
        python_executable: str = sys.executable,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        restart_delay_seconds: float = ANALYTICS_RESTART_DELAY_SECONDS,
    ) -> None:
        self._config = config
        self._python_executable = python_executable
        self._popen = popen
        self._monotonic = monotonic
        self._restart_delay_seconds = restart_delay_seconds
        self._process: subprocess.Popen[bytes] | None = None
        self._next_start_at = 0.0

    def poll(self) -> None:
        now = self._monotonic()
        process = self._process
        if process is not None and process.poll() is None:
            return
        if process is not None:
            self._process = None
            self._next_start_at = now + self._restart_delay_seconds
            _project_supervisor_health(self._config, "analytics_worker_exited")
        if now < self._next_start_at:
            return
        try:
            self._process = self._popen(
                self._config.command(self._python_executable),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            self._next_start_at = now + self._restart_delay_seconds
            _project_supervisor_health(self._config, "analytics_worker_start_failed")

    def close(self) -> None:
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
    root: Path, codex_session_id: CodexSessionId
) -> VerifiedRollout | None:
    """Find the rollout whose metadata confirms the exact Codex session ID."""
    if not root.is_dir():
        return None
    verified = [
        source
        for path in sorted(root.rglob(f"*{codex_session_id}*.jsonl"))
        if (source := _verify_root_rollout(path, codex_session_id, allowed_root=root))
        is not None
    ]
    if len(verified) > 1:
        raise RodexAnalyticsError(
            f"multiple rollout files declare Codex identity {codex_session_id}"
        )
    return verified[0] if verified else None


def analytics_worker_main(arguments: list[str] | None = None) -> int:
    config = AnalyticsWorkerConfig.parse(arguments)
    _lower_process_priority()
    stop = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)
    worker = AnalyticsRolloutWorker(config)
    try:
        worker.run_until_stopped(stop)
    finally:
        worker.mark_stopped()
    return 0


def _verify_root_rollout(
    path: str | Path,
    expected: CodexThreadId,
    *,
    allowed_root: Path | None = None,
) -> VerifiedRollout | None:
    try:
        source_path = _resolve_rollout_path(path, allowed_root=allowed_root)
        descriptor = _open_rollout_descriptor(source_path)
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
    except (OSError, UnicodeError, RodexAnalyticsError):
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
        source_path = _resolve_rollout_path(path, allowed_root=allowed_root)
        descriptor = _open_rollout_descriptor(source_path)
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
        RodexAnalyticsError,
    ):
        return None


def _discover_verified_thread_rollouts(
    root: Path,
    registered_sources: Sequence[RodexSessionStatisticsSource],
) -> list[VerifiedRollout] | None:
    """Discover the authenticated descendant closure for registered root history."""
    registered = {source.codex_thread_id: source for source in registered_sources}
    root_sources = tuple(
        source for source in registered_sources if source.source_kind == "root"
    )
    if not root_sources:
        return None
    closure: dict[CodexThreadId, VerifiedRollout] = {}
    for root_source in root_sources:
        root_verified = (
            None
            if root_source.rollout_file_path is None
            else _verify_root_rollout(
                root_source.rollout_file_path,
                root_source.codex_thread_id,
                allowed_root=root,
            )
        )
        if root_verified is None:
            root_verified = locate_verified_rollout(root, root_source.codex_thread_id)
        if root_verified is None:
            return None
        closure[root_source.codex_thread_id] = replace(
            root_verified, first_linked_at_utc=root_source.first_linked_at_utc
        )
    candidates: dict[CodexThreadId, VerifiedRollout] = {}
    if not root.is_dir():
        return None
    paths = tuple(sorted(root.rglob("*.jsonl")))
    root_thread_ids = frozenset(source.codex_thread_id for source in root_sources)
    for path in paths:
        candidate = _verify_subagent_rollout(path, root_thread_ids, allowed_root=root)
        if candidate is None:
            continue
        prior = candidates.get(candidate.codex_thread_id)
        if prior is not None and prior.path != candidate.path:
            raise RodexAnalyticsError(
                f"multiple rollout files declare Codex thread {candidate.codex_thread_id}"
            )
        candidates[candidate.codex_thread_id] = candidate
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


def _subagent_only_rollout_content(content: bytes, cutoff: int | None) -> bytes:
    """Remove the inherited parent prefix while retaining child session metadata."""
    if cutoff is None:
        raise RodexAnalyticsError("sub-agent rollout has no history cutoff")
    retained: list[bytes] = []
    for index, line in enumerate(content.splitlines(keepends=True)):
        if index == 0:
            retained.append(line)
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeError):
            continue
        ordinal = record.get("ordinal") if isinstance(record, dict) else None
        if isinstance(ordinal, int) and not isinstance(ordinal, bool) and ordinal > cutoff:
            retained.append(line)
    if len(retained) == 1:
        raise RodexAnalyticsError("sub-agent rollout contains no child history records")
    return b"".join(retained)


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


def _copy_complete_rollout_prefix(
    verified: VerifiedRollout,
    temporary_path: Path,
    verified_at_utc: str,
) -> StableRolloutCopy:
    authenticated, analyzed = _authenticate_rollout_prefix(
        verified.path, verified.codex_thread_id
    )
    staged = (
        analyzed
        if verified.source_kind == "root"
        else _subagent_only_rollout_content(
            analyzed, verified.subagent_history_start_ordinal
        )
    )
    try:
        output_descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(output_descriptor, "wb") as output:
            output.write(staged)
    except OSError as error:
        raise RodexAnalyticsError(f"could not stage rollout prefix: {error}") from error
    return StableRolloutCopy(
        temporary_path=temporary_path,
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


def _authenticate_rollout_prefix(
    source_path: Path,
    expected_codex_thread_id: CodexThreadId,
    *,
    allowed_root: Path | None = None,
) -> tuple[AuthenticatedRolloutPrefix, bytes]:
    try:
        resolved_source = _resolve_rollout_path(source_path, allowed_root=allowed_root)
        descriptor = _open_rollout_descriptor(resolved_source)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            content = source.read()
            after = os.fstat(source.fileno())
        path_state = os.stat(resolved_source, follow_symlinks=False)
    except (OSError, RodexAnalyticsError) as error:
        raise RodexAnalyticsError(f"could not authenticate rollout: {error}") from error
    identity = (before.st_dev, before.st_ino)
    if identity != (after.st_dev, after.st_ino) or identity != (
        path_state.st_dev,
        path_state.st_ino,
    ):
        raise RodexAnalyticsError("rollout source identity changed while reading")
    final_newline = content.rfind(b"\n")
    if final_newline < 0:
        raise RodexAnalyticsError("rollout contains no complete newline-terminated record")
    analyzed = content[: final_newline + 1]
    if not _rollout_content_declares_codex_thread_id(analyzed, expected_codex_thread_id):
        raise RodexAnalyticsError("rollout has an unexpected Codex identity")
    return (
        AuthenticatedRolloutPrefix(
            path=resolved_source,
            source_device=after.st_dev,
            source_inode=after.st_ino,
            source_size_bytes=after.st_size,
            source_mtime_ns=after.st_mtime_ns,
            source_ctime_ns=after.st_ctime_ns,
            analyzed_size_bytes=len(analyzed),
            analyzed_prefix_sha256=hashlib.sha256(analyzed).hexdigest(),
        ),
        analyzed,
    )


def _resolve_rollout_path(path: str | Path, *, allowed_root: Path | None = None) -> Path:
    candidate = Path(path).expanduser()
    state = candidate.lstat()
    if stat_module.S_ISLNK(state.st_mode):
        raise RodexAnalyticsError(f"rollout source is a symbolic link: {candidate}")
    resolved = candidate.resolve()
    if allowed_root is not None:
        root = allowed_root.expanduser().resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise RodexAnalyticsError(
                f"rollout source escapes the configured sessions root: {candidate}"
            ) from error
    return resolved


def _open_rollout_descriptor(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        state = os.fstat(descriptor)
        if not stat_module.S_ISREG(state.st_mode):
            raise RodexAnalyticsError(f"rollout source is not a regular file: {path}")
        if state.st_uid != os.getuid():
            raise RodexAnalyticsError(
                f"rollout source is not owned by uid {os.getuid()}: {path}"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _rollout_content_declares_codex_thread_id(
    content: bytes, expected: CodexThreadId
) -> bool:
    for line in content.splitlines()[:32]:
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        return isinstance(payload, dict) and payload.get("id") == str(expected)
    return False


def _verify_source_unchanged(
    source: StableRolloutCopy,
) -> AuthenticatedRolloutPrefix:
    current, _ = _authenticate_rollout_prefix(
        source.observation.rollout_file_path,
        source.observation.codex_thread_id,
    )
    original = source.authenticated_source
    if (
        current.source_device,
        current.source_inode,
        current.analyzed_size_bytes,
        current.analyzed_prefix_sha256,
    ) != (
        original.source_device,
        original.source_inode,
        original.analyzed_size_bytes,
        original.analyzed_prefix_sha256,
    ):
        raise RodexAnalyticsError(
            "rollout complete-record prefix changed during analytics calculation"
        )
    return current


def _operation_value(
    result: object, operation: str, *, allow_partial: bool = False
) -> object:
    value = getattr(result, "value", result)
    status = getattr(result, "status", "ok")
    if status != "fatal" and value is not None and (allow_partial or status != "error"):
        return value
    diagnostics = getattr(result, "diagnostics", ())
    detail = "; ".join(
        str(getattr(diagnostic, "message", diagnostic)) for diagnostic in diagnostics
    )
    raise RodexAnalyticsError(f"could not {operation}" + (f": {detail}" if detail else ""))


def _protocol_id(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        value = value.get("protocol_id")
    else:
        value = getattr(value, "protocol_id", None)
    if not isinstance(value, str) or not value:
        raise RodexAnalyticsError("analyzer returned no temporary protocol identity")
    return value


def _mapping_value(value: object) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        raise RodexAnalyticsError("analyzer returned an invalid statistics snapshot")
    return dict(value)


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
    if isinstance(error, RodexAnalyticsError):
        return "analytics_error"
    if isinstance(error, OSError):
        return "analytics_io_error"
    return "analytics_internal_error"


def _project_supervisor_health(config: AnalyticsWorkerConfig, code: str) -> None:
    session_id = lookup_rodex_sessions_id_from_a_rodex_session_id(
        config.rodex_session_id, config.rodex_database_path
    )
    if session_id is None:
        return
    try:
        expected_codex_session_id = lookup_codex_session_id_from_a_rodex_sessions_id(
            session_id, config.rodex_database_path
        )
        if expected_codex_session_id is None:
            return
        view = read_rodex_session_statistics(session_id, config.rodex_database_path)
        now = datetime.now(UTC)
        record_rodex_session_statistics_worker_health(
            session_id,
            config.rodex_database_path,
            expected_current_codex_session_id=expected_codex_session_id,
            worker_state="degraded",
            diagnostic_code=code,
            last_attempted_at_utc=now.isoformat(timespec="microseconds"),
            consecutive_failures=(
                1 if view.worker is None else view.worker.consecutive_failures + 1
            ),
            next_retry_at_utc=(
                now + timedelta(seconds=ANALYTICS_RESTART_DELAY_SECONDS)
            ).isoformat(timespec="microseconds"),
        )
    except Exception:
        return
