from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from threading import Event, Lock
from typing import BinaryIO, cast

import pytest

import rodex.runtime as runtime_module
from rodex.analytics import AnalyticsWorkerConfig
from rodex.runtime import (
    RODEX_TMUX_HISTORY_LIMIT_LINES,
    LiveRodexRuntime,
    LiveTmuxSession,
    RodexCodexSessionNotFoundError,
    RodexRuntimeError,
    RodexRuntimeLauncher,
    run_session_host,
)


class FakeWebSocket:
    def __init__(self, loaded: list[str]) -> None:
        self.sent: list[dict[str, object]] = []
        self.responses = iter(
            [
                {"id": 0, "result": {"platformOs": "linux"}},
                {"method": "remoteControl/status/changed", "params": {}},
                {"id": 1, "result": {"data": loaded, "nextCursor": None}},
            ]
        )

    def __enter__(self) -> FakeWebSocket:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def recv(self, timeout: float) -> str:
        assert timeout > 0
        return json.dumps(next(self.responses))


class RecordingConnector:
    def __init__(self, loaded: list[str]) -> None:
        self.websocket = FakeWebSocket(loaded)
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> FakeWebSocket:
        self.calls.append((args, kwargs))
        return self.websocket


class RuntimeRunner:
    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self.calls: list[list[str]] = []
        self.options: list[dict[str, object]] = []

    def __call__(
        self, command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        self.options.append(options)
        if "new-session" in command:
            (self.runtime_root / "app-0123456789abcdef.sock").touch()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_observer_uses_distinct_codex_identity_fields_and_no_compression(
    tmp_path: Path,
) -> None:
    codex_uuid = "01a00654-f2bc-7a30-834a-a5f886a65f82"
    connector = RecordingConnector([codex_uuid])
    launcher = RodexRuntimeLauncher("codex", "tmux", connector=connector)

    assert launcher._list_loaded_codex_threads(tmp_path / "app.sock") == [codex_uuid]

    _, options = connector.calls[0]
    assert options["compression"] is None
    assert connector.websocket.sent == [
        {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "rodex",
                    "title": "Rodex",
                    "version": "0.5.0a1",
                }
            },
        },
        {"method": "initialized", "params": {}},
        {"method": "thread/loaded/list", "id": 1, "params": {}},
    ]


def test_start_directly_hosts_codex_in_tmux_and_returns_its_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_uuid = "01a00654-f2bc-7a30-834a-a5f886a65f82"
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        runtime_module.secrets, "token_hex", lambda size: "0123456789abcdef"
    )
    runner = RuntimeRunner(tmp_path)
    connector = RecordingConnector([codex_uuid])
    launcher = RodexRuntimeLauncher(
        "/usr/bin/codex",
        "/usr/bin/tmux",
        runner=runner,
        connector=connector,
        python_executable="/venv/bin/python",
    )

    live, observed_uuid = launcher.start(tmp_path, ["--model", "example"])

    assert observed_uuid == uuid.UUID(codex_uuid)
    assert live.tmux_session_name == "rodex-0123456789abcdef"
    new_session = runner.calls[0]
    assert new_session[:3] == [
        "/usr/bin/tmux",
        "-S",
        str(tmp_path / "tmux.sock"),
    ]
    assert new_session[3:8] == [
        "set-option",
        "-g",
        "history-limit",
        str(RODEX_TMUX_HISTORY_LIMIT_LINES),
        ";",
    ]
    assert new_session[8:12] == [
        "new-session",
        "-d",
        "-s",
        "rodex-0123456789abcdef",
    ]
    host_command = new_session[-1]
    assert "/venv/bin/python -m rodex.session_host" in host_command
    assert f"--protocol-event-socket {tmp_path / 'events-0123456789abcdef.sock'}" in (
        host_command
    )
    assert "--model example" in host_command
    assert "--rodex-session-uuid" not in host_command
    assert "--rodex-database" not in host_command
    assert "--codex-sessions-root" not in host_command
    assert "send-keys" not in host_command
    assert [command[3:] for command in runner.calls[-3:]] == [
        [
            "set-option",
            "-t",
            "=rodex-0123456789abcdef:",
            "@rodex_protocol_proxy_socket_path",
            str(tmp_path / "proxy-0123456789abcdef.sock"),
        ],
        [
            "set-option",
            "-t",
            "=rodex-0123456789abcdef:",
            "@rodex_protocol_event_socket_path",
            str(tmp_path / "events-0123456789abcdef.sock"),
        ],
        [
            "set-option",
            "-t",
            "=rodex-0123456789abcdef:",
            "@rodex_codex_session_uuid",
            codex_uuid,
        ],
    ]


