"""Arm a native child to die with ``rodexd``, then replace this interpreter."""

from __future__ import annotations

import argparse
import ctypes
import os
import signal
from collections.abc import Sequence

_PR_SET_PDEATHSIG = 1


def arm_parent_death_signal(expected_parent_pid: int) -> None:
    """Set Linux parent-death SIGTERM and close the race with parent exit."""
    if type(expected_parent_pid) is not int or expected_parent_pid <= 0:
        raise ValueError("expected parent process ID must be positive")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m rodex.process_guard")
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(arguments)
    command = parsed.command[1:] if parsed.command[:1] == ["--"] else parsed.command
    if not command:
        parser.error("an executable command is required after --")
    arm_parent_death_signal(parsed.parent_pid)
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
