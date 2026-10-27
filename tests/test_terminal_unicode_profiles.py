"""Profile-specific Unicode occupancy is established by real tmux, including its quirks."""

import pytest
from terminal_tmux_fixture import tmux_terminal

from rodex.input_menu import InputMenuRow, InputMenuView
from rodex.native_composer import matches_composer
from rodex.native_terminal_projection import NativeTerminalProjection
from rodex.terminal_cells import TMUX_32_PROFILE, TMUX_34_PROFILE, terminal_profile
from rodex.terminal_surface import TerminalSurfaceRenderer, _clip_cells, _terminal_safe_text, _wrap_cells

CLUSTERS = ["☺️", "👩\u200d💻", "e\u0301", "👨\u200d👩\u200d👧\u200d👦", "A\u200d💻", "👩\u200dA", "👍🏽", "🇬🇧", "☺︎"]


def assert_native(terminal, projection):
    screen = projection.screen
    assert terminal.screen() == ([line.rstrip() for line in screen.display], (screen.cursor.x, screen.cursor.y))


@pytest.mark.parametrize("text", CLUSTERS)
@pytest.mark.parametrize("prefix", ["", "x" * 10, "x" * 11])
def test_native_clusters_at_every_byte_split_and_margin(tmp_path, text, prefix):
    with tmux_terminal(tmp_path, columns=12, rows=8) as terminal:
        data = (prefix + text).encode()
        native_cpr = terminal.write(data)
        expected = terminal.screen()
        for split in range(len(data) + 1):
            projection = NativeTerminalProjection(12, 8, profile=terminal.profile)
            first, second = projection.feed(data[:split]), projection.feed(data[split:])
            assert first.native + second.native == data
            assert projection.feed(b"\x1b[6n").replies == native_cpr
            assert expected == (
                [line.rstrip() for line in projection.screen.display],
                (projection.screen.cursor.x, projection.screen.cursor.y),
            )
        terminal.write(b"!")
        projection.feed(b"!")
        assert_native(terminal, projection)


@pytest.mark.parametrize("text", CLUSTERS[:4])
@pytest.mark.parametrize("edit", ["@", "P", "X", "2@", "2P", "2X", "K"])
@pytest.mark.parametrize("column", [2, 3, 4])
def test_cluster_edits_and_reflow_preserve_explicit_grid_cells(tmp_path, text, edit, column):
    with tmux_terminal(tmp_path, columns=24, rows=8) as terminal:
        renderer = TerminalSurfaceRenderer(24, 8, profile=terminal.profile)
        data = ("a" + text + "!tail\x1b[1;" + str(column) + "H\x1b[" + edit).encode()
        terminal.write(data)
        renderer.native_output(data)
        assert_native(terminal, renderer.native)
        for width in (8, 30, 24):
            terminal.size(width, 8)
            renderer.resize(width, 8)
            assert terminal.write(b"") == renderer.native.feed(b"\x1b[6n").replies
            assert_native(terminal, renderer.native)
        expected = terminal.screen()
        terminal.write(renderer.redraw())
        assert terminal.screen() == expected
        terminal.write(b"Z")
        renderer.native_output(b"Z")
        assert_native(terminal, renderer.native)


@pytest.mark.parametrize(
    "text",
    ["hello ☺️!", "hello 👩\u200d💻!", "abcde\x1b[1;2H☺️!", "x" * 11 + "e\u0301", "x" * 10 + "☺️", "a" * 9 + "界e\u0301"],
)
def test_redraw_reconstructs_clusters_and_overlapping_native_cells(tmp_path, text):
    with tmux_terminal(tmp_path, columns=12, rows=8) as terminal:
        renderer = TerminalSurfaceRenderer(12, 8, profile=terminal.profile)
        data = (text + "\x1b[3;3H").encode()  # Safe cursor, including a cluster at the right margin.
        terminal.write(data)
        renderer.native_output(data)
        assert_native(terminal, renderer.native)
        expected = terminal.screen()
        terminal.write(renderer.redraw())
        assert terminal.screen() == expected


@pytest.mark.parametrize("profile,width", [(TMUX_32_PROFILE, 4), (TMUX_34_PROFILE, 2)])
def test_cluster_clipping_wrapping_and_sanitizing_use_profile_occupancy(profile, width):
    text = "👩\u200d💻"
    assert _terminal_safe_text(text) == text
    assert profile.text_width(text) == width
    assert _clip_cells(text + "!", width, profile=profile) == text
    assert _wrap_cells(text + "!", width, profile=profile) == [text, "!"]
    assert _clip_cells("☺️", 1, profile=profile) == ("☺️" if profile == TMUX_32_PROFILE else "")


@pytest.mark.parametrize("text", ["👩\u200d💻", "☺️", "世界", "e\u0301", "👨\u200d👩\u200d👧\u200d👦"])
def test_composer_anchor_uses_cells_instead_of_unicode_string_indexes(tmp_path, text):
    with tmux_terminal(tmp_path, columns=40, rows=8) as terminal:
        renderer = TerminalSurfaceRenderer(40, 8, profile=terminal.profile)
        initial = ("\x1b[4;1H\u203a " + text).encode()
        terminal.write(initial)
        renderer.native_output(initial)
        display, cursor = terminal.screen()
        assert matches_composer((display[3],), cursor[0], text, 40, profile=terminal.profile)
        state = InputMenuView(text + "x", text, (InputMenuRow("test", "description"),), 0)
        accepted, paint = renderer.display(state)
        assert accepted
        terminal.write(paint)
        assert terminal.screen()[1][0] == 2 + terminal.profile.text_width(text + "x")
        _, restore = renderer.display(None)
        terminal.write(restore)
        assert terminal.screen() == (display, cursor)


