"""Stateful pinned-analyzer boundary for the Rodex analytics spine."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import median
from typing import Any, Protocol

from rodex_registry import (
    CodexThreadId,
    SessionStatisticsProjection,
    StatisticsDistribution,
    StatisticsNamedCount,
    StatisticsProjectionError,
    TurnStatisticsProjection,
    parse_session_statistics_snapshot,
    parse_turn_statistics_snapshot,
)


class RodexAnalyticsError(RuntimeError):
    """The optional analytics subsystem could not satisfy a request."""


@dataclass(frozen=True, slots=True)
class AnalyticsCalculation:
    """Usable session and turn projections from one analyzer calculation."""

    statistics_projection: SessionStatisticsProjection
    coverage_state: str


@dataclass(frozen=True, slots=True)
class AnalyticsAnalyzerSource:
    """One source's initialization bytes and candidate complete-line suffix."""

    codex_thread_id: CodexThreadId
    analyzer_content: bytes
    appended_analyzer_content: bytes


class AnalyticsBoundary(Protocol):
    def analyze_rollouts(
        self, sources: Sequence[AnalyticsAnalyzerSource], user_id: str
    ) -> AnalyticsCalculation: ...

    def accept_batch(self) -> None: ...


AnalyticsBoundaryFactory = Callable[[], AnalyticsBoundary]


class _AnalyzerLibrary(Protocol):
    def create_new_codex_protocol_id(self, user_id: str) -> object: ...

    def load_file(self, protocol_id: str, path: Path) -> object: ...

    def get_stats(
        self, protocol_id: str, *, include_turn_statistics: bool = False
    ) -> object: ...

    def close(self) -> object: ...


class CodexProtocolAnalyticsAdapter:
    """Full-replay adapter retained as the semantic test oracle."""

    def analyze_rollouts(
        self, sources: Sequence[AnalyticsAnalyzerSource], user_id: str
    ) -> AnalyticsCalculation:
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
            for index, source in enumerate(sources):
                loaded = _load_analyzer_bytes(
                    library, protocol_id, source.analyzer_content, index
                )
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
            return AnalyticsCalculation(
                statistics_projection=_parse_projection(stats),
                coverage_state=coverage_state,
            )
        finally:
            with suppress(Exception):
                library.close()

    def accept_batch(self) -> None:
        """Full replay owns no resident candidate state."""


@dataclass(slots=True)
class _SourceState:
    session_id: str
    pending_content: bytes = b""


@dataclass(slots=True)
class _OrderNode:
    key: int
    priority: int
    count: int = 1
    size: int = 1
    total: int = 0
    left: _OrderNode | None = None
    right: _OrderNode | None = None

    def __post_init__(self) -> None:
        self.total = self.key


