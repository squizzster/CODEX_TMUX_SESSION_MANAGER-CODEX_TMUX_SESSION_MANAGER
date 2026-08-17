"""Launch a normal Codex TUI with Rodex identity and tmux durability."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import stat as stat_module
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cool_name import (
    CoolNameError,
    normalise_rodex_display_name,
)
from rodex_registry import (
    CodexSessionId,
    RodexRegistryId,
    RodexSessionError,
    RodexSessionId,
    RodexSessionTurnStatisticsAmbiguousError,
    create_a_rodex_session,
    default_rodex_database_path,
    generate_an_unregistered_rodex_session_id_candidate,
    list_rodex_session_runtimes_for_a_user,
    lookup_codex_session_id_from_a_rodex_sessions_id,
    lookup_owned_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_registry_id,
    lookup_rodex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_session_names,
    lookup_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_sessions_id_from_a_rodex_session_id,
    lookup_rodex_tmux_session,
    open_a_user_defined_cool_name_assignment,
    parse_codex_session_id,
    read_rodex_session_statistics,
    read_rodex_session_turn_statistics,
    record_a_rodex_session_access,
    record_a_rodex_session_runtime_resume,
    session_statistics_as_dict,
    turn_statistics_as_dict,
    update_rodex_tmux_session_name,
)
from rodex_sql import RodexSQLError

from .control import CodexControlClient, LiveRodexControl, RodexControlError
from .runtime import (
    RODEX_REGISTRATION_PENDING,
    RODEX_REGISTRATION_REGISTERED,
    LiveTmuxSession,
    RodexCodexSessionNotFoundError,
    RodexRuntimeError,
    RodexRuntimeLauncher,
    default_tmux_server_socket_path,
)


class RodexLaunchError(RuntimeError):
    """Rodex could not launch its Codex/tmux runtime."""


class RodexExecutableNotFoundError(RodexLaunchError):
    """A required executable could not be resolved from PATH."""


@dataclass(frozen=True, slots=True)
class _PreparedNamedSession:
    session_id: int
    display_name: str
    active_tmux: LiveTmuxSession
    attach_message: str


_RUNNING_COMMAND: Final = "_running"
_CONTEXT_COMMAND: Final = "_context"
_ALIAS_COMMAND: Final = "_alias"
_SEND_COMMAND: Final = "_send"
_WAIT_COMMAND: Final = "_wait"
_TAIL_COMMAND: Final = "_tail"
_CREATE_COMMAND: Final = "_create"
_DETACH_COMMAND: Final = "_detach"
_HELP_COMMAND: Final = "_help"
_STATS_COMMAND: Final = "_stats"
_STATS_STATUS_COMMAND: Final = "_stats-status"
_MOUSE_COMMAND: Final = "_mouse"
_FORCE_FLAG: Final = "--force"
_RODEX_COMMANDS: Final = frozenset(
    {
        _RUNNING_COMMAND,
        _CONTEXT_COMMAND,
        _ALIAS_COMMAND,
        _SEND_COMMAND,
        _WAIT_COMMAND,
        _TAIL_COMMAND,
        _CREATE_COMMAND,
        _DETACH_COMMAND,
        _HELP_COMMAND,
        _STATS_COMMAND,
        _STATS_STATUS_COMMAND,
        _MOUSE_COMMAND,
    }
)

_HELP_TEXT: Final = """\
usage: rodex [COMMAND [ARGUMENTS]]

Rodex commands:
  (no command)                       Create and attach to a managed session.
  _help                              Show this help and exit.
  _create [NAME] [-- CODEX_ARGS...]  Create and attach to a managed session.
  _detach [SESSION|CODEX_ARGS...]    Create, resume, or recover without attaching.
  _running                           List running Rodex sessions.
  _context [--json]                  Show this pane's verified live Rodex context.
  _alias [--force] SESSION NAME      Assign a preferred session name.
  _send SESSION PROMPT               Send work to a running session.
  _wait SESSION                      Wait until a running session is idle.
  _tail SESSION                      Follow live protocol events as JSON lines.
  _stats SESSION [--turn ID] [--source CODEX_SESSION_ID] [--json]
                                     Show persistent session or exact-turn statistics.
  _stats-status SESSION              Show analytics freshness and health.
  _mouse SESSION [MODE]              Show or set mouse: on, off, toggle, inherit.

