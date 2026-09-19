from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from rodex.interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from rodex.server_overloaded_recovery import (
    SERVER_OVERLOADED_CONTINUATION_TEXT,
    SERVER_OVERLOADED_RECOVERY_SOURCE,
    ServerOverloadedRecoveryController,
)

ROOT_THREAD = "01a0b3b6-4645-7fa1-841d-9e579eeded0e"


@dataclass
class FakeClock:
    monotonic_seconds: float = 0.0
    epoch_seconds: float = 1_789_732_794.0

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def wall_clock(self) -> float:
        return self.epoch_seconds

    def advance(self, seconds: float) -> None:
        self.monotonic_seconds += seconds
        self.epoch_seconds += seconds


class FakeDeferredCall:
    def __init__(self, delay_seconds: float, callback) -> None:
        self.delay_seconds = delay_seconds
        self.callback = callback
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self, *, even_if_cancelled: bool = False) -> None:
        if not self.cancelled or even_if_cancelled:
            self.callback()


class RecoveryHarness:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.deferred_calls: list[FakeDeferredCall] = []
        self.model_requests: list[InteractionRequest] = []
        self.logs: list[str] = []
        self.pipeline = SessionInteractionPipeline()
        self.pipeline.register(
            InteractionTarget(
                "main",
                "runtime",
                frozenset({InteractionOperation.MESSAGE}),
                lambda: True,
                self._deliver_model_request,
                model_thread_id=lambda: ROOT_THREAD,
                binding_identity=lambda: "primary-connection",
            )
        )
        self.pipeline.register(
            InteractionTarget(
                "terminal",
                "runtime",
                frozenset({InteractionOperation.TERMINAL_INPUT}),
                lambda: True,
                lambda _request: InteractionResult(DeliveryStatus.DELIVERED),
            )
        )
        self.controller = ServerOverloadedRecoveryController(
            self.pipeline,
            logger=self.logs.append,
            monotonic=self.clock.monotonic,
            wall_clock=self.clock.wall_clock,
            deferred_call_factory=self._defer,
        )

    def _defer(self, delay_seconds: float, callback) -> FakeDeferredCall:
        deferred = FakeDeferredCall(delay_seconds, callback)
        self.deferred_calls.append(deferred)
        return deferred

    def _deliver_model_request(self, request: InteractionRequest) -> InteractionResult:
        self.model_requests.append(request)
        return InteractionResult(DeliveryStatus.MODEL_TURN_STARTED, value={"turn_id": "continued-turn"})

    def close(self) -> None:
        self.controller.close()


def thread_started(thread_id: str = ROOT_THREAD) -> dict[str, object]:
    return {"method": "thread/started", "params": {"thread": {"id": thread_id}}}


def server_overloaded(turn_id: str, *, thread_id: str = ROOT_THREAD) -> dict[str, object]:
    return {
        "method": "turn/completed",
        "params": {
            "threadId": thread_id,
            "turn": {
                "id": turn_id,
                "status": "failed",
                "items": [],
                "error": {
                    "message": "Selected model is at capacity. Please try a different model.",
                    "codexErrorInfo": "serverOverloaded",
                },
            },
        },
    }


def test_server_overloaded_starts_continue_through_the_shared_pipeline_after_initial_delay() -> None:
    harness = RecoveryHarness()
    try:
        harness.controller.observe_protocol_event(thread_started())
        harness.controller.observe_protocol_event(server_overloaded("failed-turn-1"))

        snapshot = harness.controller.snapshot
        assert snapshot.root_thread_id == ROOT_THREAD
        assert snapshot.server_overloaded_count == 1
        assert snapshot.last_server_overloaded_at_utc == "2026-09-18T11:59:54.000Z"
        assert snapshot.pending_delay_seconds == 30
        assert harness.model_requests == []
        assert harness.deferred_calls[0].started
        assert harness.deferred_calls[0].delay_seconds == 30

        harness.deferred_calls[0].fire()

        assert len(harness.model_requests) == 1
        request = harness.model_requests[0]
        assert request.operation == InteractionOperation.MESSAGE
        assert request.source == SERVER_OVERLOADED_RECOVERY_SOURCE
        assert request.text == SERVER_OVERLOADED_CONTINUATION_TEXT
        assert request.start_model_turn is True
        assert request.expected_thread_id == ROOT_THREAD
        assert request.dispatch_id == f"rodex:server-overloaded:{ROOT_THREAD}:failed-turn-1"
        assert harness.controller.snapshot.pending_failed_turn_id is None
        assert "serverOverloadedCount=1 retryDelaySeconds=30" in harness.logs[0]
        assert "dispatchStarted" in harness.logs[-1]
    finally:
        harness.close()


