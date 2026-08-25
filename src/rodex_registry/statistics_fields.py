"""Typed scalar layouts joining statistics projections to SQLite rows."""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from types import NoneType
from typing import Final, get_args, get_type_hints

from .statistics_projection import (
    SessionStatisticsProjection,
    TurnStatisticsProjection,
)


class StatisticsScalarKind(StrEnum):
    INTEGER = "INTEGER"
    REAL = "REAL"
    TEXT = "TEXT"
    BOOLEAN = "BOOLEAN"


@dataclass(frozen=True, slots=True)
class StatisticsScalarField:
    """One named projection scalar and its SQLite representation."""

    name: str
    kind: StatisticsScalarKind
    nullable: bool

    @property
    def sqlite_type(self) -> str:
        return "INTEGER" if self.kind is StatisticsScalarKind.BOOLEAN else self.kind.value

    @property
    def schema_column(self) -> tuple[str, str, int, int]:
        return self.name, self.sqlite_type, int(not self.nullable), 0

    def read(self, value: object) -> object:
        if value is None:
            if not self.nullable:
                raise ValueError(
                    f"stored statistics scalar is unexpectedly null: {self.name}"
                )
            return None
        if self.kind is StatisticsScalarKind.BOOLEAN:
            return bool(value)
        if self.kind is StatisticsScalarKind.INTEGER:
            return int(value)
        if self.kind is StatisticsScalarKind.REAL:
            return float(value)
        return str(value)


@dataclass(frozen=True, slots=True)
class StatisticsScalarLayout:
    """The authoritative ordered scalar contract for one projection table."""

    fields: tuple[StatisticsScalarField, ...]

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    @property
    def columns_sql(self) -> str:
        return ", ".join(self.columns)

    @property
    def placeholders_sql(self) -> str:
        return ", ".join("?" for _field in self.fields)

    @property
    def excluded_updates_sql(self) -> str:
        return ", ".join(f"{field.name} = excluded.{field.name}" for field in self.fields)

    @property
    def excluded_changes_sql(self) -> str:
        """Return a null-safe predicate that rejects unchanged UPSERT writes."""
        return " OR ".join(
            f"{field.name} IS NOT excluded.{field.name}" for field in self.fields
        )

    @property
    def schema_columns(self) -> tuple[tuple[str, str, int, int], ...]:
        return tuple(field.schema_column for field in self.fields)

    def write_values(self, projection: object) -> tuple[object, ...]:
        return tuple(getattr(projection, field.name) for field in self.fields)

    def read_values(self, values: tuple[object, ...]) -> dict[str, object]:
        return {
            field.name: field.read(value)
            for field, value in zip(self.fields, values, strict=True)
        }


def _scalar_layout(
    projection_type: type[object],
    *,
    excluded: frozenset[str],
) -> StatisticsScalarLayout:
    annotations = get_type_hints(projection_type)
    scalar_fields = []
    for item in fields(projection_type):
        if item.name in excluded:
            continue
        annotation = annotations[item.name]
        alternatives = get_args(annotation)
        nullable = NoneType in alternatives
        scalar_type = next(
            (alternative for alternative in alternatives if alternative is not NoneType),
            annotation,
        )
        if scalar_type is bool:
            kind = StatisticsScalarKind.BOOLEAN
        elif scalar_type is int:
            kind = StatisticsScalarKind.INTEGER
        elif scalar_type is float:
            kind = StatisticsScalarKind.REAL
        elif scalar_type is str:
            kind = StatisticsScalarKind.TEXT
        else:
            raise TypeError(
                f"unsupported statistics scalar annotation for {item.name}: {annotation}"
            )
        scalar_fields.append(StatisticsScalarField(item.name, kind, nullable))
    return StatisticsScalarLayout(tuple(scalar_fields))


SESSION_STATISTICS_SCALARS: Final = _scalar_layout(
    SessionStatisticsProjection,
    excluded=frozenset(
        {
            "collaboration_operations_count",
            "collaboration_agents_started_count",
            "distributions",
            "named_counts",
            "audit_limits",
            "turn_statistics",
        }
    ),
)
TURN_STATISTICS_SCALARS: Final = _scalar_layout(
    TurnStatisticsProjection,
    excluded=frozenset(
        {
            "codex_thread_id",
            "codex_turn_id",
            "started_at_utc",
            "terminal_at_utc",
            "outcome",
            "model",
            "reasoning_effort",
            "collaboration_operations_count",
            "collaboration_agents_started_count",
            "named_counts",
        }
    ),
)
