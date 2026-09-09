"""Configured admission and lossless terminal framing, independent of native rendering."""

import re
from dataclasses import replace

import pytest

from rodex.input_interceptor_config import (
    INPUT_INTERCEPTORS,
    ArgumentMenuConfig,
    InputInterceptorRegistration,
    InterceptionRule,
    LiveInterceptionRule,
)
from rodex.input_menu import INPUT_MENU_TARGET, InputMenuStage, InputMenuView
from rodex.interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from rodex.terminal_input import PASTE_END, PASTE_START, TerminalInputDecoder, TerminalInputInterceptor


@pytest.mark.parametrize("text", ["/ro", "/rod", "/rode", "/rodex"])
def test_single_configured_pattern_accepts_exact_candidates(text):
    assert INPUT_INTERCEPTORS[0].live.matches(text)


@pytest.mark.parametrize("text", ["", "/", "/r", "/robot", "/rodexx", "/rodex ", "/rodex hi", "/ro\n", "x/ro"])
def test_single_configured_pattern_rejects_nonmatches(text):
    assert not INPUT_INTERCEPTORS[0].live.matches(text)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/rodex ", True),
        ("/rodex example", True),
        ("/rode example", False),
        ("/rodex", False),
        ("/ro", False),
        ("before\n/rodex example", False),
        ("/rodex example\nother input", False),
    ],
)
def test_enter_expression_is_independent_and_matches_complete_input(text, expected):
    assert INPUT_INTERCEPTORS[0].on_enter.matches(text) is expected


@pytest.mark.parametrize("pattern", [r"/hello", r"^$", r"^($"])
def test_invalid_configured_expressions_fail_at_registration(pattern):
    with pytest.raises((ValueError, re.error)):
        InterceptionRule(pattern)


class InputHarness:
    def __init__(self, *, confirm=True, registrations=INPUT_INTERCEPTORS, reject=False):
        self.forwarded = bytearray()
        self.events = []
        self.prefix_checks = []
        self.pipeline = SessionInteractionPipeline()
        self.pipeline.register(
            InteractionTarget(
                INPUT_MENU_TARGET,
                "test-runtime",
                frozenset({InteractionOperation.INTERACTIVE_INPUT, InteractionOperation.INPUT_RELEASE}),
                exists=lambda: True,
                deliver=self.deliver,
            )
        )
        for entry in registrations:
            self.pipeline.register(
                InteractionTarget(
                    entry.target,
                    "test-runtime",
                    frozenset(
                        {
                            InteractionOperation.INTERACTIVE_INPUT,
                            InteractionOperation.SUBMITTED_COMMAND,
                            InteractionOperation.INPUT_CONFIGURATION_ERROR,
                            InteractionOperation.INPUT_RELEASE,
                        }
                    ),
                    exists=lambda: True,
                    deliver=self.deliver,
                )
            )
        self.reject = reject
        self.decoder = TerminalInputDecoder()

        def check(prefix):
            self.prefix_checks.append(prefix)
            return confirm

        self.interceptor = TerminalInputInterceptor(registrations, self.pipeline, self.forwarded.extend, check)

    def deliver(self, request):
        self.events.append(request)
        return InteractionResult(DeliveryStatus.REJECTED if self.reject else DeliveryStatus.DELIVERED)

    def feed(self, data):
        for event in self.decoder.feed(data, now=0):
            self.interceptor.accept(event)

    def escape(self):
        self.feed(b"\x1b")
        for event in self.decoder.expire_incomplete(1):
            self.interceptor.accept(event)


def test_slash_and_r_are_immediate_native_input_then_o_enters_local_owner():
    harness = InputHarness()
    harness.feed(b"/")
    assert harness.forwarded == b"/"
    harness.feed(b"r")
    assert harness.forwarded == b"/r"
    assert harness.events == []
    harness.feed(b"o")
    assert harness.forwarded == b"/r"
    assert harness.prefix_checks == ["/r"]
    assert harness.events[-1].text == "/ro"
    assert harness.events[-1].operation == InteractionOperation.INTERACTIVE_INPUT


