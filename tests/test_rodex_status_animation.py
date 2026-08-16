from __future__ import annotations

import asyncio
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path

import pytest

from rodex.status_animation import (
    FRAME_INTERVAL_SECONDS,
    STATUS_ANIMATION_TOKEN_OPTION,
    AsyncCommandResult,
    animate_status,
    status_frames,
)


class FakeTmux:
    def __init__(self, attached_count: int) -> None:
        self.attached_count = attached_count
        self.animation_token = ""
        self.commands: list[list[str]] = []

    async def __call__(self, command: Sequence[str]) -> AsyncCommandResult:
        recorded = list(command)
        self.commands.append(recorded)
        if "display-message" in recorded:
            return AsyncCommandResult(0, f"{self.attached_count}\n")
        if recorded[-2:-1] == [STATUS_ANIMATION_TOKEN_OPTION]:
            self.animation_token = recorded[-1]
        if "show-options" in recorded:
            return AsyncCommandResult(0, f"{self.animation_token}\n")
        if "list-clients" in recorded:
            return AsyncCommandResult(0, "/dev/pts/10\n/dev/pts/11\n")
        if "if-shell" in recorded:
            tmux_commands = recorded[-1]
            if "status-format[0]" not in tmux_commands:
                self.animation_token = ""
        return AsyncCommandResult(0)


def test_status_frames_only_cover_shared_arrival_and_final_private_departure() -> None:
    assert status_frames("attached", 0) == ()
    assert status_frames("attached", 1) == ()
    assert status_frames("detached", 0) == ()
    assert status_frames("detached", 2) == ()

    one_other = status_frames("attached", 2)
    two_others = status_frames("attached", 3)
    private = status_frames("detached", 1)

    assert len(one_other) * FRAME_INTERVAL_SECONDS == pytest.approx(5)
    assert len(two_others) == len(one_other)
    assert len(private) * FRAME_INTERVAL_SECONDS == pytest.approx(5)
    assert "Shared with 1 other" in one_other[-1].text
    assert "Shared with 2 others" in two_others[-1].text
    assert private[-1].text == "[ Private session ]"


def test_animation_uses_scheduled_frames_and_restores_the_entire_status_format() -> None:
    tmux = FakeTmux(attached_count=2)
    deadlines: list[float] = []

    async def record_deadline(deadline: float) -> None:
        deadlines.append(deadline)

    asyncio.run(
        animate_status(
            "/usr/bin/tmux",
            Path("/run/user/1009/rodex/tmux.sock"),
            "automatic-beluga",
            "attached",
            runner=tmux,
            wait_until=record_deadline,
            token_factory=lambda: "animation-token",
        )
    )

    frame_commands = [
        command
        for command in tmux.commands
        if "if-shell" in command and "status-format[0]" in command[-1]
    ]
    restore_commands = [
        command
        for command in tmux.commands
        if "if-shell" in command
        and "status-format" in command[-1]
        and "status-format[0]" not in command[-1]
    ]
    refresh_commands = [command for command in tmux.commands if "refresh-client" in command]

    assert len(frame_commands) == 25
    assert len(deadlines) == 25
    assert all(
        later - earlier == pytest.approx(FRAME_INTERVAL_SECONDS)
        for earlier, later in pairwise(deadlines)
    )
    assert len(restore_commands) == 1
    assert "set-option -u -t =automatic-beluga: status-format" in restore_commands[0][-1]
    assert "status-format[0]" not in restore_commands[0][-1]
    assert [command[-1] for command in refresh_commands] == [
        "/dev/pts/10",
        "/dev/pts/11",
    ]


def test_new_animation_token_stops_an_older_animation_without_restoring_over_it() -> None:
    tmux = FakeTmux(attached_count=2)
    waits = 0

    async def supersede_after_first_frame(_deadline: float) -> None:
        nonlocal waits
        waits += 1
        tmux.animation_token = "newer-animation"

    asyncio.run(
        animate_status(
            "tmux",
            Path("/tmp/rodex/tmux.sock"),
            "automatic-beluga",
            "attached",
            runner=tmux,
            wait_until=supersede_after_first_frame,
            token_factory=lambda: "older-animation",
        )
    )

    frame_commands = [
        command
        for command in tmux.commands
        if "if-shell" in command and "status-format[0]" in command[-1]
    ]
    restore_commands = [
        command
        for command in tmux.commands
        if "if-shell" in command
        and "status-format" in command[-1]
        and "status-format[0]" not in command[-1]
    ]
    assert waits == 1
    assert len(frame_commands) == 1
    assert restore_commands == []
    assert tmux.animation_token == "newer-animation"


def test_nonqualifying_attachment_cancels_animation_and_restores_normal_status() -> None:
    tmux = FakeTmux(attached_count=1)

    async def unexpected_wait(_deadline: float) -> None:
        pytest.fail("a nonqualifying attachment must not schedule animation frames")

    asyncio.run(
        animate_status(
            "tmux",
            Path("/tmp/rodex/tmux.sock"),
            "automatic-beluga",
            "attached",
            runner=tmux,
            wait_until=unexpected_wait,
            token_factory=lambda: "cancelling-animation",
        )
    )

    assert not any("status-format[0]" in command[-1] for command in tmux.commands)
    assert any(
        "status-format" in command[-1] and "status-format[0]" not in command[-1]
        for command in tmux.commands
    )
    assert tmux.animation_token == ""