class _OrderStatisticMultiset:
    """Exact integer distribution with logarithmic insert, remove, and selection."""

    def __init__(self, values: Sequence[int] = ()) -> None:
        self._root: _OrderNode | None = None
        for value in values:
            self.add(value)

    def add(self, value: int) -> None:
        self._root = self._insert(self._root, value)

    def discard(self, value: int) -> None:
        self._root = self._remove(self._root, value)

    def distribution(self, kind: str) -> StatisticsDistribution:
        root = self._root
        if root is None:
            return StatisticsDistribution(kind, 0, 0, None, None, None, None, None)
        middle = root.size // 2
        median_value = (
            float(self._select(root, middle))
            if root.size % 2
            else round(
                (self._select(root, middle - 1) + self._select(root, middle)) / 2,
                1,
            )
        )
        return StatisticsDistribution(
            distribution_kind=kind,
            observation_count=root.size,
            total=root.total,
            median=median_value,
            p75=self._percentile(root, 75),
            p90=self._percentile(root, 90),
            p95=self._percentile(root, 95),
            maximum=self._select(root, root.size - 1),
        )

    @classmethod
    def _insert(cls, node: _OrderNode | None, key: int) -> _OrderNode:
        if node is None:
            return _OrderNode(key, _order_priority(key))
        if key == node.key:
            node.count += 1
        elif key < node.key:
            node.left = cls._insert(node.left, key)
            if node.left.priority < node.priority:
                node = cls._rotate_right(node)
        else:
            node.right = cls._insert(node.right, key)
            if node.right.priority < node.priority:
                node = cls._rotate_left(node)
        cls._refresh(node)
        return node

    @classmethod
    def _remove(cls, node: _OrderNode | None, key: int) -> _OrderNode | None:
        if node is None:
            raise RodexAnalyticsError("incremental distribution lost a value")
        if key < node.key:
            node.left = cls._remove(node.left, key)
        elif key > node.key:
            node.right = cls._remove(node.right, key)
        elif node.count > 1:
            node.count -= 1
        elif node.left is None:
            return node.right
        elif node.right is None:
            return node.left
        elif node.left.priority < node.right.priority:
            node = cls._rotate_right(node)
            node.right = cls._remove(node.right, key)
        else:
            node = cls._rotate_left(node)
            node.left = cls._remove(node.left, key)
        cls._refresh(node)
        return node

    @staticmethod
    def _select(node: _OrderNode, ordinal: int) -> int:
        current = node
        remaining = ordinal
        while True:
            left_size = 0 if current.left is None else current.left.size
            if remaining < left_size:
                assert current.left is not None
                current = current.left
            elif remaining < left_size + current.count:
                return current.key
            else:
                remaining -= left_size + current.count
                assert current.right is not None
                current = current.right

    @classmethod
    def _percentile(cls, node: _OrderNode, percentile: int) -> int:
        ordinal = max(0, math.ceil(percentile / 100 * node.size) - 1)
        return cls._select(node, ordinal)

    @staticmethod
    def _refresh(node: _OrderNode) -> None:
        node.size = node.count
        node.total = node.key * node.count
        if node.left is not None:
            node.size += node.left.size
            node.total += node.left.total
        if node.right is not None:
            node.size += node.right.size
            node.total += node.right.total

    @classmethod
    def _rotate_left(cls, node: _OrderNode) -> _OrderNode:
        replacement = node.right
        assert replacement is not None
        node.right = replacement.left
        replacement.left = node
        cls._refresh(node)
        cls._refresh(replacement)
        return replacement

    @classmethod
    def _rotate_right(cls, node: _OrderNode) -> _OrderNode:
        replacement = node.left
        assert replacement is not None
        node.left = replacement.right
        replacement.right = node
        cls._refresh(node)
        cls._refresh(replacement)
        return replacement


def _order_priority(value: int) -> int:
    mixed = (value + 0x9E3779B97F4A7C15) & ((1 << 64) - 1)
    mixed = (mixed ^ (mixed >> 30)) * 0xBF58476D1CE4E5B9 & ((1 << 64) - 1)
    mixed = (mixed ^ (mixed >> 27)) * 0x94D049BB133111EB & ((1 << 64) - 1)
    return mixed ^ (mixed >> 31)


@dataclass(slots=True)
class _BatchTouches:
    turn_keys: set[tuple[str, str]] = field(default_factory=set)
    command_hashes: set[str] = field(default_factory=set)
    path_hashes: set[str] = field(default_factory=set)
    tool_keys: set[tuple[str, str]] = field(default_factory=set)


