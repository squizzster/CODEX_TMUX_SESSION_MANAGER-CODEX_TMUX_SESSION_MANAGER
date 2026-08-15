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
    lookup_rodex_session_id_from_a_cool_name,
    lookup_rodex_tmux_session,
    record_a_rodex_session_access,
    update_rodex_tmux_session_name,
)

from .runtime import LiveTmuxSession, RodexRuntimeError, RodexRuntimeLauncher


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
    tmux_binary = shutil.which(configured_tmux)
    if tmux_binary is None:
        raise RodexLaunchError(f"tmux executable was not found: {configured_tmux}")

    resolved_database = (
        Path(database_path).expanduser().resolve()
        if database_path is not None
        else default_rodex_database_path()
    )
    codex_binary = shutil.which(configured_codex)
    runtime_launcher = launcher or RodexRuntimeLauncher(
        codex_binary or configured_codex, tmux_binary
    )
    if _reattach_named_session(arguments, resolved_database, runtime_launcher):
        return 0
    if codex_binary is None:
        raise RodexLaunchError(f"Codex executable was not found: {configured_codex}")

    live_runtime, codex_session_uuid = runtime_launcher.start(Path.cwd(), arguments)
    active_tmux: LiveTmuxSession = live_runtime
    try:
        session = create_a_rodex_session(
            resolved_database,
            codex_session_uuid=codex_session_uuid,
            tmux_server_socket_path=live_runtime.tmux_server_socket_path,
            tmux_session_name=live_runtime.tmux_session_name,
        )
        active_tmux = _prepare_tmux_identity(
            runtime_launcher,
            active_tmux,
            session.cool_name,
            session.id,
            resolved_database,
        )
    except BaseException:
        runtime_launcher.stop(active_tmux, check=False)
        raise

    print(
        f"Rodex {session.cool_name} [{session.rodex_uuid}] -> Codex {codex_session_uuid} "
        f"({active_tmux.tmux_session_name})",
        flush=True,
    )
    runtime_launcher.attach(active_tmux)
    return 0


def _reattach_named_session(
    arguments: list[str],
    database_path: Path,
    launcher: RodexRuntimeLauncher,
) -> bool:
    if len(arguments) != 1 or arguments[0].startswith("-"):
        return False
    cool_name = arguments[0]
    session_id = lookup_rodex_session_id_from_a_cool_name(cool_name, database_path)
    if session_id is None:
        return False
    tmux_link = lookup_rodex_tmux_session(session_id, database_path)
    if tmux_link is None:
        raise RodexLaunchError(f"Rodex session has no tmux endpoint: {cool_name}")
    active_tmux = _prepare_tmux_identity(
        launcher,
        LiveTmuxSession(
            tmux_server_socket_path=Path(tmux_link.tmux_server_socket_path),
            tmux_session_name=tmux_link.tmux_session_name,
        ),
        cool_name,
        session_id,
        database_path,
    )
    record_a_rodex_session_access(session_id, database_path)
    print(f"Reattaching Rodex {cool_name} ({active_tmux.tmux_session_name})", flush=True)
    launcher.attach(active_tmux)
    return True


def _prepare_tmux_identity(
    launcher: RodexRuntimeLauncher,
    active_tmux: LiveTmuxSession,
    cool_name: str,
    session_id: int,
    database_path: Path,
) -> LiveTmuxSession:
    if active_tmux.tmux_session_name != cool_name:
        active_tmux = launcher.rename(active_tmux, cool_name)
        update_rodex_tmux_session_name(session_id, cool_name, database_path)
    launcher.configure_identity_status(active_tmux)
    return active_tmux


def main() -> None:
    try:
        raise SystemExit(run())
    except (RodexLaunchError, RodexRuntimeError, RodexSessionError, OSError) as error:
        print(f"rodex: {error}", file=sys.stderr)
        raise SystemExit(127) from error
