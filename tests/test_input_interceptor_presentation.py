"""Configuration drives live display and submitted handling through the shared pipeline."""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from rodex.input_interceptor_config import (
    INPUT_INTERCEPTORS,
    ArgumentMenuConfig,
    InputInterceptorRegistration,
    InterceptionOption,
    InterceptionRule,
    LiveInterceptionRule,
)
from rodex.input_interceptor_presentation import InputInterceptorPresentation
from rodex.input_menu import INPUT_MENU_TARGET, InputInterceptionMenu, InputMenuView
from rodex.interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from rodex.pane_control import TmuxPaneController
from rodex.tmux_session_capability import TmuxRuntimeCapability
from rodex_registry import RodexRuntimeId


def capability():
    return TmuxRuntimeCapability(
        Path("/test-owned/tmux.sock"),
        "0123456789abcdef0123456789abcdef",
        "$1",
        "%9",
        RodexRuntimeId.parse("0c01ee2ead7240e1"),
    )


class FakePane:
    known_pane_id = "%9"
    snapshot = ("\u203a /r", 4)

    def capture_cursor_line(self):
        return self.snapshot


def setup_presentation(registrations=INPUT_INTERCEPTORS, *, display_available=True):
    pipeline = SessionInteractionPipeline()
    messages = []
    pipeline.register(
        InteractionTarget(
            "main",
            "runtime",
            frozenset({InteractionOperation.MESSAGE}),
            exists=lambda: True,
            deliver=lambda request: messages.append(request) or InteractionResult(DeliveryStatus.DELIVERED),
        )
    )
    pane = FakePane()
    presentation = InputInterceptorPresentation(pipeline, registrations, capability(), pane)
    pipeline.register(
        InteractionTarget(
            "terminal",
            "runtime",
            frozenset({InteractionOperation.DISPLAY_STATE}),
            exists=lambda: display_available,
            deliver=lambda request: messages.append(request) or InteractionResult(DeliveryStatus.DELIVERED),
        )
    )
    return pipeline, presentation, pane, messages


def menu_request(text, registrations=INPUT_INTERCEPTORS, prefix="/r"):
    menu = InputInterceptionMenu(registrations, text)
    return InteractionRequest(
        INPUT_MENU_TARGET,
        InteractionOperation.INTERACTIVE_INPUT,
        "test",
        text=text,
        payload=menu.view(prefix).serialize(),
    )


@pytest.mark.parametrize("text", ["/ro", "/rod", "/rode", "/rodex"])
def test_every_live_regex_match_displays_the_same_configured_completion(text):
    pipeline, _presentation, _pane, messages = setup_presentation()
    assert pipeline.execute(menu_request(text)).accepted
    assert len(messages) == 1 and messages[0].operation == InteractionOperation.DISPLAY_STATE
    state = InputMenuView.deserialize(messages[0].payload)
    assert state == InputInterceptionMenu(INPUT_INTERCEPTORS, text).view("/r")
    assert state.rows[0].label == "/rodex" and state.rows[0].helper_text == "issue a rodex command"
    assert not messages[0].start_model_turn


def test_placeholder_submission_is_display_only_and_release_clears_terminal_state():
    pipeline, presentation, _pane, messages = setup_presentation()
    entry = INPUT_INTERCEPTORS[0]
    assert pipeline.execute(
        InteractionRequest(entry.target, InteractionOperation.SUBMITTED_COMMAND, "test", text="/rodex hi")
    ).accepted
    assert len(messages) == 1 and "placeholder only" in messages[0].text
    assert all(not message.start_model_turn for message in messages)
    pipeline.execute(InteractionRequest(INPUT_MENU_TARGET, InteractionOperation.INPUT_RELEASE, "test", text="/rodex hi"))
    assert messages[-1].operation == InteractionOperation.DISPLAY_STATE and messages[-1].payload is None
    presentation.close()
    assert entry.target not in pipeline._targets


def test_unavailable_display_rejects_takeover():
    pipeline, _presentation, _pane, messages = setup_presentation(display_available=False)
    result = pipeline.execute(menu_request("/rodex #(echo should-not-execute)\n#{pane_id}"))
    assert result.status == DeliveryStatus.REJECTED
    assert messages == []


def test_another_configuration_drives_matching_helper_completion_and_argument_menu():
    entry = InputInterceptorRegistration(
        "example",
        "!hello",
        LiveInterceptionRule(r"^!h(?:ello)?$", helper_text="custom helper text"),
        InterceptionRule(r"^!hello (.*?)$"),
        ArgumentMenuConfig("Custom heading", "Custom subheading", (InterceptionOption("future", "a future option"),)),
    )
    pipeline, _presentation, _pane, messages = setup_presentation((entry,))
    request = menu_request("!h", (entry,), "!")
    assert pipeline.execute(request).accepted
    assert InputMenuView.deserialize(messages[-1].payload).rows[0].helper_text == "custom helper text"
    assert InputMenuView.deserialize(messages[-1].payload).rows[0].label == "!hello"
    assert pipeline.execute(menu_request("!help", (entry,), "!")).accepted
    assert InputMenuView.deserialize(messages[-1].payload).rows == ()
    assert not pipeline.execute(
        replace(request, target=entry.target, operation=InteractionOperation.SUBMITTED_COMMAND)
    ).accepted
    assert pipeline.execute(
        replace(request, target=entry.target, operation=InteractionOperation.SUBMITTED_COMMAND, text="!hello future")
    ).accepted
    assert "!hello future: placeholder only" in messages[-1].text
    assert all(not message.start_model_turn for message in messages)


@pytest.mark.parametrize(
    "snapshot,expected",
    [
        (("\u203a /r", 4), True),
        (("  \u203a /r", 6), True),
        (("\u203a /r", 3), False),
        (("\u203a text /r", 9), False),
        (("\u203a /review", 4), False),
        (None, False),
    ],
)
def test_native_handoff_requires_exact_prefix_and_end_cursor(snapshot, expected):
    _pipeline, presentation, pane, _messages = setup_presentation()
    pane.snapshot = snapshot
    assert presentation.confirm_native_prefix("/r") is expected


@pytest.mark.parametrize(
    "prefix,snapshot",
    [("", ("\u203a", 2)), ("/rodex ", ("\u203a /rodex", 9)), ("/rodex 世界", ("\u203a /rodex 世界", 13))],
)
def test_handoff_accounts_for_captured_trailing_spaces_and_terminal_cell_width(prefix, snapshot):
    _pipeline, presentation, pane, _messages = setup_presentation()
    pane.snapshot = snapshot
    assert presentation.confirm_native_prefix(prefix)


@pytest.mark.parametrize(
    "snapshot,expected",
    [
        ("4|0|0\n\u203a /r\n4|0|0\n", ("\u203a /r", 4)),
        ("4|0|1\n\u203a /r\n4|0|1\n", None),
        ("4|0|0\n\u203a /r\n3|0|0\n", None),
        ("4|9|0\n\u203a /r\n4|9|0\n", None),
        ("invalid\n\u203a /r\ninvalid\n", None),
        ("", None),
    ],
)
def test_cursor_capture_brackets_one_fenced_snapshot(snapshot, expected):
    commands = []

    def runner(command, **_options):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, snapshot, "")

    pane = TmuxPaneController("tmux", capability(), "%9", primary=True, runner=runner)
    assert pane.capture_cursor_line() == expected
    assert len(commands) == 1
    assert commands[0][3:7] == ["if-shell", "-t", "%9", "-F"]
    assert "capture-pane -p -t %9" in commands[0][-2]