class _IncrementalProjectionIndex:
    """Maintain aggregate and changed-turn projections without walking history."""

    def __init__(self, analyzer: Any, projection: SessionStatisticsProjection) -> None:
        self.turns: dict[tuple[CodexThreadId, str], TurnStatisticsProjection] = {}
        self.outcomes: Counter[str] = Counter()
        self.workspaces: Counter[str] = Counter()
        self.models: Counter[str] = Counter()
        self.reasoning_efforts: Counter[str] = Counter()
        self.local_hours: Counter[int] = Counter()
        self.hands_on = 0
        self.failed_command_turns = 0
        self.recovered_turns = 0
        self.edited_turns = 0
        self.verified_after_edit = 0
        self.web_turns = 0
        self.web_follow_through = 0
        self.completed_durations = _OrderStatisticMultiset()
        self.ttfts = _OrderStatisticMultiset()
        self.turn_tokens = _OrderStatisticMultiset()
        self.commands_per_turn = _OrderStatisticMultiset()
        self.tools_per_turn = _OrderStatisticMultiset()
        self.files_per_turn = _OrderStatisticMultiset()
        self.command_durations = _OrderStatisticMultiset(analyzer.command_durations_ms)
        self._command_duration_count = len(analyzer.command_durations_ms)
        self._context_observation_count = len(analyzer.context_observations)
        self._context_high_water = max(analyzer.context_observations, default=0.0)
        self._command_hash_counts = dict(analyzer.command_hashes)
        self.repeated_commands = sum(
            count for count in self._command_hash_counts.values() if count > 1
        )
        self._path_counts = dict(analyzer.path_operation_counts)
        self.revisited_paths = sum(count >= 2 for count in self._path_counts.values())
        self._tool_requests = dict(analyzer.tool_requests)
        self.tool_names = Counter(self._tool_requests.values())
        self._paired_tool_keys = set(analyzer.tool_requests) & analyzer.tool_outputs
        self._base = projection
        for turn in projection.turn_statistics:
            self._replace_turn(turn)

    def refresh(
        self,
        analyzer: Any,
        touches: _BatchTouches,
        turn_report: Callable[[object], Mapping[str, object]],
        *,
        source_count: int,
        revision: int,
        new_event_type_warnings: int,
    ) -> SessionStatisticsProjection:
        changed_turns: list[TurnStatisticsProjection] = []
        for session_id, turn_id in touches.turn_keys:
            turn = analyzer.turns.get((session_id, turn_id))
            if turn is None:
                continue
            parsed = parse_turn_statistics_snapshot(turn_report(turn))
            self._replace_turn(parsed)
            changed_turns.append(parsed)
        for value in analyzer.command_durations_ms[self._command_duration_count :]:
            self.command_durations.add(value)
        self._command_duration_count = len(analyzer.command_durations_ms)
        for ratio in analyzer.context_observations[self._context_observation_count :]:
            self._context_high_water = max(self._context_high_water, ratio)
        self._context_observation_count = len(analyzer.context_observations)
        for command_hash in touches.command_hashes:
            previous = self._command_hash_counts.get(command_hash, 0)
            current = analyzer.command_hashes[command_hash]
            self.repeated_commands += _repeat_contribution(current) - _repeat_contribution(
                previous
            )
            self._command_hash_counts[command_hash] = current
        for path_hash in touches.path_hashes:
            previous = self._path_counts.get(path_hash, 0)
            current = analyzer.path_operation_counts[path_hash]
            self.revisited_paths += int(current >= 2) - int(previous >= 2)
            self._path_counts[path_hash] = current
        for tool_key in touches.tool_keys:
            previous_name = self._tool_requests.get(tool_key)
            current_name = analyzer.tool_requests.get(tool_key)
            if previous_name is None and current_name is not None:
                self._tool_requests[tool_key] = current_name
                self.tool_names[current_name] += 1
            if (
                tool_key not in self._paired_tool_keys
                and tool_key in analyzer.tool_requests
                and tool_key in analyzer.tool_outputs
            ):
                self._paired_tool_keys.add(tool_key)
        changed_turns.sort(
            key=lambda item: (
                str(item.codex_thread_id),
                "" if item.started_at_utc is None else item.started_at_utc,
                item.codex_turn_id,
            )
        )
        distributions = (
            self.completed_durations.distribution("completed_turn_duration_ms"),
            self.ttfts.distribution("time_to_first_token_ms"),
            self.turn_tokens.distribution("per_turn_total_tokens"),
            self.command_durations.distribution("command_duration_ms"),
            self.commands_per_turn.distribution("commands_per_turn"),
            self.tools_per_turn.distribution("tool_requests_per_turn"),
            self.files_per_turn.distribution("files_per_turn"),
        )
        named_counts = tuple(
            item
            for kind, counts, use_most_common in (
                ("command_exit_status", analyzer.command_statuses, False),
                ("command_family", analyzer.command_families, True),
                ("model_tool", self.tool_names, True),
                ("file_change_type", analyzer.file_change_types, False),
                ("web_action", analyzer.web_action_types, False),
                ("collaboration_tool", analyzer.collaboration_tools, True),
                ("model", self.models, True),
                ("reasoning_effort", self.reasoning_efforts, True),
                ("goal_status", analyzer.goal_statuses, False),
            )
            for item in _named_count_projections(kind, counts, use_most_common)
        )
        total_turns = len(self.turns)
        completed = self.outcomes["completed"]
        aborted = self.outcomes["aborted"]
        open_turns = self.outcomes["open"]
        workspace_turns = sum(self.workspaces.values())
        zero_exit = analyzer.command_statuses["zero_exit"]
        nonzero_exit = analyzer.command_statuses["nonzero_exit"]
        input_tokens = analyzer.token_totals["input_tokens"]
        output_tokens = analyzer.token_totals["output_tokens"]
        latest_context = list(analyzer.last_context_ratio.values())
        self._base = replace(
            self._base,
            analyzer_event_count=analyzer.records,
            analyzer_source_count=source_count,
            history_sessions_count=len(analyzer.sessions),
            history_records_count=analyzer.records,
            history_malformed_records_count=analyzer.malformed_lines,
            turns_started_count=total_turns,
            turns_completed_count=completed,
            turns_aborted_count=aborted,
            turns_open_count=open_turns,
            input_tokens=analyzer.token_totals["input_tokens"],
            cached_input_tokens=analyzer.token_totals["cached_input_tokens"],
            cache_write_input_tokens=analyzer.token_totals["cache_write_input_tokens"],
            output_tokens=analyzer.token_totals["output_tokens"],
            reasoning_output_tokens=analyzer.token_totals["reasoning_output_tokens"],
            total_tokens=analyzer.token_totals["total_tokens"],
            context_observation_count=len(analyzer.context_observations),
            context_latest_session_median_percent=(
                None if not latest_context else round(median(latest_context) * 100, 1)
            ),
            context_high_water_percent=round(self._context_high_water * 100, 1),
            commands_executed_count=len(analyzer.commands),
            model_tool_requests_count=len(analyzer.tool_requests),
            model_tool_outputs_paired_count=len(self._paired_tool_keys),
            file_change_operations_count=len(analyzer.file_operations),
            file_change_distinct_paths_count=len(analyzer.path_operation_counts),
            file_change_occurrences_count=sum(analyzer.file_change_types.values()),
            web_operations_count=len(analyzer.web_operations),
            web_queries_count=analyzer.web_query_count,
            web_result_records_count=analyzer.web_result_count,
            web_distinct_result_or_action_urls_count=len(analyzer.web_urls),
            collaboration_operations_count=len(analyzer.collaboration_operations),
            collaboration_agents_started_count=len(analyzer.agent_threads),
            compactions_count=len(analyzer.compactions),
            distinct_workspaces_count=len(self.workspaces),
            typical_turns_count=total_turns,
            hands_on_turn_count=self.hands_on,
            hands_on_turn_rate_percent=_rate(self.hands_on, total_turns),
            turns_with_nonzero_command_count=self.failed_command_turns,
            turns_subsequently_completed_count=self.recovered_turns,
            completed_after_nonzero_command_percent=_rate(
                self.recovered_turns, self.failed_command_turns
            ),
            command_zero_exit_rate_percent=_rate(zero_exit, zero_exit + nonzero_exit),
            repeated_command_execution_count=self.repeated_commands,
            exact_command_repeat_rate_percent=_rate(
                self.repeated_commands, len(analyzer.commands)
            ),
            cached_input_share_percent=_rate(
                analyzer.token_totals["cached_input_tokens"], input_tokens
            ),
            reasoning_output_share_percent=_rate(
                analyzer.token_totals["reasoning_output_tokens"], output_tokens
            ),
            edited_turns_count=self.edited_turns,
            verified_after_edit_count=self.verified_after_edit,
            edit_then_verify_percent=_rate(self.verified_after_edit, self.edited_turns),
            web_turns_count=self.web_turns,
            web_later_command_or_file_work_count=self.web_follow_through,
            web_follow_through_percent=_rate(self.web_follow_through, self.web_turns),
            revisited_distinct_path_count=self.revisited_paths,
            file_revisit_rate_percent=_rate(
                self.revisited_paths, len(analyzer.path_operation_counts)
            ),
            workspace_tagged_turn_count=workspace_turns,
            turns_in_busiest_workspace_count=max(self.workspaces.values(), default=0),
            busiest_workspace_turn_share_percent=_rate(
                max(self.workspaces.values(), default=0), workspace_turns
            ),
            turns_with_local_hour_count=sum(self.local_hours.values()),
            busiest_local_hour=(
                self.local_hours.most_common(1)[0][0] if self.local_hours else None
            ),
            turns_in_busiest_local_hour_count=(
                self.local_hours.most_common(1)[0][1] if self.local_hours else 0
            ),
            goal_updates_count=analyzer.goal_updates,
            audit_token_snapshots_count=analyzer.token_snapshots,
            audit_repeated_token_snapshots_count=analyzer.token_repeated_snapshots,
            audit_token_epochs_count=sum(analyzer.token_epochs.values()),
            audit_duplicate_operations_ignored_count=analyzer.duplicate_operations,
            audit_duplicate_terminals_ignored_count=analyzer.duplicate_terminals,
            audit_terminal_events_without_start_ignored_count=(
                analyzer.terminal_without_start
            ),
            audit_new_event_type_warnings_count=new_event_type_warnings,
            distributions=distributions,
            named_counts=named_counts,
            turn_statistics=tuple(changed_turns),
        )
        return self._base

    def _replace_turn(self, turn: TurnStatisticsProjection) -> None:
        key = (turn.codex_thread_id, turn.codex_turn_id)
        previous = self.turns.get(key)
        if previous is not None:
            self._adjust_turn(previous, -1)
        self.turns[key] = turn
        self._adjust_turn(turn, 1)

    def _adjust_turn(self, turn: TurnStatisticsProjection, direction: int) -> None:
        _adjust_counter(self.outcomes, turn.outcome, direction)
        if turn.workspace_digest is not None:
            _adjust_counter(self.workspaces, turn.workspace_digest, direction)
        if turn.model is not None:
            _adjust_counter(self.models, turn.model, direction)
        if turn.reasoning_effort is not None:
            _adjust_counter(self.reasoning_efforts, turn.reasoning_effort, direction)
        if turn.local_start_hour is not None:
            _adjust_counter(self.local_hours, turn.local_start_hour, direction)
        self.hands_on += direction * int(turn.hands_on)
        has_failed_command = any(
            item.count_kind == "command_exit_status"
            and item.count_name == "nonzero_exit"
            and item.occurrence_count > 0
            for item in turn.named_counts
        )
        self.failed_command_turns += direction * int(has_failed_command)
        self.recovered_turns += direction * int(turn.completed_after_nonzero_command)
        self.edited_turns += direction * int(turn.file_change_operations_count > 0)
        self.verified_after_edit += direction * int(turn.edited_then_verified)
        self.web_turns += direction * int(turn.web_operations_count > 0)
        self.web_follow_through += direction * int(
            turn.web_research_followed_by_command_or_file_work
        )
        _adjust_distribution(
            self.completed_durations,
            turn.duration_ms if turn.outcome == "completed" else None,
            direction,
        )
        _adjust_distribution(
            self.ttfts,
            turn.time_to_first_token_ms if turn.outcome == "completed" else None,
            direction,
        )
        for distribution, value in (
            (self.turn_tokens, turn.total_tokens),
            (self.commands_per_turn, turn.commands_executed_count),
            (self.tools_per_turn, turn.model_tool_requests_count),
            (self.files_per_turn, turn.file_change_distinct_paths_count),
        ):
            _adjust_distribution(distribution, value, direction)