@pytest.mark.parametrize("cancel", ["input", "disconnect", "close"])
def test_recovery_cancellation_survives_a_blocked_hook(cancel):
    harness = RecoveryHarness()
    entered, release = Event(), Event()

    def hook(request):
        if request.source == SERVER_OVERLOADED_RECOVERY_SOURCE:
            entered.set()
            assert release.wait(2)
        return request

    harness.pipeline._hooks = (hook,)
    original = harness._deliver_model_request

    def admitted_delivery(request):
        # Production invokes this at the final control-transport admission seam.
        harness.controller.admit_dispatch(request)
        return original(request)

    from dataclasses import replace

    target = harness.pipeline._targets["main"]
    harness.pipeline.unregister(target)
    harness.pipeline.register(replace(target, deliver=admitted_delivery))
    harness.controller.bind_root_thread(ROOT_THREAD)
    harness.controller.observe_protocol_event(server_overloaded("blocked"))
    thread = Thread(target=harness.deferred_calls[-1].fire)
    thread.start()
    try:
        assert entered.wait(1)
        if cancel == "input":
            harness.pipeline.execute(
                InteractionRequest("terminal", InteractionOperation.TERMINAL_INPUT, "terminal-gateway", payload=b"x")
            )
        elif cancel == "disconnect":
            harness.controller.reset_after_disconnect()
        else:
            harness.controller.close()
        release.set()
        thread.join(1)
        assert not thread.is_alive()
        assert harness.model_requests == []
        assert harness.pipeline.records[-1].status == DeliveryStatus.REJECTED
    finally:
        release.set()
        thread.join(2)
        harness.close()


@pytest.mark.parametrize("stage", ["session-lock", "transport", "after-admission", "after-admission-indeterminate"])
def test_recovery_admission_through_real_exact_coordinator_and_control(tmp_path, monkeypatch, stage):
    from dataclasses import replace

    from test_rodex_control import FakeWebSocket

    from rodex.control import CodexControlClient
    from rodex.exact_turn_mutation import ExactTurnMutationCoordinator, ExactTurnTarget

    harness = RecoveryHarness()
    entered, release = Event(), Event()
    client = CodexControlClient(request_id_factory=lambda: 42)
    protocol = FakeWebSocket(
        responses=[] if stage.endswith("indeterminate") else [{"id": 42, "result": {"turn": {"id": "continued"}}}]
    )
    events = FakeWebSocket(responses=[{"method": "rodex/event-stream/ready", "params": {"activeTurns": {}}}])
    control = SimpleNamespace(codex_session_id=ROOT_THREAD, runtime_id="runtime")
    target = ExactTurnTarget("session", 1, "session", object(), control)
    coordinator = ExactTurnMutationCoordinator(tmp_path / "unused.sqlite3", object(), client)

    @contextmanager
    def locked(_selector):
        if stage == "session-lock":
            entered.set()
            assert release.wait(2)
        yield object()

    @contextmanager
    def open_protocol(*args, **kwargs):
        if stage == "transport":
            entered.set()
            assert release.wait(2)
        yield protocol

    monkeypatch.setattr(coordinator, "_locked_selector", locked)
    monkeypatch.setattr(coordinator, "_resolve_target", lambda _: target)
    monkeypatch.setattr(coordinator, "_revalidator", lambda _: lambda: None)
    monkeypatch.setattr(client, "_open_protocol", open_protocol)
    monkeypatch.setattr(client, "_open_events", lambda *args, **kwargs: events)
    monkeypatch.setattr(
        client,
        "_verify_and_read_thread",
        lambda *args, **kwargs: {
            "id": ROOT_THREAD,
            "sessionId": ROOT_THREAD,
            "cwd": str(tmp_path),
            "status": {"type": "idle"},
            "turns": [],
            "canAcceptDirectInput": True,
        },
    )
    original_send = protocol.send

    def send(message):
        if stage.startswith("after-admission"):
            harness.controller.close()
        original_send(message)

    protocol.send = send

    def deliver(request):
        _, dispatch = coordinator.start(
            "session",
            request.text,
            dispatch_id=request.dispatch_id,
            before_dispatch=lambda: harness.controller.admit_dispatch(request),
        )
        return InteractionResult(DeliveryStatus.MODEL_TURN_STARTED, value=dispatch)

    original_target = harness.pipeline._targets["main"]
    harness.pipeline.unregister(original_target)
    harness.pipeline.register(replace(original_target, deliver=deliver))
    harness.controller.bind_root_thread(ROOT_THREAD)
    harness.controller.observe_protocol_event(server_overloaded("boundary"))
    thread = Thread(target=harness.deferred_calls[-1].fire)
    thread.start()
    try:
        if not stage.startswith("after-admission"):
            assert entered.wait(1)
            harness.controller.reset_after_disconnect()
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        sent = [frame for frame in protocol.sent if frame["method"] == "turn/start"]
        expected_status = (
            DeliveryStatus.INDETERMINATE
            if stage.endswith("indeterminate")
            else DeliveryStatus.MODEL_TURN_STARTED
            if stage == "after-admission"
            else DeliveryStatus.REJECTED
        )
        assert len(sent) == int(stage.startswith("after-admission"))
        assert harness.pipeline.records[-1].status == expected_status
    finally:
        release.set()
        thread.join(2)
        harness.close()


