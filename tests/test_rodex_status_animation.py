from __future__ import annotations

import asyncio
import re
import shlex
import uuid
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path

import pytest

from rodex.status_animation import (
    FRAME_INTERVAL_SECONDS,
    AsyncCommandResult,
    animate_status,
    status_frames,
)
from rodex.tmux_session_capability import TmuxSessionCapability
from rodex.tmux_status import (
    RODEX_STATUS_STYLE,
    STATUS_CLAIM_PRIORITY_OPTION,
    STATUS_CLAIM_PUBLISHER_OPTION,
    STATUS_CLAIM_TOKEN_OPTION,
)
from rodex_registry.identity import RodexRegistryId, RodexRuntimeId, RodexSessionId


def _capability(socket_path: Path) -> TmuxSessionCapability:
    return TmuxSessionCapability(
        socket_path,
        "0123456789abcdef0123456789abcdef",
        "$7",
        "%9",
        RodexRuntimeId.parse("0123456789abcdef"),
        RodexSessionId.parse("1111111111111111"),
        RodexRegistryId.parse("2222222222222222"),
        7,
        uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
    )


class FakeTmux:
    def __init__(self, attached_count: int) -> None:
        self.attached_count = attached_count
        self.options: dict[str, str] = {}
        self.commands: list[list[str]] = []

    @property
    def animation_token(self) -> str:
        return self.options.get(STATUS_CLAIM_TOKEN_OPTION, "")

    @animation_token.setter
    def animation_token(self, token: str) -> None:
        if token:
            self.options[STATUS_CLAIM_TOKEN_OPTION] = token
        else:
            self.options.pop(STATUS_CLAIM_TOKEN_OPTION, None)

    async def __call__(self, command: Sequence[str]) -> AsyncCommandResult:
        recorded = list(command)
        self.commands.append(recorded)
        if "display-message" in recorded:
            format_string = recorded[-1]
            if "#{session_attached}" in format_string:
                return AsyncCommandResult(0, f"1\t{self.attached_count}\n")
            if STATUS_CLAIM_TOKEN_OPTION in format_string:
                token = self.options.get(STATUS_CLAIM_TOKEN_OPTION, "")
                return AsyncCommandResult(0, f"1\t{token}\n")
            return AsyncCommandResult(0, "1\n")
        if "show-options" in recorded:
            value = self.options.get(recorded[-1], "")
            return AsyncCommandResult(0 if value else 1, f"{value}\n")
        if "list-clients" in recorded:
            return AsyncCommandResult(0, "/dev/pts/10\n/dev/pts/11\n")
        if "if-shell" in recorded and self._condition_is_true(recorded[-2]):
            for tmux_command in recorded[-1].split(" ; "):
                self._apply(shlex.split(tmux_command))
        return AsyncCommandResult(0)

    def _condition_is_true(self, condition: str) -> bool:
        if "#{<=:" in condition:
            current = int(self.options.get(STATUS_CLAIM_PRIORITY_OPTION, "0"))
            match = re.search(r"#\{<=:#\{@rodex_status_claim_priority\},(\d+)\}", condition)
            assert match is not None
            requested = int(match.group(1))
            return current <= requested
        if STATUS_CLAIM_TOKEN_OPTION in condition:
            match = re.search(
                r"#\{==:#\{@rodex_status_claim_token\},([^}]*)\}",
                condition,
            )
            assert match is not None
            expected = match.group(1)
            return self.options.get(STATUS_CLAIM_TOKEN_OPTION) == expected
        if STATUS_CLAIM_PUBLISHER_OPTION in condition:
            match = re.search(
                r"#\{==:#\{@rodex_status_claim_publisher\},([^}]*)\}",
                condition,
            )
            assert match is not None
            expected = match.group(1)
            return self.options.get(STATUS_CLAIM_PUBLISHER_OPTION) == expected
        if "@rodex_registration_state" in condition:
            return True
        raise AssertionError(f"unexpected tmux condition: {condition}")

    def _apply(self, command: list[str]) -> None:
        if command[:2] == ["set-option", "-u"]:
            self.options.pop(command[-1], None)
        elif command[:1] == ["set-option"]:
            self.options[command[-2]] = command[-1]


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
            _capability(Path("/run/user/1009/rodex/tmux.sock")),
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
    refresh_commands = [
        command
        for command in tmux.commands
        if any("refresh-client" in argument for argument in command)
    ]

    assert len(frame_commands) == 25
    assert all(command[3:6] == ["if-shell", "-t", "%9"] for command in frame_commands)
    assert len(deadlines) == 25
    assert all(
        later - earlier == pytest.approx(FRAME_INTERVAL_SECONDS)
        for earlier, later in pairwise(deadlines)
    )
    assert len(restore_commands) == 1
    assert restore_commands[0][3:6] == ["if-shell", "-t", "%9"]
    restore_steps = [shlex.split(step) for step in restore_commands[0][-1].split(" ; ")]
    assert [
        "set-option",
        "-u",
        "-t",
        "%9",
        "status-format",
    ] in restore_steps
    assert [
        "set-option",
        "-t",
        "%9",
        "status-style",
        RODEX_STATUS_STYLE,
    ] in restore_steps
    assert "status-format[0]" not in restore_commands[0][-1]
    assert tmux.options["status-style"] == RODEX_STATUS_STYLE
    assert refresh_commands == []


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
            _capability(Path("/tmp/rodex/tmux.sock")),
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
            _capability(Path("/tmp/rodex/tmux.sock")),
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
