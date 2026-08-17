"""Strict relational projection of the current Codex analyzer statistics contract."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from .identity import CodexSessionId, parse_codex_session_id

__all__ = [
    "SessionStatisticsProjection",
    "StatisticsDistribution",
    "StatisticsNamedCount",
    "StatisticsProjectionError",
    "TurnStatisticsProjection",
    "parse_session_statistics_snapshot",
    "session_statistics_as_dict",
    "turn_statistics_as_dict",
    "validate_session_statistics_projection",
]


class StatisticsProjectionError(ValueError):
    """The analyzer result does not match the supported relational vocabulary."""


@dataclass(frozen=True, slots=True)
class StatisticsDistribution:
    """One fully materialized analyzer distribution."""

    distribution_kind: str
    observation_count: int
    total: int
    median: float | None
    p75: int | None
    p90: int | None
    p95: int | None
    maximum: int | None


@dataclass(frozen=True, slots=True)
class StatisticsNamedCount:
    """One base value from a dynamic analyzer count map."""

    count_kind: str
    count_name: str
    occurrence_count: int


@dataclass(frozen=True, slots=True)
class TurnStatisticsProjection:
    """All current derived values for one exact Codex turn."""

    codex_session_id: CodexSessionId
    codex_turn_id: str
    started_at_utc: str | None
    terminal_at_utc: str | None
    outcome: str
    duration_ms: int | None
    time_to_first_token_ms: int | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    context_observation_count: int
    context_high_water_percent: float
    commands_executed_count: int
    command_duration_observation_count: int
    command_duration_total_ms: int
    command_duration_median_ms: float | None
    command_duration_p75_ms: int | None
    command_duration_p90_ms: int | None
    command_duration_p95_ms: int | None
    command_duration_maximum_ms: int | None
    model_tool_requests_count: int
    model_tool_outputs_paired_count: int
    file_change_operations_count: int
    file_change_distinct_paths_count: int
    file_change_occurrences_count: int
    web_operations_count: int
    web_queries_count: int
    web_result_records_count: int
    web_distinct_result_or_action_urls_count: int
    collaboration_operations_count: int
    collaboration_agents_started_count: int
    compactions_count: int
    workspace_digest: str | None
    model: str | None
    local_start_hour: int | None
    hands_on: bool
    completed_after_nonzero_command: bool
    cached_input_share_percent: float | None
    reasoning_output_share_percent: float | None
    edited_then_verified: bool
    web_research_followed_by_command_or_file_work: bool
    goal_updates_count: int
    named_counts: tuple[StatisticsNamedCount, ...]

    @property
    def command_duration(self) -> StatisticsDistribution:
        """Return the flattened SQL duration columns as their domain value."""
        return StatisticsDistribution(
            distribution_kind="command_duration_ms",
            observation_count=self.command_duration_observation_count,
            total=self.command_duration_total_ms,
            median=self.command_duration_median_ms,
            p75=self.command_duration_p75_ms,
            p90=self.command_duration_p90_ms,
            p95=self.command_duration_p95_ms,
            maximum=self.command_duration_maximum_ms,
        )


@dataclass(frozen=True, slots=True)
class SessionStatisticsProjection:
    """Complete current analyzer snapshot with no temporary analyzer identity."""

    analyzer_event_count: int
    analyzer_source_count: int
    history_sessions_count: int
    history_records_count: int
    history_malformed_records_count: int
    turns_started_count: int
    turns_completed_count: int
    turns_aborted_count: int
    turns_open_count: int
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int
    context_observation_count: int
    context_latest_session_median_percent: float | None
    context_high_water_percent: float
    commands_executed_count: int
    model_tool_requests_count: int
    model_tool_outputs_paired_count: int
    file_change_operations_count: int
    file_change_distinct_paths_count: int
    file_change_occurrences_count: int
    web_operations_count: int
    web_queries_count: int
    web_result_records_count: int
    web_distinct_result_or_action_urls_count: int
    collaboration_operations_count: int
    collaboration_agents_started_count: int
    compactions_count: int
    distinct_workspaces_count: int
    typical_turns_count: int
    hands_on_turn_count: int
    hands_on_turn_rate_percent: float | None
    turns_with_nonzero_command_count: int
    turns_subsequently_completed_count: int
    completed_after_nonzero_command_percent: float | None
    command_zero_exit_rate_percent: float | None
    repeated_command_execution_count: int
    exact_command_repeat_rate_percent: float | None
    cached_input_share_percent: float | None
    reasoning_output_share_percent: float | None
    edited_turns_count: int
    verified_after_edit_count: int
    edit_then_verify_percent: float | None
    web_turns_count: int
    web_later_command_or_file_work_count: int
    web_follow_through_percent: float | None
    revisited_distinct_path_count: int
    file_revisit_rate_percent: float | None
    workspace_tagged_turn_count: int
    turns_in_busiest_workspace_count: int
    busiest_workspace_turn_share_percent: float | None
    turns_with_local_hour_count: int
    busiest_local_hour: int | None
    turns_in_busiest_local_hour_count: int
    goal_updates_count: int
    audit_privacy: str
    audit_percentile_method: str
    audit_token_method: str
    audit_token_snapshots_count: int
    audit_repeated_token_snapshots_count: int
    audit_token_epochs_count: int
    audit_duplicate_operations_ignored_count: int
    audit_duplicate_terminals_ignored_count: int
    audit_terminal_events_without_start_ignored_count: int
    audit_new_event_type_warnings_count: int
    distributions: tuple[StatisticsDistribution, ...]
    named_counts: tuple[StatisticsNamedCount, ...]
    audit_limits: tuple[str, ...]
    turn_statistics: tuple[TurnStatisticsProjection, ...]


def session_statistics_as_dict(
    projection: SessionStatisticsProjection,
) -> dict[str, object]:
    """Reconstruct the analyzer-shaped aggregate at the presentation boundary."""
    distributions = {
        item.distribution_kind: _distribution_as_dict(item)
        for item in projection.distributions
    }
    return {
        "event_count": projection.analyzer_event_count,
        "source_count": projection.analyzer_source_count,
        "selected_stats": None,
        "must_have_basic_stats": {
            "history_coverage": {
                "sessions": projection.history_sessions_count,
                "records": projection.history_records_count,
                "malformed_records": projection.history_malformed_records_count,
            },
            "turns": {
                "started": projection.turns_started_count,
                "completed": projection.turns_completed_count,
                "aborted": projection.turns_aborted_count,
                "open": projection.turns_open_count,
            },
            "completed_turn_duration_ms": distributions["completed_turn_duration_ms"],
            "time_to_first_token_ms": distributions["time_to_first_token_ms"],
            "token_usage": {
                **_token_values(projection),
                "per_turn_total_tokens": distributions["per_turn_total_tokens"],
            },
            "context_window": {
                "observation_count": projection.context_observation_count,
                "latest_session_median_percent": (
                    projection.context_latest_session_median_percent
                ),
                "high_water_percent": projection.context_high_water_percent,
            },
            "commands_executed": {
                "count": projection.commands_executed_count,
                "exit_status": _count_map(projection.named_counts, "command_exit_status"),
                "duration_ms": distributions["command_duration_ms"],
                "families": _count_map(projection.named_counts, "command_family"),
            },
            "model_tool_requests": {
                "count": projection.model_tool_requests_count,
                "output_paired": projection.model_tool_outputs_paired_count,
                "by_tool": _count_map(projection.named_counts, "model_tool"),
            },
            "file_changes": {
                "operations": projection.file_change_operations_count,
                "distinct_paths": projection.file_change_distinct_paths_count,
                "change_occurrences": projection.file_change_occurrences_count,
                "by_type": _count_map(projection.named_counts, "file_change_type"),
            },
            "web_activity": {
                "operations": projection.web_operations_count,
                "queries": projection.web_queries_count,
                "result_records": projection.web_result_records_count,
                "distinct_result_or_action_urls": (
                    projection.web_distinct_result_or_action_urls_count
                ),
                "by_action": _count_map(projection.named_counts, "web_action"),
            },
            "collaboration": {
                "operations": projection.collaboration_operations_count,
                "agents_started": projection.collaboration_agents_started_count,
                "by_tool": _count_map(projection.named_counts, "collaboration_tool"),
            },
            "compactions": projection.compactions_count,
            "workspaces_and_models": {
                "distinct_workspaces": projection.distinct_workspaces_count,
                "models": _count_map(projection.named_counts, "model"),
            },
        },
        "recommended_insight_stats": {
            "typical_turn_anatomy": {
                "turns": projection.typical_turns_count,
                "commands_per_turn": distributions["commands_per_turn"],
                "tool_requests_per_turn": distributions["tool_requests_per_turn"],
                "files_per_turn": distributions["files_per_turn"],
            },
            "hands_on_turn_rate_percent": projection.hands_on_turn_rate_percent,
            "hands_on_turn_count": projection.hands_on_turn_count,
            "completed_after_nonzero_command": {
                "turns_with_nonzero_command": (projection.turns_with_nonzero_command_count),
                "subsequently_completed": projection.turns_subsequently_completed_count,
                "percent": projection.completed_after_nonzero_command_percent,
            },
            "command_zero_exit_rate_percent": projection.command_zero_exit_rate_percent,
            "repeated_command_execution_count": (
                projection.repeated_command_execution_count
            ),
            "exact_command_repeat_rate_percent": (
                projection.exact_command_repeat_rate_percent
            ),
            "cached_input_share_percent": projection.cached_input_share_percent,
            "reasoning_output_share_percent": (projection.reasoning_output_share_percent),
            "turns_with_edit_then_verification": {
                "edited_turns": projection.edited_turns_count,
                "verified_after_edit": projection.verified_after_edit_count,
                "percent": projection.edit_then_verify_percent,
            },
            "web_research_follow_through": {
                "web_turns": projection.web_turns_count,
                "later_command_or_file_work": (
                    projection.web_later_command_or_file_work_count
                ),
                "percent": projection.web_follow_through_percent,
            },
            "revisited_distinct_path_count": projection.revisited_distinct_path_count,
            "file_revisit_rate_percent": projection.file_revisit_rate_percent,
            "workspace_tagged_turn_count": projection.workspace_tagged_turn_count,
            "turns_in_busiest_workspace_count": (
                projection.turns_in_busiest_workspace_count
            ),
            "busiest_workspace_turn_share_percent": (
                projection.busiest_workspace_turn_share_percent
            ),
            "working_rhythm": {
                "turns_with_hour": projection.turns_with_local_hour_count,
                "busiest_local_hour": projection.busiest_local_hour,
                "turns_in_busiest_hour": (projection.turns_in_busiest_local_hour_count),
            },
            "goal_tracking": {
                "updates": projection.goal_updates_count,
                "statuses": _count_map(projection.named_counts, "goal_status"),
            },
        },
        "audit": {
            "privacy": projection.audit_privacy,
            "percentile_method": projection.audit_percentile_method,
            "token_method": projection.audit_token_method,
            "token_snapshots": projection.audit_token_snapshots_count,
            "repeated_token_snapshots": (projection.audit_repeated_token_snapshots_count),
            "token_epochs": projection.audit_token_epochs_count,
            "duplicate_operations_ignored": (
                projection.audit_duplicate_operations_ignored_count
            ),
            "duplicate_terminals_ignored": (
                projection.audit_duplicate_terminals_ignored_count
            ),
            "terminal_events_without_start_ignored": (
                projection.audit_terminal_events_without_start_ignored_count
            ),
            "limits": list(projection.audit_limits),
            "new_event_type_warnings": projection.audit_new_event_type_warnings_count,
        },
    }


def turn_statistics_as_dict(projection: TurnStatisticsProjection) -> dict[str, object]:
    """Reconstruct one analyzer-shaped turn projection for CLI JSON output."""
    return {
        "must_have_basic_stats": {
            "timing": {
                "duration_ms": projection.duration_ms,
                "time_to_first_token_ms": projection.time_to_first_token_ms,
            },
            "token_usage": _token_values(projection),
            "context_window": {
                "observation_count": projection.context_observation_count,
                "high_water_percent": projection.context_high_water_percent,
            },
            "commands_executed": {
                "count": projection.commands_executed_count,
                "exit_status": _count_map(projection.named_counts, "command_exit_status"),
                "duration_ms": _distribution_as_dict(projection.command_duration),
                "families": _count_map(projection.named_counts, "command_family"),
            },
            "model_tool_requests": {
                "count": projection.model_tool_requests_count,
                "output_paired": projection.model_tool_outputs_paired_count,
                "by_tool": _count_map(projection.named_counts, "model_tool"),
            },
            "file_changes": {
                "operations": projection.file_change_operations_count,
                "distinct_paths": projection.file_change_distinct_paths_count,
                "change_occurrences": projection.file_change_occurrences_count,
                "by_type": _count_map(projection.named_counts, "file_change_type"),
            },
            "web_activity": {
                "operations": projection.web_operations_count,
                "queries": projection.web_queries_count,
                "result_records": projection.web_result_records_count,
                "distinct_result_or_action_urls": (
                    projection.web_distinct_result_or_action_urls_count
                ),
                "by_action": _count_map(projection.named_counts, "web_action"),
            },
            "collaboration": {
                "operations": projection.collaboration_operations_count,
                "agents_started": projection.collaboration_agents_started_count,
                "by_tool": _count_map(projection.named_counts, "collaboration_tool"),
            },
            "compactions": projection.compactions_count,
            "workspace_and_model": {
                "workspace_digest": projection.workspace_digest,
                "model": projection.model,
                "local_start_hour": projection.local_start_hour,
            },
        },
        "recommended_insight_stats": {
            "hands_on": projection.hands_on,
            "completed_after_nonzero_command": (projection.completed_after_nonzero_command),
            "cached_input_share_percent": projection.cached_input_share_percent,
            "reasoning_output_share_percent": (projection.reasoning_output_share_percent),
            "edited_then_verified": projection.edited_then_verified,
            "web_research_followed_by_command_or_file_work": (
                projection.web_research_followed_by_command_or_file_work
            ),
            "goal_tracking": {
                "updates": projection.goal_updates_count,
                "statuses": _count_map(projection.named_counts, "goal_status"),
            },
        },
    }


def validate_session_statistics_projection(
    projection: SessionStatisticsProjection,
) -> SessionStatisticsProjection:
    """Re-parse a typed value so direct constructors cannot bypass the contract."""
    if not isinstance(projection, SessionStatisticsProjection):
        raise TypeError("projection must be a SessionStatisticsProjection")
    try:
        aggregate = session_statistics_as_dict(projection)
        turns = [
            {
                "session_id": str(turn.codex_session_id),
                "turn_id": turn.codex_turn_id,
                "started_at": turn.started_at_utc,
                "terminal_at": turn.terminal_at_utc,
                "outcome": turn.outcome,
                **turn_statistics_as_dict(turn),
            }
            for turn in projection.turn_statistics
        ]
    except (KeyError, TypeError) as error:
        raise StatisticsProjectionError(
            "typed statistics projection is structurally incomplete"
        ) from error
    return parse_session_statistics_snapshot(
        {
            "protocol_id": "rodex_projection_validation",
            "user_id": "rodex_projection_validation",
            "revision": 1,
            **aggregate,
            "turn_statistics": turns,
        }
    )


def _distribution_as_dict(distribution: StatisticsDistribution) -> dict[str, object]:
    return {
        "n": distribution.observation_count,
        "total": distribution.total,
        "median": distribution.median,
        "p75": distribution.p75,
        "p90": distribution.p90,
        "p95": distribution.p95,
        "max": distribution.maximum,
    }


def _count_map(values: tuple[StatisticsNamedCount, ...], count_kind: str) -> dict[str, int]:
    return {
        item.count_name: item.occurrence_count
        for item in values
        if item.count_kind == count_kind
    }


def _token_values(
    projection: SessionStatisticsProjection | TurnStatisticsProjection,
) -> dict[str, int]:
    return {
        "input_tokens": projection.input_tokens,
        "cached_input_tokens": projection.cached_input_tokens,
        "cache_write_input_tokens": projection.cache_write_input_tokens,
        "output_tokens": projection.output_tokens,
        "reasoning_output_tokens": projection.reasoning_output_tokens,
        "total_tokens": projection.total_tokens,
    }


_SNAPSHOT_KEYS: Final = frozenset(
    {
        "protocol_id",
        "user_id",
        "revision",
        "event_count",
        "source_count",
        "selected_stats",
        "must_have_basic_stats",
        "recommended_insight_stats",
        "audit",
        "turn_statistics",
    }
)
_SESSION_BASIC_KEYS: Final = frozenset(
    {
        "history_coverage",
        "turns",
        "completed_turn_duration_ms",
        "time_to_first_token_ms",
        "token_usage",
        "context_window",
        "commands_executed",
        "model_tool_requests",
        "file_changes",
        "web_activity",
        "collaboration",
        "compactions",
        "workspaces_and_models",
    }
)
_SESSION_INSIGHT_KEYS: Final = frozenset(
    {
        "typical_turn_anatomy",
        "hands_on_turn_count",
        "hands_on_turn_rate_percent",
        "completed_after_nonzero_command",
        "command_zero_exit_rate_percent",
        "repeated_command_execution_count",
        "exact_command_repeat_rate_percent",
        "cached_input_share_percent",
        "reasoning_output_share_percent",
        "turns_with_edit_then_verification",
        "web_research_follow_through",
        "revisited_distinct_path_count",
        "file_revisit_rate_percent",
        "workspace_tagged_turn_count",
        "turns_in_busiest_workspace_count",
        "busiest_workspace_turn_share_percent",
        "working_rhythm",
        "goal_tracking",
    }
)
_AUDIT_KEYS: Final = frozenset(
    {
        "privacy",
        "percentile_method",
        "token_method",
        "token_snapshots",
        "repeated_token_snapshots",
        "token_epochs",
        "duplicate_operations_ignored",
        "duplicate_terminals_ignored",
        "terminal_events_without_start_ignored",
        "limits",
        "new_event_type_warnings",
    }
)
_TURN_KEYS: Final = frozenset(
    {
        "session_id",
        "turn_id",
        "started_at",
        "terminal_at",
        "outcome",
        "must_have_basic_stats",
        "recommended_insight_stats",
    }
)
_TURN_BASIC_KEYS: Final = frozenset(
    {
        "timing",
        "token_usage",
        "context_window",
        "commands_executed",
        "model_tool_requests",
        "file_changes",
        "web_activity",
        "collaboration",
        "compactions",
        "workspace_and_model",
    }
)
_TURN_INSIGHT_KEYS: Final = frozenset(
    {
        "hands_on",
        "completed_after_nonzero_command",
        "cached_input_share_percent",
        "reasoning_output_share_percent",
        "edited_then_verified",
        "web_research_followed_by_command_or_file_work",
        "goal_tracking",
    }
)
_TOKEN_KEYS: Final = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    }
)
_DISTRIBUTION_KEYS: Final = frozenset({"n", "total", "median", "p75", "p90", "p95", "max"})


def parse_session_statistics_snapshot(
    snapshot_mapping: Mapping[str, object],
) -> SessionStatisticsProjection:
    """Parse one complete current analyzer snapshot or reject the whole projection."""
    snapshot = _exact_mapping(snapshot_mapping, _SNAPSHOT_KEYS, "snapshot")
    _required_text(snapshot["protocol_id"], "snapshot.protocol_id")
    _required_text(snapshot["user_id"], "snapshot.user_id")
    _nonnegative_int(snapshot["revision"], "snapshot.revision")
    if snapshot["selected_stats"] is not None:
        raise StatisticsProjectionError("snapshot.selected_stats must be null")

    basic = _exact_mapping(
        snapshot["must_have_basic_stats"],
        _SESSION_BASIC_KEYS,
        "snapshot.must_have_basic_stats",
    )
    insights = _exact_mapping(
        snapshot["recommended_insight_stats"],
        _SESSION_INSIGHT_KEYS,
        "snapshot.recommended_insight_stats",
    )
    audit = _exact_mapping(snapshot["audit"], _AUDIT_KEYS, "snapshot.audit")

    history = _exact_mapping(
        basic["history_coverage"],
        {"sessions", "records", "malformed_records"},
        "snapshot.must_have_basic_stats.history_coverage",
    )
    turns = _exact_mapping(
        basic["turns"],
        {"started", "completed", "aborted", "open"},
        "snapshot.must_have_basic_stats.turns",
    )
    token_usage = _exact_mapping(
        basic["token_usage"],
        {*_TOKEN_KEYS, "per_turn_total_tokens"},
        "snapshot.must_have_basic_stats.token_usage",
    )
    context = _exact_mapping(
        basic["context_window"],
        {"observation_count", "latest_session_median_percent", "high_water_percent"},
        "snapshot.must_have_basic_stats.context_window",
    )
    commands = _exact_mapping(
        basic["commands_executed"],
        {"count", "exit_status", "duration_ms", "families"},
        "snapshot.must_have_basic_stats.commands_executed",
    )
    tools = _exact_mapping(
        basic["model_tool_requests"],
        {"count", "output_paired", "by_tool"},
        "snapshot.must_have_basic_stats.model_tool_requests",
    )
    files = _exact_mapping(
        basic["file_changes"],
        {"operations", "distinct_paths", "change_occurrences", "by_type"},
        "snapshot.must_have_basic_stats.file_changes",
    )
    web = _exact_mapping(
        basic["web_activity"],
        {
            "operations",
            "queries",
            "result_records",
            "distinct_result_or_action_urls",
            "by_action",
        },
        "snapshot.must_have_basic_stats.web_activity",
    )
    collaboration = _exact_mapping(
        basic["collaboration"],
        {"operations", "agents_started", "by_tool"},
        "snapshot.must_have_basic_stats.collaboration",
    )
    workspaces = _exact_mapping(
        basic["workspaces_and_models"],
        {"distinct_workspaces", "models"},
        "snapshot.must_have_basic_stats.workspaces_and_models",
    )
    anatomy = _exact_mapping(
        insights["typical_turn_anatomy"],
        {"turns", "commands_per_turn", "tool_requests_per_turn", "files_per_turn"},
        "snapshot.recommended_insight_stats.typical_turn_anatomy",
    )
    recovery = _exact_mapping(
        insights["completed_after_nonzero_command"],
        {"turns_with_nonzero_command", "subsequently_completed", "percent"},
        "snapshot.recommended_insight_stats.completed_after_nonzero_command",
    )
    edit_verify = _exact_mapping(
        insights["turns_with_edit_then_verification"],
        {"edited_turns", "verified_after_edit", "percent"},
        "snapshot.recommended_insight_stats.turns_with_edit_then_verification",
    )
    web_follow = _exact_mapping(
        insights["web_research_follow_through"],
        {"web_turns", "later_command_or_file_work", "percent"},
        "snapshot.recommended_insight_stats.web_research_follow_through",
    )
    rhythm = _exact_mapping(
        insights["working_rhythm"],
        {"turns_with_hour", "busiest_local_hour", "turns_in_busiest_hour"},
        "snapshot.recommended_insight_stats.working_rhythm",
    )
    goals = _exact_mapping(
        insights["goal_tracking"],
        {"updates", "statuses"},
        "snapshot.recommended_insight_stats.goal_tracking",
    )

    distributions = (
        _distribution(
            "completed_turn_duration_ms",
            basic["completed_turn_duration_ms"],
            "snapshot.must_have_basic_stats.completed_turn_duration_ms",
        ),
        _distribution(
            "time_to_first_token_ms",
            basic["time_to_first_token_ms"],
            "snapshot.must_have_basic_stats.time_to_first_token_ms",
        ),
        _distribution(
            "per_turn_total_tokens",
            token_usage["per_turn_total_tokens"],
            "snapshot.must_have_basic_stats.token_usage.per_turn_total_tokens",
        ),
        _distribution(
            "command_duration_ms",
            commands["duration_ms"],
            "snapshot.must_have_basic_stats.commands_executed.duration_ms",
        ),
        _distribution(
            "commands_per_turn",
            anatomy["commands_per_turn"],
            "snapshot.recommended_insight_stats.typical_turn_anatomy.commands_per_turn",
        ),
        _distribution(
            "tool_requests_per_turn",
            anatomy["tool_requests_per_turn"],
            "snapshot.recommended_insight_stats.typical_turn_anatomy.tool_requests_per_turn",
        ),
        _distribution(
            "files_per_turn",
            anatomy["files_per_turn"],
            "snapshot.recommended_insight_stats.typical_turn_anatomy.files_per_turn",
        ),
    )
    named_counts = (
        *_named_counts(
            "command_exit_status", commands["exit_status"], "commands.exit_status"
        ),
        *_named_counts("command_family", commands["families"], "commands.families"),
        *_named_counts("model_tool", tools["by_tool"], "model_tool_requests.by_tool"),
        *_named_counts("file_change_type", files["by_type"], "file_changes.by_type"),
        *_named_counts("web_action", web["by_action"], "web_activity.by_action"),
        *_named_counts(
            "collaboration_tool", collaboration["by_tool"], "collaboration.by_tool"
        ),
        *_named_counts("model", workspaces["models"], "workspaces_and_models.models"),
        *_named_counts("goal_status", goals["statuses"], "goal_tracking.statuses"),
    )
    turn_statistics = _turns(snapshot["turn_statistics"])

    event_count = _nonnegative_int(snapshot["event_count"], "snapshot.event_count")
    source_count = _nonnegative_int(snapshot["source_count"], "snapshot.source_count")
    records_count = _nonnegative_int(history["records"], "history_coverage.records")
    if event_count != records_count:
        raise StatisticsProjectionError(
            "snapshot.event_count must equal history_coverage.records"
        )
    started_count = _nonnegative_int(turns["started"], "turns.started")
    completed_count = _nonnegative_int(turns["completed"], "turns.completed")
    aborted_count = _nonnegative_int(turns["aborted"], "turns.aborted")
    open_count = _nonnegative_int(turns["open"], "turns.open")
    if started_count != completed_count + aborted_count + open_count:
        raise StatisticsProjectionError("turn outcome counts must sum to turns.started")
    typical_turns = _nonnegative_int(anatomy["turns"], "typical_turn_anatomy.turns")
    if typical_turns != started_count:
        raise StatisticsProjectionError(
            "typical_turn_anatomy.turns must equal turns.started"
        )
    if len(turn_statistics) != started_count:
        raise StatisticsProjectionError("turn_statistics must contain every started turn")
    observed_outcomes = {
        outcome: sum(item.outcome == outcome for item in turn_statistics)
        for outcome in ("completed", "aborted", "open")
    }
    expected_outcomes = {
        "completed": completed_count,
        "aborted": aborted_count,
        "open": open_count,
    }
    if observed_outcomes != expected_outcomes:
        raise StatisticsProjectionError(
            "turn_statistics outcomes must equal aggregate turn outcomes"
        )

    input_tokens = _nonnegative_int(token_usage["input_tokens"], "token_usage.input_tokens")
    cached_tokens = _nonnegative_int(
        token_usage["cached_input_tokens"], "token_usage.cached_input_tokens"
    )
    if cached_tokens > input_tokens:
        raise StatisticsProjectionError("cached_input_tokens cannot exceed input_tokens")
    tool_count = _nonnegative_int(tools["count"], "model_tool_requests.count")
    paired_count = _nonnegative_int(
        tools["output_paired"], "model_tool_requests.output_paired"
    )
    if paired_count > tool_count:
        raise StatisticsProjectionError("model tool paired outputs cannot exceed requests")
    failed_turns = _nonnegative_int(
        recovery["turns_with_nonzero_command"],
        "completed_after_nonzero_command.turns_with_nonzero_command",
    )
    recovered_turns = _nonnegative_int(
        recovery["subsequently_completed"],
        "completed_after_nonzero_command.subsequently_completed",
    )
    _not_greater(recovered_turns, failed_turns, "subsequently_completed")
    edited_turns = _nonnegative_int(edit_verify["edited_turns"], "edited_turns")
    verified_turns = _nonnegative_int(
        edit_verify["verified_after_edit"], "verified_after_edit"
    )
    _not_greater(verified_turns, edited_turns, "verified_after_edit")
    web_turns = _nonnegative_int(web_follow["web_turns"], "web_follow.web_turns")
    web_later = _nonnegative_int(
        web_follow["later_command_or_file_work"], "web_follow.later_work"
    )
    _not_greater(web_later, web_turns, "later_command_or_file_work")
    commands_count = _nonnegative_int(commands["count"], "commands.count")
    repeated_commands = _nonnegative_int(
        insights["repeated_command_execution_count"], "repeated_command_execution_count"
    )
    _not_greater(repeated_commands, commands_count, "repeated_command_execution_count")
    revisited_paths = _nonnegative_int(
        insights["revisited_distinct_path_count"], "revisited_distinct_path_count"
    )
    _not_greater(
        revisited_paths,
        _nonnegative_int(files["distinct_paths"], "files.distinct_paths"),
        "revisited_distinct_path_count",
    )
    workspace_tagged_turns = _nonnegative_int(
        insights["workspace_tagged_turn_count"], "workspace_tagged_turn_count"
    )
    _not_greater(workspace_tagged_turns, started_count, "workspace_tagged_turn_count")
    busiest_workspace_turns = _nonnegative_int(
        insights["turns_in_busiest_workspace_count"],
        "turns_in_busiest_workspace_count",
    )
    _not_greater(
        busiest_workspace_turns,
        workspace_tagged_turns,
        "turns_in_busiest_workspace_count",
    )
    hours_count = _nonnegative_int(
        rhythm["turns_with_hour"], "working_rhythm.turns_with_hour"
    )
    busiest_hour = _optional_hour(
        rhythm["busiest_local_hour"], "working_rhythm.busiest_local_hour"
    )
    busiest_count = _nonnegative_int(
        rhythm["turns_in_busiest_hour"], "working_rhythm.turns_in_busiest_hour"
    )
    _not_greater(busiest_count, hours_count, "turns_in_busiest_hour")
    if (hours_count == 0) != (busiest_hour is None):
        raise StatisticsProjectionError(
            "working_rhythm.busiest_local_hour must be null exactly when no hour exists"
        )
    if hours_count > 0 and busiest_count == 0:
        raise StatisticsProjectionError(
            "working_rhythm.turns_in_busiest_hour must be positive when hours exist"
        )
    goal_updates = _nonnegative_int(goals["updates"], "goal_tracking.updates")
    if goal_updates != _count_total(named_counts, "goal_status"):
        raise StatisticsProjectionError("goal_tracking status counts must sum to updates")

    limits = _ordered_texts(audit["limits"], "snapshot.audit.limits")
    audit_snapshots = _nonnegative_int(audit["token_snapshots"], "audit.token_snapshots")
    repeated_snapshots = _nonnegative_int(
        audit["repeated_token_snapshots"], "audit.repeated_token_snapshots"
    )
    _not_greater(repeated_snapshots, audit_snapshots, "repeated_token_snapshots")

    return SessionStatisticsProjection(
        analyzer_event_count=event_count,
        analyzer_source_count=source_count,
        history_sessions_count=_nonnegative_int(history["sessions"], "history.sessions"),
        history_records_count=records_count,
        history_malformed_records_count=_nonnegative_int(
            history["malformed_records"], "history.malformed_records"
        ),
        turns_started_count=started_count,
        turns_completed_count=completed_count,
        turns_aborted_count=aborted_count,
        turns_open_count=open_count,
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        cache_write_input_tokens=_nonnegative_int(
            token_usage["cache_write_input_tokens"], "token_usage.cache_write_input_tokens"
        ),
        output_tokens=_nonnegative_int(
            token_usage["output_tokens"], "token_usage.output_tokens"
        ),
        reasoning_output_tokens=_nonnegative_int(
            token_usage["reasoning_output_tokens"], "token_usage.reasoning_output_tokens"
        ),
        total_tokens=_nonnegative_int(
            token_usage["total_tokens"], "token_usage.total_tokens"
        ),
        context_observation_count=_nonnegative_int(
            context["observation_count"], "context_window.observation_count"
        ),
        context_latest_session_median_percent=_optional_percent(
            context["latest_session_median_percent"],
            "context_window.latest_session_median_percent",
        ),
        context_high_water_percent=_percent(
            context["high_water_percent"], "context_window.high_water_percent"
        ),
        commands_executed_count=commands_count,
        model_tool_requests_count=tool_count,
        model_tool_outputs_paired_count=paired_count,
        file_change_operations_count=_nonnegative_int(
            files["operations"], "files.operations"
        ),
        file_change_distinct_paths_count=_nonnegative_int(
            files["distinct_paths"], "files.distinct_paths"
        ),
        file_change_occurrences_count=_nonnegative_int(
            files["change_occurrences"], "files.change_occurrences"
        ),
        web_operations_count=_nonnegative_int(web["operations"], "web.operations"),
        web_queries_count=_nonnegative_int(web["queries"], "web.queries"),
        web_result_records_count=_nonnegative_int(
            web["result_records"], "web.result_records"
        ),
        web_distinct_result_or_action_urls_count=_nonnegative_int(
            web["distinct_result_or_action_urls"], "web.distinct_result_or_action_urls"
        ),
        collaboration_operations_count=_nonnegative_int(
            collaboration["operations"], "collaboration.operations"
        ),
        collaboration_agents_started_count=_nonnegative_int(
            collaboration["agents_started"], "collaboration.agents_started"
        ),
        compactions_count=_nonnegative_int(basic["compactions"], "compactions"),
        distinct_workspaces_count=_nonnegative_int(
            workspaces["distinct_workspaces"], "workspaces.distinct_workspaces"
        ),
        typical_turns_count=typical_turns,
        hands_on_turn_count=_bounded_count(
            insights["hands_on_turn_count"], started_count, "hands_on_turn_count"
        ),
        hands_on_turn_rate_percent=_optional_percent(
            insights["hands_on_turn_rate_percent"], "hands_on_turn_rate_percent"
        ),
        turns_with_nonzero_command_count=failed_turns,
        turns_subsequently_completed_count=recovered_turns,
        completed_after_nonzero_command_percent=_optional_percent(
            recovery["percent"], "completed_after_nonzero_command.percent"
        ),
        command_zero_exit_rate_percent=_optional_percent(
            insights["command_zero_exit_rate_percent"], "command_zero_exit_rate_percent"
        ),
        repeated_command_execution_count=repeated_commands,
        exact_command_repeat_rate_percent=_optional_percent(
            insights["exact_command_repeat_rate_percent"],
            "exact_command_repeat_rate_percent",
        ),
        cached_input_share_percent=_optional_percent(
            insights["cached_input_share_percent"], "cached_input_share_percent"
        ),
        reasoning_output_share_percent=_optional_percent(
            insights["reasoning_output_share_percent"], "reasoning_output_share_percent"
        ),
        edited_turns_count=edited_turns,
        verified_after_edit_count=verified_turns,
        edit_then_verify_percent=_optional_percent(
            edit_verify["percent"], "turns_with_edit_then_verification.percent"
        ),
        web_turns_count=web_turns,
        web_later_command_or_file_work_count=web_later,
        web_follow_through_percent=_optional_percent(
            web_follow["percent"], "web_research_follow_through.percent"
        ),
        revisited_distinct_path_count=revisited_paths,
        file_revisit_rate_percent=_optional_percent(
            insights["file_revisit_rate_percent"], "file_revisit_rate_percent"
        ),
        workspace_tagged_turn_count=workspace_tagged_turns,
        turns_in_busiest_workspace_count=busiest_workspace_turns,
        busiest_workspace_turn_share_percent=_optional_percent(
            insights["busiest_workspace_turn_share_percent"],
            "busiest_workspace_turn_share_percent",
        ),
        turns_with_local_hour_count=hours_count,
        busiest_local_hour=busiest_hour,
        turns_in_busiest_local_hour_count=busiest_count,
        goal_updates_count=goal_updates,
        audit_privacy=_required_text(audit["privacy"], "audit.privacy"),
        audit_percentile_method=_required_text(
            audit["percentile_method"], "audit.percentile_method"
        ),
        audit_token_method=_required_text(audit["token_method"], "audit.token_method"),
        audit_token_snapshots_count=audit_snapshots,
        audit_repeated_token_snapshots_count=repeated_snapshots,
        audit_token_epochs_count=_nonnegative_int(
            audit["token_epochs"], "audit.token_epochs"
        ),
        audit_duplicate_operations_ignored_count=_nonnegative_int(
            audit["duplicate_operations_ignored"], "audit.duplicate_operations_ignored"
        ),
        audit_duplicate_terminals_ignored_count=_nonnegative_int(
            audit["duplicate_terminals_ignored"], "audit.duplicate_terminals_ignored"
        ),
        audit_terminal_events_without_start_ignored_count=_nonnegative_int(
            audit["terminal_events_without_start_ignored"],
            "audit.terminal_events_without_start_ignored",
        ),
        audit_new_event_type_warnings_count=_nonnegative_int(
            audit["new_event_type_warnings"], "audit.new_event_type_warnings"
        ),
        distributions=distributions,
        named_counts=named_counts,
        audit_limits=limits,
        turn_statistics=turn_statistics,
    )


def _turns(value: object) -> tuple[TurnStatisticsProjection, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise StatisticsProjectionError("snapshot.turn_statistics must be a sequence")
    parsed = tuple(_turn(item, index) for index, item in enumerate(value))
    identities = {(item.codex_session_id, item.codex_turn_id) for item in parsed}
    if len(identities) != len(parsed):
        raise StatisticsProjectionError(
            "snapshot.turn_statistics contains a duplicate identity"
        )
    return parsed


def _turn(value: object, index: int) -> TurnStatisticsProjection:
    path = f"snapshot.turn_statistics[{index}]"
    turn = _exact_mapping(value, _TURN_KEYS, path)
    try:
        codex_session_id = parse_codex_session_id(
            _required_text(turn["session_id"], f"{path}.session_id")
        )
    except ValueError as error:
        raise StatisticsProjectionError(
            f"{path}.session_id must be a valid Codex session ID"
        ) from error
    turn_id = _required_text(turn["turn_id"], f"{path}.turn_id")
    started_at = _optional_timestamp(turn["started_at"], f"{path}.started_at")
    terminal_at = _optional_timestamp(turn["terminal_at"], f"{path}.terminal_at")
    outcome = _required_text(turn["outcome"], f"{path}.outcome")
    if outcome not in {"open", "completed", "aborted"}:
        raise StatisticsProjectionError(f"{path}.outcome is unsupported: {outcome}")
    if outcome == "open" and terminal_at is not None:
        raise StatisticsProjectionError(
            f"{path}: open turns cannot have a terminal timestamp"
        )
    if started_at is not None and terminal_at is not None and terminal_at < started_at:
        raise StatisticsProjectionError(f"{path}: terminal timestamp precedes start")

    basic = _exact_mapping(turn["must_have_basic_stats"], _TURN_BASIC_KEYS, f"{path}.basic")
    insights = _exact_mapping(
        turn["recommended_insight_stats"], _TURN_INSIGHT_KEYS, f"{path}.insights"
    )
    timing = _exact_mapping(
        basic["timing"], {"duration_ms", "time_to_first_token_ms"}, f"{path}.timing"
    )
    tokens = _exact_mapping(basic["token_usage"], _TOKEN_KEYS, f"{path}.token_usage")
    context = _exact_mapping(
        basic["context_window"],
        {"observation_count", "high_water_percent"},
        f"{path}.context_window",
    )
    commands = _exact_mapping(
        basic["commands_executed"],
        {"count", "exit_status", "duration_ms", "families"},
        f"{path}.commands_executed",
    )
    tools = _exact_mapping(
        basic["model_tool_requests"], {"count", "output_paired", "by_tool"}, f"{path}.tools"
    )
    files = _exact_mapping(
        basic["file_changes"],
        {"operations", "distinct_paths", "change_occurrences", "by_type"},
        f"{path}.files",
    )
    web = _exact_mapping(
        basic["web_activity"],
        {
            "operations",
            "queries",
            "result_records",
            "distinct_result_or_action_urls",
            "by_action",
        },
        f"{path}.web",
    )
    collaboration = _exact_mapping(
        basic["collaboration"],
        {"operations", "agents_started", "by_tool"},
        f"{path}.collaboration",
    )
    workspace = _exact_mapping(
        basic["workspace_and_model"],
        {"workspace_digest", "model", "local_start_hour"},
        f"{path}.workspace_and_model",
    )
    goals = _exact_mapping(
        insights["goal_tracking"], {"updates", "statuses"}, f"{path}.goal_tracking"
    )

    input_tokens = _nonnegative_int(tokens["input_tokens"], f"{path}.input_tokens")
    cached_tokens = _nonnegative_int(
        tokens["cached_input_tokens"], f"{path}.cached_input_tokens"
    )
    if cached_tokens > input_tokens:
        raise StatisticsProjectionError(f"{path}: cached_input_tokens exceeds input_tokens")
    command_count = _nonnegative_int(commands["count"], f"{path}.commands.count")
    command_duration = _distribution(
        "command_duration_ms", commands["duration_ms"], f"{path}.commands.duration_ms"
    )
    if command_duration.observation_count > command_count:
        raise StatisticsProjectionError(f"{path}: command duration samples exceed commands")
    tool_count = _nonnegative_int(tools["count"], f"{path}.tools.count")
    paired_count = _nonnegative_int(tools["output_paired"], f"{path}.tools.output_paired")
    _not_greater(paired_count, tool_count, f"{path}.tools.output_paired")

    named_counts = (
        *_named_counts(
            "command_exit_status", commands["exit_status"], f"{path}.commands.exit_status"
        ),
        *_named_counts("command_family", commands["families"], f"{path}.commands.families"),
        *_named_counts("model_tool", tools["by_tool"], f"{path}.tools.by_tool"),
        *_named_counts("file_change_type", files["by_type"], f"{path}.files.by_type"),
        *_named_counts("web_action", web["by_action"], f"{path}.web.by_action"),
        *_named_counts(
            "collaboration_tool", collaboration["by_tool"], f"{path}.collaboration.by_tool"
        ),
        *_named_counts("goal_status", goals["statuses"], f"{path}.goals.statuses"),
    )
    goal_updates = _nonnegative_int(goals["updates"], f"{path}.goals.updates")
    if goal_updates != _count_total(named_counts, "goal_status"):
        raise StatisticsProjectionError(f"{path}: goal status counts must sum to updates")

    digest = _optional_text(workspace["workspace_digest"], f"{path}.workspace_digest")
    if digest is not None and (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise StatisticsProjectionError(
            f"{path}.workspace_digest must be lowercase SHA-256"
        )

    return TurnStatisticsProjection(
        codex_session_id=codex_session_id,
        codex_turn_id=turn_id,
        started_at_utc=started_at,
        terminal_at_utc=terminal_at,
        outcome=outcome,
        duration_ms=_optional_nonnegative_int(timing["duration_ms"], f"{path}.duration_ms"),
        time_to_first_token_ms=_optional_nonnegative_int(
            timing["time_to_first_token_ms"], f"{path}.time_to_first_token_ms"
        ),
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        cache_write_input_tokens=_nonnegative_int(
            tokens["cache_write_input_tokens"], f"{path}.cache_write_input_tokens"
        ),
        output_tokens=_nonnegative_int(tokens["output_tokens"], f"{path}.output_tokens"),
        reasoning_output_tokens=_nonnegative_int(
            tokens["reasoning_output_tokens"], f"{path}.reasoning_output_tokens"
        ),
        total_tokens=_nonnegative_int(tokens["total_tokens"], f"{path}.total_tokens"),
        context_observation_count=_nonnegative_int(
            context["observation_count"], f"{path}.context_observation_count"
        ),
        context_high_water_percent=_percent(
            context["high_water_percent"], f"{path}.context_high_water_percent"
        ),
        commands_executed_count=command_count,
        command_duration_observation_count=command_duration.observation_count,
        command_duration_total_ms=command_duration.total,
        command_duration_median_ms=command_duration.median,
        command_duration_p75_ms=command_duration.p75,
        command_duration_p90_ms=command_duration.p90,
        command_duration_p95_ms=command_duration.p95,
        command_duration_maximum_ms=command_duration.maximum,
        model_tool_requests_count=tool_count,
        model_tool_outputs_paired_count=paired_count,
        file_change_operations_count=_nonnegative_int(
            files["operations"], f"{path}.file_operations"
        ),
        file_change_distinct_paths_count=_nonnegative_int(
            files["distinct_paths"], f"{path}.file_distinct_paths"
        ),
        file_change_occurrences_count=_nonnegative_int(
            files["change_occurrences"], f"{path}.file_occurrences"
        ),
        web_operations_count=_nonnegative_int(web["operations"], f"{path}.web_operations"),
        web_queries_count=_nonnegative_int(web["queries"], f"{path}.web_queries"),
        web_result_records_count=_nonnegative_int(
            web["result_records"], f"{path}.web_result_records"
        ),
        web_distinct_result_or_action_urls_count=_nonnegative_int(
            web["distinct_result_or_action_urls"], f"{path}.web_distinct_urls"
        ),
        collaboration_operations_count=_nonnegative_int(
            collaboration["operations"], f"{path}.collaboration_operations"
        ),
        collaboration_agents_started_count=_nonnegative_int(
            collaboration["agents_started"], f"{path}.collaboration_agents"
        ),
        compactions_count=_nonnegative_int(basic["compactions"], f"{path}.compactions"),
        workspace_digest=digest,
        model=_optional_text(workspace["model"], f"{path}.model"),
        local_start_hour=_optional_hour(
            workspace["local_start_hour"], f"{path}.local_start_hour"
        ),
        hands_on=_boolean(insights["hands_on"], f"{path}.hands_on"),
        completed_after_nonzero_command=_boolean(
            insights["completed_after_nonzero_command"],
            f"{path}.completed_after_nonzero_command",
        ),
        cached_input_share_percent=_optional_percent(
            insights["cached_input_share_percent"], f"{path}.cached_input_share_percent"
        ),
        reasoning_output_share_percent=_optional_percent(
            insights["reasoning_output_share_percent"],
            f"{path}.reasoning_output_share_percent",
        ),
        edited_then_verified=_boolean(
            insights["edited_then_verified"], f"{path}.edited_then_verified"
        ),
        web_research_followed_by_command_or_file_work=_boolean(
            insights["web_research_followed_by_command_or_file_work"],
            f"{path}.web_research_followed_by_command_or_file_work",
        ),
        goal_updates_count=goal_updates,
        named_counts=named_counts,
    )


def _exact_mapping(
    value: object, expected: set[str] | frozenset[str], path: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise StatisticsProjectionError(f"{path} must be a mapping")
    observed = set(value)
    if observed != set(expected):
        missing = sorted(set(expected) - observed)
        unexpected = sorted(observed - set(expected), key=str)
        raise StatisticsProjectionError(
            f"{path} keys do not match; missing={missing!r}, unexpected={unexpected!r}"
        )
    return value


def _distribution(kind: str, value: object, path: str) -> StatisticsDistribution:
    distribution = _exact_mapping(value, _DISTRIBUTION_KEYS, path)
    observation_count = _nonnegative_int(distribution["n"], f"{path}.n")
    total = _nonnegative_int(distribution["total"], f"{path}.total")
    median = _optional_nonnegative_number(distribution["median"], f"{path}.median")
    p75 = _optional_nonnegative_int(distribution["p75"], f"{path}.p75")
    p90 = _optional_nonnegative_int(distribution["p90"], f"{path}.p90")
    p95 = _optional_nonnegative_int(distribution["p95"], f"{path}.p95")
    maximum = _optional_nonnegative_int(distribution["max"], f"{path}.max")
    tail = (median, p75, p90, p95, maximum)
    if observation_count == 0:
        if total != 0 or any(item is not None for item in tail):
            raise StatisticsProjectionError(
                f"{path}: an empty distribution has only zero total"
            )
    elif any(item is None for item in tail):
        raise StatisticsProjectionError(
            f"{path}: a nonempty distribution requires all summaries"
        )
    else:
        assert median is not None and p75 is not None and p90 is not None
        assert p95 is not None and maximum is not None
        if not median <= p75 <= p90 <= p95 <= maximum:
            raise StatisticsProjectionError(
                f"{path}: distribution summaries are not monotonic"
            )
        if maximum > total:
            raise StatisticsProjectionError(f"{path}: maximum cannot exceed total")
    return StatisticsDistribution(
        kind, observation_count, total, median, p75, p90, p95, maximum
    )


def _named_counts(kind: str, value: object, path: str) -> tuple[StatisticsNamedCount, ...]:
    if not isinstance(value, Mapping):
        raise StatisticsProjectionError(f"{path} must be a mapping")
    counts: list[StatisticsNamedCount] = []
    for name, count in value.items():
        normalized_name = _required_text(name, f"{path} key")
        counts.append(
            StatisticsNamedCount(
                count_kind=kind,
                count_name=normalized_name,
                occurrence_count=_positive_int(count, f"{path}.{normalized_name}"),
            )
        )
    return tuple(sorted(counts, key=lambda item: item.count_name))


def _count_total(counts: tuple[StatisticsNamedCount, ...], kind: str) -> int:
    return sum(item.occurrence_count for item in counts if item.count_kind == kind)


def _ordered_texts(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise StatisticsProjectionError(f"{path} must be a sequence")
    return tuple(
        _required_text(item, f"{path}[{index}]") for index, item in enumerate(value)
    )


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StatisticsProjectionError(f"{path} must be nonempty text")
    return value


def _optional_text(value: object, path: str) -> str | None:
    return None if value is None else _required_text(value, path)


def _nonnegative_int(value: object, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise StatisticsProjectionError(f"{path} must be a nonnegative integer")
    return value


def _optional_nonnegative_int(value: object, path: str) -> int | None:
    return None if value is None else _nonnegative_int(value, path)


def _positive_int(value: object, path: str) -> int:
    number = _nonnegative_int(value, path)
    if number == 0:
        raise StatisticsProjectionError(f"{path} must be a positive integer")
    return number


def _bounded_count(value: object, maximum: int, path: str) -> int:
    number = _nonnegative_int(value, path)
    _not_greater(number, maximum, path)
    return number


def _optional_nonnegative_number(value: object, path: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise StatisticsProjectionError(
            f"{path} must be a nonnegative finite number or null"
        )
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise StatisticsProjectionError(
            f"{path} must be a nonnegative finite number or null"
        )
    return number


def _percent(value: object, path: str) -> float:
    number = _optional_percent(value, path)
    if number is None:
        raise StatisticsProjectionError(f"{path} cannot be null")
    return number


def _optional_percent(value: object, path: str) -> float | None:
    number = _optional_nonnegative_number(value, path)
    if number is not None and number > 100:
        raise StatisticsProjectionError(f"{path} must be between 0 and 100")
    return number


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise StatisticsProjectionError(f"{path} must be a boolean")
    return value


def _optional_hour(value: object, path: str) -> int | None:
    hour = _optional_nonnegative_int(value, path)
    if hour is not None and hour > 23:
        raise StatisticsProjectionError(f"{path} must be between 0 and 23")
    return hour


def _optional_timestamp(value: object, path: str) -> str | None:
    if value is None:
        return None
    text = _required_text(value, path)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise StatisticsProjectionError(f"{path} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StatisticsProjectionError(f"{path} must include a UTC offset")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _not_greater(value: int, maximum: int, path: str) -> None:
    if value > maximum:
        raise StatisticsProjectionError(f"{path} cannot exceed its containing count")
