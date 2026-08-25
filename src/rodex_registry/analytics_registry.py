"""Single persistence boundary for one Rodex analytics worker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .identity import CodexSessionId, CodexThreadId, parse_codex_session_id
from .schema import existing_rodex_database_path
from .statistics import (
    RodexAnalyticsCheckpoint,
    RodexAnalyticsPublishReceipt,
    RodexSessionStatisticsSourceObservation,
    RodexSessionStatisticsWorker,
    publish_rodex_session_statistics,
    read_rodex_analytics_checkpoint,
    record_rodex_session_statistics_worker_health,
)
from .statistics_projection import SessionStatisticsProjection
from .validation import _validate_session_id


@dataclass(frozen=True, slots=True)
class RodexAnalyticsPublication:
    """One complete calculation offered to the analytics registry boundary."""

    based_on_statistics_publication_sequence: int | None
    statistics_projection_schema_version: str
    calculated_at_utc: str
    coverage_state: str
    statistics_projection: SessionStatisticsProjection
    analyzed_sources: tuple[RodexSessionStatisticsSourceObservation, ...]
    changed_source_thread_ids: frozenset[CodexThreadId] | None = None
    changed_turn_keys: frozenset[tuple[CodexThreadId, str]] | None = None
    removed_turn_keys: frozenset[tuple[CodexThreadId, str]] = frozenset()


class RodexAnalyticsRegistry:
    """Own analytics checkpoint, publication, and worker-health persistence."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        session_id: int,
        expected_codex_session_id: CodexSessionId | str,
    ) -> None:
        _validate_session_id(session_id)
        self._database_path = existing_rodex_database_path(database_path)
        self._session_id = session_id
        self._expected_codex_session_id = parse_codex_session_id(expected_codex_session_id)

    @classmethod
    def open(
        cls,
        database_path: str | os.PathLike[str],
        *,
        session_id: int,
        expected_codex_session_id: CodexSessionId | str,
    ) -> Self:
        """Open the existing registry and bind one immutable worker identity."""
        return cls(
            database_path,
            session_id=session_id,
            expected_codex_session_id=expected_codex_session_id,
        )

    @property
    def database_path(self) -> Path:
        return self._database_path

    @property
    def session_id(self) -> int:
        return self._session_id

    @property
    def expected_codex_session_id(self) -> CodexSessionId:
        return self._expected_codex_session_id

    def load_checkpoint(self) -> RodexAnalyticsCheckpoint:
        """Load the last committed analytics checkpoint for this worker."""
        return read_rodex_analytics_checkpoint(
            self._session_id,
            self._database_path,
            expected_current_codex_session_id=self._expected_codex_session_id,
        )

    def publish(
        self, publication: RodexAnalyticsPublication
    ) -> RodexAnalyticsPublishReceipt:
        """Publish one identity-fenced calculation through the registry API."""
        return publish_rodex_session_statistics(
            self._session_id,
            self._database_path,
            expected_current_codex_session_id=self._expected_codex_session_id,
            based_on_statistics_publication_sequence=(
                publication.based_on_statistics_publication_sequence
            ),
            statistics_projection_schema_version=(
                publication.statistics_projection_schema_version
            ),
            calculated_at_utc=publication.calculated_at_utc,
            coverage_state=publication.coverage_state,
            statistics_projection=publication.statistics_projection,
            analyzed_sources=publication.analyzed_sources,
            changed_source_thread_ids=publication.changed_source_thread_ids,
            changed_turn_keys=publication.changed_turn_keys,
            removed_turn_keys=publication.removed_turn_keys,
        )

    def record_health_transition(
        self,
        *,
        worker_state: str,
        diagnostic_code: str | None,
        attempted_at_utc: str,
        failed: bool = False,
        next_retry_at_utc: str | None = None,
        prior_consecutive_failures: int | None = None,
    ) -> RodexSessionStatisticsWorker:
        """Persist one health transition while preserving last-good statistics."""
        if prior_consecutive_failures is None:
            prior = self.load_checkpoint().worker
            prior_consecutive_failures = 0 if prior is None else prior.consecutive_failures
        consecutive_failures = prior_consecutive_failures + 1 if failed else 0
        return record_rodex_session_statistics_worker_health(
            self._session_id,
            self._database_path,
            expected_current_codex_session_id=self._expected_codex_session_id,
            worker_state=worker_state,
            diagnostic_code=diagnostic_code,
            last_attempted_at_utc=attempted_at_utc,
            consecutive_failures=consecutive_failures,
            next_retry_at_utc=next_retry_at_utc,
        )
