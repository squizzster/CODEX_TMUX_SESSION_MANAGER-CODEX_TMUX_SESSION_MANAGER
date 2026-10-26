"""IL/DL preserve horizontal state, including the delayed-autowrap coordinate."""

import pytest
from terminal_tmux_fixture import tmux_terminal

from rodex.terminal_surface import TerminalSurfaceRenderer


@pytest.mark.parametrize("operation", ["L", "M"])
@pytest.mark.parametrize("count", [1, 2, 20])
@pytest.mark.parametrize(
    "row,column,margins", [(1, 4, False), (2, 7, True), (1, 5, True), (5, 6, True), (2, 24, False), (2, 24, True)]
)
def test_line_operation_preserves_cursor_continuation_redraw_and_resize(tmp_path, operation, count, row, column, margins):
    with tmux_terminal(tmp_path, columns=24, rows=8) as terminal:
        renderer = TerminalSurfaceRenderer(24, 8)
        initial = b"first\r\nsecond\x1b[4;1Hfourth"
        if margins:
            initial += b"\x1b[2;4r"
        initial += f"\x1b[{row};{column}H".encode()
        if column == 24:
            initial += b"x"  # x == columns, not columns - 1.
        initial += f"\x1b[{count}{operation}".encode()
        output = renderer.native_output(initial)
        assert output.stream == initial
        native_cpr = terminal.write(initial)
        screen = renderer.native.screen
        assert screen.cursor.x == (24 if column == 24 else column - 1)
        assert native_cpr == renderer.native.feed(b"\x1b[6n").replies
        assert terminal.screen() == ([line.rstrip() for line in screen.display], (screen.cursor.x, screen.cursor.y))
        # A delayed wrap must defer reconstruction until the next printable.
        if column == 24:
            assert renderer.redraw() == b""
        terminal.write(b"X")
        renderer.native_output(b"X")
        expected = terminal.screen()
        assert expected == ([line.rstrip() for line in screen.display], (screen.cursor.x, screen.cursor.y))
        for width in (18, 40, 24):
            terminal.size(width, 8)
            renderer.resize(width, 8)
            assert terminal.write(b"") == renderer.native.feed(b"\x1b[6n").replies
            assert terminal.screen() == ([line.rstrip() for line in screen.display], (screen.cursor.x, screen.cursor.y))
        expected = terminal.screen()
        terminal.write(renderer.redraw())
        assert terminal.screen() == expected
