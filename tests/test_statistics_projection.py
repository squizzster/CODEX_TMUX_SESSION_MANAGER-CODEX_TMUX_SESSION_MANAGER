from __future__ import annotations

import copy
import math
import uuid
from dataclasses import FrozenInstanceError

import pytest

from rodex_registry.statistics_projection import (
    SessionStatisticsProjection,
    StatisticsDistribution,
    StatisticsNamedCount,
    StatisticsProjectionError,
    TurnStatisticsProjection,
    parse_session_statistics_snapshot,
    session_statistics_as_dict,
    turn_statistics_as_dict,
)

CODEX_SESSION_ID = "01a00654-f2bc-7a30-834a-a5f886a65f82"


def _distribution(*values: int) -> dict[str, int | float | None]:
    if not values:
        return {
            "n": 0,
            "total": 0,
            "median": None,
            "p75": None,
            "p90": None,
            "p95": None,
            "max": None,
        }
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "total": sum(ordered),
        "median": sum(ordered[(len(ordered) - 1) // 2 : len(ordered) // 2 + 1])
        / (2 if len(ordered) % 2 == 0 else 1),
        "p75": ordered[-1],
        "p90": ordered[-1],
        "p95": ordered[-1],
        "max": ordered[-1],
    }


def _turn() -> dict[str, object]:
    return {
        "session_id": CODEX_SESSION_ID,
        "turn_id": "turn-a",
        "started_at": "2026-08-16T13:00:00+01:00",
        "terminal_at": "2026-08-16T13:00:01+01:00",
        "outcome": "completed",
        "must_have_basic_stats": {
            "timing": {"duration_ms": 1_000, "time_to_first_token_ms": 25},
            "token_usage": {
                "input_tokens": 80,
                "cached_input_tokens": 40,
                "cache_write_input_tokens": 4,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
                "total_tokens": 100,
            },
            "context_window": {"observation_count": 2, "high_water_percent": 25.5},
            "commands_executed": {
                "count": 2,
                "exit_status": {"zero_exit": 1, "nonzero_exit": 1},
                "duration_ms": _distribution(100, 200),
                "families": {"verification": 1, "version_control": 1},
            },
            "model_tool_requests": {
                "count": 2,
                "output_paired": 1,
                "by_tool": {"exec": 1, "wait": 1},
            },
            "file_changes": {
                "operations": 1,
                "distinct_paths": 2,
                "change_occurrences": 2,
                "by_type": {"add": 1, "update": 1},
            },
            "web_activity": {
                "operations": 1,
                "queries": 1,
                "result_records": 3,
                "distinct_result_or_action_urls": 2,
                "by_action": {"search": 1},
            },
            "collaboration": {
                "operations": 1,
                "agents_started": 1,
                "by_tool": {"spawn_agent": 1},
            },
            "compactions": 1,
            "workspace_and_model": {
                "workspace_digest": "a" * 64,
                "model": "gpt-test",
                "local_start_hour": 13,
            },
        },
        "recommended_insight_stats": {
            "hands_on": True,
            "completed_after_nonzero_command": True,
            "cached_input_share_percent": 50.0,
            "reasoning_output_share_percent": 25.0,
            "edited_then_verified": True,
            "web_research_followed_by_command_or_file_work": True,
            "goal_tracking": {"updates": 1, "statuses": {"complete": 1}},
        },
    }


def _snapshot() -> dict[str, object]:
    return {
        "protocol_id": "temporary-protocol",
        "user_id": "temporary-user",
        "revision": 2,
        "event_count": 20,
        "source_count": 1,
        "selected_stats": None,
        "must_have_basic_stats": {
            "history_coverage": {"sessions": 1, "records": 20, "malformed_records": 2},
            "turns": {"started": 1, "completed": 1, "aborted": 0, "open": 0},
            "completed_turn_duration_ms": _distribution(1_000),
            "time_to_first_token_ms": _distribution(25),
            "token_usage": {
                "input_tokens": 80,
                "cached_input_tokens": 40,
                "cache_write_input_tokens": 4,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
                "total_tokens": 100,
                "per_turn_total_tokens": _distribution(100),
            },
            "context_window": {
                "observation_count": 2,
                "latest_session_median_percent": 20.0,
                "high_water_percent": 25.5,
            },
            "commands_executed": {
                "count": 2,
                "exit_status": {"zero_exit": 1, "nonzero_exit": 1},
                "duration_ms": _distribution(100, 200),
                "families": {"verification": 1, "version_control": 1},
            },
            "model_tool_requests": {
                "count": 2,
                "output_paired": 1,
                "by_tool": {"wait": 1, "exec": 1},
            },
            "file_changes": {
                "operations": 1,
                "distinct_paths": 2,
                "change_occurrences": 2,
                "by_type": {"update": 1, "add": 1},
            },
            "web_activity": {
                "operations": 1,
                "queries": 1,
                "result_records": 3,
                "distinct_result_or_action_urls": 2,
                "by_action": {"search": 1},
            },
            "collaboration": {
                "operations": 1,
                "agents_started": 1,
                "by_tool": {"spawn_agent": 1},
            },
            "compactions": 1,
            "workspaces_and_models": {
                "distinct_workspaces": 1,
                "models": {"gpt-test": 1},
            },
        },
        "recommended_insight_stats": {
            "typical_turn_anatomy": {
                "turns": 1,
                "commands_per_turn": _distribution(2),
                "tool_requests_per_turn": _distribution(2),
                "files_per_turn": _distribution(2),
            },
            "hands_on_turn_count": 1,
            "hands_on_turn_rate_percent": 100.0,
            "completed_after_nonzero_command": {
                "turns_with_nonzero_command": 1,
                "subsequently_completed": 1,
                "percent": 100.0,
            },
            "command_zero_exit_rate_percent": 50.0,
            "repeated_command_execution_count": 1,
            "exact_command_repeat_rate_percent": 50.0,
            "cached_input_share_percent": 50.0,
            "reasoning_output_share_percent": 25.0,
            "turns_with_edit_then_verification": {
                "edited_turns": 1,
                "verified_after_edit": 1,
                "percent": 100.0,
            },
            "web_research_follow_through": {
                "web_turns": 1,
                "later_command_or_file_work": 1,
                "percent": 100.0,
            },
            "revisited_distinct_path_count": 1,
            "file_revisit_rate_percent": 50.0,
            "workspace_tagged_turn_count": 1,
            "turns_in_busiest_workspace_count": 1,
            "busiest_workspace_turn_share_percent": 100.0,
            "working_rhythm": {
                "turns_with_hour": 1,
                "busiest_local_hour": 13,
                "turns_in_busiest_hour": 1,
            },
            "goal_tracking": {"updates": 1, "statuses": {"complete": 1}},
        },
        "audit": {
            "privacy": "derived only",
            "percentile_method": "nearest rank",
            "token_method": "positive delta",
            "token_snapshots": 2,
            "repeated_token_snapshots": 1,
            "token_epochs": 1,
            "duplicate_operations_ignored": 1,
            "duplicate_terminals_ignored": 0,
            "terminal_events_without_start_ignored": 0,
            "limits": ["first", "second"],
            "new_event_type_warnings": 1,
        },
        "turn_statistics": [_turn()],
    }


def test_complete_snapshot_becomes_typed_immutable_relational_values() -> None:
    projection = parse_session_statistics_snapshot(_snapshot())

    assert isinstance(projection, SessionStatisticsProjection)
    assert projection.analyzer_event_count == 20
    assert projection.repeated_command_execution_count == 1
    assert projection.revisited_distinct_path_count == 1
    assert projection.workspace_tagged_turn_count == 1
    assert projection.turns_in_busiest_workspace_count == 1
    assert [item.distribution_kind for item in projection.distributions] == [
        "completed_turn_duration_ms",
        "time_to_first_token_ms",
        "per_turn_total_tokens",
        "command_duration_ms",
        "commands_per_turn",
        "tool_requests_per_turn",
        "files_per_turn",
    ]
    command_duration = projection.distributions[3]
    assert isinstance(command_duration, StatisticsDistribution)
    assert command_duration.median == 150.0
    assert projection.audit_limits == ("first", "second")
    assert all(isinstance(item, StatisticsNamedCount) for item in projection.named_counts)
    tool_counts = [
        (item.count_name, item.occurrence_count)
        for item in projection.named_counts
        if item.count_kind == "model_tool"
    ]
    assert tool_counts == [("exec", 1), ("wait", 1)]

    turn = projection.turn_statistics[0]
    assert isinstance(turn, TurnStatisticsProjection)
    assert turn.codex_session_id == uuid.UUID(CODEX_SESSION_ID)
    assert turn.started_at_utc == "2026-08-16T12:00:00.000000Z"
    assert turn.command_duration.median == 150.0
    with pytest.raises(FrozenInstanceError):
        projection.analyzer_event_count = 21  # type: ignore[misc]


def test_every_analyzer_stat_is_reconstructed_exactly_from_typed_values() -> None:
    snapshot = _snapshot()
    projection = parse_session_statistics_snapshot(snapshot)

    assert session_statistics_as_dict(projection) == {
        key: snapshot[key]
        for key in (
            "event_count",
            "source_count",
            "selected_stats",
            "must_have_basic_stats",
            "recommended_insight_stats",
            "audit",
        )
    }
    assert turn_statistics_as_dict(projection.turn_statistics[0]) == {
        key: snapshot["turn_statistics"][0][key]
        for key in ("must_have_basic_stats", "recommended_insight_stats")
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.__setitem__("future", 1), "snapshot keys do not match"),
        (lambda value: value.__setitem__("selected_stats", ("turns",)), "selected_stats"),
        (
            lambda value: value["must_have_basic_stats"]["turns"].__setitem__("future", 1),
            "turns keys do not match",
        ),
        (
            lambda value: value["recommended_insight_stats"].pop(
                "revisited_distinct_path_count"
            ),
            "recommended_insight_stats keys do not match",
        ),
        (
            lambda value: value["turn_statistics"][0]["must_have_basic_stats"][
                "timing"
            ].__setitem__("future", 1),
            "timing keys do not match",
        ),
    ],
)
def test_schema_drift_is_rejected(mutation, message: str) -> None:
    snapshot = _snapshot()
    mutation(snapshot)

    with pytest.raises(StatisticsProjectionError, match=message):
        parse_session_statistics_snapshot(snapshot)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["must_have_basic_stats"]["turns"].__setitem__(
                "started", True
            ),
            "nonnegative integer",
        ),
        (
            lambda value: value["recommended_insight_stats"].__setitem__(
                "hands_on_turn_rate_percent", math.nan
            ),
            "finite number",
        ),
        (
            lambda value: value["must_have_basic_stats"]["model_tool_requests"][
                "by_tool"
            ].__setitem__("exec", 0),
            "positive integer",
        ),
        (
            lambda value: value["must_have_basic_stats"][
                "completed_turn_duration_ms"
            ].__setitem__("p90", None),
            "requires all summaries",
        ),
        (
            lambda value: value["turn_statistics"][0][
                "recommended_insight_stats"
            ].__setitem__("hands_on", 1),
            "must be a boolean",
        ),
        (
            lambda value: value["turn_statistics"][0].__setitem__(
                "terminal_at", "2026-08-16T11:59:59Z"
            ),
            "precedes start",
        ),
    ],
)
def test_invalid_base_values_are_rejected(mutation, message: str) -> None:
    snapshot = _snapshot()
    mutation(snapshot)

    with pytest.raises(StatisticsProjectionError, match=message):
        parse_session_statistics_snapshot(snapshot)