def _adjust_distribution(
    distribution: _OrderStatisticMultiset,
    value: int | None,
    direction: int,
) -> None:
    if value is None:
        return
    if direction > 0:
        distribution.add(value)
    else:
        distribution.discard(value)


def _adjust_counter(counter: Counter[Any], key: Any, direction: int) -> None:
    counter[key] += direction
    if counter[key] == 0:
        del counter[key]


def _repeat_contribution(count: int) -> int:
    return count if count > 1 else 0


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else None


def _named_count_projections(
    kind: str,
    counts: Mapping[Any, int],
    use_most_common: bool,
) -> tuple[StatisticsNamedCount, ...]:
    items = (
        counts.most_common()  # type: ignore[union-attr]
        if use_most_common and isinstance(counts, Counter)
        else sorted(counts.items(), key=lambda item: str(item[0]))
    )
    return tuple(
        StatisticsNamedCount(kind, str(name), count) for name, count in items if count > 0
    )


class StatefulCodexProtocolAnalyticsAdapter:
    """Retain the pinned analyzer ledgers and consume only candidate suffixes."""

    def __init__(self) -> None:
        try:
            statistics = importlib.import_module("codex_protocol_log_analyzer.statistics")
            library = importlib.import_module("codex_protocol_log_analyzer.library")
            analyzer_type = statistics._StatisticalAnalyzer
            self._new_event_name = library._new_event_name
            self._command_identity = statistics._command_identity
            self._digest = statistics._digest
            self._analyzer = analyzer_type()
        except Exception as error:
            raise RodexAnalyticsError(
                f"pinned analyzer state contract is unavailable: {error}"
            ) from error
        self._sources: dict[CodexThreadId, _SourceState] = {}
        self._user_id: str | None = None
        self._revision = 0
        self._unknown_event_types_by_source: dict[CodexThreadId, set[str]] = {}
        self._coverage_gapped = False
        self._candidate_calculation: AnalyticsCalculation | None = None
        self._projection_index: _IncrementalProjectionIndex | None = None
        self._poisoned_reason: str | None = None

    def analyze_rollouts(
        self, sources: Sequence[AnalyticsAnalyzerSource], user_id: str
    ) -> AnalyticsCalculation:
        if self._poisoned_reason is not None:
            raise RodexAnalyticsError(
                f"stateful analyzer requires clean restart: {self._poisoned_reason}"
            )
        if self._user_id is None:
            self._user_id = user_id
        elif self._user_id != user_id:
            raise RodexAnalyticsError("analytics user identity changed")
        by_thread = {source.codex_thread_id: source for source in sources}
        if len(by_thread) != len(sources):
            raise RodexAnalyticsError("analyzer batch contains duplicate thread identity")
        prepared: list[
            tuple[AnalyticsAnalyzerSource, bytes, list[dict[str, Any]], int]
        ] = []
        touches = _BatchTouches()
        for source in sources:
            state = self._sources.get(source.codex_thread_id)
            offered = (
                source.analyzer_content
                if state is None
                else source.appended_analyzer_content
            )
            pending = b"" if state is None else state.pending_content
            if pending and not offered.startswith(pending):
                raise RodexAnalyticsError(
                    f"analyzer retry diverged for thread {source.codex_thread_id}"
                )
            new_content = offered[len(pending) :]
            records, malformed = _decode_complete_records(new_content)
            _validate_source_records(source.codex_thread_id, records)
            prepared.append((source, new_content, records, malformed))

        changed = False
        try:
            for source, new_content, records, malformed in prepared:
                state = self._sources.get(source.codex_thread_id)
                if state is None:
                    identity_digest = hashlib.sha256(
                        str(source.codex_thread_id).encode()
                    ).hexdigest()
                    source_key = f"source:{identity_digest}"
                    state = _SourceState(source_key)
                    self._sources[source.codex_thread_id] = state
                    self._analyzer.sessions.add(source_key)
                if malformed:
                    self._analyzer.malformed_lines += malformed
                    self._coverage_gapped = True
                    changed = True
                for record in records:
                    self._consume_record(
                        source.codex_thread_id,
                        state,
                        record,
                        touches,
                    )
                    event_name = self._new_event_name(record)
                    if event_name is not None:
                        self._unknown_event_types_by_source.setdefault(
                            source.codex_thread_id, set()
                        ).add(str(event_name))
                        self._coverage_gapped = True
                    changed = True
                state.pending_content += new_content
        except Exception as error:
            self._poisoned_reason = type(error).__name__
            raise RodexAnalyticsError(
                "pinned analyzer failed while consuming a validated append"
            ) from error
        if not changed and self._candidate_calculation is not None:
            return self._candidate_calculation
        if changed:
            self._revision += 1
        warning_count = sum(
            len(names) for names in self._unknown_event_types_by_source.values()
        )
        projection_index = self._projection_index
        if projection_index is None:
            report = self._analyzer.report(source="rodex stateful rollout sources")
            snapshot = report.to_dict()
            snapshot.pop("source")
            audit = dict(snapshot["audit"])
            audit["new_event_type_warnings"] = warning_count
            snapshot.update(
                {
                    "protocol_id": "rodex_stateful_analyzer",
                    "user_id": self._user_id,
                    "revision": self._revision,
                    "event_count": self._analyzer.records,
                    "source_count": len(self._sources),
                    "selected_stats": None,
                    "audit": audit,
                }
            )
            projection = _parse_projection(snapshot)
            projection_index = _IncrementalProjectionIndex(self._analyzer, projection)
            self._projection_index = projection_index
        else:
            projection = projection_index.refresh(
                self._analyzer,
                touches,
                lambda turn: self._analyzer._turn_statistical_report(turn).to_dict(),
                source_count=len(self._sources),
                revision=self._revision,
                new_event_type_warnings=warning_count,
            )
        calculation = AnalyticsCalculation(
            statistics_projection=projection,
            coverage_state="gapped" if self._coverage_gapped else "complete",
        )
        self._candidate_calculation = calculation
        return calculation

    def accept_batch(self) -> None:
        """Acknowledge candidate bytes only after their SQL projection commits."""
        for state in self._sources.values():
            state.pending_content = b""
        self._candidate_calculation = None

    def _consume_record(
        self,
        thread_id: CodexThreadId,
        state: _SourceState,
        record: dict[str, Any],
        touches: _BatchTouches,
    ) -> None:
        self._analyzer.records += 1
        self._analyzer.sequence += 1
        payload_value = record.get("payload")
        payload = dict(payload_value) if isinstance(payload_value, Mapping) else {}
        if record.get("type") == "session_meta":
            identifier = payload.get("id")
            assert identifier == str(thread_id)
            self._analyzer.sessions.discard(state.session_id)
            state.session_id = identifier
            self._analyzer.sessions.add(identifier)
            return
        self._remember_touches(record, payload, state.session_id, touches)
        self._analyzer._consume(record, payload, state.session_id)

    def _remember_touches(
        self,
        record: Mapping[str, Any],
        payload: Mapping[str, Any],
        session_id: str,
        touches: _BatchTouches,
    ) -> None:
        record_type = record.get("type")
        payload_type = payload.get("type")
        turn_id = payload.get("turn_id") or self._analyzer.active_turn.get(session_id)
        if isinstance(turn_id, str) and turn_id:
            touches.turn_keys.add((session_id, turn_id))
        command: Mapping[str, Any] | None = None
        changed_paths: Mapping[str, Any] | None = None
        if record_type == "event_msg" and payload_type == "item_completed":
            item = payload.get("item")
            if isinstance(item, Mapping) and item.get("type") == "CommandExecution":
                command = item
            if isinstance(item, Mapping) and item.get("type") == "FileChange":
                changes = item.get("changes")
                changed_paths = changes if isinstance(changes, Mapping) else None
        elif record_type == "event_msg" and payload_type == "exec_command_end":
            command = payload
        elif record_type == "event_msg" and payload_type == "patch_apply_end":
            changes = payload.get("changes")
            changed_paths = changes if isinstance(changes, Mapping) else None
        if command is not None:
            _family, command_hash = self._command_identity(dict(command))
            if command_hash:
                touches.command_hashes.add(command_hash)
        if changed_paths is not None:
            for path, change in changed_paths.items():
                if isinstance(path, str):
                    touches.path_hashes.add(self._digest(path))
                if isinstance(change, Mapping):
                    move_path = change.get("move_path")
                    if isinstance(move_path, str) and move_path:
                        touches.path_hashes.add(self._digest(move_path))
        if record_type == "response_item":
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                touches.tool_keys.add((session_id, call_id))


