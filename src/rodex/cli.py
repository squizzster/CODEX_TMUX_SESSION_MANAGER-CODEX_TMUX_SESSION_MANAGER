"""Launch a normal Codex TUI with Rodex identity and tmux durability."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from cool_name import (
    CoolNameError,
    normalise_rodex_display_name,
)
from rodex_functions import (
    RodexSessionError,
    create_a_rodex_session,
    default_rodex_database_path,
    generate_an_unregistered_rodex_uuid_candidate,
    list_rodex_session_runtimes_for_a_user,
    lookup_codex_uuid_from_a_rodex_session_id,
    lookup_owned_rodex_session_id_from_a_cool_name,
    lookup_rodex_session_id_from_a_cool_name,
    lookup_rodex_session_names,
    lookup_rodex_tmux_session,
    lookup_rodex_uuid_from_an_id,
    open_a_user_defined_cool_name_assignment,
    read_rodex_session_statistics,
    record_a_rodex_session_access,
    record_a_rodex_session_runtime_resume,
    update_rodex_tmux_session_name,
)
from rodex_sql import RodexSQLError

from .control import CodexControlClient, LiveRodexControl, RodexControlError
from .runtime import (
    LiveTmuxSession,
    RodexCodexSessionNotFoundError,
    RodexRuntimeError,
    RodexRuntimeLauncher,
)


class RodexLaunchError(RuntimeError):
    """Rodex could not launch its Codex/tmux runtime."""


class RodexExecutableNotFoundError(RodexLaunchError):
    """A required executable could not be resolved from PATH."""


_RUNNING_COMMAND: Final = "_running"
_ALIAS_COMMAND: Final = "_alias"
_SEND_COMMAND: Final = "_send"
_WAIT_COMMAND: Final = "_wait"
_TAIL_COMMAND: Final = "_tail"
_CREATE_COMMAND: Final = "_create"
_DETACH_COMMAND: Final = "_detach"
_HELP_COMMAND: Final = "_help"
_STATS_COMMAND: Final = "_stats"
_STATS_STATUS_COMMAND: Final = "_stats-status"
_FORCE_FLAG: Final = "--force"
_RODEX_COMMANDS: Final = frozenset(
    {
        _RUNNING_COMMAND,
        _ALIAS_COMMAND,
        _SEND_COMMAND,
        _WAIT_COMMAND,
        _TAIL_COMMAND,
        _CREATE_COMMAND,
        _DETACH_COMMAND,
        _HELP_COMMAND,
        _STATS_COMMAND,
        _STATS_STATUS_COMMAND,
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
  _alias [--force] SESSION NAME      Assign a preferred session name.
  _send SESSION PROMPT               Send work to a running session.
  _wait SESSION                      Wait until a running session is idle.
  _tail SESSION                      Follow live protocol events as JSON lines.
  _stats SESSION [--json]            Show the latest persistent aggregate statistics.
  _stats-status SESSION              Show analytics freshness and health.

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
        Path(database_path).expanduser().resolve()
        if database_path is not None
        else default_rodex_database_path()
    )
    rodex_command = (
        arguments[0] if arguments[:1] and arguments[0] in _RODEX_COMMANDS else None
    )
    possible_existing_name = _possible_existing_rodex_name(arguments, resolved_database)
    if rodex_command is None and not possible_existing_name:
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
            lookup_rodex_session_id_from_a_cool_name(requested_name, resolved_database)
            is not None
        ):
            raise RodexLaunchError(f"Rodex session name already exists: {requested_name}")

    planned_rodex_uuid = generate_an_unregistered_rodex_uuid_candidate(resolved_database)
    live_runtime, codex_session_uuid = runtime_launcher.start(
        Path.cwd(),
        codex_arguments,
        rodex_session_uuid=planned_rodex_uuid,
        rodex_database_path=resolved_database,
    )
    active_tmux: LiveTmuxSession = live_runtime
    try:
        session = create_a_rodex_session(
            resolved_database,
            codex_session_uuid=codex_session_uuid,
            rodex_session_uuid=planned_rodex_uuid,
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
    rodex_uuid = lookup_rodex_uuid_from_an_id(session_id, database_path)
    if rodex_uuid is None:
        raise RodexLaunchError(f"Rodex session has no Rodex identity: {cool_name}")

    replaced_unsaved_codex_identity = False
    try:
        resumed_runtime, observed_codex_uuid = launcher.start(
            Path.cwd(),
            ["resume", str(codex_session_uuid)],
            rodex_session_uuid=rodex_uuid,
            rodex_database_path=database_path,
        )
    except RodexCodexSessionNotFoundError:
        try:
            resumed_runtime, observed_codex_uuid = launcher.start(
                Path.cwd(),
                [],
                rodex_session_uuid=rodex_uuid,
                rodex_database_path=database_path,
            )
        except RodexRuntimeError as error:
            raise RodexLaunchError(
                f"Rodex session {display_name!r} is recorded but not running; "
                f"Codex session {codex_session_uuid} was not saved and a replacement "
                f"Codex runtime could not be started: {error}"
            ) from error
        replaced_unsaved_codex_identity = True
    except RodexRuntimeError as error:
        raise RodexLaunchError(
            f"Rodex session {display_name!r} is recorded but not running; "
            f"Codex session {codex_session_uuid} could not be resumed: {error}"
        ) from error
    active_tmux: LiveTmuxSession = resumed_runtime
    try:
        if (
            not replaced_unsaved_codex_identity
            and observed_codex_uuid != codex_session_uuid
        ):
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
            codex_session_uuid=(
                observed_codex_uuid if replaced_unsaved_codex_identity else None
            ),
        )
        launcher.configure_identity_status(active_tmux)
    except BaseException:
        launcher.stop(active_tmux, check=False)
        raise

    if detach:
        _print_existing_detached_runtime(session_id, display_name, database_path)
        return True
    action = "Recovered" if replaced_unsaved_codex_identity else "Resumed"
    print(
        f"{action} Rodex {display_name} -> Codex {observed_codex_uuid} "
        f"({active_tmux.tmux_session_name})",
        flush=True,
    )
    launcher.attach(active_tmux)
    return True


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
            lookup_owned_rodex_session_id_from_a_cool_name(arguments[0], database_path)
            is not None
        )
    except (CoolNameError, ValueError):
        return False


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
    if command == _RUNNING_COMMAND:
        if len(arguments) != 1:
            raise RodexLaunchError("usage: rodex _running")
        _print_running_sessions(database_path, launcher)
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


def _run_statistics_command(arguments: list[str], database_path: Path) -> bool:
    """Serve persistent aggregate statistics without requiring Codex or tmux."""
    if not arguments or arguments[0] not in {_STATS_COMMAND, _STATS_STATUS_COMMAND}:
        return False
    command = arguments[0]
    if command == _STATS_STATUS_COMMAND:
        if len(arguments) != 2:
            raise RodexLaunchError("usage: rodex _stats-status SESSION_NAME")
        session_name = arguments[1]
        as_json = False
    else:
        if len(arguments) not in {2, 3} or (
            len(arguments) == 3 and arguments[2] != "--json"
        ):
            raise RodexLaunchError("usage: rodex _stats SESSION_NAME [--json]")
        session_name = arguments[1]
        as_json = len(arguments) == 3
    session_id = lookup_owned_rodex_session_id_from_a_cool_name(session_name, database_path)
    if session_id is None:
        raise RodexLaunchError(f"unknown Rodex session: {session_name}")
    view = read_rodex_session_statistics(session_id, database_path)
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
    payload["statistics"] = snapshot.aggregate_statistics
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    else:
        _print_human_statistics(payload)
    return True


def _print_human_statistics(payload: dict[str, object]) -> None:
    statistics = payload["statistics"]
    if not isinstance(statistics, dict):
        raise RodexLaunchError("stored analytics snapshot is invalid")
    print(
        f"Rodex {payload['rodex_session_name']} statistics "
        f"(revision {payload['statistics_revision']}, "
        f"{payload['worker_state']})",
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
        if argument == _FORCE_FLAG:
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