def test_exact_resume_fails_when_codex_reports_that_uuid_is_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_uuid = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        runtime_module.secrets, "token_hex", lambda size: "0123456789abcdef"
    )
    calls: list[list[str]] = []

    def exited_resume_runner(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "new-session" in command:
            (tmp_path / "app-0123456789abcdef.log").write_text(
                f"ERROR: No saved session found with ID {codex_uuid}. "
                "Run `codex resume` without an ID to choose from existing sessions.\n"
            )
        return subprocess.CompletedProcess(
            command,
            1 if "has-session" in command else 0,
            stdout="",
            stderr="",
        )

    launcher = RodexRuntimeLauncher(
        "/usr/bin/codex",
        "/usr/bin/tmux",
        runner=exited_resume_runner,
        python_executable="/venv/bin/python",
    )

    with pytest.raises(RodexCodexSessionNotFoundError) as raised:
        launcher.start(tmp_path, ["resume", str(codex_uuid)])

    assert str(raised.value) == (
        f"Codex has no saved session for exact identity {codex_uuid}"
    )
    assert any("kill-session" in command for command in calls)


def test_exact_resume_rejects_a_different_loaded_uuid_before_publishing_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested_uuid = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
    observed_uuid = uuid.UUID(int=requested_uuid.int + 1)
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        runtime_module.secrets, "token_hex", lambda size: "0123456789abcdef"
    )
    runner = RuntimeRunner(tmp_path)
    connector = RecordingConnector([str(observed_uuid)])
    launcher = RodexRuntimeLauncher(
        "/usr/bin/codex",
        "/usr/bin/tmux",
        runner=runner,
        connector=connector,
        python_executable="/venv/bin/python",
    )

    with pytest.raises(RodexRuntimeError) as raised:
        launcher.start(tmp_path, ["resume", str(requested_uuid)])

    assert str(raised.value) == (
        "Codex resumed an unexpected exact identity: "
        f"requested {requested_uuid}, observed {observed_uuid}"
    )
    tmux_operations = [command[3] for command in runner.calls]
    assert tmux_operations == ["set-option", "has-session", "kill-session"]
    assert "new-session" in runner.calls[0]


def test_attach_uses_live_stdio_and_escapes_an_existing_tmux_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMUX", "/tmp/outer")
    runner = RuntimeRunner(tmp_path)
    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    live = LiveRodexRuntime(
        tmp_path / "tmux.sock",
        "rodex-one",
        tmp_path / "app.sock",
        tmp_path / "app.log",
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
    )

    launcher.attach(live)

    assert runner.calls[-1][-3:] == ["attach-session", "-t", "=rodex-one"]
    assert "capture_output" not in runner.options[-1]
    environment = runner.options[-1]["env"]
    assert isinstance(environment, dict)
    assert "TMUX" not in environment


def test_session_exists_checks_the_exact_recorded_tmux_endpoint(tmp_path: Path) -> None:
    runner = RuntimeRunner(tmp_path)
    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    live = LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga")

    assert launcher.session_exists(live)

    assert runner.calls[-1] == [
        "tmux",
        "-S",
        str(tmp_path / "tmux.sock"),
        "has-session",
        "-t",
        "=automatic-beluga",
    ]
    assert runner.options[-1]["check"] is False


