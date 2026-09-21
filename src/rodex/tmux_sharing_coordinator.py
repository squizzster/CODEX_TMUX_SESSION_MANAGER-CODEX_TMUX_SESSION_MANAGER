"""Server-global discovery for session-local sharing transitions.

tmux client hooks do not reliably retain the detached client's source session after
that session is destroyed.  Hooks therefore carry no session authority here.  They
only wake this coordinator, which enumerates live sessions and submits each changed
count through an exact, runtime-fenced capability.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .daemon_client import RODEX_RUNTIME_WAKE_TERMINAL_RESIZE, RodexDaemonClient, RodexDaemonError
from .installation import RodexInstallationError, retained_installation_interpreter
from .legacy_runtime_compat import (
    LEGACY_MUTABLE_TMUX_PROTOCOL,
    LegacyRuntimeCompatibilityError,
    notify_legacy_runtime_resize,
)
from .status_animation_admission import status_animation_admission_command
from .tmux_executor import SyncTmuxExecutor, SyncTmuxRunner
from .tmux_session_capability import (
    RODEX_CODEX_SESSION_ID_OPTION,
    RODEX_INTERNAL_SESSION_ID_OPTION,
    RODEX_PRIMARY_PANE_ID_OPTION,
    RODEX_REGISTRATION_REGISTERED,
    RODEX_REGISTRATION_STATE_OPTION,
    RODEX_REGISTRY_ID_OPTION,
    RODEX_RUNTIME_ID_OPTION,
    RODEX_SESSION_ID_OPTION,
    RODEX_SHARED_TMUX_PROTOCOL,
    RODEX_SHARED_TMUX_PROTOCOL_OPTION,
    RODEX_SHARED_TMUX_SERVER_ID_OPTION,
    TmuxSessionCapability,
    combine_tmux_if_shell_conditions,
    parse_tmux_session_capability,
    registered_primary_pane_if_shell_condition,
    server_identity_if_shell_condition,
    tmux_format_literal,
)

RODEX_SHARING_ATTACHED_COUNT_OPTION: Final = "@rodex_sharing_attached_count"
RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION: Final = "@rodex_shared_tmux_coordinator_command"
RODEX_SHARED_TMUX_HOOK_INDEX: Final = 731
RODEX_LEGACY_COORDINATION_HOOKS: Final = (
    "client-attached",
    "client-detached",
    "client-session-changed",
    "client-resized",
    "after-kill-pane",
    "after-resize-pane",
    "after-resize-window",
    "after-select-layout",
    "after-split-window",
)
_SESSION_RECORD_FORMAT: Final = "\t".join(
    (
        "#{session_id}",
        f"#{{{RODEX_PRIMARY_PANE_ID_OPTION}}}",
        "#{session_attached}",
        f"#{{{RODEX_SHARING_ATTACHED_COUNT_OPTION}}}",
        f"#{{{RODEX_RUNTIME_ID_OPTION}}}",
        f"#{{{RODEX_REGISTRATION_STATE_OPTION}}}",
        f"#{{{RODEX_SESSION_ID_OPTION}}}",
        f"#{{{RODEX_REGISTRY_ID_OPTION}}}",
        f"#{{{RODEX_INTERNAL_SESSION_ID_OPTION}}}",
        f"#{{{RODEX_CODEX_SESSION_ID_OPTION}}}",
    )
)


@dataclass(frozen=True, slots=True)
class _SharingState:
    capability: TmuxSessionCapability
    attached_count: int
    previous_attached_count: int | None


def sharing_coordinator_hook_command(
    python_executable: str,
    tmux_binary: str,
    tmux_server_socket_path: Path,
    tmux_server_id: str,
    *,
    isolated: bool = True,
) -> str:
    """Build a global hook that wakes discovery without conveying authority."""
    arguments = [tmux_format_literal(python_executable)]
    if isolated:
        arguments.append("-I")
    arguments.extend(
        (
            "-m",
            "rodex.tmux_sharing_coordinator",
            "--tmux-binary",
            tmux_format_literal(tmux_binary),
            "--tmux-server-socket",
            tmux_format_literal(str(tmux_server_socket_path)),
            "--expected-server-id",
            tmux_server_id,
        )
    )
    command = shlex.join(arguments)
    return shlex.join(("run-shell", "-b", f"exec {command} >/dev/null 2>&1"))


def reconcile_sharing_state(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    expected_server_id: str,
    *,
    python_executable: str = sys.executable,
    runner: SyncTmuxRunner = subprocess.run,
    runtime_notifier: Callable[[str], None] | None = None,
) -> int:
    """CAS changed live counts into exact per-session animation admission."""
    executor = SyncTmuxExecutor(
        tmux_binary,
        tmux_server_socket_path,
        runner=runner,
    )
    server_identity = executor.run(
        (
            "show-options",
            "-s",
            "-v",
            RODEX_SHARED_TMUX_PROTOCOL_OPTION,
            ";",
            "show-options",
            "-s",
            "-v",
            RODEX_SHARED_TMUX_SERVER_ID_OPTION,
        )
    )
    identity_fields = server_identity.stdout.splitlines()
    if server_identity.returncode != 0 or identity_fields not in (
        [RODEX_SHARED_TMUX_PROTOCOL, expected_server_id],
        [LEGACY_MUTABLE_TMUX_PROTOCOL, expected_server_id],
    ):
        return 1
    tmux_protocol = identity_fields[0]
    listed = executor.run(("list-sessions", "-F", _SESSION_RECORD_FORMAT))
    if listed.returncode != 0:
        return listed.returncode

    try:
        states = tuple(
            state
            for line in listed.stdout.splitlines()
            if (
                state := _parse_sharing_state(
                    tmux_server_socket_path,
                    expected_server_id,
                    line,
                )
            )
            is not None
        )
        _require_unique_registered_roster(states)
    except ValueError:
        return 1
    if runtime_notifier is None:

        def runtime_notifier(runtime_id: str) -> None:
            _notify_runtime_resize(
                tmux_server_socket_path.parent,
                python_executable,
                runtime_id,
                tmux_protocol,
            )

    for state in states:
        runtime_notifier(str(state.capability.runtime_id))
        reconciled = _reconcile_one(executor, python_executable, tmux_binary, tmux_protocol, state)
        if reconciled != 0:
            return reconciled
    return 0


def retain_legacy_coordinator(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    expected_server_id: str,
    *,
    python_executable: str = sys.executable,
    runner: SyncTmuxRunner = subprocess.run,
    installation_resolver: Callable[[], Path] = retained_installation_interpreter,
) -> int:
    """Move one redirected tmux-v4 hook onto this bridge's immutable installation."""
    executor = SyncTmuxExecutor(tmux_binary, tmux_server_socket_path, runner=runner)
    server_identity = executor.run(
        (
            "show-options",
            "-s",
            "-v",
            RODEX_SHARED_TMUX_PROTOCOL_OPTION,
            ";",
            "show-options",
            "-s",
            "-v",
            RODEX_SHARED_TMUX_SERVER_ID_OPTION,
        )
    )
    identity_fields = server_identity.stdout.splitlines()
    if server_identity.returncode != 0 or len(identity_fields) != 2:
        return 1
    if identity_fields != [LEGACY_MUTABLE_TMUX_PROTOCOL, expected_server_id]:
        return 0 if identity_fields == [RODEX_SHARED_TMUX_PROTOCOL, expected_server_id] else 1
    hook_names = tuple(f"{event}[{RODEX_SHARED_TMUX_HOOK_INDEX}]" for event in RODEX_LEGACY_COORDINATION_HOOKS)
    observed_owner = executor.run(
        (
            "show-options",
            "-s",
            "-v",
            RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION,
            *(argument for hook_name in hook_names for argument in (";", "show-hooks", "-g", hook_name)),
        )
    )
    owner_lines = observed_owner.stdout.splitlines()
    if observed_owner.returncode != 0 or len(owner_lines) != len(hook_names) + 1:
        return 1
    installed_command = owner_lines[0]
    for hook_name, line in zip(hook_names, owner_lines[1:], strict=True):
        observed_name, separator, observed_command = line.partition(" ")
        if (
            observed_name != hook_name
            or not separator
            or not _shell_commands_are_equivalent(observed_command, installed_command)
        ):
            return 1
    current_process_command = sharing_coordinator_hook_command(
        python_executable,
        tmux_binary,
        tmux_server_socket_path,
        expected_server_id,
    )
    current_process_unisolated_command = sharing_coordinator_hook_command(
        python_executable,
        tmux_binary,
        tmux_server_socket_path,
        expected_server_id,
        isolated=False,
    )
    if installed_command not in {current_process_command, current_process_unisolated_command}:
        return 1
    retained_python = installation_resolver()
    retained_command = sharing_coordinator_hook_command(
        str(retained_python),
        tmux_binary,
        tmux_server_socket_path,
        expected_server_id,
    )
    if installed_command == retained_command:
        return 0
    actions: tuple[tuple[str, ...], ...] = (
        *(
            (
                "set-option",
                "-g",
                hook_name,
                retained_command,
            )
            for hook_name in hook_names
        ),
        (
            "set-option",
            "-s",
            RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION,
            retained_command,
        ),
    )
    server_condition = server_identity_if_shell_condition(
        expected_server_id,
        tmux_protocol=LEGACY_MUTABLE_TMUX_PROTOCOL,
    )
    mutation_condition = combine_tmux_if_shell_conditions(
        server_condition,
        f"#{{==:#{{{RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION}}},#{{l:{tmux_format_literal(installed_command)}}}}}",
    )
    result = executor.run(
        (
            "if-shell",
            "-F",
            mutation_condition,
            _command_sequence(*actions),
            shlex.join(("run-shell", "false")),
        )
    )
    if result.returncode != 0:
        return result.returncode
    verification = executor.run(
        (
            "if-shell",
            "-F",
            combine_tmux_if_shell_conditions(
                server_condition,
                f"#{{==:#{{{RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION}}},"
                f"#{{l:{tmux_format_literal(retained_command)}}}}}",
            ),
            _command_sequence(
                (
                    "show-options",
                    "-s",
                    "-v",
                    RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION,
                ),
                *(("show-hooks", "-g", hook_name) for hook_name in hook_names),
            ),
            shlex.join(("run-shell", "false")),
        )
    )
    verified_lines = verification.stdout.splitlines()
    if (
        verification.returncode != 0
        or len(verified_lines) != len(hook_names) + 1
        or not _shell_commands_are_equivalent(verified_lines[0], retained_command)
    ):
        return 1
    for hook_name, line in zip(hook_names, verified_lines[1:], strict=True):
        observed_name, separator, observed_command = line.partition(" ")
        if (
            observed_name != hook_name
            or not separator
            or not _shell_commands_are_equivalent(observed_command, retained_command)
        ):
            return 1
    return 0


