"""Verified live-runtime resolution and transition coordination."""

from __future__ import annotations

import fcntl
import os
import stat as stat_module
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import local

from rodex_registry import (
    CodexSessionId,
    RodexRegistryId,
    RodexRuntimeRegistrationRejectedError,
    RodexSessionId,
    lookup_owned_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_registry_id,
    lookup_rodex_runtime_instance,
    lookup_rodex_runtime_registration,
    lookup_rodex_session_id_from_a_rodex_sessions_id,
    record_a_rodex_session_runtime_resume,
    repair_rodex_runtime_tmux_session_name,
)

from .control import LiveRodexControl
from .errors import ExactRuntimeIdentityRequiredError, RodexLaunchError
from .runtime import (
    RODEX_REGISTRATION_PENDING,
    RODEX_REGISTRATION_REGISTERED,
    LiveTmuxSession,
    RodexRuntimeError,
    RodexRuntimeLauncher,
)

_transition_lock_ownership = local()


@contextmanager
def session_transition_lock(
    database_path: Path,
    session_identity: RodexSessionId,
) -> Iterator[None]:
    """Serialize one durable session's publication, liveness, and replacement."""
    if not isinstance(session_identity, RodexSessionId):
        raise TypeError("session transition identity must be a RodexSessionId")
    database_path = database_path.expanduser().resolve()
    owner_process_id = os.getpid()
    ownership_key = (owner_process_id, database_path, session_identity)
    held_locks = getattr(_transition_lock_ownership, "held_locks", None)
    if held_locks is None:
        held_locks = set()
        _transition_lock_ownership.held_locks = held_locks
    if ownership_key in held_locks:
        yield
        return
    lock_path = database_path.parent / f".{database_path.name}.session-{session_identity}.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        state = os.fstat(descriptor)
        if not stat_module.S_ISREG(state.st_mode) or state.st_uid != os.getuid():
            raise RodexLaunchError(f"session transition lock is not a private regular file: {lock_path}")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        held_locks.add(ownership_key)
        try:
            yield
        finally:
            held_locks.remove(ownership_key)
    finally:
        if os.getpid() == owner_process_id:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def resolve_live_control(
    session_name: str,
    database_path: Path,
    launcher: RodexRuntimeLauncher,
) -> tuple[int, LiveTmuxSession, LiveRodexControl]:
    session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(session_name, database_path)
    if session_id is None:
        raise RodexLaunchError(f"unknown Rodex session: {session_name}")
    expected_rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(session_id, database_path)
    if expected_rodex_session_id is None:
        raise RodexLaunchError(f"Rodex session has no Rodex identity: {session_name}")
    with session_transition_lock(database_path, expected_rodex_session_id):
        if lookup_owned_rodex_sessions_id_from_a_cool_name(session_name, database_path) != session_id:
            raise RodexLaunchError(f"Rodex selector changed while resolving its runtime: {session_name}")
        return _resolve_live_control_locked(session_name, database_path, launcher, session_id, expected_rodex_session_id)


def _resolve_live_control_locked(
    session_name: str,
    database_path: Path,
    launcher: RodexRuntimeLauncher,
    session_id: int,
    expected_rodex_session_id: RodexSessionId,
) -> tuple[int, LiveTmuxSession, LiveRodexControl]:
    registration = lookup_rodex_runtime_registration(session_id, database_path)
    if registration is None:
        raise RodexLaunchError(f"Rodex session has no tmux endpoint: {session_name}")
    tmux_link = registration.tmux_session
    runtime = LiveTmuxSession(
        Path(tmux_link.tmux_server_socket_path), tmux_link.tmux_session_name, runtime_id=registration.runtime_id
    )
    if not launcher.session_exists(runtime):
        raise RodexLaunchError(f"Rodex session is not running: {session_name}")
    expected_codex_session_id = registration.codex_session_id
    if expected_codex_session_id is None:
        raise RodexLaunchError(f"Rodex session has no Codex identity: {session_name}")
    expected_registry_id = lookup_rodex_registry_id(database_path)
    control = verify_live_runtime_identity(
        launcher,
        runtime,
        session_id=session_id,
        database_path=database_path,
        expected_rodex_session_id=expected_rodex_session_id,
        expected_registry_id=expected_registry_id,
        expected_codex_session_id=expected_codex_session_id,
    )
    if control.runtime_id is None:
        raise RodexLaunchError("live runtime did not advertise its exact runtime incarnation")
    if control.tmux_capability is None:
        raise RodexLaunchError("live runtime did not advertise registered tmux authority")
    runtime = replace(
        runtime,
        tmux_session_name=control.tmux_session_name or runtime.tmux_session_name,
        runtime_id=control.runtime_id,
        tmux_capability=control.tmux_capability,
        tmux_runtime_capability=control.tmux_runtime_capability,
    )
    return session_id, runtime, control


