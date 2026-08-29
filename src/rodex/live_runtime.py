"""Verified live-runtime resolution and transition coordination."""

from __future__ import annotations

import fcntl
import os
import stat as stat_module
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rodex_registry import (
    CodexSessionId,
    RodexRegistryId,
    RodexSessionId,
    lookup_codex_session_id_from_a_rodex_sessions_id,
    lookup_owned_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_registry_id,
    lookup_rodex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_tmux_session,
    record_a_rodex_session_runtime_resume,
)

from .control import LiveRodexControl
from .errors import RodexLaunchError
from .runtime import (
    RODEX_REGISTRATION_PENDING,
    RODEX_REGISTRATION_REGISTERED,
    LiveTmuxSession,
    RodexRuntimeError,
    RodexRuntimeLauncher,
)


@contextmanager
def session_transition_lock(
    database_path: Path,
    session_identity: RodexSessionId,
) -> Iterator[None]:
    """Serialize one durable session's publication, liveness, and replacement."""
    if not isinstance(session_identity, RodexSessionId):
        raise TypeError("session transition identity must be a RodexSessionId")
    lock_path = (
        database_path.parent / f".{database_path.name}.session-{session_identity}.lock"
    )
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


def resolve_live_control(
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
    control = verify_live_runtime_identity(
        launcher,
        runtime,
        session_id=session_id,
        database_path=database_path,
        expected_rodex_session_id=expected_rodex_session_id,
        expected_registry_id=expected_registry_id,
        expected_codex_session_id=expected_codex_session_id,
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
    control = launcher.discover_runtime_control(runtime)
    if (
        control.registration_state == RODEX_REGISTRATION_PENDING
        and control.rodex_session_id == expected_rodex_session_id
        and control.rodex_registry_id == expected_registry_id
        and control.codex_session_id == expected_codex_session_id
    ):
        if control.runtime_id is None:
            raise RodexLaunchError(
                "pending live runtime did not advertise its exact runtime identity"
            )
        record_a_rodex_session_runtime_resume(
            session_id,
            runtime.tmux_server_socket_path,
            runtime.tmux_session_name,
            database_path,
            runtime_id=control.runtime_id,
        )
        launcher.confirm_runtime_registration(runtime, session_id)
        control = launcher.discover_runtime_control(runtime)
    require_live_runtime_identity(
        control,
        expected_rodex_session_id=expected_rodex_session_id,
        expected_registry_id=expected_registry_id,
        expected_codex_session_id=expected_codex_session_id,
    )
    return control


def require_live_runtime_identity(
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


def find_relocated_live_runtime(
    launcher: RodexRuntimeLauncher,
    tmux_server_socket_path: Path,
    *,
    expected_rodex_session_id: RodexSessionId,
    expected_registry_id: RodexRegistryId,
    expected_codex_session_id: CodexSessionId,
) -> tuple[LiveTmuxSession, LiveRodexControl] | None:
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
