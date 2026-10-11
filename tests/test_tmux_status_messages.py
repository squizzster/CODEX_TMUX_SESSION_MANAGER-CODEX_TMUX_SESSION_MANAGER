from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pyte
import pytest
from terminal_tmux_fixture import tmux_terminal
from test_managed_startup import RodexTerminalClient
from test_tmux_status import FakeTmux
from wcwidth import wcswidth

from rodex.status_bar import RODEX_STATUS_COLOURS, context_status_segment, literal_status_text
from rodex.tmux_session_capability import (
    RODEX_SHARED_TMUX_PROTOCOL,
    parse_tmux_session_capability,
    registered_primary_pane_if_shell_condition,
)
from rodex.tmux_status import (
    RODEX_STATUS_LEFT_FORMAT,
    STATUS_CLAIM_TOKEN_OPTION,
    StatusPriority,
    TimedStatusPublisher,
    TmuxStatusPipeline,
    TmuxStatusPresentation,
)


def test_status_removes_controls_collapses_whitespace_and_bounds_cells():
    value = literal_status_text("find\x00\x1b\t .\n\r\u202e  " + "界" * 200, width=140)
    assert value.startswith(" find . ")
    assert value.endswith("… ")
    assert wcswidth(value) <= 142
    assert len(literal_status_text("a" + "\u0301" * 10000, width=140)) < 284
    assert literal_status_text("#{host} #(touch sentinel) #[fg=red]") == " ##{host} ##(touch sentinel) ##[fg=red] "


def test_message_presentation_retains_the_tmux_prefix_indicator():
    presentation = TmuxStatusPresentation.message("Rodex policy inactive").status_format

    assert "#{client_prefix}" in presentation
    assert "#{==:#{prefix},C-b}" in presentation
    assert "CTRL-B MODE" in presentation
    assert presentation.endswith(" Rodex policy inactive ")


def make_publisher(pipeline, duration=0.03):
    return TimedStatusPublisher(
        pipeline,
        publisher="test-message",
        priority=StatusPriority.COMMAND_POLICY,
        duration=duration,
    )


def test_status_coalesces_while_tmux_is_slow_and_restores_only_its_token():
    tmux = FakeTmux()
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

    status = make_publisher(SlowPipeline())
    try:
        status.publish(TmuxStatusPresentation.message("first"))
        assert entered.wait(1)
        for index in range(100):
            status.publish(TmuxStatusPresentation.message(f"next-{index}"))
        assert status._pending == TmuxStatusPresentation.message("next-99")
        release.set()
        deadline = time.monotonic() + 2
        while len(messages) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert messages == [
            TmuxStatusPresentation.message("first").status_format,
            TmuxStatusPresentation.message("next-99").status_format,
        ]
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


def test_each_message_reappears_then_restores_the_normal_bar():
    tmux = FakeTmux()
    status = make_publisher(TmuxStatusPipeline(tmux, "%4"))
    try:
        for command in ("printf first", "printf second"):
            status.publish(TmuxStatusPresentation.message(command))
            deadline = time.monotonic() + 1
            while STATUS_CLAIM_TOKEN_OPTION not in tmux.options and time.monotonic() < deadline:
                time.sleep(0.005)
            assert command in tmux.options["status-format[0]"]

            deadline = time.monotonic() + 1
            while STATUS_CLAIM_TOKEN_OPTION in tmux.options and time.monotonic() < deadline:
                time.sleep(0.005)
            assert STATUS_CLAIM_TOKEN_OPTION not in tmux.options
            assert "status-format[0]" not in tmux.options
            assert tmux.status_left == RODEX_STATUS_LEFT_FORMAT
    finally:
        status.close()


def test_timed_message_cannot_replace_a_safety_warning():
    tmux = FakeTmux()
    pipeline = TmuxStatusPipeline(tmux, "%4")
    status = make_publisher(pipeline)
    pipeline.publish_transient(
        publisher="warning",
        token="warning-token",
        priority=StatusPriority.SAFETY_WARNING,
        presentation=TmuxStatusPresentation(status_left="warning"),
    )
    try:
        status.publish(TmuxStatusPresentation.message("find ."))
        time.sleep(0.1)
    finally:
        status.close()
    assert tmux.options[STATUS_CLAIM_TOKEN_OPTION] == "warning-token"
    assert tmux.status_left == "warning"


