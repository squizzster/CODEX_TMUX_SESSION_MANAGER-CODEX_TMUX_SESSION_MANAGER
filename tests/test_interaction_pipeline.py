from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from rodex.control import PromptDispatch, RodexControlError, RodexDispatchIndeterminateError
from rodex.exact_turn_mutation import ExactTurnMutationCoordinator
from rodex.interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRejected,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)


def target_binding(name, delivered, *, thread=None, exists=lambda: True, opener=None, binding=lambda: None):
    return InteractionTarget(
        name,
        "runtime-1",
        frozenset(InteractionOperation),
        exists,
        lambda request: (
            delivered.append(request)
            or InteractionResult(
                DeliveryStatus.MODEL_TURN_STARTED if request.start_model_turn else DeliveryStatus.DELIVERED,
            )
        ),
        model_thread_id=lambda: thread,
        open=opener,
        binding_identity=binding,
    )


@pytest.mark.parametrize("name", ["main", "agent:researcher"])
@pytest.mark.parametrize("start_model_turn", [False, True])
def test_same_message_contract_for_every_target(name, start_model_turn):
    delivered = []
    pipeline = SessionInteractionPipeline()
    pipeline.register(target_binding(name, delivered, thread="thread-1"))
    result = pipeline.send_message(target=name, text="hello", start_model_turn=start_model_turn)
    assert result.accepted
    assert delivered[0].text == "hello"
    assert delivered[0].start_model_turn is start_model_turn
    assert pipeline.records[-1].start_model_turn is start_model_turn


def test_observer_without_model_binding_can_display_but_cannot_start_a_turn():
    delivered = []
    pipeline = SessionInteractionPipeline()
    pipeline.register(target_binding("agent-observer", delivered))
    assert pipeline.send_message(target="agent-observer", text="hello").accepted
    result = pipeline.send_message(target="agent-observer", text="hello", start_model_turn=True)
    assert result.status == DeliveryStatus.REJECTED
    assert "unambiguous" in result.detail
    assert len(delivered) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"target": "other"},
        {"source": "other"},
        {"start_model_turn": True},
        {"open_if_missing": True},
        {"dispatch_id": "different"},
        {"operation": InteractionOperation.OPEN},
        {"expected_turn_id": "different"},
    ],
)
def test_hooks_cannot_escalate_intent_or_change_identity(changes):
    delivered = []
    pipeline = SessionInteractionPipeline(hooks=(lambda request: replace(request, **changes),))
    pipeline.register(target_binding("main", delivered, thread="thread"))
    assert pipeline.send_message(target="main", text="hello").status == DeliveryStatus.REJECTED
    assert delivered == []


def test_hooks_transform_in_order_before_delivery_and_outcome_observers_cannot_undo_delivery():
    delivered = []
    seen = []

    def broken_observer(record):
        seen.append(record)
        raise RuntimeError("diagnostics failed")

    pipeline = SessionInteractionPipeline(
        hooks=(
            lambda request: replace(request, text=request.text + " one"),
            lambda request: replace(request, text=request.text + " two"),
        ),
        outcome_observers=(broken_observer,),
    )
    pipeline.register(target_binding("main", delivered))
    assert pipeline.send_message(target="main", text="hello").accepted
    assert delivered[0].text == "hello one two"
    assert seen == list(pipeline.records)
    assert not hasattr(seen[0], "text")


@pytest.mark.parametrize(
    "changed",
    [
        {"id": 2},
        {"method": "turn/start"},
        {"params": {"threadId": "other", "input": [{"type": "text", "text": "hello"}]}},
        {"params": {"threadId": "thread", "input": [{"type": "text", "text": "hello"}], "decision": "accept"}},
    ],
)
def test_protocol_hooks_cannot_change_control_fields(changed):
    frame = {
        "id": 1,
        "method": "turn/steer",
        "params": {
            "threadId": "thread",
            "input": [{"type": "text", "text": "hello"}],
        },
    }
    delivered = []
    pipeline = SessionInteractionPipeline(hooks=(lambda request: replace(request, payload=json.dumps(frame | changed)),))
    pipeline.register(target_binding("wire", delivered))
    result = pipeline.execute(
        InteractionRequest("wire", InteractionOperation.PROTOCOL_INPUT, "tui", payload=json.dumps(frame))
    )
    assert result.status == DeliveryStatus.REJECTED
    assert delivered == []


