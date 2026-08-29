"""Canonical connection-epoch transition and reset coordination."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from threading import Event, Lock
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


class RuntimeShutdownCoordinator:
    """Latch one terminal host reason and wake every registered boundary."""

    def __init__(self) -> None:
        self._terminal_event = Event()
        self._terminal_reason: str | None = None
        self._interrupts: dict[int, Callable[[], None]] = {}
        self._next_subscription = 0
        self._lock = Lock()

    @property
    def terminal_event(self) -> Event:
        return self._terminal_event

    @property
    def terminal_reason(self) -> str | None:
        with self._lock:
            return self._terminal_reason

    def subscribe_interrupt(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a nonblocking wake callback, invoking immediately if terminal."""
        with self._lock:
            if self._terminal_reason is None:
                self._next_subscription += 1
                subscription = self._next_subscription
                self._interrupts[subscription] = callback
                invoke_now = False
            else:
                subscription = 0
                invoke_now = True
        if invoke_now:
            _invoke_interrupt(callback)

        def unsubscribe() -> None:
            with self._lock:
                self._interrupts.pop(subscription, None)

        return unsubscribe

    def request_shutdown(self, reason: str) -> bool:
        """Latch the first terminal reason and independently wake all boundaries."""
        if not reason:
            raise ValueError("runtime shutdown reason must be non-empty")
        with self._lock:
            if self._terminal_reason is not None:
                return False
            self._terminal_reason = reason
            callbacks = tuple(self._interrupts.values())
            self._interrupts.clear()
            self._terminal_event.set()
        for callback in callbacks:
            _invoke_interrupt(callback)
        return True


def _participant_name(participant: PrimaryConnectionResetParticipant) -> str:
    configured = getattr(participant, "name", None)
    if isinstance(configured, str) and configured:
        return configured
    return type(participant).__name__


def _invoke_interrupt(callback: Callable[[], None]) -> None:
    with suppress(Exception):
        callback()