def test_backoff_doubles_within_five_minutes_caps_at_sixteen_minutes_and_then_resets() -> None:
    harness = RecoveryHarness()
    try:
        harness.controller.bind_root_thread(ROOT_THREAD)
        expected_delays = [30, 60, 120, 240, 480, 960, 960, 960]
        for index, expected_delay in enumerate(expected_delays, 1):
            harness.controller.observe_protocol_event(server_overloaded(f"failed-turn-{index}"))
            assert harness.controller.snapshot.server_overloaded_count == index
            assert harness.controller.snapshot.pending_delay_seconds == expected_delay
            harness.clock.advance(10)

        assert [call.delay_seconds for call in harness.deferred_calls] == expected_delays
        assert all(call.cancelled for call in harness.deferred_calls[:-1])

        harness.clock.advance(301)
        harness.controller.observe_protocol_event(server_overloaded("failed-turn-after-window"))

        assert harness.controller.snapshot.server_overloaded_count == 1
        assert harness.controller.snapshot.pending_delay_seconds == 30
    finally:
        harness.close()


def test_exactly_five_minutes_continues_backoff_but_more_than_five_minutes_resets() -> None:
    harness = RecoveryHarness()
    try:
        harness.controller.bind_root_thread(ROOT_THREAD)
        harness.controller.observe_protocol_event(server_overloaded("failed-turn-1"))
        harness.clock.advance(300)
        harness.controller.observe_protocol_event(server_overloaded("failed-turn-2"))
        assert harness.controller.snapshot.server_overloaded_count == 2
        assert harness.controller.snapshot.pending_delay_seconds == 60

        harness.clock.advance(300.001)
        harness.controller.observe_protocol_event(server_overloaded("failed-turn-3"))
        assert harness.controller.snapshot.server_overloaded_count == 1
        assert harness.controller.snapshot.pending_delay_seconds == 30
    finally:
        harness.close()


def test_terminal_input_cancels_pending_continue_through_pipeline_outcome_observation() -> None:
    harness = RecoveryHarness()
    try:
        harness.controller.bind_root_thread(ROOT_THREAD)
        harness.controller.observe_protocol_event(server_overloaded("failed-turn-1"))
        pending = harness.deferred_calls[-1]

        result = harness.pipeline.execute(
            InteractionRequest(
                "terminal",
                InteractionOperation.TERMINAL_INPUT,
                "terminal-gateway",
                payload=b"user typed",
            )
        )

        assert result.accepted
        assert pending.cancelled
        assert harness.controller.snapshot.pending_failed_turn_id is None
        pending.fire(even_if_cancelled=True)
        assert harness.model_requests == []
        assert "reason=user_input_observed" in harness.logs[-1]
    finally:
        harness.close()


@pytest.mark.parametrize(
    "event",
    [
        {
            "method": "error",
            "params": {
                "threadId": ROOT_THREAD,
                "turnId": "failed-turn",
                "willRetry": False,
                "error": {"codexErrorInfo": "serverOverloaded"},
            },
        },
        {
            "method": "warning",
            "params": {"threadId": ROOT_THREAD, "message": "Selected model is at capacity."},
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": ROOT_THREAD,
                "turn": {
                    "id": "failed-turn",
                    "status": "failed",
                    "error": {"message": "Selected model is at capacity.", "codexErrorInfo": "other"},
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": ROOT_THREAD,
                "turn": {
                    "id": "completed-turn",
                    "status": "completed",
                    "error": {"codexErrorInfo": "serverOverloaded"},
                },
            },
        },
        server_overloaded("child-failed-turn", thread_id="child-thread"),
    ],
)
def test_only_terminal_root_turn_server_overloaded_code_triggers_recovery(event: dict[str, object]) -> None:
    harness = RecoveryHarness()
    try:
        harness.controller.bind_root_thread(ROOT_THREAD)
        harness.controller.observe_protocol_event(event)
        assert harness.deferred_calls == []
        assert harness.controller.snapshot.server_overloaded_count == 0
    finally:
        harness.close()


def test_duplicate_terminal_event_is_idempotent_and_disconnect_cancels_without_resetting_backoff() -> None:
    harness = RecoveryHarness()
    try:
        harness.controller.bind_root_thread(ROOT_THREAD)
        event = server_overloaded("failed-turn-1")
        harness.controller.observe_protocol_event(event)
        harness.controller.observe_protocol_event(event)

        assert len(harness.deferred_calls) == 1
        assert harness.controller.snapshot.server_overloaded_count == 1
        harness.controller.reset_after_disconnect()
        assert harness.deferred_calls[0].cancelled
        assert harness.controller.snapshot.server_overloaded_count == 1
        assert harness.controller.snapshot.pending_failed_turn_id is None
    finally:
        harness.close()
