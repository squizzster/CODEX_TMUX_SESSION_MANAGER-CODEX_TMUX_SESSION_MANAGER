"""Pure Python terminal transcripts: no Codex, Rodex, tmux, sockets, or child processes."""

from dataclasses import replace

import pyte
import pytest

from rodex.input_interceptor_config import INPUT_INTERCEPTORS
from rodex.input_menu import ARGUMENT_MENU_FOOTER, InputInterceptionMenu, InputMenuRow, InputMenuView
from rodex.presentation_policy import PresentationItemText, PresentationSnapshot, PresentationSurface
from rodex.terminal_surface import TerminalSurfaceRenderer

NATIVE_MENU = (
    b"\x1b[2J\x1b[1;1HNative conversation"
    + "\x1b[5;1H\u203a /r".encode()
    + b"\x1b[7;1H  /review  review changes\x1b[8;1H  /resume  resume a chat\x1b[5;5H"
)
STATE = InputMenuView("/ro", "/r", (InputMenuRow("/rodex", "issue a rodex command"),), 0)


class DisplayHarness:
    def __init__(self, columns=80, rows=12):
        self.renderer = TerminalSurfaceRenderer(columns, rows)
        self.visible = pyte.HistoryScreen(columns, rows)
        self._output = pyte.ByteStream(self.visible)

    def native(self, data):
        rendered = self.renderer.native_output(data)
        self._output.feed(rendered)
        return rendered

    def display(self, state):
        accepted, rendered = self.renderer.display(state)
        self._output.feed(rendered)
        return accepted

    def present(self, snapshot):
        rendered = self.renderer.present(snapshot)
        self._output.feed(rendered)
        return rendered


@pytest.mark.parametrize("draft", ["/ro", "/rod", "/rode", "/rodex"])
def test_surface_renderer_is_inline_beneath_visible_draft_and_restores_native_exactly(draft):
    harness = DisplayHarness()
    assert harness.native(NATIVE_MENU) == NATIVE_MENU
    native_display = harness.visible.display
    assert harness.display(replace(STATE, draft=draft))
    assert harness.visible.display[0].rstrip() == "Native conversation"
    assert harness.visible.display[4].rstrip() == f"\u203a {draft}"
    assert harness.visible.display[5].strip() == ""
    assert harness.visible.display[6].strip() == "/rodex   issue a rodex command"
    assert "/review" not in "\n".join(harness.visible.display)
    assert "/resume" not in "\n".join(harness.visible.display)
    assert harness.visible.cursor.x == len(draft) + 2
    assert harness.renderer.native.screen.display == native_display
    assert harness.display(None)
    assert harness.visible.display == native_display
    assert harness.visible.cursor.x == 4
    assert not harness.visible.history.top


def test_nonmatching_owned_draft_has_no_configured_suggestion_and_no_command_name_assumptions():
    harness = DisplayHarness()
    harness.native(NATIVE_MENU.replace(b"/r", b"!h"))
    state = InputMenuView("!hello", "!h", (InputMenuRow("!hello", "custom helper"),), 0)
    assert harness.display(state)
    assert harness.visible.display[6].strip() == "!hello   custom helper"
    assert harness.display(replace(state, draft="!help", rows=(), selected_index=None))
    assert harness.visible.display[6].strip() == "no matches"


@pytest.mark.parametrize(
    "frame",
    [
        "\x1b[1;1H世界\x1b[5;5H".encode(),
        b"\x1b]0;title /rodex\x1b\\",
        b"\x1b[?2026h\x1b[1;1Hupdated\x1b[5;5H\x1b[?2026l",
    ],
)
def test_every_output_split_preserves_native_tokens_and_local_display(frame):
    for split in range(1, len(frame)):
        harness = DisplayHarness()
        harness.native(NATIVE_MENU)
        assert harness.display(STATE)
        harness.native(frame[:split])
        if not harness.renderer.native.paintable:
            assert "/rodex" not in "\n".join(harness.visible.display)
            assert harness.display(replace(STATE, draft="/rode"))  # Typing during an incomplete redraw retains ownership.
        harness.native(frame[split:])
        assert harness.visible.display[6].strip() == "/rodex   issue a rodex command"
        assert harness.display(None)
        assert harness.visible.display == harness.renderer.native.screen.display


def test_native_scrolling_does_not_copy_local_surface_renderer_into_scrollback():
    harness = DisplayHarness()
    harness.native(NATIVE_MENU)
    assert harness.display(STATE)
    harness.native(b"\x1b[12;1H\r\n" * 15)
    history = "".join(cell.data for line in harness.visible.history.top for cell in line.values())
    assert "/rodex" not in history and "issue a rodex command" not in history
    assert harness.visible.display == harness.renderer.native.screen.display