@pytest.mark.parametrize("preempted", [False, True])
def test_rejected_native_timer_restores_only_its_own_notice(tmp_path, preempted):
    tmux = FakeTmux()

    def rejecting_scheduler(*arguments):
        if arguments[0] == "run-shell":
            if preempted:
                pipeline.publish_transient(
                    publisher="warning",
                    token="warning-token",
                    priority=StatusPriority.SAFETY_WARNING,
                    presentation=TmuxStatusPresentation.message("warning"),
                )
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="timer rejected")
        return tmux(*arguments)

    pipeline = TmuxStatusPipeline(rejecting_scheduler, "%4")
    capability = parse_tmux_session_capability(
        tmp_path / "tmux.sock",
        "a" * 32,
        "$0",
        "%4",
        "1" * 16,
        "2" * 16,
        "3" * 16,
        "1",
        "01a00654-f2bc-7a30-834a-a5f886a65f82",
    )
    assert (
        pipeline.publish_timed(
            publisher="fixture",
            priority=StatusPriority.COMMAND_POLICY,
            presentation=TmuxStatusPresentation.message("notice"),
            duration=5,
            capability=capability,
        )
        is None
    )
    if preempted:
        assert tmux.options[STATUS_CLAIM_TOKEN_OPTION] == "warning-token"
        assert tmux.options["status-format[0]"] == TmuxStatusPresentation.message("warning").status_format
    else:
        assert STATUS_CLAIM_TOKEN_OPTION not in tmux.options
        assert "status-format[0]" not in tmux.options
        assert tmux.status_left == RODEX_STATUS_LEFT_FORMAT


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
            presentation=TmuxStatusPresentation.message(message),
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
            os.write(client.terminal, b"\x02")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                client.poll()
                stream.feed(client.output[offset:])
                offset = len(client.output)
                if "CTRL-B MODE" in screen.display[-1]:
                    break
            assert "CTRL-B MODE" in screen.display[-1]
            os.write(client.terminal, b"d")
        assert not marker.exists()
        pipeline.restore_if_token_matches("policy-token")
        assert "Rodex policy" not in terminal.tmux("show-options", "-v", "-t", "%0", "status-format[0]")


