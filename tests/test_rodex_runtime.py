from __future__ import annotations

import json
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
    assert "--model example" in host_command
    assert "send-keys" not in host_command


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
    )

    launcher.attach(live)

    assert runner.calls[-1][-3:] == ["attach-session", "-t", "rodex-one"]
    assert "capture_output" not in runner.options[-1]
    environment = runner.options[-1]["env"]
    assert isinstance(environment, dict)
    assert "TMUX" not in environment


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
        "rodex-token",
        "automatic-beluga",
    ]
    assert [command[3:] for command in runner.calls[1:]] == [
        ["set-option", "-t", "automatic-beluga", "status", "on"],
        [
            "set-option",
            "-t",
            "automatic-beluga",
            "status-left",
            "#[fg=green,bold] Rodex: #S #[default]",
        ],
        ["set-option", "-t", "automatic-beluga", "status-left-length", "48"],
    ]


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
        "rodex-0123456789abcdef",
    ]


def test_configured_runtime_path_must_fit_a_unix_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_root = tmp_path / ("x" * 100)
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(long_root))

    launcher = RodexRuntimeLauncher("codex", "tmux")
    with pytest.raises(RodexRuntimeError, match="too long"):
        launcher.start(tmp_path, [])
