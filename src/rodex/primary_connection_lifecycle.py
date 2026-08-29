"""Canonical connection-epoch transition and reset coordination."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from threading import Lock
from typing import Protocol


class PrimaryConnectionResetParticipant(Protocol):
    def reset_after_disconnect(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PrimaryConnectionResetFailure:
    participant_name: str
    error: Exception


class PrimaryConnectionLifecycleCoordinator:
    """Advance one epoch after independently attempting every registered reset."""

    def __init__(
        self,
        participants: Iterable[PrimaryConnectionResetParticipant],
    ) -> None:
        self._participants = tuple(participants)
        self._epoch = 0
        self._lock = Lock()

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def reset_after_disconnect(self) -> tuple[PrimaryConnectionResetFailure, ...]:
        """Attempt all resets and complete the epoch even when one participant fails."""
        failures: list[PrimaryConnectionResetFailure] = []
        with self._lock:
            for participant in self._participants:
                try:
                    participant.reset_after_disconnect()
                except Exception as error:
                    failures.append(
                        PrimaryConnectionResetFailure(
                            _participant_name(participant),
                            error,
                        )
                    )
            self._epoch += 1
        return tuple(failures)

    def __call__(self) -> None:
        self.reset_after_disconnect()


def _participant_name(participant: PrimaryConnectionResetParticipant) -> str:
    configured = getattr(participant, "name", None)
    if isinstance(configured, str) and configured:
        return configured
    return type(participant).__name__
