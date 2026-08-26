"""Blocking protocol-event scheduling for the singular analytics pipeline."""

from __future__ import annotations

import json
import queue
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Final

from websockets.sync.client import unix_connect

from rodex_registry import CodexThreadId, parse_codex_thread_id

from .app_server_contract import CODEX_APP_SERVER
from .protocol_proxy import (
    ANALYTICS_EVENT_STREAM_PATH,
    ANALYTICS_WAKE_EVENT_METHODS,
    EVENT_STREAM_READY_METHOD,
)

ANALYTICS_QUIET_SECONDS: Final = 0.5
ANALYTICS_MAX_BATCH_SECONDS: Final = 5.0
ANALYTICS_RETRY_INITIAL_SECONDS: Final = 0.5
ANALYTICS_MAX_RETRY_WINDOW_SECONDS: Final = 5.0
ANALYTICS_SUBSCRIBER_START_TIMEOUT_SECONDS: Final = 5.0
_DIRTY: Final = object()
_STOP: Final = object()
_STREAM_CLOSED: Final = object()
_SIGNAL_QUEUE_CAPACITY: Final = 2
_WINDOW_RETRY_RESULTS: Final = frozenset(
    {"awaiting_append", "catching_up", "publication_retry"}
)
_ONE_SHOT_RETRY_RESULT: Final = "clean_replay"
_POST_RECONCILIATION_RETRY_RESULTS: Final = frozenset(
    {"publication_retry", _ONE_SHOT_RETRY_RESULT}
)


class AnalyticsEventStreamClosed(RuntimeError):
    """The runtime event stream closed while analytics was active."""


@dataclass(frozen=True, slots=True)
class AnalyticsDirtyBatch:
    """Exact source identities coalesced into one bounded reconciliation."""

    thread_ids: frozenset[CodexThreadId]
    full_reconcile: bool = False


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


@dataclass(slots=True)
class _AnalyticsRetryWindow:
    """Bound exact catch-up retries without creating permanent idle I/O."""

    generation_started_at: float
    deadline: float
    next_retry_at: float
    next_delay_seconds: float
    repeatable: bool

    @classmethod
    def start(
        cls,
        now: float,
        *,
        generation_started_at: float,
        initial_seconds: float,
        max_window_seconds: float,
        repeatable: bool,
    ) -> _AnalyticsRetryWindow:
        deadline = generation_started_at + max_window_seconds
        return cls(
            generation_started_at=generation_started_at,
            deadline=deadline,
            next_retry_at=min(now + initial_seconds, deadline),
            next_delay_seconds=initial_seconds * 2,
            repeatable=repeatable,
        )

    def retain_after(self, result: object, now: float) -> bool:
        if (
            not self.repeatable
            or result not in _WINDOW_RETRY_RESULTS
            or now >= self.deadline
        ):
            return False
        self.next_retry_at = min(now + self.next_delay_seconds, self.deadline)
        self.next_delay_seconds *= 2
        return True


