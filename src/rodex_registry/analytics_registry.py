"""Single persistence boundary for one Rodex analytics worker."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .errors import RodexAnalyticsPublicationRetryableError
from .identity import (
    CodexSessionId,
    CodexThreadId,
    RodexAnalyticsIdentityFence,
    RodexRegistryId,
    RodexRuntimeId,
    RodexSessionId,
)
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
        rodex_session_id: RodexSessionId | str,
        rodex_registry_id: RodexRegistryId | str,
        runtime_id: RodexRuntimeId | str,
        expected_codex_session_id: CodexSessionId | str,
    ) -> None:
        _validate_session_id(session_id)
        self._database_path = existing_rodex_database_path(database_path)
        self._identity = RodexAnalyticsIdentityFence(
            rodex_sessions_id=session_id,
            rodex_session_id=rodex_session_id,
            rodex_registry_id=rodex_registry_id,
            runtime_id=runtime_id,
            codex_session_id=expected_codex_session_id,
        )
        self._model_name_ids: dict[str, int] = {}
        self._reasoning_effort_name_ids: dict[str, int] = {}

    @classmethod
    def open(
        cls,
        database_path: str | os.PathLike[str],
        *,
        session_id: int,
        rodex_session_id: RodexSessionId | str,
        rodex_registry_id: RodexRegistryId | str,
        runtime_id: RodexRuntimeId | str,
        expected_codex_session_id: CodexSessionId | str,
    ) -> Self:
        """Open the existing registry and bind one immutable worker identity."""
        return cls(
            database_path,
            session_id=session_id,
            rodex_session_id=rodex_session_id,
            rodex_registry_id=rodex_registry_id,
            runtime_id=runtime_id,
            expected_codex_session_id=expected_codex_session_id,
        )

    @property
    def database_path(self) -> Path:
        return self._database_path

    @property
    def session_id(self) -> int:
        return self._identity.rodex_sessions_id

    @property
    def expected_codex_session_id(self) -> CodexSessionId:
        return self._identity.codex_session_id

    def load_checkpoint(self) -> RodexAnalyticsCheckpoint:
        """Load the last committed analytics checkpoint for this worker."""
        return read_rodex_analytics_checkpoint(
            self._identity.rodex_sessions_id,
            self._database_path,
            expected_current_codex_session_id=self._identity.codex_session_id,
            identity_fence=self._identity,
        )

    def publish(
        self, publication: RodexAnalyticsPublication
    ) -> RodexAnalyticsPublishReceipt:
        """Publish one identity-fenced calculation through the registry API."""
        model_name_ids = dict(self._model_name_ids)
        reasoning_effort_name_ids = dict(self._reasoning_effort_name_ids)
        try:
            receipt = publish_rodex_session_statistics(
                self._identity.rodex_sessions_id,
                self._database_path,
                expected_current_codex_session_id=self._identity.codex_session_id,
                identity_fence=self._identity,
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
                model_name_ids=model_name_ids,
                reasoning_effort_name_ids=reasoning_effort_name_ids,
            )
        except sqlite3.OperationalError as error:
            error_code = getattr(error, "sqlite_errorcode", 0)
            if error_code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise RodexAnalyticsPublicationRetryableError(
                    "analytics publication was blocked by a transient SQLite lock"
                ) from error
            raise
        self._model_name_ids = model_name_ids
        self._reasoning_effort_name_ids = reasoning_effort_name_ids
        return receipt

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
        consecutive_failures = (
            None
            if failed and prior_consecutive_failures is None
            else (prior_consecutive_failures or 0) + 1
            if failed
            else 0
        )
        return record_rodex_session_statistics_worker_health(
            self._identity.rodex_sessions_id,
            self._database_path,
            expected_current_codex_session_id=self._identity.codex_session_id,
            identity_fence=self._identity,
            worker_state=worker_state,
            diagnostic_code=diagnostic_code,
            last_attempted_at_utc=attempted_at_utc,
            consecutive_failures=consecutive_failures,
            increment_failure=failed and prior_consecutive_failures is None,
            next_retry_at_utc=next_retry_at_utc,
        )
