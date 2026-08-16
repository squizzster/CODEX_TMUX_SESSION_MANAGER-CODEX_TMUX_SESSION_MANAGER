from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rodex.tmux_completion_observer import (
    TmuxCompletionObserver,
    completion_message,
    output_may_affect_completion,
)

PROMPT = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} "


class RecordingRunner:
    def __init__(
        self,
        screen_text: str,
        *,
        cursor_text: str = "17\n",
        capture_returncode: int = 0,
    ) -> None:
        self.screen_text = screen_text
        self.cursor_text = cursor_text
        self.capture_returncode = capture_returncode
        self.commands: list[list[str]] = []
        self.completion_token = ""

    def __call__(
        self, command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        assert options == {"check": False, "text": True, "capture_output": True}
        if "#{cursor_y}" in command:
            return subprocess.CompletedProcess(
                command, 0, stdout=self.cursor_text, stderr=""
            )
        if "capture-pane" in command:
            return subprocess.CompletedProcess(
                command,
                self.capture_returncode,
                stdout=self.screen_text,
                stderr="capture failed" if self.capture_returncode else "",
            )
        if "show-options" in command:
            return subprocess.CompletedProcess(
                command,
                0 if self.completion_token else 1,
                stdout=f"{self.completion_token}\n" if self.completion_token else "",
                stderr="",
            )
        if "set-option" in command:
            if "-u" in command:
                self.completion_token = ""
            else:
                self.completion_token = command[-1]
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


@pytest.mark.parametrize(
    ("prompt_text", "expected"),
    [
        ("/", "Rodex completion: /rodex  manage this Rodex session"),
        ("/r", "Rodex completion: /rodex  [type o to narrow]"),
        ("/ro", "Rodex completion: /rodex  [Tab to complete]"),
        ("/rod", "Rodex completion: /rodex  [Tab to complete]"),
        ("/rode", "Rodex completion: /rodex  [Tab to complete]"),
        ("/rodex", "Rodex command ready: /rodex  [Enter for help]"),
        ("/rodex hi", None),
        ("/model", None),
        (None, None),
    ],
)
def test_completion_message_is_exact(prompt_text: str | None, expected: str | None) -> None:
    assert completion_message(prompt_text) == expected


def test_pane_output_only_wakes_for_a_slash_or_visible_completion() -> None:
    assert output_may_affect_completion(b"draw /", completion_visible=False)
    assert output_may_affect_completion(b"redraw", completion_visible=True)
    assert not output_may_affect_completion(b"ordinary output", completion_visible=False)


def test_observer_displays_completion_for_the_target_pane(tmp_path: Path) -> None:
    runner = RecordingRunner(f"header\n{PROMPT}/ro\nno matches\n")
    observer = TmuxCompletionObserver(
        "tmux",
        tmp_path / "tmux.sock",
        "%4",
        runner=runner,
    )

    observer.inspect_redraw()

    assert observer.completion_visible
    assert runner.commands[1][-2:] == ["-S", "17"]
    assert runner.commands[2][-2] == "@rodex_completion_token"
    display = runner.commands[3]
    assert display[-1] == "Rodex completion: /rodex  [Tab to complete]"
    assert display[display.index("-d") + 1] == "5000"
    assert "-c" not in display


def test_observer_clears_ribbon_when_prompt_leaves_prefix(tmp_path: Path) -> None:
    runner = RecordingRunner(f"{PROMPT}/rod\nno matches\n")
    observer = TmuxCompletionObserver(
        "tmux",
        tmp_path / "tmux.sock",
        "%4",
        runner=runner,
    )
    observer.inspect_redraw()
    runner.commands.clear()
    runner.screen_text = f"{PROMPT}/model\n"

    observer.inspect_redraw()

    assert not observer.completion_visible
    clears = [command for command in runner.commands if "display-message" in command][1:]
    assert len(clears) == 1
    assert all(command[-1] == "" for command in clears)
    assert all(command[command.index("-d") + 1] == "1" for command in clears)


def test_stale_observer_does_not_clear_a_newer_message_owner(tmp_path: Path) -> None:
    runner = RecordingRunner(f"{PROMPT}/ro\nno matches\n")
    observer = TmuxCompletionObserver(
        "tmux",
        tmp_path / "tmux.sock",
        "%4",
        runner=runner,
        token_factory=lambda: "ribbon-owner",
    )
    observer.inspect_redraw()
    runner.commands.clear()
    runner.completion_token = "newer-message-owner"
    runner.screen_text = f"{PROMPT}/model\n"

    observer.inspect_redraw()

    assert not observer.completion_visible
    assert runner.completion_token == "newer-message-owner"
    assert not any(
        "display-message" in command and command[-1] == "" for command in runner.commands
    )


def test_observer_yields_to_a_future_native_prefix_match(tmp_path: Path) -> None:
    runner = RecordingRunner(f"{PROMPT}/ro\n/route  choose a route\n")
    observer = TmuxCompletionObserver(
        "tmux",
        tmp_path / "tmux.sock",
        "%4",
        runner=runner,
    )

    observer.inspect_redraw()

    assert not observer.completion_visible
    assert len(runner.commands) == 2


def test_observer_fails_open_when_cursor_or_capture_is_unavailable(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(f"{PROMPT}/ro\nno matches\n", cursor_text="unknown\n")
    observer = TmuxCompletionObserver(
        "tmux",
        tmp_path / "tmux.sock",
        "%4",
        runner=runner,
    )

    observer.inspect_redraw()

    assert not observer.completion_visible
    assert len(runner.commands) == 1
