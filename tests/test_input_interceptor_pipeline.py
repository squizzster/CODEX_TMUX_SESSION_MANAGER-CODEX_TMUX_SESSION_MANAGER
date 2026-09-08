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
from rodex.input_menu import ARGUMENT_MENU_FOOTER
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


def make_pipeline(registrations=INPUT_INTERCEPTORS, prefix="/r"):
    pipeline = SessionInteractionPipeline()
    native_input = bytearray()
    main_messages = []
    gateway = TerminalSessionGateway.__new__(TerminalSessionGateway)
    gateway._display_queue = bytearray()
    gateway._completion = TerminalCompletionRenderer(100, 16)
    gateway._decoder = TerminalInputDecoder()
    gateway._interceptor = TerminalInputInterceptor(registrations, pipeline, native_input.extend, lambda _prefix: True)
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
        pipeline, registrations, capability, SimpleNamespace(capture_cursor_line=lambda: None)
    )
    screen = pyte.Screen(100, 16)
    output = pyte.ByteStream(screen)

    def dispatch(operation, data):
        assert pipeline.execute(InteractionRequest("terminal", operation, "test", payload=data)).accepted
        output.feed(bytes(gateway._display_queue))
        gateway._display_queue.clear()

    dispatch(InteractionOperation.TERMINAL_OUTPUT, f"\x1b[5;1H\u203a {prefix}".encode())
    return SimpleNamespace(
        pipeline=pipeline,
        gateway=gateway,
        screen=screen,
        native_input=native_input,
        messages=main_messages,
        presentation=presentation,
        dispatch=dispatch,
        send=lambda data: dispatch(InteractionOperation.TERMINAL_INPUT, data),
    )


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
    harness = make_pipeline((entry,), prefix)
    dispatch, screen = harness.dispatch, harness.screen
    native_input, main_messages = harness.native_input, harness.messages
    pipeline, presentation = harness.pipeline, harness.presentation
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


def test_shared_command_menu_highlights_selected_row_then_confirms_configured_option_display_only():
    harness = make_pipeline()
    harness.send(b"/rod")
    screen = harness.screen
    assert screen.display[4].rstrip() == "\u203a /rod"
    assert screen.display[6].strip() == "/rodex   issue a rodex command"
    assert (
        screen.display[7].strip() == "/rodx    dummy test to see if we can move up and down in the rodex/rodx menu here"
    )
    assert screen.buffer[6][2].fg == "cyan" and screen.buffer[6][2].bold
    assert screen.buffer[7][2].fg == "default" and not screen.buffer[7][2].bold
    harness.send(b"\x1b[B")
    assert screen.buffer[7][2].fg == "cyan" and screen.buffer[6][2].fg == "default"
    harness.send(b"\x1b[B\r\n")
    config = INPUT_INTERCEPTORS[0].argument_menu
    assert screen.display[4].rstrip() == config.heading
    assert screen.display[5].rstrip() == config.subheading
    assert screen.display[7].rstrip() == "\u203a 1. light   Light test argument"
    assert screen.display[8].rstrip() == "  2. dark    Dark one"
    assert screen.display[9].rstrip() == "  3. dusk    Dusky one"
    assert screen.display[11].rstrip() == ARGUMENT_MENU_FOOTER
    assert harness.messages == [] and harness.native_input == b"/r"
    harness.send(b"\x1b[A")
    assert screen.display[9].startswith("\u203a 3. dusk") and screen.buffer[9][0].fg == "cyan"
    # A native redraw goes through the same gateway without dropping the selection.
    harness.dispatch(InteractionOperation.TERMINAL_OUTPUT, b"\x1b[5;5H")
    assert screen.display[9].startswith("\u203a 3. dusk")
    harness.send(b"\r\n")
    assert len(harness.messages) == 1
    assert harness.messages[0].text == "/rodex dusk: placeholder only — no action performed."
    assert harness.native_input == b"/r\x7f\x7f"
    assert not any(record.start_model_turn for record in harness.pipeline.records)
    assert not any("Press enter" in line for line in screen.display)
    assert screen.display == harness.gateway._completion.native.screen.display


def test_filtering_to_one_command_keeps_it_highlighted_and_escape_restores_the_command_menu():
    harness = make_pipeline()
    harness.send(b"/rod\x1b[B")
    harness.send(b"e\x1b[A\x1b[B")
    assert harness.screen.buffer[6][2].fg == "cyan"
    assert harness.screen.display[7].strip() == ""
    harness.send(b"\r\x1b\x1b[B")  # Back to command menu, then Down on its sole row.
    assert harness.screen.display[4].rstrip() == "\u203a /rode"
    assert harness.screen.buffer[6][2].fg == "cyan"
    assert harness.messages == [] and harness.native_input == b"/r"


def test_rodx_has_an_empty_configured_picker_with_no_dummy_action_to_confirm():
    harness = make_pipeline()
    harness.send(b"/rodx\r\n\x1b[A\x1b[B\r")
    assert any(line.rstrip() == "Dummy test command" for line in harness.screen.display)
    assert any(line.strip() == "No options configured." for line in harness.screen.display)
    assert any(line.rstrip() == ARGUMENT_MENU_FOOTER for line in harness.screen.display)
    assert harness.messages == [] and harness.native_input == b"/r"
