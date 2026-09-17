"""Claim the child PTY in a fresh, unthreaded process, then execute the native TUI."""

from __future__ import annotations

import fcntl
import os
import sys
import termios

from .process_guard import arm_parent_death_signal


def main() -> None:
    arguments = sys.argv[1:]
    gate_fd: int | None = None
    expected_parent_pid: int | None = None
    if arguments[:1] == ["--parent-pid"]:
        if len(arguments) < 3 or not arguments[1].isdigit():
            raise SystemExit("terminal-exec received an invalid parent identity")
        expected_parent_pid = int(arguments[1])
        arguments = arguments[2:]
    if arguments[:1] == ["--start-gate-fd"]:
        if len(arguments) < 3 or not arguments[1].isdigit() or arguments[2] != "--":
            raise SystemExit("terminal-exec received an invalid start gate")
        gate_fd = int(arguments[1])
        command = arguments[3:]
    else:
        command = arguments
    if not command:
        raise SystemExit("terminal-exec requires an executable")
    if expected_parent_pid is None:
        raise SystemExit("terminal-exec requires its exact parent identity")
    arm_parent_death_signal(expected_parent_pid)
    if gate_fd is not None:
        try:
            if os.read(gate_fd, 1) != b"1":
                raise SystemExit("terminal-exec start gate closed before release")
        finally:
            os.close(gate_fd)
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(0, os.getpgrp())
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
