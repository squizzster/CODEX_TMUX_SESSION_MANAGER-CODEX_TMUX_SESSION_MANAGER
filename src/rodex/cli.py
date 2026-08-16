"""Launch a normal Codex TUI with Rodex identity and tmux durability."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from cool_name import CoolNameError, normalise_rodex_display_name
from rodex_functions import (
    RodexSessionError,
    create_a_rodex_session,
    default_rodex_database_path,
    list_rodex_session_runtimes_for_a_user,
    lookup_codex_uuid_from_a_rodex_session_id,
    lookup_owned_rodex_session_id_from_a_cool_name,
    lookup_rodex_session_id_from_a_cool_name,
    lookup_rodex_session_names,
    lookup_rodex_tmux_session,
    lookup_rodex_uuid_from_an_id,
    open_a_user_defined_cool_name_assignment,
    record_a_rodex_session_access,
    record_a_rodex_session_runtime_resume,
    update_rodex_tmux_session_name,
)
from rodex_sql import RodexSQLError

from .control import CodexControlClient, LiveRodexControl, RodexControlError
from .runtime import LiveTmuxSession, RodexRuntimeError, RodexRuntimeLauncher


class RodexLaunchError(RuntimeError):
    """Rodex could not launch its Codex/tmux runtime."""


class RodexExecutableNotFoundError(RodexLaunchError):
    """A required executable could not be resolved from PATH."""


_RUNNING_COMMANDS = frozenset({"running", "--running", "sessions", "--sessions"})
_ALIAS_COMMANDS = frozenset({"alias", "--alias"})
_SEND_COMMANDS = frozenset({"send", "--send"})
_TAIL_COMMANDS = frozenset({"tail", "--tail"})
_WAIT_COMMANDS = frozenset({"wait", "--wait"})
_FORCE_FLAGS = frozenset({"-f", "--f", "-force", "--force"})
_CREATE_FLAGS = frozenset({"--c", "-create", "--create"})
_DETACH_FLAGS = frozenset({"-d", "--d", "-detach", "--detach"})


def run(
    argv: Sequence[str] | None = None,
    *,
    database_path: str | os.PathLike[str] | None = None,
    launcher: RodexRuntimeLauncher | None = None,
    control_client: CodexControlClient | None = None,
) -> int:
    """Create, register, and attach to one tmux-hosted Codex session."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    configured_codex = os.environ.get("RODEX_CODEX_BINARY", "codex")
    configured_tmux = os.environ.get("RODEX_TMUX_BINARY", "tmux")
    tmux_binary = shutil.which(configured_tmux)
    if tmux_binary is None:
        raise RodexExecutableNotFoundError(
            f"tmux executable was not found: {configured_tmux}"
        )

    resolved_database = (
        Path(database_path).expanduser().resolve()
        if database_path is not None
        else default_rodex_database_path()
    )
    codex_binary = shutil.which(configured_codex)
    runtime_launcher = launcher or RodexRuntimeLauncher(
        codex_binary or configured_codex, tmux_binary
    )
    runtime_control = control_client or CodexControlClient()
    if _run_reserved_command(
        arguments,
        resolved_database,
        runtime_launcher,
        runtime_control,
    ):
        return 0
    codex_arguments, requested_name, detach = _parse_launch_arguments(arguments)
    if _open_named_session(
        codex_arguments,
        resolved_database,
        runtime_launcher,
        codex_available=codex_binary is not None,
        detach=detach,
    ):
        return 0
    if codex_binary is None:
        raise RodexExecutableNotFoundError(
            f"Codex executable was not found: {configured_codex}"
        )

    if requested_name is not None:
        requested_name = normalise_rodex_display_name(requested_name)
        if (
            lookup_rodex_session_id_from_a_cool_name(requested_name, resolved_database)
            is not None
        ):
            raise RodexLaunchError(f"Rodex session name already exists: {requested_name}")

    live_runtime, codex_session_uuid = runtime_launcher.start(Path.cwd(), codex_arguments)
    active_tmux: LiveTmuxSession = live_runtime
    try:
        session = create_a_rodex_session(
            resolved_database,
            codex_session_uuid=codex_session_uuid,
            tmux_server_socket_path=live_runtime.tmux_server_socket_path,
            tmux_session_name=live_runtime.tmux_session_name,
        )
        display_name = session.cool_name
        if requested_name is None:
            active_tmux = _rename_tmux_identity(runtime_launcher, active_tmux, display_name)
            update_rodex_tmux_session_name(
                session.id, active_tmux.tmux_session_name, resolved_database
            )
        else:
            with open_a_user_defined_cool_name_assignment(
                session.cool_name, requested_name, resolved_database
            ) as assignment:
                active_tmux = _rename_tmux_identity(
                    runtime_launcher, active_tmux, assignment.names.display_name
                )
                assignment.renamed_tmux_session_name = active_tmux.tmux_session_name
            display_name = assignment.names.display_name
        runtime_launcher.configure_identity_status(active_tmux)
    except BaseException:
        runtime_launcher.stop(active_tmux, check=False)
        raise

    if detach:
        _print_detached_runtime(display_name, session.rodex_uuid, codex_session_uuid)
        return 0
    print(
        f"Rodex {display_name} [{session.rodex_uuid}] -> Codex {codex_session_uuid} "
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
    detach: bool,
) -> bool:
    if len(arguments) != 1 or arguments[0].startswith("-"):
        return False
    cool_name = arguments[0]
    session_id = lookup_owned_rodex_session_id_from_a_cool_name(cool_name, database_path)
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
        active_tmux = _prepare_existing_tmux_identity(
            launcher,
            recorded_tmux,
            display_name,
            session_id,
            database_path,
        )
        record_a_rodex_session_access(session_id, database_path)
        if detach:
            _print_existing_detached_runtime(session_id, display_name, database_path)
            return True
        print(
            f"Reattaching Rodex {display_name} ({active_tmux.tmux_session_name})",
            flush=True,
        )
        launcher.attach(active_tmux)
        return True

    if not codex_available:
        configured_codex = os.environ.get("RODEX_CODEX_BINARY", "codex")
        raise RodexExecutableNotFoundError(
            f"Codex executable was not found: {configured_codex}"
        )
    codex_session_uuid = lookup_codex_uuid_from_a_rodex_session_id(
        session_id, database_path
    )
    if codex_session_uuid is None:
        raise RodexLaunchError(f"Rodex session has no Codex identity: {cool_name}")

    try:
        resumed_runtime, observed_codex_uuid = launcher.start(
            Path.cwd(), ["resume", str(codex_session_uuid)]
        )
    except RodexRuntimeError as error:
        raise RodexLaunchError(
            f"Rodex session {display_name!r} is recorded but not running; "
            f"Codex session {codex_session_uuid} could not be resumed: {error}"
        ) from error
    active_tmux: LiveTmuxSession = resumed_runtime
    try:
        if observed_codex_uuid != codex_session_uuid:
            raise RodexLaunchError(
                "Codex resumed an unexpected session: "
                f"expected {codex_session_uuid}, observed {observed_codex_uuid}"
            )
        active_tmux = _rename_tmux_identity(launcher, active_tmux, display_name)
        record_a_rodex_session_runtime_resume(
            session_id,
            active_tmux.tmux_server_socket_path,
            active_tmux.tmux_session_name,
            database_path,
        )
        launcher.configure_identity_status(active_tmux)
    except BaseException:
        launcher.stop(active_tmux, check=False)
        raise

    if detach:
        _print_existing_detached_runtime(session_id, display_name, database_path)
        return True
    print(
        f"Resumed Rodex {display_name} -> Codex {codex_session_uuid} "
        f"({active_tmux.tmux_session_name})",
        flush=True,
    )
    launcher.attach(active_tmux)
    return True


