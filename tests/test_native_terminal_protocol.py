"""Terminal-control behavior is identical across presentation policies and read splits."""

import os
import re
import signal

import pytest
from terminal_tmux_fixture import tmux_terminal

from rodex.native_terminal_projection import NativeTerminalProjection
from rodex.presentation_policy import PresentationSnapshot, PresentationSurface
from rodex.terminal_surface import TerminalSurfaceRenderer


def policy(name):
    return PresentationSnapshot(
        1,
        name,
        PresentationSurface.SEMANTIC if name == "light" else PresentationSurface.NATIVE,
        "RODEX LIGHT" if name == "light" else "",
        (),
        (),
    )


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_queries_reply_once_at_query_time_with_every_read_split(mode):
    data = b"\x1b[5;9H\x1b[6n\x1b[2;3H\x1b[?6n\x1b[5n\x1b[06n\x1b[?006n\x1b[005n"
    for split in range(1, len(data)):
        renderer = TerminalSurfaceRenderer()
        renderer.present(policy(mode))
        parts = [renderer.native_output(data[:split]), renderer.native_output(data[split:])]
        assert b"".join(part.replies for part in parts) == (b"\x1b[5;9R\x1b[?2;3R\x1b[0n\x1b[2;3R\x1b[?2;3R\x1b[0n")
        assert all(b"\x1b[6n" not in part.stream and b"\x1b[?6n" not in part.stream for part in parts)


def test_cursor_query_respects_origin_and_delayed_autowrap():
    projection = NativeTerminalProjection(8, 10)
    assert projection.feed(b"12345678\x1b[6n").replies == b"\x1b[1;8R"
    assert projection.feed(b"\x1b[3;8r\x1b[?6h\x1b[2;3H\x1b[6n").replies == b"\x1b[2;3R"


@pytest.mark.parametrize("source,target", [("dark", "light"), ("light", "dark")])
@pytest.mark.parametrize(
    "token",
    [
        b"\x1b[6n",
        b"\x1b[c",
        b"\x1b]10;?\x1b\\",
        b"\x1bP+q544e\x1b\\",
        b"\x1b[?2026h\x1b[1;1Hnative\x1b[?2026l",
        b"\x1b[?02026h\x1b[1;1Hnative\x1b[?02026l",
        b"\x1b[?2026;25h\x1b[1;1Hnative\x1b[?2026;25l",
        "世界".encode(),
    ],
)
def test_policy_handoff_waits_for_complete_terminal_boundary(source, target, token):
    for split in range(1, len(token)):
        renderer = TerminalSurfaceRenderer(80, 12)
        renderer.native_output("\x1b[9;1H\u203a prompt\x1b[9;3H".encode())
        renderer.present(policy(source))
        first = renderer.native_output(token[:split])
        boundary = renderer.native.at_boundary
        transition = renderer.present(policy(target))
        if not boundary:
            assert transition == b""
            assert renderer.presentation_surface == policy(source).surface
        second = renderer.native_output(token[split:])
        assert renderer.presentation_surface == policy(target).surface
        stream = first.stream + second.stream
        if token == b"\x1b[6n":
            assert first.replies + second.replies == b"\x1b[9;3R"
            assert token not in stream
        elif token.startswith((b"\x1b]", b"\x1bP")) or token == b"\x1b[c":
            assert stream.count(token) == 1
        elif token.startswith(b"\x1b[?"):
            opener, closer = re.findall(rb"\x1b\[\?[0-9;]+[hl]", token)
            assert stream.count(opener) == stream.count(closer) == 1


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_real_child_receives_terminal_replies_after_resize_and_mode_change(tmp_path, mode):
    with tmux_terminal(tmp_path / "reference") as reference:
        expected_capabilities = reference.write(b"\x1b[c")
    with tmux_terminal(tmp_path / "gateway", mode=mode) as terminal:
        assert sorted(re.findall(rb"\x1b\[[0-9;?]*[Rc]", terminal.write(b"\x1b[c"))) == sorted(
            re.findall(rb"\x1b\[[0-9;?]*[Rc]", expected_capabilities)
        )
        initial = "\x1b[18;1HWorking counter\x1b[20;1H\u203a Ask Codex\x1b[20;3H".encode()
        assert terminal.write(initial) == b"\x1b[20;3R"
        # Light mode keeps its visible cursor in the composer. Native CPR must
        # describe the child's cursor even when its numeric spelling varies.
        assert terminal.write(b"\x1b[2;3H\x1b[06n") == b"\x1b[2;3R\x1b[2;3R"
        assert terminal.write(b"\x1b[20;3H") == b"\x1b[20;3R"
        terminal.size(80, 15)
        assert terminal.write(b"") == b"\x1b[15;3R"
        assert terminal.write(b"\x1b[1G\x1b[2AUPDATED COUNTER") == b"\x1b[13;16R"
        terminal.size(80, 23)
        assert terminal.write(b"") == b"\x1b[18;16R"
        pane_pid = int(terminal.tmux("display-message", "-p", "-t", "%0", "#{pane_pid}"))
        os.kill(pane_pid, signal.SIGUSR2 if mode == "light" else signal.SIGUSR1)
        assert terminal.write(b"\x1b[5n\x1b[c").startswith(b"\x1b[0n")
        assert terminal.write(b"") == b"\x1b[18;16R"


def test_lost_daemon_resize_hint_leaves_native_coordinates_stale_until_delivered(tmp_path):
    """Negative control for the incident: pane resize alone cannot resize a daemon PTY."""
    with tmux_terminal(tmp_path, mode="dark") as terminal:
        initial = b"\x1b[18;1HWorking counter\x1b[20;1Hcomposer\x1b[20;3H"
        assert terminal.write(initial) == b"\x1b[20;3R"
        terminal.tmux("resize-window", "-x", "80", "-y", "15", "-t", "fixture")
        # A lost hook leaves the child and shared native model at their old height,
        # even though tmux has already moved the visible cursor to the last row.
        assert terminal.write(b"") == b"\x1b[20;3R"
        assert terminal.last_size == [23, 80]
        assert terminal.screen()[1] == (2, 14)
        terminal.size(80, 15)
        assert terminal.write(b"") == b"\x1b[15;3R"
        assert terminal.last_size == [15, 80]
        terminal.write(b"\x1b[1G\x1b[2AUPDATED WORKING COUNTER")
        display, cursor = terminal.screen()
        assert display[12].startswith("UPDATED WORKING COUNTER")
        assert cursor[1] == 12
