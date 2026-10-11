from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event

import pyte
from terminal_tmux_fixture import tmux_terminal
from test_managed_startup import RodexTerminalClient
from test_tmux_status import FakeTmux
from wcwidth import wcswidth

from rodex.command_policy_status import POLICY_STATUS_WIDTH, CommandPolicyStatus, policy_status_format
from rodex.tmux_session_capability import TmuxRuntimeCapability
from rodex.tmux_status import STATUS_CLAIM_TOKEN_OPTION, StatusPriority, TmuxStatusPipeline, TmuxStatusPresentation
from rodex_registry import RodexRuntimeId


def test_status_removes_controls_collapses_whitespace_and_bounds_cells():
    value = policy_status_format("find\x00\x1b\t .\n\r\u202e  " + "界" * 200)
    assert value.startswith(" find . ")
    assert value.endswith("… ")
    assert wcswidth(value) <= POLICY_STATUS_WIDTH + 2
    assert len(policy_status_format("a" + "\u0301" * 10000)) < POLICY_STATUS_WIDTH * 2 + 4
    assert policy_status_format("#{host} #(touch sentinel) #[fg=red]") == " ##{host} ##(touch sentinel) ##[fg=red] "


def make_status(tmp_path, duration=0.03):
    status = CommandPolicyStatus(
        "tmux",
        TmuxRuntimeCapability(tmp_path / "tmux.sock", "a" * 32, "$1", "%4", RodexRuntimeId(1)),
        "%4",
        duration=duration,
    )
    return status


def test_status_coalesces_while_tmux_is_slow_and_restores_only_its_token(tmp_path):
    tmux = FakeTmux()
    status = make_status(tmp_path)
    pipeline = TmuxStatusPipeline(tmux, "%4")
    entered, release = Event(), Event()
    messages = []

    class SlowPipeline:
        def publish_transient(self, **kwargs):
            messages.append(kwargs["presentation"].status_format)
            entered.set()
            assert release.wait(3)
            return pipeline.publish_transient(**kwargs)

        def restore_if_token_matches(self, token):
            pipeline.restore_if_token_matches(token)

    status._pipeline = SlowPipeline()
    try:
        status.allowed("first")
        assert entered.wait(1)
        for index in range(100):
            status.allowed(f"next-{index}")
        assert status._pending == " Rodex policy allowed: next-99 "
        release.set()
        deadline = time.monotonic() + 2
        while len(messages) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert messages == [" Rodex policy allowed: first ", " Rodex policy allowed: next-99 "]
        assert pipeline.publish_transient(
            publisher="warning",
            token="warning-token",
            priority=StatusPriority.SAFETY_WARNING,
            presentation=TmuxStatusPresentation(status_left="warning"),
        )
    finally:
        release.set()
        status.close()
    assert tmux.options[STATUS_CLAIM_TOKEN_OPTION] == "warning-token"
    assert tmux.status_left == "warning"


def test_command_policy_cannot_replace_a_safety_warning(tmp_path):
    tmux = FakeTmux()
    status = make_status(tmp_path)
    status._pipeline = TmuxStatusPipeline(tmux, "%4")
    status._pipeline.publish_transient(
        publisher="warning",
        token="warning-token",
        priority=StatusPriority.SAFETY_WARNING,
        presentation=TmuxStatusPresentation(status_left="warning"),
    )
    try:
        status.allowed("find .")
        time.sleep(0.1)
    finally:
        status.close()
    assert tmux.options[STATUS_CLAIM_TOKEN_OPTION] == "warning-token"
    assert tmux.status_left == "warning"


def test_real_tmux_keeps_command_formats_literal_and_never_runs_jobs(tmp_path):
    with tmux_terminal(tmp_path) as terminal, tempfile.TemporaryDirectory(prefix="rp-") as directory:
        marker = Path(directory) / "must-not-exist"
        message = f"Rodex policy allowed: #{{host}} #(touch {marker}) #[fg=red]"

        def tmux(*arguments):
            return subprocess.run(
                ["tmux", "-S", terminal.socket_path, *arguments],
                capture_output=True,
                text=True,
                timeout=2,
            )

        pipeline = TmuxStatusPipeline(tmux, "%0")
        assert pipeline.publish_transient(
            publisher="command-policy",
            token="policy-token",
            priority=StatusPriority.COMMAND_POLICY,
            presentation=TmuxStatusPresentation(status_format=policy_status_format(message)),
        )
        terminal.tmux("set-option", "-t", "%0", "status", "on")
        environment = {**os.environ, "TERM": "xterm-256color"}
        environment.pop("TMUX", None)
        with RodexTerminalClient(
            ["tmux", "-S", terminal.socket_path, "attach-session", "-t", "fixture"], environment, tmp_path
        ) as client:
            screen = pyte.Screen(120, 36)
            stream = pyte.ByteStream(screen)
            offset = 0
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                client.poll()
                stream.feed(client.output[offset:])
                offset = len(client.output)
                if screen.display[-1].strip() == message:
                    break
            assert screen.display[-1].strip() == message
        assert not marker.exists()
        pipeline.restore_if_token_matches("policy-token")
        assert "Rodex policy" not in terminal.tmux("show-options", "-v", "-t", "%0", "status-format[0]")
