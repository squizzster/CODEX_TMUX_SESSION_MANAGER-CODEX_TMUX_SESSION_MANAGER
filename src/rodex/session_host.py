"""Internal executable entry point for the process hosted inside tmux."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from .runtime import run_session_host


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.session_host")
    parser.add_argument("--codex-binary", required=True)
    parser.add_argument("--app-server-socket", required=True, type=Path)
    parser.add_argument("--app-server-log", required=True, type=Path)
    parser.add_argument("--protocol-proxy-socket", required=True, type=Path)
    parser.add_argument("--tmux-binary", required=True)
    parser.add_argument("--tmux-server-socket", required=True, type=Path)
    parser.add_argument("codex_arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    codex_arguments = list(args.codex_arguments)
    if codex_arguments[:1] == ["--"]:
        codex_arguments.pop(0)
    return run_session_host(
        args.codex_binary,
        args.app_server_socket,
        args.app_server_log,
        args.protocol_proxy_socket,
        args.tmux_binary,
        args.tmux_server_socket,
        codex_arguments,
    )


if __name__ == "__main__":
    raise SystemExit(main())