def test_rename_and_status_configuration_use_the_real_tmux_session_name(
    tmp_path: Path,
) -> None:
    runner = RuntimeRunner(tmp_path)
    launcher = RodexRuntimeLauncher(
        "codex", "tmux", runner=runner, python_executable="/venv/bin/python"
    )
    original = LiveTmuxSession(tmp_path / "tmux.sock", "rodex-token")

    renamed = launcher.rename(original, "automatic-beluga")
    launcher.configure_identity_status(renamed)

    assert renamed == LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga")
    assert runner.calls[0][-4:] == [
        "rename-session",
        "-t",
        "=rodex-token",
        "automatic-beluga",
    ]
    status_commands = [command[3:] for command in runner.calls[1:]]
    assert status_commands[0:9] == [
        [
            "set-option",
            "-u",
            "-t",
            "=automatic-beluga:",
            "@rodex_status_animation_token",
        ],
        [
            "set-option",
            "-u",
            "-t",
            "=automatic-beluga:",
            "@rodex_completion_token",
        ],
        ["set-option", "-u", "-t", "=automatic-beluga:", "status-format"],
        ["set-option", "-u", "-t", "=automatic-beluga:", "status-style"],
        ["set-option", "-t", "=automatic-beluga:", "status", "on"],
        [
            "set-option",
            "-t",
            "=automatic-beluga:",
            "status-left",
            "#[fg=green,bold] Rodex: #S #[fg=cyan,bold]| Tools: "
            "#{@rodex_tool_calls} #[default]",
        ],
        ["set-option", "-t", "=automatic-beluga:", "status-left-length", "68"],
        [
            "set-option",
            "-t",
            "=automatic-beluga:",
            "status-right",
            "#{?session_many_attached,"
            "#[fg=yellow]#[bold] [Shared with #{e|-:#{session_attached},1} "
            "#{?#{==:#{session_attached},2},other,others}] #[default],"
            "#[fg=green]#[bold] [Private session] #[default]}"
            " | %H:%M %d-%b-%y",
        ],
        ["set-option", "-t", "=automatic-beluga:", "status-right-length", "64"],
    ]
    assert len(status_commands) == 14
    for event, hook_command in zip(
        ("attached", "detached"), status_commands[9:11], strict=True
    ):
        assert hook_command[:4] == [
            "set-hook",
            "-t",
            "=automatic-beluga:",
            f"client-{event}",
        ]
        assert hook_command[4].startswith("run-shell -b ")
        assert "/venv/bin/python -m rodex.status_animation" in hook_command[4]
        assert f"--event {event}" in hook_command[4]
        assert hook_command[4].endswith(">/dev/null 2>&1'")
    assert status_commands[11] == ["pipe-pane", "-t", "=automatic-beluga:"]
    assert status_commands[12:14] == [
        ["list-keys", "-T", "root", "Enter"],
        ["list-keys", "-T", "root", "Tab"],
    ]


def test_tmux_slash_switch_reinstalls_retained_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "RODEX_TMUX_SLASH_ENABLED", True)
    runner = RuntimeRunner(tmp_path)
    launcher = RodexRuntimeLauncher(
        "codex", "tmux", runner=runner, python_executable="/venv/bin/python"
    )
    runtime = LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga")

    launcher.configure_identity_status(runtime)

    status_commands = [command[3:] for command in runner.calls]
    completion_pipe = status_commands[11]
    assert completion_pipe[:4] == [
        "pipe-pane",
        "-O",
        "-t",
        "=automatic-beluga:",
    ]
    assert "/venv/bin/python -m rodex.tmux_completion_observer" in completion_pipe[4]
    for key, input_binding in zip(("Enter", "Tab"), status_commands[12:14], strict=True):
        assert input_binding[:3] == ["bind-key", "-n", key]
        assert "/venv/bin/python -m rodex.tmux_input_proxy" in input_binding[3]
        assert f"--key {key}" in input_binding[3]


def test_rodex_does_not_override_user_mouse_preferences(tmp_path: Path) -> None:
    runner = RuntimeRunner(tmp_path)
    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    runtime = LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga")

    launcher._start_tmux_session(runtime, tmp_path, "sleep 30")
    launcher.configure_identity_status(runtime)

    assert not any("mouse" in command for command in runner.calls)