def menu_view(harness):
    return InputMenuView.deserialize(
        next(
            event.payload
            for event in reversed(harness.events)
            if event.operation == InteractionOperation.INTERACTIVE_INPUT
        )
    )


@pytest.mark.parametrize("up,down", [(b"\x1b[A", b"\x1b[B"), (b"\x1bOA", b"\x1bOB"), (b"\x1b[57352u", b"\x1b[57353u")])
@pytest.mark.parametrize("chunk_size", [1, 100])
def test_arrow_encodings_navigate_both_menus_without_leaking_native_input(up, down, chunk_size):
    harness = InputHarness()

    def feed(data):
        for offset in range(0, len(data), chunk_size):
            harness.feed(data[offset : offset + chunk_size])

    feed(b"/rod" + up)
    assert menu_view(harness).selected_index == 1
    feed(down)
    assert menu_view(harness).selected_index == 0
    feed(b"\r\n")
    assert menu_view(harness).stage == InputMenuStage.ARGUMENTS
    assert menu_view(harness).selected_index == 0
    assert not any(event.operation == InteractionOperation.SUBMITTED_COMMAND for event in harness.events)
    feed(up)
    assert menu_view(harness).selected_index == 2
    feed(down + down)
    assert menu_view(harness).selected_index == 1
    assert harness.forwarded == b"/r"
    feed(b"\r\n")
    assert [event.text for event in harness.events if event.operation == InteractionOperation.SUBMITTED_COMMAND] == [
        "/rodex dark"
    ]
    assert harness.forwarded == b"/r\x7f\x7f" and not harness.interceptor.active


def test_escape_returns_to_prior_command_selection_then_closes_local_menu():
    rodex, rodx = INPUT_INTERCEPTORS
    harness = InputHarness(registrations=(rodex, replace(rodx, argument_menu=rodex.argument_menu)))
    harness.feed(b"/rod\x1b[B\r")
    assert menu_view(harness).stage == InputMenuStage.ARGUMENTS and len(menu_view(harness).rows) == 3
    harness.feed(b"\x1b[A\x1b[B")
    assert harness.forwarded == b"/r"
    harness.escape()
    assert menu_view(harness).stage == InputMenuStage.COMMANDS
    assert menu_view(harness).selected_index == 1 and menu_view(harness).draft == "/rod"
    harness.escape()
    assert not harness.interceptor.active and harness.forwarded == b"/r"


def test_two_rapid_escapes_and_escape_followed_by_arrow_keep_their_key_boundaries():
    harness = InputHarness()
    harness.feed(b"/rod\r\x1b\x1b[B")
    assert menu_view(harness).stage == InputMenuStage.COMMANDS and menu_view(harness).selected_index == 1
    harness.feed(b"\x1b[A\r\x1b\x1b")
    for event in harness.decoder.expire_incomplete(1):
        harness.interceptor.accept(event)
    assert not harness.interceptor.active and harness.forwarded == b"/r"


def test_rejected_menu_navigation_does_not_change_what_enter_will_select():
    harness = InputHarness()
    harness.feed(b"/rod")
    harness.reject = True
    harness.feed(b"\x1b[B")
    harness.reject = False
    harness.feed(b"\r")
    assert [row.label for row in menu_view(harness).rows] == ["light", "dark", "dusk"]


def test_rejected_confirmation_keeps_the_option_picker_for_an_explicit_retry():
    harness = InputHarness()
    harness.feed(b"/rod\r\x1b[B")
    harness.reject = True
    harness.feed(b"\r\n")
    assert harness.interceptor.active and harness.forwarded == b"/r"
    harness.reject = False
    harness.feed(b"\r")
    submitted = [event for event in harness.events if event.operation == InteractionOperation.SUBMITTED_COMMAND]
    assert [event.text for event in submitted] == ["/rodex dark", "/rodex dark"]
    assert not harness.interceptor.active and harness.forwarded == b"/r\x7f\x7f"


