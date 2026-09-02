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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

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
    tmux_format_literal,
)

RODEX_SHARING_ATTACHED_COUNT_OPTION: Final = "@rodex_sharing_attached_count"
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
) -> str:
    """Build a global hook that wakes discovery without conveying authority."""
    command = shlex.join(
        (
            tmux_format_literal(python_executable),
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
    return shlex.join(("run-shell", "-b", f"exec {command} >/dev/null 2>&1"))


def reconcile_sharing_state(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    expected_server_id: str,
    *,
    python_executable: str = sys.executable,
    runner: SyncTmuxRunner = subprocess.run,
) -> int:
    """CAS changed live counts into exact per-session animation admission."""
    executor = SyncTmuxExecutor(
        tmux_binary,
        tmux_server_socket_path,
        runner=runner,
    )
    protocol = executor.run(
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
    if (
        protocol.returncode != 0
        or protocol.stdout.strip() != f"{RODEX_SHARED_TMUX_PROTOCOL}\n{expected_server_id}"
    ):
        return 1
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
    for state in states:
        reconciled = _reconcile_one(executor, python_executable, tmux_binary, state)
        if reconciled != 0:
            return reconciled
    return 0


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
        (
            (state.capability.registry_id, state.capability.rodex_session_id)
            for state in states
        ),
        (
            (state.capability.registry_id, state.capability.internal_session_id)
            for state in states
        ),
        (
            (state.capability.registry_id, state.capability.codex_session_id)
            for state in states
        ),
    )
    for projection in identity_projections:
        identities = tuple(projection)
        if len(identities) != len(set(identities)):
            raise ValueError("registered tmux sharing roster is ambiguous")


def _reconcile_one(
    executor: SyncTmuxExecutor,
    python_executable: str,
    tmux_binary: str,
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
        previous_count_condition = (
            f"#{{==:#{{{RODEX_SHARING_ATTACHED_COUNT_OPTION}}},"
            f"{state.previous_attached_count}}}"
        )
        event = (
            "attached"
            if state.attached_count > state.previous_attached_count
            else "detached"
        )
        action = _command_sequence(
            (
                "set-option",
                "-t",
                capability.pane_target,
                RODEX_SHARING_ATTACHED_COUNT_OPTION,
                str(state.attached_count),
            ),
            status_animation_admission_command(
                python_executable,
                tmux_binary,
                capability,
                event,
            ),
        )
    return executor.run(
        (
            "if-shell",
            "-t",
            capability.pane_target,
            "-F",
            combine_tmux_if_shell_conditions(
                registered_primary_pane_if_shell_condition(capability),
                current_count_condition,
                previous_count_condition,
            ),
            action,
        )
    ).returncode


def _command_sequence(*commands: tuple[str, ...] | str) -> str:
    return " ; ".join(
        command if isinstance(command, str) else shlex.join(command) for command in commands
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.tmux_sharing_coordinator")
    parser.add_argument("--tmux-binary", required=True)
    parser.add_argument("--tmux-server-socket", required=True, type=Path)
    parser.add_argument("--expected-server-id", required=True)
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(arguments)
    return reconcile_sharing_state(
        args.tmux_binary,
        args.tmux_server_socket,
        args.expected_server_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
