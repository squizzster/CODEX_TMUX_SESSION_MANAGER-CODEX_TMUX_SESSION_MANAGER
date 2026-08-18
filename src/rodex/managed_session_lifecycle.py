"""Create, open, resume, and recover managed Rodex session lifecycles."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from cool_name import (
    CoolNameError,
    normalise_rodex_display_name,
)
from rodex_registry import (
    CodexSessionId,
    RodexSessionId,
    create_a_rodex_session,
    generate_an_unregistered_rodex_session_id_candidate,
    lookup_codex_session_id_from_a_rodex_sessions_id,
    lookup_owned_rodex_sessions_id_from_a_codex_session_id,
    lookup_owned_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_registry_id,
    lookup_rodex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_session_names,
    lookup_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_tmux_session,
    open_a_user_defined_cool_name_assignment,
    parse_codex_session_id,
    record_a_rodex_session_access,
    record_a_rodex_session_runtime_resume,
    update_rodex_tmux_session_name,
)

from .command_contract import CREATE_COMMAND, DETACH_COMMAND
from .errors import RodexExecutableNotFoundError, RodexLaunchError
from .live_runtime import (
    find_relocated_live_runtime,
    rename_tmux_identity,
    require_live_runtime_identity,
    restore_tmux_identity,
    session_transition_lock,
    verify_live_runtime_identity,
)
from .runtime import (
    RODEX_REGISTRATION_PENDING,
    LiveTmuxSession,
    RodexCodexSessionNotFoundError,
    RodexRuntimeError,
    RodexRuntimeLauncher,
    default_tmux_server_socket_path,
)

ExecutableResolver = Callable[[str], str | None]
RuntimeLauncherFactory = Callable[[str, str], RodexRuntimeLauncher]


@dataclass(frozen=True, slots=True)
class OwnedSessionSelection:
    """One supplied selector resolved to a session owned by the current user."""

    supplied_selector: str
    rodex_sessions_id: int


@dataclass(frozen=True, slots=True)
class ManagedSessionLaunchRequest:
    """The launch-domain meaning parsed from one `_create` or `_detach` command."""

    codex_arguments: tuple[str, ...]
    requested_name: str | None
    detach: bool


@dataclass(frozen=True, slots=True)
class _PreparedSelectedSession:
    session_id: int
    display_name: str
    active_tmux: LiveTmuxSession
    attach_message: str


class ManagedSessionLifecycle:
    """Own create, select, attach, resume, recovery, and collision policy."""

    def resolve_selector(
        self, selector: str, database_path: Path
    ) -> OwnedSessionSelection | None:
        try:
            session_id = _lookup_owned_rodex_session_selector(selector, database_path)
        except (CoolNameError, ValueError):
            return None
        if session_id is None:
            return None
        return OwnedSessionSelection(selector, session_id)

    def execute_selector(
        self,
        selection: OwnedSessionSelection,
        database_path: Path,
        launcher: RodexRuntimeLauncher,
        *,
        codex_available: bool,
        configured_codex: str,
    ) -> int:
        _open_selected_session(
            selection,
            database_path,
            launcher,
            codex_available=codex_available,
            configured_codex=configured_codex,
            detach=False,
        )
        return 0

    def execute_launch(
        self,
        arguments: list[str],
        resolved_database: Path,
        runtime_launcher: RodexRuntimeLauncher,
        *,
        codex_binary: str | None,
        configured_codex: str,
    ) -> int:
        request = _parse_launch_arguments(arguments)
        selection = _resolve_session_arguments(
            request.codex_arguments,
            resolved_database,
            self,
        )
        if selection is not None:
            _open_selected_session(
                selection,
                resolved_database,
                runtime_launcher,
                codex_available=codex_binary is not None,
                configured_codex=configured_codex,
                detach=request.detach,
            )
            return 0
        return _create_managed_session(
            request,
            resolved_database,
            runtime_launcher,
            codex_binary=codex_binary,
            configured_codex=configured_codex,
        )

    def guard_unregistered_selector_collision(
        self,
        selector: str,
        *,
        configured_codex: str,
        configured_tmux: str,
        resolve_executable: ExecutableResolver,
        runtime_launcher_factory: RuntimeLauncherFactory,
    ) -> None:
        socket_path = default_tmux_server_socket_path()
        if not socket_path.exists():
            return
        tmux_binary = resolve_executable(configured_tmux)
        if tmux_binary is None:
            return
        active_launcher = runtime_launcher_factory(configured_codex, tmux_binary)
        candidate = LiveTmuxSession(socket_path, selector)
        if active_launcher.session_exists(candidate):
            raise RodexLaunchError(
                f"live tmux session {selector!r} exists on Rodex's private server "
                "but is not registered in this Rodex database; refusing Codex "
                "passthrough and unsafe adoption"
            )


def _create_managed_session(
    request: ManagedSessionLaunchRequest,
    resolved_database: Path,
    runtime_launcher: RodexRuntimeLauncher,
    *,
    codex_binary: str | None,
    configured_codex: str,
) -> int:
    if codex_binary is None:
        raise RodexExecutableNotFoundError(
            f"Codex executable was not found: {configured_codex}"
        )

    planned_rodex_session_id = generate_an_unregistered_rodex_session_id_candidate(
        resolved_database
    )
    requested_name = request.requested_name
    if requested_name is not None:
        requested_name = normalise_rodex_display_name(requested_name)
        if (
            lookup_rodex_sessions_id_from_a_cool_name(requested_name, resolved_database)
            is not None
        ):
            raise RodexLaunchError(f"Rodex session name already exists: {requested_name}")

    registry_id = lookup_rodex_registry_id(resolved_database)
    live_runtime, codex_session_id = runtime_launcher.start(
        Path.cwd(),
        list(request.codex_arguments),
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

    if request.detach:
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


def _open_selected_session(
    selection: OwnedSessionSelection,
    database_path: Path,
    launcher: RodexRuntimeLauncher,
    *,
    codex_available: bool,
    configured_codex: str,
    detach: bool,
) -> None:
    session_id = selection.rodex_sessions_id
    with session_transition_lock(database_path, session_id):
        prepared = _prepare_selected_session(
            session_id,
            selection.supplied_selector,
            database_path,
            launcher,
            codex_available=codex_available,
            configured_codex=configured_codex,
        )
    if detach:
        _print_existing_detached_runtime(
            prepared.session_id,
            prepared.display_name,
            database_path,
        )
        return
    print(prepared.attach_message, flush=True)
    launcher.attach(prepared.active_tmux)


def _prepare_selected_session(
    session_id: int,
    session_selector: str,
    database_path: Path,
    launcher: RodexRuntimeLauncher,
    *,
    codex_available: bool,
    configured_codex: str,
) -> _PreparedSelectedSession:
    """Resolve or resume one identity while its cross-process transition is locked."""
    names = lookup_rodex_session_names(session_id, database_path)
    if names is None:
        raise RodexLaunchError(f"Rodex session disappeared: {session_selector}")
    display_name = names.display_name
    tmux_link = lookup_rodex_tmux_session(session_id, database_path)
    if tmux_link is None:
        raise RodexLaunchError(f"Rodex session has no tmux endpoint: {session_selector}")
    recorded_tmux = LiveTmuxSession(
        tmux_server_socket_path=Path(tmux_link.tmux_server_socket_path),
        tmux_session_name=tmux_link.tmux_session_name,
    )
    codex_session_id = lookup_codex_session_id_from_a_rodex_sessions_id(
        session_id, database_path
    )
    if codex_session_id is None:
        raise RodexLaunchError(f"Rodex session has no Codex identity: {session_selector}")
    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(
        session_id, database_path
    )
    if rodex_session_id is None:
        raise RodexLaunchError(f"Rodex session has no Rodex identity: {session_selector}")
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
        return _PreparedSelectedSession(
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
        return _PreparedSelectedSession(
            session_id,
            display_name,
            active_tmux,
            f"Reattached relocated Rodex {display_name} ({active_tmux.tmux_session_name})",
        )

    if not codex_available:
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
    return _PreparedSelectedSession(
        session_id,
        display_name,
        active_tmux,
        f"{action} Rodex {display_name} -> Codex {observed_codex_session_id} "
        f"({active_tmux.tmux_session_name})",
    )


def _parse_launch_arguments(
    arguments: Sequence[str],
) -> ManagedSessionLaunchRequest:
    command = arguments[0]
    command_arguments = tuple(arguments[1:])
    if command == DETACH_COMMAND:
        return ManagedSessionLaunchRequest(
            _without_separator(command_arguments), None, True
        )
    if command != CREATE_COMMAND:
        raise AssertionError(f"lifecycle received unexpected launch command: {command}")
    if not command_arguments or command_arguments[0].startswith("-"):
        return ManagedSessionLaunchRequest(
            _without_separator(command_arguments), None, False
        )
    requested_name = command_arguments[0]
    return ManagedSessionLaunchRequest(
        _without_separator(command_arguments[1:]), requested_name, False
    )


def _without_separator(arguments: tuple[str, ...]) -> tuple[str, ...]:
    return arguments[1:] if arguments[:1] == ("--",) else arguments


def _resolve_session_arguments(
    arguments: tuple[str, ...],
    database_path: Path,
    lifecycle: ManagedSessionLifecycle,
) -> OwnedSessionSelection | None:
    if len(arguments) != 1 or arguments[0].startswith("-"):
        return None
    return lifecycle.resolve_selector(arguments[0], database_path)


def _lookup_owned_rodex_session_selector(
    session_selector: str, database_path: Path
) -> int | None:
    """Resolve a canonical Codex session UUID first, then an owned display name."""
    codex_session_id = _parse_canonical_codex_session_selector(session_selector)
    if codex_session_id is not None:
        session_id = lookup_owned_rodex_sessions_id_from_a_codex_session_id(
            codex_session_id, database_path
        )
        if session_id is not None:
            return session_id
    try:
        return lookup_owned_rodex_sessions_id_from_a_cool_name(
            session_selector, database_path
        )
    except CoolNameError:
        return None


def _parse_canonical_codex_session_selector(value: str) -> CodexSessionId | None:
    """Accept the hyphenated, case-insensitive UUID spelling used by Codex."""
    try:
        parsed = parse_codex_session_id(value)
    except (TypeError, ValueError):
        return None
    return parsed if str(parsed) == value.lower() else None


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
