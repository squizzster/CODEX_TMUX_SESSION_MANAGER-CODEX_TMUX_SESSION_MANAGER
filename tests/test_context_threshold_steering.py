from __future__ import annotations

from dataclasses import replace
from threading import Event, Thread

import pytest

from rodex.context_threshold_steering import (
    CONTEXT_STEER_MESSAGE,
    CONTEXT_STEER_SOURCE,
    CONTEXT_STEER_THRESHOLD_PERCENT,
    ContextThresholdSteeringController,
)
from rodex.interaction_pipeline import (
    DeliveryStatus,
    InteractionDeliveryIndeterminate,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)

ROOT_THREAD = "01a0b3b6-4645-7fa1-841d-9e579eeded0e"


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


class SteeringHarness:
    def __init__(self, *, bind: bool = True) -> None:
        self.pipeline = SessionInteractionPipeline()
        self.deferred_calls: list[FakeDeferredCall] = []
        self.requests: list[InteractionRequest] = []
        self.logs: list[str] = []
        self.controller = ContextThresholdSteeringController(
            self.pipeline,
            logger=self.logs.append,
            deferred_call_factory=self._defer,
        )
        self.target = InteractionTarget(
            "main",
            "runtime",
            frozenset({InteractionOperation.STEER}),
            lambda: True,
            self._deliver,
            model_thread_id=lambda: ROOT_THREAD,
            binding_identity=lambda: "primary-connection",
        )
        self.pipeline.register(self.target)
        if bind:
            self.controller.bind_root_thread(ROOT_THREAD)

    def _defer(self, delay_seconds: float, callback) -> FakeDeferredCall:
        deferred = FakeDeferredCall(delay_seconds, callback)
        self.deferred_calls.append(deferred)
        return deferred

    def _deliver(self, request: InteractionRequest) -> InteractionResult:
        self.controller.admit_dispatch(request)
        self.requests.append(request)
        return InteractionResult(DeliveryStatus.COMPLETED, value={"turn_id": request.expected_turn_id})

    def start_turn(self, turn_id: str = "turn-1") -> None:
        self.controller.observe_protocol_event(turn_started(turn_id))

    def close(self) -> None:
        self.controller.close()


def thread_started(thread_id: str = ROOT_THREAD) -> dict[str, object]:
    return {"method": "thread/started", "params": {"thread": {"id": thread_id}}}


def turn_started(turn_id: str, *, thread_id: str = ROOT_THREAD) -> dict[str, object]:
    return {
        "method": "turn/started",
        "params": {"threadId": thread_id, "turn": {"id": turn_id}},
    }


def turn_completed(turn_id: str, *, thread_id: str = ROOT_THREAD) -> dict[str, object]:
    return {
        "method": "turn/completed",
        "params": {"threadId": thread_id, "turn": {"id": turn_id}},
    }


def compaction(method: str, item_id: str = "compact-1") -> dict[str, object]:
    return {
        "method": method,
        "params": {
            "threadId": ROOT_THREAD,
            "turnId": "turn-1",
            "item": {"type": "contextCompaction", "id": item_id},
        },
    }


def test_exact_seventy_percent_steers_once_with_the_configured_message() -> None:
    harness = SteeringHarness()
    try:
        harness.start_turn()
        harness.controller.observe_context_percent(ROOT_THREAD, 69.999)
        assert harness.deferred_calls == []

        harness.controller.observe_context_percent(ROOT_THREAD, 70.0)

        assert len(harness.deferred_calls) == 1
        assert harness.deferred_calls[0].started
        assert harness.deferred_calls[0].delay_seconds == 0
        harness.deferred_calls[0].fire()
        assert len(harness.requests) == 1
        request = harness.requests[0]
        assert request.operation is InteractionOperation.STEER
        assert request.source == CONTEXT_STEER_SOURCE
        assert request.text == CONTEXT_STEER_MESSAGE
        assert request.start_model_turn is False
        assert request.expected_thread_id == ROOT_THREAD
        assert request.expected_turn_id == "turn-1"
        assert request.dispatch_id is not None
        assert request.dispatch_id.startswith(f"rodex:context-threshold:{ROOT_THREAD}:turn-1:")
        assert harness.controller.snapshot.last_completed_dispatch_id == request.dispatch_id

        harness.controller.observe_context_percent(ROOT_THREAD, 90.0)
        assert len(harness.deferred_calls) == 1
        assert len(harness.requests) == 1
    finally:
        harness.close()


def test_a_below_threshold_observation_rearms_the_next_crossing() -> None:
    harness = SteeringHarness()
    try:
        harness.start_turn()
        harness.controller.observe_context_percent(ROOT_THREAD, CONTEXT_STEER_THRESHOLD_PERCENT)
        harness.deferred_calls[-1].fire()
        first_dispatch_id = harness.requests[-1].dispatch_id

        harness.controller.observe_context_percent(ROOT_THREAD, 69.0)
        harness.controller.observe_context_percent(ROOT_THREAD, 70.0)
        harness.deferred_calls[-1].fire()

        assert len(harness.requests) == 2
        assert harness.requests[-1].dispatch_id != first_dispatch_id
    finally:
        harness.close()


