"""Launch a normal Codex TUI with Rodex identity and tmux durability."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from cool_name import CoolNameError
from rodex_functions import (
    RodexSessionError,
    assign_a_user_defined_cool_name,
    create_a_rodex_session,
    default_rodex_database_path,
    list_rodex_session_runtimes_for_a_user,
    lookup_codex_uuid_from_a_rodex_session_id,
    lookup_rodex_session_id_from_a_cool_name,
    lookup_rodex_session_names,
    lookup_rodex_tmux_session,
    record_a_rodex_session_access,
    record_a_rodex_session_runtime_resume,
    update_rodex_tmux_session_name,
)

from .runtime import LiveTmuxSession, RodexRuntimeError, RodexRuntimeLauncher


class RodexLaunchError(RuntimeError):
    """Rodex could not launch its Codex/tmux runtime."""


_RUNNING_COMMANDS = frozenset({"running", "--running"})
_ALIAS_COMMANDS = frozenset({"alias", "--alias"})
_FORCE_FLAGS = frozenset({"-f", "--f", "-force", "--force"})


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
    if _run_reserved_command(arguments, resolved_database, runtime_launcher):
        return 0
    if _open_named_session(
        arguments,
        resolved_database,
        runtime_launcher,
        codex_available=codex_binary is not None,
    ):
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


def _open_named_session(
    arguments: list[str],
    database_path: Path,
    launcher: RodexRuntimeLauncher,
    *,
    codex_available: bool,
) -> bool:
    if len(arguments) != 1 or arguments[0].startswith("-"):
        return False
    cool_name = arguments[0]
    session_id = lookup_rodex_session_id_from_a_cool_name(cool_name, database_path)
    if session_id is None:
        return False
    names = lookup_rodex_session_names(session_id, database_path)
    if names is None:
        raise RodexLaunchError(f"Rodex session disappeared: {cool_name}")
    display_name = names.display_name
    tmux_link = lookup_rodex_tmux_session(session_id, database_path)
    if tmux_link is None:
        raise RodexLaunchError(f"Rodex session has no tmux endpoint: {cool_name}")
    recorded_tmux = LiveTmuxSession(
        tmux_server_socket_path=Path(tmux_link.tmux_server_socket_path),
        tmux_session_name=tmux_link.tmux_session_name,
    )
    if launcher.session_exists(recorded_tmux):
        active_tmux = _prepare_tmux_identity(
            launcher,
            recorded_tmux,
            display_name,
            session_id,
            database_path,
        )
        record_a_rodex_session_access(session_id, database_path)
        print(
            f"Reattaching Rodex {display_name} ({active_tmux.tmux_session_name})",
            flush=True,
        )
        launcher.attach(active_tmux)
        return True

    if not codex_available:
        configured_codex = os.environ.get("RODEX_CODEX_BINARY", "codex")
        raise RodexLaunchError(f"Codex executable was not found: {configured_codex}")
    codex_session_uuid = lookup_codex_uuid_from_a_rodex_session_id(
        session_id, database_path
    )
    if codex_session_uuid is None:
        raise RodexLaunchError(f"Rodex session has no Codex identity: {cool_name}")

    resumed_runtime, observed_codex_uuid = launcher.start(
        Path.cwd(), ["resume", str(codex_session_uuid)]
    )
    active_tmux: LiveTmuxSession = resumed_runtime
    try:
        if observed_codex_uuid != codex_session_uuid:
            raise RodexLaunchError(
                "Codex resumed an unexpected session: "
                f"expected {codex_session_uuid}, observed {observed_codex_uuid}"
            )
        active_tmux = _prepare_tmux_identity(
            launcher,
            active_tmux,
            display_name,
            session_id,
            database_path,
            persist_name=False,
        )
        record_a_rodex_session_runtime_resume(
            session_id,
            active_tmux.tmux_server_socket_path,
            active_tmux.tmux_session_name,
            database_path,
        )
    except BaseException:
        launcher.stop(active_tmux, check=False)
        raise

    print(
        f"Resumed Rodex {display_name} -> Codex {codex_session_uuid} "
        f"({active_tmux.tmux_session_name})",
        flush=True,
    )
    launcher.attach(active_tmux)
    return True


def _run_reserved_command(
    arguments: list[str],
    database_path: Path,
    launcher: RodexRuntimeLauncher,
) -> bool:
    if not arguments:
        return False
    command = arguments[0]
    if command in _RUNNING_COMMANDS:
        if len(arguments) != 1:
            raise RodexLaunchError("usage: rodex running")
        _print_running_sessions(database_path, launcher)
        return True
    if command in _ALIAS_COMMANDS:
        force, operands = _parse_alias_arguments(arguments[1:])
        if len(operands) != 2:
            raise RodexLaunchError(
                "usage: rodex alias [-f|--force] SESSION_NAME USER_DEFINED_NAME"
            )
        names = assign_a_user_defined_cool_name(
            operands[0], operands[1], database_path, force=force
        )
        tmux_link = lookup_rodex_tmux_session(names.rodex_sessions_id, database_path)
        if tmux_link is not None:
            recorded_tmux = LiveTmuxSession(
                tmux_server_socket_path=Path(tmux_link.tmux_server_socket_path),
                tmux_session_name=tmux_link.tmux_session_name,
            )
            if launcher.session_exists(recorded_tmux):
                _prepare_tmux_identity(
                    launcher,
                    recorded_tmux,
                    names.display_name,
                    names.rodex_sessions_id,
                    database_path,
                )
        print(f"Rodex name: {names.display_name}", flush=True)
        return True
    return False


def _parse_alias_arguments(arguments: list[str]) -> tuple[bool, list[str]]:
    force = False
    operands: list[str] = []
    for argument in arguments:
        if argument in _FORCE_FLAGS:
            force = True
        elif argument.startswith("-"):
            raise RodexLaunchError(f"unknown alias option: {argument}")
        else:
            operands.append(argument)
    return force, operands


def _print_running_sessions(
    database_path: Path,
    launcher: RodexRuntimeLauncher,
) -> None:
    persisted = list_rodex_session_runtimes_for_a_user(database_path)
    running = [
        runtime
        for runtime in persisted
        if launcher.session_exists(
            LiveTmuxSession(
                tmux_server_socket_path=Path(runtime.tmux_server_socket_path),
                tmux_session_name=runtime.tmux_session_name,
            )
        )
    ]
    if not running:
        print("No running Rodex sessions.", flush=True)
        return
    print(f"Running Rodex sessions: {len(running)}", flush=True)
    for runtime in running:
        print(
            f"{runtime.display_name} -> Codex {runtime.codex_session_uuid}",
            flush=True,
        )


def _prepare_tmux_identity(
    launcher: RodexRuntimeLauncher,
    active_tmux: LiveTmuxSession,
    cool_name: str,
    session_id: int,
    database_path: Path,
    *,
    persist_name: bool = True,
) -> LiveTmuxSession:
    if active_tmux.tmux_session_name != cool_name:
        active_tmux = launcher.rename(active_tmux, cool_name)
        if persist_name:
            update_rodex_tmux_session_name(session_id, cool_name, database_path)
    launcher.configure_identity_status(active_tmux)
    return active_tmux


def main() -> None:
    try:
        raise SystemExit(run())
    except (
        CoolNameError,
        RodexLaunchError,
        RodexRuntimeError,
        RodexSessionError,
        OSError,
    ) as error:
        print(f"rodex: {error}", file=sys.stderr)
        raise SystemExit(127) from error