def test_changed_native_composer_prevents_confirmation_without_losing_option_selection():
    harness = InputHarness()
    harness.feed(b"/rod\r\x1b[A")
    harness.interceptor._confirm_native_prefix = lambda _prefix: False
    harness.feed(b"\r\n")
    assert harness.interceptor.active and harness.forwarded == b"/r"
    assert not any(event.operation == InteractionOperation.SUBMITTED_COMMAND for event in harness.events)
    harness.interceptor._confirm_native_prefix = lambda _prefix: True
    harness.feed(b"\r")
    submitted = [event.text for event in harness.events if event.operation == InteractionOperation.SUBMITTED_COMMAND]
    assert submitted == ["/rodex dusk"]


def test_rejected_back_and_tab_keep_the_last_accepted_menu_state():
    rodex, rodx = INPUT_INTERCEPTORS
    harness = InputHarness(registrations=(rodex, replace(rodx, argument_menu=rodex.argument_menu)))
    harness.feed(b"/rod\x1b[B")
    harness.reject = True
    harness.feed(b"\t")
    harness.reject = False
    harness.feed(b"\x7f")
    assert menu_view(harness).draft == "/ro" and menu_view(harness).selected_index == 1
    harness.feed(b"\r")
    harness.reject = True
    harness.escape()
    harness.reject = False
    harness.feed(b"\x1b[B")
    assert menu_view(harness).stage == InputMenuStage.ARGUMENTS
    assert menu_view(harness).selected_index == 1 and harness.forwarded == b"/r"


@pytest.mark.parametrize("failure", ["delivery", "native-prefix"])
def test_unavailable_configuration_error_handoff_keeps_an_editable_command_menu_until_retry(failure):
    harness = InputHarness()
    harness.feed(b"/rodx")
    harness.reject = failure == "delivery"
    harness.interceptor._confirm_native_prefix = lambda _prefix: failure != "native-prefix"
    harness.feed(b"\r\n")
    assert harness.interceptor.active and harness.forwarded == b"/r"
    assert menu_view(harness).stage == InputMenuStage.COMMANDS
    harness.reject = False
    harness.interceptor._confirm_native_prefix = lambda _prefix: True
    harness.feed(b"\x7f")
    assert menu_view(harness).draft == "/rod" and menu_view(harness).selected_index == 1
    harness.feed(b"\r\nordinary typing")
    assert not harness.interceptor.active and harness.forwarded == b"/r\x7f\x7fordinary typing"
    assert not any(event.operation == InteractionOperation.SUBMITTED_COMMAND for event in harness.events)


@pytest.mark.parametrize("raw", [b"\x1b\x1b", b"\x1b\x1b[A", b"\x1b[A\x1bOB", b"\x1b[57352u"])
def test_escape_and_navigation_framing_preserves_every_byte_outside_local_menus(raw):
    harness = InputHarness()
    harness.feed(raw)
    for event in harness.decoder.expire_incomplete(1):
        harness.interceptor.accept(event)
    assert harness.forwarded == raw and harness.events == []


def test_picker_ignores_unrelated_editor_keys_and_pastes_and_forwards_terminal_replies():
    harness = InputHarness()
    harness.feed(b"/rod\r\x1b[Dhello" + PASTE_START + b"/elsewhere\r\n" + PASTE_END + b"\x1b[12;3R")
    assert harness.forwarded == b"/r\x1b[12;3R"
    assert menu_view(harness).stage == InputMenuStage.ARGUMENTS
    assert not any(event.operation == InteractionOperation.SUBMITTED_COMMAND for event in harness.events)


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4, 7, 100])
def test_live_and_enter_use_their_own_configured_rules_across_input_reads(chunk_size):
    harness = InputHarness()
    data = b"/rodex example argument\r\n"
    for offset in range(0, len(data), chunk_size):
        harness.feed(data[offset : offset + chunk_size])
    assert harness.forwarded == b"/r\x7f\x7f"
    submitted = [event for event in harness.events if event.operation == InteractionOperation.SUBMITTED_COMMAND]
    assert [event.text for event in submitted] == ["/rodex example argument"]
    assert all(not event.start_model_turn for event in harness.events)
    assert not harness.interceptor.active


