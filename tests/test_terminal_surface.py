"""Pure Python terminal transcripts: no Codex, Rodex, tmux, sockets, or child processes."""

from dataclasses import replace

import pyte
import pytest

from rodex.input_interceptor_config import INPUT_INTERCEPTORS
from rodex.input_menu import ARGUMENT_MENU_FOOTER, InputInterceptionMenu, InputMenuRow, InputMenuView
from rodex.presentation_policy import PresentationItemText, PresentationSnapshot, PresentationSurface
from rodex.prompt_expansion_display import PromptExpansion
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
        output = rendered.stream + rendered.frame
        self._output.feed(output)
        return output

    def display(self, state):
        accepted, rendered = self.renderer.display(state)
        self._output.feed(rendered)
        return accepted

    def present(self, snapshot):
        rendered = self.renderer.present(snapshot)
        self._output.feed(rendered)
        return rendered

    def expansion(self, text, original="push"):
        rendered = self.renderer.show_prompt_expansion(None if text is None else PromptExpansion(original, text))
        self._output.feed(rendered)
        return rendered


@pytest.mark.parametrize("surface", list(PresentationSurface))
def test_prompt_expansion_is_visible_but_never_changes_native_composer_or_history(surface):
    harness = DisplayHarness()
    harness.native(NATIVE_MENU)
    harness.present(PresentationSnapshot(1, "test", surface, "heading", (), ()))
    before = harness.visible.display
    cursor = (harness.visible.cursor.y, harness.visible.cursor.x)
    harness.expansion("Commit/stash → Fetch/prune → Switch main")
    assert harness.visible.display[2].rstrip() == "push => Commit/stash → Fetch/prune → Switch main"
    for cell in harness.visible.buffer[2].values():
        assert cell.fg == cell.bg == "default"
        assert not cell.bold and not cell.blink
    assert "Rodex expanded input:" not in "\n".join(harness.visible.display)
    assert "translated" not in "\n".join(harness.visible.display)
    assert "Commit/stash" in "\n".join(harness.visible.display)
    assert harness.renderer.native.screen.display[4].rstrip() == "\u203a /r"
    assert (harness.visible.cursor.y, harness.visible.cursor.x) == cursor
    harness.expansion(None)
    assert harness.visible.display == before
    assert not harness.visible.history.top


@pytest.mark.parametrize("keyword", ["push", "review", "future-keyword", "検証"])
@pytest.mark.parametrize("glyph", ["\u203a", "»"])
def test_expansion_uses_actual_keyword_directly_beneath_native_echo(keyword, glyph):
    harness = DisplayHarness()
    native = f"\x1b[2J\x1b[2;1H{glyph} {keyword}\x1b[7;1H{glyph} draft".encode()
    harness.native(native)
    before = harness.visible.display
    cursor = (harness.visible.cursor.y, harness.visible.cursor.x)
    harness.expansion("Expanded instruction", original=keyword)
    assert harness.visible.display[1].rstrip() == f"{glyph} {keyword}"
    assert harness.visible.display[2].rstrip() == f"{keyword} => Expanded instruction"
    assert sum("=>" in row for row in harness.visible.display) == 1
    assert harness.renderer.native.screen.display == before
    assert (harness.visible.cursor.y, harness.visible.cursor.x) == cursor
    harness.expansion(None)
    assert harness.visible.display == before


def test_expansion_wraps_one_notice_and_does_not_displace_native_output_for_placement():
    harness = DisplayHarness(columns=30)
    harness.native("\x1b[2J\x1b[2;1H\u203a review\x1b[3;1HNative response\x1b[10;1H\u203a draft".encode())
    before = harness.visible.display
    harness.expansion("Inspect changes and report findings.\nKeep working.", original="review")
    assert harness.visible.display[1].rstrip() == "\u203a review"
    assert harness.visible.display[2].rstrip() == "Native response"
    notice = "\n".join(harness.visible.display)
    assert "review => Inspect changes" in notice
    assert "Keep working." in notice
    assert "translated" not in notice
    harness.expansion(None)
    assert harness.visible.display == before
    assert not harness.visible.history.top


def test_expansion_truncates_within_six_rows_and_sanitizes_original_too():
    harness = DisplayHarness(columns=30)
    harness.native("\x1b[2J\x1b[10;1H\u203a draft".encode())
    rendered = harness.expansion("line\n" * 30, original="other\x1b]52;c;secret\x07\nkeyword")
    assert b"\x1b]52" not in rendered and b"\x07" not in rendered
    assert harness.visible.display[7].rstrip().endswith("…")
    assert harness.visible.display[8].strip() == ""
    assert harness.visible.display[9].rstrip() == "\u203a draft"
    assert not harness.visible.history.top


def test_expansion_placement_follows_latest_visible_echo_and_native_redraw():
    harness = DisplayHarness()
    harness.native("\x1b[2J\x1b[2;1H\u203a review\x1b[5;1H\u203a review\x1b[10;1H\u203a draft".encode())
    harness.expansion("Expanded instruction", original="review")
    assert harness.visible.display[2].strip() == ""
    assert harness.visible.display[5].rstrip() == "review => Expanded instruction"
    harness.native("\x1b[2J\x1b[3;1H\u203a review\x1b[8;1H\u203a draft".encode())
    assert harness.visible.display[3].rstrip() == "review => Expanded instruction"
    assert sum("review =>" in row for row in harness.visible.display) == 1
    harness.expansion(None)
    assert harness.visible.display == harness.renderer.native.screen.display
    assert not harness.visible.history.top