def test_high_context_waits_for_registration_and_an_exact_active_turn() -> None:
    harness = SteeringHarness(bind=False)
    try:
        harness.controller.observe_protocol_event(thread_started())
        harness.controller.observe_context_percent(ROOT_THREAD, 75.0)
        assert harness.deferred_calls == []

        harness.controller.bind_root_thread(ROOT_THREAD)
        assert harness.deferred_calls == []

        harness.start_turn("turn-ready")
        assert len(harness.deferred_calls) == 1
        harness.deferred_calls[0].fire()
        assert harness.requests[0].expected_turn_id == "turn-ready"
    finally:
        harness.close()


def test_child_thread_context_and_turns_cannot_trigger_main_steering() -> None:
    harness = SteeringHarness()
    try:
        harness.controller.observe_protocol_event(turn_started("child-turn", thread_id="child-thread"))
        harness.controller.observe_context_percent("child-thread", 90.0)
        assert harness.deferred_calls == []

        harness.start_turn("root-turn")
        harness.controller.observe_context_percent(ROOT_THREAD, 70.0)
        harness.deferred_calls[0].fire()
        assert [request.expected_turn_id for request in harness.requests] == ["root-turn"]
    finally:
        harness.close()


def test_compaction_cancels_pending_work_and_requires_a_fresh_crossing() -> None:
    harness = SteeringHarness()
    try:
        harness.start_turn()
        harness.controller.observe_context_percent(ROOT_THREAD, 70.0)
        pending = harness.deferred_calls[-1]

        harness.controller.observe_protocol_event(compaction("item/started"))
        assert pending.cancelled
        pending.fire(even_if_cancelled=True)
        assert harness.requests == []

        harness.controller.observe_context_percent(ROOT_THREAD, 20.0)
        harness.controller.observe_protocol_event(compaction("item/completed"))
        harness.controller.observe_context_percent(ROOT_THREAD, 75.0)
        assert len(harness.deferred_calls) == 1

        harness.controller.observe_context_percent(ROOT_THREAD, 20.0)
        harness.controller.observe_context_percent(ROOT_THREAD, 70.0)
        assert len(harness.deferred_calls) == 2
        harness.deferred_calls[-1].fire()
        assert len(harness.requests) == 1
    finally:
        harness.close()


def test_turn_completion_cancels_a_not_yet_admitted_steer() -> None:
    harness = SteeringHarness()
    try:
        harness.start_turn()
        harness.controller.observe_context_percent(ROOT_THREAD, 70.0)
        pending = harness.deferred_calls[-1]

        harness.controller.observe_protocol_event(turn_completed("turn-1"))

        assert pending.cancelled
        pending.fire(even_if_cancelled=True)
        assert harness.requests == []
    finally:
        harness.close()


def test_a_replacement_active_turn_cancels_the_old_turns_pending_steer() -> None:
    harness = SteeringHarness()
    try:
        harness.start_turn("old-turn")
        harness.controller.observe_context_percent(ROOT_THREAD, 70.0)
        pending = harness.deferred_calls[-1]

        harness.start_turn("replacement-turn")

        assert pending.cancelled
        pending.fire(even_if_cancelled=True)
        assert harness.requests == []
        assert harness.controller.snapshot.active_turn_id == "replacement-turn"
    finally:
        harness.close()


def test_context_drop_while_a_hook_waits_rejects_at_final_admission() -> None:
    harness = SteeringHarness()
    entered, release = Event(), Event()

    def block_context_steer(request: InteractionRequest) -> InteractionRequest:
        if request.source == CONTEXT_STEER_SOURCE:
            entered.set()
            assert release.wait(2)
        return request

    harness.pipeline._hooks = (block_context_steer,)
    harness.start_turn()
    harness.controller.observe_context_percent(ROOT_THREAD, 70.0)
    worker = Thread(target=harness.deferred_calls[-1].fire)
    worker.start()
    try:
        assert entered.wait(1)
        harness.controller.observe_context_percent(ROOT_THREAD, 69.0)
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        assert harness.requests == []
        assert harness.pipeline.records[-1].status is DeliveryStatus.REJECTED
    finally:
        release.set()
        worker.join(2)
        harness.close()


def test_indeterminate_delivery_is_not_recorded_as_success_or_retried() -> None:
    harness = SteeringHarness()

    def indeterminate(request: InteractionRequest) -> InteractionResult:
        harness.controller.admit_dispatch(request)
        raise InteractionDeliveryIndeterminate("steer delivery may have happened")

    original_target = harness.pipeline._targets["main"]
    harness.pipeline.unregister(original_target)
    harness.pipeline.register(replace(original_target, deliver=indeterminate))
    try:
        harness.start_turn()
        harness.controller.observe_context_percent(ROOT_THREAD, 70.0)
        harness.deferred_calls[-1].fire()

        assert harness.controller.snapshot.last_completed_dispatch_id is None
        assert harness.pipeline.records[-1].status is DeliveryStatus.INDETERMINATE
        harness.controller.observe_context_percent(ROOT_THREAD, 90.0)
        assert len(harness.deferred_calls) == 1
    finally:
        harness.close()


@pytest.mark.parametrize("threshold", [True, 0, -1, float("inf"), float("nan"), 101])
def test_threshold_configuration_requires_a_finite_percentage(threshold: object) -> None:
    with pytest.raises(ValueError, match="threshold"):
        ContextThresholdSteeringController(SessionInteractionPipeline(), threshold_percent=threshold)  # type: ignore[arg-type]


def test_message_configuration_cannot_be_empty() -> None:
    with pytest.raises(ValueError, match="message"):
        ContextThresholdSteeringController(SessionInteractionPipeline(), message="  ")