def test_real_tmux_session_preserves_scrollback_with_mouse_disabled(
    tmp_path: Path,
) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"
    runtime = LiveTmuxSession(socket_path, "rodex-scrollback")
    output_script = tmp_path / "emit-scrollback.sh"
    output_script.write_text(
        "#!/bin/sh\nseq 1 300\nsleep 30\n",
        encoding="utf-8",
    )
    output_script.chmod(0o755)
    launcher = RodexRuntimeLauncher("codex", tmux_binary)

    launcher._start_tmux_session(
        runtime,
        tmp_path,
        shlex.join([str(output_script)]),
    )
    try:
        deadline = time.monotonic() + 2
        history_size = 0
        while time.monotonic() < deadline:
            pane_state = subprocess.run(
                [
                    tmux_binary,
                    "-S",
                    str(socket_path),
                    "display-message",
                    "-p",
                    "-t",
                    "=rodex-scrollback:",
                    "-F",
                    "#{history_size} #{history_limit} #{mouse}",
                ],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.split()
            history_size = int(pane_state[0])
            if history_size >= 278:
                break
            time.sleep(0.01)

        assert history_size >= 278
        assert pane_state[1:] == [str(RODEX_TMUX_HISTORY_LIMIT_LINES), "0"]
        captured = subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "capture-pane",
                "-p",
                "-S",
                "-",
                "-t",
                "=rodex-scrollback:",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
        assert captured[0] == "1"
        assert "300" in captured
    finally:
        launcher.stop(runtime, check=False)


def test_real_tmux_survives_rename_and_status_configuration(tmp_path: Path) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"
    original = LiveRodexRuntime(
        socket_path,
        "rodex-integration-token",
        tmp_path / "app.sock",
        tmp_path / "app.log",
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
    )
    launcher = RodexRuntimeLauncher("codex", tmux_binary)
    codex_uuid = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
    control_clients: list[subprocess.Popen[str]] = []

    def tmux_format(format_string: str) -> str:
        return subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "display-message",
                "-p",
                "-t",
                "=automatic-beluga:",
                "-F",
                format_string,
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def wait_for_attached_clients(expected: int) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if tmux_format("#{session_attached}") == str(expected):
                return
            time.sleep(0.01)
        pytest.fail(f"tmux did not report {expected} attached clients")

    def session_option(option_name: str, *, inherited: bool = False) -> str:
        command = [
            tmux_binary,
            "-S",
            str(socket_path),
            "show-options",
        ]
        if inherited:
            command.append("-A")
        command.extend(("-v", "-t", "=automatic-beluga:", option_name))
        return subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def wait_for_session_option(option_name: str, *, populated: bool) -> str:
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            value = session_option(option_name)
            if bool(value) is populated:
                return value
            time.sleep(0.01)
        expected = "populated" if populated else "unset"
        pytest.fail(f"tmux option {option_name} did not become {expected}")

    def wait_for_session_option_text(option_name: str, expected_text: str) -> str:
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            value = session_option(option_name)
            if expected_text in value:
                return value
            time.sleep(0.01)
        pytest.fail(f"tmux option {option_name} did not contain {expected_text!r}")

    subprocess.run(
        [
            tmux_binary,
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            original.tmux_session_name,
            "sleep 30",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    try:
        launcher.publish_runtime_control(original, codex_uuid)
        subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "set-option",
                "-g",
                "mouse",
                "on",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        renamed = launcher.rename(original, "automatic-beluga")
        launcher.configure_identity_status(renamed)

        assert launcher.session_exists(renamed)
        assert session_option("mouse") == ""
        assert session_option("mouse", inherited=True) == "on"
        assert tmux_format("#{pane_pipe}") == "0"
        tab_binding = subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "list-keys",
                "-T",
                "root",
                "Tab",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
        assert tab_binding.returncode != 0
        assert "rodex.tmux_input_proxy" not in tab_binding.stdout
        discovered = launcher.discover_runtime_control(renamed)
        assert discovered.protocol_proxy_socket_path == tmp_path / "proxy.sock"
        assert discovered.protocol_event_socket_path == tmp_path / "events.sock"
        assert discovered.codex_session_uuid == codex_uuid
        shown_status = subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "show-options",
                "-v",
                "-t",
                "=automatic-beluga:",
                "status-left",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        assert "Rodex: #S" in shown_status.stdout
        shown_sharing_status = subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "show-options",
                "-v",
                "-t",
                "=automatic-beluga:",
                "status-right",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        assert "[Shared with" in shown_sharing_status.stdout
        assert "[Private session]" in shown_sharing_status.stdout
        assert "session_attached" in shown_sharing_status.stdout
        assert "[Private session]" in tmux_format("#{E:status-right}")

        for expected, indicator in (
            (1, "[Private session]"),
            (2, "[Shared with 1 other]"),
            (3, "[Shared with 2 others]"),
        ):
            control_clients.append(
                subprocess.Popen(
                    [
                        tmux_binary,
                        "-S",
                        str(socket_path),
                        "-C",
                        "attach-session",
                        "-t",
                        "=automatic-beluga",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            wait_for_attached_clients(expected)
            assert indicator in tmux_format("#{E:status-right}")

        rendered_status = tmux_format("#{E:status-right}")
        assert "%H" not in rendered_status
        animated_status = wait_for_session_option_text(
            "status-format[0]", "SHARED WITH 2 OTHERS"
        )
        assert "#[align=centre]" in animated_status
        wait_for_session_option("status-format[0]", populated=False)
        assert session_option("status-style") == ""
        assert "#{status-left-style}" in session_option("status-format[0]", inherited=True)

        departed_while_shared = control_clients.pop()
        departed_while_shared.communicate("detach-client\n", timeout=2)
        wait_for_attached_clients(2)
        final_departure = control_clients.pop()
        final_departure.communicate("detach-client\n", timeout=2)
        wait_for_attached_clients(1)

        private_animation = wait_for_session_option_text(
            "status-format[0]", "PRIVATE SESSION"
        )
        assert "#[align=centre]" in private_animation
        wait_for_session_option("status-format[0]", populated=False)
        assert session_option("status-style") == ""
        assert "Rodex: automatic-beluga" in tmux_format("#{T:status-left}")
        assert "[Private session]" in tmux_format("#{E:status-right}")
    finally:
        subprocess.run(
            [tmux_binary, "-S", str(socket_path), "kill-server"],
            check=False,
            text=True,
            capture_output=True,
        )
        for client in control_clients:
            client.communicate(timeout=2)


def test_more_than_one_loaded_codex_thread_aborts_the_exact_tmux_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        runtime_module.secrets, "token_hex", lambda size: "0123456789abcdef"
    )
    runner = RuntimeRunner(tmp_path)
    connector = RecordingConnector(
        [
            "01a00654-f2bc-7a30-834a-a5f886a65f82",
            "01a00656-4c5e-7eb0-ae87-4765725b6f00",
        ]
    )
    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner, connector=connector)

    with pytest.raises(RodexRuntimeError, match="more than one"):
        launcher.start(tmp_path, [])

    assert runner.calls[-1][-3:] == [
        "kill-session",
        "-t",
        "=rodex-0123456789abcdef",
    ]


def test_configured_runtime_path_must_fit_a_unix_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_root = tmp_path / ("x" * 100)
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(long_root))

    launcher = RodexRuntimeLauncher("codex", "tmux")
    with pytest.raises(RodexRuntimeError, match="too long"):
        launcher.start(tmp_path, [])


def test_runtime_path_keepalive_refreshes_a_bound_socket_and_runtime_files(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    socket_path = runtime_root / "live.sock"
    log_path = runtime_root / "live.log"
    log_path.touch()
    old_timestamp = 946684800

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(socket_path))
        os.utime(runtime_root, (old_timestamp, old_timestamp))
        os.utime(socket_path, (old_timestamp, old_timestamp))
        os.utime(log_path, (old_timestamp, old_timestamp))
        keepalive = runtime_module._RuntimePathKeepalive(
            (runtime_root, socket_path, log_path)
        )

        keepalive.start()
        keepalive.close()

    assert runtime_root.stat().st_mtime > old_timestamp
    assert socket_path.stat().st_mtime > old_timestamp
    assert log_path.stat().st_mtime > old_timestamp
    assert keepalive.failure is None


def test_runtime_path_keepalive_does_not_recreate_a_missing_socket(
    tmp_path: Path,
) -> None:
    missing_socket = tmp_path / "missing.sock"
    keepalive = runtime_module._RuntimePathKeepalive((missing_socket,))

    with pytest.raises(RodexRuntimeError, match=str(missing_socket)):
        keepalive.start()

    assert not missing_socket.exists()


def test_runtime_path_keepalive_rejects_path_substitution(tmp_path: Path) -> None:
    runtime_path = tmp_path / "live.sock"
    runtime_path.touch()
    keepalive = runtime_module._RuntimePathKeepalive((runtime_path,))
    keepalive.start()
    keepalive.close()
    runtime_path.unlink()
    runtime_path.touch()

    with pytest.raises(RodexRuntimeError, match="identity changed"):
        keepalive._refresh()


def test_runtime_root_rejects_a_precreated_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(RodexRuntimeError, match="not a real directory"):
        runtime_module._prepare_runtime_root(runtime_root)


def test_runtime_root_rejects_a_group_writable_parent(tmp_path: Path) -> None:
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o770)

    with pytest.raises(RodexRuntimeError, match="not private"):
        runtime_module._prepare_runtime_root(parent / "runtime")


def test_private_runtime_log_repairs_mode_and_rejects_symlinks(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "app.log"
    log_path.touch(mode=0o644)
    with runtime_module._open_private_runtime_log(log_path) as log:
        assert os.fstat(log.fileno()).st_mode & 0o777 == 0o600

    target = tmp_path / "target.log"
    target.touch()
    log_path.unlink()
    log_path.symlink_to(target)
    with pytest.raises(OSError):
        runtime_module._open_private_runtime_log(log_path)


def test_runtime_control_publishes_pending_then_registered_identity(
    tmp_path: Path,
) -> None:
    runner = RuntimeRunner(tmp_path)
    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    runtime = LiveRodexRuntime(
        tmp_path / "tmux.sock",
        "rodex-token",
        tmp_path / "app.sock",
        tmp_path / "app.log",
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
    )
    codex_uuid = uuid.uuid4()
    rodex_uuid = uuid.uuid4()
    registry_uuid = uuid.uuid4()

    launcher.publish_runtime_control(runtime, codex_uuid, rodex_uuid, registry_uuid)
    launcher.confirm_runtime_registration(runtime)

    options = [command[3:] for command in runner.calls]
    assert options[-4:] == [
        [
            "set-option",
            "-t",
            "=rodex-token:",
            "@rodex_session_uuid",
            str(rodex_uuid),
        ],
        [
            "set-option",
            "-t",
            "=rodex-token:",
            "@rodex_registry_uuid",
            str(registry_uuid),
        ],
        [
            "set-option",
            "-t",
            "=rodex-token:",
            "@rodex_registration_state",
            "pending",
        ],
        [
            "set-option",
            "-t",
            "=rodex-token:",
            "@rodex_registration_state",
            "registered",
        ],
    ]


def test_runtime_registration_check_uses_the_exact_pane_and_private_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def run_tmux(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="registered\n", stderr="")

    monkeypatch.setattr(runtime_module.subprocess, "run", run_tmux)

    assert runtime_module._runtime_registration_is_confirmed(
        "/usr/bin/tmux", tmp_path / "tmux.sock", "%4"
    )
    assert observed == [
        [
            "/usr/bin/tmux",
            "-S",
            str(tmp_path / "tmux.sock"),
            "show-options",
            "-v",
            "-t",
            "%4",
            "@rodex_registration_state",
        ]
    ]


def test_runtime_path_keepalive_reports_a_periodic_refresh_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_path = tmp_path / "live.sock"
    runtime_path.touch()
    real_utime = os.utime
    refresh_calls = 0

    def fail_after_the_initial_refresh(
        path: Path, times: object, *, follow_symlinks: bool
    ) -> None:
        nonlocal refresh_calls
        assert path == runtime_path
        assert times is None
        assert follow_symlinks is False
        refresh_calls += 1
        if refresh_calls > 1:
            raise FileNotFoundError(2, "missing live runtime path", path)
        real_utime(path, times, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(runtime_module.os, "utime", fail_after_the_initial_refresh)
    keepalive = runtime_module._RuntimePathKeepalive(
        (runtime_path,),
        interval_seconds=0.01,
    )

    keepalive.start()
    failure = keepalive.wait_for_failure(timeout=1)
    keepalive.close()

    assert refresh_calls == 2
    assert failure is keepalive.failure
    assert str(runtime_path) in str(failure)


def test_runtime_path_keepalives_share_runtime_paths_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    tmux_socket = runtime_root / "tmux.sock"
    private_a = runtime_root / "app-a.sock"
    private_b = runtime_root / "app-b.sock"
    for path in (tmux_socket, private_a, private_b):
        path.touch()

    real_utime = os.utime
    refresh_counts: dict[Path, int] = {}
    refresh_counts_lock = Lock()
    private_b_refreshed = Event()
    private_b_target = 2

    def record_refresh(path: Path, times: object, *, follow_symlinks: bool) -> None:
        nonlocal private_b_target
        real_utime(path, times, follow_symlinks=follow_symlinks)
        with refresh_counts_lock:
            refresh_counts[path] = refresh_counts.get(path, 0) + 1
            if path == private_b and refresh_counts[path] >= private_b_target:
                private_b_refreshed.set()

    monkeypatch.setattr(runtime_module.os, "utime", record_refresh)
    keepalive_a = runtime_module._RuntimePathKeepalive(
        (runtime_root, tmux_socket, private_a), interval_seconds=0.01
    )
    keepalive_b = runtime_module._RuntimePathKeepalive(
        (runtime_root, tmux_socket, private_b), interval_seconds=0.01
    )

    keepalive_a.start()
    keepalive_b.start()
    try:
        assert private_b_refreshed.wait(timeout=1)
        keepalive_a.close()
        with refresh_counts_lock:
            private_a_count_after_close = refresh_counts[private_a]
            private_b_target = refresh_counts[private_b] + 2
            private_b_refreshed.clear()
        assert private_b_refreshed.wait(timeout=1)
    finally:
        keepalive_a.close()
        keepalive_b.close()

    assert refresh_counts[private_a] == private_a_count_after_close
    assert refresh_counts[private_b] >= private_b_target
    assert refresh_counts[runtime_root] > refresh_counts[private_a]
    assert refresh_counts[tmux_socket] > refresh_counts[private_a]


@pytest.mark.parametrize(
    ("codex_arguments", "captures_stderr"),
    [
        (["resume", "01a00654-f2bc-7a30-834a-a5f886a65f82"], True),
        (["--model", "example"], False),
    ],
)
def test_session_host_connects_the_tui_through_the_protocol_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codex_arguments: list[str],
    captures_stderr: bool,
) -> None:
    app_socket = tmp_path / "app.sock"
    proxy_socket = tmp_path / "proxy.sock"
    event_socket = tmp_path / "events.sock"
    tmux_socket = tmp_path / "tmux.sock"
    tui_commands: list[list[str]] = []
    tui_options: list[dict[str, object]] = []
    status_updates: list[int] = []
    proxy_lifecycle: list[str] = []
    protected_paths: tuple[Path, ...] | None = None

    class FakeProcess:
        def __init__(self, command: list[str]) -> None:
            self.command = command
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: int | None = None) -> int:
            self.returncode = 0
            return 0

        def kill(self) -> None:
            self.returncode = -9

    class FakeStatus:
        def __init__(self, *args: object) -> None:
            assert args == ("/usr/bin/tmux", tmux_socket, "%4")

        def update(self, count: int) -> None:
            status_updates.append(count)

    class FakeProxy:
        def __init__(self, *args: object) -> None:
            assert args[:2] == (proxy_socket, app_socket)

        def start(self) -> None:
            proxy_lifecycle.append("start")

        def close(self) -> None:
            proxy_lifecycle.append("close")

    class FakeEventTap:
        def __init__(self, path: Path) -> None:
            assert path == event_socket

        def start(self) -> None:
            proxy_lifecycle.append("event-start")

        def publish(self, _message: str | bytes) -> None:
            return None

        def close(self) -> None:
            proxy_lifecycle.append("event-close")

    class FakeKeepalive:
        failure: RodexRuntimeError | None = None

        def __init__(
            self,
            paths: tuple[Path, ...],
        ) -> None:
            nonlocal protected_paths
            protected_paths = paths
            self.closed = False

        def start(self) -> None:
            proxy_lifecycle.append("keepalive-start")

        def close(self) -> None:
            if not self.closed:
                proxy_lifecycle.append("keepalive-close")
                self.closed = True

    class FailingAnalyticsSupervisor:
        def __init__(self, config: AnalyticsWorkerConfig) -> None:
            assert config.rodex_database_path == tmp_path / "rodex.sqlite3"

        def poll(self) -> None:
            proxy_lifecycle.append("analytics-poll-failed")
            raise OSError("analytics unavailable")

        def close(self) -> None:
            raise AssertionError("failed analytics supervisor was released")

    def start_process(command: list[str], **options: object) -> FakeProcess:
        if "app-server" not in command:
            tui_commands.append(command)
            tui_options.append(options)
        return FakeProcess(command)

    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(runtime_module.subprocess, "Popen", start_process)
    monkeypatch.setattr(
        runtime_module,
        "_wait_for_app_server_socket",
        lambda *_args: app_socket.touch(),
    )
    monkeypatch.setattr(runtime_module, "TmuxToolCallStatus", FakeStatus)
    monkeypatch.setattr(runtime_module, "CodexProtocolEventTap", FakeEventTap)
    monkeypatch.setattr(runtime_module, "CodexProtocolProxy", FakeProxy)
    monkeypatch.setattr(runtime_module, "_RuntimePathKeepalive", FakeKeepalive)

    def record_signal_change(_signum: int, handler: object) -> object:
        proxy_lifecycle.append("signal-install" if callable(handler) else "signal-restore")
        return runtime_module.signal.SIG_DFL

    monkeypatch.setattr(runtime_module.signal, "signal", record_signal_change)

    assert (
        run_session_host(
            "/usr/bin/codex",
            app_socket,
            tmp_path / "app.log",
            proxy_socket,
            event_socket,
            "/usr/bin/tmux",
            tmux_socket,
            codex_arguments,
            analytics_config=AnalyticsWorkerConfig(
                rodex_database_path=tmp_path / "rodex.sqlite3",
                codex_sessions_root=tmp_path / "sessions",
                rodex_uuid=uuid.UUID(int=1),
            ),
            analytics_supervisor_factory=FailingAnalyticsSupervisor,
        )
        == 0
    )

    assert status_updates == [0]
    assert proxy_lifecycle == [
        "signal-install",
        "signal-install",
        "event-start",
        "start",
        "keepalive-start",
        "analytics-poll-failed",
        "keepalive-close",
        "close",
        "event-close",
        "signal-restore",
        "signal-restore",
    ]
    assert protected_paths == (
        tmp_path,
        tmux_socket,
        app_socket,
        tmp_path / "app.log",
        proxy_socket,
        event_socket,
    )
    assert tui_commands == [
        [
            "/usr/bin/codex",
            "--no-alt-screen",
            "--remote",
            f"unix://{proxy_socket}",
            *codex_arguments,
        ]
    ]
    assert len(tui_options) == 1
    if captures_stderr:
        captured_stderr = tui_options[0].get("stderr")
        assert captured_stderr is not None
        assert cast(BinaryIO, captured_stderr).closed
        assert not (tmp_path / "app.log").exists()
    else:
        assert tui_options == [{}]


def test_session_host_terminates_the_tui_when_runtime_keepalive_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle: list[str] = []
    keepalives: list[FailingKeepalive] = []

    class FakeProcess:
        def __init__(self, command: list[str]) -> None:
            self.command = command
            self.kind = "app" if "app-server" in command else "tui"
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            lifecycle.append(f"{self.kind}-terminate")
            if self.kind == "app":
                self.returncode = -15

        def wait(self, timeout: int | None = None) -> int:
            if self.kind == "tui" and keepalives[0].failure is None:
                keepalives[0].fail()
                raise subprocess.TimeoutExpired(self.command, timeout)
            if self.kind == "tui" and self.returncode is None:
                raise subprocess.TimeoutExpired(self.command, timeout)
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            lifecycle.append(f"{self.kind}-kill")
            self.returncode = -9

    class FakeStatus:
        def __init__(self, *args: object) -> None:
            return None

        def update(self, count: int) -> None:
            assert count == 0

    class FakeProxy:
        def __init__(self, *args: object) -> None:
            return None

        def start(self) -> None:
            return None

        def close(self) -> None:
            lifecycle.append("proxy-close")

    class FakeEventTap:
        def __init__(self, path: Path) -> None:
            return None

        def start(self) -> None:
            return None

        def publish(self, _message: str | bytes) -> None:
            return None

        def close(self) -> None:
            lifecycle.append("event-close")

    class FailingKeepalive:
        def __init__(
            self,
            paths: tuple[Path, ...],
        ) -> None:
            assert paths
            self.failure: RodexRuntimeError | None = None
            keepalives.append(self)

        def start(self) -> None:
            return None

        def close(self) -> None:
            return None

        def fail(self) -> None:
            self.failure = RodexRuntimeError("runtime keepalive lost proxy.sock")

    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(
        runtime_module.subprocess,
        "Popen",
        lambda command, **options: FakeProcess(command),
    )
    monkeypatch.setattr(
        runtime_module,
        "_wait_for_app_server_socket",
        lambda *_args: (tmp_path / "app.sock").touch(),
    )
    monkeypatch.setattr(runtime_module, "TmuxToolCallStatus", FakeStatus)
    monkeypatch.setattr(runtime_module, "CodexProtocolEventTap", FakeEventTap)
    monkeypatch.setattr(runtime_module, "CodexProtocolProxy", FakeProxy)
    monkeypatch.setattr(runtime_module, "_RuntimePathKeepalive", FailingKeepalive)

    with pytest.raises(RodexRuntimeError, match=r"lost proxy\.sock"):
        run_session_host(
            "/usr/bin/codex",
            tmp_path / "app.sock",
            tmp_path / "app.log",
            tmp_path / "proxy.sock",
            tmp_path / "events.sock",
            "/usr/bin/tmux",
            tmp_path / "tmux.sock",
            [],
        )

    assert lifecycle == [
        "tui-terminate",
        "tui-kill",
        "proxy-close",
        "event-close",
        "app-terminate",
    ]
    assert "runtime keepalive lost proxy.sock" in (tmp_path / "app.log").read_text(
        encoding="utf-8"
    )
