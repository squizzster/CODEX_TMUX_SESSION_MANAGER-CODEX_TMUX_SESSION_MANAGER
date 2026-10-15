"""Forwarded text is a bounded local view, never another model submission."""

import json
from dataclasses import replace

import pytest

from rodex.interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from rodex.prompt_expansion_display import PROMPT_EXPANSION_MAX_CHARACTERS, PromptExpansion, PromptExpansionDisplay
from rodex.protocol_input_text import PromptTextEdit


def submission(*texts, method="turn/start"):
    return json.dumps({"id": 1, "method": method, "params": {"input": [{"type": "text", "text": t} for t in texts]}})


@pytest.mark.parametrize("method", ["turn/start", "turn/steer", "thread/queue/add", "thread/queue/update"])
def test_forwarded_actual_multipart_expansion_without_a_second_hook_call(method):
    display = PromptExpansionDisplay()
    source = object()
    display.bind_source(source)
    calls, sent = [], []

    def hook(texts):
        calls.append(texts)
        return tuple((PromptTextEdit(len(text), len(text), "!"),) for text in texts)

    pipeline = SessionInteractionPipeline(input_text_hook=hook)

    def deliver(request):
        assert display.snapshot(0) == (None, None)  # No speculative preview before send.
        sent.append(request.payload)
        return InteractionResult(DeliveryStatus.DELIVERED)

    pipeline.register(
        InteractionTarget(
            "primary",
            "runtime",
            frozenset({InteractionOperation.PROTOCOL_INPUT}),
            lambda: True,
            deliver,
            observe_delivered=lambda original, forwarded: display.observe_forwarded(
                source, original.payload, forwarded.payload
            ),
        )
    )
    assert pipeline.execute(
        InteractionRequest(
            "primary", InteractionOperation.PROTOCOL_INPUT, "test", payload=submission("Hello", "world", method=method)
        )
    ).accepted
    assert calls == [("Hello", "world")]
    assert len(sent) == 1
    assert display.snapshot(0)[0] == PromptExpansion("Hello\nworld", "Hello!\nworld!")
    assert not hasattr(pipeline.records[0], "payload")


@pytest.mark.parametrize("failure", ["reject", "raise", "display"])
def test_failed_delivery_never_displays_and_display_failure_never_retries(failure):
    observations, sends = [], []
    pipeline = SessionInteractionPipeline()

    def deliver(request):
        sends.append(request)
        if failure == "raise":
            raise OSError("transport failed")
        return InteractionResult(DeliveryStatus.REJECTED if failure == "reject" else DeliveryStatus.DELIVERED)

    def observe(original, forwarded):
        observations.append(forwarded)
        raise RuntimeError("display failed")

    target = InteractionTarget("primary", "runtime", frozenset(InteractionOperation), lambda: True, deliver)
    pipeline.register(replace(target, observe_delivered=observe))
    request = InteractionRequest("primary", InteractionOperation.PROTOCOL_INPUT, "test", payload=submission("push"))
    if failure == "raise":
        with pytest.raises(OSError):
            pipeline.execute(request)
    else:
        assert pipeline.execute(request).accepted == (failure == "display")
    assert len(sends) == 1
    assert len(observations) == (1 if failure == "display" else 0)


def test_latest_only_source_fencing_startup_expiry_and_unchanged_submission():
    display = PromptExpansionDisplay()
    old, current = object(), object()
    display.bind_source(old)
    display.observe_forwarded(old, submission("push"), submission("expanded"))
    wakes = []
    unsubscribe = display.subscribe(lambda: wakes.append(True))
    assert display.snapshot(0)[0] == PromptExpansion("push", "expanded")  # Initial argv precedes gateway subscription.
    display.bind_source(current)
    display.observe_forwarded(old, submission("push"), submission("stale"))
    display.release_source(old)
    assert display.snapshot(0) == (None, None)
    for i in range(1000):
        display.observe_forwarded(current, submission("push"), submission(str(i)))
    assert display.snapshot(0)[0] == PromptExpansion("push", "999")
    display.observe_forwarded(current, submission("plain"), submission("plain"))
    assert display.snapshot(0)[0] == PromptExpansion("push", "999")  # Unchanged native helper input does not erase it.
    display.observe_forwarded(current, submission("push"), submission("x" * 100_000))
    expansion, expiry = display.snapshot(0)
    assert len(expansion.original_text) + len(expansion.forwarded_text) <= PROMPT_EXPANSION_MAX_CHARACTERS
    assert expansion.forwarded_text.endswith("…")
    assert display.snapshot(expiry) == (None, None)
    unsubscribe()
    before = len(wakes)
    display.release_source(current)
    assert len(wakes) == before


def test_replacing_gateway_subscription_cannot_be_unsubscribed_by_old_gateway():
    display = PromptExpansionDisplay()
    wakes = []
    old = display.subscribe(lambda: wakes.append("old"))
    display.subscribe(lambda: wakes.append("new"))
    old()
    display.bind_source(object())
    assert wakes == ["new"]


def test_large_original_and_multipart_expansion_share_one_bounded_display_budget():
    display = PromptExpansionDisplay()
    source = object()
    display.bind_source(source)
    display.observe_forwarded(source, submission("o" * 100_000), submission("a" * 4000, "b" * 100_000))
    expansion, _expiry = display.snapshot(0)
    assert len(expansion.original_text) == 512
    assert expansion.original_text.endswith("…")
    assert expansion.forwarded_text.startswith("a" * 4000 + "\n")
    assert expansion.forwarded_text.endswith("…")
    assert len(expansion.original_text) + len(expansion.forwarded_text) == PROMPT_EXPANSION_MAX_CHARACTERS
