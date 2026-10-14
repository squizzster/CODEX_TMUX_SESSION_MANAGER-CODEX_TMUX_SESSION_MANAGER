"""Inject one exact steer when the registered main context crosses its limit."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock, Timer
from typing import Final, Protocol

from .app_server_contract import CODEX_APP_SERVER
from .interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRejected,
    InteractionRequest,
    InteractionResult,
    SessionInteractionPipeline,
)

CONTEXT_STEER_THRESHOLD_PERCENT: Final = 70.0
CONTEXT_STEER_MESSAGE: Final = (
    "What's the most relevant objectives currently?, where/why? Compact insights, correctly finish whatever "
    "your're currently doing...no rush...."
)
CONTEXT_STEER_SOURCE: Final = "context-threshold-steer"


class DeferredCall(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


DeferredCallFactory = Callable[[float, Callable[[], None]], DeferredCall]
SteeringLogger = Callable[[str], None]


def _timer_factory(delay_seconds: float, callback: Callable[[], None]) -> Timer:
    timer = Timer(delay_seconds, callback)
    timer.daemon = True
    return timer


@dataclass(frozen=True, slots=True)
class ContextThresholdSteerAttempt:
    thread_id: str
    turn_id: str
    context_percent: float
    threshold_percent: float
    dispatch_id: str


@dataclass(frozen=True, slots=True)
class ContextThresholdSteeringSnapshot:
    root_thread_id: str | None
    dispatch_ready: bool
    active_turn_id: str | None
    context_percent: float | None
    threshold_armed: bool
    compacting: bool
    pending_turn_id: str | None
    last_completed_dispatch_id: str | None


class ContextThresholdSteeringController:
    """Turn exact context observations into at most one admitted steer per crossing."""

    name: Final = "context-threshold-steering"

    def __init__(
        self,
        interaction_pipeline: SessionInteractionPipeline,
        *,
        threshold_percent: float = CONTEXT_STEER_THRESHOLD_PERCENT,
        message: str = CONTEXT_STEER_MESSAGE,
        logger: SteeringLogger = lambda _message: None,
        deferred_call_factory: DeferredCallFactory = _timer_factory,
    ) -> None:
        if (
            not isinstance(threshold_percent, (int, float))
            or isinstance(threshold_percent, bool)
            or not math.isfinite(threshold_percent)
            or not 0 < threshold_percent <= 100
        ):
            raise ValueError("context steer threshold must be finite and between zero and 100")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("context steer message must be non-empty")
        self._interaction_pipeline = interaction_pipeline
        self._threshold_percent = float(threshold_percent)
        self._message = message
        self._logger = logger
        self._deferred_call_factory = deferred_call_factory
        self._lock = Lock()
        self._root_thread_id: str | None = None
        self._dispatch_ready = False
        self._active_turn_id: str | None = None
        self._context_percent: float | None = None
        self._threshold_armed = True
        self._active_compaction_item_ids: set[str] = set()
        self._pending_attempt: ContextThresholdSteerAttempt | None = None
        self._pending_call: DeferredCall | None = None
        self._dispatch_requests: dict[str, tuple[int, ContextThresholdSteerAttempt]] = {}
        self._admitted_request_ids: set[str] = set()
        self._last_completed_dispatch_id: str | None = None
        self._generation = 0
        self._closed = False

    @property
    def snapshot(self) -> ContextThresholdSteeringSnapshot:
        with self._lock:
            pending = self._pending_attempt
            return ContextThresholdSteeringSnapshot(
                self._root_thread_id,
                self._dispatch_ready,
                self._active_turn_id,
                self._context_percent,
                self._threshold_armed,
                bool(self._active_compaction_item_ids),
                None if pending is None else pending.turn_id,
                self._last_completed_dispatch_id,
            )

    def bind_root_thread(self, thread_id: str) -> None:
        """Admit steering only after durable runtime registration identifies main."""
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("context steering requires a root thread identity")
        cancelled_call: DeferredCall | None = None
        scheduled: tuple[ContextThresholdSteerAttempt, DeferredCall] | None = None
        with self._lock:
            if self._closed:
                return
            if self._root_thread_id != thread_id:
                _cancelled, cancelled_call = self._clear_pending_locked()
                self._root_thread_id = thread_id
                self._active_turn_id = None
                self._context_percent = None
                self._threshold_armed = True
                self._active_compaction_item_ids.clear()
            self._dispatch_ready = True
            scheduled = self._schedule_if_ready_locked()
        if cancelled_call is not None:
            cancelled_call.cancel()
        self._start_scheduled(scheduled)

    def observe_protocol_event(self, event: Mapping[str, object] | None) -> None:
        """Track the exact main active turn and compaction state without blocking."""
        if event is None:
            return
        method = event.get("method")
        params = event.get("params")
        if not isinstance(method, str) or not isinstance(params, Mapping):
            return
        if method == CODEX_APP_SERVER.thread_started_method:
            thread = params.get("thread")
            thread_id = thread.get("id") if isinstance(thread, Mapping) else None
            if isinstance(thread_id, str) and thread_id:
                with self._lock:
                    if not self._closed and self._root_thread_id is None:
                        # The first primary thread is observed before durable
                        # registration. It may accumulate state but cannot dispatch.
                        self._root_thread_id = thread_id
            return

        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or not thread_id:
            return
        cancelled_call: DeferredCall | None = None
        scheduled: tuple[ContextThresholdSteerAttempt, DeferredCall] | None = None
        with self._lock:
            if self._closed or thread_id != self._root_thread_id:
                return
            if method == CODEX_APP_SERVER.turn_started_method:
                turn_id = _turn_id(params)
                if turn_id is None:
                    return
                if self._active_turn_id is not None and self._active_turn_id != turn_id:
                    _cancelled, cancelled_call = self._clear_pending_locked()
                self._active_turn_id = turn_id
                scheduled = self._schedule_if_ready_locked()
            elif method == CODEX_APP_SERVER.turn_completed_method:
                turn_id = _turn_id(params)
                if self._active_turn_id is None or turn_id == self._active_turn_id:
                    self._active_turn_id = None
                    self._active_compaction_item_ids.clear()
                    _cancelled, cancelled_call = self._clear_pending_locked()
            elif method == CODEX_APP_SERVER.thread_status_changed_method:
                thread = params.get("thread")
                status = thread.get("status") if isinstance(thread, Mapping) else params.get("status")
                status_type = status.get("type") if isinstance(status, Mapping) else None
                if status_type in {"idle", "systemError", "notLoaded"}:
                    self._active_turn_id = None
                    self._active_compaction_item_ids.clear()
                    _cancelled, cancelled_call = self._clear_pending_locked()
            elif method in {"item/started", "item/completed"}:
                item = params.get("item")
                if not isinstance(item, Mapping) or item.get("type") != "contextCompaction":
                    return
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    return
                if method == "item/started":
                    self._active_compaction_item_ids.add(item_id)
                    self._context_percent = None
                    self._threshold_armed = False
                    _cancelled, cancelled_call = self._clear_pending_locked()
                else:
                    self._active_compaction_item_ids.discard(item_id)
            else:
                return
        if cancelled_call is not None:
            cancelled_call.cancel()
        self._start_scheduled(scheduled)

    def observe_context_percent(self, thread_id: str, context_percent: float) -> None:
        """Consume one authenticated exact-percentage observation for main."""
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("context observation requires a thread identity")
        if (
            not isinstance(context_percent, (int, float))
            or isinstance(context_percent, bool)
            or not math.isfinite(context_percent)
            or context_percent < 0
        ):
            raise ValueError("context observation must be a finite non-negative percentage")
        exact_percent = float(context_percent)
        cancelled_call: DeferredCall | None = None
        scheduled: tuple[ContextThresholdSteerAttempt, DeferredCall] | None = None
        with self._lock:
            if self._closed or thread_id != self._root_thread_id:
                return
            # Usage snapshots observed during compaction cannot establish a new
            # crossing. Wait for a post-compaction observation to rearm.
            if self._active_compaction_item_ids:
                return
            self._context_percent = exact_percent
            if exact_percent < self._threshold_percent:
                self._threshold_armed = True
                _cancelled, cancelled_call = self._clear_pending_locked()
            else:
                scheduled = self._schedule_if_ready_locked()
        if cancelled_call is not None:
            cancelled_call.cancel()
        self._start_scheduled(scheduled)

    def admit_dispatch(self, request: InteractionRequest) -> None:
        """Recheck threshold authority at the final exact-control send boundary."""
        if request.source != CONTEXT_STEER_SOURCE:
            return
        with self._lock:
            retained = self._dispatch_requests.pop(request.request_id, None)
            if retained is None:
                raise InteractionRejected("context steer has no trusted pending admission")
            generation, attempt = retained
            if (
                self._closed
                or generation != self._generation
                or self._pending_attempt is not attempt
                or not self._dispatch_ready
                or self._root_thread_id != attempt.thread_id
                or self._active_turn_id != attempt.turn_id
                or self._active_compaction_item_ids
                or self._context_percent is None
                or self._context_percent < self._threshold_percent
                or request.operation != InteractionOperation.STEER
                or request.expected_thread_id != attempt.thread_id
                or request.expected_turn_id != attempt.turn_id
                or request.dispatch_id != attempt.dispatch_id
                or request.text != self._message
            ):
                raise InteractionRejected("context steer became stale before dispatch admission")
            self._admitted_request_ids.add(request.request_id)
            self._clear_pending_locked()

    def reset_after_disconnect(self) -> None:
        """Discard connection/turn state while retaining durable root identity."""
        cancelled_call: DeferredCall | None = None
        with self._lock:
            if self._closed:
                return
            _cancelled, cancelled_call = self._clear_pending_locked()
            self._active_turn_id = None
            self._context_percent = None
            self._threshold_armed = True
            self._active_compaction_item_ids.clear()
        if cancelled_call is not None:
            cancelled_call.cancel()

    def close(self) -> None:
        cancelled_call: DeferredCall | None = None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            _cancelled, cancelled_call = self._clear_pending_locked()
            self._active_turn_id = None
            self._context_percent = None
            self._active_compaction_item_ids.clear()
        if cancelled_call is not None:
            cancelled_call.cancel()

    def _schedule_if_ready_locked(self) -> tuple[ContextThresholdSteerAttempt, DeferredCall] | None:
        if (
            self._closed
            or not self._dispatch_ready
            or self._root_thread_id is None
            or self._active_turn_id is None
            or self._context_percent is None
            or self._context_percent < self._threshold_percent
            or not self._threshold_armed
            or self._active_compaction_item_ids
            or self._pending_attempt is not None
        ):
            return None
        self._generation += 1
        generation = self._generation
        attempt = ContextThresholdSteerAttempt(
            self._root_thread_id,
            self._active_turn_id,
            self._context_percent,
            self._threshold_percent,
            f"rodex:context-threshold:{self._root_thread_id}:{self._active_turn_id}:{generation}",
        )
        deferred_call = self._deferred_call_factory(0.0, lambda: self._dispatch_pending(generation))
        self._threshold_armed = False
        self._pending_attempt = attempt
        self._pending_call = deferred_call
        return attempt, deferred_call

    def _start_scheduled(
        self,
        scheduled: tuple[ContextThresholdSteerAttempt, DeferredCall] | None,
    ) -> None:
        if scheduled is None:
            return
        attempt, deferred_call = scheduled
        self._safe_log(
            "observed "
            f"threadId={attempt.thread_id} turnId={attempt.turn_id} "
            f"contextPercent={attempt.context_percent:g} thresholdPercent={attempt.threshold_percent:g} "
            f"dispatchId={attempt.dispatch_id}"
        )
        try:
            deferred_call.start()
        except Exception as error:
            with self._lock:
                if self._pending_call is deferred_call:
                    self._clear_pending_locked()
                    self._threshold_armed = True
            self._safe_log(f"scheduleFailed errorType={type(error).__name__}")

    def _dispatch_pending(self, generation: int) -> None:
        with self._lock:
            if (
                self._closed
                or generation != self._generation
                or self._pending_attempt is None
                or self._pending_call is None
            ):
                return
            attempt = self._pending_attempt
            self._pending_call = None
        request = InteractionRequest(
            "main",
            InteractionOperation.STEER,
            CONTEXT_STEER_SOURCE,
            text=self._message,
            dispatch_id=attempt.dispatch_id,
            expected_turn_id=attempt.turn_id,
            expected_thread_id=attempt.thread_id,
        )
        with self._lock:
            if self._closed or generation != self._generation or self._pending_attempt is not attempt:
                return
            self._dispatch_requests[request.request_id] = (generation, attempt)
        result: InteractionResult | None = None
        error: Exception | None = None
        try:
            result = self._interaction_pipeline.execute(request)
        except Exception as caught:
            error = caught
        finally:
            with self._lock:
                self._dispatch_requests.pop(request.request_id, None)
                admitted = request.request_id in self._admitted_request_ids
                self._admitted_request_ids.discard(request.request_id)
                if generation == self._generation and self._pending_attempt is attempt:
                    self._clear_pending_locked()
                if admitted and result is not None and result.status is DeliveryStatus.COMPLETED:
                    self._last_completed_dispatch_id = attempt.dispatch_id
        if error is not None:
            self._safe_log(
                f"dispatchFailed threadId={attempt.thread_id} turnId={attempt.turn_id} "
                f"dispatchId={attempt.dispatch_id} errorType={type(error).__name__}"
            )
            return
        assert result is not None
        outcome = "completed" if admitted and result.status is DeliveryStatus.COMPLETED else "rejected"
        self._safe_log(
            f"dispatch{outcome.title()} threadId={attempt.thread_id} turnId={attempt.turn_id} "
            f"dispatchId={attempt.dispatch_id} status={result.status.value}"
        )

    def _clear_pending_locked(
        self,
    ) -> tuple[ContextThresholdSteerAttempt | None, DeferredCall | None]:
        attempt = self._pending_attempt
        deferred_call = self._pending_call
        self._generation += 1
        self._pending_attempt = None
        self._pending_call = None
        return attempt, deferred_call

    def _safe_log(self, message: str) -> None:
        with suppress(Exception):
            self._logger(f"Rodex contextThreshold steering: {message}")


def _turn_id(params: Mapping[str, object]) -> str | None:
    turn = params.get("turn")
    turn_id = turn.get("id") if isinstance(turn, Mapping) else None
    return turn_id if isinstance(turn_id, str) and turn_id else None