Use a Rodex session name as the sole argument to attach, resume, or recover it.
Every other invocation is passed unchanged to Codex.
"""

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
    if arguments[:1] == [_HELP_COMMAND]:
        if len(arguments) != 1:
            raise RodexLaunchError("usage: rodex _help")
        print(_HELP_TEXT, end="")
        return 0
    if not arguments:
        arguments = [_CREATE_COMMAND]

    configured_codex = os.environ.get("RODEX_CODEX_BINARY", "codex")
    resolved_database = (
        Path(os.path.abspath(Path(database_path).expanduser()))
        if database_path is not None
        else default_rodex_database_path()
    )
    rodex_command = (
        arguments[0] if arguments[:1] and arguments[0] in _RODEX_COMMANDS else None
    )
    possible_existing_name = _possible_existing_rodex_name(arguments, resolved_database)
    if rodex_command is None and not possible_existing_name:
        _reject_live_unregistered_name_collision(
            arguments,
            configured_codex,
        )
        return _delegate_to_codex(configured_codex, arguments, codex_delegator)
    if _run_statistics_command(arguments, resolved_database):
        return 0

    configured_tmux = os.environ.get("RODEX_TMUX_BINARY", "tmux")
    tmux_binary = shutil.which(configured_tmux)
    if tmux_binary is None:
        raise RodexExecutableNotFoundError(
            f"tmux executable was not found: {configured_tmux}"
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
    if rodex_command not in {_CREATE_COMMAND, _DETACH_COMMAND}:
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
        )
        runtime_launcher.confirm_runtime_registration(active_tmux)
        display_name = session.cool_name
        if requested_name is None:
            active_tmux = _rename_tmux_identity(runtime_launcher, active_tmux, display_name)
            update_rodex_tmux_session_name(
                session.rodex_sessions_id,
                active_tmux.tmux_session_name,
                resolved_database,
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
    with _open_named_session_transition_lock(database_path, session_id):
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
        _verify_live_runtime_identity(
            launcher,
            recorded_tmux,
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

    relocated = _find_relocated_live_runtime(
        launcher,
        recorded_tmux.tmux_server_socket_path,
        expected_rodex_session_id=rodex_session_id,
        expected_registry_id=registry_id,
        expected_codex_session_id=codex_session_id,
    )
    if relocated is not None:
        active_tmux = _prepare_existing_tmux_identity(
            launcher,
            relocated,
            display_name,
            session_id,
            database_path,
        )
        record_a_rodex_session_access(session_id, database_path)
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
        active_tmux = _rename_tmux_identity(launcher, active_tmux, display_name)
        record_a_rodex_session_runtime_resume(
            session_id,
            active_tmux.tmux_server_socket_path,
            active_tmux.tmux_session_name,
            database_path,
            codex_session_id=(
                observed_codex_session_id if replaced_unsaved_codex_identity else None
            ),
        )
        launcher.confirm_runtime_registration(active_tmux)
        launcher.configure_identity_status(active_tmux)
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


@contextmanager
def _open_named_session_transition_lock(
    database_path: Path,
    session_id: int,
) -> Iterator[None]:
    """Serialize one named session's liveness decision and runtime replacement."""
    lock_path = database_path.parent / f".{database_path.name}.session-{session_id}.lock"
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        state = os.fstat(descriptor)
        if not stat_module.S_ISREG(state.st_mode) or state.st_uid != os.getuid():
            raise RodexLaunchError(
                f"session transition lock is not a private regular file: {lock_path}"
            )
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _parse_launch_arguments(
    arguments: list[str],
) -> tuple[list[str], str | None, bool]:
    command = arguments[0]
    command_arguments = arguments[1:]
    if command == _DETACH_COMMAND:
        return _without_separator(command_arguments), None, True
    if command != _CREATE_COMMAND:
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