def test_opaque_control_string_is_forwarded_without_local_bytes_inside_it():
    frame = b"\x1bPignored /rodex\x1b\\"
    for split in range(1, len(frame)):
        renderer = TerminalSurfaceRenderer(80, 12)
        renderer.native_output(NATIVE_MENU)
        assert renderer.display(STATE)[0]
        first = renderer.native_output(frame[:split])
        second = renderer.native_output(frame[split:])
        assert first.endswith(frame[:split])
        assert second.startswith(frame[split:])
        assert "ignored" not in "\n".join(renderer.native.screen.display)


def test_local_paint_does_not_overwrite_native_saved_cursor_or_rendition():
    harness = DisplayHarness()
    harness.native(NATIVE_MENU + b"\x1b[31;1m\x1b7")
    assert harness.display(STATE)
    harness.native(b"\x1b8X")
    assert harness.visible.display[4].rstrip() == "\u203a /rX"
    cell = harness.visible.buffer[4][4]
    assert cell.fg == "red" and cell.bold
    assert harness.visible.savepoints[-1:] == harness.renderer.native.screen.savepoints[-1:]


def test_native_guard_suspends_local_draft_then_restores_it():
    harness = DisplayHarness()
    harness.native(NATIVE_MENU)
    assert harness.display(STATE)
    harness._output.feed(harness.renderer.suspend())
    assert harness.visible.display[4].rstrip() == "\u203a /r"
    harness.native(b"\x1b[5;5H")
    assert harness.visible.display[4].rstrip() == "\u203a /r"
    harness._output.feed(harness.renderer.resume())
    assert harness.visible.display[4].rstrip() == "\u203a /ro"


def test_resize_retains_local_draft_until_native_redraw_reanchors():
    harness = DisplayHarness()
    harness.native(NATIVE_MENU)
    assert harness.display(STATE)
    harness.visible.resize(columns=60, lines=10)
    harness._output.feed(harness.renderer.resize(60, 10))
    harness.native(NATIVE_MENU)
    assert harness.visible.display[4].rstrip() == "\u203a /ro"
    assert harness.visible.display[6].strip() == "/rodex   issue a rodex command"


def test_narrower_viewport_restoration_does_not_wrap_and_scroll_the_conversation():
    harness = DisplayHarness(columns=40)
    harness.native(NATIVE_MENU)
    assert harness.display(STATE)
    harness.visible.resize(columns=15)
    harness._output.feed(harness.renderer.resize(15, 12))
    assert harness.visible.display[0].rstrip() == "Native conversa"
    assert not harness.visible.history.top


def test_queued_old_width_local_output_cannot_scroll_a_newly_narrowed_viewport():
    harness = DisplayHarness(columns=80)
    harness.native(NATIVE_MENU)
    accepted, queued = harness.renderer.display(STATE)
    assert accepted
    harness.visible.resize(columns=15)
    harness._output.feed(queued)
    assert not harness.visible.history.top
    harness._output.feed(harness.renderer.resize(15, 12))
    harness.native(NATIVE_MENU)
    assert not harness.visible.history.top


@pytest.mark.parametrize("native_mode", [b"\x1b[?7l", b"\x1b[?7h"])
def test_local_paint_restores_the_native_wrapping_mode(native_mode):
    harness = DisplayHarness()
    harness.native(NATIVE_MENU + native_mode)
    native_modes = harness.visible.mode.copy()
    assert harness.display(STATE)
    assert harness.visible.mode == native_modes
    assert harness.display(None)
    assert harness.visible.mode == native_modes


def test_wide_and_control_text_is_clipped_without_escape_effects_or_scrolling():
    harness = DisplayHarness(columns=22)
    harness.native(NATIVE_MENU)
    assert harness.display(replace(STATE, draft="/rodex 世界\x1b[2J\n" + "x" * 80))
    assert harness.visible.display[0].rstrip() == "Native conversation"
    assert harness.visible.display[4].rstrip() == "\u203a /rodex 世界 [2J xxx"
    assert not harness.visible.history.top


def test_unavailable_composer_rejects_initial_takeover_without_output():
    renderer = TerminalSurfaceRenderer()
    assert renderer.display(STATE) == (False, b"")
    assert renderer.native_output(b"unchanged") == b"unchanged"


def test_empty_native_prefix_supports_atomic_pasted_live_matches():
    harness = DisplayHarness()
    harness.native("\x1b[5;1H\u203a \x1b[5;3H".encode())
    assert harness.display(replace(STATE, native_prefix=""))
    assert harness.visible.display[4].rstrip() == "\u203a /ro"


def test_argument_picker_height_shrink_restores_all_moved_native_cells_before_redraw():
    harness = DisplayHarness(rows=16)
    harness.native(NATIVE_MENU.replace(b"5;", b"9;").replace(b"7;", b"11;").replace(b"8;", b"12;"))
    menu = InputInterceptionMenu(INPUT_INTERCEPTORS, "/rod")
    menu.open_arguments()
    assert harness.display(menu.view("/r"))
    harness.visible.resize(lines=8)
    harness._output.feed(harness.renderer.resize(80, 8))
    assert harness.visible.display == harness.renderer.native.screen.display
    assert not any("Press enter" in line for line in harness.visible.display)


