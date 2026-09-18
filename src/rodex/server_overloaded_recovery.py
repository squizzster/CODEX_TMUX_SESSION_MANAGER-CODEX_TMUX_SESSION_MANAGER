"""Native recovery for terminal Codex turns rejected by model capacity."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock, Timer
from typing import Final, Protocol

from .app_server_contract import CODEX_APP_SERVER
from .interaction_pipeline import (
    InteractionOperation,
    InteractionRecord,
    InteractionRequest,
    InteractionResult,
    SessionInteractionPipeline,
)

SERVER_OVERLOADED_ERROR_CODE: Final = "serverOverloaded"
SERVER_OVERLOADED_CONTINUATION_TEXT: Final = "Continue..."
SERVER_OVERLOADED_INITIAL_DELAY_SECONDS: Final = 30
SERVER_OVERLOADED_RESET_WINDOW_SECONDS: Final = 5 * 60
SERVER_OVERLOADED_MAX_DELAY_SECONDS: Final = 16 * 60
SERVER_OVERLOADED_OBSERVED_TURN_LIMIT: Final = 256
SERVER_OVERLOADED_RECOVERY_SOURCE: Final = "server-overloaded-recovery"


class DeferredCall(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


DeferredCallFactory = Callable[[float, Callable[[], None]], DeferredCall]
RecoveryLogger = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ServerOverloadedRecoveryAttempt:
    thread_id: str
    failed_turn_id: str
    server_overloaded_count: int
    delay_seconds: int
    last_server_overloaded_at_utc: str
    dispatch_id: str


@dataclass(frozen=True, slots=True)
class ServerOverloadedRecoverySnapshot:
    root_thread_id: str | None
    server_overloaded_count: int
    last_server_overloaded_at_utc: str | None
    pending_delay_seconds: int | None
    pending_failed_turn_id: str | None


def _timer_factory(delay_seconds: float, callback: Callable[[], None]) -> Timer:
    timer = Timer(delay_seconds, callback)
    timer.daemon = True
    return timer


class ServerOverloadedRecoveryController:
    """Schedule one guarded continuation through the shared interaction pipeline."""

    name: Final = "server-overloaded-recovery"

    def __init__(
        self,
        interaction_pipeline: SessionInteractionPipeline,
        *,
        logger: RecoveryLogger = lambda _message: None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        deferred_call_factory: DeferredCallFactory = _timer_factory,
    ) -> None:
        self._interaction_pipeline = interaction_pipeline
        self._logger = logger
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._deferred_call_factory = deferred_call_factory
        self._lock = Lock()
        self._root_thread_id: str | None = None
        self._server_overloaded_count = 0
        self._last_server_overloaded_monotonic: float | None = None
        self._last_server_overloaded_at_utc: str | None = None
        self._observed_terminal_turns: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._pending_attempt: ServerOverloadedRecoveryAttempt | None = None
        self._pending_call: DeferredCall | None = None
        self._generation = 0
        self._closed = False
        self._unsubscribe_outcomes = interaction_pipeline.subscribe_outcomes(self.observe_interaction_outcome)

    @property
    def snapshot(self) -> ServerOverloadedRecoverySnapshot:
        with self._lock:
            pending = self._pending_attempt
            return ServerOverloadedRecoverySnapshot(
                self._root_thread_id,
                self._server_overloaded_count,
                self._last_server_overloaded_at_utc,
                None if pending is None else pending.delay_seconds,
                None if pending is None else pending.failed_turn_id,
            )

    def bind_root_thread(self, thread_id: str) -> None:
        """Bind recovery to the exact registered main thread, never a child thread."""
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("server-overloaded recovery requires a root thread identity")
        cancelled: ServerOverloadedRecoveryAttempt | None = None
        cancelled_call: DeferredCall | None = None
        with self._lock:
            if self._closed or self._root_thread_id == thread_id:
                return
            if self._root_thread_id is not None:
                cancelled, cancelled_call = self._clear_pending_locked()
                self._server_overloaded_count = 0
                self._last_server_overloaded_monotonic = None
                self._last_server_overloaded_at_utc = None
                self._observed_terminal_turns.clear()
            self._root_thread_id = thread_id
        if cancelled_call is not None:
            cancelled_call.cancel()
        if cancelled is not None:
            self._log_cancelled(cancelled, "root thread changed")

    def observe_protocol_event(self, event: Mapping[str, object] | None) -> None:
        """Consume one decoded primary App Server event without delaying its transport."""
        if event is None:
            return
        if event.get("method") == CODEX_APP_SERVER.thread_started_method:
            self._bind_first_started_thread(event)
            return
        terminal_failure = self._server_overloaded_terminal_failure(event)
        if terminal_failure is None:
            return
        thread_id, failed_turn_id = terminal_failure
        self._schedule_recovery(thread_id, failed_turn_id)

    def observe_interaction_outcome(self, record: InteractionRecord) -> None:
        """Cancel pending recovery when the owned terminal reports real keyboard input."""
        if record.operation != InteractionOperation.TERMINAL_INPUT or record.source != "terminal-gateway":
            return
        self._cancel_pending("user input observed")

    def reset_after_disconnect(self) -> None:
        """Cancel connection-bound work while retaining this runtime's backoff history."""
        self._cancel_pending("primary connection disconnected")

    def close(self) -> None:
        cancelled: ServerOverloadedRecoveryAttempt | None = None
        cancelled_call: DeferredCall | None = None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            cancelled, cancelled_call = self._clear_pending_locked()
        self._unsubscribe_outcomes()
        if cancelled_call is not None:
            cancelled_call.cancel()
        if cancelled is not None:
            self._log_cancelled(cancelled, "runtime closed")

    def _bind_first_started_thread(self, event: Mapping[str, object]) -> None:
        params = event.get("params")
        thread = params.get("thread") if isinstance(params, Mapping) else None
        thread_id = thread.get("id") if isinstance(thread, Mapping) else None
        if not isinstance(thread_id, str) or not thread_id:
            return
        with self._lock:
            should_bind = not self._closed and self._root_thread_id is None
        if should_bind:
            self.bind_root_thread(thread_id)

    def _server_overloaded_terminal_failure(self, event: Mapping[str, object]) -> tuple[str, str] | None:
        if event.get("method") != CODEX_APP_SERVER.turn_completed_method:
            return None
        params = event.get("params")
        if not isinstance(params, Mapping):
            return None
        thread_id = params.get("threadId")
        turn = params.get("turn")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(turn, Mapping):
            return None
        turn_id = turn.get("id")
        error = turn.get("error")
        if (
            not isinstance(turn_id, str)
            or not turn_id
            or turn.get("status") != "failed"
            or not isinstance(error, Mapping)
            or error.get("codexErrorInfo") != SERVER_OVERLOADED_ERROR_CODE
        ):
            return None
        with self._lock:
            if self._closed or thread_id != self._root_thread_id:
                return None
        return thread_id, turn_id

    def _schedule_recovery(self, thread_id: str, failed_turn_id: str) -> None:
        observed_monotonic = self._monotonic()
        observed_at_utc = _utc_timestamp(self._wall_clock())
        previous_call: DeferredCall | None = None
        with self._lock:
            if self._closed or thread_id != self._root_thread_id:
                return
            turn_key = (thread_id, failed_turn_id)
            if turn_key in self._observed_terminal_turns:
                return
            self._observed_terminal_turns[turn_key] = None
            if len(self._observed_terminal_turns) > SERVER_OVERLOADED_OBSERVED_TURN_LIMIT:
                self._observed_terminal_turns.popitem(last=False)

            since_previous = (
                None
                if self._last_server_overloaded_monotonic is None
                else observed_monotonic - self._last_server_overloaded_monotonic
            )
            if since_previous is not None and 0 <= since_previous <= SERVER_OVERLOADED_RESET_WINDOW_SECONDS:
                self._server_overloaded_count += 1
            else:
                self._server_overloaded_count = 1
            self._last_server_overloaded_monotonic = observed_monotonic
            self._last_server_overloaded_at_utc = observed_at_utc
            delay_seconds = _recovery_delay_seconds(self._server_overloaded_count)

            previous_call = self._pending_call
            self._generation += 1
            generation = self._generation
            attempt = ServerOverloadedRecoveryAttempt(
                thread_id,
                failed_turn_id,
                self._server_overloaded_count,
                delay_seconds,
                observed_at_utc,
                f"rodex:server-overloaded:{thread_id}:{failed_turn_id}",
            )
            deferred_call = self._deferred_call_factory(
                delay_seconds,
                lambda: self._dispatch_pending(generation),
            )
            self._pending_attempt = attempt
            self._pending_call = deferred_call

        if previous_call is not None:
            previous_call.cancel()
        self._safe_log(
            "observed "
            f"lastServerOverloadedAt={attempt.last_server_overloaded_at_utc} "
            f"threadId={attempt.thread_id} failedTurnId={attempt.failed_turn_id} "
            f"serverOverloadedCount={attempt.server_overloaded_count} "
            f"retryDelaySeconds={attempt.delay_seconds}"
        )
        try:
            deferred_call.start()
        except Exception as error:
            with self._lock:
                if self._pending_call is deferred_call:
                    self._generation += 1
                    self._pending_attempt = None
                    self._pending_call = None
            self._safe_log(f"scheduleFailed error={_bounded_log_value(error)}")

    def _dispatch_pending(self, generation: int) -> None:
        with self._lock:
            if self._closed or generation != self._generation or self._pending_attempt is None:
                return
            attempt = self._pending_attempt
            self._pending_attempt = None
            self._pending_call = None
        request = InteractionRequest(
            "main",
            InteractionOperation.MESSAGE,
            SERVER_OVERLOADED_RECOVERY_SOURCE,
            text=SERVER_OVERLOADED_CONTINUATION_TEXT,
            start_model_turn=True,
            dispatch_id=attempt.dispatch_id,
            expected_thread_id=attempt.thread_id,
        )
        try:
            result = self._interaction_pipeline.execute(request)
        except Exception as error:
            self._safe_log(
                f"dispatchFailed threadId={attempt.thread_id} failedTurnId={attempt.failed_turn_id} "
                f"error={_bounded_log_value(error)}"
            )
            return
        self._log_dispatch_result(attempt, result)

    def _log_dispatch_result(self, attempt: ServerOverloadedRecoveryAttempt, result: InteractionResult) -> None:
        outcome = "started" if result.accepted else "rejected"
        detail = _bounded_log_value(result.detail) if result.detail else "none"
        self._safe_log(
            f"dispatch{outcome.title()} threadId={attempt.thread_id} failedTurnId={attempt.failed_turn_id} "
            f"dispatchId={attempt.dispatch_id} status={result.status.value} detail={detail}"
        )

    def _cancel_pending(self, reason: str) -> None:
        with self._lock:
            if self._closed or self._pending_attempt is None:
                return
            cancelled, cancelled_call = self._clear_pending_locked()
        if cancelled_call is not None:
            cancelled_call.cancel()
        assert cancelled is not None
        self._log_cancelled(cancelled, reason)

    def _clear_pending_locked(self) -> tuple[ServerOverloadedRecoveryAttempt | None, DeferredCall | None]:
        cancelled = self._pending_attempt
        cancelled_call = self._pending_call
        self._generation += 1
        self._pending_attempt = None
        self._pending_call = None
        return cancelled, cancelled_call

    def _log_cancelled(self, attempt: ServerOverloadedRecoveryAttempt, reason: str) -> None:
        self._safe_log(
            f"cancelled threadId={attempt.thread_id} failedTurnId={attempt.failed_turn_id} "
            f"reason={reason.replace(' ', '_')}"
        )

    def _safe_log(self, message: str) -> None:
        with suppress(Exception):
            self._logger(f"Rodex serverOverloaded recovery: {message}")


def _recovery_delay_seconds(server_overloaded_count: int) -> int:
    doublings_before_cap = (
        SERVER_OVERLOADED_MAX_DELAY_SECONDS // SERVER_OVERLOADED_INITIAL_DELAY_SECONDS
    ).bit_length() - 1
    exponent = min(max(server_overloaded_count - 1, 0), doublings_before_cap)
    return min(SERVER_OVERLOADED_INITIAL_DELAY_SECONDS * (2**exponent), SERVER_OVERLOADED_MAX_DELAY_SECONDS)


def _utc_timestamp(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_log_value(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")[:240]