def _notify_runtime_resize(
    runtime_root: Path,
    python_executable: str,
    runtime_id: str,
    tmux_protocol: str,
) -> None:
    """Resize is a hint; exact terminal dimensions remain daemon-validated."""
    if tmux_protocol == LEGACY_MUTABLE_TMUX_PROTOCOL:
        with suppress(LegacyRuntimeCompatibilityError):
            notify_legacy_runtime_resize(runtime_root, runtime_id)
        return
    with suppress(OSError, TimeoutError, RodexDaemonError):
        RodexDaemonClient(runtime_root, python_executable).notify_runtime(
            runtime_id,
            RODEX_RUNTIME_WAKE_TERMINAL_RESIZE,
        )


def _parse_sharing_state(
    tmux_server_socket_path: Path,
    expected_server_id: str,
    line: str,
) -> _SharingState | None:
    fields = line.split("\t")
    if len(fields) != 10:
        raise ValueError("tmux returned a malformed sharing roster")
    (
        tmux_session_id,
        tmux_primary_pane_id,
        attached_count_text,
        previous_count_text,
        runtime_id_text,
        registration_state,
        rodex_session_id_text,
        registry_id_text,
        internal_session_id_text,
        codex_session_id_text,
    ) = fields
    if registration_state != RODEX_REGISTRATION_REGISTERED:
        return None
    try:
        attached_count = int(attached_count_text)
        previous_count = None if previous_count_text == "" else int(previous_count_text)
    except (TypeError, ValueError) as error:
        raise ValueError("registered tmux sharing state is malformed") from error
    try:
        capability = parse_tmux_session_capability(
            tmux_server_socket_path,
            expected_server_id,
            tmux_session_id,
            tmux_primary_pane_id,
            runtime_id_text,
            rodex_session_id_text,
            registry_id_text,
            internal_session_id_text,
            codex_session_id_text,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("registered tmux capability is malformed") from error
    if attached_count < 0 or (previous_count is not None and previous_count < 0):
        raise ValueError("registered tmux sharing counts are malformed")
    return _SharingState(capability, attached_count, previous_count)


def _require_unique_registered_roster(states: tuple[_SharingState, ...]) -> None:
    """Reject an ambiguous server roster before applying any mutation."""
    identity_projections = (
        (state.capability.tmux_session_id for state in states),
        (state.capability.tmux_primary_pane_id for state in states),
        (state.capability.runtime_id for state in states),
        ((state.capability.registry_id, state.capability.rodex_session_id) for state in states),
        ((state.capability.registry_id, state.capability.internal_session_id) for state in states),
        ((state.capability.registry_id, state.capability.codex_session_id) for state in states),
    )
    for projection in identity_projections:
        identities = tuple(projection)
        if len(identities) != len(set(identities)):
            raise ValueError("registered tmux sharing roster is ambiguous")


def _reconcile_one(
    executor: SyncTmuxExecutor,
    python_executable: str,
    tmux_binary: str,
    tmux_protocol: str,
    state: _SharingState,
) -> int:
    capability = state.capability
    current_count_condition = f"#{{==:#{{session_attached}},{state.attached_count}}}"
    if state.previous_attached_count is None:
        previous_count_condition = f"#{{==:#{{{RODEX_SHARING_ATTACHED_COUNT_OPTION}}},}}"
        action = _command_sequence(
            (
                "set-option",
                "-t",
                capability.pane_target,
                RODEX_SHARING_ATTACHED_COUNT_OPTION,
                str(state.attached_count),
            )
        )
    elif state.previous_attached_count == state.attached_count:
        return 0
    else:
        previous_count_condition = f"#{{==:#{{{RODEX_SHARING_ATTACHED_COUNT_OPTION}}},{state.previous_attached_count}}}"
        event = "attached" if state.attached_count > state.previous_attached_count else "detached"
        commands: tuple[tuple[str, ...] | str, ...] = (
            (
                "set-option",
                "-t",
                capability.pane_target,
                RODEX_SHARING_ATTACHED_COUNT_OPTION,
                str(state.attached_count),
            ),
        )
        if tmux_protocol == RODEX_SHARED_TMUX_PROTOCOL:
            commands += (
                status_animation_admission_command(
                    python_executable,
                    tmux_binary,
                    capability,
                    event,
                ),
            )
        action = _command_sequence(*commands)
    return executor.run(
        (
            "if-shell",
            "-t",
            capability.pane_target,
            "-F",
            combine_tmux_if_shell_conditions(
                registered_primary_pane_if_shell_condition(capability, tmux_protocol=tmux_protocol),
                current_count_condition,
                previous_count_condition,
            ),
            action,
        )
    ).returncode


def _command_sequence(*commands: tuple[str, ...] | str) -> str:
    return " ; ".join(command if isinstance(command, str) else shlex.join(command) for command in commands)


def _shell_commands_are_equivalent(left: str, right: str) -> bool:
    """Compare tmux-normalized shell commands without trusting textual quoting."""
    if not left or not right:
        return False
    try:
        return shlex.split(left) == shlex.split(right)
    except ValueError:
        return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.tmux_sharing_coordinator")
    parser.add_argument("--tmux-binary", required=True)
    parser.add_argument("--tmux-server-socket", required=True, type=Path)
    parser.add_argument("--expected-server-id", required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(arguments)
    reconciled = reconcile_sharing_state(
        args.tmux_binary,
        args.tmux_server_socket,
        args.expected_server_id,
    )
    if reconciled != 0:
        return reconciled
    try:
        return retain_legacy_coordinator(
            args.tmux_binary,
            args.tmux_server_socket,
            args.expected_server_id,
        )
    except (OSError, RodexInstallationError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
