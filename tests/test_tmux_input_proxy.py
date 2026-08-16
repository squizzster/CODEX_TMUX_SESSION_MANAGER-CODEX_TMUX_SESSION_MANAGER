from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rodex.tmux_input_proxy import (
    RodexInputCommand,
    extract_prompt_text,
    extract_raw_prompt_text,
    extract_rodex_input_command,
    has_native_no_matches_marker,
    has_native_slash_completion,
    native_popup_confirms_no_match,
    proxy_enter_key,
    proxy_input_key,
)
from rodex.tmux_status import RODEX_STATUS_LEFT_FORMAT

PROMPT = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} "


class RecordingRunner:
    def __init__(
        self,
        screen_text: str,
        *,
        capture_returncode: int = 0,
        cursor_text: str = "19:5\n",
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
        if "display-message" in command and any(
            "#{cursor_y}" in argument for argument in command
        ):
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


def test_extract_prompt_text_uses_the_last_visible_composer() -> None:
    assert extract_prompt_text(f"{PROMPT}old\noutput\n{PROMPT}/ro\n") == "/ro"
    assert extract_raw_prompt_text(f"{PROMPT} /ro \n") == " /ro "


def test_native_slash_completion_detection_uses_rendered_command_rows() -> None:
    assert has_native_slash_completion(f"{PROMPT}/ro\n/route  choose a route\n", "/ro")
    assert not has_native_slash_completion(f"{PROMPT}/ro\nno matches\n", "/ro")
    assert has_native_no_matches_marker(f"{PROMPT}/ro\n  no matches  \n")
    assert native_popup_confirms_no_match(f"{PROMPT}/ro\nno matches\n", "/ro")


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [("/ro", "dex"), ("/rod", "ex"), ("/rode", "x")],
)
def test_tab_completes_unambiguous_rodex_prefix(
    tmp_path: Path, prefix: str, suffix: str
) -> None:
    runner = RecordingRunner(
        f"{PROMPT}{prefix}\nno matches\n",
        cursor_text=f"19:{len(PROMPT) + len(prefix)}\n",
    )

    assert (
        proxy_input_key(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-1",
            "Tab",
            runner=runner,
        )
        == 0
    )

    assert runner.commands[-1][-5:] == ["send-keys", "-l", "-t", "%4", suffix]


@pytest.mark.parametrize("prompt", [f"{PROMPT}/\n", f"{PROMPT}/r\n", f"{PROMPT}/model\n"])
def test_tab_for_native_completion_is_forwarded_unchanged(
    tmp_path: Path, prompt: str
) -> None:
    runner = RecordingRunner(prompt)

    assert (
        proxy_input_key(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-1",
            "Tab",
            runner=runner,
        )
        == 0
    )

    assert runner.commands[-1][-4:] == ["send-keys", "-t", "%4", "Tab"]


def test_future_native_prefix_match_takes_precedence_over_rodex_tab(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(f"{PROMPT}/ro\n/route  choose a route\n")

    assert (
        proxy_input_key(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-1",
            "Tab",
            runner=runner,
        )
        == 0
    )

    assert runner.commands[-1][-4:] == ["send-keys", "-t", "%4", "Tab"]
    assert runner.commands[-2][-2:] == ["-S", "19"]


@pytest.mark.parametrize(
    ("prompt", "cursor_x"),
    [("/ro ", 6), (" /ro", 6), ("/ro", 4)],
)
def test_tab_requires_exact_prefix_with_cursor_at_its_end(
    tmp_path: Path, prompt: str, cursor_x: int
) -> None:
    runner = RecordingRunner(
        f"{PROMPT}{prompt}\nno matches\n", cursor_text=f"19:{cursor_x}\n"
    )

    assert (
        proxy_input_key(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-1",
            "Tab",
            runner=runner,
        )
        == 0
    )

    assert runner.commands[-1][-4:] == ["send-keys", "-t", "%4", "Tab"]


def test_tab_requires_positive_native_no_matches_marker(tmp_path: Path) -> None:
    runner = RecordingRunner(f"{PROMPT}/ro\n", cursor_text="19:5\n")

    assert (
        proxy_input_key(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-1",
            "Tab",
            runner=runner,
        )
        == 0
    )

    assert runner.commands[-1][-4:] == ["send-keys", "-t", "%4", "Tab"]


def test_unknown_proxy_key_is_rejected_before_tmux(tmp_path: Path) -> None:
    runner = RecordingRunner("")

    with pytest.raises(ValueError, match="unsupported Rodex input key"):
        proxy_input_key(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "azure-crocodile",
            "client-1",
            "Space",
            runner=runner,
        )

    assert runner.commands == []


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
    assert runner.commands[3][-5:] == [
        "set-option",
        "-u",
        "-t",
        "%4",
        "@rodex_completion_token",
    ]
    assert runner.commands[4][-3:] == [
        "%4",
        "status-left",
        RODEX_STATUS_LEFT_FORMAT,
    ]
    assert runner.commands[5][-6:] == [
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

    assert runner.commands[5][-5:] == [
        "show-options",
        "-v",
        "-t",
        "%4",
        "@rodex_codex_session_uuid",
    ]
    assert runner.commands[6][-1] == (
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
