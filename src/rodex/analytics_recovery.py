"""One failure budget, independent of protocol event and append generations."""

from dataclasses import dataclass

ANALYTICS_FAILURE_INITIAL_DELAY_SECONDS = 30.0
ANALYTICS_FAILURE_MAX_BACKOFF_SECONDS = 900.0
ANALYTICS_FAILURE_IDLE_COST_MULTIPLIER = 99.0


@dataclass(slots=True)
class AnalyticsRecoveryBudget:
    """Bound repeated failed work to at most 1% of elapsed time after each attempt.

    The exponential floor is capped, but the measured-cost component is not:
    larger histories must not defeat the duty-cycle bound. Only accepted work
    resets the budget; new bytes, events and retirement cannot do so.
    """

    failures: int = 0
    retry_at: float | None = None

    def failed(self, *, started_at: float, finished_at: float) -> None:
        self.failures += 1
        floor = min(
            ANALYTICS_FAILURE_INITIAL_DELAY_SECONDS * 2 ** min(self.failures - 1, 5),
            ANALYTICS_FAILURE_MAX_BACKOFF_SECONDS,
        )
        self.retry_at = finished_at + max(
            floor,
            max(0.0, finished_at - started_at) * ANALYTICS_FAILURE_IDLE_COST_MULTIPLIER,
        )

    def accepted(self) -> None:
        self.failures = 0
        self.retry_at = None
