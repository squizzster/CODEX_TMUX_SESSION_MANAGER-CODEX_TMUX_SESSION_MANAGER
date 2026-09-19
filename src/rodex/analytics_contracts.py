"""Dependency-light inputs and results for the optional analyzer implementation."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from rodex_registry import CodexThreadId, SessionStatisticsProjection


class RodexAnalyticsError(RuntimeError):
    """The optional analytics subsystem could not satisfy a request."""


@dataclass(frozen=True, slots=True)
class AnalyticsCalculation:
    statistics_projection: SessionStatisticsProjection
    coverage_state: str


@dataclass(frozen=True, slots=True)
class AnalyticsAnalyzerSource:
    codex_thread_id: CodexThreadId
    analyzer_content: bytes
    appended_analyzer_content: bytes


class AnalyticsBoundary(Protocol):
    def analyze_rollouts(self, sources: Sequence[AnalyticsAnalyzerSource], user_id: str) -> AnalyticsCalculation: ...

    def accept_batch(self) -> None: ...


AnalyticsBoundaryFactory = Callable[[], AnalyticsBoundary]


def create_analytics_adapter() -> AnalyticsBoundary:
    """Load private dependency APIs only inside the worker's fail-open boundary."""
    from .analytics_analyzer import StatefulCodexProtocolAnalyticsAdapter

    return StatefulCodexProtocolAnalyticsAdapter()
