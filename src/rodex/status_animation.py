"""Asynchronous, one-shot tmux status animation for sharing transitions."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .status_bar import RODEX_STATUS_COLOURS
from .tmux_status import (
    STATUS_ANIMATION_FRAME_INTERVAL_SECONDS,
    STATUS_CLAIM_TOKEN_OPTION,
    STATUS_PUBLISHER_SHARING_ANIMATION,
    StatusPriority,
    TmuxStatusClaimCommands,
    TmuxStatusPresentation,
)

StatusEvent = Literal["attached", "detached"]

FRAME_INTERVAL_SECONDS: Final = STATUS_ANIMATION_FRAME_INTERVAL_SECONDS
_CLIENT_NAME_FORMAT: Final = "#{client_name}"
_ATTACHED_COUNT_FORMAT: Final = "#{session_attached}"


@dataclass(frozen=True, slots=True)
class StatusFrame:
    background: str
    text: str


@dataclass(frozen=True, slots=True)
class AsyncCommandResult:
    returncode: int
    stdout: str = ""


AsyncCommandRunner = Callable[[Sequence[str]], Awaitable[AsyncCommandResult]]
FrameWaiter = Callable[[float], Awaitable[None]]

_ARRIVAL_TEXT: Final = (
    "·                 ✦                 ·",
    "·      ✦          ◇          ✦      ·",
    "░                ◇                ░",
    "░▒░           ◇     ◇           ░▒░",
    "░▒▓▒░      ◇    ◈    ◇      ░▒▓▒░",
    "▓████▓▒░   REALITY FRACTURE   ░▒▓████▓",
    "◢██████  REALITY LINK OPENING  ██████◣",
    "<<< ⚡ INCOMING SIGNAL ⚡ >>>",
    "<<< ⚡ INCOMING COLLABORATOR ⚡ >>>",
    "◈  IDENTITY HANDSHAKE  ◈",
    "◈  SYNCHRONIZING SHARED REALITY  ◈",
    "⟪⟪⟪  QUANTUM CHANNEL LOCKED  ⟫⟫⟫",
    "▓▒░  TWO MINDS · ONE TERMINAL  ░▒▓",
    "◇◇◇       {shared}       ◇◇◇",
    "◈◈◈       {shared}       ◈◈◈",
    "████       {shared}       ████",
    "◉          {shared}          ◉",
    "◉ ◉        {shared}        ◉ ◉",
    "◉ ◉ ◉      {shared}      ◉ ◉ ◉",
    "✦ ◈ ✦      {shared}      ✦ ◈ ✦",
    "░▒▓█  COLLABORATIVE REALITY STABLE  █▓▒░",
    "◈      COLLABORATIVE REALITY STABLE      ◈",
    "✦         SHARED REALITY STABLE         ✦",
    "·    ✦       SHARED REALITY       ✦    ·",
    "[ {shared_title} ]",
)
_ARRIVAL_BACKGROUNDS: Final = RODEX_STATUS_COLOURS.animation_arrival_backgrounds

_DEPARTURE_TEXT: Final = (
    "◉          SHARED CHANNEL ACTIVE          ◉",
    "◉ ◉      COLLABORATOR SIGNAL FADING      ◉ ◉",
    "<<< COLLABORATOR DEPARTING >>>",
    "▓████▓▒░  SEVERING QUANTUM LINK  ░▒▓████▓",
    "▓▒░  COLLAPSING SHARED CHANNEL  ░▒▓",
    "⟫⟫⟫  REALITY LINK CLOSING  ⟪⟪⟪",
    "◈  DISENTANGLING TERMINALS  ◈",
    "◢█████  CLOSING AIRLOCK  █████◣",
    "░▒▓█▓▒░  CHANNEL CONTRACTING  ░▒▓█▓▒░",
    "░▒▓▒░      ◇    ◈    ◇      ░▒▓▒░",
    "░▒░           ◇     ◇           ░▒░",
    "░                ◇                ░",
    "·      ✦          ◇          ✦      ·",
    "·                 ✦                 ·",
    "                    ·",
    "             LOCKING CHANNEL",
    "          ░▒▓  LOCKING  ▓▒░",
    "       ▓████  REALITY SEALED  ████▓",
    "          🔒  REALITY SEALED  🔒",
    "       ◈  PRIVATE CHANNEL SECURED  ◈",
    "✦          PRIVATE SESSION          ✦",
    "·    ✦     PRIVATE SESSION     ✦    ·",
    "·          PRIVATE SESSION          ·",
    "           PRIVATE SESSION",
    "[ Private session ]",
)
_DEPARTURE_BACKGROUNDS: Final = RODEX_STATUS_COLOURS.animation_departure_backgrounds


def status_frames(event: StatusEvent, attached_count: int) -> tuple[StatusFrame, ...]:
    """Return the qualifying five-second transition, or no frames."""
    if event == "attached" and attached_count >= 2:
        other_count = attached_count - 1
        suffix = "other" if other_count == 1 else "others"
        shared_title = f"Shared with {other_count} {suffix}"
        shared = shared_title.upper()
        return tuple(
            StatusFrame(background, text.format(shared=shared, shared_title=shared_title))
            for background, text in zip(_ARRIVAL_BACKGROUNDS, _ARRIVAL_TEXT, strict=True)
        )
    if event == "detached" and attached_count == 1:
        return tuple(
            StatusFrame(background, text)
            for background, text in zip(
                _DEPARTURE_BACKGROUNDS, _DEPARTURE_TEXT, strict=True
            )
        )
    return ()


async def animate_status(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    tmux_session_name: str,
    event: StatusEvent,
    *,
    runner: AsyncCommandRunner | None = None,
    wait_until: FrameWaiter | None = None,
    token_factory: Callable[[], str] = lambda: secrets.token_hex(16),
) -> None:
    """Animate a qualifying attachment transition without blocking its caller."""
    command_runner = runner or _run_command
    frame_waiter = wait_until or _wait_until
    tmux_prefix = (tmux_binary, "-S", str(tmux_server_socket_path))
    pane_target = f"={tmux_session_name}:"
    session_target = f"={tmux_session_name}"
    status_commands = TmuxStatusClaimCommands(pane_target)

    async def tmux(*arguments: str) -> AsyncCommandResult:
        return await command_runner((*tmux_prefix, *arguments))

    count_result = await tmux(
        "display-message",
        "-p",
        "-t",
        pane_target,
        "-F",
        _ATTACHED_COUNT_FORMAT,
    )
    try:
        attached_count = int(count_result.stdout.strip())
    except (TypeError, ValueError):
        return
    if count_result.returncode != 0 or attached_count < 0:
        return

    frames = status_frames(event, attached_count)
    if not frames:
        await tmux(
            "if-shell",
            "-t",
            pane_target,
            "-F",
            status_commands.publisher_matches(STATUS_PUBLISHER_SHARING_ANIMATION),
            status_commands.restore_base(),
        )
        return

    token = token_factory()
    claim_result = await tmux(
        "if-shell",
        "-t",
        pane_target,
        "-F",
        status_commands.priority_allows(StatusPriority.SHARING_ANIMATION),
        status_commands.claim_and_present(
            publisher=STATUS_PUBLISHER_SHARING_ANIMATION,
            token=token,
            priority=StatusPriority.SHARING_ANIMATION,
            presentation=_frame_presentation(frames[0]),
        ),
    )
    if claim_result.returncode != 0 or not await _animation_token_matches(
        tmux, pane_target, token
    ):
        return

    loop = asyncio.get_running_loop()
    next_frame_at = loop.time() + FRAME_INTERVAL_SECONDS
    await frame_waiter(next_frame_at)
    for frame in frames[1:]:
        if not await _animation_token_matches(tmux, pane_target, token):
            return
        apply_result = await tmux(
            "if-shell",
            "-t",
            pane_target,
            "-F",
            status_commands.token_matches(token),
            status_commands.present(_frame_presentation(frame)),
        )
        if apply_result.returncode != 0:
            break
        next_frame_at += FRAME_INTERVAL_SECONDS
        await frame_waiter(next_frame_at)

    await _restore_normal_status(
        tmux,
        pane_target,
        session_target,
        token,
        status_commands,
    )


async def _run_command(command: Sequence[str]) -> AsyncCommandResult:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return AsyncCommandResult(127)
    stdout, _ = await process.communicate()
    return AsyncCommandResult(
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
    )


async def _wait_until(deadline: float) -> None:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    handle = loop.call_at(deadline, ready.set_result, None)
    try:
        await ready
    finally:
        handle.cancel()


async def _animation_token_matches(
    tmux: Callable[..., Awaitable[AsyncCommandResult]],
    pane_target: str,
    token: str,
) -> bool:
    result = await tmux("show-options", "-v", "-t", pane_target, STATUS_CLAIM_TOKEN_OPTION)
    return result.returncode == 0 and result.stdout.strip() == token


async def _restore_normal_status(
    tmux: Callable[..., Awaitable[AsyncCommandResult]],
    pane_target: str,
    session_target: str,
    token: str,
    status_commands: TmuxStatusClaimCommands,
) -> None:
    await tmux(
        "if-shell",
        "-t",
        pane_target,
        "-F",
        status_commands.token_matches(token),
        status_commands.restore_base(),
    )
    clients = await tmux("list-clients", "-t", session_target, "-F", _CLIENT_NAME_FORMAT)
    if clients.returncode != 0:
        return
    for client_name in clients.stdout.splitlines():
        if client_name:
            await tmux("refresh-client", "-S", "-t", client_name)


def _frame_presentation(frame: StatusFrame) -> TmuxStatusPresentation:
    return TmuxStatusPresentation(
        status_style=(
            f"bg={frame.background},fg={RODEX_STATUS_COLOURS.animation_foreground},bold"
        ),
        status_format=f"#[align=centre]{frame.text}",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.status_animation")
    parser.add_argument("--tmux-binary", required=True)
    parser.add_argument("--tmux-server-socket", required=True, type=Path)
    parser.add_argument("--tmux-session-name", required=True)
    parser.add_argument("--event", required=True, choices=("attached", "detached"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    asyncio.run(
        animate_status(
            args.tmux_binary,
            args.tmux_server_socket,
            args.tmux_session_name,
            args.event,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