def test_nonmatching_enter_returns_the_held_draft_and_user_enter_to_native():
    harness = InputHarness()
    harness.feed(b"/robot\r")
    submitted = [event.text for event in harness.events if event.operation == InteractionOperation.SUBMITTED_COMMAND]
    assert submitted == []
    assert harness.forwarded == b"/r" + PASTE_START + b"obot" + PASTE_END + b"\r"


def test_rejected_submission_preserves_local_draft_and_native_prefix_for_retry():
    harness = InputHarness()
    harness.feed(b"/rodex example")
    harness.reject = True
    harness.feed(b"\r\n")
    assert harness.interceptor.active
    assert harness.forwarded == b"/r"
    assert harness.events[-1].text == "/rodex example"
    harness.reject = False
    harness.feed(b"\r")
    assert not harness.interceptor.active
    assert harness.forwarded == b"/r\x7f\x7f"


def test_registry_can_add_an_interceptor_without_transport_changes():
    registration = InputInterceptorRegistration(
        "another",
        "/xyz",
        LiveInterceptionRule(r"^/xy(?:z)?$", helper_text="another placeholder"),
        InterceptionRule(r"^/xyz (.*?)$"),
    )
    harness = InputHarness(registrations=(*INPUT_INTERCEPTORS, registration))
    harness.feed(b"/xyz anything\r")
    assert harness.events[-2].target == registration.target
    assert harness.events[-2].operation == InteractionOperation.SUBMITTED_COMMAND
    assert harness.forwarded == b"/x\x7f\x7f"


def test_enter_intercepts_an_atomic_paste_that_never_matched_the_live_expression():
    harness = InputHarness()
    draft = b"/rodex example"
    harness.feed(PASTE_START + draft + PASTE_END)
    assert not harness.interceptor.active and harness.events == []
    harness.feed(b"\r\n")
    assert [event.text for event in harness.events] == [draft.decode()]
    assert harness.events[0].operation == InteractionOperation.SUBMITTED_COMMAND
    assert harness.forwarded == PASTE_START + draft + PASTE_END + b"\x7f" * len(draft)


def test_ambiguous_typed_submission_stays_native_despite_shared_live_menu():
    duplicate = replace(INPUT_INTERCEPTORS[0], name="another")
    harness = InputHarness(registrations=(*INPUT_INTERCEPTORS, duplicate))
    harness.feed(b"/rodex example\r")
    assert harness.forwarded == b"/r" + PASTE_START + b"odex example" + PASTE_END + b"\r"
    assert not any(event.operation == InteractionOperation.SUBMITTED_COMMAND for event in harness.events)


def test_changing_only_enter_configuration_changes_submission_behavior():
    registration = replace(
        INPUT_INTERCEPTORS[0], on_enter=InterceptionRule(r"^/rode example$"), argument_menu=ArgumentMenuConfig()
    )
    harness = InputHarness(registrations=(registration,))
    harness.feed(b"/rode example\r")
    assert [event.text for event in harness.events if event.operation == InteractionOperation.SUBMITTED_COMMAND] == [
        "/rode example"
    ]
    assert harness.forwarded == b"/r\x7f\x7f"


def test_transport_does_not_assume_registered_patterns_are_slash_commands():
    registration = InputInterceptorRegistration(
        "example",
        "!hi",
        LiveInterceptionRule(r"^!h(?:i)?$", helper_text="another placeholder"),
        InterceptionRule(r"^!hi (.*?)$"),
    )
    harness = InputHarness(registrations=(registration,))
    harness.feed(b"!hi anything\r")
    assert harness.events[-2].target == registration.target
    assert harness.events[-2].text == "!hi anything"
    assert harness.forwarded == b"!\x7f"