def _run_reserved_command(
    arguments: list[str],
    database_path: Path,
    launcher: RodexRuntimeLauncher,
    control_client: CodexControlClient,
) -> bool:
    if not arguments:
        return False
    command = arguments[0]
    if command == _CONTEXT_COMMAND:
        if tuple(arguments) not in {
            (_CONTEXT_COMMAND,),
            (_CONTEXT_COMMAND, "--json"),
        }:
            raise RodexLaunchError("usage: rodex _context [--json]")
        _print_current_rodex_context(database_path, launcher)
        return True
    if command == _RUNNING_COMMAND:
        if len(arguments) != 1:
            raise RodexLaunchError("usage: rodex _running")
        _print_running_sessions(database_path, launcher)
        return True
    if command == _MOUSE_COMMAND:
        if len(arguments) not in {2, 3}:
            raise RodexLaunchError(
                "usage: rodex _mouse SESSION_NAME [on|off|toggle|inherit|status]"
            )
        mode = arguments[2] if len(arguments) == 3 else "status"
        if mode not in {"on", "off", "toggle", "inherit", "status"}:
            raise RodexLaunchError(
                "usage: rodex _mouse SESSION_NAME [on|off|toggle|inherit|status]"
            )
        session_id, runtime, _ = _resolve_live_control(
            arguments[1], database_path, launcher
        )
        mouse_state = launcher.set_mouse_mode(runtime, mode)
        record_a_rodex_session_access(session_id, database_path)
        print(f"Rodex {arguments[1]} mouse: {mouse_state}", flush=True)
        return True
    if command == _SEND_COMMAND:
        if len(arguments) < 3:
            raise RodexLaunchError("usage: rodex _send SESSION_NAME PROMPT")
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
    if command == _WAIT_COMMAND:
        if len(arguments) != 2:
            raise RodexLaunchError("usage: rodex _wait SESSION_NAME")
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
    if command == _TAIL_COMMAND:
        if len(arguments) != 2:
            raise RodexLaunchError("usage: rodex _tail SESSION_NAME")
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
    if command == _ALIAS_COMMAND:
        force, operands = _parse_alias_arguments(arguments[1:])
        if len(operands) != 2:
            raise RodexLaunchError(
                "usage: rodex _alias [--force] SESSION_NAME USER_DEFINED_NAME"
            )
        alias_session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(
            operands[0], database_path
        )
        previous_names = (
            None
            if alias_session_id is None
            else lookup_rodex_session_names(alias_session_id, database_path)
        )
        expected_codex_session_id = (
            None
            if alias_session_id is None
            else lookup_codex_session_id_from_a_rodex_sessions_id(
                alias_session_id, database_path
            )
        )
        expected_rodex_session_id = (
            None
            if alias_session_id is None
            else lookup_rodex_session_id_from_a_rodex_sessions_id(
                alias_session_id, database_path
            )
        )
        expected_registry_id = (
            None if alias_session_id is None else lookup_rodex_registry_id(database_path)
        )
        recorded_tmux: LiveTmuxSession | None = None
        active_tmux: LiveTmuxSession | None = None
        verified_control: LiveRodexControl | None = None
        if alias_session_id is not None:
            tmux_link = lookup_rodex_tmux_session(alias_session_id, database_path)
            if tmux_link is not None:
                recorded_tmux = LiveTmuxSession(
                    tmux_server_socket_path=Path(tmux_link.tmux_server_socket_path),
                    tmux_session_name=tmux_link.tmux_session_name,
                )
                if launcher.session_exists(recorded_tmux):
                    if (
                        expected_codex_session_id is None
                        or expected_rodex_session_id is None
                        or expected_registry_id is None
                    ):
                        raise RodexLaunchError(
                            "Rodex session identity disappeared during alias change"
                        )
                    verified_control = _verify_live_runtime_identity(
                        launcher,
                        recorded_tmux,
                        expected_rodex_session_id=expected_rodex_session_id,
                        expected_registry_id=expected_registry_id,
                        expected_codex_session_id=expected_codex_session_id,
                    )
        try:
            with open_a_user_defined_cool_name_assignment(
                operands[0],
                operands[1],
                database_path,
                force=force,
            ) as assignment:
                tmux_link = assignment.tmux_session
                if (
                    tmux_link is not None
                    and recorded_tmux is not None
                    and verified_control is not None
                ):
                    _revalidate_live_control(launcher, recorded_tmux, verified_control)
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
        if (
            active_tmux is not None
            and verified_control is not None
            and expected_rodex_session_id is not None
            and previous_names is not None
            and previous_names.display_name != assignment.names.display_name
        ):
            auto_info = (
                f"RODEX_AUTO_INFO: Rodex session {expected_rodex_session_id} "
                f"is now named {assignment.names.display_name!r}."
            )
            try:
                control_client.send_prompt(
                    verified_control,
                    auto_info,
                    revalidate=lambda: _revalidate_live_control(
                        launcher,
                        active_tmux,
                        verified_control,
                    ),
                )
            except (RodexControlError, RodexLaunchError, RodexRuntimeError) as error:
                raise RodexLaunchError(
                    f"Rodex name changed to {assignment.names.display_name!r}, but "
                    f"RODEX_AUTO_INFO delivery failed: {error}"
                ) from error
        print(f"Rodex name: {assignment.names.display_name}", flush=True)
        return True
    return False


