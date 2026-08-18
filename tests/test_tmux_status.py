from __future__ import annotations

import shlex
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rodex.status_bar import RODEX_STATUS_LEFT_FORMAT
from rodex.tmux_status import (
    STATUS_LEFT_CLAIM_PRIORITY_OPTION,
    STATUS_LEFT_CLAIM_PUBLISHER_OPTION,
    STATUS_LEFT_CLAIM_TOKEN_OPTION,
    STATUS_LEFT_PUBLISHER_COMPLETION,
    STATUS_LEFT_PUBLISHER_SHARED_CTRL_C,
    StatusLeftPriority,
    TmuxStatusLeftPipeline,
)


class FakeTmux:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.options: dict[str, str] = {}
        self.status_left = RODEX_STATUS_LEFT_FORMAT

    def __call__(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = list(arguments)
        self.commands.append(command)
        if command[:4] == ["if-shell", "-t", "%4", "-F"] or (
            command[:2] == ["if-shell", "-t"]
        ):
            if self._condition_is_true(command[-2]):
                for tmux_command in command[-1].split(" ; "):
                    self._apply(shlex.split(tmux_command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == ["show-options", "-v"]:
            value = self.options.get(command[-1], "")
            return subprocess.CompletedProcess(
                command,
                0 if value else 1,
                stdout=value,
                stderr="",
            )
        self._apply(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def _condition_is_true(self, condition: str) -> bool:
        if condition == "1":
            return True
        if condition.startswith("#{<=:"):
            current = int(self.options.get(STATUS_LEFT_CLAIM_PRIORITY_OPTION, "0"))
            requested = int(condition.rsplit(",", maxsplit=1)[1].removesuffix("}"))
            return current <= requested
        if STATUS_LEFT_CLAIM_TOKEN_OPTION in condition:
            expected = condition.rsplit(",", maxsplit=1)[1].removesuffix("}")
            return self.options.get(STATUS_LEFT_CLAIM_TOKEN_OPTION) == expected
        if STATUS_LEFT_CLAIM_PUBLISHER_OPTION in condition:
            expected = condition.rsplit(",", maxsplit=1)[1].removesuffix("}")
            return self.options.get(STATUS_LEFT_CLAIM_PUBLISHER_OPTION) == expected
        raise AssertionError(f"unexpected tmux condition: {condition}")

    def _apply(self, command: list[str]) -> None:
        if command[:2] == ["set-option", "-u"]:
            self.options.pop(command[-1], None)
        elif command[:1] == ["set-option"] and command[-2] == "status-left":
            self.status_left = command[-1]
        elif command[:1] == ["set-option"]:
            self.options[command[-2]] = command[-1]


def publish(
    status: TmuxStatusLeftPipeline,
    *,
    publisher: str,
    token: str,
    priority: StatusLeftPriority,
    message: str,
) -> bool:
    return status.publish_transient(
        publisher=publisher,
        token=token,
        priority=priority,
        status_format=message,
    )


def test_status_pipeline_publishes_and_exact_token_restores_atomically() -> None:
    tmux = FakeTmux()
    status = TmuxStatusLeftPipeline(tmux, "%4")

    assert publish(
        status,
        publisher=STATUS_LEFT_PUBLISHER_COMPLETION,
        token="completion-1",
        priority=StatusLeftPriority.COMPLETION,
        message="completion message",
    )
    assert tmux.options == {
        STATUS_LEFT_CLAIM_PRIORITY_OPTION: "10",
        STATUS_LEFT_CLAIM_PUBLISHER_OPTION: "completion",
        STATUS_LEFT_CLAIM_TOKEN_OPTION: "completion-1",
    }
    assert tmux.status_left == "completion message"
    assert tmux.commands[0][:5] == [
        "if-shell",
        "-t",
        "%4",
        "-F",
        "#{<=:#{@rodex_status_left_claim_priority},10}",
    ]

    status.restore_if_token_matches("stale-token")
    assert tmux.status_left == "completion message"
    status.restore_if_token_matches("completion-1")
    assert tmux.options == {}
    assert tmux.status_left == RODEX_STATUS_LEFT_FORMAT


def test_higher_priority_warning_supersedes_and_blocks_completion() -> None:
    tmux = FakeTmux()
    status = TmuxStatusLeftPipeline(tmux, "%4")
    assert publish(
        status,
        publisher=STATUS_LEFT_PUBLISHER_COMPLETION,
        token="completion-1",
        priority=StatusLeftPriority.COMPLETION,
        message="completion message",
    )
    assert publish(
        status,
        publisher=STATUS_LEFT_PUBLISHER_SHARED_CTRL_C,
        token="warning-1",
        priority=StatusLeftPriority.SAFETY_WARNING,
        message="safety warning",
    )
    assert not publish(
        status,
        publisher=STATUS_LEFT_PUBLISHER_COMPLETION,
        token="completion-2",
        priority=StatusLeftPriority.COMPLETION,
        message="new completion",
    )
    assert tmux.options[STATUS_LEFT_CLAIM_TOKEN_OPTION] == "warning-1"
    assert tmux.status_left == "safety warning"

    status.restore_if_token_matches("completion-1")
    assert tmux.status_left == "safety warning"
    status.restore_if_publisher_matches(STATUS_LEFT_PUBLISHER_SHARED_CTRL_C)
    assert tmux.status_left == RODEX_STATUS_LEFT_FORMAT


def test_real_tmux_concurrent_publishers_leave_a_matching_atomic_claim(
    tmp_path: Path,
) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"
    subprocess.run(
        [
            tmux_binary,
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "status",
            "sleep 10",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        [
            tmux_binary,
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "decoy",
            "sleep 10",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    for option, value in (
        (STATUS_LEFT_CLAIM_PUBLISHER_OPTION, "decoy"),
        (STATUS_LEFT_CLAIM_TOKEN_OPTION, "decoy-token"),
        (STATUS_LEFT_CLAIM_PRIORITY_OPTION, "100"),
        ("status-left", "decoy message"),
    ):
        subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "set-option",
                "-t",
                "=decoy:",
                option,
                value,
            ],
            check=True,
            text=True,
            capture_output=True,
        )
    barrier = threading.Barrier(2)

    def publish_after_barrier(
        publisher: str,
        token: str,
        priority: StatusLeftPriority,
        message: str,
    ) -> None:
        def tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [tmux_binary, "-S", str(socket_path), *arguments],
                check=False,
                text=True,
                capture_output=True,
            )

        barrier.wait()
        TmuxStatusLeftPipeline(tmux, "=status:").publish_transient(
            publisher=publisher,
            token=token,
            priority=priority,
            status_format=message,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(
                    publish_after_barrier,
                    STATUS_LEFT_PUBLISHER_COMPLETION,
                    "completion-token",
                    StatusLeftPriority.COMPLETION,
                    "completion message",
                ),
                executor.submit(
                    publish_after_barrier,
                    STATUS_LEFT_PUBLISHER_SHARED_CTRL_C,
                    "warning-token",
                    StatusLeftPriority.SAFETY_WARNING,
                    "safety warning",
                ),
            )
            for future in futures:
                future.result(timeout=2)

        def show(option: str) -> str:
            return subprocess.run(
                [
                    tmux_binary,
                    "-S",
                    str(socket_path),
                    "show-options",
                    "-v",
                    "-t",
                    "=status:",
                    option,
                ],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()

        assert (
            show(STATUS_LEFT_CLAIM_PUBLISHER_OPTION) == STATUS_LEFT_PUBLISHER_SHARED_CTRL_C
        )
        assert show(STATUS_LEFT_CLAIM_TOKEN_OPTION) == "warning-token"
        assert show(STATUS_LEFT_CLAIM_PRIORITY_OPTION) == "100"
        assert show("status-left") == "safety warning"

        def show_decoy(option: str) -> str:
            return subprocess.run(
                [
                    tmux_binary,
                    "-S",
                    str(socket_path),
                    "show-options",
                    "-v",
                    "-t",
                    "=decoy:",
                    option,
                ],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()

        assert show_decoy(STATUS_LEFT_CLAIM_TOKEN_OPTION) == "decoy-token"
        assert show_decoy("status-left") == "decoy message"
    finally:
        subprocess.run(
            [tmux_binary, "-S", str(socket_path), "kill-server"],
            check=False,
            text=True,
            capture_output=True,
        )