@pytest.mark.parametrize("blank_rows", [0, 1, 2])
def test_long_expansion_never_covers_its_echo_or_the_following_native_status(blank_rows):
    harness = DisplayHarness(columns=30)
    status_row = 3 + blank_rows
    composer_row = status_row + 1
    harness.native(
        f"\x1b[2J\x1b[2;1H\u203a push\x1b[{status_row};1HNative status\x1b[{composer_row};1H\u203a draft".encode()
    )
    before = harness.visible.display
    harness.expansion("a long expanded instruction " * 10)
    assert harness.visible.display[1].rstrip() == "\u203a push"
    assert harness.visible.display[status_row - 1].rstrip() == "Native status"
    if blank_rows:
        assert harness.visible.display[2].startswith("push => ")
        assert harness.visible.display[status_row - 2].rstrip().endswith("…")
    else:
        assert harness.visible.display == before
    harness.expansion(None)
    assert harness.visible.display == before


def test_prompt_expansion_sanitizes_controls_restores_on_scroll_and_yields_to_menu():
    harness = DisplayHarness()
    harness.native(NATIVE_MENU)
    rendered = harness.expansion("世界 é \x1b]52;c;secret\x07\x1b[2J")
    assert b"\x1b]52" not in rendered and b"\x07" not in rendered
    assert "世界 é" in "\n".join(harness.visible.display)  # pyte normalizes combining accents.
    assert harness.display(STATE)
    assert "push =>" not in "\n".join(harness.visible.display)
    harness.display(None)
    assert "push =>" in "\n".join(harness.visible.display)
    harness._output.feed(harness.renderer.suspend())
    assert harness.visible.display == harness.renderer.native.screen.display
    harness._output.feed(harness.renderer.resume())
    harness.native(b"\x1b[12;1H\r\n" * 15)
    history = "".join(cell.data for line in harness.visible.history.top for cell in line.values())
    assert "push =>" not in history and "secret" not in history


def test_initial_prompt_display_uses_composer_boundary_not_cursor_position():
    harness = DisplayHarness()
    harness.native(NATIVE_MENU + b"\x1b[1;1H\x1b[?25l")
    harness.expansion("initial argv expansion")
    assert "push =>" in "\n".join(harness.visible.display)
    assert (harness.visible.cursor.y, harness.visible.cursor.x) == (0, 0)
    assert harness.visible.cursor.hidden
    harness.native(b"\x1b[2J\x1b[1;1HApproval dialog")
    assert "push =>" not in "\n".join(harness.visible.display)


@pytest.mark.parametrize("frame", [b"\x1b]0;title\x1b\\", b"\x1bPopaque\x1b\\", "世界".encode()])
def test_prompt_expansion_change_or_expiry_never_splits_native_tokens(frame):
    for split in range(1, len(frame)):
        harness = DisplayHarness()
        harness.native(NATIVE_MENU)
        harness.expansion("expanded")
        first = harness.native(frame[:split])
        inside_token = not harness.renderer.native.at_boundary
        if inside_token:
            assert harness.expansion(None) == b""
            assert harness.expansion("new expansion") == b""
        second = harness.native(frame[split:])
        if inside_token:
            assert frame in first + second
        harness.expansion(None)
        assert harness.visible.display == harness.renderer.native.screen.display


@pytest.mark.parametrize("draft", ["/ro", "/rod", "/rode", "/rodex"])
@pytest.mark.parametrize("glyph", ["\u203a", "»"])
def test_surface_renderer_is_inline_beneath_visible_draft_and_restores_native_exactly(draft, glyph):
    harness = DisplayHarness()
    native_menu = NATIVE_MENU.replace("\u203a".encode(), glyph.encode())
    assert harness.native(native_menu) == native_menu
    native_display = harness.visible.display
    assert harness.display(replace(STATE, draft=draft))
    assert harness.visible.display[0].rstrip() == "Native conversation"
    assert harness.visible.display[4].rstrip() == f"{glyph} {draft}"
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
        # CSI/escape prefixes may be held until classified, but the opaque
        # control string must remain contiguous through arbitrary read splits.
        assert frame in first.stream + first.frame + second.stream + second.frame
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
    assert renderer.native_output(b"unchanged").stream == b"unchanged"


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


def test_semantic_frame_with_no_transcript_space_does_not_scroll_the_composer():
    harness = DisplayHarness(rows=3)
    harness.native("\x1b[2;1H\u203a prompt\x1b[2;3H".encode())
    item = PresentationItemText("root", "turn", "message", "agentMessage", "commentary", None, "hidden\n" * 20, True)
    rendered = harness.present(PresentationSnapshot(1, "light", PresentationSurface.SEMANTIC, "RODEX LIGHT", (), (item,)))
    assert b"hidden" not in rendered
    assert harness.visible.display[1].rstrip() == "\u203a prompt"
    assert not harness.visible.history.top


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