@pytest.mark.parametrize("confirm,reject", [(False, False), (True, True)])
def test_unavailable_handoff_leaves_every_native_byte_intact(confirm, reject):
    harness = InputHarness(confirm=confirm, reject=reject)
    harness.feed(b"/rodex\r")
    assert harness.forwarded == b"/rodex\r"
    assert not harness.interceptor.active


@pytest.mark.parametrize("cancel", [b"\x7f", b"\x08", b"\x03", b"\x1b"])
def test_cancel_or_backspace_out_leaves_native_prefix_untouched(cancel):
    harness = InputHarness()
    harness.feed(b"/ro")
    if cancel == b"\x1b":
        harness.escape()
    else:
        harness.feed(cancel)
    assert harness.forwarded == b"/r"
    assert not harness.interceptor.active
    harness.feed(b"o")
    assert harness.interceptor.active


def test_tab_completion_and_native_editing_release_do_not_submit():
    harness = InputHarness()
    harness.feed(b"/ro\t")
    assert harness.events[-1].text == "/rodex"
    harness.feed(b"\x1b[D")
    assert harness.forwarded == b"/r" + PASTE_START + b"odex" + PASTE_END + b"\x1b[D"
    assert not harness.interceptor.active


def test_paste_is_atomic_for_matching_and_never_synthesizes_enter():
    harness = InputHarness()
    harness.feed(PASTE_START + b"/robot\nordinary text" + PASTE_END)
    assert not harness.interceptor.active
    assert harness.events == []
    assert harness.forwarded == PASTE_START + b"/robot\nordinary text" + PASTE_END
    harness.feed(b"\x15/rodex ")
    harness.feed(PASTE_START + "hello\n世界".encode() + PASTE_END)
    assert harness.interceptor.active
    assert harness.events[-1].text == "/rodex hello\n世界"
    assert not any(event.operation == InteractionOperation.SUBMITTED_COMMAND for event in harness.events)


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 23, 100000])
def test_decoder_preserves_utf8_paste_control_and_reply_bytes_across_reads(chunk_size):
    data = (
        "hello 世界".encode()
        + b"\x1b[D\x1b[12;3R\x1b]52;c;/rodex\x07\xff\xfe"
        + PASTE_START
        + b"/rodex\ntext"
        + PASTE_END
    )
    decoder = TerminalInputDecoder()
    events = []
    for offset in range(0, len(data), chunk_size):
        events.extend(decoder.feed(data[offset : offset + chunk_size], now=0))
    assert b"".join(event.raw for event in events) == data
    assert [event.text for event in events if event.kind == "paste"] == ["/rodex\ntext"]
    assert any(event.kind == "terminal_reply" and b"/rodex" in event.raw for event in events)


def test_terminal_replies_pass_through_even_while_local_input_is_owned():
    harness = InputHarness()
    harness.feed(b"/ro\x1b[12;3R\x1b]52;c;reply\x1b\\")
    assert harness.forwarded == b"/r\x1b[12;3R\x1b]52;c;reply\x1b\\"
    assert harness.interceptor.active
    assert harness.events[-1].text == "/ro"


def test_focus_changes_and_window_reports_do_not_cancel_local_editing():
    harness = InputHarness()
    reports = b"\x1b[I\x1b[O\x1b[8;36;120t\x1b[?2026;1$y"
    harness.feed(b"/ro" + reports)
    assert harness.forwarded == b"/r" + reports
    assert harness.interceptor.active
    assert harness.events[-1].text == "/ro"


def test_invalid_utf8_paste_releases_without_replacing_original_bytes():
    harness = InputHarness()
    harness.feed(b"/rodex ")
    paste = PASTE_START + b"a\xffb" + PASTE_END
    harness.feed(paste)
    assert harness.forwarded == b"/r" + PASTE_START + b"odex " + PASTE_END + paste
    assert not harness.interceptor.active


