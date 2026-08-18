"""Launch a normal Codex TUI with Rodex identity and tmux durability."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cool_name import (
    CoolNameError,
    normalise_rodex_display_name,
)
from rodex_registry import (
    CodexSessionId,
    RodexSessionError,
    RodexSessionId,
    create_a_rodex_session,
    default_rodex_database_path,
    generate_an_unregistered_rodex_session_id_candidate,
    lookup_codex_session_id_from_a_rodex_sessions_id,
    lookup_owned_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_registry_id,
    lookup_rodex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_session_names,
    lookup_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_tmux_session,
    open_a_user_defined_cool_name_assignment,
    record_a_rodex_session_access,
    record_a_rodex_session_runtime_resume,
    update_rodex_tmux_session_name,
)
from rodex_sql import RodexSQLError

from .command_contract import (
    CREATE_COMMAND,
    DETACH_COMMAND,
    HELP_COMMAND,
    HELP_TEXT,
    RODEX_COMMANDS,
    machine_spec_for_arguments,
)
from .control import (
    CodexControlClient,
    RodexControlError,
)
from .errors import RodexExecutableNotFoundError, RodexLaunchError
from .live_runtime import (
    find_relocated_live_runtime,
    rename_tmux_identity,
    require_live_runtime_identity,
    restore_tmux_identity,
    session_transition_lock,
    verify_live_runtime_identity,
)
from .machine_commands import print_machine_error, run_machine_command
from .runtime import (
    RODEX_REGISTRATION_PENDING,
    LiveTmuxSession,
    RodexCodexSessionNotFoundError,
    RodexRuntimeError,
    RodexRuntimeLauncher,
    default_tmux_server_socket_path,
)
from .session_commands import run_session_command
from .statistics_commands import run_statistics_command


@dataclass(frozen=True, slots=True)
class _PreparedNamedSession:
    session_id: int
    display_name: str
    active_tmux: LiveTmuxSession
    attach_message: str


CodexDelegator = Callable[[str, Sequence[str]], int]


def _exec_codex(codex_binary: str, arguments: Sequence[str]) -> int:
    """Replace Rodex with a Codex command that does not belong in managed tmux."""
    os.execv(codex_binary, [codex_binary, *arguments])
    raise AssertionError("os.execv returned unexpectedly")


def run(
    argv: Sequence[str] | None = None,
    *,
    database_path: str | os.PathLike[str] | None = None,
    launcher: RodexRuntimeLauncher | None = None,
    control_client: CodexControlClient | None = None,
    codex_delegator: CodexDelegator = _exec_codex,
) -> int:
    """Route explicit Rodex commands and pass every other invocation to Codex."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == [HELP_COMMAND]:
        if len(arguments) != 1:
            raise RodexLaunchError("usage: rodex _help")
        print(HELP_TEXT, end="")
        return 0
    if not arguments:
        arguments = [CREATE_COMMAND]

    configured_codex = os.environ.get("RODEX_CODEX_BINARY", "codex")
    resolved_database = (
        Path(os.path.abspath(Path(database_path).expanduser()))
        if database_path is not None
        else default_rodex_database_path()
    )
    rodex_command = (
        arguments[0] if arguments[:1] and arguments[0] in RODEX_COMMANDS else None
    )
    possible_existing_name = _possible_existing_rodex_name(arguments, resolved_database)
    if rodex_command is None and not possible_existing_name:
        _reject_live_unregistered_name_collision(
            arguments,
            configured_codex,
        )
        return _delegate_to_codex(configured_codex, arguments, codex_delegator)
    if run_statistics_command(arguments, resolved_database):
        return 0

    configured_tmux = os.environ.get("RODEX_TMUX_BINARY", "tmux")
    tmux_binary = shutil.which(configured_tmux)
    if tmux_binary is None:
        machine_spec = machine_spec_for_arguments(arguments)
        if machine_spec is not None:
            print_machine_error(
                machine_spec.operation,
                "runtime_unavailable",
                f"tmux executable was not found: {configured_tmux}",
                retryable=True,
                session_name=arguments[1] if len(arguments) > 1 else None,
                control=None,
            )
            return 3
        raise RodexExecutableNotFoundError(
            f"tmux executable was not found: {configured_tmux}"
        )

    codex_binary = shutil.which(configured_codex)
    runtime_launcher = launcher or RodexRuntimeLauncher(
        codex_binary or configured_codex, tmux_binary
    )
    runtime_control = control_client or CodexControlClient()
    machine_status = run_machine_command(
        arguments,
        resolved_database,
        runtime_launcher,
        runtime_control,
    )
    if machine_status is not None:
        return machine_status
    if run_session_command(
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
    if rodex_command not in {CREATE_COMMAND, DETACH_COMMAND}:
        return _delegate_to_codex(configured_codex, arguments, codex_delegator)
    if codex_binary is None:
        raise RodexExecutableNotFoundError(
            f"Codex executable was not found: {configured_codex}"
        )

    if requested_name is not None:
        requested_name = normalise_rodex_display_name(requested_name)
        if (
            lookup_rodex_sessions_id_from_a_cool_name(requested_name, resolved_database)
            is not None
        ):
            raise RodexLaunchError(f"Rodex session name already exists: {requested_name}")

    planned_rodex_session_id = generate_an_unregistered_rodex_session_id_candidate(
        resolved_database
    )
    registry_id = lookup_rodex_registry_id(resolved_database)
    live_runtime, codex_session_id = runtime_launcher.start(
        Path.cwd(),
        codex_arguments,
        rodex_session_id=planned_rodex_session_id,
        rodex_registry_id=registry_id,
        rodex_database_path=resolved_database,
    )
    active_tmux: LiveTmuxSession = live_runtime
    try:
        session = create_a_rodex_session(
            resolved_database,
            codex_session_id=codex_session_id,
            rodex_session_id=planned_rodex_session_id,
            tmux_server_socket_path=live_runtime.tmux_server_socket_path,
            tmux_session_name=live_runtime.tmux_session_name,
            runtime_identifier=live_runtime.runtime_identifier,
        )
        runtime_launcher.confirm_runtime_registration(active_tmux)
        display_name = session.cool_name
        if requested_name is None:
            active_tmux = rename_tmux_identity(runtime_launcher, active_tmux, display_name)
            update_rodex_tmux_session_name(
                session.rodex_sessions_id,
                active_tmux.tmux_session_name,
                resolved_database,
            )
        else:
            with open_a_user_defined_cool_name_assignment(
                session.cool_name, requested_name, resolved_database
            ) as assignment:
                active_tmux = rename_tmux_identity(
                    runtime_launcher, active_tmux, assignment.names.display_name
                )
                assignment.renamed_tmux_session_name = active_tmux.tmux_session_name
            display_name = assignment.names.display_name
        runtime_launcher.initialise_session_ui(active_tmux)
    except BaseException:
        runtime_launcher.stop(active_tmux, check=False)
        raise

    if detach:
        _print_detached_runtime(display_name, session.rodex_session_id, codex_session_id)
        return 0
    print(
        f"Rodex {display_name} [{session.rodex_session_id}] "
        f"-> Codex {codex_session_id} "
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
    session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(cool_name, database_path)
    if session_id is None:
        return False
    with session_transition_lock(database_path, session_id):
        prepared = _prepare_named_session(
            session_id,
            cool_name,
            database_path,
            launcher,
            codex_available=codex_available,
        )
    if detach:
        _print_existing_detached_runtime(
            prepared.session_id,
            prepared.display_name,
            database_path,
        )
        return True
    print(prepared.attach_message, flush=True)
    launcher.attach(prepared.active_tmux)
    return True


def _prepare_named_session(
    session_id: int,
    cool_name: str,
    database_path: Path,
    launcher: RodexRuntimeLauncher,
    *,
    codex_available: bool,
) -> _PreparedNamedSession:
    """Resolve or resume one identity while its cross-process transition is locked."""
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
    codex_session_id = lookup_codex_session_id_from_a_rodex_sessions_id(
        session_id, database_path
    )
    if codex_session_id is None:
        raise RodexLaunchError(f"Rodex session has no Codex identity: {cool_name}")
    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(
        session_id, database_path
    )
    if rodex_session_id is None:
        raise RodexLaunchError(f"Rodex session has no Rodex identity: {cool_name}")
    registry_id = lookup_rodex_registry_id(database_path)
    if launcher.session_exists(recorded_tmux):
        verify_live_runtime_identity(
            launcher,
            recorded_tmux,
            session_id=session_id,
            database_path=database_path,
            expected_rodex_session_id=rodex_session_id,
            expected_registry_id=registry_id,
            expected_codex_session_id=codex_session_id,
        )
        active_tmux = _prepare_existing_tmux_identity(
            launcher,
            recorded_tmux,
            display_name,
            session_id,
            database_path,
        )
        record_a_rodex_session_access(session_id, database_path)
        return _PreparedNamedSession(
            session_id,
            display_name,
            active_tmux,
            f"Reattaching Rodex {display_name} ({active_tmux.tmux_session_name})",
        )

    relocated_match = find_relocated_live_runtime(
        launcher,
        recorded_tmux.tmux_server_socket_path,
        expected_rodex_session_id=rodex_session_id,
        expected_registry_id=registry_id,
        expected_codex_session_id=codex_session_id,
    )
    if relocated_match is not None:
        relocated, relocated_control = relocated_match
        record_a_rodex_session_runtime_resume(
            session_id,
            relocated.tmux_server_socket_path,
            relocated.tmux_session_name,
            database_path,
            runtime_identifier=relocated_control.runtime_identifier,
        )
        if relocated_control.registration_state == RODEX_REGISTRATION_PENDING:
            launcher.confirm_runtime_registration(relocated)
            require_live_runtime_identity(
                launcher.discover_runtime_control(relocated),
                expected_rodex_session_id=rodex_session_id,
                expected_registry_id=registry_id,
                expected_codex_session_id=codex_session_id,
            )
        active_tmux = _prepare_existing_tmux_identity(
            launcher,
            relocated,
            display_name,
            session_id,
            database_path,
        )
        return _PreparedNamedSession(
            session_id,
            display_name,
            active_tmux,
            f"Reattached relocated Rodex {display_name} ({active_tmux.tmux_session_name})",
        )

    if not codex_available:
        configured_codex = os.environ.get("RODEX_CODEX_BINARY", "codex")
        raise RodexExecutableNotFoundError(
            f"Codex executable was not found: {configured_codex}"
        )
    replaced_unsaved_codex_identity = False
    try:
        resumed_runtime, observed_codex_session_id = launcher.start(
            Path.cwd(),
            ["resume", str(codex_session_id)],
            rodex_session_id=rodex_session_id,
            rodex_registry_id=registry_id,
            rodex_database_path=database_path,
        )
    except RodexCodexSessionNotFoundError:
        try:
            resumed_runtime, observed_codex_session_id = launcher.start(
                Path.cwd(),
                [],
                rodex_session_id=rodex_session_id,
                rodex_registry_id=registry_id,
                rodex_database_path=database_path,
            )
        except RodexRuntimeError as error:
            raise RodexLaunchError(
                f"Rodex session {display_name!r} is recorded but not running; "
                f"Codex session {codex_session_id} was not saved and a replacement "
                f"Codex runtime could not be started: {error}"
            ) from error
        replaced_unsaved_codex_identity = True
    except RodexRuntimeError as error:
        raise RodexLaunchError(
            f"Rodex session {display_name!r} is recorded but not running; "
            f"Codex session {codex_session_id} could not be resumed: {error}"
        ) from error
    active_tmux: LiveTmuxSession = resumed_runtime
    try:
        if (
            not replaced_unsaved_codex_identity
            and observed_codex_session_id != codex_session_id
        ):
            raise RodexLaunchError(
                "Codex resumed an unexpected session: "
                f"expected {codex_session_id}, observed {observed_codex_session_id}"
            )
        active_tmux = rename_tmux_identity(launcher, active_tmux, display_name)
        record_a_rodex_session_runtime_resume(
            session_id,
            active_tmux.tmux_server_socket_path,
            active_tmux.tmux_session_name,
            database_path,
            codex_session_id=(
                observed_codex_session_id if replaced_unsaved_codex_identity else None
            ),
            runtime_identifier=resumed_runtime.runtime_identifier,
        )
        launcher.confirm_runtime_registration(active_tmux)
        launcher.initialise_session_ui(active_tmux)
    except BaseException:
        launcher.stop(active_tmux, check=False)
        raise

    action = "Recovered" if replaced_unsaved_codex_identity else "Resumed"
    return _PreparedNamedSession(
        session_id,
        display_name,
        active_tmux,
        f"{action} Rodex {display_name} -> Codex {observed_codex_session_id} "
        f"({active_tmux.tmux_session_name})",
    )


def _parse_launch_arguments(
    arguments: list[str],
) -> tuple[list[str], str | None, bool]:
    command = arguments[0]
    command_arguments = arguments[1:]
    if command == DETACH_COMMAND:
        return _without_separator(command_arguments), None, True
    if command != CREATE_COMMAND:
        return arguments, None, False
    if not command_arguments or command_arguments[0].startswith("-"):
        return _without_separator(command_arguments), None, False
    requested_name = command_arguments[0]
    return _without_separator(command_arguments[1:]), requested_name, False


def _without_separator(arguments: list[str]) -> list[str]:
    return arguments[1:] if arguments[:1] == ["--"] else arguments


def _possible_existing_rodex_name(arguments: list[str], database_path: Path) -> bool:
    """Recognize only the one bare-name exception to explicit Rodex commands."""
    if len(arguments) != 1 or arguments[0].startswith(("-", "_")):
        return False
    if not database_path.exists():
        return False
    try:
        return (
            lookup_owned_rodex_sessions_id_from_a_cool_name(arguments[0], database_path)
            is not None
        )
    except (CoolNameError, ValueError):
        return False


def _reject_live_unregistered_name_collision(
    arguments: list[str],
    configured_codex: str,
) -> None:
    """Fail closed when Codex passthrough would collide with a live tmux name."""
    if len(arguments) != 1 or arguments[0].startswith(("-", "_")):
        return
    socket_path = default_tmux_server_socket_path()
    if not socket_path.exists():
        return
    configured_tmux = os.environ.get("RODEX_TMUX_BINARY", "tmux")
    tmux_binary = shutil.which(configured_tmux)
    if tmux_binary is None:
        return
    active_launcher = RodexRuntimeLauncher(configured_codex, tmux_binary)
    candidate = LiveTmuxSession(socket_path, arguments[0])
    if active_launcher.session_exists(candidate):
        raise RodexLaunchError(
            f"live tmux session {arguments[0]!r} exists on Rodex's private server "
            "but is not registered in this Rodex database; refusing Codex "
            "passthrough and unsafe adoption"
        )


def _delegate_to_codex(
    configured_codex: str,
    arguments: Sequence[str],
    codex_delegator: CodexDelegator,
) -> int:
    codex_binary = shutil.which(configured_codex)
    if codex_binary is None:
        raise RodexExecutableNotFoundError(
            f"Codex executable was not found: {configured_codex}"
        )
    return codex_delegator(codex_binary, arguments)


def _print_existing_detached_runtime(
    session_id: int, display_name: str, database_path: Path
) -> None:
    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(
        session_id, database_path
    )
    codex_session_id = lookup_codex_session_id_from_a_rodex_sessions_id(
        session_id, database_path
    )
    if rodex_session_id is None or codex_session_id is None:
        raise RodexLaunchError(f"Rodex session identity disappeared: {display_name}")
    _print_detached_runtime(display_name, rodex_session_id, codex_session_id)


def _print_detached_runtime(
    display_name: str,
    rodex_session_id: RodexSessionId,
    codex_session_id: CodexSessionId,
) -> None:
    print(
        json.dumps(
            {
                "status": "running",
                "rodex_session_name": display_name,
                "rodex_session_id": str(rodex_session_id),
                "codex_session_id": str(codex_session_id),
            },
            indent=2,
        ),
        flush=True,
    )


def _prepare_existing_tmux_identity(
    launcher: RodexRuntimeLauncher,
    recorded_tmux: LiveTmuxSession,
    display_name: str,
    session_id: int,
    database_path: Path,
) -> LiveTmuxSession:
    active_tmux = rename_tmux_identity(launcher, recorded_tmux, display_name)
    if active_tmux.tmux_session_name != recorded_tmux.tmux_session_name:
        try:
            update_rodex_tmux_session_name(
                session_id, active_tmux.tmux_session_name, database_path
            )
        except BaseException:
            restore_tmux_identity(launcher, active_tmux, recorded_tmux)
            raise
    if active_tmux.tmux_session_name != recorded_tmux.tmux_session_name:
        launcher.refresh_name_bound_hooks(active_tmux)
    else:
        launcher.reconcile_session_ui(active_tmux)
    return active_tmux


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
