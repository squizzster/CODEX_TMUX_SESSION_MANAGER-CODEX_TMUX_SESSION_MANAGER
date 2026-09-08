"""Input ownership needs a visible status claim and an exact native handoff snapshot."""

import subprocess
from pathlib import Path

import pytest

from rodex.input_interceptor_config import INPUT_INTERCEPTORS
from rodex.input_interceptor_presentation import InputInterceptorPresentation
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
from rodex.tmux_status import StatusPriority
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


class FakeStatus:
    def __init__(self):
        self.presentations = []
        self.restored = []
        self.available = True

    def publish_transient(self, **options):
        self.presentations.append(options)
        return self.available

    def restore_if_token_matches(self, token):
        self.restored.append(token)


def setup_presentation():
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
    presentation = InputInterceptorPresentation(pipeline, INPUT_INTERCEPTORS, "tmux", capability(), pane)
    presentation._status = FakeStatus()
    return pipeline, presentation, pane, messages


def test_placeholder_menu_and_submission_are_display_only_and_release_restores_own_token():
    pipeline, presentation, _pane, messages = setup_presentation()
    entry = INPUT_INTERCEPTORS[0]
    for text in ("/ro", "/rod", "/rode"):
        assert pipeline.execute(
            InteractionRequest(entry.target, InteractionOperation.INTERACTIVE_INPUT, "test", text=text)
        ).accepted
    assert messages == []
    for _ in range(2):
        pipeline.execute(InteractionRequest(entry.target, InteractionOperation.INTERACTIVE_INPUT, "test", text="/rodex"))
    assert len(messages) == 1 and "Placeholder menu" in messages[0].text
    assert pipeline.execute(
        InteractionRequest(entry.target, InteractionOperation.SUBMITTED_COMMAND, "test", text="/rodex hi")
    ).accepted
    assert len(messages) == 2 and "placeholder only" in messages[1].text
    assert all(not message.start_model_turn for message in messages)
    pipeline.execute(InteractionRequest(entry.target, InteractionOperation.INPUT_RELEASE, "test", text="/rodex hi"))
    assert presentation._status.restored == [presentation._token]
    presentation.close()
    assert entry.target not in pipeline._targets


def test_unavailable_status_rejects_takeover_and_user_text_stays_literal():
    pipeline, presentation, _pane, messages = setup_presentation()
    entry = INPUT_INTERCEPTORS[0]
    presentation._status.available = False
    result = pipeline.execute(
        InteractionRequest(
            entry.target,
            InteractionOperation.INTERACTIVE_INPUT,
            "test",
            text="/rodex #(echo should-not-execute)\n#{pane_id}",
        )
    )
    assert result.status == DeliveryStatus.REJECTED
    rendered = presentation._status.presentations[-1]["presentation"].status_format
    assert "##(echo should-not-execute)" in rendered and "##{pane_id}" in rendered
    assert "\n" not in rendered
    assert messages == []


def test_owned_local_input_is_visible_above_animation_but_not_exit_warnings():
    assert StatusPriority.SHARING_ANIMATION < StatusPriority.LOCAL_INPUT < StatusPriority.SAFETY_WARNING


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