@pytest.mark.parametrize("raw", [b"\x1b", b"\x1b[", b"\x1bO", b"\xc3", b"\x1b]52;c;data"])
def test_incomplete_frames_expire_without_changing_bytes(raw):
    decoder = TerminalInputDecoder()
    assert decoder.feed(raw, now=0) == []
    assert decoder.expire_incomplete(0.01) == []
    assert b"".join(event.raw for event in decoder.expire_incomplete(1)) == raw


def test_expiring_a_partial_reply_keeps_its_suffix_out_of_candidate_matching():
    harness = InputHarness()
    harness.feed(b"\x1b]52;c;")
    for event in harness.decoder.expire_incomplete(1):
        harness.interceptor.accept(event)
    harness.feed(b"/rodex\x07")
    assert harness.forwarded == b"\x1b]52;c;/rodex\x07"
    assert harness.events == []


@pytest.mark.parametrize("encoding", ["csi-u", "modify-other-keys"])
def test_encoded_keyboard_presses_reach_the_same_match_and_submit_owner(encoding):
    def encode(character):
        return (f"\x1b[{ord(character)}u" if encoding == "csi-u" else f"\x1b[27;1;{ord(character)}~").encode()

    harness = InputHarness()
    for character in "/rodex example\r":
        for byte in encode(character):
            harness.feed(bytes([byte]))
    assert harness.forwarded == encode("/") + encode("r") + b"\x7f\x7f"
    submitted = [event.text for event in harness.events if event.operation == InteractionOperation.SUBMITTED_COMMAND]
    assert submitted == ["/rodex example"]


def test_encoded_key_release_is_not_a_second_character_or_submission():
    harness = InputHarness()
    harness.feed(b"/ro\x1b[111;1:3u")
    assert harness.forwarded == b"/r\x1b[111;1:3u"
    assert harness.events[-1].text == "/ro"


def test_shift_enter_retains_its_native_editing_semantics_not_local_submission():
    harness = InputHarness()
    harness.feed(b"/rodex\x1b[13;2u")
    assert harness.forwarded == b"/r" + PASTE_START + b"odex" + PASTE_END + b"\x1b[13;2u"
    assert not any(event.operation == InteractionOperation.SUBMITTED_COMMAND for event in harness.events)


def test_large_paste_remains_bounded_opaque_and_lossless():
    harness = InputHarness()
    paste = PASTE_START + b"x" * 150000 + b"/rodex\n" + PASTE_END
    for offset in range(0, len(paste), 4096):
        harness.feed(paste[offset : offset + 4096])
        assert len(harness.decoder._pending) <= 65536 + 4096
    assert harness.forwarded == paste
    assert harness.events == []


def test_terminal_operations_allow_byte_hooks_but_never_change_intent():
    delivered = []
    pipeline = SessionInteractionPipeline(hooks=(lambda request: replace(request, payload=b"replacement"),))
    pipeline.register(
        InteractionTarget(
            "terminal",
            "runtime",
            frozenset({InteractionOperation.TERMINAL_INPUT}),
            exists=lambda: True,
            deliver=lambda request: delivered.append(request) or InteractionResult(DeliveryStatus.DELIVERED),
        )
    )
    assert pipeline.execute(
        InteractionRequest("terminal", InteractionOperation.TERMINAL_INPUT, "test", payload=b"input")
    ).accepted
    assert delivered[0].payload == b"replacement"
    assert not pipeline.execute(
        InteractionRequest("terminal", InteractionOperation.TERMINAL_INPUT, "test", payload="text")
    ).accepted
    assert not pipeline.execute(
        InteractionRequest(
            "terminal", InteractionOperation.TERMINAL_INPUT, "test", payload=b"input", start_model_turn=True
        )
    ).accepted
