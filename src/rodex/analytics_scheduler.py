"""Blocking protocol-event scheduling for the singular analytics pipeline."""

from __future__ import annotations

import json
import queue
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Final

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import unix_connect

from .app_server_contract import CODEX_APP_SERVER

ANALYTICS_QUIET_SECONDS: Final = 0.5
ANALYTICS_MAX_BATCH_SECONDS: Final = 5.0
_DIRTY: Final = object()
_STOP: Final = object()
_STREAM_CLOSED: Final = object()
_RELEVANT_METHODS: Final = frozenset(
    {
        CODEX_APP_SERVER.thread_started_method,
        CODEX_APP_SERVER.turn_completed_method,
    }
)


class AnalyticsEventStreamClosed(RuntimeError):
    """The runtime event stream closed while analytics was active."""


@dataclass(slots=True)
class AnalyticsBurstWindow:
    """Quiet and hard deadlines for one non-empty dirty generation."""

    first_event_at: float
    last_event_at: float
    quiet_seconds: float = ANALYTICS_QUIET_SECONDS
    max_batch_seconds: float = ANALYTICS_MAX_BATCH_SECONDS

    @classmethod
    def start(
        cls,
        now: float,
        *,
        quiet_seconds: float = ANALYTICS_QUIET_SECONDS,
        max_batch_seconds: float = ANALYTICS_MAX_BATCH_SECONDS,
    ) -> AnalyticsBurstWindow:
        if quiet_seconds < 0 or max_batch_seconds <= 0:
            raise ValueError("analytics batch intervals must be positive")
        return cls(now, now, quiet_seconds, max_batch_seconds)

    def observe(self, now: float) -> None:
        self.last_event_at = now

    @property
    def deadline(self) -> float:
        return min(
            self.last_event_at + self.quiet_seconds,
            self.first_event_at + self.max_batch_seconds,
        )


class AnalyticsEventScheduler:
    """Coalesce relevant runtime events and reconcile only non-empty work."""

    def __init__(
        self,
        *,
        quiet_seconds: float = ANALYTICS_QUIET_SECONDS,
        max_batch_seconds: float = ANALYTICS_MAX_BATCH_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if quiet_seconds < 0 or max_batch_seconds <= 0:
            raise ValueError("analytics batch intervals must be positive")
        self._quiet_seconds = quiet_seconds
        self._max_batch_seconds = max_batch_seconds
        self._monotonic = monotonic
        self._signals: queue.Queue[object] = queue.Queue(maxsize=2)
        self._closed = False
        self._close_lock = Lock()

    def offer_protocol_message(self, message: str | bytes) -> None:
        """Mark analytics dirty only for a relevant App Server lifecycle event."""
        if _is_relevant_protocol_event(message):
            self.offer_dirty()

    def offer_dirty(self) -> None:
        """Offer bounded dirty state without blocking the protocol producer."""
        if self._closed:
            return
        try:
            self._signals.put_nowait(_DIRTY)
        except queue.Full:
            return

    def event_stream_closed(self) -> None:
        self._offer_terminal(_STREAM_CLOSED)

    def close(self) -> None:
        self._offer_terminal(_STOP)

    def run(self, reconcile: Callable[[], object]) -> None:
        """Reconcile startup once, then block until dirty work or shutdown."""
        reconcile()
        while True:
            signal = self._signals.get()
            if signal is _STOP:
                return
            if signal is _STREAM_CLOSED:
                raise AnalyticsEventStreamClosed("analytics event stream closed")
            burst = AnalyticsBurstWindow.start(
                self._monotonic(),
                quiet_seconds=self._quiet_seconds,
                max_batch_seconds=self._max_batch_seconds,
            )
            while True:
                remaining = burst.deadline - self._monotonic()
                if remaining <= 0:
                    break
                try:
                    signal = self._signals.get(timeout=remaining)
                except queue.Empty:
                    break
                if signal is _STOP:
                    return
                if signal is _STREAM_CLOSED:
                    raise AnalyticsEventStreamClosed("analytics event stream closed")
                burst.observe(self._monotonic())
            reconcile()

    def _offer_terminal(self, signal: object) -> None:
        with self._close_lock:
            if signal is _STOP:
                self._closed = True
            while True:
                try:
                    self._signals.put_nowait(signal)
                    return
                except queue.Full:
                    try:
                        self._signals.get_nowait()
                    except queue.Empty:
                        continue


class AnalyticsProtocolEventSubscriber:
    """Read the runtime's event socket without performing analytics on its thread."""

    def __init__(
        self,
        event_socket_path: Path,
        scheduler: AnalyticsEventScheduler,
    ) -> None:
        self._event_socket_path = event_socket_path
        self._scheduler = scheduler
        self._stop = Event()
        self._connection: Any | None = None
        self._connection_lock = Lock()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("analytics event subscriber is already running")
        self._thread = Thread(
            target=self._run,
            name="rodex-analytics-event-subscriber",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            connection.close()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5)

    def _run(self) -> None:
        unexpected_close = True
        try:
            with unix_connect(
                str(self._event_socket_path),
                compression=None,
                max_size=None,
            ) as connection:
                with self._connection_lock:
                    self._connection = connection
                for message in connection:
                    if self._stop.is_set():
                        unexpected_close = False
                        return
                    self._scheduler.offer_protocol_message(message)
        except (ConnectionClosed, InvalidHandshake, OSError):
            unexpected_close = not self._stop.is_set()
        finally:
            with self._connection_lock:
                self._connection = None
            if unexpected_close:
                self._scheduler.event_stream_closed()


def _is_relevant_protocol_event(message: str | bytes) -> bool:
    try:
        decoded = json.loads(message)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return False
    return isinstance(decoded, dict) and decoded.get("method") in _RELEVANT_METHODS
