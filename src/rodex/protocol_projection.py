"""Exception containment and content-free health for auxiliary protocol observers.

This is synchronous: it does not bound callback execution time. Native forwarding,
prompt admission and mandatory event continuity must stay outside this guard.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import ParamSpec

_Arguments = ParamSpec("_Arguments")


@dataclass(frozen=True, slots=True)
class ProtocolProjectionFailure:
    participant_name: str
    failure_count: int
    last_exception_type: str


class ProtocolProjectionGuard:
    """Attempt each named auxiliary participant without aborting other participants."""

    def __init__(self) -> None:
        self._failures: dict[str, ProtocolProjectionFailure] = {}
        self._lock = Lock()

    @property
    def failures(self) -> tuple[ProtocolProjectionFailure, ...]:
        """Return immutable health records, never callback inputs or exception text."""
        with self._lock:
            return tuple(self._failures.values())

    def observe(
        self,
        participant_name: str,
        observer: Callable[_Arguments, object],
        *args: _Arguments.args,
        **kwargs: _Arguments.kwargs,
    ) -> bool:
        """Report completion so owners can revoke derived authority after a failure.

        Use fixed owner-defined participant names, not protocol-derived labels.
        """
        try:
            observer(*args, **kwargs)
        except Exception as error:
            with self._lock:
                previous = self._failures.get(participant_name)
                self._failures[participant_name] = ProtocolProjectionFailure(
                    participant_name,
                    1 if previous is None else previous.failure_count + 1,
                    type(error).__name__,
                )
            return False
        return True