def verify_live_runtime_identity(
    launcher: RodexRuntimeLauncher,
    runtime: LiveTmuxSession,
    *,
    session_id: int,
    database_path: Path,
    expected_rodex_session_id: RodexSessionId,
    expected_registry_id: RodexRegistryId,
    expected_codex_session_id: CodexSessionId,
) -> LiveRodexControl:
    """Complete and validate registration under the owning session transition."""
    with session_transition_lock(database_path, expected_rodex_session_id):
        return _verify_live_runtime_identity_locked(
            launcher,
            runtime,
            session_id=session_id,
            database_path=database_path,
            expected_rodex_session_id=expected_rodex_session_id,
            expected_registry_id=expected_registry_id,
            expected_codex_session_id=expected_codex_session_id,
        )


def _verify_live_runtime_identity_locked(
    launcher: RodexRuntimeLauncher,
    runtime: LiveTmuxSession,
    *,
    session_id: int,
    database_path: Path,
    expected_rodex_session_id: RodexSessionId,
    expected_registry_id: RodexRegistryId,
    expected_codex_session_id: CodexSessionId,
) -> LiveRodexControl:
    registration = lookup_rodex_runtime_registration(session_id, database_path)
    if (
        registration is None
        or Path(registration.tmux_session.tmux_server_socket_path) != runtime.tmux_server_socket_path
        or registration.tmux_session.tmux_session_name != runtime.tmux_session_name
        or registration.codex_session_id != expected_codex_session_id
    ):
        raise RodexRuntimeRegistrationRejectedError("live discovery no longer matches its durable runtime endpoint")
    control = launcher.discover_runtime_control(runtime)
    require_live_runtime_identity(
        control,
        expected_rodex_session_id=expected_rodex_session_id,
        expected_registry_id=expected_registry_id,
        expected_codex_session_id=expected_codex_session_id,
        allow_pending=True,
    )
    if registration.runtime_id is None:
        raise ExactRuntimeIdentityRequiredError("live runtime lacks its required durable incarnation")
    if control.runtime_id != registration.runtime_id:
        if control.registration_state == RODEX_REGISTRATION_PENDING:
            raise RodexRuntimeRegistrationRejectedError("a pending reader cannot replace the durable runtime incarnation")
        raise RodexLaunchError("live runtime advertised an unexpected durable runtime incarnation")
    if control.registration_state == RODEX_REGISTRATION_REGISTERED and (
        control.tmux_capability is None or control.tmux_capability.internal_session_id != session_id
    ):
        raise RodexLaunchError("live runtime advertised an unexpected internal session identity")
    primary = control.tmux_runtime_capability
    if runtime.tmux_runtime_capability is not None and primary != runtime.tmux_runtime_capability:
        raise RodexLaunchError("live runtime primary authority changed during verification")
    if primary is not None and (
        primary.tmux_server_socket_path != runtime.tmux_server_socket_path
        or primary.runtime_id != control.runtime_id
        or (control.tmux_capability is not None and control.tmux_capability.runtime_capability != primary)
    ):
        raise RodexLaunchError("live runtime primary authority disagrees with registration")
    identified_runtime = replace(
        runtime,
        tmux_session_name=control.tmux_session_name or runtime.tmux_session_name,
        runtime_id=control.runtime_id,
        tmux_capability=control.tmux_capability,
        tmux_runtime_capability=primary,
    )
    if identified_runtime.tmux_session_name != runtime.tmux_session_name:
        if primary is None:
            raise RodexLaunchError("runtime name repair requires exact primary tmux authority")
        if launcher.discover_runtime_control(identified_runtime) != control:
            raise RodexLaunchError("live runtime changed before name repair")
        repaired_tmux = repair_rodex_runtime_tmux_session_name(
            registration, identified_runtime.tmux_session_name, database_path
        )
        registration = replace(registration, tmux_session=repaired_tmux)
        if launcher.discover_runtime_control(identified_runtime) != control:
            raise RodexLaunchError("live runtime changed after name repair")
    if control.registration_state == RODEX_REGISTRATION_PENDING:
        record_a_rodex_session_runtime_resume(
            session_id,
            identified_runtime.tmux_server_socket_path,
            identified_runtime.tmux_session_name,
            database_path,
            codex_session_id=expected_codex_session_id,
            runtime_id=control.runtime_id,
            expected_previous_runtime_id=registration.runtime_id,
        )
        launcher.confirm_runtime_registration(
            identified_runtime,
            session_id,
            expected_rodex_session_id=expected_rodex_session_id,
            expected_registry_id=expected_registry_id,
            expected_codex_session_id=expected_codex_session_id,
        )
        control = launcher.discover_runtime_control(identified_runtime)
    require_live_runtime_identity(
        control,
        expected_rodex_session_id=expected_rodex_session_id,
        expected_registry_id=expected_registry_id,
        expected_codex_session_id=expected_codex_session_id,
    )
    capability = control.tmux_capability
    if capability is None or capability.internal_session_id != session_id:
        raise RodexLaunchError("live runtime advertised an unexpected internal session identity")
    if primary is not None and (control.tmux_runtime_capability != primary or capability.runtime_capability != primary):
        raise RodexLaunchError("live runtime primary authority changed during registration")
    if control.tmux_session_name is not None and control.tmux_session_name != registration.tmux_session.tmux_session_name:
        raise RodexLaunchError("live runtime name changed during verification")
    require_durable_runtime_instance(session_id, database_path, control)
    if lookup_rodex_runtime_registration(session_id, database_path) != registration:
        raise RodexRuntimeRegistrationRejectedError("durable runtime registration changed during verification")
    return control