class AnalyticsEventScheduler:
    """Coalesce relevant runtime events and reconcile only non-empty work."""

    def __init__(
        self,
        *,
        quiet_seconds: float = ANALYTICS_QUIET_SECONDS,
        max_batch_seconds: float = ANALYTICS_MAX_BATCH_SECONDS,
        retry_initial_seconds: float = ANALYTICS_RETRY_INITIAL_SECONDS,
        max_retry_window_seconds: float = ANALYTICS_MAX_RETRY_WINDOW_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        event_observer: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        if (
            quiet_seconds < 0
            or max_batch_seconds <= 0
            or retry_initial_seconds <= 0
            or max_retry_window_seconds < retry_initial_seconds
        ):
            raise ValueError("analytics batch intervals must be positive")
        self._quiet_seconds = quiet_seconds
        self._max_batch_seconds = max_batch_seconds
        self._retry_initial_seconds = retry_initial_seconds
        self._max_retry_window_seconds = max_retry_window_seconds
        self._monotonic = monotonic
        self._event_observer = event_observer
        self._signals: queue.Queue[object] = queue.Queue(maxsize=_SIGNAL_QUEUE_CAPACITY)
        self._pending_thread_ids: set[CodexThreadId] = set()
        self._pending_first_at: float | None = None
        self._pending_last_at: float | None = None
        self._closed = False
        self._state_lock = Lock()

    def offer_protocol_event(self, event: Mapping[str, Any]) -> None:
        """Mark analytics dirty only for a relevant App Server lifecycle event."""
        if self._event_observer is not None:
            self._event_observer(event)
        thread_id = _analytics_event_thread_id(event)
        if thread_id is not None:
            self.offer_dirty(thread_id)

    def offer_dirty(self, thread_id: CodexThreadId) -> None:
        """Retain an exact dirty identity and offer a bounded nonblocking wake-up."""
        parsed_thread_id = parse_codex_thread_id(thread_id)
        with self._state_lock:
            if self._closed:
                return
            now = self._monotonic()
            if self._pending_first_at is None:
                self._pending_first_at = now
            self._pending_last_at = max(self._pending_last_at or now, now)
            self._pending_thread_ids.add(parsed_thread_id)
        try:
            self._signals.put_nowait(_DIRTY)
        except queue.Full:
            return

    def event_stream_closed(self) -> None:
        self._offer_terminal(_STREAM_CLOSED)

    def close(self) -> None:
        self._offer_terminal(_STOP)

    def run(self, reconcile: Callable[[AnalyticsDirtyBatch], object]) -> None:
        """Reconcile bursts and bound exact catch-up retries to one short window."""
        terminal = self._queued_terminal()
        if terminal is _STOP:
            return
        if terminal is _STREAM_CLOSED:
            raise AnalyticsEventStreamClosed("analytics event stream closed")
        startup_started_at = self._monotonic()
        retry_window = self._retry_window_for(
            reconcile(AnalyticsDirtyBatch(frozenset(), full_reconcile=True)),
            generation_started_at=startup_started_at,
        )
        while True:
            terminal = self._queued_terminal()
            if terminal is _STOP:
                return
            if terminal is _STREAM_CLOSED:
                raise AnalyticsEventStreamClosed("analytics event stream closed")
            now = self._monotonic()
            pending_deadline = self._pending_deadline()
            if pending_deadline is not None and now >= pending_deadline:
                batch, generation_started_at = self._take_pending_batch()
                retry_window = self._retry_window_for(
                    reconcile(batch),
                    generation_started_at=generation_started_at,
                )
                continue
            if retry_window is not None and now >= retry_window.next_retry_at:
                completed_repeatable_retry = retry_window.repeatable
                generation_started_at = retry_window.generation_started_at
                result = reconcile(AnalyticsDirtyBatch(frozenset()))
                if not retry_window.retain_after(result, self._monotonic()):
                    repeatable_window_finished = (
                        completed_repeatable_retry and result in _WINDOW_RETRY_RESULTS
                    )
                    one_shot_replay_finished = (
                        not completed_repeatable_retry and result == _ONE_SHOT_RETRY_RESULT
                    )
                    if repeatable_window_finished or one_shot_replay_finished:
                        retry_window = None
                    else:
                        retry_window = self._retry_window_for(
                            result,
                            generation_started_at=generation_started_at,
                        )
                continue
            retry_deadline = None if retry_window is None else retry_window.next_retry_at
            deadlines = tuple(
                item for item in (pending_deadline, retry_deadline) if item is not None
            )
            deadline = min(deadlines) if deadlines else None
            timeout = None if deadline is None else max(0.0, deadline - now)
            try:
                signal = self._signals.get(timeout=timeout)
            except queue.Empty:
                continue
            if signal is _STOP:
                return
            if signal is _STREAM_CLOSED:
                raise AnalyticsEventStreamClosed("analytics event stream closed")

    def _queued_terminal(self) -> object | None:
        for _ in range(_SIGNAL_QUEUE_CAPACITY):
            try:
                signal = self._signals.get_nowait()
            except queue.Empty:
                return None
            if signal is _STOP or signal is _STREAM_CLOSED:
                return signal
        return None

    def _retry_window_for(
        self,
        result: object,
        *,
        generation_started_at: float,
    ) -> _AnalyticsRetryWindow | None:
        if result not in _WINDOW_RETRY_RESULTS and result != _ONE_SHOT_RETRY_RESULT:
            return None
        now = self._monotonic()
        window_started_at = (
            now if result in _POST_RECONCILIATION_RETRY_RESULTS else generation_started_at
        )
        if now >= window_started_at + self._max_retry_window_seconds:
            return None
        return _AnalyticsRetryWindow.start(
            now,
            generation_started_at=window_started_at,
            initial_seconds=self._retry_initial_seconds,
            max_window_seconds=self._max_retry_window_seconds,
            repeatable=result in _WINDOW_RETRY_RESULTS,
        )

    def _offer_terminal(self, signal: object) -> None:
        with self._state_lock:
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

    def _pending_deadline(self) -> float | None:
        with self._state_lock:
            first_at = self._pending_first_at
            last_at = self._pending_last_at
        if first_at is None or last_at is None:
            return None
        return min(
            last_at + self._quiet_seconds,
            first_at + self._max_batch_seconds,
        )

    def _take_pending_batch(self) -> tuple[AnalyticsDirtyBatch, float]:
        with self._state_lock:
            batch = AnalyticsDirtyBatch(frozenset(self._pending_thread_ids))
            generation_started_at = self._pending_first_at
            self._pending_thread_ids.clear()
            self._pending_first_at = None
            self._pending_last_at = None
        if generation_started_at is None:
            raise RuntimeError("analytics pending batch lost its first event time")
        return batch, generation_started_at


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
        self._ready = Event()
        self._startup_error: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("analytics event subscriber is already running")
        self._thread = Thread(
            target=self._run,
            name="rodex-analytics-event-subscriber",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(ANALYTICS_SUBSCRIBER_START_TIMEOUT_SECONDS):
            self.close()
            raise AnalyticsEventStreamClosed("analytics event stream did not become ready")
        if self._startup_error is not None:
            self.close()
            raise AnalyticsEventStreamClosed(
                "analytics event stream failed during startup"
            ) from self._startup_error

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
                uri=f"ws://localhost{ANALYTICS_EVENT_STREAM_PATH}",
                compression=None,
                max_size=None,
            ) as connection:
                with self._connection_lock:
                    self._connection = connection
                for message in connection:
                    if self._stop.is_set():
                        unexpected_close = False
                        return
                    event = _decode_protocol_event(message)
                    if not self._ready.is_set() and (
                        event is None or event.get("method") != EVENT_STREAM_READY_METHOD
                    ):
                        raise AnalyticsEventStreamClosed(
                            "analytics event stream sent no ready snapshot"
                        )
                    if event is not None:
                        self._scheduler.offer_protocol_event(event)
                    self._ready.set()
        except Exception as error:
            if not self._ready.is_set():
                self._startup_error = error
            unexpected_close = not self._stop.is_set()
        finally:
            with self._connection_lock:
                self._connection = None
            if unexpected_close:
                self._scheduler.event_stream_closed()
            self._ready.set()


def _is_relevant_protocol_event(message: str | bytes) -> bool:
    decoded = _decode_protocol_event(message)
    return decoded is not None and decoded.get("method") in ANALYTICS_WAKE_EVENT_METHODS


def _analytics_event_thread_id(
    event: Mapping[str, Any],
) -> CodexThreadId | None:
    method = event.get("method")
    if method not in ANALYTICS_WAKE_EVENT_METHODS:
        return None
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    if method == CODEX_APP_SERVER.thread_started_method:
        thread = params.get("thread")
        value = thread.get("id") if isinstance(thread, Mapping) else None
    else:
        value = params.get("threadId")
    try:
        return parse_codex_thread_id(value)
    except (TypeError, ValueError):
        return None


def _decode_protocol_event(message: str | bytes) -> dict[str, Any] | None:
    try:
        decoded = json.loads(message)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None