def _run_statistics_command(arguments: list[str], database_path: Path) -> bool:
    """Serve persistent statistics without requiring Codex, tmux, or analysis."""
    if not arguments or arguments[0] not in {_STATS_COMMAND, _STATS_STATUS_COMMAND}:
        return False
    command = arguments[0]
    if command == _STATS_STATUS_COMMAND:
        if len(arguments) != 2:
            raise RodexLaunchError("usage: rodex _stats-status SESSION_NAME")
        session_name = arguments[1]
        as_json = False
        turn_id = None
        source_codex_session_id = None
    else:
        if len(arguments) < 2:
            raise RodexLaunchError(
                "usage: rodex _stats SESSION_NAME "
                "[--turn TURN_ID] [--source CODEX_SESSION_ID] [--json]"
            )
        session_name = arguments[1]
        as_json = False
        turn_id: str | None = None
        source_codex_session_id: CodexSessionId | None = None
        index = 2
        while index < len(arguments):
            option = arguments[index]
            if option == "--json" and not as_json:
                as_json = True
                index += 1
            elif option == "--turn" and turn_id is None and index + 1 < len(arguments):
                turn_id = arguments[index + 1]
                index += 2
            elif (
                option == "--source"
                and source_codex_session_id is None
                and index + 1 < len(arguments)
            ):
                try:
                    source_codex_session_id = parse_codex_session_id(arguments[index + 1])
                except ValueError as error:
                    raise RodexLaunchError(
                        "--source requires a valid Codex session ID"
                    ) from error
                index += 2
            else:
                raise RodexLaunchError(
                    "usage: rodex _stats SESSION_NAME "
                    "[--turn TURN_ID] [--source CODEX_SESSION_ID] [--json]"
                )
        if source_codex_session_id is not None and turn_id is None:
            raise RodexLaunchError("--source requires --turn")
    session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(
        session_name, database_path
    )
    if session_id is None:
        raise RodexLaunchError(f"unknown Rodex session: {session_name}")
    try:
        view = (
            read_rodex_session_statistics(session_id, database_path)
            if turn_id is None
            else read_rodex_session_turn_statistics(
                session_id,
                turn_id,
                database_path,
                codex_session_id=source_codex_session_id,
            )
        )
    except RodexSessionTurnStatisticsAmbiguousError as error:
        raise RodexLaunchError(str(error)) from error
    snapshot = view.statistics
    worker = view.worker
    payload = {
        "rodex_session_name": session_name,
        "statistics_revision": (None if snapshot is None else snapshot.statistics_revision),
        "statistics_projection_schema_version": (
            None if snapshot is None else snapshot.statistics_projection_schema_version
        ),
        "calculated_at_utc": None if snapshot is None else snapshot.calculated_at_utc,
        "coverage_state": None if snapshot is None else snapshot.coverage_state,
        "worker_state": "not_started" if worker is None else worker.worker_state,
        "diagnostic_code": None if worker is None else worker.diagnostic_code,
        "last_attempted_at_utc": (None if worker is None else worker.last_attempted_at_utc),
        "consecutive_failures": (0 if worker is None else worker.consecutive_failures),
        "next_retry_at_utc": None if worker is None else worker.next_retry_at_utc,
        "registered_source_count": len(view.sources),
        "included_source_count": (
            0
            if snapshot is None
            else sum(
                source.included_statistics_revision == snapshot.statistics_revision
                for source in view.sources
            )
        ),
    }
    if command == _STATS_STATUS_COMMAND:
        print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
        return True
    if snapshot is None:
        raise RodexLaunchError(
            f"Rodex session has no analytics snapshot yet: {session_name}"
        )
    if turn_id is None:
        payload["statistics"] = session_statistics_as_dict(snapshot.projection)
    else:
        turn = view.turn
        if turn is None:
            raise RodexLaunchError(
                f"turn is not present in the latest statistics snapshot: {turn_id}"
            )
        payload["turn"] = {
            "codex_session_id": str(turn.codex_session_id),
            "turn_id": turn.codex_turn_id,
            "started_at_utc": turn.started_at_utc,
            "terminal_at_utc": turn.terminal_at_utc,
            "outcome": turn.outcome,
            "included_statistics_revision": turn.included_statistics_revision,
        }
        payload["statistics"] = turn_statistics_as_dict(turn.projection)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    else:
        _print_human_statistics(payload)
    return True


