"""Launch a normal Codex TUI with Rodex identity and tmux durability."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from rodex_functions import (
    RodexSessionError,
    create_a_rodex_session,
    default_rodex_database_path,
)

from .runtime import RodexRuntimeError, RodexRuntimeLauncher


class RodexLaunchError(RuntimeError):
    """Rodex could not launch its Codex/tmux runtime."""


def run(
    argv: Sequence[str] | None = None,
    *,
    database_path: str | os.PathLike[str] | None = None,
    launcher: RodexRuntimeLauncher | None = None,
) -> int:
    """Create, register, and attach to one tmux-hosted Codex session."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    configured_codex = os.environ.get("RODEX_CODEX_BINARY", "codex")
    configured_tmux = os.environ.get("RODEX_TMUX_BINARY", "tmux")
    codex_binary = shutil.which(configured_codex)
    tmux_binary = shutil.which(configured_tmux)
    if codex_binary is None:
        raise RodexLaunchError(f"Codex executable was not found: {configured_codex}")
    if tmux_binary is None:
        raise RodexLaunchError(f"tmux executable was not found: {configured_tmux}")

    resolved_database = (
        Path(database_path).expanduser().resolve()
        if database_path is not None
        else default_rodex_database_path()
    )
    runtime_launcher = launcher or RodexRuntimeLauncher(codex_binary, tmux_binary)
    live_runtime, codex_session_uuid = runtime_launcher.start(Path.cwd(), arguments)
    try:
        session = create_a_rodex_session(
            resolved_database,
            codex_session_uuid=codex_session_uuid,
            tmux_server_socket_path=live_runtime.tmux_server_socket_path,
            tmux_session_name=live_runtime.tmux_session_name,
        )
    except BaseException:
        runtime_launcher.stop(live_runtime, check=False)
        raise

    print(
        f"Rodex {session.rodex_uuid} -> Codex {codex_session_uuid} "
        f"({live_runtime.tmux_session_name})",
        flush=True,
    )
    runtime_launcher.attach(live_runtime)
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (RodexLaunchError, RodexRuntimeError, RodexSessionError, OSError) as error:
        print(f"rodex: {error}", file=sys.stderr)
        raise SystemExit(127) from error
