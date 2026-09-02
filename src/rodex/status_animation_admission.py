"""Canonical tmux lease and handoff state machine for status animations."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import shlex
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, Literal

from .status_animation import FrameWaiter, StatusEvent, animate_status
from .tmux_executor import (
    DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    AsyncTmuxExecutor,
    AsyncTmuxRunner,
    TmuxCommandResult,
)
from .tmux_session_capability import (
    TmuxSessionCapability,
    combine_tmux_if_shell_conditions,
    parse_tmux_session_capability,
    registered_primary_pane_if_shell_condition,
    registered_primary_pane_read_arguments,
    tmux_format_literal,
)

ANIMATION_OWNER_WATCHDOG_DELAY_SECONDS: Final = 15.0
STATUS_ANIMATION_PENDING_EVENT_OPTION: Final = "@rodex_status_animation_pending_event"
STATUS_ANIMATION_GENERATION_OPTION: Final = "@rodex_status_animation_generation"
STATUS_ANIMATION_OWNER_TOKEN_OPTION: Final = "@rodex_status_animation_owner_token"
STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION: Final = "@rodex_status_animation_watchdog_token"


def status_animation_admission_command(
    python_executable: str,
    tmux_binary: str,
    capability: TmuxSessionCapability,
    event: StatusEvent,
) -> str:
    """Build one exact, runtime-fenced admission transaction."""
    if event not in {"attached", "detached"}:
        raise ValueError(f"unsupported status animation event: {event}")
    owner_token_format = f"#{{{STATUS_ANIMATION_GENERATION_OPTION}}}"
    owner_command = _animation_process_command(
        python_executable,
        tmux_binary,
        capability,
        event,
        owner_token_format,
        mode="admitted",
    )
    watchdog_gate = _delayed_watchdog_gate_command(
        python_executable,
        tmux_binary,
        capability,
        event,
        owner_token_format,
    )
    generation_increment = (
        "#{e|+:"
        f"#{{?#{{{STATUS_ANIMATION_GENERATION_OPTION}}},"
        f"#{{{STATUS_ANIMATION_GENERATION_OPTION}}},0}},1}}"
    )
    owner_is_empty = f"#{{==:#{{{STATUS_ANIMATION_OWNER_TOKEN_OPTION}}},}}"
    watchdog_is_empty = f"#{{==:#{{{STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION}}},}}"
    owner_is_unleased = f"#{{||:{owner_is_empty},{watchdog_is_empty}}}"
    start_owner_commands = _command_sequence(
        (
            "set-option",
            "-t",
            capability.pane_target,
            "-F",
            STATUS_ANIMATION_OWNER_TOKEN_OPTION,
            owner_token_format,
        ),
        (
            "set-option",
            "-t",
            capability.pane_target,
            "-F",
            STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION,
            owner_token_format,
        ),
        ("run-shell", "-b", f"exec {owner_command} >/dev/null 2>&1"),
        (
            "run-shell",
            "-b",
            "-d",
            str(ANIMATION_OWNER_WATCHDOG_DELAY_SECONDS),
            watchdog_gate,
        ),
    )
    admission_commands = _command_sequence(
        (
            "set-option",
            "-t",
            capability.pane_target,
            STATUS_ANIMATION_PENDING_EVENT_OPTION,
            event,
        ),
        (
            "set-option",
            "-t",
            capability.pane_target,
            "-F",
            STATUS_ANIMATION_GENERATION_OPTION,
            generation_increment,
        ),
        (
            "if-shell",
            "-t",
            capability.pane_target,
            "-F",
            combine_tmux_if_shell_conditions(
                registered_primary_pane_if_shell_condition(capability),
                owner_is_unleased,
            ),
            start_owner_commands,
        ),
    )
    return _command_sequence(
        (
            "if-shell",
            "-t",
            capability.pane_target,
            "-F",
            registered_primary_pane_if_shell_condition(capability),
            admission_commands,
        )
    )


async def animate_admitted_status(
    tmux_binary: str,
    capability: TmuxSessionCapability,
    fallback_event: StatusEvent,
    owner_token: str,
    *,
    watchdog: bool = False,
    runner: AsyncTmuxRunner | None = None,
    wait_until: FrameWaiter | None = None,
    token_factory: Callable[[], str] = lambda: secrets.token_hex(16),
    command_timeout_seconds: float = DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    python_executable: str = sys.executable,
) -> None:
    """Drain the newest transition while holding one self-healing tmux lease."""
    if not owner_token:
        return
    executor = AsyncTmuxExecutor(
        tmux_binary,
        capability.tmux_server_socket_path,
        runner=runner,
        timeout_seconds=command_timeout_seconds,
    )
    pane_target = capability.pane_target

    async def tmux(*arguments: str) -> TmuxCommandResult:
        return await executor.run(arguments)

    if not await _registered_capability_is_current(tmux, capability):
        return
    if (
        await _read_tmux_option(tmux, pane_target, STATUS_ANIMATION_OWNER_TOKEN_OPTION)
        != owner_token
    ):
        return
    if watchdog:
        if (
            await _read_tmux_option(
                tmux, pane_target, STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION
            )
            is not None
        ):
            return
        recovered_owner = f"recovery-{token_factory()}"
        successor_gate = _delayed_watchdog_gate_command(
            python_executable,
            tmux_binary,
            capability,
            fallback_event,
            recovered_owner,
        )
        recovery_commands = _command_sequence(
            (
                "set-option",
                "-t",
                pane_target,
                STATUS_ANIMATION_OWNER_TOKEN_OPTION,
                recovered_owner,
            ),
            (
                "set-option",
                "-t",
                pane_target,
                STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION,
                recovered_owner,
            ),
            (
                "run-shell",
                "-b",
                "-d",
                str(ANIMATION_OWNER_WATCHDOG_DELAY_SECONDS),
                successor_gate,
            ),
        )
        await tmux(
            "if-shell",
            "-t",
            pane_target,
            "-F",
            combine_tmux_if_shell_conditions(
                registered_primary_pane_if_shell_condition(capability),
                _owner_unleased_condition(owner_token),
            ),
            recovery_commands,
        )
        if (
            await _read_tmux_option(tmux, pane_target, STATUS_ANIMATION_OWNER_TOKEN_OPTION)
            != recovered_owner
        ):
            return
        owner_token = recovered_owner

    consumed_generation: str | None = None
    try:
        while True:
            if (
                not await _registered_capability_is_current(tmux, capability)
                or await _read_tmux_option(
                    tmux, pane_target, STATUS_ANIMATION_OWNER_TOKEN_OPTION
                )
                != owner_token
            ):
                return
            pending = await _read_pending_transition(tmux, pane_target)
            if pending is None:
                await _release_animation_owner(
                    tmux,
                    pane_target,
                    owner_token,
                    capability,
                )
                return
            consumed_generation, event = pending
            await animate_status(
                tmux_binary,
                capability,
                event or fallback_event,
                runner=runner,
                wait_until=wait_until,
                token_factory=token_factory,
                command_timeout_seconds=command_timeout_seconds,
            )
            await _release_consumed_transition(
                tmux,
                pane_target,
                owner_token,
                consumed_generation,
                capability,
            )
            current_owner = await _read_tmux_option(
                tmux, pane_target, STATUS_ANIMATION_OWNER_TOKEN_OPTION
            )
            if current_owner != owner_token:
                return
            latest_generation = await _read_tmux_option(
                tmux,
                pane_target,
                STATUS_ANIMATION_GENERATION_OPTION,
            )
            if latest_generation == consumed_generation:
                # The still-scheduled lease recovery owns any failed release retry.
                return
    finally:
        if consumed_generation is None:
            await _release_animation_owner(
                tmux,
                pane_target,
                owner_token,
                capability,
            )


async def _read_pending_transition(
    tmux: Callable[..., Awaitable[TmuxCommandResult]],
    pane_target: str,
) -> tuple[str, StatusEvent] | None:
    generation = await _read_tmux_option(
        tmux, pane_target, STATUS_ANIMATION_GENERATION_OPTION
    )
    event = await _read_tmux_option(
        tmux, pane_target, STATUS_ANIMATION_PENDING_EVENT_OPTION
    )
    if generation is None or event not in {"attached", "detached"}:
        return None
    return generation, event


async def _registered_capability_is_current(
    tmux: Callable[..., Awaitable[TmuxCommandResult]],
    capability: TmuxSessionCapability,
) -> bool:
    result = await tmux(
        *registered_primary_pane_read_arguments(capability, "#{pane_id}")
    )
    return result.returncode == 0 and result.stdout.strip() == capability.pane_target


async def _read_tmux_option(
    tmux: Callable[..., Awaitable[TmuxCommandResult]],
    pane_target: str,
    option: str,
) -> str | None:
    result = await tmux("show-options", "-v", "-t", pane_target, option)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


async def _release_consumed_transition(
    tmux: Callable[..., Awaitable[TmuxCommandResult]],
    pane_target: str,
    owner_token: str,
    generation: str,
    capability: TmuxSessionCapability,
) -> None:
    condition = (
        "#{&&:"
        f"{_option_matches(STATUS_ANIMATION_OWNER_TOKEN_OPTION, owner_token)},"
        f"{_option_matches(STATUS_ANIMATION_GENERATION_OPTION, generation)}"
        "}"
    )
    commands = _command_sequence(
        *(
            ("set-option", "-u", "-t", pane_target, option)
            for option in (
                STATUS_ANIMATION_PENDING_EVENT_OPTION,
                STATUS_ANIMATION_OWNER_TOKEN_OPTION,
                STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION,
            )
        )
    )
    await tmux(
        "if-shell",
        "-t",
        pane_target,
        "-F",
        combine_tmux_if_shell_conditions(
            registered_primary_pane_if_shell_condition(capability), condition
        ),
        commands,
    )


async def _release_animation_owner(
    tmux: Callable[..., Awaitable[TmuxCommandResult]],
    pane_target: str,
    owner_token: str,
    capability: TmuxSessionCapability,
) -> None:
    commands = _command_sequence(
        *(
            ("set-option", "-u", "-t", pane_target, option)
            for option in (
                STATUS_ANIMATION_OWNER_TOKEN_OPTION,
                STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION,
            )
        )
    )
    await tmux(
        "if-shell",
        "-t",
        pane_target,
        "-F",
        combine_tmux_if_shell_conditions(
            registered_primary_pane_if_shell_condition(capability),
            _option_matches(STATUS_ANIMATION_OWNER_TOKEN_OPTION, owner_token),
        ),
        commands,
    )


def _animation_process_command(
    python_executable: str,
    tmux_binary: str,
    capability: TmuxSessionCapability,
    event: StatusEvent,
    owner_token: str,
    *,
    mode: Literal["admitted", "watchdog", "watchdog-gate"],
) -> str:
    arguments = [
        tmux_format_literal(python_executable),
        "-m",
        "rodex.status_animation_admission",
        "--tmux-binary",
        tmux_format_literal(tmux_binary),
        "--tmux-server-socket",
        tmux_format_literal(str(capability.tmux_server_socket_path)),
        "--expected-server-id",
        capability.tmux_server_id,
        "--tmux-session-id",
        capability.tmux_session_id,
        "--tmux-primary-pane-id",
        capability.tmux_primary_pane_id,
        "--expected-runtime-id",
        str(capability.runtime_id),
        "--expected-rodex-session-id",
        str(capability.rodex_session_id),
        "--expected-registry-id",
        str(capability.registry_id),
        "--expected-internal-session-id",
        str(capability.internal_session_id),
        "--expected-codex-session-id",
        str(capability.codex_session_id),
        "--event",
        event,
        "--owner-token",
        owner_token,
    ]
    arguments.append(f"--{mode}")
    return shlex.join(arguments)


def _delayed_watchdog_gate_command(
    python_executable: str,
    tmux_binary: str,
    capability: TmuxSessionCapability,
    event: StatusEvent,
    owner_token: str,
) -> str:
    return _animation_process_command(
        python_executable,
        tmux_binary,
        capability,
        event,
        owner_token,
        mode="watchdog-gate",
    )


async def run_watchdog_gate(
    tmux_binary: str,
    capability: TmuxSessionCapability,
    event: StatusEvent,
    owner_token: str,
    *,
    runner: AsyncTmuxRunner | None = None,
    command_timeout_seconds: float = DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    python_executable: str = sys.executable,
) -> None:
    """Clear one stale marker and launch recovery through the canonical executor."""
    executor = AsyncTmuxExecutor(
        tmux_binary,
        capability.tmux_server_socket_path,
        runner=runner,
        timeout_seconds=command_timeout_seconds,
    )
    pane_target = capability.pane_target
    watchdog_command = _animation_process_command(
        python_executable,
        tmux_binary,
        capability,
        event,
        owner_token,
        mode="watchdog",
    )
    lease_condition = (
        "#{&&:"
        f"{_option_matches(STATUS_ANIMATION_OWNER_TOKEN_OPTION, owner_token)},"
        f"{_option_matches(STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION, owner_token)}"
        "}"
    )
    condition = combine_tmux_if_shell_conditions(
        registered_primary_pane_if_shell_condition(capability),
        lease_condition,
    )
    action = _command_sequence(
        (
            "set-option",
            "-u",
            "-t",
            pane_target,
            STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION,
        ),
        ("run-shell", "-b", f"exec {watchdog_command} >/dev/null 2>&1"),
    )
    await executor.run(
        (
            "if-shell",
            "-t",
            pane_target,
            "-F",
            condition,
            action,
        )
    )


def _owner_unleased_condition(owner_token: str) -> str:
    return (
        "#{&&:"
        f"{_option_matches(STATUS_ANIMATION_OWNER_TOKEN_OPTION, owner_token)},"
        f"#{{==:#{{{STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION}}},}}"
        "}"
    )


def _option_matches(option: str, expected: str) -> str:
    return f"#{{==:#{{{option}}},{expected}}}"


def _command_sequence(*commands: tuple[str, ...]) -> str:
    return " ; ".join(shlex.join(command) for command in commands)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.status_animation_admission")
    parser.add_argument("--tmux-binary", required=True)
    parser.add_argument("--tmux-server-socket", required=True, type=Path)
    parser.add_argument("--expected-server-id", required=True)
    parser.add_argument("--tmux-session-id", required=True)
    parser.add_argument("--tmux-primary-pane-id", required=True)
    parser.add_argument("--expected-runtime-id", required=True)
    parser.add_argument("--expected-rodex-session-id", required=True)
    parser.add_argument("--expected-registry-id", required=True)
    parser.add_argument("--expected-internal-session-id", required=True)
    parser.add_argument("--expected-codex-session-id", required=True)
    parser.add_argument("--event", required=True, choices=("attached", "detached"))
    parser.add_argument("--owner-token", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--admitted", action="store_true")
    mode.add_argument("--watchdog", action="store_true")
    mode.add_argument("--watchdog-gate", action="store_true")
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = _build_parser().parse_args(arguments)
    capability = parse_tmux_session_capability(
        args.tmux_server_socket,
        args.expected_server_id,
        args.tmux_session_id,
        args.tmux_primary_pane_id,
        args.expected_runtime_id,
        args.expected_rodex_session_id,
        args.expected_registry_id,
        args.expected_internal_session_id,
        args.expected_codex_session_id,
    )
    if args.watchdog_gate:
        operation = run_watchdog_gate(
            args.tmux_binary,
            capability,
            args.event,
            args.owner_token,
        )
    else:
        operation = animate_admitted_status(
            args.tmux_binary,
            capability,
            args.event,
            args.owner_token,
            watchdog=args.watchdog,
        )
    asyncio.run(operation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