def _print_human_statistics(payload: dict[str, object]) -> None:
    statistics = payload["statistics"]
    if not isinstance(statistics, dict):
        raise RodexLaunchError("stored analytics snapshot is invalid")
    turn = payload.get("turn")
    subject = ""
    if isinstance(turn, dict):
        subject = f" turn {turn.get('turn_id')}"
    print(
        f"Rodex {payload['rodex_session_name']}{subject} statistics "
        f"(revision {payload['statistics_revision']}, {payload['worker_state']})",
        flush=True,
    )
    for category in ("must_have_basic_stats", "recommended_insight_stats"):
        values = statistics.get(category)
        if isinstance(values, dict):
            title = category.replace("_", " ").title()
            print(f"{title}:", flush=True)
            for name, value in values.items():
                print(f"  {name}: {json.dumps(value, sort_keys=True)}", flush=True)


def _resolve_live_control(
    session_name: str,
    database_path: Path,
    launcher: RodexRuntimeLauncher,
) -> tuple[int, LiveTmuxSession, LiveRodexControl]:
    session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(
        session_name, database_path
    )
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
    expected_codex_session_id = lookup_codex_session_id_from_a_rodex_sessions_id(
        session_id, database_path
    )
    if expected_codex_session_id is None:
        raise RodexLaunchError(f"Rodex session has no Codex identity: {session_name}")
    expected_rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(
        session_id, database_path
    )
    if expected_rodex_session_id is None:
        raise RodexLaunchError(f"Rodex session has no Rodex identity: {session_name}")
    expected_registry_id = lookup_rodex_registry_id(database_path)
    control = _verify_live_runtime_identity(
        launcher,
        runtime,
        expected_rodex_session_id=expected_rodex_session_id,
        expected_registry_id=expected_registry_id,
        expected_codex_session_id=expected_codex_session_id,
    )
    return session_id, runtime, control


def _verify_live_runtime_identity(
    launcher: RodexRuntimeLauncher,
    runtime: LiveTmuxSession,
    *,
    expected_rodex_session_id: RodexSessionId,
    expected_registry_id: RodexRegistryId,
    expected_codex_session_id: CodexSessionId,
) -> LiveRodexControl:
    control = launcher.discover_runtime_control(runtime)
    if (
        control.registration_state == RODEX_REGISTRATION_PENDING
        and control.rodex_session_id == expected_rodex_session_id
        and control.rodex_registry_id == expected_registry_id
        and control.codex_session_id == expected_codex_session_id
    ):
        launcher.confirm_runtime_registration(runtime)
        control = launcher.discover_runtime_control(runtime)
    _require_live_runtime_identity(
        control,
        expected_rodex_session_id=expected_rodex_session_id,
        expected_registry_id=expected_registry_id,
        expected_codex_session_id=expected_codex_session_id,
    )
    return control