def test_empty_distributions_and_null_rates_remain_explicit() -> None:
    snapshot = _snapshot()
    basic = snapshot["must_have_basic_stats"]
    basic["completed_turn_duration_ms"] = _distribution()
    context = basic["context_window"]
    context["latest_session_median_percent"] = None
    insights = snapshot["recommended_insight_stats"]
    insights["command_zero_exit_rate_percent"] = None

    projection = parse_session_statistics_snapshot(snapshot)

    assert projection.distributions[0] == StatisticsDistribution(
        distribution_kind="completed_turn_duration_ms",
        observation_count=0,
        total=0,
        median=None,
        p75=None,
        p90=None,
        p95=None,
        maximum=None,
    )
    assert projection.context_latest_session_median_percent is None
    assert projection.command_zero_exit_rate_percent is None


def test_duplicate_turn_identity_is_rejected() -> None:
    snapshot = _snapshot()
    snapshot["turn_statistics"].append(copy.deepcopy(snapshot["turn_statistics"][0]))

    with pytest.raises(StatisticsProjectionError, match="duplicate identity"):
        parse_session_statistics_snapshot(snapshot)


def test_turn_collection_must_match_aggregate_count_and_outcomes() -> None:
    missing = _snapshot()
    missing["turn_statistics"] = []
    with pytest.raises(StatisticsProjectionError, match="every started turn"):
        parse_session_statistics_snapshot(missing)

    wrong_outcome = _snapshot()
    wrong_outcome["turn_statistics"][0]["outcome"] = "aborted"
    with pytest.raises(StatisticsProjectionError, match="aggregate turn outcomes"):
        parse_session_statistics_snapshot(wrong_outcome)
