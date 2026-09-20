"""Resize expectations come from real tmux, never a second copy of pyte."""

import unicodedata

import pytest
from terminal_tmux_fixture import tmux_terminal

from rodex.native_terminal_projection import NativeTerminalProjection


def assert_matches(terminal, projection):
    display, cursor = terminal.screen()
    assert [unicodedata.normalize("NFC", line.rstrip()) for line in projection.screen.display] == [
        unicodedata.normalize("NFC", line) for line in display
    ]
    assert (projection.screen.cursor.x, projection.screen.cursor.y) == cursor


@pytest.mark.parametrize("cursor_row", [4, 14, 19, 22])
def test_height_shrink_expand_and_relative_update_match_tmux(tmp_path, cursor_row):
    with tmux_terminal(tmp_path) as terminal:
        projection = NativeTerminalProjection(80, 23)
        initial = b"".join(f"\x1b[{row + 1};1Hline {row:02d}".encode() for row in range(23))
        initial += f"\x1b[{cursor_row + 1};3H".encode()
        projection.feed(initial)
        terminal.write(initial)
        assert_matches(terminal, projection)
        for rows in (15, 10, 23, 28, 23):
            terminal.size(80, rows)
            projection.screen.resize(lines=rows, columns=80)
            terminal.write(b"")
            assert_matches(terminal, projection)
            update = b"\x1b[1G\x1b[2AUPDATED COUNTER"
            projection.feed(update)
            terminal.write(update)
            assert_matches(terminal, projection)


@pytest.mark.parametrize("text", ["abcdefghijklmnopqrstuvwxyz" * 4, "abcd 世界 éf " * 10])
def test_width_reflow_and_cursor_follow_wrapped_text(tmp_path, text):
    with tmux_terminal(tmp_path, columns=40, rows=12) as terminal:
        projection = NativeTerminalProjection(40, 12)
        initial = text.encode() + b"\r\nnext line\x1b[2;4H"
        terminal.write(initial)
        projection.feed(initial)
        assert_matches(terminal, projection)
        for columns, rows in ((18, 12), (60, 12), (25, 8), (40, 12)):
            terminal.size(columns, rows)
            projection.screen.resize(lines=rows, columns=columns)
            terminal.write(b"")
            assert_matches(terminal, projection)


def test_scrolling_and_native_clear_bound_resize_restoration(tmp_path):
    with tmux_terminal(tmp_path, columns=40, rows=10) as terminal:
        projection = NativeTerminalProjection(40, 10)
        for content, rows in ((b"line\r\n" * 15, 15), (b"\x1b[2J\x1b[Hfresh", 20), (b"\x1b[3J", 25)):
            terminal.write(content)
            projection.feed(content)
            terminal.size(40, rows)
            projection.screen.resize(lines=rows)
            terminal.write(b"")
            assert_matches(terminal, projection)