@contextmanager
def registered_status_terminal(tmp_path):
    with tmux_terminal(tmp_path) as terminal:
        capability = parse_tmux_session_capability(
            Path(terminal.socket_path),
            "a" * 32,
            "$0",
            "%0",
            "1" * 16,
            "2" * 16,
            "3" * 16,
            "1",
            "01a00654-f2bc-7a30-834a-a5f886a65f82",
        )
        for option, value in (
            ("@rodex_shared_tmux_protocol", RODEX_SHARED_TMUX_PROTOCOL),
            ("@rodex_shared_tmux_server_id", capability.tmux_server_id),
            ("@rodex_server_runtime_id", str(capability.runtime_id)),
        ):
            terminal.tmux("set-option", "-s", option, value)
        for option, value in (
            ("@rodex_primary_pane_id", capability.pane_target),
            ("@rodex_runtime_id", str(capability.runtime_id)),
            ("@rodex_session_id", str(capability.rodex_session_id)),
            ("@rodex_registry_id", str(capability.registry_id)),
            ("@rodex_sessions_id", str(capability.internal_session_id)),
            ("@rodex_codex_session_id", str(capability.codex_session_id)),
            ("@rodex_registration_state", "registered"),
        ):
            terminal.tmux("set-option", "-t", "%0", option, value)
        terminal.tmux("set-option", "-p", "-t", "%0", "@rodex_pane_runtime_id", str(capability.runtime_id))

        def tmux(*arguments):
            return subprocess.run(
                [
                    "tmux",
                    "-S",
                    terminal.socket_path,
                    "if-shell",
                    "-t",
                    "%0",
                    "-F",
                    registered_primary_pane_if_shell_condition(capability),
                    shlex.join(arguments),
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )

        pipeline = TmuxStatusPipeline(tmux, "%0")
        pipeline.configure_base_status(reset_transient_claims=True)
        yield terminal, pipeline, capability


@pytest.mark.parametrize("expiry", ["scheduled", "worker"])
def test_real_tmux_notice_restores_rendered_colours_live_fields_and_ctrl_b(tmp_path, expiry):
    with registered_status_terminal(tmp_path) as (terminal, pipeline, capability):
        terminal.tmux("set-option", "-as", "terminal-overrides", ",xterm-256color:Tc")
        terminal.tmux("set-option", "-t", "%0", "@rodex_tool_calls", "0")
        terminal.tmux("set-option", "-t", "%0", "@rodex_context_status", context_status_segment(42))
        environment = {**os.environ, "TERM": "xterm-256color"}
        environment.pop("TMUX", None)
        with RodexTerminalClient(
            ["tmux", "-S", terminal.socket_path, "attach-session", "-t", "fixture"], environment, tmp_path
        ) as client:
            screen = pyte.Screen(120, 36)
            stream = pyte.ByteStream(screen)
            offset = 0

            def wait_for_status(text):
                nonlocal offset
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    client.poll()
                    stream.feed(client.output[offset:])
                    offset = len(client.output)
                    if text in screen.display[-1]:
                        assert "#[" not in screen.display[-1]
                        return screen.display[-1]
                pytest.fail(f"expected {text!r} in status, got {screen.display[-1]!r}")

            def check_style(row, text, colour):
                cell = screen.buffer[35][row.index(text)]
                assert cell.fg == colour.removeprefix("#").lower()
                assert cell.bold

            wait_for_status("Context: 42%")
            publisher = make_publisher(pipeline, duration=1)
            try:
                message = TmuxStatusPresentation.message("Notice expires in one second")
                if expiry == "scheduled":
                    assert pipeline.publish_timed(
                        publisher="fixture",
                        priority=StatusPriority.COMMAND_POLICY,
                        presentation=message,
                        duration=1,
                        capability=capability,
                    )
                else:
                    publisher.publish(message)
                wait_for_status("Notice expires")
                os.write(client.terminal, b"\x02")
                row = wait_for_status("CTRL-B MODE")
                assert screen.buffer[35][row.index("CTRL-B MODE")].bold
                os.write(client.terminal, b"\x1b")

                row = wait_for_status("Context: 42%")
                check_style(row, "Rodex: fixture", RODEX_STATUS_COLOURS.primary_blue)
                check_style(row, "Tools: 0", RODEX_STATUS_COLOURS.tool_count)
                check_style(row, "Mouse: OFF", RODEX_STATUS_COLOURS.mouse_mode)
                assert terminal.tmux("show-options", "-v", "-t", "%0", "status-left").strip() == RODEX_STATUS_LEFT_FORMAT

                # Live fields must remain formats after expiry, not frozen strings.
                terminal.tmux("set-option", "-t", "%0", "@rodex_tool_calls", "17")
                terminal.tmux("set-option", "-t", "%0", "@rodex_context_status", context_status_segment(81))
                terminal.tmux("refresh-client", "-S")
                row = wait_for_status("Context: 81%")
                check_style(row, "Tools: 17", RODEX_STATUS_COLOURS.tool_count)
                check_style(row, "Context: 81%", RODEX_STATUS_COLOURS.context_danger)

                os.write(client.terminal, b"\x02")
                wait_for_status("CTRL-B MODE")
                os.write(client.terminal, b"d")
                deadline = time.monotonic() + 3
                while client.exit_code is None and time.monotonic() < deadline:
                    client.poll()
                assert client.exit_code == 0
            finally:
                publisher.close()


@pytest.mark.parametrize("replacement", ["new_notice", "runtime_identity"])
def test_native_expiry_cannot_clear_a_replacement_notice_or_runtime(tmp_path, replacement):
    with registered_status_terminal(tmp_path) as (terminal, pipeline, capability):
        token = pipeline.publish_timed(
            publisher="fixture",
            priority=StatusPriority.COMMAND_POLICY,
            presentation=TmuxStatusPresentation.message("old notice"),
            duration=0.2,
            capability=capability,
        )
        assert token
        if replacement == "new_notice":
            assert pipeline.publish_transient(
                publisher="replacement",
                token="new-token",
                priority=StatusPriority.SAFETY_WARNING,
                presentation=TmuxStatusPresentation.message("new notice"),
            )
            token = "new-token"
        else:
            terminal.tmux("set-option", "-t", "%0", "@rodex_runtime_id", "4" * 16)
        time.sleep(0.4)
        assert terminal.tmux("show-options", "-v", "-t", "%0", STATUS_CLAIM_TOKEN_OPTION).strip() == token
