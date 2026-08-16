"""Internal executable entry point for the process hosted inside tmux."""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Sequence
from pathlib import Path

from .analytics import AnalyticsWorkerConfig
from .runtime import run_session_host


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.session_host")
    parser.add_argument("--codex-binary", required=True)
    parser.add_argument("--app-server-socket", required=True, type=Path)
    parser.add_argument("--app-server-log", required=True, type=Path)
    parser.add_argument("--protocol-proxy-socket", required=True, type=Path)
    parser.add_argument("--protocol-event-socket", required=True, type=Path)
    parser.add_argument("--tmux-binary", required=True)
    parser.add_argument("--tmux-server-socket", required=True, type=Path)
    parser.add_argument("--rodex-database", type=Path)
    parser.add_argument("--codex-sessions-root", type=Path)
    parser.add_argument("--rodex-session-uuid")
    parser.add_argument("codex_arguments", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    codex_arguments = list(args.codex_arguments)
    if codex_arguments[:1] == ["--"]:
        codex_arguments.pop(0)
    analytics_values = (
        args.rodex_database,
        args.codex_sessions_root,
        args.rodex_session_uuid,
    )
    if any(value is not None for value in analytics_values) and not all(
        value is not None for value in analytics_values
    ):
        raise SystemExit("analytics arguments must be supplied together")
    analytics_config = (
        None
        if args.rodex_session_uuid is None
        else AnalyticsWorkerConfig(
            rodex_database_path=args.rodex_database,
            codex_sessions_root=args.codex_sessions_root,
            rodex_uuid=uuid.UUID(args.rodex_session_uuid),
        )
    )
    return run_session_host(
        args.codex_binary,
        args.app_server_socket,
        args.app_server_log,
        args.protocol_proxy_socket,
        args.protocol_event_socket,
        args.tmux_binary,
        args.tmux_server_socket,
        codex_arguments,
        analytics_config=analytics_config,
    )


if __name__ == "__main__":
    raise SystemExit(main())
