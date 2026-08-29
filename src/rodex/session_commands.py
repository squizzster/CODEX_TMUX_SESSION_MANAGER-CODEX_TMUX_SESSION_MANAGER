"""Human-facing commands for verified live Rodex sessions."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from rodex_registry import (
    RodexSessionError,
    list_rodex_session_runtimes_for_a_user,
    lookup_owned_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_registry_id,
    lookup_rodex_runtime_instance,
    lookup_rodex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_sessions_id_from_a_rodex_session_id,
    lookup_rodex_tmux_session,
    record_a_rodex_session_access,
)
from rodex_sql import RodexDatabaseMovedError, RodexSQLError

from .command_contract import (
    ALIAS_COMMAND,
    CAT_COMMAND,
    CONTEXT_COMMAND,
    EVENTS_COMMAND,
    FORCE_FLAG,
    MOUSE_COMMAND,
    RUNNING_COMMAND,
    TAIL_COMMAND,
    WAIT_COMMAND,
)
from .control import CodexControlClient, LiveRodexControl
from .errors import RodexLaunchError
from .exact_turn_mutation import ExactTurnMutationCoordinator
from .live_runtime import (
    resolve_live_control,
    revalidate_live_control,
    verify_live_runtime_identity,
)
from .runtime import (
    LiveTmuxSession,
    RodexRuntimeError,
    RodexRuntimeLauncher,
    default_tmux_server_socket_path,
)
from .session_read_pipeline import LiveSessionReadPipeline
from .session_tail import follow_session_tail, parse_session_tail_request


def execute_session_command(
    arguments: list[str],
    database_path: Path,
    launcher: RodexRuntimeLauncher,
    control_client: CodexControlClient,
) -> None:
    if not arguments:
        raise AssertionError("application pipeline selected an empty session command")
    command = arguments[0]
    if command == CONTEXT_COMMAND:
        if tuple(arguments) not in {
            (CONTEXT_COMMAND,),
            (CONTEXT_COMMAND, "--json"),
        }:
            raise RodexLaunchError("usage: rodex _context [--json]")
        _print_current_rodex_context(database_path, launcher)
        return
    if command == RUNNING_COMMAND:
        if len(arguments) != 1:
            raise RodexLaunchError("usage: rodex _running")
        _print_running_sessions(database_path, launcher)
        return
    if command == MOUSE_COMMAND:
        if len(arguments) not in {2, 3}:
            raise RodexLaunchError(
                "usage: rodex _mouse SESSION_NAME [on|off|toggle|inherit|status]"
            )
        mode = arguments[2] if len(arguments) == 3 else "status"
        if mode not in {"on", "off", "toggle", "inherit", "status"}:
            raise RodexLaunchError(
                "usage: rodex _mouse SESSION_NAME [on|off|toggle|inherit|status]"
            )
        session_id, runtime, _ = resolve_live_control(arguments[1], database_path, launcher)
        mouse_state = launcher.set_mouse_mode(runtime, mode)
        _record_access_best_effort(session_id, database_path)
        print(f"Rodex {arguments[1]} mouse: {mouse_state}", flush=True)
        return
    if command == WAIT_COMMAND:
        if len(arguments) != 2:
            raise RodexLaunchError("usage: rodex _wait SESSION_NAME")
        session_id, runtime, control = resolve_live_control(
            arguments[1], database_path, launcher
        )
        control_client.wait_until_idle(
            control,
            revalidate=lambda: revalidate_live_control(launcher, runtime, control),
        )
        _record_access_best_effort(session_id, database_path)
        print(f"Rodex {arguments[1]}: Codex turn complete", flush=True)
        return
    if command == CAT_COMMAND:
        if len(arguments) != 2:
            raise RodexLaunchError("usage: rodex _cat SESSION_NAME")
        scrollback = LiveSessionReadPipeline(database_path, launcher).snapshot(
            arguments[1], launcher.capture_scrollback
        )
        if scrollback:
            sys.stdout.write("\n".join(scrollback) + "\n")
            sys.stdout.flush()
        return
    if command == TAIL_COMMAND:
        request = parse_session_tail_request(arguments)
        LiveSessionReadPipeline(database_path, launcher).stream_scrollback(
            request.session_name,
            lambda runtime, revalidate: follow_session_tail(
                request,
                runtime,
                launcher.capture_scrollback_snapshot,
                launcher.capture_scrollback_state,
                revalidate,
            ),
        )
        return
    if command == EVENTS_COMMAND:
        if len(arguments) != 2:
            raise RodexLaunchError("usage: rodex _events SESSION_NAME")
        _refuse_same_session_event_sink(arguments[1], database_path, launcher)
        LiveSessionReadPipeline(database_path, launcher).stream_events(
            arguments[1],
            lambda control, revalidate: _stream_protocol_events(
                arguments[1],
                control_client,
                control,
                revalidate,
            ),
        )
        return
    if command == ALIAS_COMMAND:
        force, operands = _parse_alias_arguments(arguments[1:])
        if len(operands) != 2:
            raise RodexLaunchError(
                "usage: rodex _alias [--force] SESSION_NAME USER_DEFINED_NAME"
            )
        mutation_coordinator = ExactTurnMutationCoordinator(
            database_path,
            launcher,
            control_client,
        )
        display_name = mutation_coordinator.alias_transition(
            operands[0],
            operands[1],
            force=force,
        )
        print(f"Rodex name: {display_name}", flush=True)
        return
    raise AssertionError(
        f"application pipeline selected unknown session command: {command}"
    )


def _parse_alias_arguments(arguments: list[str]) -> tuple[bool, list[str]]:
    force = False
    operands: list[str] = []
    for argument in arguments:
        if argument == FORCE_FLAG:
            force = True
        elif argument.startswith("-"):
            raise RodexLaunchError(f"unknown alias option: {argument}")
        else:
            operands.append(argument)
    return force, operands


def _record_access_best_effort(session_id: int, database_path: Path) -> None:
    try:
        record_a_rodex_session_access(session_id, database_path)
    except RodexDatabaseMovedError:
        raise
    except (OSError, RodexSQLError, RodexSessionError, sqlite3.Error) as error:
        print(
            f"rodex: warning: access telemetry was not recorded: {error}",
            file=sys.stderr,
            flush=True,
        )


def _stream_protocol_events(
    session_name: str,
    control_client: CodexControlClient,
    control: LiveRodexControl,
    revalidate: Callable[[], None],
) -> None:
    print(
        f"Rodex {session_name}: following live Codex protocol events",
        file=sys.stderr,
        flush=True,
    )
    control_client.stream_events(
        control,
        lambda event: print(event, flush=True),
        revalidate=revalidate,
    )


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
    control = verify_live_runtime_identity(
        launcher,
        live_tmux,
        session_id=session_id,
        database_path=database_path,
        expected_rodex_session_id=advertised.rodex_session_id,
        expected_registry_id=registry_id,
        expected_codex_session_id=persisted.codex_session_id,
    )
    persisted_runtime = lookup_rodex_runtime_instance(session_id, database_path)
    runtime_identity_persisted = (
        persisted_runtime is not None and control.runtime_id == persisted_runtime.runtime_id
    )
    if persisted_runtime is not None and control.runtime_id != persisted_runtime.runtime_id:
        raise RodexLaunchError("the current tmux pane advertises an unexpected runtime ID")
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
                "runtime_id": (
                    None if control.runtime_id is None else str(control.runtime_id)
                ),
                "runtime_identity_persisted": runtime_identity_persisted,
                "attached_clients": tmux_context.attached_client_count,
                "shared": tmux_context.attached_client_count > 1,
            },
            indent=2,
        ),
        flush=True,
    )


def _refuse_same_session_event_sink(
    session_name: str,
    database_path: Path,
    launcher: RodexRuntimeLauncher,
) -> None:
    """Prevent protocol-event output from feeding the pane it observes."""
    if not os.environ.get("TMUX") or not os.environ.get("TMUX_PANE"):
        return
    session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(
        session_name, database_path
    )
    if session_id is None:
        return
    tmux = lookup_rodex_tmux_session(session_id, database_path)
    if tmux is None:
        return
    try:
        current = launcher.discover_current_tmux_pane_context()
    except (OSError, RodexRuntimeError, ValueError) as error:
        raise RodexLaunchError(
            "cannot safely open _events from this tmux pane because its session "
            "identity could not be verified; use another terminal or _trace --follow"
        ) from error
    if (
        current.tmux_session.tmux_session_name == tmux.tmux_session_name
        and current.tmux_session.tmux_server_socket_path
        == Path(tmux.tmux_server_socket_path)
    ):
        raise RodexLaunchError(
            "_events output cannot use the same Rodex session it observes; "
            "run it from another terminal or use _trace --follow"
        )


def _print_running_sessions(
    database_path: Path,
    launcher: RodexRuntimeLauncher,
) -> None:
    persisted = (
        list_rodex_session_runtimes_for_a_user(database_path)
        if database_path.exists()
        else []
    )
    registry_id = lookup_rodex_registry_id(database_path) if persisted else None
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
        assert registry_id is not None
        try:
            verify_live_runtime_identity(
                launcher,
                live,
                session_id=runtime.rodex_sessions_id,
                database_path=database_path,
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