def _parse_launch_arguments(
    arguments: list[str],
) -> tuple[list[str], str | None, bool]:
    try:
        boundary = arguments.index("--")
    except ValueError:
        rodex_arguments = arguments
        codex_arguments: list[str] = []
    else:
        rodex_arguments = arguments[:boundary]
        codex_arguments = arguments[boundary:]

    detach_arguments = [
        argument for argument in rodex_arguments if argument in _DETACH_FLAGS
    ]
    if len(detach_arguments) > 1:
        raise RodexLaunchError("a detach flag may be supplied only once")
    remaining = [argument for argument in rodex_arguments if argument not in _DETACH_FLAGS]
    create_positions = [
        index for index, argument in enumerate(remaining) if argument in _CREATE_FLAGS
    ]
    if not create_positions:
        return [*remaining, *codex_arguments], None, bool(detach_arguments)
    if len(create_positions) != 1 or create_positions[0] + 1 >= len(remaining):
        raise RodexLaunchError("usage: rodex [--detach] --create SESSION_NAME")
    create_position = create_positions[0]
    requested_name = remaining[create_position + 1]
    forwarded_arguments = [
        *remaining[:create_position],
        *remaining[create_position + 2 :],
        *codex_arguments,
    ]
    return forwarded_arguments, requested_name, bool(detach_arguments)


