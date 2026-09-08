"""Claim the child PTY in a fresh, unthreaded process, then execute the native TUI."""

from __future__ import annotations

import fcntl
import os
import sys
import termios


def main() -> None:
    command = sys.argv[1:]
    if not command:
        raise SystemExit("terminal-exec requires an executable")
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(0, os.getpgrp())
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
