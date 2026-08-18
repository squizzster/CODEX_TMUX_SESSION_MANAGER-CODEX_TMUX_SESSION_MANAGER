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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from rodex_registry import (
    CodexSessionId,
    RodexSessionStatistics,
    RodexSessionStatisticsSource,
    RodexSessionStatisticsSourceObservation,
    SessionStatisticsProjection,
    StatisticsProjectionError,
    current_rodex_sessions_user_identity,
    lookup_codex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_sessions_id_from_a_rodex_session_id,
    parse_session_statistics_snapshot,
    publish_rodex_session_statistics,
    read_rodex_session_statistics,
    record_rodex_session_statistics_worker_health,
)
from rodex_sql import RodexDatabaseNotFoundError

from .process_contracts import AnalyticsWorkerConfig

ANALYTICS_POLL_INTERVAL_SECONDS = 0.5
ANALYTICS_RESTART_DELAY_SECONDS = 2.0
STATISTICS_PROJECTION_SCHEMA_VERSION = "rodex-statistics-v4"


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
    """A rollout whose internal session metadata matches the requested Codex session ID."""

    path: Path
    size_bytes: int
    modified_at_ns: int


@dataclass(frozen=True, slots=True)
class AnalyticsCalculation:
    """Usable session and turn projections from one analyzer calculation."""

    statistics_projection: SessionStatisticsProjection
    coverage_state: str


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
        self._source_authentication: dict[CodexSessionId, AuthenticatedRolloutPrefix] = {}

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
                publish_rodex_session_statistics(
                    session_id,
                    self._config.rodex_database_path,
                    expected_current_codex_session_id=codex_session_id,
                    based_on_statistics_revision=(
                        None
                        if view.statistics is None
                        else view.statistics.statistics_revision
                    ),
                    statistics_projection_schema_version=(
                        STATISTICS_PROJECTION_SCHEMA_VERSION
                    ),
                    calculated_at_utc=self._timestamp(),
                    coverage_state=calculation.coverage_state,
                    statistics_projection=calculation.statistics_projection,
                    analyzed_sources=[item.observation for item in stable_copies],
                )
                self._source_authentication = {
                    item.observation.codex_session_id: authenticated
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
        copies: list[StableRolloutCopy] = []
        for index, source in enumerate(sources):
            verified = (
                None
                if source.rollout_file_path is None
                else _verify_recorded_source(
                    source.rollout_file_path,
                    source.codex_session_id,
                    allowed_root=self._config.codex_sessions_root,
                )
            )
            if verified is None:
                verified = locate_verified_rollout(
                    self._config.codex_sessions_root,
                    source.codex_session_id,
                )
            if verified is None:
                return None
            copies.append(
                _copy_complete_rollout_prefix(
                    verified.path,
                    source.codex_session_id,
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
        revision = statistics.statistics_revision
        for source in sources:
            if (
                source.included_statistics_revision != revision
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
            authenticated = self._source_authentication.get(source.codex_session_id)
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
                        source.codex_session_id,
                        allowed_root=self._config.codex_sessions_root,
                    )
                except RodexAnalyticsError:
                    return False
                self._source_authentication[source.codex_session_id] = authenticated
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
        if (source := _verify_recorded_source(path, codex_session_id, allowed_root=root))
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


def _verify_recorded_source(
    path: str | Path,
    expected: CodexSessionId,
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
                if not isinstance(payload, dict) or payload.get("id") != str(expected):
                    return None
                return VerifiedRollout(source_path, state.st_size, state.st_mtime_ns)
    except (OSError, UnicodeError, RodexAnalyticsError):
        return None
    return None


def _copy_complete_rollout_prefix(
    source_path: Path,
    expected_codex_session_id: CodexSessionId,
    temporary_path: Path,
    verified_at_utc: str,
) -> StableRolloutCopy:
    authenticated, analyzed = _authenticate_rollout_prefix(
        source_path, expected_codex_session_id
    )
    try:
        output_descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(output_descriptor, "wb") as output:
            output.write(analyzed)
    except OSError as error:
        raise RodexAnalyticsError(f"could not stage rollout prefix: {error}") from error
    return StableRolloutCopy(
        temporary_path=temporary_path,
        observation=RodexSessionStatisticsSourceObservation(
            codex_session_id=expected_codex_session_id,
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
    expected_codex_session_id: CodexSessionId,
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
    if not _rollout_content_declares_codex_session_id(analyzed, expected_codex_session_id):
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


def _rollout_content_declares_codex_session_id(
    content: bytes, expected: CodexSessionId
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
        source.observation.codex_session_id,
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