def _print_existing_detached_runtime(
    session_id: int, display_name: str, database_path: Path
) -> None:
    rodex_uuid = lookup_rodex_uuid_from_an_id(session_id, database_path)
    codex_uuid = lookup_codex_uuid_from_a_rodex_session_id(session_id, database_path)
    if rodex_uuid is None or codex_uuid is None:
        raise RodexLaunchError(f"Rodex session identity disappeared: {display_name}")
    _print_detached_runtime(display_name, rodex_uuid, codex_uuid)


def _print_detached_runtime(
    display_name: str, rodex_uuid: uuid.UUID, codex_uuid: uuid.UUID
) -> None:
    print(
        json.dumps(
            {
                "status": "running",
                "rodex_session_name": display_name,
                "rodex_session_uuid": str(rodex_uuid),
                "codex_session_uuid": str(codex_uuid),
            },
            indent=2,
        ),
        flush=True,
    )


def _run_reserved_command(
    arguments: list[str],
    database_path: Path,
    launcher: RodexRuntimeLauncher,
    control_client: CodexControlClient,
) -> bool:
    if not arguments:
        return False
    command = arguments[0]
    if command in _RUNNING_COMMANDS:
        if len(arguments) != 1:
            raise RodexLaunchError("usage: rodex running")
        _print_running_sessions(database_path, launcher)
        return True
    if command in _SEND_COMMANDS:
        if len(arguments) < 3:
            raise RodexLaunchError("usage: rodex send SESSION_NAME PROMPT")
        session_name = arguments[1]
        prompt = " ".join(arguments[2:])
        session_id, runtime, control = _resolve_live_control(
            session_name, database_path, launcher
        )
        dispatch = control_client.send_prompt(
            control,
            prompt,
            revalidate=lambda: _revalidate_live_control(launcher, runtime, control),
        )
        record_a_rodex_session_access(session_id, database_path)
        print(
            f"Rodex {session_name}: {dispatch.action} Codex turn {dispatch.turn_id}",
            flush=True,
        )
        return True
    if command in _WAIT_COMMANDS:
        if len(arguments) != 2:
            raise RodexLaunchError("usage: rodex wait SESSION_NAME")
        session_id, runtime, control = _resolve_live_control(
            arguments[1], database_path, launcher
        )
        control_client.wait_until_idle(
            control,
            revalidate=lambda: _revalidate_live_control(launcher, runtime, control),
        )
        record_a_rodex_session_access(session_id, database_path)
        print(f"Rodex {arguments[1]}: Codex turn complete", flush=True)
        return True
    if command in _TAIL_COMMANDS:
        if len(arguments) != 2:
            raise RodexLaunchError("usage: rodex tail SESSION_NAME")
        session_id, runtime, control = _resolve_live_control(
            arguments[1], database_path, launcher
        )
        record_a_rodex_session_access(session_id, database_path)
        print(
            f"Rodex {arguments[1]}: following live Codex protocol events",
            file=sys.stderr,
            flush=True,
        )
        control_client.tail(
            control,
            lambda event: print(event, flush=True),
            revalidate=lambda: _revalidate_live_control(launcher, runtime, control),
        )
        return True
    if command in _ALIAS_COMMANDS:
        force, operands = _parse_alias_arguments(arguments[1:])
        if len(operands) != 2:
            raise RodexLaunchError(
                "usage: rodex alias [-f|--force] SESSION_NAME USER_DEFINED_NAME"
            )
        recorded_tmux: LiveTmuxSession | None = None
        active_tmux: LiveTmuxSession | None = None
        try:
            with open_a_user_defined_cool_name_assignment(
                operands[0],
                operands[1],
                database_path,
                force=force,
            ) as assignment:
                tmux_link = assignment.tmux_session
                if tmux_link is not None:
                    recorded_tmux = LiveTmuxSession(
                        tmux_server_socket_path=Path(tmux_link.tmux_server_socket_path),
                        tmux_session_name=tmux_link.tmux_session_name,
                    )
                    if launcher.session_exists(recorded_tmux):
                        active_tmux = _rename_tmux_identity(
                            launcher, recorded_tmux, assignment.names.display_name
                        )
                        assignment.renamed_tmux_session_name = active_tmux.tmux_session_name
        except BaseException:
            if (
                recorded_tmux is not None
                and active_tmux is not None
                and active_tmux.tmux_session_name != recorded_tmux.tmux_session_name
            ):
                _restore_tmux_identity(launcher, active_tmux, recorded_tmux)
            raise
        if active_tmux is not None:
            launcher.configure_identity_status(active_tmux)
        print(f"Rodex name: {assignment.names.display_name}", flush=True)
        return True
    return False