def test_protocol_text_changes_preserve_envelope_and_no_hook_preserves_exact_bytes():
    frame = b'{ "id":1, "params": {"input":[{"type":"text","text":"hello"}]}}'
    delivered = []
    pipeline = SessionInteractionPipeline()
    pipeline.register(target_binding("wire", delivered))
    assert pipeline.execute(
        InteractionRequest("wire", InteractionOperation.PROTOCOL_INPUT, "tui", payload=frame)
    ).accepted
    assert delivered[0].payload is frame
    modified = SessionInteractionPipeline(
        hooks=(lambda request: replace(request, payload=request.payload.replace(b"hello", b"world")),)
    )
    modified.register(target_binding("wire", delivered))
    assert modified.execute(
        InteractionRequest("wire", InteractionOperation.PROTOCOL_INPUT, "tui", payload=frame)
    ).accepted
    assert b"world" in delivered[-1].payload


def test_unknown_and_missing_targets_never_create_implicitly():
    pipeline = SessionInteractionPipeline()
    assert pipeline.send_message(target="unknown", text="hello", open_if_missing=True).status == DeliveryStatus.REJECTED
    opened = []
    pipeline.register(target_binding("known", [], exists=lambda: False, opener=lambda: opened.append(True)))
    assert pipeline.send_message(target="known", text="hello").status == DeliveryStatus.REJECTED
    assert opened == []


def test_open_if_missing_records_nested_open_and_checks_the_new_binding():
    delivered = []
    exists = False
    pipeline = SessionInteractionPipeline()

    def open_target():
        nonlocal exists
        exists = True
        return InteractionResult(DeliveryStatus.COMPLETED)

    pipeline.register(
        target_binding(
            "pane", delivered, exists=lambda: exists, opener=open_target, binding=lambda: "pane-1" if exists else None
        )
    )
    assert pipeline.send_message(target="pane", text="hello", open_if_missing=True).accepted
    assert delivered[0].expected_binding == "pane-1"


@pytest.mark.parametrize("during_open", [False, True])
def test_replaced_registry_binding_is_rejected_before_effect(during_open):
    delivered = []
    pipeline = None
    original = None

    def replace_binding():
        pipeline.unregister(original)
        pipeline.register(target_binding("pane", delivered))
        return InteractionResult(DeliveryStatus.COMPLETED)

    def hook(request):
        if not during_open:
            replace_binding()
        return request

    pipeline = SessionInteractionPipeline(hooks=(hook,))
    original = target_binding("pane", delivered, exists=lambda: not during_open, opener=replace_binding)
    pipeline.register(original)
    assert pipeline.send_message(target="pane", text="hello", open_if_missing=True).status == DeliveryStatus.REJECTED
    assert delivered == []


def test_target_closed_during_hook_is_rejected_without_retry():
    available = True
    delivered = []

    def hook(request):
        nonlocal available
        available = False
        return request

    pipeline = SessionInteractionPipeline(hooks=(hook,))
    pipeline.register(target_binding("pane", delivered, exists=lambda: available))
    assert pipeline.send_message(target="pane", text="hello").status == DeliveryStatus.REJECTED
    assert not delivered


def test_indeterminate_dispatch_preserves_exception_and_outcome_without_retry():
    calls = []
    error = RodexDispatchIndeterminateError("may be accepted", dispatch_id="dispatch-1")

    def deliver(request):
        calls.append(request)
        raise error

    pipeline = SessionInteractionPipeline()
    pipeline.register(replace(target_binding("main", []), deliver=deliver))
    with pytest.raises(RodexDispatchIndeterminateError) as caught:
        pipeline.send_message(target="main", text="hello")
    assert caught.value is error
    assert len(calls) == 1
    assert pipeline.records[-1].status == DeliveryStatus.INDETERMINATE