def _require_live_runtime_identity(
    control: LiveRodexControl,
    *,
    expected_rodex_session_id: RodexSessionId,
    expected_registry_id: RodexRegistryId,
    expected_codex_session_id: CodexSessionId,
) -> None:
    if control.registration_state != RODEX_REGISTRATION_REGISTERED:
        raise RodexLaunchError(
            "live runtime is not durably registered: "
            f"expected {RODEX_REGISTRATION_REGISTERED}, "
            f"observed {control.registration_state or 'missing'}"
        )
    if control.rodex_session_id != expected_rodex_session_id:
        raise RodexLaunchError(
            "live runtime advertised an unexpected Rodex identity: "
            f"expected {expected_rodex_session_id}, "
            f"observed {control.rodex_session_id or 'missing'}"
        )
    if control.rodex_registry_id != expected_registry_id:
        raise RodexLaunchError(
            "live runtime advertised an unexpected Rodex registry identity: "
            f"expected {expected_registry_id}, "
            f"observed {control.rodex_registry_id or 'missing'}"
        )
    if control.codex_session_id != expected_codex_session_id:
        raise RodexLaunchError(
            "live runtime advertised an unexpected Codex identity: "
            f"expected {expected_codex_session_id}, observed {control.codex_session_id}"
        )


def _find_relocated_live_runtime(
    launcher: RodexRuntimeLauncher,
    tmux_server_socket_path: Path,
    *,
    expected_rodex_session_id: RodexSessionId,
    expected_registry_id: RodexRegistryId,
    expected_codex_session_id: CodexSessionId,
) -> LiveTmuxSession | None:
    matches: list[tuple[LiveTmuxSession, LiveRodexControl]] = []
    unverifiable_same_codex: list[str] = []
    for name in launcher.list_session_names(tmux_server_socket_path):
        candidate = LiveTmuxSession(tmux_server_socket_path, name)
        try:
            control = launcher.discover_runtime_control(candidate)
        except RodexRuntimeError:
            continue
        if control.codex_session_id != expected_codex_session_id:
            continue
        if (
            control.rodex_session_id == expected_rodex_session_id
            and control.rodex_registry_id == expected_registry_id
            and control.registration_state
            in {RODEX_REGISTRATION_PENDING, RODEX_REGISTRATION_REGISTERED}
        ):
            matches.append((candidate, control))
        else:
            unverifiable_same_codex.append(name)
    if unverifiable_same_codex:
        raise RodexLaunchError(
            "live runtime with the expected Codex identity lacks the matching "
            "registered Rodex identity: " + ", ".join(sorted(unverifiable_same_codex))
        )
    if len(matches) > 1:
        raise RodexLaunchError(
            "multiple live runtimes advertise the same Rodex/Codex identity: "
            + ", ".join(sorted(item.tmux_session_name for item, _control in matches))
        )
    if not matches:
        return None
    candidate, control = matches[0]
    if control.registration_state == RODEX_REGISTRATION_PENDING:
        launcher.confirm_runtime_registration(candidate)
        _require_live_runtime_identity(
            launcher.discover_runtime_control(candidate),
            expected_rodex_session_id=expected_rodex_session_id,
            expected_registry_id=expected_registry_id,
            expected_codex_session_id=expected_codex_session_id,
        )
    return candidate


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
        if argument == _FORCE_FLAG:
            force = True
        elif argument.startswith("-"):
            raise RodexLaunchError(f"unknown alias option: {argument}")
        else:
            operands.append(argument)
    return force, operands