def require_durable_runtime_instance(
    session_id: int,
    database_path: Path,
    control: LiveRodexControl,
) -> None:
    """Require the current durable incarnation for every managed read, attach, or mutation."""
    durable_runtime = lookup_rodex_runtime_instance(session_id, database_path)
    if durable_runtime is None or control.runtime_id is None:
        raise ExactRuntimeIdentityRequiredError("live runtime lacks its required durable incarnation")
    if durable_runtime.runtime_id != control.runtime_id:
        raise RodexLaunchError("live runtime advertised an unexpected durable runtime incarnation")


def require_live_runtime_identity(
    control: LiveRodexControl,
    *,
    expected_rodex_session_id: RodexSessionId,
    expected_registry_id: RodexRegistryId,
    expected_codex_session_id: CodexSessionId,
    allow_pending: bool = False,
) -> None:
    if control.registration_state != RODEX_REGISTRATION_REGISTERED and not (
        allow_pending and control.registration_state == RODEX_REGISTRATION_PENDING
    ):
        raise RodexLaunchError(
            "live runtime is not durably registered: "
            f"expected {RODEX_REGISTRATION_REGISTERED}, "
            f"observed {control.registration_state or 'missing'}"
        )
    if control.runtime_id is None:
        raise RodexLaunchError("live runtime did not advertise its exact runtime incarnation")
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


def find_relocated_live_runtime(
    launcher: RodexRuntimeLauncher,
    tmux_server_socket_path: Path,
    *,
    expected_rodex_session_id: RodexSessionId,
    expected_registry_id: RodexRegistryId,
    expected_codex_session_id: CodexSessionId,
) -> tuple[LiveTmuxSession, LiveRodexControl] | None:
    matches: list[tuple[LiveTmuxSession, LiveRodexControl]] = []
    conflicting_same_registry: list[str] = []
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
            and control.registration_state in {RODEX_REGISTRATION_PENDING, RODEX_REGISTRATION_REGISTERED}
        ):
            matches.append((candidate, control))
        elif control.rodex_registry_id == expected_registry_id:
            conflicting_same_registry.append(name)
    if conflicting_same_registry:
        raise RodexLaunchError(
            "live runtime in the expected registry has the Codex identity but not "
            "the matching Rodex identity: " + ", ".join(sorted(conflicting_same_registry))
        )
    if len(matches) > 1:
        raise RodexLaunchError(
            "multiple live runtimes advertise the same Rodex/Codex identity: "
            + ", ".join(sorted(item.tmux_session_name for item, _control in matches))
        )
    if not matches:
        return None
    return matches[0]


def revalidate_live_control(
    launcher: RodexRuntimeLauncher,
    runtime: LiveTmuxSession,
    expected: LiveRodexControl,
) -> None:
    if not launcher.session_exists(runtime):
        raise RodexLaunchError("Rodex runtime ended during control discovery")
    if launcher.discover_runtime_control(runtime) != expected:
        raise RodexLaunchError("Rodex runtime changed during control discovery")


def rename_tmux_identity(
    launcher: RodexRuntimeLauncher,
    active_tmux: LiveTmuxSession,
    display_name: str,
) -> LiveTmuxSession:
    """Make the canonical per-user display name the live tmux session name."""
    if active_tmux.tmux_session_name == display_name:
        return active_tmux
    return launcher.rename(active_tmux, display_name)


def restore_tmux_identity(
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
