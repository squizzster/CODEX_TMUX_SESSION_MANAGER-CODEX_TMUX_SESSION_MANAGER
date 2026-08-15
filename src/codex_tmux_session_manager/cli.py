"""Command-line interface for managed Codex tmux sessions."""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from .tmux import SessionError, TmuxSessions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctsm", description="Keep named Codex sessions alive in tmux."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("doctor", help="check local prerequisites")
    commands.add_parser("list", help="list managed sessions")

    start = commands.add_parser("start", help="start a Codex session")
    start.add_argument("name", help="short name for the session")
    start.add_argument(
        "--cwd", type=Path, default=Path.cwd(), help="Codex workspace (default: cwd)"
    )
    start.add_argument("--prompt", help="optional initial Codex prompt")
    start.add_argument(
        "--attach", action="store_true", help="attach immediately after starting"
    )

    for command in ("attach", "stop"):
        subparser = commands.add_parser(command, help=f"{command} a managed session")
        subparser.add_argument("name")
    return parser


def prerequisite_status() -> list[tuple[str, str | None]]:
    return [(command, shutil.which(command)) for command in ("tmux", "codex")]


def run(argv: Sequence[str] | None = None, sessions: TmuxSessions | None = None) -> int:
    args = build_parser().parse_args(argv)
    manager = sessions or TmuxSessions()

    try:
        if args.command == "doctor":
            statuses = prerequisite_status()
            for command, path in statuses:
                print(
                    f"{command:<5} {'ok' if path else 'missing':<7} {path or ''}".rstrip()
                )
            return 0 if all(path for _, path in statuses) else 1

        if args.command == "list":
            found = manager.list()
            if not found:
                print("No managed sessions.")
                return 0
            print(f"{'NAME':<24} {'STATE':<10} WINDOWS")
            for session in found:
                state = "attached" if session.attached else "detached"
                print(f"{session.name:<24} {state:<10} {session.windows}")
            return 0

        if args.command == "start":
            missing = [name for name, path in prerequisite_status() if not path]
            if missing:
                raise SessionError(f"missing prerequisite(s): {', '.join(missing)}")
            manager.start(args.name, args.cwd, args.prompt)
            print(f"Started {args.name!r} in {args.cwd.expanduser().resolve()}")
            if args.attach:
                manager.attach(args.name)
            return 0

        if args.command == "attach":
            manager.attach(args.name)
            return 0

        if args.command == "stop":
            manager.stop(args.name)
            print(f"Stopped {args.name!r}")
            return 0
    except SessionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    raise AssertionError(f"unhandled command: {args.command}")


def main() -> None:
    raise SystemExit(run())
