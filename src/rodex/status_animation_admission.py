"""Canonical tmux lease and handoff state machine for status animations."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import shlex
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, Literal, Protocol

from .status_animation import FrameWaiter, StatusEvent, animate_status
from .tmux_executor import (
    DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    AsyncTmuxExecutor,
    AsyncTmuxRunner,
    TmuxCommandResult,
)

ANIMATION_OWNER_WATCHDOG_DELAY_SECONDS: Final = 15.0
STATUS_ANIMATION_PENDING_EVENT_OPTION: Final = "@rodex_status_animation_pending_event"
STATUS_ANIMATION_GENERATION_OPTION: Final = "@rodex_status_animation_generation"
STATUS_ANIMATION_OWNER_TOKEN_OPTION: Final = "@rodex_status_animation_owner_token"
STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION: Final = "@rodex_status_animation_watchdog_token"


class TmuxSessionAddress(Protocol):
    tmux_server_socket_path: Path
    tmux_session_name: str


def status_animation_hook_command(
    python_executable: str,
    tmux_binary: str,
    runtime: TmuxSessionAddress,
    event: StatusEvent,
) -> str:
    """Build one tmux-serialized admission transaction for a client transition."""
    if event not in {"attached", "detached"}:
        raise ValueError(f"unsupported status animation event: {event}")
    owner_token_format = f"#{{{STATUS_ANIMATION_GENERATION_OPTION}}}"
    stable_session_target = "#{session_id}"
    owner_command = _animation_process_command(
        python_executable,
        tmux_binary,
        runtime.tmux_server_socket_path,
        stable_session_target,
        event,
        owner_token_format,
        mode="admitted",
    )
    watchdog_gate = _delayed_watchdog_gate_command(
        python_executable,
        tmux_binary,
        runtime.tmux_server_socket_path,
        stable_session_target,
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
            "-F",
            STATUS_ANIMATION_OWNER_TOKEN_OPTION,
            owner_token_format,
        ),
        (
            "set-option",
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
    return _command_sequence(
        (
            "set-option",
            STATUS_ANIMATION_PENDING_EVENT_OPTION,
            event,
        ),
        (
            "set-option",
            "-F",
            STATUS_ANIMATION_GENERATION_OPTION,
            generation_increment,
        ),
        (
            "if-shell",
            "-F",
            owner_is_unleased,
            start_owner_commands,
        ),
    )


async def animate_admitted_status(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    tmux_session_target: str,
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
        tmux_server_socket_path,
        runner=runner,
        timeout_seconds=command_timeout_seconds,
    )
    session_target, pane_target = _tmux_session_targets(tmux_session_target)

    async def tmux(*arguments: str) -> TmuxCommandResult:
        return await executor.run(arguments)

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
            tmux_server_socket_path,
            session_target,
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
            _owner_unleased_condition(owner_token),
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
                await _read_tmux_option(
                    tmux, pane_target, STATUS_ANIMATION_OWNER_TOKEN_OPTION
                )
                != owner_token
            ):
                return
            pending = await _read_pending_transition(tmux, pane_target)
            if pending is None:
                await _release_animation_owner(tmux, pane_target, owner_token)
                return
            consumed_generation, event = pending
            await animate_status(
                tmux_binary,
                tmux_server_socket_path,
                session_target,
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
            await _release_animation_owner(tmux, pane_target, owner_token)


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
    await tmux("if-shell", "-t", pane_target, "-F", condition, commands)


async def _release_animation_owner(
    tmux: Callable[..., Awaitable[TmuxCommandResult]],
    pane_target: str,
    owner_token: str,
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
        _option_matches(STATUS_ANIMATION_OWNER_TOKEN_OPTION, owner_token),
        commands,
    )


def _animation_process_command(
    python_executable: str,
    tmux_binary: str,
    tmux_server_socket_path: Path,
    tmux_session_target: str,
    event: StatusEvent,
    owner_token: str,
    *,
    mode: Literal["admitted", "watchdog", "watchdog-gate"],
) -> str:
    arguments = [
        python_executable,
        "-m",
        "rodex.status_animation_admission",
        "--tmux-binary",
        tmux_binary,
        "--tmux-server-socket",
        str(tmux_server_socket_path),
        "--tmux-session-target",
        tmux_session_target,
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
    tmux_server_socket_path: Path,
    tmux_session_target: str,
    event: StatusEvent,
    owner_token: str,
) -> str:
    return _animation_process_command(
        python_executable,
        tmux_binary,
        tmux_server_socket_path,
        tmux_session_target,
        event,
        owner_token,
        mode="watchdog-gate",
    )


async def run_watchdog_gate(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    tmux_session_target: str,
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
        tmux_server_socket_path,
        runner=runner,
        timeout_seconds=command_timeout_seconds,
    )
    _session_target, pane_target = _tmux_session_targets(tmux_session_target)
    watchdog_command = _animation_process_command(
        python_executable,
        tmux_binary,
        tmux_server_socket_path,
        tmux_session_target,
        event,
        owner_token,
        mode="watchdog",
    )
    condition = (
        "#{&&:"
        f"{_option_matches(STATUS_ANIMATION_OWNER_TOKEN_OPTION, owner_token)},"
        f"{_option_matches(STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION, owner_token)}"
        "}"
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


def _tmux_session_targets(identity: str) -> tuple[str, str]:
    session_target = (
        identity if identity.startswith("$") or "#{" in identity else f"={identity}"
    )
    return session_target, f"{session_target}:"


def _command_sequence(*commands: tuple[str, ...]) -> str:
    return " ; ".join(shlex.join(command) for command in commands)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.status_animation_admission")
    parser.add_argument("--tmux-binary", required=True)
    parser.add_argument("--tmux-server-socket", required=True, type=Path)
    parser.add_argument("--tmux-session-target", required=True)
    parser.add_argument("--event", required=True, choices=("attached", "detached"))
    parser.add_argument("--owner-token", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--admitted", action="store_true")
    mode.add_argument("--watchdog", action="store_true")
    mode.add_argument("--watchdog-gate", action="store_true")
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = _build_parser().parse_args(arguments)
    if args.watchdog_gate:
        operation = run_watchdog_gate(
            args.tmux_binary,
            args.tmux_server_socket,
            args.tmux_session_target,
            args.event,
            args.owner_token,
        )
    else:
        operation = animate_admitted_status(
            args.tmux_binary,
            args.tmux_server_socket,
            args.tmux_session_target,
            args.event,
            args.owner_token,
            watchdog=args.watchdog,
        )
    asyncio.run(operation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