def test_unknown_profile_is_explicit_and_leaves_queries_and_display_to_native():
    profile = terminal_profile("future")
    assert not profile.verified and profile.name == "unverified-tmux-future"
    renderer = TerminalSurfaceRenderer(profile=profile)
    data = "👩\u200d💻\x1b[6n".encode()
    output = renderer.native_output(data)
    assert output.stream == data and not output.replies and not renderer.redraw()
    assert not matches_composer(("\u203a test",), 6, "test", 80, profile=profile)


@pytest.mark.parametrize("columns", [1, 2])
def test_tiny_viewports_defer_reconstruction_until_clusters_can_be_staged(columns):
    renderer = TerminalSurfaceRenderer(columns, 8)
    assert renderer.native_output(b"A\r").stream == b"A\r"
    assert not renderer.redraw()
    assert renderer.resize(3, 8)


@pytest.mark.parametrize("text", ["👩\u200d💻", "☺️", "e\u0301"])
def test_gateway_profile_and_delivered_resize_match_native(tmp_path, text):
    data = ("prefix " + text * 5 + " suffix\r\n\u203a " + text).encode()
    with tmux_terminal(tmp_path / "reference", columns=24, rows=8) as reference:
        expected = []
        initial_cpr = reference.write(data)
        initial_screen = reference.screen()
        for width in (12, 32, 24):
            reference.size(width, 8)
            cpr = reference.write(b"!\x1b[2;3H")
            expected.append((cpr, reference.screen()))
    with tmux_terminal(tmp_path / "gateway", columns=24, rows=8, mode="dark") as gateway:
        assert gateway.write(data) == initial_cpr
        assert gateway.screen() == initial_screen
        for width, (cpr, screen) in zip((12, 32, 24), expected, strict=True):
            gateway.size(width, 8)
            assert gateway.write(b"!\x1b[2;3H") == cpr
            assert gateway.screen() == screen


@pytest.mark.parametrize("text", ["☺️🏽!", "☺️🇬🇧!", "☺️\u200d💻!", "a" + "\u0301" * 10, "a" + "\u0301" * 11])
def test_generated_text_uses_native_grid_continuation_and_cluster_capacity(tmp_path, text):
    with tmux_terminal(tmp_path, columns=40, rows=8) as terminal:
        projection = NativeTerminalProjection(40, 8, profile=terminal.profile)
        projection.feed(text.encode())
        assert terminal.write(text.encode()) == projection.feed(b"\x1b[6n").replies
        assert_native(terminal, projection)
        assert terminal.profile.text_width(text) == projection.screen.cursor.x
        clipped = _clip_cells(text, projection.screen.cursor.x, profile=terminal.profile)
        assert clipped == (
            text[:-1] if not terminal.profile.joined_emoji and len(text.encode()) > 21 and text.startswith("a") else text
        )


@pytest.mark.parametrize("text", ["x" * 30, "abc👩\u200d💻" * 8, "x" * 23 + "猫" + "e\u0301" * 10])
def test_delivered_resize_reconstructs_authoritative_wraps_after_redraw(tmp_path, text):
    with tmux_terminal(tmp_path, columns=24, rows=8) as terminal:
        profile = terminal.profile
        terminal.write(text.encode())
        reference = []
        for width, rows in [(18, 8), (30, 10), (24, 8)]:
            terminal.size(width, rows)
            terminal.write(b"Z")
            reference.append(terminal.screen())
        terminal.size(24, 8)
        terminal.write(b"\x1bc\x1b[3J")
        renderer = TerminalSurfaceRenderer(24, 8, profile=profile)
        rendered = renderer.native_output(text.encode())
        terminal.write(rendered.stream + rendered.frame)
        terminal.write(renderer.redraw())
        for (width, rows), expected in zip([(18, 8), (30, 10), (24, 8)], reference, strict=True):
            terminal.size(width, rows)
            terminal.write(renderer.resize(width, rows))
            rendered = renderer.native_output(b"Z")
            terminal.write(rendered.stream + rendered.frame)
            assert terminal.screen() == expected
            assert_native(terminal, renderer.native)


@pytest.mark.parametrize("count", [1, 2])
def test_trailing_orphan_padding_survives_redraw_and_following_combining(tmp_path, count):
    with tmux_terminal(tmp_path, columns=12, rows=8) as terminal:
        data = b"a" * (12 - count * 2) + ("界" * count).encode()
        for index in range(count):
            data += f"\x1b[1;{13 - count * 2 + index}H\x1b[P".encode()
        data += f"\x1b[1;{13 - count * 2}H\x1b[{count}@\x1b[3;2H".encode()
        following = "\x1b[1;12H\u0301".encode()
        terminal.write(data + following)
        expected = terminal.screen()
        terminal.write(b"\x1bc\x1b[3J")
        renderer = TerminalSurfaceRenderer(12, 8, profile=terminal.profile)
        renderer.native_output(data)
        terminal.write(data)
        terminal.write(renderer.redraw())
        output = renderer.native_output(following)
        terminal.write(output.stream + output.frame)
        assert terminal.screen() == expected
        assert_native(terminal, renderer.native)
