from __future__ import annotations

import json
import os
import pty
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from rodex.runtime import LiveTmuxSession, RodexRuntimeLauncher
from rodex.tmux_shared_ctrl_c import handle_shared_ctrl_c
from rodex.tmux_status import (
    RODEX_STATUS_LEFT_FORMAT,
    RODEX_STATUS_STYLE,
    STATUS_CLAIM_PRIORITY_OPTION,
    STATUS_CLAIM_PUBLISHER_OPTION,
    STATUS_CLAIM_TOKEN_OPTION,
)


class RecordingTmux:
    def __init__(self, *, attached_count: int = 2) -> None:
        self.commands: list[list[str]] = []
        self.confirmation = ""
        self.attached_count = attached_count
        self.sent_ctrl_c_count = 0
        self.status_options: dict[str, str] = {}
        self.status_left = RODEX_STATUS_LEFT_FORMAT

    def __call__(
        self, command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        arguments = command[3:]
        output = ""
        returncode = 0
        if arguments[:1] == ["display-message"]:
            output = f"{self.attached_count}\n"
        elif arguments[:2] == ["show-options", "-v"]:
            if arguments[-1] == "@rodex_shared_ctrl_c_confirmation":
                output = self.confirmation
            else:
                output = self.status_options.get(arguments[-1], "")
            returncode = 0 if output else 1
        elif arguments[:2] == ["if-shell", "-t"]:
            format_index = arguments.index("-F")
            condition = arguments[format_index + 1]
            branch_index = format_index + (2 if self._condition_is_true(condition) else 3)
            if branch_index < len(arguments):
                for action in arguments[branch_index].split(" ; "):
                    if action:
                        self._apply(shlex.split(action))
        elif arguments[:2] == ["set-option", "-u"]:
            self._apply(arguments)
        elif arguments[:2] == ["set-option", "-o"]:
            if arguments[-2] in self.status_options:
                returncode = 1
            else:
                self.status_options[arguments[-2]] = arguments[-1]
        elif arguments[:1] == ["set-option"]:
            self._apply(arguments)
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=output,
            stderr="",
        )

    def _condition_is_true(self, condition: str) -> bool:
        if condition == "#{==:#{session_attached},1}":
            return self.attached_count == 1
        if condition.startswith("#{<=:"):
            current = int(self.status_options.get(STATUS_CLAIM_PRIORITY_OPTION, "0"))
            requested = int(condition.rsplit(",", maxsplit=1)[1].removesuffix("}"))
            return current <= requested
        if STATUS_CLAIM_TOKEN_OPTION in condition:
            expected = condition.rsplit(",", maxsplit=1)[1].removesuffix("}")
            return self.status_options.get(STATUS_CLAIM_TOKEN_OPTION) == expected
        if "@rodex_shared_ctrl_c_confirmation_claim" in condition:
            claim = self.status_options.get("@rodex_shared_ctrl_c_confirmation_claim")
            return bool(claim) and self.confirmation == claim
        raise AssertionError(f"unexpected condition: {condition}")

    def _apply(self, arguments: list[str]) -> None:
        if arguments[:1] == ["send-keys"]:
            self.sent_ctrl_c_count += 1
        elif arguments[:2] == ["set-option", "-u"]:
            if arguments[-1] == "@rodex_shared_ctrl_c_confirmation":
                self.confirmation = ""
            else:
                self.status_options.pop(arguments[-1], None)
        elif arguments[-2] == "@rodex_shared_ctrl_c_confirmation":
            self.confirmation = arguments[-1]
        elif arguments[-2] == "status-left":
            self.status_left = arguments[-1]
        else:
            self.status_options[arguments[-2]] = arguments[-1]


def test_private_ctrl_c_is_forwarded_without_confirmation(tmp_path: Path) -> None:
    runner = RecordingTmux(attached_count=1)

    assert (
        handle_shared_ctrl_c(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "client-one",
            runner=runner,
        )
        == 0
    )

    assert runner.sent_ctrl_c_count == 1
    assert any(
        command[3:4] == ["if-shell"]
        and "#{session_attached}" in " ".join(command)
        and "send-keys" in " ".join(command)
        for command in runner.commands
    )
    assert any("#{session_attached}" in command for command in runner.commands)


