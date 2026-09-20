"""Real tmux geometry events reach a daemon-style gateway without foreground signals."""

import os
import shlex
import signal
import sys
import time

import pytest
from terminal_tmux_fixture import tmux_terminal

from rodex.runtime import RODEX_SHARED_TMUX_COORDINATION_HOOKS


@pytest.mark.parametrize("foreground_resize", [False, True])
def test_resize_events_cover_layout_navigation_swaps_and_natural_observer_exit(tmp_path, foreground_resize):
    with tmux_terminal(tmp_path, mode="dark", foreground_resize=foreground_resize) as terminal:
        notification = terminal.control.getpeername() + ".resize"
        command = shlex.join(
            [
                sys.executable,
                "-I",
                "-c",
                "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.sendall(b'resize')",
                notification,
            ]
        )
        for event in RODEX_SHARED_TMUX_COORDINATION_HOOKS:
            terminal.tmux("set-hook", "-g", event, shlex.join(["run-shell", "-b", command]))

        def converged():
            expected = list(
                map(
                    int,
                    terminal.tmux("display-message", "-p", "-t", "%0", "#{pane_height},#{pane_width}").strip().split(","),
                )
            )
            deadline = time.monotonic() + 3
            while True:
                terminal.write(b"")
                if terminal.last_size == expected:
                    return expected
                assert time.monotonic() < deadline, (terminal.last_size, expected)
                time.sleep(0.01)

        sibling = terminal.tmux("split-window", "-d", "-v", "-l", "7", "-P", "-F", "#{pane_id}", "sleep 60").strip()
        assert converged() == [15, 80]
        terminal.tmux("next-layout", "-t", "fixture")
        assert converged() in ([23, 39], [23, 40])
        terminal.tmux("previous-layout", "-t", "fixture")
        converged()
        terminal.tmux("resize-pane", "-t", "%0", "-y", "15")
        assert converged() == [15, 80]
        if foreground_resize:
            terminal.tmux("swap-pane", "-d", "-s", "%0", "-t", sibling)
            assert converged() == [7, 80]
        terminal.tmux("resize-window", "-t", "fixture", "-x", "90", "-y", "28")
        converged()
        terminal.tmux("select-layout", "-t", "fixture", "even-vertical")
        converged()
        # This is process exit, with no tmux kill-pane command/hook.
        child = int(terminal.tmux("display-message", "-p", "-t", sibling, "#{pane_pid}"))
        os.kill(child, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while len(terminal.tmux("list-panes", "-t", "fixture").splitlines()) != 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert converged() == [28, 90]
        sibling = terminal.tmux("split-window", "-d", "-v", "-l", "7", "-P", "-F", "#{pane_id}", "sleep 60").strip()
        converged()
        terminal.tmux("kill-pane", "-t", sibling)
        assert converged() == [28, 90]
