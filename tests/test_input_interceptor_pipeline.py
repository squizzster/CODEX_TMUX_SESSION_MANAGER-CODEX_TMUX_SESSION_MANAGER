"""Real Python pipeline adapters with in-memory terminal I/O; never launch an application."""

from pathlib import Path
from types import SimpleNamespace

import pyte
import pytest

from rodex.input_interceptor_config import (
    INPUT_INTERCEPTORS,
    InputInterceptorRegistration,
    InterceptionRule,
    LiveInterceptionRule,
)
from rodex.input_interceptor_presentation import InputInterceptorPresentation
from rodex.interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from rodex.terminal_completion import TerminalCompletionRenderer
from rodex.terminal_gateway import TerminalSessionGateway
from rodex.terminal_input import TerminalInputDecoder, TerminalInputInterceptor
from rodex.tmux_session_capability import TmuxRuntimeCapability
from rodex_registry import RodexRuntimeId


@pytest.mark.parametrize(
    "entry,draft,prefix",
    [(INPUT_INTERCEPTORS[0], draft, "/r") for draft in ("/ro", "/rod", "/rode", "/rodex")]
    + [
        (
            InputInterceptorRegistration(
                "unrelated",
                "!hello",
                LiveInterceptionRule(r"^!h(?:ello)?$", helper_text="a configured custom helper"),
                InterceptionRule(r"^!hello (.*?)$"),
            ),
            "!hello",
            "!",
        ),
    ],
)
def test_keyboard_to_configured_inline_completion_and_enter_to_main_are_one_pipeline(entry, draft, prefix):
    pipeline = SessionInteractionPipeline()
    native_input = bytearray()
    main_messages = []
    gateway = TerminalSessionGateway.__new__(TerminalSessionGateway)
    gateway._display_queue = bytearray()
    gateway._completion = TerminalCompletionRenderer(80, 12)
    gateway._decoder = TerminalInputDecoder()
    gateway._interceptor = TerminalInputInterceptor((entry,), pipeline, native_input.extend, lambda _prefix: True)
    pipeline.register(
        InteractionTarget(
            "terminal",
            "runtime",
            frozenset(
                {
                    InteractionOperation.TERMINAL_INPUT,
                    InteractionOperation.TERMINAL_OUTPUT,
                    InteractionOperation.DISPLAY_STATE,
                }
            ),
            exists=lambda: True,
            deliver=gateway._deliver,
        )
    )
    pipeline.register(
        InteractionTarget(
            "main",
            "runtime",
            frozenset({InteractionOperation.MESSAGE}),
            exists=lambda: True,
            deliver=lambda request: main_messages.append(request) or InteractionResult(DeliveryStatus.DELIVERED),
        )
    )
    capability = TmuxRuntimeCapability(
        Path("/unused-test-socket"),
        "0123456789abcdef0123456789abcdef",
        "$1",
        "%9",
        RodexRuntimeId.parse("0c01ee2ead7240e1"),
    )
    presentation = InputInterceptorPresentation(
        pipeline, (entry,), capability, SimpleNamespace(capture_cursor_line=lambda: None)
    )
    screen = pyte.Screen(80, 12)
    output = pyte.ByteStream(screen)

    def dispatch(operation, data):
        assert pipeline.execute(InteractionRequest("terminal", operation, "test", payload=data)).accepted
        output.feed(bytes(gateway._display_queue))
        gateway._display_queue.clear()

    # The native editor has rendered the forwarded prefix; its response is an
    # ordinary terminal-output request, not an alternate presentation route.
    dispatch(InteractionOperation.TERMINAL_OUTPUT, f"\x1b[5;1H\u203a {prefix}".encode())
    dispatch(InteractionOperation.TERMINAL_INPUT, draft.encode())
    assert native_input == prefix.encode()
    assert screen.display[4].rstrip() == f"\u203a {draft}"
    assert screen.display[6].strip() == f"{entry.completion_text}   {entry.live.helper_text}"
    assert main_messages == []
    # Tab selects configured completion text. Only the configured Enter rule
    # admits submission, and only the main display adapter receives its response.
    dispatch(InteractionOperation.TERMINAL_INPUT, b"\t example\r")
    assert len(main_messages) == 1 and main_messages[0].target == "main"
    assert "placeholder only" in main_messages[0].text
    assert native_input == prefix.encode() + b"\x7f" * len(prefix)
    assert screen.display[4].rstrip() == f"\u203a {prefix}"
    assert all(not record.start_model_turn for record in pipeline.records)
    assert {record.operation for record in pipeline.records} == {
        InteractionOperation.TERMINAL_INPUT,
        InteractionOperation.TERMINAL_OUTPUT,
        InteractionOperation.INTERACTIVE_INPUT,
        InteractionOperation.DISPLAY_STATE,
        InteractionOperation.SUBMITTED_COMMAND,
        InteractionOperation.MESSAGE,
        InteractionOperation.INPUT_RELEASE,
    }
    presentation.close()