def _decode_complete_records(content: bytes) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            malformed += 1
            continue
        if not isinstance(record, dict):
            malformed += 1
            continue
        records.append(record)
    return records, malformed


def _validate_source_records(
    thread_id: CodexThreadId, records: Sequence[Mapping[str, Any]]
) -> None:
    """Reject identity errors before the pinned analyzer mutates resident state."""
    for record in records:
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        identifier = payload.get("id") if isinstance(payload, Mapping) else None
        if identifier != str(thread_id):
            raise RodexAnalyticsError(
                f"analyzer source identity changed for thread {thread_id}"
            )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number: {value}")


def _parse_projection(stats: Mapping[str, Any]) -> SessionStatisticsProjection:
    try:
        return parse_session_statistics_snapshot(stats)
    except StatisticsProjectionError as error:
        raise RodexAnalyticsError(
            f"analyzer statistics contract mismatch: {error}"
        ) from error


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


def _load_analyzer_bytes(
    library: _AnalyzerLibrary,
    protocol_id: str,
    content: bytes,
    source_index: int,
) -> object:
    descriptor = _create_memory_file(f"rodex-analytics-{source_index}")
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise RodexAnalyticsError("could not populate memory-backed analyzer file")
            remaining = remaining[written:]
        _seal_memory_file(descriptor)
        return library.load_file(protocol_id, Path(f"/proc/self/fd/{descriptor}"))
    except OSError as error:
        raise RodexAnalyticsError(
            f"could not prepare memory-backed analyzer file: {error}"
        ) from error
    finally:
        os.close(descriptor)


def _create_memory_file(name: str) -> int:
    flags = 0x0001 | 0x0002
    if hasattr(os, "memfd_create"):
        return os.memfd_create(name, flags)
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        memfd_create = libc.memfd_create
        memfd_create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
        memfd_create.restype = ctypes.c_int
        descriptor = memfd_create(name.encode(), flags)
    except (AttributeError, ImportError) as error:
        raise RodexAnalyticsError("memory-backed analyzer files are unavailable") from error
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise RodexAnalyticsError(
            f"could not create memory-backed analyzer file: {os.strerror(error_number)}"
        )
    return int(descriptor)


def _seal_memory_file(descriptor: int) -> None:
    try:
        import fcntl

        fcntl.fcntl(descriptor, 1033, 0x0001 | 0x0002 | 0x0004 | 0x0008)
    except (ImportError, OSError) as error:
        raise RodexAnalyticsError(
            f"could not seal memory-backed analyzer file: {error}"
        ) from error
