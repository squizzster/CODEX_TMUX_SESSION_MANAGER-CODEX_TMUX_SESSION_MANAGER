"""Exec one command after removing every ambient environment name not authorized."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from .process_environment import select_exact_process_environment


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m rodex.environment_exec")
    parser.add_argument("--environment-name", action="append", default=[])
    parser.add_argument("--operation-id", help="creation identity retained in tmux's pane launch command")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(arguments)
    if parsed.operation_id is not None and (
        len(parsed.operation_id) != 32 or any(character not in "0123456789abcdef" for character in parsed.operation_id)
    ):
        parser.error("--operation-id must be 32 lowercase hexadecimal characters")
    command = parsed.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("an executable command is required after --")
    environment = select_exact_process_environment(
        os.environ,
        parsed.environment_name,
    )
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    main()