def test_semantic_surface_hides_native_activity_and_working_but_keeps_commentary_and_composer():
    harness = DisplayHarness()
    native = (
        b"\x1b[2J\x1b[1;1H"
        b"Hidden historical answer"
        b"\x1b[3;1H\xe2\x80\xa2 Ran secret-command"
        b"\x1b[7;1H\xe2\x80\xa2 Working (4s)"
        b"\x1b[9;1H\xe2\x80\xba Ask Codex to do anything"
        b"\x1b[12;1Hgpt-test max \xc2\xb7 /workspace"
        b"\x1b[9;3H"
    )
    harness.native(native)
    light = PresentationSnapshot(
        1,
        "light",
        PresentationSurface.SEMANTIC,
        "RODEX LIGHT  commentary only",
        (),
        (
            PresentationItemText(
                "root",
                "turn",
                "message",
                "agentMessage",
                "commentary",
                None,
                "Visible human update",
                True,
            ),
        ),
    )
    assert harness.present(light)
    visible = "\n".join(harness.visible.display)
    assert "RODEX LIGHT" in visible
    assert "Visible human update" in visible
    assert "Ask Codex to do anything" in visible
    assert "Hidden historical answer" not in visible
    assert "Ran secret-command" not in visible
    assert "Working" not in visible

    # Native activity continues to update its hidden projection but cannot leak
    # into an unchanged semantic frame.
    rendered = harness.native(b"\x1b[4;1H\xe2\x80\xa2 Edited hidden.py\x1b[9;3H")
    assert rendered == b""
    assert "Edited hidden.py" not in "\n".join(harness.visible.display)


def test_semantic_renderer_uses_configured_heading_and_typed_item_text_without_command_branches():
    harness = DisplayHarness()
    harness.native(b"\x1b[2J\x1b[9;1H\xe2\x80\xba prompt\x1b[9;9H")
    snapshot = PresentationSnapshot(
        revision=1,
        policy_name="future-command-output",
        surface=PresentationSurface.SEMANTIC,
        heading="CONFIGURED COMMAND OUTPUT",
        events=(),
        items=(
            PresentationItemText(
                thread_id="root",
                turn_id="turn",
                item_id="command",
                item_type="commandExecution",
                phase=None,
                status="inProgress",
                text="Visible typed output",
                complete=False,
            ),
        ),
    )

    harness.present(snapshot)
    visible = "\n".join(harness.visible.display)
    assert "CONFIGURED COMMAND OUTPUT" in visible
    assert "Visible typed output" in visible


def test_dark_restores_the_complete_native_screen_after_semantic_filtering():
    harness = DisplayHarness()
    harness.native(b"\x1b[2J\x1b[1;1HNative activity\x1b[9;1H\xe2\x80\xba prompt\x1b[9;9H")
    harness.present(PresentationSnapshot(1, "light", PresentationSurface.SEMANTIC, "RODEX LIGHT", (), ()))
    assert "Native activity" not in "\n".join(harness.visible.display)

    rendered = harness.present(PresentationSnapshot(2, "dark", PresentationSurface.NATIVE, "", (), ()))
    assert rendered
    assert harness.visible.display == harness.renderer.native.screen.display
    assert "Native activity" in "\n".join(harness.visible.display)


def test_semantic_surface_retains_configured_input_menu_navigation():
    harness = DisplayHarness()
    harness.native(NATIVE_MENU)
    harness.present(PresentationSnapshot(1, "light", PresentationSurface.SEMANTIC, "RODEX LIGHT", (), ()))
    assert harness.display(STATE)
    visible = "\n".join(harness.visible.display)
    assert "/rodex" in visible and "issue a rodex command" in visible
    assert harness.visible.display[6].strip() == "/rodex   issue a rodex command"


def test_unknown_native_control_state_is_shown_instead_of_becoming_unusable():
    harness = DisplayHarness()
    harness.native(b"\x1b[2J\x1b[4;1HApproval required\x1b[6;1H1. Allow\x1b[6;1H")
    harness.present(PresentationSnapshot(1, "light", PresentationSurface.SEMANTIC, "RODEX LIGHT", (), ()))
    assert "Approval required" in "\n".join(harness.visible.display)


@pytest.mark.parametrize("rows", [3, 4, 6, 8])
def test_small_argument_viewport_keeps_selected_option_and_fixed_footer_visible(rows):
    harness = DisplayHarness(rows=rows)
    harness.native("\x1b[2;1H\u203a /r".encode())
    menu = InputInterceptionMenu(INPUT_INTERCEPTORS, "/rod")
    menu.open_arguments()
    menu.move_selection(-1)
    assert harness.display(menu.view("/r"))
    assert any("\u203a 3. dusk" in line for line in harness.visible.display)
    assert harness.visible.display[-1].rstrip() == ARGUMENT_MENU_FOOTER
    assert not harness.visible.history.top
    assert harness.display(None)
    assert harness.visible.display == harness.renderer.native.screen.display
