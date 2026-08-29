"""Fail-open statistics and typed agent traces for managed Codex rollouts."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

from rodex_registry import (
    COLLABORATION_MODEL_TOOL_NAMES,
    CodexSessionId,
    CodexThreadId,
    RodexAgentTraceEvent,
    RodexAnalyticsCheckpoint,
    RodexAnalyticsPublication,
    RodexAnalyticsPublicationRetryableError,
    RodexAnalyticsPublishReceipt,
    RodexAnalyticsRegistry,
    RodexAnalyticsStatisticsCheckpoint,
    RodexSessionCodexThread,
    RodexSessionCodexThreadObservation,
    RodexSessionStatisticsConflictError,
    SessionStatisticsProjection,
    StatisticsNamedCount,
    TraceSubagentActivity,
    TurnStatisticsProjection,
    current_rodex_sessions_user_identity,
    parse_codex_thread_id,
)
from rodex_sql import RodexDatabaseMovedError, RodexDatabaseNotFoundError

from .agent_observer import notify_agent_observer_trace_publication
from .agent_trace import AGENT_TRACE_SCHEMA_VERSION, StatefulAgentTraceNormalizer
from .analytics_analyzer import (
    AnalyticsAnalyzerSource,
    AnalyticsBoundary,
    AnalyticsBoundaryFactory,
    RodexAnalyticsError,
    StatefulCodexProtocolAnalyticsAdapter,
)
from .analytics_scheduler import (
    AnalyticsDirtyBatch,
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
ANALYTICS_HEALTH_RETRY_DELAY_SECONDS = 1.0
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
    history_inheritance_kind: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedCollaborationProjection:
    """Canonical collaboration facts joined to authenticated source lineage."""

    statistics_projection: SessionStatisticsProjection
    analyzed_sources: tuple[RodexSessionCodexThreadObservation, ...]


@dataclass(frozen=True, slots=True)
class StableRolloutRead:
    """One in-memory complete-line prefix plus its exact source state."""

    analyzer_content: bytes
    has_accepted_baseline: bool
    accepted_analyzer_content: bytes
    appended_analyzer_content: bytes
    appended_source_line_ordinals: tuple[int, ...]
    verified_source: VerifiedRollout
    prepared_read: AnalyticsSourceRead
    observation: RodexSessionCodexThreadObservation
    authenticated_source: AuthenticatedRolloutPrefix


@dataclass(frozen=True, slots=True)
class _PreparedAnalyticsPublication:
    """One analyzed source prefix retained unchanged until SQLite accepts it."""

    publication: RodexAnalyticsPublication
    stable_reads: tuple[StableRolloutRead, ...]
    observations: tuple[RodexSessionCodexThreadObservation, ...]
    turn_updates_by_key: dict[tuple[CodexThreadId, str], TurnStatisticsProjection]
    replace_all_turns: bool
    followup_thread_ids: frozenset[CodexThreadId]
    unresolved_thread_ids: frozenset[CodexThreadId]


@dataclass(frozen=True, slots=True)
class _AnalyticsFailureFingerprint:
    """Permanent diagnostic plus the exact source prefixes that produced it."""

    diagnostic_code: str
    diagnostic_detail: str
    sources: tuple[tuple[CodexThreadId, AuthenticatedRolloutPrefix], ...]


@dataclass(frozen=True, slots=True)
class _PendingAnalyticsFailureHealth:
    """One failed degraded-health transition waiting on a bounded retry."""

    fingerprint: _AnalyticsFailureFingerprint
    retry_not_before_monotonic: float


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


def _latest_turns_by_thread(
    turns: Sequence[TurnStatisticsProjection],
) -> dict[CodexThreadId, TurnStatisticsProjection]:
    latest: dict[CodexThreadId, TurnStatisticsProjection] = {}
    for turn in turns:
        current = latest.get(turn.codex_thread_id)
        if current is None or (
            "" if current.started_at_utc is None else current.started_at_utc,
            current.codex_turn_id,
        ) < (
            "" if turn.started_at_utc is None else turn.started_at_utc,
            turn.codex_turn_id,
        ):
            latest[turn.codex_thread_id] = turn
    return latest


def _trace_target_thread_ids(
    events: Sequence[RodexAgentTraceEvent],
    *,
    already_observed: Mapping[CodexThreadId, object],
) -> frozenset[CodexThreadId]:
    """Select exact 128-bit child targets named by newly normalized activity."""
    return frozenset(
        event.detail.target_codex_thread_id
        for event in events
        if isinstance(getattr(event, "detail", None), TraceSubagentActivity)
        and event.detail.target_codex_thread_id is not None
        and event.detail.target_codex_thread_id not in already_observed
    )


def _changed_observation_thread_ids(
    accepted: Mapping[CodexThreadId, RodexSessionCodexThreadObservation],
    stable_reads: Sequence[StableRolloutRead],
) -> frozenset[CodexThreadId]:
    """Compare only the exact sources selected by the lifecycle batch."""
    changed: set[CodexThreadId] = set()
    for read in stable_reads:
        observation = read.observation
        prior = accepted.get(observation.codex_thread_id)
        if prior is None or (
            prior.source_kind != observation.source_kind
            or prior.parent_codex_thread_id != observation.parent_codex_thread_id
            or prior.thread_depth != observation.thread_depth
            or prior.rollout_file_path != observation.rollout_file_path
            or prior.analyzed_size_bytes != observation.analyzed_size_bytes
            or prior.analyzed_prefix_sha256 != observation.analyzed_prefix_sha256
        ):
            changed.add(observation.codex_thread_id)
    return frozenset(changed)


def _checkpoint_source_observations(
    sources: Sequence[RodexSessionCodexThread],
) -> dict[CodexThreadId, RodexSessionCodexThreadObservation]:
    """Restore the accepted source projection without reclassifying file growth."""
    by_row_id = {source.id: source for source in sources}
    restored: dict[CodexThreadId, RodexSessionCodexThreadObservation] = {}
    for source in sources:
        if (
            source.rollout_file_path is None
            or source.analyzed_size_bytes is None
            or source.analyzed_mtime_ns is None
            or source.analyzed_prefix_sha256 is None
            or source.verified_at_utc is None
        ):
            continue
        parent = (
            None
            if source.parent_rodex_sessions_codex_threads_id is None
            else by_row_id.get(source.parent_rodex_sessions_codex_threads_id)
        )
        if source.parent_rodex_sessions_codex_threads_id is not None and parent is None:
            raise RodexAnalyticsError("checkpoint source lost its parent membership")
        restored[source.codex_thread_id] = RodexSessionCodexThreadObservation(
            codex_thread_id=source.codex_thread_id,
            source_kind=source.source_kind,
            parent_codex_thread_id=(None if parent is None else parent.codex_thread_id),
            thread_depth=source.thread_depth,
            agent_path=source.agent_path,
            agent_nickname=source.agent_nickname,
            subagent_history_start_ordinal=source.subagent_history_start_ordinal,
            spawning_codex_turn_id=source.spawning_codex_turn_id,
            first_linked_at_utc=source.first_linked_at_utc,
            rollout_file_path=Path(source.rollout_file_path),
            analyzed_size_bytes=source.analyzed_size_bytes,
            analyzed_mtime_ns=source.analyzed_mtime_ns,
            analyzed_prefix_sha256=source.analyzed_prefix_sha256,
            verified_at_utc=source.verified_at_utc,
            history_inheritance_kind=source.history_inheritance_kind,
        )
    return restored


def _merge_source_observations(
    accepted: Mapping[CodexThreadId, RodexSessionCodexThreadObservation],
    stable_reads: Sequence[StableRolloutRead],
) -> dict[CodexThreadId, RodexSessionCodexThreadObservation]:
    merged = dict(accepted)
    for read in stable_reads:
        observation = read.observation
        prior = accepted.get(observation.codex_thread_id)
        if prior is not None:
            observation = replace(
                observation,
                spawning_codex_turn_id=prior.spawning_codex_turn_id,
            )
        merged[observation.codex_thread_id] = observation
    return dict(
        sorted(
            merged.items(),
            key=lambda item: (item[1].thread_depth, str(item[0])),
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
        monotonic: Callable[[], float] = time.monotonic,
        trace_publication_notifier: Callable[[Path, int, bool], None] = (
            notify_agent_observer_trace_publication
        ),
    ) -> None:
        if not config.is_activated:
            raise ValueError("analytics worker requires committed runtime identity")
        self._config = config
        self._adapter_factory = adapter_factory
        self._adapter: AnalyticsBoundary | None = None
        self._now = now
        self._monotonic = monotonic
        self._trace_publication_notifier = trace_publication_notifier
        assert config.rodex_sessions_id is not None
        assert config.codex_session_id is not None
        self._session_id = config.rodex_sessions_id
        self._expected_codex_session_id = config.codex_session_id
        self._registry: RodexAnalyticsRegistry | None = None
        self._checkpoint: RodexAnalyticsCheckpoint | None = None
        self._publication_sequence: int | None = None
        self._trace_publication_sequence: int | None = None
        self._accepted_observations: dict[
            CodexThreadId, RodexSessionCodexThreadObservation
        ] = {}
        self._requires_full_reconcile = True
        self._prepared_publication: _PreparedAnalyticsPublication | None = None
        self._deferred_dirty_thread_ids: set[CodexThreadId] = set()
        self._pending_resolution_thread_ids: set[CodexThreadId] = set()
        self._source_catalog = AnalyticsSourceCatalog(config.codex_sessions_root)
        self._session_tree_bootstrap_complete = False
        self._source_reader = AnalyticsSourceReader()
        self._trace_normalizer = StatefulAgentTraceNormalizer()
        self._verified_sources: dict[CodexThreadId, VerifiedRollout] = {}
        self._schedule_followup: Callable[[CodexThreadId], None] | None = None
        self._last_health_transition: tuple[str, str | None] | None = None
        self._last_failure_health_fingerprint: _AnalyticsFailureFingerprint | None = None
        self._parked_failure: _AnalyticsFailureFingerprint | None = None
        self._pending_failure_health: _PendingAnalyticsFailureHealth | None = None
        self._consecutive_failures = 0
        self._published_turns: (
            dict[tuple[CodexThreadId, str], TurnStatisticsProjection] | None
        ) = None
        self._latest_turns: dict[CodexThreadId, TurnStatisticsProjection] = {}

    def observe_protocol_event(self, event: Mapping[str, Any]) -> None:
        """Feed exact lifecycle identity metadata into bounded source resolution."""
        self._source_catalog.observe_protocol_event(event)

    def poll_once(self, batch: AnalyticsDirtyBatch | None = None) -> str:
        """Perform one reconciliation; no analytics failure is allowed to escape."""
        expected_codex_session_id = self._expected_codex_session_id
        failure_reads: tuple[StableRolloutRead, ...] = ()
        try:
            session_id = self._session_id
            codex_session_id = self._expected_codex_session_id
            registry = self._registry
            if registry is None:
                registry = RodexAnalyticsRegistry.open(
                    self._config.rodex_database_path,
                    session_id=session_id,
                    rodex_session_id=self._config.rodex_session_id,
                    rodex_registry_id=self._config.rodex_registry_id,
                    runtime_id=self._config.runtime_id,
                    expected_codex_session_id=codex_session_id,
                )
                self._registry = registry
            checkpoint = self._checkpoint
            cold_start = checkpoint is None
            if checkpoint is None:
                checkpoint = registry.load_checkpoint()
                if (checkpoint.statistics is None) != (checkpoint.trace is None):
                    raise RodexAnalyticsError(
                        "analytics checkpoint has only one of its atomic publication heads"
                    )
                if (
                    checkpoint.trace is not None
                    and checkpoint.trace.trace_schema_version != AGENT_TRACE_SCHEMA_VERSION
                ):
                    raise RodexAnalyticsError(
                        "agent trace schema is incompatible with this Rodex generation"
                    )
                self._checkpoint = checkpoint
                self._accepted_observations = _checkpoint_source_observations(
                    checkpoint.sources
                )
                self._pending_resolution_thread_ids.update(
                    checkpoint.unresolved_activity_targets
                )
                self._publication_sequence = (
                    None
                    if checkpoint.statistics is None
                    else checkpoint.statistics.statistics_publication_sequence
                )
                self._trace_publication_sequence = (
                    None
                    if checkpoint.trace is None
                    else checkpoint.trace.trace_publication_sequence
                )
            if checkpoint.worker is not None and cold_start:
                self._last_health_transition = (
                    checkpoint.worker.worker_state,
                    checkpoint.worker.diagnostic_code,
                )
                self._consecutive_failures = checkpoint.worker.consecutive_failures
            if self._parked_failure_is_current(batch):
                self._retry_pending_failure_health(codex_session_id)
                return "clean_replay"
            prepared = self._prepared_publication
            if prepared is not None:
                if batch is not None:
                    self._deferred_dirty_thread_ids.update(batch.thread_ids)
                source_growth = tuple(
                    self._source_reader.verify_captured_prefix(item.authenticated_source)
                    for item in prepared.stable_reads
                )
                if any(source_growth):
                    prepared = replace(
                        prepared,
                        followup_thread_ids=(
                            prepared.followup_thread_ids
                            | frozenset(
                                item.observation.codex_thread_id
                                for item, grew in zip(
                                    prepared.stable_reads,
                                    source_growth,
                                    strict=True,
                                )
                                if grew
                            )
                        ),
                    )
                    self._prepared_publication = prepared
                receipt = registry.publish(prepared.publication)
                return self._accept_prepared_publication(prepared, receipt)
            full_reconcile = (
                self._requires_full_reconcile or batch is None or batch.full_reconcile
            )
            requested_thread_ids: frozenset[CodexThreadId] = frozenset()
            if full_reconcile:
                stable_reads = self._read_registered_sources(checkpoint.sources)
                if stable_reads is not None:
                    self._pending_resolution_thread_ids.difference_update(
                        item.observation.codex_thread_id for item in stable_reads
                    )
                unresolved_thread_ids = frozenset(self._pending_resolution_thread_ids)
            else:
                requested_thread_ids = frozenset(
                    batch.thread_ids | self._pending_resolution_thread_ids
                )
                stable_reads, unresolved_thread_ids = self._read_exact_sources(
                    requested_thread_ids
                )
                self._pending_resolution_thread_ids = set(unresolved_thread_ids)
            failure_reads = () if stable_reads is None else tuple(stable_reads)
            if stable_reads is None or (not stable_reads and unresolved_thread_ids):
                self._project_health("catching_up", "rollout_not_found", codex_session_id)
                return "catching_up"
            checkpoint_matches = cold_start and _view_matches_source_reads(
                checkpoint.statistics,
                checkpoint.sources,
                stable_reads,
            )
            accepted_observations = dict(self._accepted_observations)
            if checkpoint_matches:
                accepted_observations = {
                    item.observation.codex_thread_id: item.observation
                    for item in stable_reads
                }
            changed_source_thread_ids = _changed_observation_thread_ids(
                accepted_observations,
                stable_reads,
            )
            adapter = self._adapter
            adapter_needs_warmup = adapter is None
            if adapter is None:
                adapter = self._adapter_factory()
                self._adapter = adapter
            if not adapter_needs_warmup and not changed_source_thread_ids:
                source_growth = tuple(
                    self._source_reader.verify_captured_prefix(item.authenticated_source)
                    for item in stable_reads
                )
                self._source_reader.accept([item.prepared_read for item in stable_reads])
                self._promote_verified_sources(stable_reads)
                self._requires_full_reconcile = False
                if any(source_growth):
                    if self._schedule_followup is not None:
                        for item, grew in zip(stable_reads, source_growth, strict=True):
                            if grew:
                                self._schedule_followup(item.observation.codex_thread_id)
                    return "pending_append"
                if unresolved_thread_ids:
                    self._project_health(
                        "catching_up", "rollout_not_found", codex_session_id
                    )
                    return "catching_up"
                wake_thread_ids = (
                    batch.thread_ids
                    if batch is not None and batch.thread_ids
                    else requested_thread_ids
                )
                if wake_thread_ids:
                    self._pending_resolution_thread_ids.update(wake_thread_ids)
                    return "awaiting_append"
                self._project_health("up_to_date", None, codex_session_id)
                return "up_to_date"
            baseline_reads = tuple(
                item for item in stable_reads if item.has_accepted_baseline
            )
            has_current_baseline = (
                adapter_needs_warmup
                and checkpoint.statistics is not None
                and checkpoint.statistics.statistics_projection_schema_version
                == STATISTICS_PROJECTION_SCHEMA_VERSION
                and self._trace_publication_sequence is not None
                and bool(baseline_reads)
            )
            if has_current_baseline:
                baseline_calculation = adapter.analyze_rollouts(
                    tuple(
                        AnalyticsAnalyzerSource(
                            codex_thread_id=item.observation.codex_thread_id,
                            analyzer_content=item.accepted_analyzer_content,
                            appended_analyzer_content=item.accepted_analyzer_content,
                        )
                        for item in baseline_reads
                    ),
                    _current_analytics_user_id(),
                )
                baseline_observations = tuple(
                    self._accepted_observations[item.observation.codex_thread_id]
                    for item in baseline_reads
                )
                baseline_projection = _derive_verified_collaboration_projection(
                    baseline_calculation.statistics_projection,
                    analyzed_sources=baseline_observations,
                )
                self._published_turns = _turns_by_key(
                    baseline_projection.statistics_projection.turn_statistics
                )
                self._latest_turns = _latest_turns_by_thread(
                    baseline_projection.statistics_projection.turn_statistics
                )
                adapter.accept_batch()
                self._trace_normalizer.warmup(
                    tuple(
                        (
                            item.observation.codex_thread_id,
                            item.accepted_analyzer_content,
                        )
                        for item in baseline_reads
                    )
                )
            calculation = adapter.analyze_rollouts(
                _analyzer_sources(stable_reads),
                _current_analytics_user_id(),
            )
            source_growth = tuple(
                self._source_reader.verify_captured_prefix(item.authenticated_source)
                for item in stable_reads
            )
            append_arrived_during_analysis = any(source_growth)
            next_observations = _merge_source_observations(
                accepted_observations,
                stable_reads,
            )
            calculated_at_utc = self._timestamp()
            trace_publication = self._trace_normalizer.prepare(
                tuple(
                    (
                        item.observation.codex_thread_id,
                        item.appended_analyzer_content,
                        item.appended_source_line_ordinals,
                    )
                    for item in stable_reads
                ),
                based_on_trace_publication_sequence=self._trace_publication_sequence,
                calculated_at_utc=calculated_at_utc,
                source_coverage_state=calculation.coverage_state,
            )
            previous_turns = self._published_turns
            if previous_turns is None:
                verified_collaboration = _derive_verified_collaboration_projection(
                    calculation.statistics_projection,
                    analyzed_sources=tuple(next_observations.values()),
                )
                turn_updates = _turns_by_key(
                    verified_collaboration.statistics_projection.turn_statistics
                )
                changed_turn_keys = None
                resident_turn_updates = turn_updates
            else:
                verified_collaboration = (
                    _derive_incremental_verified_collaboration_projection(
                        calculation.statistics_projection,
                        analyzed_sources=tuple(next_observations.values()),
                        resident_turns=previous_turns,
                    )
                )
                projected_updates = _turns_by_key(
                    verified_collaboration.statistics_projection.turn_statistics
                )
                changed_turn_keys = frozenset(
                    key
                    for key, turn in projected_updates.items()
                    if previous_turns.get(key) != turn
                )
                turn_updates = {key: projected_updates[key] for key in changed_turn_keys}
                resident_turn_updates = turn_updates
            removed_turn_keys: frozenset[tuple[CodexThreadId, str]] = frozenset()
            trace_needs_publication = self._trace_publication_sequence is None
            should_publish = trace_needs_publication or (
                not checkpoint_matches
                and bool(
                    changed_source_thread_ids
                    or trace_publication.events
                    or changed_turn_keys
                    or checkpoint.statistics is None
                    or checkpoint.statistics.statistics_projection_schema_version
                    != STATISTICS_PROJECTION_SCHEMA_VERSION
                )
            )
            trace_target_thread_ids = _trace_target_thread_ids(
                trace_publication.events,
                already_observed=next_observations,
            )
            if should_publish:
                publication_projection = (
                    verified_collaboration.statistics_projection
                    if changed_turn_keys is None
                    else replace(
                        verified_collaboration.statistics_projection,
                        turn_statistics=tuple(
                            turn_updates[key] for key in sorted(changed_turn_keys, key=str)
                        ),
                    )
                )
                publication = RodexAnalyticsPublication(
                    based_on_statistics_publication_sequence=self._publication_sequence,
                    statistics_projection_schema_version=(
                        STATISTICS_PROJECTION_SCHEMA_VERSION
                    ),
                    calculated_at_utc=calculated_at_utc,
                    coverage_state=calculation.coverage_state,
                    statistics_projection=publication_projection,
                    analyzed_sources=tuple(verified_collaboration.analyzed_sources),
                    changed_source_thread_ids=changed_source_thread_ids,
                    changed_turn_keys=changed_turn_keys,
                    removed_turn_keys=removed_turn_keys,
                    agent_trace_publication=trace_publication,
                )
                prepared = _PreparedAnalyticsPublication(
                    publication=publication,
                    stable_reads=tuple(stable_reads),
                    observations=tuple(verified_collaboration.analyzed_sources),
                    turn_updates_by_key=resident_turn_updates,
                    replace_all_turns=previous_turns is None,
                    followup_thread_ids=(
                        trace_target_thread_ids
                        | frozenset(
                            item.observation.codex_thread_id
                            for item, grew in zip(
                                stable_reads,
                                source_growth,
                                strict=True,
                            )
                            if grew
                        )
                    ),
                    unresolved_thread_ids=unresolved_thread_ids,
                )
                self._prepared_publication = prepared
                receipt = registry.publish(publication)
                return self._accept_prepared_publication(prepared, receipt)
            if previous_turns is None:
                self._published_turns = dict(resident_turn_updates)
                self._latest_turns = _latest_turns_by_thread(
                    tuple(resident_turn_updates.values())
                )
            else:
                previous_turns.update(turn_updates)
                self._update_latest_turns(turn_updates.values())
            self._accepted_observations = {
                item.codex_thread_id: item
                for item in verified_collaboration.analyzed_sources
            }
            adapter.accept_batch()
            self._trace_normalizer.accept_batch()
            self._source_reader.accept([item.prepared_read for item in stable_reads])
            self._promote_verified_sources(stable_reads)
            self._requires_full_reconcile = False
            if trace_target_thread_ids:
                self._pending_resolution_thread_ids.update(trace_target_thread_ids)
                if self._schedule_followup is not None:
                    for thread_id in trace_target_thread_ids:
                        self._schedule_followup(thread_id)
                return "pending_append"
            if append_arrived_during_analysis:
                if self._schedule_followup is not None:
                    for item, grew in zip(stable_reads, source_growth, strict=True):
                        if grew:
                            self._schedule_followup(item.observation.codex_thread_id)
                return "pending_append"
            if unresolved_thread_ids:
                self._project_health("catching_up", "rollout_not_found", codex_session_id)
                return "catching_up"
            self._project_health("up_to_date", None, codex_session_id)
            return "up_to_date"
        except RodexDatabaseNotFoundError:
            return "catching_up"
        except RodexAnalyticsPublicationRetryableError:
            return "publication_retry"
        except RodexSessionStatisticsConflictError:
            self._checkpoint = None
            self._publication_sequence = None
            self._trace_publication_sequence = None
            self._published_turns = None
            self._latest_turns.clear()
            self._accepted_observations.clear()
            self._adapter = None
            self._prepared_publication = None
            self._deferred_dirty_thread_ids.clear()
            self._source_reader = AnalyticsSourceReader()
            self._verified_sources.clear()
            self._trace_normalizer.require_clean_replay()
            self._requires_full_reconcile = True
            self._parked_failure = None
            self._pending_failure_health = None
            self._last_failure_health_fingerprint = None
            return "clean_replay"
        except RodexDatabaseMovedError:
            raise
        except Exception as error:
            if batch is not None:
                self._pending_resolution_thread_ids.update(batch.thread_ids)
            self._pending_resolution_thread_ids.update(self._deferred_dirty_thread_ids)
            diagnostic_code = _diagnostic_code(error)
            failure_fingerprint = _AnalyticsFailureFingerprint(
                diagnostic_code=diagnostic_code,
                diagnostic_detail=f"{type(error).__qualname__}: {error}",
                sources=tuple(
                    sorted(
                        (
                            (item.observation.codex_thread_id, item.authenticated_source)
                            for item in failure_reads
                        ),
                        key=lambda item: str(item[0]),
                    )
                ),
            )
            health_persisted = self._project_health(
                "degraded",
                diagnostic_code,
                expected_codex_session_id,
                failed=True,
                failure_fingerprint=failure_fingerprint,
            )
            self._parked_failure = (
                failure_fingerprint
                if failure_fingerprint.sources and isinstance(error, RodexAnalyticsError)
                else None
            )
            self._pending_failure_health = (
                _PendingAnalyticsFailureHealth(
                    fingerprint=failure_fingerprint,
                    retry_not_before_monotonic=(
                        self._monotonic() + ANALYTICS_HEALTH_RETRY_DELAY_SECONDS
                    ),
                )
                if self._parked_failure is not None and not health_persisted
                else None
            )
            self._adapter = None
            self._prepared_publication = None
            self._deferred_dirty_thread_ids.clear()
            self._source_reader.require_clean_replay()
            self._verified_sources.clear()
            self._trace_normalizer.require_clean_replay()
            self._requires_full_reconcile = True
            return "clean_replay"

    def _parked_failure_is_current(
        self,
        batch: AnalyticsDirtyBatch | None,
    ) -> bool:
        parked = self._parked_failure
        if parked is None:
            return False
        captured_thread_ids = frozenset(thread_id for thread_id, _source in parked.sources)
        if batch is not None and not batch.thread_ids.issubset(captured_thread_ids):
            self._parked_failure = None
            self._pending_failure_health = None
            return False
        try:
            changed = any(
                self._source_reader.verify_captured_prefix(source)
                for _thread_id, source in parked.sources
            )
        except AnalyticsSourceReadError:
            self._parked_failure = None
            self._pending_failure_health = None
            return False
        if changed:
            self._parked_failure = None
            self._pending_failure_health = None
            return False
        return True

    def _retry_pending_failure_health(
        self,
        expected_codex_session_id: CodexSessionId,
    ) -> None:
        pending = self._pending_failure_health
        if pending is None:
            return
        monotonic_now = self._monotonic()
        if monotonic_now < pending.retry_not_before_monotonic:
            return
        if self._project_health(
            "degraded",
            pending.fingerprint.diagnostic_code,
            expected_codex_session_id,
            failed=True,
            failure_fingerprint=pending.fingerprint,
        ):
            self._pending_failure_health = None
            return
        self._pending_failure_health = replace(
            pending,
            retry_not_before_monotonic=(
                monotonic_now + ANALYTICS_HEALTH_RETRY_DELAY_SECONDS
            ),
        )

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
        sources: Sequence[RodexSessionCodexThread],
    ) -> list[StableRolloutRead] | None:
        verified_sources = _discover_verified_thread_rollouts(
            self._config.codex_sessions_root,
            sources,
            self._source_catalog,
            verified_cache=self._verified_sources,
            bootstrap_session_tree=(
                not self._session_tree_bootstrap_complete and len(sources) == 1
            ),
        )
        if verified_sources is None:
            return None
        self._session_tree_bootstrap_complete = True
        checkpoints_by_thread = {source.codex_thread_id: source for source in sources}
        reads: list[StableRolloutRead] = []
        for verified in verified_sources:
            checkpoint = checkpoints_by_thread.get(verified.codex_thread_id)
            captured = self._source_reader.read(
                AnalyticsAppendSource(
                    path=verified.path,
                    codex_thread_id=verified.codex_thread_id,
                    source_kind=verified.source_kind,
                    subagent_history_start_ordinal=(
                        verified.subagent_history_start_ordinal
                    ),
                    allowed_root=self._config.codex_sessions_root,
                    accepted_prefix_size_bytes=(
                        None if checkpoint is None else checkpoint.analyzed_size_bytes
                    ),
                    accepted_prefix_sha256=(
                        None if checkpoint is None else checkpoint.analyzed_prefix_sha256
                    ),
                )
            )
            reads.append(_stable_rollout_read(verified, captured, self._timestamp()))
        return reads

    def _read_exact_sources(
        self,
        thread_ids: frozenset[CodexThreadId],
    ) -> tuple[list[StableRolloutRead], frozenset[CodexThreadId]]:
        candidates: dict[CodexThreadId, VerifiedRollout] = {}
        unavailable: set[CodexThreadId] = set()
        discovery_queue = set(thread_ids).difference(self._verified_sources)
        attempted: set[CodexThreadId] = set()
        while discovery_queue:
            thread_id = min(discovery_queue, key=str)
            discovery_queue.remove(thread_id)
            attempted.add(thread_id)
            source = _discover_exact_thread_rollout(
                self._config.codex_sessions_root,
                thread_id,
                self._source_catalog,
                expected_root_thread_id=self._expected_codex_session_id,
            )
            if source is None:
                unavailable.add(thread_id)
                continue
            candidates[thread_id] = source
            parent_thread_id = source.parent_codex_thread_id
            if (
                parent_thread_id is not None
                and parent_thread_id not in self._verified_sources
                and parent_thread_id not in candidates
                and parent_thread_id not in attempted
            ):
                discovery_queue.add(parent_thread_id)

        closure = dict(self._verified_sources)
        pending_candidates = dict(candidates)
        while pending_candidates:
            added = False
            for thread_id, source in sorted(
                tuple(pending_candidates.items()),
                key=lambda item: (item[1].thread_depth, str(item[0])),
            ):
                parent_thread_id = source.parent_codex_thread_id
                if parent_thread_id is None:
                    if thread_id != self._expected_codex_session_id:
                        raise RodexAnalyticsError(
                            f"non-root source lost its parent: {thread_id}"
                        )
                else:
                    parent = closure.get(parent_thread_id)
                    if parent is None:
                        continue
                    if source.thread_depth != parent.thread_depth + 1:
                        raise RodexAnalyticsError(
                            f"sub-agent thread depth disagrees with parent: {thread_id}"
                        )
                closure[thread_id] = source
                del pending_candidates[thread_id]
                added = True
            if not added:
                break

        selected: dict[CodexThreadId, VerifiedRollout] = {}
        unresolved = unavailable | set(pending_candidates)
        newly_discovered_thread_ids = set(candidates)
        for thread_id in sorted(thread_ids, key=str):
            source = closure.get(thread_id)
            if source is None:
                unresolved.add(thread_id)
                continue
            selected[source.codex_thread_id] = source
            if thread_id not in newly_discovered_thread_ids:
                continue
            parent_thread_id = source.parent_codex_thread_id
            while parent_thread_id is not None:
                parent = closure.get(parent_thread_id)
                if parent is None:
                    raise RodexAnalyticsError(
                        f"verified source lost its ancestor: {source.codex_thread_id}"
                    )
                selected.setdefault(parent.codex_thread_id, parent)
                parent_thread_id = parent.parent_codex_thread_id
        reads: list[StableRolloutRead] = []
        for source in sorted(
            selected.values(),
            key=lambda item: (item.thread_depth, str(item.codex_thread_id)),
        ):
            captured = self._source_reader.read(
                AnalyticsAppendSource(
                    path=source.path,
                    codex_thread_id=source.codex_thread_id,
                    source_kind=source.source_kind,
                    subagent_history_start_ordinal=(source.subagent_history_start_ordinal),
                    allowed_root=self._config.codex_sessions_root,
                )
            )
            reads.append(_stable_rollout_read(source, captured, self._timestamp()))
        return reads, frozenset(unresolved)

    def _accept_prepared_publication(
        self,
        prepared: _PreparedAnalyticsPublication,
        receipt: RodexAnalyticsPublishReceipt,
    ) -> str:
        if self._prepared_publication is not prepared:
            raise RodexAnalyticsError("analytics publication candidate changed")
        adapter = self._adapter
        if adapter is None:
            raise RodexAnalyticsError("analytics publication has no resident analyzer")
        self._publication_sequence = receipt.statistics_publication_sequence
        if receipt.trace_publication_sequence is None:
            raise RodexAnalyticsError("analytics publication omitted its agent trace")
        self._trace_publication_sequence = receipt.trace_publication_sequence
        self._last_health_transition = ("up_to_date", None)
        self._last_failure_health_fingerprint = None
        self._consecutive_failures = 0
        if prepared.replace_all_turns:
            self._published_turns = dict(prepared.turn_updates_by_key)
            self._latest_turns = _latest_turns_by_thread(
                tuple(prepared.turn_updates_by_key.values())
            )
        else:
            published_turns = self._published_turns
            if published_turns is None:
                raise RodexAnalyticsError(
                    "incremental analytics publication lost resident turns"
                )
            published_turns.update(prepared.turn_updates_by_key)
            self._update_latest_turns(prepared.turn_updates_by_key.values())
        self._accepted_observations = {
            item.codex_thread_id: item for item in prepared.observations
        }
        adapter.accept_batch()
        self._trace_normalizer.accept_batch()
        self._source_reader.accept([item.prepared_read for item in prepared.stable_reads])
        self._promote_verified_sources(prepared.stable_reads)
        self._requires_full_reconcile = False
        self._parked_failure = None
        self._pending_failure_health = None
        self._prepared_publication = None
        followup_thread_ids = set(prepared.followup_thread_ids)
        followup_thread_ids.update(self._deferred_dirty_thread_ids)
        self._deferred_dirty_thread_ids.clear()
        self._pending_resolution_thread_ids.update(followup_thread_ids)
        if followup_thread_ids and self._schedule_followup is not None:
            for thread_id in followup_thread_ids:
                self._schedule_followup(thread_id)
        if prepared.unresolved_thread_ids:
            self._project_health(
                "catching_up",
                "rollout_not_found",
                self._expected_codex_session_id,
            )
            outcome = "catching_up"
        elif followup_thread_ids:
            outcome = "pending_append"
        else:
            outcome = "up_to_date"
        with suppress(Exception):
            self._trace_publication_notifier(
                self._config.protocol_event_socket_path,
                receipt.trace_publication_sequence,
                outcome == "up_to_date",
            )
        return outcome

    def _promote_verified_sources(
        self,
        stable_reads: Sequence[StableRolloutRead],
    ) -> None:
        for item in stable_reads:
            source = item.verified_source
            self._verified_sources[source.codex_thread_id] = source

    def _update_latest_turns(
        self,
        turns: Iterable[TurnStatisticsProjection],
    ) -> None:
        for turn in turns:
            current = self._latest_turns.get(turn.codex_thread_id)
            if (
                current is None
                or current.codex_turn_id == turn.codex_turn_id
                or (
                    "" if current.started_at_utc is None else current.started_at_utc,
                    current.codex_turn_id,
                )
                < (
                    "" if turn.started_at_utc is None else turn.started_at_utc,
                    turn.codex_turn_id,
                )
            ):
                self._latest_turns[turn.codex_thread_id] = turn

    def _project_health(
        self,
        state: str,
        diagnostic_code: str | None,
        expected_codex_session_id: CodexSessionId | None,
        *,
        failed: bool = False,
        failure_fingerprint: _AnalyticsFailureFingerprint | None = None,
    ) -> bool:
        session_id = self._session_id
        if session_id is None or expected_codex_session_id is None:
            return False
        try:
            registry = self._registry
            if registry is None:
                return False
            transition = (state, diagnostic_code)
            if failed:
                if (
                    failure_fingerprint is not None
                    and self._last_failure_health_fingerprint == failure_fingerprint
                ):
                    return True
            elif self._last_health_transition == transition:
                return True
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
            self._last_failure_health_fingerprint = failure_fingerprint if failed else None
            self._consecutive_failures = self._consecutive_failures + 1 if failed else 0
        except RodexDatabaseMovedError:
            raise
        except Exception:
            return False
        return True

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="microseconds")

    def mark_stopped(self) -> None:
        """Best-effort terminal health update for an orderly worker shutdown."""
        self._project_health("stopped", None, self._expected_codex_session_id)


class AnalyticsSubprocessSupervisor:
    """Own an optional worker through one blocking, bounded monitor thread."""

    def __init__(
        self,
        config: AnalyticsWorkerConfig,
        *,
        python_executable: str = sys.executable,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
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
        self._restart_delay_seconds = restart_delay_seconds
        self._max_start_attempts = max_start_attempts
        self._start_attempts = 0
        self._exhausted = False
        self._started = False
        self._stop = Event()
        self._process_lock = Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        """Start once; process exit and one backoff retry are event-driven."""
        if self._started:
            raise RuntimeError("analytics supervisor is already started")
        self._started = True
        self._start_next_process()
        self._thread = Thread(
            target=self._monitor,
            name="rodex-analytics-supervisor",
            daemon=True,
        )
        self._thread.start()

    def _start_next_process(self) -> None:
        if self._stop.is_set() or self._exhausted:
            return
        self._start_attempts += 1
        try:
            process = self._popen(
                self._config.command(self._python_executable),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            retry_scheduled = self._start_attempts < self._max_start_attempts
            _project_supervisor_health(
                self._config,
                "analytics_worker_start_failed",
                retry_scheduled=retry_scheduled,
                retry_delay_seconds=self._restart_delay_seconds,
            )
            if not retry_scheduled:
                self._exhausted = True
            return
        with self._process_lock:
            if self._stop.is_set():
                stop_immediately = True
            else:
                self._process = process
                stop_immediately = False
        if stop_immediately:
            _terminate_analytics_process(process)

    def _monitor(self) -> None:
        while not self._stop.is_set() and not self._exhausted:
            with self._process_lock:
                process = self._process
            if process is not None:
                try:
                    process.wait()
                except (OSError, subprocess.SubprocessError):
                    self._exhausted = True
                    return
                with self._process_lock:
                    if self._process is process:
                        self._process = None
                if self._stop.is_set():
                    return
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
            elif self._start_attempts >= self._max_start_attempts:
                self._exhausted = True
                return
            if self._stop.wait(self._restart_delay_seconds):
                return
            self._start_next_process()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for bounded supervision to finish; return false on timeout."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def close(self) -> None:
        self._stop.set()
        self._exhausted = True
        with self._process_lock:
            process = self._process
        if process is not None:
            _terminate_analytics_process(process)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive() and process is not None:
                with suppress(OSError, subprocess.SubprocessError):
                    process.kill()
                thread.join(timeout=2)
        with self._process_lock:
            self._process = None
        self._thread = None


def _terminate_analytics_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
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
                    None,
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
        forked_from_id = payload.get("forked_from_id")
        source = payload.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        if not isinstance(spawn, dict):
            return None
        depth = spawn.get("depth")
        agent_path = payload.get("agent_path")
        nickname = payload.get("agent_nickname")
        cutoff = payload.get("subagent_history_start_ordinal")
        if forked_from_id is None and cutoff is None:
            cutoff = 0
            history_inheritance_kind = "clean"
        elif forked_from_id != str(parent_thread_id):
            return None
        else:
            history_inheritance_kind = "inherited"
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
            history_inheritance_kind,
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
    registered_sources: Sequence[RodexSessionCodexThread],
    source_catalog: AnalyticsSourceCatalog,
    *,
    verified_cache: Mapping[CodexThreadId, VerifiedRollout] | None = None,
    bootstrap_session_tree: bool = False,
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
    root_thread_ids = frozenset(source.codex_thread_id for source in root_sources)
    bootstrapped_paths: dict[CodexThreadId, Path] = {}
    if bootstrap_session_tree:
        for root_source in root_sources:
            for path in source_catalog.session_tree_candidate_paths(
                root_source.codex_thread_id,
                first_linked_at_utc=root_source.first_linked_at_utc,
            ):
                candidate = _verify_subagent_rollout(
                    path,
                    root_thread_ids,
                    allowed_root=root,
                )
                if candidate is None:
                    continue
                prior_path = bootstrapped_paths.get(candidate.codex_thread_id)
                if prior_path is not None and prior_path != candidate.path:
                    raise RodexAnalyticsError(
                        "multiple rollout files declare Codex thread "
                        f"{candidate.codex_thread_id}"
                    )
                bootstrapped_paths[candidate.codex_thread_id] = candidate.path
                source_catalog.remember_resolved_path(
                    candidate.codex_thread_id,
                    candidate.path,
                )
    candidates: dict[CodexThreadId, VerifiedRollout] = {}
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

    if bootstrap_session_tree and pending:
        return None

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
            candidate.history_inheritance_kind,
        )
        parent_source = (
            None
            if source.parent_rodex_sessions_codex_threads_id is None
            else next(
                (
                    item
                    for item in registered_sources
                    if item.id == source.parent_rodex_sessions_codex_threads_id
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
            source.history_inheritance_kind,
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


def _discover_exact_thread_rollout(
    root: Path,
    thread_id: CodexThreadId,
    source_catalog: AnalyticsSourceCatalog,
    *,
    expected_root_thread_id: CodexThreadId,
) -> VerifiedRollout | None:
    """Authenticate one lifecycle-named source without assuming discovery order."""
    parsed_thread_id = parse_codex_thread_id(thread_id)
    if parsed_thread_id == expected_root_thread_id:
        return locate_verified_rollout(
            root,
            expected_root_thread_id,
            source_catalog=source_catalog,
        )
    verified = [
        source
        for path in source_catalog.candidate_paths(parsed_thread_id)
        if (
            source := _verify_subagent_rollout(
                path,
                frozenset({expected_root_thread_id}),
                allowed_root=root,
            )
        )
        is not None
        and source.codex_thread_id == parsed_thread_id
    ]
    if len(verified) > 1:
        raise RodexAnalyticsError(
            f"multiple rollout files declare Codex thread {parsed_thread_id}"
        )
    if not verified:
        return None
    source = verified[0]
    source_catalog.remember_resolved_path(parsed_thread_id, source.path)
    return source


def _derive_verified_collaboration_projection(
    projection: SessionStatisticsProjection,
    *,
    analyzed_sources: Sequence[RodexSessionCodexThreadObservation],
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


def _derive_incremental_verified_collaboration_projection(
    projection: SessionStatisticsProjection,
    *,
    analyzed_sources: Sequence[RodexSessionCodexThreadObservation],
    resident_turns: Mapping[tuple[CodexThreadId, str], TurnStatisticsProjection],
) -> VerifiedCollaborationProjection:
    """Decorate only analyzer-changed turns from resident verified source lineage."""
    projected_turns_by_key = _turns_by_key(projection.turn_statistics)
    supplied_sources = tuple(analyzed_sources)
    sources_by_thread = {item.codex_thread_id: item for item in supplied_sources}
    if len(sources_by_thread) != len(supplied_sources):
        raise RodexAnalyticsError(
            "verified collaboration sources contain a duplicate thread"
        )
    candidate_turns_by_thread: dict[
        CodexThreadId, dict[tuple[CodexThreadId, str], TurnStatisticsProjection]
    ] = {}
    resolved_sources: list[RodexSessionCodexThreadObservation] = []
    for source in supplied_sources:
        parent_thread_id = source.parent_codex_thread_id
        if parent_thread_id is None:
            resolved_sources.append(source)
            continue
        if parent_thread_id not in sources_by_thread:
            raise RodexAnalyticsError(
                f"verified sub-agent has no parent source: {source.codex_thread_id}"
            )
        if source.spawning_codex_turn_id is not None:
            resolved_sources.append(source)
            continue
        candidates = candidate_turns_by_thread.get(parent_thread_id)
        if candidates is None:
            candidates = {
                (turn.codex_thread_id, turn.codex_turn_id): turn
                for turn in resident_turns.values()
                if turn.codex_thread_id == parent_thread_id
            }
            candidates.update(
                {
                    (turn.codex_thread_id, turn.codex_turn_id): turn
                    for turn in projection.turn_statistics
                    if turn.codex_thread_id == parent_thread_id
                }
            )
            candidate_turns_by_thread[parent_thread_id] = candidates
        linked_at = _collaboration_timestamp(
            source.first_linked_at_utc,
            f"sub-agent {source.codex_thread_id} first-linked time",
        )
        owners = [
            turn
            for turn in candidates.values()
            if _turn_owns_collaboration_timestamp(turn, linked_at)
        ]
        if len(owners) != 1:
            raise RodexAnalyticsError(
                "new verified sub-agent must belong to exactly one direct-parent turn: "
                f"{source.codex_thread_id}"
            )
        owner = owners[0]
        projected_turns_by_key.setdefault(
            (owner.codex_thread_id, owner.codex_turn_id), owner
        )
        resolved_sources.append(replace(source, spawning_codex_turn_id=owner.codex_turn_id))
    sources = tuple(resolved_sources)
    sources_by_thread = {item.codex_thread_id: item for item in sources}
    children_started_by_turn: Counter[tuple[CodexThreadId, str]] = Counter()
    for source in sources:
        parent_thread_id = source.parent_codex_thread_id
        if parent_thread_id is None:
            continue
        if parent_thread_id not in sources_by_thread:
            raise RodexAnalyticsError(
                f"verified sub-agent has no parent source: {source.codex_thread_id}"
            )
        spawning_turn_id = source.spawning_codex_turn_id
        if spawning_turn_id is None:
            raise RodexAnalyticsError(
                f"verified sub-agent has no spawning turn: {source.codex_thread_id}"
            )
        children_started_by_turn[(parent_thread_id, spawning_turn_id)] += 1
    projected_turns = tuple(
        _derive_verified_turn_collaboration(
            turn,
            agents_started=children_started_by_turn.get(
                (turn.codex_thread_id, turn.codex_turn_id),
                0,
            ),
        )
        for turn in projected_turns_by_key.values()
    )
    canonical_counts = _canonical_collaboration_counts(projection.named_counts)
    return VerifiedCollaborationProjection(
        statistics_projection=replace(
            projection,
            collaboration_operations_count=sum(
                item.occurrence_count for item in canonical_counts
            ),
            collaboration_agents_started_count=sum(children_started_by_turn.values()),
            named_counts=_replace_collaboration_counts(
                projection.named_counts,
                canonical_counts,
            ),
            turn_statistics=projected_turns,
        ),
        analyzed_sources=sources,
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


def _turn_owns_collaboration_timestamp(
    turn: TurnStatisticsProjection,
    linked_at: datetime,
) -> bool:
    if turn.started_at_utc is None:
        return False
    started_at = _collaboration_timestamp(
        turn.started_at_utc,
        f"turn {turn.codex_turn_id} start time",
    )
    if started_at > linked_at:
        return False
    return turn.terminal_at_utc is None or linked_at <= _collaboration_timestamp(
        turn.terminal_at_utc,
        f"turn {turn.codex_turn_id} terminal time",
    )


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
        has_accepted_baseline=captured.has_accepted_baseline,
        accepted_analyzer_content=captured.accepted_analyzer_content,
        appended_analyzer_content=captured.appended_analyzer_content,
        appended_source_line_ordinals=captured.appended_source_line_ordinals,
        verified_source=verified,
        prepared_read=captured,
        observation=RodexSessionCodexThreadObservation(
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
            history_inheritance_kind=verified.history_inheritance_kind,
        ),
        authenticated_source=authenticated,
    )


def _view_matches_source_reads(
    statistics: RodexAnalyticsStatisticsCheckpoint | None,
    sources: Sequence[RodexSessionCodexThread],
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
            rodex_session_id=config.rodex_session_id,
            rodex_registry_id=config.rodex_registry_id,
            runtime_id=config.runtime_id,
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
    except RodexDatabaseMovedError:
        raise
    except Exception:
        return