@pytest.mark.parametrize(
    "operation,source",
    [
        (InteractionOperation.MESSAGE, "exact-control"),
        (InteractionOperation.MESSAGE, "alias-announcement"),
        (InteractionOperation.STEER, "exact-control"),
        (InteractionOperation.STEER, "alias-announcement"),
        (InteractionOperation.INTERRUPT, "exact-control"),
    ],
)
def test_exact_turn_and_alias_adapters_enter_common_pipeline(operation, source):
    calls = []
    hooks = []
    dispatch = PromptDispatch("started", "turn", "dispatch")
    client = SimpleNamespace(
        _start_turn=lambda *args, **kwargs: calls.append(("start", args, kwargs)) or dispatch,
        _steer_turn=lambda *args, **kwargs: calls.append(("steer", args, kwargs)) or dispatch,
        _interrupt_turn=lambda *args, **kwargs: calls.append(("interrupt", args, kwargs)) or "state",
    )
    pipeline = SessionInteractionPipeline(hooks=(lambda request: hooks.append(request) or request,))
    coordinator = ExactTurnMutationCoordinator(None, None, client, interaction_pipeline=pipeline)
    target = SimpleNamespace(control=SimpleNamespace(runtime_id="runtime", codex_session_id="thread"))
    coordinator._dispatch_turn_operation(
        target,
        operation,
        revalidate=lambda: None,
        prompt="hello",
        turn_id="turn",
        dispatch_id="dispatch",
        source=source,
    )
    assert len(hooks) == len(calls) == 1
    assert hooks[0].source == source
    assert hooks[0].start_model_turn is (operation == InteractionOperation.MESSAGE)
    assert calls[0][2]["revalidate"] is not None


def test_rejected_exact_message_never_reaches_model_rpc():
    def reject(request):
        raise InteractionRejected("blocked by processing hook")

    pipeline = SessionInteractionPipeline(hooks=(reject,))
    coordinator = ExactTurnMutationCoordinator(None, None, object(), interaction_pipeline=pipeline)
    target = SimpleNamespace(control=SimpleNamespace(runtime_id="runtime", codex_session_id="thread"))
    with pytest.raises(RodexControlError, match="blocked by processing hook"):
        coordinator._dispatch_turn_operation(
            target, InteractionOperation.MESSAGE, prompt="hello", revalidate=lambda: None
        )


def test_observer_snapshot_reserves_transport_identity_space():
    from rodex.agent_observer import _observer_event_frame
    from rodex.observer_contract import OBSERVER_FRAME_LENGTH, OBSERVER_MAX_FRAME_BYTES, OBSERVER_SNAPSHOT_MAX_BYTES
    from rodex.observer_state import ObserverStateReducer

    producer = ObserverStateReducer.producer()
    for number in range(32):
        snapshot = producer.observe(
            {
                "schema": "rodex-agent-observer-v2",
                "kind": "app_server_agent_message",
                "thread_id": "thread",
                "turn_id": "turn",
                "item": {"id": str(number), "type": "agentMessage", "text": "x" * 16384},
            }
        )
    encoded = _observer_event_frame(snapshot)
    assert len(encoded) - OBSERVER_FRAME_LENGTH.size <= OBSERVER_SNAPSHOT_MAX_BYTES
    addressed = _observer_event_frame(snapshot | {"pane_id": "%18446744073709551615"})
    assert len(addressed) - OBSERVER_FRAME_LENGTH.size <= OBSERVER_MAX_FRAME_BYTES


@pytest.mark.parametrize("response", ["{}", '{"id":0}', '{"id":0,"result":{"status":[]}}'])
def test_every_indeterminate_transport_result_preserves_generated_dispatch_identity(response):
    from pathlib import Path

    from rodex.interaction_transport import publish_session_interaction

    sent = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def send(self, message):
            sent.append(json.loads(message))

        def recv(self, **_kwargs):
            return response

    result = publish_session_interaction(
        Path("/unused.sock"),
        InteractionRequest("main", InteractionOperation.MESSAGE, "test", text="hello", start_model_turn=True),
        connector=lambda *_args, **_kwargs: Connection(),
    )
    assert result.status == DeliveryStatus.INDETERMINATE
    assert result.value["dispatch_id"] == sent[0]["params"]["dispatch_id"]


def test_display_can_render_blank_lines_without_starting_a_model_turn():
    delivered = []
    pipeline = SessionInteractionPipeline()
    pipeline.register(target_binding("pane", delivered, thread="thread"))
    assert pipeline.send_message(target="pane", text="\n").accepted
    assert pipeline.send_message(target="pane", text="\n", start_model_turn=True).status == DeliveryStatus.REJECTED
