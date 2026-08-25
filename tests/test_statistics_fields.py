from __future__ import annotations

from dataclasses import fields

import pytest

from rodex_registry.statistics_fields import (
    SESSION_STATISTICS_SCALARS,
    TURN_STATISTICS_SCALARS,
    StatisticsScalarKind,
)
from rodex_registry.statistics_projection import (
    SessionStatisticsProjection,
    TurnStatisticsProjection,
)


def test_scalar_layouts_follow_projection_field_order_without_parallel_lists() -> None:
    derived_collaboration = {
        "collaboration_operations_count",
        "collaboration_agents_started_count",
    }
    session_non_scalars = {
        *derived_collaboration,
        "distributions",
        "named_counts",
        "audit_limits",
        "turn_statistics",
    }
    turn_non_scalars = {
        *derived_collaboration,
        "codex_thread_id",
        "codex_turn_id",
        "started_at_utc",
        "terminal_at_utc",
        "outcome",
        "model",
        "reasoning_effort",
        "named_counts",
    }

    assert SESSION_STATISTICS_SCALARS.columns == tuple(
        field.name
        for field in fields(SessionStatisticsProjection)
        if field.name not in session_non_scalars
    )
    assert TURN_STATISTICS_SCALARS.columns == tuple(
        field.name
        for field in fields(TurnStatisticsProjection)
        if field.name not in turn_non_scalars
    )


def test_scalar_layout_generates_complete_sql_fragments() -> None:
    layout = TURN_STATISTICS_SCALARS

    assert layout.columns_sql.split(", ") == list(layout.columns)
    assert layout.placeholders_sql.split(", ") == ["?"] * len(layout.fields)
    assert layout.excluded_updates_sql.split(", ") == [
        f"{name} = excluded.{name}" for name in layout.columns
    ]
    assert layout.excluded_changes_sql.split(" OR ") == [
        f"{name} IS NOT excluded.{name}" for name in layout.columns
    ]


def test_scalar_fields_own_nullability_storage_and_row_decoding() -> None:
    by_name = {field.name: field for field in TURN_STATISTICS_SCALARS.fields}

    assert by_name["hands_on"].kind is StatisticsScalarKind.BOOLEAN
    assert by_name["hands_on"].schema_column == ("hands_on", "INTEGER", 1, 0)
    assert by_name["hands_on"].read(0) is False
    assert by_name["hands_on"].read(1) is True
    assert by_name["duration_ms"].schema_column == ("duration_ms", "INTEGER", 0, 0)
    assert by_name["duration_ms"].read(None) is None
    with pytest.raises(ValueError, match="unexpectedly null"):
        by_name["input_tokens"].read(None)
