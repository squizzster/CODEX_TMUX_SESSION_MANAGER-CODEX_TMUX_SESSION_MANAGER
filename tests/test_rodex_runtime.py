from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

import rodex.runtime as runtime_module
from rodex.runtime import (
    LiveRodexRuntime,
    LiveTmuxSession,
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
                    "version": "0.4.0",
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
    host_command = new_session[-1]
    assert "/venv/bin/python -m rodex.session_host" in host_command
    assert f"--protocol-event-socket {tmp_path / 'events-0123456789abcdef.sock'}" in (
        host_command
    )
    assert "--model example" in host_command
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
    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
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
    assert [command[3:] for command in runner.calls[1:]] == [
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
    ]


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
        renamed = launcher.rename(original, "automatic-beluga")
        launcher.configure_identity_status(renamed)

        assert launcher.session_exists(renamed)
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
    finally:
        subprocess.run(
            [tmux_binary, "-S", str(socket_path), "kill-server"],
            check=False,
            text=True,
            capture_output=True,
        )


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


def test_session_host_connects_the_tui_through_the_protocol_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_socket = tmp_path / "app.sock"
    proxy_socket = tmp_path / "proxy.sock"
    event_socket = tmp_path / "events.sock"
    tmux_socket = tmp_path / "tmux.sock"
    tui_commands: list[list[str]] = []
    status_updates: list[int] = []
    proxy_lifecycle: list[str] = []

    class FakeProcess:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: int) -> int:
            return 0

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

    def run_tui(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        tui_commands.append(command)
        assert options == {"check": False}
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(
        runtime_module.subprocess, "Popen", lambda *args, **kwargs: FakeProcess()
    )
    monkeypatch.setattr(runtime_module.subprocess, "run", run_tui)
    monkeypatch.setattr(runtime_module, "_wait_for_app_server_socket", lambda *args: None)
    monkeypatch.setattr(runtime_module, "TmuxToolCallStatus", FakeStatus)
    monkeypatch.setattr(runtime_module, "CodexProtocolEventTap", FakeEventTap)
    monkeypatch.setattr(runtime_module, "CodexProtocolProxy", FakeProxy)

    assert (
        run_session_host(
            "/usr/bin/codex",
            app_socket,
            tmp_path / "app.log",
            proxy_socket,
            event_socket,
            "/usr/bin/tmux",
            tmux_socket,
            ["resume", "codex-uuid"],
        )
        == 0
    )

    assert status_updates == [0]
    assert proxy_lifecycle == ["event-start", "start", "close", "event-close"]
    assert tui_commands == [
        [
            "/usr/bin/codex",
            "--remote",
            f"unix://{proxy_socket}",
            "resume",
            "codex-uuid",
        ]
    ]