def _resolve_live_control(
    session_name: str,
    database_path: Path,
    launcher: RodexRuntimeLauncher,
) -> tuple[int, LiveTmuxSession, LiveRodexControl]:
    session_id = lookup_owned_rodex_session_id_from_a_cool_name(session_name, database_path)
    if session_id is None:
        raise RodexLaunchError(f"unknown Rodex session: {session_name}")
    tmux_link = lookup_rodex_tmux_session(session_id, database_path)
    if tmux_link is None:
        raise RodexLaunchError(f"Rodex session has no tmux endpoint: {session_name}")
    runtime = LiveTmuxSession(
        Path(tmux_link.tmux_server_socket_path), tmux_link.tmux_session_name
    )
    if not launcher.session_exists(runtime):
        raise RodexLaunchError(f"Rodex session is not running: {session_name}")
    expected_codex_uuid = lookup_codex_uuid_from_a_rodex_session_id(
        session_id, database_path
    )
    if expected_codex_uuid is None:
        raise RodexLaunchError(f"Rodex session has no Codex identity: {session_name}")
    control = launcher.discover_runtime_control(runtime)
    if control.codex_session_uuid != expected_codex_uuid:
        raise RodexLaunchError(
            "live runtime advertised an unexpected Codex identity: "
            f"expected {expected_codex_uuid}, observed {control.codex_session_uuid}"
        )
    return session_id, runtime, control


def _revalidate_live_control(
    launcher: RodexRuntimeLauncher,
    runtime: LiveTmuxSession,
    expected: LiveRodexControl,
) -> None:
    if not launcher.session_exists(runtime):
        raise RodexLaunchError("Rodex runtime ended during control discovery")
    if launcher.discover_runtime_control(runtime) != expected:
        raise RodexLaunchError("Rodex runtime changed during control discovery")


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


def _rename_tmux_identity(
    launcher: RodexRuntimeLauncher,
    active_tmux: LiveTmuxSession,
    display_name: str,
) -> LiveTmuxSession:
    if active_tmux.tmux_session_name == display_name:
        return active_tmux
    return launcher.rename(active_tmux, display_name)


def _prepare_existing_tmux_identity(
    launcher: RodexRuntimeLauncher,
    recorded_tmux: LiveTmuxSession,
    display_name: str,
    session_id: int,
    database_path: Path,
) -> LiveTmuxSession:
    active_tmux = _rename_tmux_identity(launcher, recorded_tmux, display_name)
    if active_tmux.tmux_session_name != recorded_tmux.tmux_session_name:
        try:
            update_rodex_tmux_session_name(
                session_id, active_tmux.tmux_session_name, database_path
            )
        except BaseException:
            _restore_tmux_identity(launcher, active_tmux, recorded_tmux)
            raise
    launcher.configure_identity_status(active_tmux)
    return active_tmux


def _restore_tmux_identity(
    launcher: RodexRuntimeLauncher,
    active_tmux: LiveTmuxSession,
    recorded_tmux: LiveTmuxSession,
) -> None:
    try:
        launcher.rename(active_tmux, recorded_tmux.tmux_session_name)
    except BaseException as restore_error:
        raise RodexLaunchError(
            "tmux was renamed but its database change and rename rollback both failed"
        ) from restore_error


def main() -> None:
    try:
        raise SystemExit(run())
    except RodexExecutableNotFoundError as error:
        print(f"rodex: {error}", file=sys.stderr)
        raise SystemExit(127) from error
    except KeyboardInterrupt:
        print(file=sys.stderr)
        raise SystemExit(130) from None
    except (
        CoolNameError,
        RodexControlError,
        RodexLaunchError,
        RodexRuntimeError,
        RodexSQLError,
        RodexSessionError,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as error:
        print(f"rodex: {error}", file=sys.stderr)
        raise SystemExit(1) from error
