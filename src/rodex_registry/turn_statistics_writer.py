"""Shared mutable per-turn metrics for ordinary publication and verified rebuild."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from .schema import RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE, RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE
from .statistics_fields import TURN_STATISTICS_SCALARS
from .statistics_projection import StatisticsNamedCount, TurnStatisticsProjection


def upsert_turn_statistics_metrics(
    connection: sqlite3.Connection,
    session_id: int,
    turn_row_id: int,
    projection: TurnStatisticsProjection,
) -> None:
    """Replace only the current statistics facts for one stable Codex turn."""
    values = TURN_STATISTICS_SCALARS.write_values(projection)
    existing = connection.execute(
        f"SELECT id, {TURN_STATISTICS_SCALARS.columns_sql} "
        f"FROM {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} "
        "WHERE rodex_sessions_codex_turns_id = ?",
        (turn_row_id,),
    ).fetchone()
    if existing is None:
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} "
            "(rodex_sessions_id, rodex_sessions_codex_turns_id, "
            f"{TURN_STATISTICS_SCALARS.columns_sql}) VALUES (?, ?, "
            f"{TURN_STATISTICS_SCALARS.placeholders_sql})",
            (session_id, turn_row_id, *values),
        )
        return
    if tuple(existing[1:]) == values:
        return
    connection.execute(
        f"UPDATE {RODEX_SESSIONS_STATISTICS_TURN_METRICS_TABLE} SET "
        + ", ".join(f"{column} = ?" for column in TURN_STATISTICS_SCALARS.columns)
        + " WHERE id = ?",
        (*values, int(existing[0])),
    )


def sync_turn_named_counts(
    connection: sqlite3.Connection,
    session_id: int,
    turn_row_id: int,
    named_counts: Sequence[StatisticsNamedCount],
) -> None:
    desired = {
        (item.count_kind, item.count_name): item for item in named_counts if item.count_kind != "collaboration_tool"
    }
    stored = {
        (str(row[0]), str(row[1])): int(row[2])
        for row in connection.execute(
            f"SELECT count_kind, count_name, occurrence_count FROM "
            f"{RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
            "WHERE rodex_sessions_codex_turns_id = ?",
            (turn_row_id,),
        )
    }
    connection.executemany(
        f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
        "(rodex_sessions_id, rodex_sessions_codex_turns_id, count_kind, "
        "count_name, occurrence_count) VALUES (?, ?, ?, ?, ?)",
        (
            (
                session_id,
                turn_row_id,
                item.count_kind,
                item.count_name,
                item.occurrence_count,
            )
            for key, item in desired.items()
            if key not in stored
        ),
    )
    connection.executemany(
        f"UPDATE {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
        "SET occurrence_count = ? WHERE rodex_sessions_codex_turns_id = ? "
        "AND count_kind = ? AND count_name = ?",
        (
            (item.occurrence_count, turn_row_id, *key)
            for key, item in desired.items()
            if key in stored and stored[key] != item.occurrence_count
        ),
    )
    connection.executemany(
        f"DELETE FROM {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
        "WHERE rodex_sessions_codex_turns_id = ? "
        "AND count_kind = ? AND count_name = ?",
        ((turn_row_id, count_kind, count_name) for count_kind, count_name in stored.keys() - desired.keys()),
    )