def test_private_ctrl_c_is_withheld_if_a_client_attaches_before_send(
    tmp_path: Path,
) -> None:
    runner = RecordingTmux(attached_count=1)

    def attach_after_attachment_query(
        command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        result = runner(command, **options)
        if command[3:4] == ["display-message"]:
            runner.attached_count = 2
        return result

    assert (
        handle_shared_ctrl_c(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "client-one",
            runner=attach_after_attachment_query,
        )
        == 0
    )

    assert runner.sent_ctrl_c_count == 0


def test_prearmed_private_ctrl_c_race_clears_hidden_confirmation(
    tmp_path: Path,
) -> None:
    runner = RecordingTmux(attached_count=2)
    assert (
        handle_shared_ctrl_c(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "client-one",
            monotonic_nanoseconds=lambda: 10_000_000_000,
            confirmation_token=lambda: "warning-token",
            expiry_scheduler=lambda _callback: None,
            runner=runner,
        )
        == 0
    )
    assert runner.confirmation
    runner.attached_count = 1

    def attach_after_attachment_query(
        command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        result = runner(command, **options)
        if command[3:4] == ["display-message"]:
            runner.attached_count = 2
        return result

    assert (
        handle_shared_ctrl_c(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "client-one",
            monotonic_nanoseconds=lambda: 11_000_000_000,
            runner=attach_after_attachment_query,
        )
        == 0
    )

    assert runner.sent_ctrl_c_count == 0
    assert runner.confirmation == ""
    assert runner.status_left == RODEX_STATUS_LEFT_FORMAT


def test_first_shared_ctrl_c_publishes_a_temporary_status_warning(tmp_path: Path) -> None:
    runner = RecordingTmux()
    expiry_callbacks: list[Callable[[], None]] = []

    assert (
        handle_shared_ctrl_c(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "client-one",
            monotonic_nanoseconds=lambda: 10_000_000_000,
            confirmation_token=lambda: "warning-token",
            expiry_scheduler=expiry_callbacks.append,
            runner=runner,
        )
        == 0
    )

    assert not any("send-keys" in command for command in runner.commands)
    confirmation = json.loads(runner.confirmation)
    assert confirmation == {
        "armed_at_monotonic_ns": 10_000_000_000,
        "client_name": "client-one",
        "status_token": "warning-token",
    }
    assert "CTRL-C ARMED" in runner.status_left
    assert "SHARED session" in runner.status_left
    assert "may END it for everyone" in runner.status_left
    assert "CTRL-B d detaches only you" in runner.status_left
    assert runner.status_options == {
        STATUS_CLAIM_PRIORITY_OPTION: "100",
        STATUS_CLAIM_PUBLISHER_OPTION: "shared-ctrl-c",
        STATUS_CLAIM_TOKEN_OPTION: "warning-token",
        "status-style": RODEX_STATUS_STYLE,
    }

    expiry_callbacks[0]()

    assert runner.confirmation == ""
    assert runner.status_options == {"status-style": RODEX_STATUS_STYLE}
    assert runner.status_left == RODEX_STATUS_LEFT_FORMAT


def test_same_client_second_shared_ctrl_c_is_forwarded_within_window(
    tmp_path: Path,
) -> None:
    runner = RecordingTmux()
    moments = iter((10_000_000_000, 11_500_000_000))
    expiry_callbacks: list[Callable[[], None]] = []

    for _ in range(2):
        assert (
            handle_shared_ctrl_c(
                "tmux",
                tmp_path / "tmux.sock",
                "%4",
                "client-one",
                monotonic_nanoseconds=lambda: next(moments),
                confirmation_token=lambda: "warning-token",
                expiry_scheduler=expiry_callbacks.append,
                runner=runner,
            )
            == 0
        )

    assert runner.confirmation == ""
    assert any(
        command[3:4] == ["if-shell"] and "send-keys" in " ".join(command)
        for command in runner.commands
    )


@pytest.mark.parametrize(
    ("second_client", "second_moment"),
    [("client-two", 11_000_000_000), ("client-one", 12_000_000_001)],
)
def test_other_client_or_expired_confirmation_rearms_without_forwarding(
    tmp_path: Path,
    second_client: str,
    second_moment: int,
) -> None:
    runner = RecordingTmux()
    expiry_callbacks: list[Callable[[], None]] = []
    tokens = iter(("first-warning", "second-warning"))
    assert (
        handle_shared_ctrl_c(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            "client-one",
            monotonic_nanoseconds=lambda: 10_000_000_000,
            confirmation_token=lambda: next(tokens),
            expiry_scheduler=expiry_callbacks.append,
            runner=runner,
        )
        == 0
    )
    assert (
        handle_shared_ctrl_c(
            "tmux",
            tmp_path / "tmux.sock",
            "%4",
            second_client,
            monotonic_nanoseconds=lambda: second_moment,
            confirmation_token=lambda: next(tokens),
            expiry_scheduler=expiry_callbacks.append,
            runner=runner,
        )
        == 0
    )

    assert not any("send-keys" in command for command in runner.commands)
    confirmation = json.loads(runner.confirmation)
    assert confirmation["client_name"] == second_client
    assert confirmation["armed_at_monotonic_ns"] == second_moment
    assert confirmation["status_token"] == "second-warning"


def test_real_tmux_first_shared_ctrl_c_keeps_both_clients_attached(
    tmp_path: Path,
) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"
    session_name = "shared-ctrl-c"
    interactive_client_pid: int | None = None
    control_client: subprocess.Popen[str] | None = None
    terminal_master: int | None = None

    def tmux(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [tmux_binary, "-S", str(socket_path), *arguments],
            check=check,
            text=True,
            capture_output=True,
        )

    def wait_for_attached_count(expected: int) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            shown = tmux(
                "display-message",
                "-p",
                "-t",
                f"={session_name}:",
                "-F",
                "#{session_attached}",
            )
            if shown.stdout.strip() == str(expected):
                return
            time.sleep(0.01)
        pytest.fail(f"tmux did not report {expected} attached clients")

    tmux("new-session", "-d", "-s", session_name, "sleep 30")
    try:
        RodexRuntimeLauncher(
            "codex",
            tmux_binary,
            python_executable=sys.executable,
        ).initialise_session_ui(LiveTmuxSession(socket_path, session_name))
        control_client = subprocess.Popen(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "-C",
                "attach-session",
                "-t",
                f"={session_name}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for_attached_count(1)
        interactive_client_pid, terminal_master = pty.fork()
        interactive_environment = os.environ.copy()
        interactive_environment["TERM"] = "xterm-256color"
        interactive_arguments = [
            tmux_binary,
            "-S",
            str(socket_path),
            "attach-session",
            "-t",
            f"={session_name}",
        ]
        if interactive_client_pid == 0:
            os.execve(tmux_binary, interactive_arguments, interactive_environment)
        wait_for_attached_count(2)
        os.write(terminal_master, b"\x03")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            confirmation = tmux(
                "show-options",
                "-v",
                "-t",
                f"={session_name}:",
                "@rodex_shared_ctrl_c_confirmation",
                check=False,
            )
            warning_status = tmux(
                "display-message",
                "-p",
                "-t",
                f"={session_name}:",
                "-F",
                "#{T:status-left}",
            )
            if confirmation.stdout.strip() and "CTRL-C ARMED" in warning_status.stdout:
                break
            time.sleep(0.01)
        else:
            pytest.fail("first shared Ctrl-C did not publish its warning")
        assert "CTRL-C ARMED" in warning_status.stdout
        assert tmux("has-session", "-t", f"={session_name}", check=False).returncode == 0
        wait_for_attached_count(2)

        os.write(terminal_master, b"\x03")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if tmux("has-session", "-t", f"={session_name}", check=False).returncode != 0:
                break
            time.sleep(0.01)
        else:
            pytest.fail("confirmed Ctrl-C was not forwarded to the shared pane")
    finally:
        tmux("kill-server", check=False)
        if control_client is not None:
            try:
                control_client.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                control_client.terminate()
                control_client.communicate(timeout=2)
        if interactive_client_pid is not None:
            waited_pid, _status = os.waitpid(interactive_client_pid, os.WNOHANG)
            if waited_pid == 0:
                os.kill(interactive_client_pid, signal.SIGTERM)
                os.waitpid(interactive_client_pid, 0)
        if terminal_master is not None:
            os.close(terminal_master)
