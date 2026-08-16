from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rodex.tmux_input_proxy import (
    RodexInputCommand,
    extract_rodex_input_command,
    proxy_enter_key,
)

PROMPT = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} "


class RecordingRunner:
    def __init__(
        self,
        screen_text: str,
        *,
        capture_returncode: int = 0,
        cursor_text: str = "19\n",
    ) -> None:
        self.screen_text = screen_text
        self.capture_returncode = capture_returncode
        self.cursor_text = cursor_text
        self.commands: list[list[str]] = []

    def __call__(
        self, command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        assert options == {"check": False, "text": True, "capture_output": True}
        if "display-message" in command and "#{cursor_y}" in command:
            return subprocess.CompletedProcess(
                command, 0, stdout=self.cursor_text, stderr=""
            )
        if "show-options" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="01a00654-f2bc-7a30-834a-a5f886a65f82\n",
                stderr="",
            )
        if "capture-pane" in command:
            return subprocess.CompletedProcess(
                command,
                self.capture_returncode,
                stdout=self.screen_text,
                stderr="capture failed" if self.capture_returncode else "",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


@pytest.mark.parametrize(
    ("screen_text", "expected"),
    [
        (f"header\n\n{PROMPT}/rodex\nstatus\n", RodexInputCommand(())),
        (f"{PROMPT}/rodex hi\n", RodexInputCommand(("hi",))),
        (
            f'{PROMPT}/rodex message "two words"\n',
            RodexInputCommand(("message", "two words")),
        ),
        (f'{PROMPT}/rodex message "unfinished\n', RodexInputCommand((), True)),
        (f"{PROMPT}/rodexx hi\n", None),
        (f"{PROMPT}explain /rodex to me\n", None),
        ("no prompt here\n", None),
    ],
)
def test_extract_rodex_input_command(
    screen_text: str, expected: RodexInputCommand | None
) -> None:
    assert extract_rodex_input_command(screen_text) == expected


@pytest.mark.parametrize(
    "prompt",
    [f"{PROMPT}hello\n", f"{PROMPT}/model\n", f"{PROMPT}/rodexx hi\n"],
)
def test_ordinary_enter_is_forwarded_unchanged(tmp_path: Path, prompt: str) -> None:
    runner = RecordingRunner(prompt)

    assert (
        proxy_enter_key(
            "/usr/bin/tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-1",
            runner=runner,
        )
        == 0
    )

    assert runner.commands[-1][-4:] == ["send-keys", "-t", "%4", "Enter"]
    assert runner.commands[1][-4:] == ["-S", "19", "-E", "19"]


def test_rodex_hi_is_cleared_and_acknowledged_in_tmux(tmp_path: Path) -> None:
    runner = RecordingRunner(f"header\n\n{PROMPT}/rodex hi\nstatus\n")

    assert (
        proxy_enter_key(
            "/usr/bin/tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-1",
            runner=runner,
        )
        == 0
    )

    assert runner.commands[2][-4:] == ["send-keys", "-t", "%4", "C-c"]
    assert runner.commands[3][-6:] == [
        "display-message",
        "-d",
        "5000",
        "-t",
        "%4",
        "Rodex: hello from azure-crocodile",
    ]
    assert not any(command[-1:] == ["Enter"] for command in runner.commands[2:])


def test_rodex_detach_targets_the_current_tmux_client(tmp_path: Path) -> None:
    runner = RecordingRunner(f"{PROMPT}/rodex detach\n")

    assert (
        proxy_enter_key(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-9",
            runner=runner,
        )
        == 0
    )

    assert runner.commands[-1][-3:] == ["detach-client", "-t", "client-9"]


def test_rodex_identity_reads_the_live_codex_uuid(tmp_path: Path) -> None:
    runner = RecordingRunner(f"{PROMPT}/rodex identity\n")

    assert (
        proxy_enter_key(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-1",
            runner=runner,
        )
        == 0
    )

    assert runner.commands[3][-5:] == [
        "show-options",
        "-v",
        "-t",
        "%4",
        "@rodex_codex_session_uuid",
    ]
    assert runner.commands[4][-1] == (
        "Rodex: azure-crocodile -> Codex 01a00654-f2bc-7a30-834a-a5f886a65f82"
    )


def test_capture_failure_forwards_enter_instead_of_swallowing_input(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner("", capture_returncode=1)

    assert (
        proxy_enter_key(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-1",
            runner=runner,
        )
        == 0
    )
    assert runner.commands[-1][-4:] == ["send-keys", "-t", "%4", "Enter"]


def test_invalid_cursor_position_forwards_enter(tmp_path: Path) -> None:
    runner = RecordingRunner("", cursor_text="not-a-row\n")

    assert (
        proxy_enter_key(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-1",
            runner=runner,
        )
        == 0
    )
    assert runner.commands[-1][-4:] == ["send-keys", "-t", "%4", "Enter"]