def _print_current_rodex_context(
    database_path: Path,
    launcher: RodexRuntimeLauncher,
) -> None:
    """Print one verified snapshot of the Rodex session containing this process."""
    tmux_context = launcher.discover_current_tmux_pane_context()
    live_tmux = tmux_context.tmux_session
    advertised = launcher.discover_runtime_control(live_tmux)
    if advertised.rodex_session_id is None:
        raise RodexLaunchError(
            "the current tmux pane does not advertise a Rodex session identity"
        )
    registry_id = lookup_rodex_registry_id(database_path)
    if advertised.rodex_registry_id != registry_id:
        raise RodexLaunchError(
            "the current tmux pane belongs to a different Rodex registry: "
            f"expected {registry_id}, "
            f"observed {advertised.rodex_registry_id or 'missing'}"
        )
    session_id = lookup_rodex_sessions_id_from_a_rodex_session_id(
        advertised.rodex_session_id,
        database_path,
    )
    if session_id is None:
        raise RodexLaunchError(
            "the current tmux pane advertises a Rodex identity absent from this registry"
        )
    persisted = next(
        (
            runtime
            for runtime in list_rodex_session_runtimes_for_a_user(database_path)
            if runtime.rodex_sessions_id == session_id
        ),
        None,
    )
    if persisted is None:
        raise RodexLaunchError(
            "the current tmux pane is not owned by the current POSIX user"
        )
    recorded_tmux = LiveTmuxSession(
        Path(persisted.tmux_server_socket_path),
        persisted.tmux_session_name,
    )
    if recorded_tmux != live_tmux:
        raise RodexLaunchError(
            "the current tmux pane does not match its recorded Rodex endpoint: "
            f"expected {recorded_tmux.tmux_session_name} on "
            f"{recorded_tmux.tmux_server_socket_path}, observed "
            f"{live_tmux.tmux_session_name} on {live_tmux.tmux_server_socket_path}"
        )
    control = _verify_live_runtime_identity(
        launcher,
        live_tmux,
        expected_rodex_session_id=advertised.rodex_session_id,
        expected_registry_id=registry_id,
        expected_codex_session_id=persisted.codex_session_id,
    )
    print(
        json.dumps(
            {
                "managed_by": "rodex",
                "rodex_session_name": persisted.display_name,
                "rodex_permanent_name": persisted.cool_name,
                "rodex_user_defined_name": persisted.user_defined_cool_name,
                "rodex_session_id": str(advertised.rodex_session_id),
                "rodex_registry_id": str(registry_id),
                "rodex_database_path": str(database_path),
                "codex_session_id": str(control.codex_session_id),
                "tmux_server_socket_path": str(live_tmux.tmux_server_socket_path),
                "tmux_session_name": live_tmux.tmux_session_name,
                "tmux_session_id": tmux_context.tmux_session_id,
                "tmux_window_id": tmux_context.tmux_window_id,
                "tmux_pane_id": tmux_context.tmux_pane_id,
                "registration_state": control.registration_state,
                "attached_clients": tmux_context.attached_client_count,
                "shared": tmux_context.attached_client_count > 1,
            },
            indent=2,
        ),
        flush=True,
    )


def _print_running_sessions(
    database_path: Path,
    launcher: RodexRuntimeLauncher,
) -> None:
    persisted = list_rodex_session_runtimes_for_a_user(database_path)
    registry_id = lookup_rodex_registry_id(database_path)
    running = []
    identity_failures: list[tuple[str, str]] = []
    registered_endpoints: set[tuple[Path, str]] = set()
    sockets = {default_tmux_server_socket_path()}
    for runtime in persisted:
        socket_path = Path(runtime.tmux_server_socket_path)
        sockets.add(socket_path)
        registered_endpoints.add((socket_path, runtime.tmux_session_name))
        live = LiveTmuxSession(socket_path, runtime.tmux_session_name)
        if not launcher.session_exists(live):
            continue
        expected_rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(
            runtime.rodex_sessions_id, database_path
        )
        if expected_rodex_session_id is None:
            identity_failures.append(
                (runtime.display_name, "missing durable Rodex session ID")
            )
            continue
        try:
            _verify_live_runtime_identity(
                launcher,
                live,
                expected_rodex_session_id=expected_rodex_session_id,
                expected_registry_id=registry_id,
                expected_codex_session_id=runtime.codex_session_id,
            )
        except (RodexLaunchError, RodexRuntimeError) as error:
            identity_failures.append((runtime.display_name, str(error)))
            continue
        running.append(runtime)

    unregistered: list[tuple[Path, str]] = []
    for socket_path in sorted(sockets, key=str):
        if not socket_path.exists():
            continue
        for name in launcher.list_session_names(socket_path):
            endpoint = (socket_path, name)
            if endpoint not in registered_endpoints:
                unregistered.append(endpoint)

    if not running and not identity_failures and not unregistered:
        print("No running Rodex sessions.", flush=True)
        return
    print(f"Running Rodex sessions: {len(running)}", flush=True)
    for runtime in running:
        print(
            f"{runtime.display_name} -> Codex {runtime.codex_session_id}",
            flush=True,
        )
    if identity_failures:
        print(f"Unverified registered runtimes: {len(identity_failures)}", flush=True)
        for name, reason in identity_failures:
            print(f"{name}: {reason}", flush=True)
    if unregistered:
        print(f"Unregistered live tmux sessions: {len(unregistered)}", flush=True)
        for socket_path, name in unregistered:
            print(f"{name} on {socket_path}", flush=True)


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
