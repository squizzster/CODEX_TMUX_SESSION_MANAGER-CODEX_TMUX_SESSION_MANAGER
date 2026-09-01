from __future__ import annotations

import json
import os
import pty
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock, Thread
from typing import BinaryIO, cast

import pytest

import rodex.runtime as runtime_module
from rodex.control import LiveRodexControl
from rodex.process_contracts import AnalyticsWorkerConfig, SessionHostConfig
from rodex.runtime import (
    RODEX_TMUX_HISTORY_LIMIT_LINES,
    RODEX_TMUX_REQUIRED_CLIENT_FEATURES,
    CurrentTmuxPaneContext,
    LiveRodexRuntime,
    LiveTmuxSession,
    RodexCodexSessionNotFoundError,
    RodexRuntimeError,
    RodexRuntimeLauncher,
    TmuxScrollbackSnapshot,
    TmuxScrollbackState,
    run_session_host,
)
from rodex.tmux_session_capability import (
    RODEX_SHARED_TMUX_PROTOCOL,
    RODEX_SHARED_TMUX_PROTOCOL_OPTION,
    RODEX_SHARED_TMUX_SERVER_ID_OPTION,
    TmuxRuntimeCapability,
    TmuxSessionCapability,
)
from rodex.tmux_status import (
    RODEX_STATUS_LEFT_FORMAT,
    RODEX_STATUS_STYLE,
    STATUS_CLAIM_PRIORITY_OPTION,
    STATUS_CLAIM_PUBLISHER_OPTION,
    STATUS_CLAIM_TOKEN_OPTION,
)
from rodex_registry import (
    RodexRegistryId,
    RodexRuntimeId,
    RodexSessionId,
    initialise_rodex_database,
)

RUNTIME_ID = RodexRuntimeId.parse("0c01ee2ead7240e1")


def _registered_capability(
    socket_path: Path,
    *,
    tmux_session_id: str = "$7",
) -> TmuxSessionCapability:
    return TmuxSessionCapability(
        socket_path,
        "0123456789abcdef0123456789abcdef",
        tmux_session_id,
        "%9",
        RUNTIME_ID,
        RodexSessionId.parse("1111111111111111"),
        RodexRegistryId.parse("2222222222222222"),
        7,
        uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
    )


def _runtime_with_registered_capability(
    launcher: RodexRuntimeLauncher,
    socket_path: Path,
    session_name: str = "remarkable-aardvark",
) -> LiveTmuxSession:
    capability = _registered_capability(socket_path)
    launcher._resolve_registered_tmux_capability = lambda _runtime: capability  # type: ignore[method-assign]
    return LiveTmuxSession(
        socket_path,
        session_name,
        runtime_id=RUNTIME_ID,
        tmux_capability=capability,
    )


def _mock_registered_ui_capability(
    monkeypatch: pytest.MonkeyPatch,
    launcher: RodexRuntimeLauncher,
    runner: RuntimeRunner,
    socket_path: Path,
) -> TmuxSessionCapability:
    capability = _registered_capability(socket_path)
    monkeypatch.setattr(
        launcher,
        "_resolve_registered_tmux_capability",
        lambda _runtime: capability,
    )
    monkeypatch.setattr(
        launcher,
        "_list_registered_tmux_capabilities",
        lambda _runtime, _server_id: (capability,),
    )

    def read_server(
        _runtime: LiveTmuxSession, _option_name: str, **_options: object
    ) -> str:
        for command in reversed(runner.calls):
            fields = shlex.split(command[-2]) if command[3:4] == ["if-shell"] else []
            if fields[:3] == ["set-option", "-so", _option_name]:
                return fields[-1]
        return ""

    monkeypatch.setattr(launcher, "_read_tmux_server_option", read_server)

    def read_hook(_runtime: LiveTmuxSession, hook_name: str, **_options: object) -> str:
        for command in reversed(runner.calls):
            fields = shlex.split(command[-2]) if command[3:4] == ["if-shell"] else []
            if fields[:3] == ["set-option", "-go", hook_name]:
                return fields[-1]
        return ""

    def read_key(_runtime: LiveTmuxSession, _key: str, **_options: object) -> str:
        for command in reversed(runner.calls):
            action = command[-2] if command[3:4] == ["if-shell"] else ""
            prefix = "bind-key -n C-c "
            if action.startswith(prefix):
                return read_server(
                    _runtime,
                    "@rodex_shared_tmux_ctrl_c_command",
                )
        return ""

    monkeypatch.setattr(launcher, "_read_tmux_global_hook_command", read_hook)
    monkeypatch.setattr(launcher, "_read_tmux_root_key_command", read_key)
    return capability


def _register_real_tmux_session(
    tmux: Callable[..., subprocess.CompletedProcess[str]],
    socket_path: Path,
    session_name: str,
) -> LiveTmuxSession:
    pane_id = tmux(
        "display-message", "-p", "-t", f"={session_name}:", "-F", "#{pane_id}"
    ).stdout.strip()
    capability = TmuxSessionCapability(
        socket_path,
        "0123456789abcdef0123456789abcdef",
        tmux("display-message", "-p", "-t", pane_id, "-F", "#{session_id}").stdout.strip(),
        pane_id,
        RUNTIME_ID,
        RodexSessionId.parse("1111111111111111"),
        RodexRegistryId.parse("2222222222222222"),
        7,
        uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
    )
    tmux(
        "set-option",
        "-s",
        "@rodex_shared_tmux_protocol",
        "rodex-shared-tmux-v1",
    )
    tmux(
        "set-option",
        "-s",
        "@rodex_shared_tmux_server_id",
        capability.tmux_server_id,
    )
    for option, value in (
        ("@rodex_primary_pane_id", capability.tmux_primary_pane_id),
        ("@rodex_runtime_id", str(capability.runtime_id)),
        ("@rodex_registration_state", "registered"),
        ("@rodex_session_id", str(capability.rodex_session_id)),
        ("@rodex_registry_id", str(capability.registry_id)),
        ("@rodex_sessions_id", str(capability.internal_session_id)),
        ("@rodex_codex_session_id", str(capability.codex_session_id)),
    ):
        tmux("set-option", "-t", f"={session_name}:", option, value)
    return LiveTmuxSession(
        socket_path,
        session_name,
        runtime_id=RUNTIME_ID,
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


class CatalogWebSocket:
    def __init__(self, read_response: dict[str, object]) -> None:
        self.sent: list[dict[str, object]] = []
        self.responses = iter(
            [
                {
                    "id": 0,
                    "result": {"userAgent": "rodex-session-catalog/0.147.0 (linux)"},
                },
                read_response,
            ]
        )

    def __enter__(self) -> CatalogWebSocket:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def recv(self, timeout: float) -> str:
        assert timeout > 0
        return json.dumps(next(self.responses))


class CatalogProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float) -> int:
        assert timeout > 0
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class CatalogProcessSpawner:
    def __init__(self, process: CatalogProcess) -> None:
        self.process = process
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, command: tuple[str, ...], **options: object) -> CatalogProcess:
        self.calls.append((command, options))
        socket_argument = command[-1]
        assert socket_argument.startswith("unix://")
        Path(socket_argument.removeprefix("unix://")).touch()
        return self.process


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
        if any("new-session" in argument for argument in command):
            (self.runtime_root / "app-0123456789abcdef.sock").touch()
        if (
            command[3:4] == ["display-message"]
            and "@rodex_primary_pane_id" in command[-1]
            and "#{session_name}" in command[-1]
        ):
            target = command[command.index("-t") + 1]
            session_name = target.removeprefix("=").removesuffix(":")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    f"{RODEX_SHARED_TMUX_PROTOCOL}\t"
                    "0123456789abcdef0123456789abcdef\t"
                    f"{session_name}\t$7\t%9\t{RUNTIME_ID}\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_observer_uses_distinct_codex_identity_fields_and_no_compression(
    tmp_path: Path,
) -> None:
    codex_session_id = "01a00654-f2bc-7a30-834a-a5f886a65f82"
    connector = RecordingConnector([codex_session_id])
    launcher = RodexRuntimeLauncher("codex", "tmux", connector=connector)

    assert launcher._list_loaded_codex_threads(tmp_path / "app.sock") == [codex_session_id]

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


@pytest.mark.parametrize("persisted", [True, False])
def test_transient_app_server_reads_exact_persisted_codex_identity_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persisted: bool,
) -> None:
    codex_session_id = uuid.UUID("01a015f4-f27c-7592-8060-d12313e8d0ce")
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        runtime_module.secrets, "token_hex", lambda _size: "catalogtoken0001"
    )
    read_response: dict[str, object]
    if persisted:
        read_response = {
            "id": 1,
            "result": {
                "thread": {
                    "id": str(codex_session_id),
                    "ephemeral": False,
                    "status": {"type": "notLoaded"},
                }
            },
        }
    else:
        read_response = {
            "id": 1,
            "error": {
                "code": -32600,
                "message": f"thread not loaded: {codex_session_id}",
            },
        }
    websocket = CatalogWebSocket(read_response)

    def connector(*_args: object, **_kwargs: object) -> CatalogWebSocket:
        return websocket

    process = CatalogProcess()
    spawner = CatalogProcessSpawner(process)
    launcher = RodexRuntimeLauncher(
        "/usr/bin/codex",
        "/usr/bin/tmux",
        connector=connector,
        process_spawner=spawner,  # type: ignore[arg-type]
    )

    assert launcher.codex_session_is_persisted(codex_session_id) is persisted

    assert process.terminated
    assert spawner.calls[0][0] == (
        "/usr/bin/codex",
        "app-server",
        "--listen",
        f"unix://{tmp_path / 'catalog-catalogtoken0001.sock'}",
    )
    assert websocket.sent == [
        {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "rodex-session-catalog",
                    "title": "Rodex Session Catalog",
                    "version": "0.5.0a1",
                }
            },
        },
        {"method": "initialized", "params": {}},
        {
            "method": "thread/read",
            "id": 1,
            "params": {
                "threadId": str(codex_session_id),
                "includeTurns": False,
            },
        },
    ]
    assert not (tmp_path / "catalog-catalogtoken0001.sock").exists()
    assert not (tmp_path / "catalog-catalogtoken0001.log").exists()


def test_start_directly_hosts_codex_in_tmux_and_returns_its_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_session_id = "01a00654-f2bc-7a30-834a-a5f886a65f82"
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        runtime_module.secrets,
        "token_hex",
        lambda size: "0123456789abcdef" if size == 8 else "0123456789abcdef" * 2,
    )
    runner = RuntimeRunner(tmp_path)
    connector = RecordingConnector([codex_session_id])
    rodex_virtual_environment = Path(sys.prefix)
    launcher = RodexRuntimeLauncher(
        "/usr/bin/codex",
        "/usr/bin/tmux",
        runner=runner,
        connector=connector,
        python_executable="/venv/bin/python",
        environment={
            "PATH": f"{rodex_virtual_environment / 'bin'}:/usr/bin",
            "VIRTUAL_ENV": str(rodex_virtual_environment),
            "VIRTUAL_ENV_PROMPT": "(rodex)",
            "UV_RUN_RECURSION_DEPTH": "1",
            "USER_SETTING": "preserved",
        },
    )

    live, observed_codex_session_id = launcher.start(
        tmp_path, ["--model", "example"], runtime_id=RUNTIME_ID
    )

    assert observed_codex_session_id == uuid.UUID(codex_session_id)
    assert live.tmux_session_name == "rodex-0123456789abcdef"
    new_session = runner.calls[0]
    assert new_session[:3] == [
        "/usr/bin/tmux",
        "-S",
        str(tmp_path / "tmux-shared-v1.sock"),
    ]
    assert new_session[3:7] == ["start-server", ";", "if-shell", "-F"]
    claim = shlex.split(new_session[8])
    assert claim == [
        "set-option",
        "-s",
        RODEX_SHARED_TMUX_PROTOCOL_OPTION,
        RODEX_SHARED_TMUX_PROTOCOL,
        ";",
        "set-option",
        "-s",
        RODEX_SHARED_TMUX_SERVER_ID_OPTION,
        "0123456789abcdef0123456789abcdef",
    ]
    assert new_session[9] == "run-shell true"
    assert new_session[11:13] == ["if-shell", "-F"]
    creation = shlex.split(new_session[-2])
    assert creation[:5] == [
        "set-option",
        "-g",
        "history-limit",
        str(RODEX_TMUX_HISTORY_LIMIT_LINES),
        ";",
    ]
    assert creation[5:11] == [
        "new-session",
        "-d",
        "-s",
        "rodex-0123456789abcdef",
        "-c",
        str(tmp_path),
    ]
    assert [
        creation[index + 1] for index, argument in enumerate(creation) if argument == "-e"
    ] == [
        "PATH=/usr/bin",
        "VIRTUAL_ENV=",
        "VIRTUAL_ENV_PROMPT=",
        "UV_RUN_RECURSION_DEPTH=",
    ]
    host_command = next(
        argument for argument in creation if "-m rodex.session_host" in argument
    )
    assert "/venv/bin/python -m rodex.session_host" in host_command
    assert f"--protocol-event-socket {tmp_path / 'events-0123456789abcdef.sock'}" in (
        host_command
    )
    assert "--model example" in host_command
    assert f"--rodex-runtime-id {RUNTIME_ID}" in host_command
    assert "--rodex-database" not in host_command
    assert "--codex-sessions-root" not in host_command
    assert "send-keys" not in host_command
    assert runner.options[0]["env"] == {
        "PATH": "/usr/bin",
        "USER_SETTING": "preserved",
    }
    assert creation[-18:] == [
        ";",
        "set-environment",
        "-r",
        "-t",
        "=rodex-0123456789abcdef",
        "VIRTUAL_ENV",
        ";",
        "set-environment",
        "-r",
        "-t",
        "=rodex-0123456789abcdef",
        "VIRTUAL_ENV_PROMPT",
        ";",
        "set-environment",
        "-r",
        "-t",
        "=rodex-0123456789abcdef",
        "UV_RUN_RECURSION_DEPTH",
    ]
    published = shlex.split(runner.calls[-1][-2])
    assert published.count(";") == 3
    for option, value in (
        (
            "@rodex_protocol_proxy_socket_path",
            str(tmp_path / "proxy-0123456789abcdef.sock"),
        ),
        (
            "@rodex_protocol_event_socket_path",
            str(tmp_path / "events-0123456789abcdef.sock"),
        ),
        ("@rodex_codex_session_id", codex_session_id),
        ("@rodex_runtime_id", str(RUNTIME_ID)),
    ):
        expected = [
            "set-option",
            "-t",
            "%9",
            option,
            value,
        ]
        assert any(
            published[index : index + len(expected)] == expected
            for index in range(len(published))
        )
    assert live.runtime_id == RUNTIME_ID


def test_start_passes_the_exact_leading_zero_session_id_to_the_session_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_session_id = "01a00654-f2bc-7a30-834a-a5f886a65f82"
    rodex_session_id = RodexSessionId.parse("0000000000000001")
    registry_id = RodexRegistryId.parse("06179a3581264d53")
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("RODEX_CODEX_SESSIONS_ROOT", str(tmp_path / "sessions"))
    monkeypatch.setattr(
        runtime_module.secrets,
        "token_hex",
        lambda size: "0123456789abcdef" if size == 8 else "0123456789abcdef" * 2,
    )
    runner = RuntimeRunner(tmp_path)
    launcher = RodexRuntimeLauncher(
        "/usr/bin/codex",
        "/usr/bin/tmux",
        runner=runner,
        connector=RecordingConnector([codex_session_id]),
        python_executable="/venv/bin/python",
    )

    launcher.start(
        tmp_path,
        [],
        runtime_id=RUNTIME_ID,
        rodex_session_id=rodex_session_id,
        rodex_registry_id=registry_id,
        rodex_database_path=tmp_path / "rodex-v3.sqlite3",
    )

    host_command = next(
        argument for argument in runner.calls[0] if "-m rodex.session_host" in argument
    )
    assert "--rodex-session-id 0000000000000001" in host_command
    assert f"--rodex-database {tmp_path / 'rodex-v3.sqlite3'}" in host_command


def test_exact_resume_fails_when_codex_reports_that_session_id_is_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_session_id = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        runtime_module.secrets,
        "token_hex",
        lambda size: "0123456789abcdef" if size == 8 else "0123456789abcdef" * 2,
    )
    calls: list[list[str]] = []

    def exited_resume_runner(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if any("new-session" in argument for argument in command):
            (tmp_path / "app-0123456789abcdef.log").write_text(
                f"ERROR: No saved session found with ID {codex_session_id}. "
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
    monkeypatch.setattr(launcher, "_stable_tmux_session_target", lambda _runtime: "$7")
    monkeypatch.setattr(
        launcher,
        "_resolve_bootstrap_tmux_capability",
        lambda _runtime: (
            _registered_capability(tmp_path / "tmux-shared-v1.sock").runtime_capability
        ),
    )

    with pytest.raises(RodexCodexSessionNotFoundError) as raised:
        launcher.start(
            tmp_path,
            ["resume", str(codex_session_id)],
            runtime_id=RUNTIME_ID,
        )

    assert str(raised.value) == (
        f"Codex has no saved session for exact identity {codex_session_id}"
    )
    assert any("kill-session" in argument for command in calls for argument in command)


def test_exact_resume_rejects_a_different_loaded_id_before_publishing_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested_codex_session_id = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
    observed_codex_session_id = uuid.UUID(int=requested_codex_session_id.int + 1)
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        runtime_module.secrets,
        "token_hex",
        lambda size: "0123456789abcdef" if size == 8 else "0123456789abcdef" * 2,
    )
    runner = RuntimeRunner(tmp_path)
    connector = RecordingConnector([str(observed_codex_session_id)])
    launcher = RodexRuntimeLauncher(
        "/usr/bin/codex",
        "/usr/bin/tmux",
        runner=runner,
        connector=connector,
        python_executable="/venv/bin/python",
    )
    monkeypatch.setattr(launcher, "_stable_tmux_session_target", lambda _runtime: "$7")

    with pytest.raises(RodexRuntimeError) as raised:
        launcher.start(
            tmp_path,
            ["resume", str(requested_codex_session_id)],
            runtime_id=RUNTIME_ID,
        )

    assert str(raised.value) == (
        "Codex resumed an unexpected exact identity: "
        f"requested {requested_codex_session_id}, observed {observed_codex_session_id}"
    )
    tmux_operations = [command[3] for command in runner.calls]
    assert tmux_operations == [
        "start-server",
        "has-session",
        "display-message",
        "if-shell",
    ]
    assert "kill-session" in runner.calls[-1][-2]
    assert any("new-session" in argument for argument in runner.calls[0])


def test_attach_uses_live_stdio_and_escapes_an_existing_tmux_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMUX", "/tmp/outer")
    runner = RuntimeRunner(tmp_path)
    attach_events: list[str] = []
    published_notices: list[tuple[Path, str]] = []
    launcher = RodexRuntimeLauncher(
        "codex",
        "tmux",
        runner=runner,
        attach_notice=lambda: attach_events.append("notice") or "Codex update available",
        tui_notice_publisher=lambda path, message: (
            published_notices.append((path, message)) or True
        ),
        environment={
            "PATH": "/project-xyz/.venv/bin:/usr/bin",
            "TERM": "xterm-256color",
            "TMUX": "/tmp/outer",
            "VIRTUAL_ENV": "/project-xyz/.venv",
        },
    )
    live = LiveRodexRuntime(
        tmp_path / "tmux.sock",
        "rodex-one",
        tmp_path / "app.sock",
        tmp_path / "app.log",
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
        runtime_id=RUNTIME_ID,
    )
    monkeypatch.setattr(
        launcher,
        "_resolve_registered_tmux_capability",
        lambda _runtime: _registered_capability(tmp_path / "tmux.sock"),
    )

    launcher.attach(live)

    assert attach_events == ["notice"]
    assert published_notices == [(tmp_path / "proxy.sock", "Codex update available")]
    assert runner.calls[-1][3:8] == [
        "-T",
        RODEX_TMUX_REQUIRED_CLIENT_FEATURES,
        "if-shell",
        "-t",
        "%9",
    ]
    assert "attach-session" in runner.calls[-1][-2]
    assert runner.calls[-1][-1] == "run-shell false"
    assert "$7" in runner.calls[-1][-2]
    assert "capture_output" not in runner.options[-1]
    environment = runner.options[-1]["env"]
    assert isinstance(environment, dict)
    assert "TMUX" not in environment
    assert environment["VIRTUAL_ENV"] == "/project-xyz/.venv"
    assert environment["PATH"] == "/project-xyz/.venv/bin:/usr/bin"


def test_attach_uses_stable_runtime_identity_when_alias_wins_before_tmux_attach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attach_notice_entered = Event()
    alias_completed = Event()
    errors: list[BaseException] = []

    class AliasDuringAttachRunner(RuntimeRunner):
        def __init__(self) -> None:
            super().__init__(tmp_path)
            self.live_name = "before-alias"

        def __call__(
            self, command: list[str], **options: object
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append(command)
            self.options.append(options)
            if "if-shell" in command and any(
                "rename-session" in argument for argument in command
            ):
                assert "$9" in command[-2]
                assert "after-alias" in command[-2]
                self.live_name = "after-alias"
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if any("attach-session" in argument for argument in command):
                assert self.live_name == "after-alias"
                assert "$9" in command[-2]
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def wait_while_alias_completes() -> None:
        attach_notice_entered.set()
        assert alias_completed.wait(5)

    runner = AliasDuringAttachRunner()
    launcher = RodexRuntimeLauncher(
        "codex",
        "tmux",
        runner=runner,
        attach_notice=wait_while_alias_completes,
    )
    prepared = LiveTmuxSession(
        tmp_path / "tmux.sock",
        "before-alias",
        runtime_id=RUNTIME_ID,
    )
    monkeypatch.setattr(
        launcher,
        "_stable_tmux_session_target",
        lambda _runtime: "$9",
    )
    monkeypatch.setattr(
        launcher,
        "_resolve_registered_tmux_capability",
        lambda _runtime: _registered_capability(
            tmp_path / "tmux.sock",
            tmux_session_id="$9",
        ),
    )

    def attach_prepared_runtime() -> None:
        try:
            launcher.attach(prepared)
        except BaseException as error:
            errors.append(error)

    attaching = Thread(target=attach_prepared_runtime)
    attaching.start()
    assert attach_notice_entered.wait(5), errors
    renamed = launcher.rename(prepared, "after-alias")
    assert renamed.tmux_session_name == "after-alias"
    alias_completed.set()
    attaching.join(5)

    assert not attaching.is_alive()
    assert errors == []
    assert [command[3] for command in runner.calls] == [
        "if-shell",
        "-T",
    ]
    assert "$9" in runner.calls[-1][-2]


def test_mouse_uses_stable_runtime_identity_after_name_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReusedNameRunner(RuntimeRunner):
        def __call__(
            self, command: list[str], **options: object
        ) -> subprocess.CompletedProcess[str]:
            self.calls.append(command)
            self.options.append(options)
            if "if-shell" in command:
                action = shlex.split(command[-2])
                assert action[-2:] == ["%9", "mouse"] or "%9" in action
                if action[0] == "show-options":
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="on\n",
                        stderr="",
                    )
                assert action[0] == "set-option"
                assert action[-1] == "on"
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            raise AssertionError(f"unexpected tmux command: {command}")

    runner = ReusedNameRunner(tmp_path)
    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    stale_name = LiveTmuxSession(
        tmp_path / "tmux.sock",
        "reused-name",
        runtime_id=RUNTIME_ID,
    )
    monkeypatch.setattr(
        launcher,
        "_resolve_registered_tmux_capability",
        lambda _runtime: _registered_capability(tmp_path / "tmux.sock"),
    )

    assert launcher.set_mouse_mode(stale_name, "on") == "on"
    assert len(runner.calls) == 2
    assert all("=reused-name:" not in command for command in runner.calls)


def test_attach_notice_failure_never_blocks_tmux_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RuntimeRunner(tmp_path)

    def fail_notice() -> str | None:
        raise OSError("npm unavailable")

    launcher = RodexRuntimeLauncher(
        "codex", "tmux", runner=runner, attach_notice=fail_notice
    )
    monkeypatch.setattr(
        launcher,
        "_resolve_registered_tmux_capability",
        lambda _runtime: _registered_capability(tmp_path / "tmux.sock"),
    )

    launcher.attach(
        LiveTmuxSession(
            tmp_path / "tmux.sock",
            "rodex-one",
            runtime_id=RUNTIME_ID,
        )
    )

    assert len(runner.calls) == 1
    assert "attach-session" in runner.calls[-1][-2]
    assert "$7" in runner.calls[-1][-2]


def test_tui_notice_delivery_failure_never_blocks_tmux_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RuntimeRunner(tmp_path)

    def fail_delivery(_path: Path, _message: str) -> bool:
        raise OSError("proxy unavailable")

    launcher = RodexRuntimeLauncher(
        "codex",
        "tmux",
        runner=runner,
        attach_notice=lambda: "Codex update available",
        tui_notice_publisher=fail_delivery,
    )
    runtime = LiveRodexRuntime(
        tmp_path / "tmux.sock",
        "rodex-one",
        tmp_path / "app.sock",
        tmp_path / "app.log",
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
        runtime_id=RUNTIME_ID,
    )
    monkeypatch.setattr(
        launcher,
        "_resolve_registered_tmux_capability",
        lambda _runtime: _registered_capability(tmp_path / "tmux.sock"),
    )

    launcher.attach(runtime)

    assert len(runner.calls) == 1
    assert "attach-session" in runner.calls[-1][-2]
    assert "$7" in runner.calls[-1][-2]


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


def test_current_tmux_context_resolves_the_inherited_pane_on_its_exact_socket(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "runtime,with-comma" / "tmux.sock"
    calls: list[list[str]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="tangible-booby\t$3\t@5\t%7\t2\n",
            stderr="",
        )

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)

    assert launcher.discover_current_tmux_pane_context(
        {"TMUX": f"{socket_path},1234,0", "TMUX_PANE": "%7"}
    ) == CurrentTmuxPaneContext(
        tmux_session=LiveTmuxSession(socket_path, "tangible-booby"),
        tmux_session_id="$3",
        tmux_window_id="@5",
        tmux_pane_id="%7",
        attached_client_count=2,
    )
    assert calls == [
        [
            "tmux",
            "-S",
            str(socket_path),
            "display-message",
            "-p",
            "-t",
            "%7",
            "-F",
            (
                "#{session_name}\t#{session_id}\t#{window_id}\t#{pane_id}\t"
                "#{session_attached}"
            ),
        ]
    ]


def test_current_tmux_context_requires_an_inherited_tmux_pane(tmp_path: Path) -> None:
    launcher = RodexRuntimeLauncher(
        "codex",
        "tmux",
        runner=lambda *_args, **_options: pytest.fail("tmux should not run"),
    )

    with pytest.raises(RodexRuntimeError, match="must run inside the tmux pane"):
        launcher.discover_current_tmux_pane_context({})

    with pytest.raises(RodexRuntimeError, match="TMUX_PANE identity is invalid"):
        launcher.discover_current_tmux_pane_context(
            {"TMUX": f"{tmp_path / 'tmux.sock'},1234,0", "TMUX_PANE": "pane-7"}
        )


def test_scrollback_capture_reads_all_lines_from_the_exact_tmux_pane(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="first\nsecond\n\n\n",
            stderr="",
        )

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    runtime = _runtime_with_registered_capability(launcher, tmp_path / "tmux.sock")

    assert launcher.capture_scrollback(runtime) == ("first", "second")
    assert len(calls) == 1
    assert calls[0][3:6] == ["if-shell", "-t", "%9"]
    assert "capture-pane -p -S - -t %9" in calls[0][-2]
    assert calls[0][-1] == "run-shell false"


def test_real_tmux_scrollback_ignores_the_selected_observer_pane(
    tmp_path: Path,
) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"
    session_name = "primary-pane-read"

    def tmux(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [tmux_binary, "-S", str(socket_path), *arguments],
            check=check,
            text=True,
            capture_output=True,
        )

    tmux(
        "new-session",
        "-d",
        "-s",
        session_name,
        "printf 'primary-marker\\n'; sleep 30",
    )
    try:
        runtime = _register_real_tmux_session(tmux, socket_path, session_name)
        observer_pane_id = tmux(
            "split-window",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            f"={session_name}:",
            "printf 'observer-marker\\n'; sleep 30",
        ).stdout.strip()
        tmux("select-pane", "-t", observer_pane_id)

        captured = RodexRuntimeLauncher("codex", tmux_binary).capture_scrollback(runtime)

        assert "primary-marker" in captured
        assert "observer-marker" not in captured
    finally:
        tmux("kill-server", check=False)


def test_scrollback_snapshot_marks_tmuxs_committed_history_boundary(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        stdout = "2\nhistory one\nhistory two\nvisible\nprompt\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    runtime = _runtime_with_registered_capability(launcher, tmp_path / "tmux.sock")

    assert launcher.capture_scrollback_snapshot(runtime) == TmuxScrollbackSnapshot(
        ("history one", "history two", "visible", "prompt"), 2
    )
    assert len(calls) == 1
    assert calls[0][3:6] == ["if-shell", "-t", "%9"]
    assert "display-message" in calls[0][-2]
    assert " ; capture-pane" in calls[0][-2]


def test_scrollback_snapshot_preserves_a_blank_committed_history_row(
    tmp_path: Path,
) -> None:
    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        stdout = "2\nhistory one\n\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)

    assert launcher.capture_scrollback_snapshot(
        _runtime_with_registered_capability(launcher, tmp_path / "tmux.sock")
    ) == TmuxScrollbackSnapshot(("history one", ""), 2)


def test_scrollback_state_consolidates_identity_and_bounded_pane_capture(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    identity = "\t".join(
        (
            "$3",
            "@5",
            "%7",
            "4321",
            "proxy",
            "events",
            "codex",
            "rodex",
            "registry",
            "registered",
            "runtime",
        )
    )

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"2\t{identity}\nhistory one\nhistory two\nvisible\nprompt\n",
            stderr="",
        )

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    runtime = _runtime_with_registered_capability(launcher, tmp_path / "tmux.sock")

    assert launcher.capture_scrollback_state(runtime) == TmuxScrollbackState(
        history_line_count=2,
        history_tail_lines=("history one", "history two"),
        visible_lines=("visible", "prompt"),
        runtime_identity=identity,
    )
    assert len(calls) == 1
    assert calls[0][3:6] == ["if-shell", "-t", "%9"]
    assert "display-message" in calls[0][-2]
    assert " ; capture-pane" in calls[0][-2]


@pytest.mark.parametrize("history_size", ["", "one", "-1", "1\t2"])
def test_scrollback_snapshot_rejects_invalid_history_metadata(
    tmp_path: Path, history_size: str
) -> None:
    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{history_size}\n",
            stderr="",
        )

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)

    with pytest.raises(RodexRuntimeError, match="invalid history size"):
        launcher.capture_scrollback_snapshot(
            _runtime_with_registered_capability(launcher, tmp_path / "tmux.sock")
        )


@pytest.mark.parametrize(
    "reported_context",
    [
        "tangible-booby\t3\t@5\t%7\t1",
        "tangible-booby\t$3\t5\t%7\t1",
        "tangible-booby\t$3\t@5\t%8\t1",
    ],
)
def test_current_tmux_context_rejects_invalid_reported_object_identities(
    tmp_path: Path,
    reported_context: str,
) -> None:
    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{reported_context}\n",
            stderr="",
        )

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)

    with pytest.raises(RodexRuntimeError, match="invalid current session context"):
        launcher.discover_current_tmux_pane_context(
            {"TMUX": f"{tmp_path / 'tmux.sock'},1234,0", "TMUX_PANE": "%7"}
        )


@pytest.mark.parametrize("reported_count", ["many", "-1"])
def test_current_tmux_context_rejects_an_invalid_attached_client_count(
    tmp_path: Path,
    reported_count: str,
) -> None:
    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"tangible-booby\t$3\t@5\t%7\t{reported_count}\n",
            stderr="",
        )

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)

    with pytest.raises(RodexRuntimeError, match="invalid attached-client count"):
        launcher.discover_current_tmux_pane_context(
            {"TMUX": f"{tmp_path / 'tmux.sock'},1234,0", "TMUX_PANE": "%7"}
        )


def test_rename_and_session_ui_initialisation_use_the_real_tmux_session_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RuntimeRunner(tmp_path)
    launcher = RodexRuntimeLauncher(
        "codex", "tmux", runner=runner, python_executable="/venv/bin/python"
    )
    original = LiveTmuxSession(
        tmp_path / "tmux.sock",
        "rodex-token",
        runtime_id=RUNTIME_ID,
    )
    capability = _registered_capability(tmp_path / "tmux.sock")
    monkeypatch.setattr(
        launcher,
        "_stable_tmux_session_target",
        lambda _runtime: capability.session_target,
    )
    monkeypatch.setattr(
        launcher,
        "_resolve_registered_tmux_capability",
        lambda _runtime: capability,
    )
    monkeypatch.setattr(
        launcher,
        "_list_registered_tmux_capabilities",
        lambda _runtime, _server_id: (capability,),
    )

    def read_server(_runtime: LiveTmuxSession, option_name: str, **_options: object) -> str:
        for command in reversed(runner.calls):
            fields = shlex.split(command[-2]) if command[3:4] == ["if-shell"] else []
            if fields[:3] == ["set-option", "-so", option_name]:
                return fields[-1]
        return ""

    monkeypatch.setattr(launcher, "_read_tmux_server_option", read_server)

    def read_hook(_runtime: LiveTmuxSession, hook_name: str, **_options: object) -> str:
        for command in reversed(runner.calls):
            fields = shlex.split(command[-2]) if command[3:4] == ["if-shell"] else []
            if fields[:3] == ["set-option", "-go", hook_name]:
                return fields[-1]
        return ""

    def read_key(_runtime: LiveTmuxSession, _key: str, **_options: object) -> str:
        for command in reversed(runner.calls):
            action = command[-2] if command[3:4] == ["if-shell"] else ""
            prefix = "bind-key -n C-c "
            if action.startswith(prefix):
                return read_server(
                    _runtime,
                    "@rodex_shared_tmux_ctrl_c_command",
                )
        return ""

    monkeypatch.setattr(launcher, "_read_tmux_global_hook_command", read_hook)
    monkeypatch.setattr(launcher, "_read_tmux_root_key_command", read_key)

    renamed = launcher.rename(original, "automatic-beluga")
    launcher.initialise_session_ui(renamed)

    assert renamed == LiveTmuxSession(
        tmp_path / "tmux.sock",
        "automatic-beluga",
        runtime_id=RUNTIME_ID,
        tmux_capability=capability,
    )
    assert runner.calls[0][3:8] == ["if-shell", "-t", "%9", "-F", runner.calls[0][7]]
    assert "rename-session" in runner.calls[0][-2]
    assert "$7" in runner.calls[0][-2]
    assert "automatic-beluga" in runner.calls[0][-2]
    assert runner.options[0]["timeout"] == 5.0
    status_commands = [command[3:] for command in runner.calls[1:]]
    status_actions = [
        shlex.split(command[-2])
        for command in runner.calls[1:]
        if command[3:4] == ["if-shell"]
    ]
    assert not any("=automatic-beluga:" in command for command in status_commands)
    local_hook_removals = [
        command for command in status_commands if "set-hook -u -t" in command[-1]
    ]
    assert local_hook_removals == []
    hook_commands = [
        command
        for command in status_actions
        if command[:2] == ["set-option", "-go"] and command[2].startswith("client-")
    ]
    assert [command[2] for command in hook_commands] == [
        "client-attached[731]",
        "client-detached[731]",
    ]
    assert all("rodex.tmux_sharing_coordinator" in command[-1] for command in hook_commands)
    assert all(
        "rodex.status_animation_admission" not in command[-1] for command in hook_commands
    )
    assert all("#{session_id}" not in command[-1] for command in hook_commands)
    fenced_status_writes = [
        command
        for command in status_commands
        if command[:3] == ["if-shell", "-t", "%9"] and "set-option" in command[-2]
    ]
    assert fenced_status_writes
    assert any(RODEX_STATUS_LEFT_FORMAT in command[-2] for command in fenced_status_writes)
    assert any(RODEX_STATUS_STYLE in command[-2] for command in fenced_status_writes)
    ctrl_c_owner_claim = next(
        command
        for command in status_actions
        if command[:3] == ["set-option", "-so", "@rodex_shared_tmux_ctrl_c_command"]
    )
    assert ctrl_c_owner_claim[:2] == ["set-option", "-so"]
    assert ctrl_c_owner_claim[2] == "@rodex_shared_tmux_ctrl_c_command"
    shared_ctrl_c_binding = next(
        command for command in status_actions if command[:3] == ["bind-key", "-n", "C-c"]
    )
    assert shared_ctrl_c_binding[:3] == ["bind-key", "-n", "C-c"]
    assert shared_ctrl_c_binding[3].startswith("run-shell -b ")
    assert "/venv/bin/python -m rodex.tmux_shared_ctrl_c" in shared_ctrl_c_binding[3]
    for option, tmux_format in (
        ("--tmux-session-id", "#{q:session_id}"),
        ("--tmux-primary-pane-id", "#{q:@rodex_primary_pane_id}"),
        ("--expected-runtime-id", "#{q:@rodex_runtime_id}"),
        ("--expected-rodex-session-id", "#{q:@rodex_session_id}"),
        ("--expected-registry-id", "#{q:@rodex_registry_id}"),
        ("--expected-internal-session-id", "#{q:@rodex_sessions_id}"),
        ("--expected-codex-session-id", "#{q:@rodex_codex_session_id}"),
        ("--pane-id", "#{q:pane_id}"),
        ("--client-name", "#{q:client_name}"),
    ):
        assert option in shared_ctrl_c_binding[3]
        assert tmux_format in shared_ctrl_c_binding[3]
    assert "--attached-count" not in shared_ctrl_c_binding[3]


def test_rename_reports_a_bounded_tmux_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def runner(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, options["timeout"])

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    monkeypatch.setattr(
        launcher,
        "_resolve_registered_tmux_capability",
        lambda _runtime: _registered_capability(tmp_path / "tmux.sock"),
    )

    with pytest.raises(RodexRuntimeError, match=r"timed out after 5s: if-shell"):
        launcher.rename(
            LiveTmuxSession(
                tmp_path / "tmux.sock",
                "rodex-token",
                runtime_id=RUNTIME_ID,
            ),
            "automatic-beluga",
        )


def test_primary_disconnect_resets_every_connection_scoped_observer() -> None:
    resets: list[str] = []

    class ResetRecorder:
        def __init__(self, name: str) -> None:
            self.name = name

        def reset_after_disconnect(self) -> None:
            resets.append(self.name)

    runtime_module.PrimaryConnectionLifecycleCoordinator(
        (
            ResetRecorder("context"),
            ResetRecorder("event-tap"),
            ResetRecorder("agent-observer"),
        )
    ).reset_after_disconnect()

    assert resets == ["context", "event-tap", "agent-observer"]


def test_fresh_session_host_process_survives_initialization(
    tmp_path: Path,
) -> None:
    database = initialise_rodex_database(tmp_path / "rodex.sqlite3")
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import signal, socket, sys\n"
        "if sys.argv[1:2] == ['app-server']:\n"
        "    endpoint = sys.argv[sys.argv.index('--listen') + 1]\n"
        "    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "    listener.bind(endpoint.removeprefix('unix://'))\n"
        "    listener.listen()\n"
        "while True:\n"
        "    signal.pause()\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o700)
    fake_tmux = tmp_path / "fake-tmux"
    fake_tmux.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_tmux.chmod(0o700)
    tmux_socket = tmp_path / "tmux.sock"
    tmux_socket.touch()
    ready = tmp_path / "analytics-started"
    event_socket = tmp_path / "events.sock"
    child_source = """
import sys
import uuid
from pathlib import Path

import rodex.runtime as runtime
from rodex.process_contracts import AnalyticsWorkerConfig, SessionHostConfig
from rodex.tmux_session_capability import TmuxRuntimeCapability
from rodex_registry import RodexRegistryId, RodexRuntimeId, RodexSessionId

database, fake_codex, fake_tmux, tmux_socket, ready, event_socket = map(Path, sys.argv[1:])
activated = AnalyticsWorkerConfig(
    rodex_database_path=database,
    codex_sessions_root=database.parent / "sessions",
    rodex_session_id=RodexSessionId(1),
    rodex_registry_id=RodexRegistryId.parse("0000000000000001"),
    runtime_id=RodexRuntimeId.parse("0c01ee2ead7240e1"),
    protocol_event_socket_path=event_socket,
    rodex_sessions_id=1,
    codex_session_id=uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
)
runtime._registered_analytics_worker_config = lambda *_args: activated
runtime._resolve_session_host_tmux_capability = lambda *_args: TmuxRuntimeCapability(
    tmux_socket,
    "0123456789abcdef0123456789abcdef",
    "$1",
    "%1",
    activated.runtime_id,
)

class HoldingSupervisor:
    def __init__(self, config):
        assert config == activated

    def start(self):
        ready.write_text("ready", encoding="utf-8")

    def close(self):
        return None

config = SessionHostConfig(
    codex_binary=str(fake_codex),
    app_server_socket_path=database.parent / "app.sock",
    app_server_log_path=database.parent / "app.log",
    protocol_proxy_socket_path=database.parent / "proxy.sock",
    protocol_event_socket_path=event_socket,
    tmux_binary=str(fake_tmux),
    tmux_server_socket_path=tmux_socket,
    runtime_id=activated.runtime_id,
    analytics=activated,
)
raise SystemExit(
    runtime.run_session_host(config, analytics_supervisor_factory=HoldingSupervisor)
)
"""
    environment = os.environ.copy()
    environment["TMUX_PANE"] = "%1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            child_source,
            os.fspath(database),
            os.fspath(fake_codex),
            os.fspath(fake_tmux),
            os.fspath(tmux_socket),
            os.fspath(ready),
            os.fspath(event_socket),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        while not ready.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        startup_error = "session host startup timed out"
        if process.poll() is not None:
            _debug_stdout, debug_stderr = process.communicate(timeout=3)
            startup_error = f"session host exited {process.returncode}: {debug_stderr}"
        assert ready.exists(), startup_error
        assert ready.read_text(encoding="utf-8") == "ready"
        time.sleep(0.2)
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            _stdout, stderr = process.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            _stdout, stderr = process.communicate(timeout=3)
    assert stderr == ""


@pytest.mark.evolutionary_regression
def test_shared_coordination_refresh_does_not_clear_or_replace_status_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coordinator repair preserves an unrelated live status claim."""
    runner = RuntimeRunner(tmp_path)
    launcher = RodexRuntimeLauncher(
        "codex", "tmux", runner=runner, python_executable="/venv/bin/python"
    )

    runtime = LiveTmuxSession(
        tmp_path / "tmux.sock",
        "renamed-beluga",
        runtime_id=RUNTIME_ID,
    )
    capability = _registered_capability(tmp_path / "tmux.sock")
    monkeypatch.setattr(
        launcher,
        "_resolve_registered_tmux_capability",
        lambda _runtime: capability,
    )
    monkeypatch.setattr(
        launcher,
        "_list_registered_tmux_capabilities",
        lambda _runtime, _server_id: (capability,),
    )

    def read_server(
        _runtime: LiveTmuxSession, _option_name: str, **_options: object
    ) -> str:
        for command in reversed(runner.calls):
            fields = shlex.split(command[-2]) if command[3:4] == ["if-shell"] else []
            if fields[:3] == ["set-option", "-so", _option_name]:
                return fields[-1]
        return ""

    monkeypatch.setattr(launcher, "_read_tmux_server_option", read_server)

    def read_hook(_runtime: LiveTmuxSession, hook_name: str, **_options: object) -> str:
        for command in reversed(runner.calls):
            fields = shlex.split(command[-2]) if command[3:4] == ["if-shell"] else []
            if fields[:3] == ["set-option", "-go", hook_name]:
                return fields[-1]
        return ""

    monkeypatch.setattr(launcher, "_read_tmux_global_hook_command", read_hook)

    launcher.refresh_shared_tmux_coordination(runtime)

    status_commands = [command[3:] for command in runner.calls]
    status_actions = [
        shlex.split(command[-2]) for command in runner.calls if command[3:4] == ["if-shell"]
    ]
    assert any(
        command[:2] == ["set-option", "-go"] and command[2].startswith("client-")
        for command in status_actions
    )
    assert not any(
        option in command
        for command in status_commands
        for option in (
            STATUS_CLAIM_PUBLISHER_OPTION,
            STATUS_CLAIM_TOKEN_OPTION,
            STATUS_CLAIM_PRIORITY_OPTION,
            "status-left",
            "status-format",
            "status-style",
        )
    )


def test_shared_ctrl_c_guard_fails_closed_on_a_user_owned_binding(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        output = (
            "bind-key -T root C-c display-message user-binding\n"
            if "list-keys -T root C-c" in command
            else ""
        )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    with pytest.raises(
        RodexRuntimeError,
        match="root C-c already has a non-Rodex binding",
    ):
        launcher._install_shared_ctrl_c_guard(
            LiveTmuxSession(
                tmp_path / "tmux.sock",
                "automatic-beluga",
                runtime_id=RUNTIME_ID,
            ),
            _registered_capability(tmp_path / "tmux.sock"),
        )

    assert not any(
        "bind-key -n C-c" in argument for command in calls for argument in command
    )
    assert not any(
        "rodex.tmux_shared_ctrl_c" in argument for command in calls for argument in command
    )


def test_stale_server_capability_cannot_submit_a_server_global_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def replacement_server(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=replacement_server)
    capability = _registered_capability(tmp_path / "tmux.sock")
    monkeypatch.setattr(
        launcher,
        "_resolve_registered_tmux_capability",
        lambda _runtime: capability,
    )

    with pytest.raises(RodexRuntimeError, match="different Rodex installation"):
        launcher.refresh_shared_tmux_coordination(
            LiveTmuxSession(
                tmp_path / "tmux.sock",
                "stale-runtime",
                runtime_id=RUNTIME_ID,
                tmux_capability=capability,
            )
        )

    assert calls
    assert all(command[3] == "if-shell" for command in calls)
    assert all(command[4] == "-F" for command in calls)
    assert all(capability.tmux_server_id in command[5] for command in calls)
    assert not any(command[3] in {"set-option", "bind-key"} for command in calls)


def test_real_tmux_fast_ctrl_b_d_detaches_without_ending_session(
    tmp_path: Path,
) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"
    session_name = "fast-prefix-detach"
    client_pid: int | None = None
    terminal_master: int | None = None

    def tmux(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [tmux_binary, "-S", str(socket_path), *arguments],
            check=check,
            text=True,
            capture_output=True,
        )

    tmux("new-session", "-d", "-s", session_name, "sleep 30")
    try:
        launcher = RodexRuntimeLauncher(
            "codex", tmux_binary, python_executable=sys.executable
        )
        launcher.initialise_session_ui(
            _register_real_tmux_session(tmux, socket_path, session_name)
        )
        binding = tmux("list-keys", "-T", "root", "C-b", check=False)
        assert "rodex.tmux_status" not in binding.stdout

        client_pid, terminal_master = pty.fork()
        if client_pid == 0:
            environment = os.environ.copy()
            environment["TERM"] = "xterm-256color"
            os.execve(
                tmux_binary,
                [
                    tmux_binary,
                    "-S",
                    str(socket_path),
                    "-T",
                    RODEX_TMUX_REQUIRED_CLIENT_FEATURES,
                    "attach-session",
                    "-t",
                    f"={session_name}",
                ],
                environment,
            )

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            attached = tmux(
                "display-message",
                "-p",
                "-t",
                f"={session_name}:",
                "-F",
                "#{session_attached}",
            )
            if attached.stdout.strip() == "1":
                break
            time.sleep(0.01)
        else:
            pytest.fail("tmux client did not attach")

        client_features = tmux(
            "list-clients",
            "-t",
            f"={session_name}",
            "-F",
            "#{client_termfeatures}",
        ).stdout.strip()
        assert "RGB" in client_features.split(",")

        os.write(terminal_master, b"\x02d")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            attached = tmux(
                "display-message",
                "-p",
                "-t",
                f"={session_name}:",
                "-F",
                "#{session_attached}",
            )
            if attached.stdout.strip() == "0":
                break
            time.sleep(0.01)
        else:
            pytest.fail("fast Ctrl-b d did not detach the current client")

        assert tmux("has-session", "-t", f"={session_name}", check=False).returncode == 0
    finally:
        tmux("kill-server", check=False)
        if client_pid is not None:
            waited_pid, _status = os.waitpid(client_pid, os.WNOHANG)
            if waited_pid == 0:
                os.kill(client_pid, signal.SIGTERM)
                os.waitpid(client_pid, 0)
        if terminal_master is not None:
            os.close(terminal_master)


def test_real_tmux_ctrl_b_banner_tracks_the_waiting_prefix_state(
    tmp_path: Path,
) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"
    session_name = "visible-prefix-mode"
    client_pid: int | None = None
    terminal_master: int | None = None

    def tmux(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [tmux_binary, "-S", str(socket_path), *arguments],
            check=check,
            text=True,
            capture_output=True,
        )

    tmux("new-session", "-d", "-s", session_name, "sleep 30")
    try:
        RodexRuntimeLauncher("codex", tmux_binary).initialise_session_ui(
            _register_real_tmux_session(tmux, socket_path, session_name)
        )
        client_pid, terminal_master = pty.fork()
        if client_pid == 0:
            environment = os.environ.copy()
            environment["TERM"] = "xterm-256color"
            os.execve(
                tmux_binary,
                [
                    tmux_binary,
                    "-S",
                    str(socket_path),
                    "attach-session",
                    "-t",
                    f"={session_name}",
                ],
                environment,
            )

        deadline = time.monotonic() + 2
        client_name = ""
        while time.monotonic() < deadline:
            clients = tmux(
                "list-clients",
                "-t",
                f"={session_name}",
                "-F",
                "#{client_name}",
            )
            client_name = clients.stdout.strip()
            if client_name:
                break
            time.sleep(0.01)
        else:
            pytest.fail("tmux client did not attach")

        os.write(terminal_master, b"\x02")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rendered = tmux(
                "list-clients",
                "-t",
                f"={session_name}",
                "-F",
                "#{client_prefix}|#{T:status-left}",
            )
            if rendered.stdout.startswith("1|") and "CTRL-B MODE" in rendered.stdout:
                break
            time.sleep(0.01)
        else:
            pytest.fail("Ctrl-b did not expose the waiting prefix banner")

        os.write(terminal_master, b"d")
        assert tmux("has-session", "-t", f"={session_name}", check=False).returncode == 0
    finally:
        tmux("kill-server", check=False)
        if client_pid is not None:
            waited_pid, _status = os.waitpid(client_pid, os.WNOHANG)
            if waited_pid == 0:
                os.kill(client_pid, signal.SIGTERM)
                os.waitpid(client_pid, 0)
        if terminal_master is not None:
            os.close(terminal_master)


def test_rodex_does_not_override_user_mouse_preferences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RuntimeRunner(tmp_path)
    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    runtime = LiveTmuxSession(
        tmp_path / "tmux.sock",
        "automatic-beluga",
        runtime_id=RUNTIME_ID,
    )
    _mock_registered_ui_capability(
        monkeypatch,
        launcher,
        runner,
        tmp_path / "tmux.sock",
    )

    launcher._start_tmux_session(runtime, tmp_path, "sleep 30")
    launcher.initialise_session_ui(runtime)

    assert not any("mouse" in command for command in runner.calls)


def test_real_tmux_creation_refuses_an_unmarked_nonempty_server_without_mutation(
    tmp_path: Path,
) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"

    def tmux(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [tmux_binary, "-S", str(socket_path), *arguments],
            check=check,
            text=True,
            capture_output=True,
        )

    tmux("new-session", "-d", "-s", "foreign-owner", "sleep 30")
    tmux("set-option", "-g", "history-limit", "1234")
    launcher = RodexRuntimeLauncher("codex", tmux_binary)
    runtime = LiveTmuxSession(
        socket_path,
        "rodex-attempt",
        runtime_id=RUNTIME_ID,
    )

    try:
        with pytest.raises(RodexRuntimeError):
            launcher._start_tmux_session(runtime, tmp_path, "sleep 30")

        assert tmux("list-sessions", "-F", "#{session_name}").stdout.splitlines() == [
            "foreign-owner"
        ]
        assert tmux("show-options", "-gv", "history-limit").stdout.strip() == "1234"
        for option in (
            RODEX_SHARED_TMUX_PROTOCOL_OPTION,
            RODEX_SHARED_TMUX_SERVER_ID_OPTION,
        ):
            assert (
                tmux("show-options", "-s", "-v", option, check=False).stdout.strip() == ""
            )
    finally:
        tmux("kill-server", check=False)


@pytest.mark.parametrize("user_virtualenv_is_active", [False, True])
def test_real_tmux_session_replaces_stale_rodex_environment_with_caller_state(
    tmp_path: Path,
    user_virtualenv_is_active: bool,
) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"
    stale_environment = os.environ.copy()
    stale_environment.update(
        {
            "PATH": f"{Path(sys.prefix) / 'bin'}:/usr/bin",
            "VIRTUAL_ENV": str(Path(sys.prefix)),
            "VIRTUAL_ENV_PROMPT": "(rodex)",
            "UV_RUN_RECURSION_DEPTH": "1",
        }
    )
    subprocess.run(
        [
            tmux_binary,
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "stale-environment-owner",
            "sleep 30",
        ],
        check=True,
        env=stale_environment,
    )
    capability = _registered_capability(socket_path)
    for option_name, value in (
        (RODEX_SHARED_TMUX_PROTOCOL_OPTION, RODEX_SHARED_TMUX_PROTOCOL),
        (RODEX_SHARED_TMUX_SERVER_ID_OPTION, capability.tmux_server_id),
    ):
        subprocess.run(
            [tmux_binary, "-S", str(socket_path), "set-option", "-s", option_name, value],
            check=True,
        )
    caller_environment = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin",
        "TERM": "xterm-256color",
    }
    expected_virtual_environment: str | None = None
    if user_virtualenv_is_active:
        expected_virtual_environment = str(tmp_path / "project-xyz" / ".venv")
        caller_environment.update(
            {
                "PATH": f"{expected_virtual_environment}/bin:/usr/bin",
                "VIRTUAL_ENV": expected_virtual_environment,
                "VIRTUAL_ENV_PROMPT": "(project-xyz)",
                "UV_RUN_RECURSION_DEPTH": "7",
            }
        )
    probe_path = tmp_path / "managed-environment.json"
    probe_source = (
        "import json, os, sys, time; from pathlib import Path; "
        "names = ('PATH', 'VIRTUAL_ENV', 'VIRTUAL_ENV_PROMPT', "
        "'UV_RUN_RECURSION_DEPTH'); "
        "Path(sys.argv[1]).write_text(json.dumps({name: os.environ.get(name) "
        "for name in names})); time.sleep(10)"
    )
    launcher = RodexRuntimeLauncher(
        "codex",
        tmux_binary,
        python_executable=sys.executable,
        environment=caller_environment,
    )
    runtime = LiveTmuxSession(
        socket_path,
        "caller-environment",
        runtime_id=RUNTIME_ID,
    )

    try:
        launcher._start_tmux_session(
            runtime,
            tmp_path,
            shlex.join((sys.executable, "-c", probe_source, str(probe_path))),
        )
        deadline = time.monotonic() + 2
        while not probe_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert probe_path.exists()
        observed = json.loads(probe_path.read_text(encoding="utf-8"))
        assert observed["PATH"] == caller_environment["PATH"]
        assert observed["VIRTUAL_ENV"] == expected_virtual_environment
        assert observed["VIRTUAL_ENV_PROMPT"] == (
            "(project-xyz)" if user_virtualenv_is_active else None
        )
        assert observed["UV_RUN_RECURSION_DEPTH"] == (
            "7" if user_virtualenv_is_active else None
        )
        session_virtual_environment = subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "show-environment",
                "-t",
                "=caller-environment",
                "VIRTUAL_ENV",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert session_virtual_environment == (
            f"VIRTUAL_ENV={expected_virtual_environment}"
            if user_virtualenv_is_active
            else "-VIRTUAL_ENV"
        )
    finally:
        subprocess.run(
            [tmux_binary, "-S", str(socket_path), "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def test_real_tmux_session_preserves_scrollback_with_mouse_disabled(
    tmp_path: Path,
) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"
    runtime = LiveTmuxSession(
        socket_path,
        "rodex-scrollback",
        runtime_id=RUNTIME_ID,
    )
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
        launcher._stop_startup_runtime(runtime)


def test_real_tmux_mouse_identity_survives_rename_and_old_name_reuse(
    tmp_path: Path,
) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"
    original = LiveRodexRuntime(
        socket_path,
        "alpha",
        tmp_path / "app.sock",
        tmp_path / "app.log",
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
        runtime_id=RUNTIME_ID,
    )
    launcher = RodexRuntimeLauncher("codex", tmux_binary)

    def tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [tmux_binary, "-S", str(socket_path), *arguments],
            check=True,
            text=True,
            capture_output=True,
        )

    def mouse_for(name: str) -> str:
        return tmux("show-options", "-A", "-v", "-t", f"={name}:", "mouse").stdout.strip()

    tmux("new-session", "-d", "-s", "alpha", "sleep 30")
    try:
        tmux("new-session", "-d", "-s", "beta", "sleep 30")
        capability = _registered_capability(socket_path)
        tmux(
            "set-option",
            "-s",
            RODEX_SHARED_TMUX_PROTOCOL_OPTION,
            RODEX_SHARED_TMUX_PROTOCOL,
        )
        tmux(
            "set-option",
            "-s",
            RODEX_SHARED_TMUX_SERVER_ID_OPTION,
            capability.tmux_server_id,
        )
        tmux("set-option", "-t", "=alpha:", "@rodex_runtime_id", str(RUNTIME_ID))
        primary_pane_id = tmux(
            "display-message", "-p", "-t", "=alpha:", "-F", "#{pane_id}"
        ).stdout.strip()
        tmux(
            "set-option",
            "-t",
            "=alpha:",
            "@rodex_primary_pane_id",
            primary_pane_id,
        )
        codex_session_id = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
        rodex_session_id = RodexSessionId.parse("1111111111111111")
        registry_id = RodexRegistryId.parse("2222222222222222")
        launcher.publish_runtime_control(
            original,
            codex_session_id,
            rodex_session_id,
            registry_id,
        )
        launcher.confirm_runtime_registration(
            original,
            7,
            expected_rodex_session_id=rodex_session_id,
            expected_registry_id=registry_id,
            expected_codex_session_id=codex_session_id,
        )
        tmux("rename-session", "-t", "=alpha", "gamma")
        tmux("rename-session", "-t", "=beta", "alpha")

        stale_name = LiveTmuxSession(
            socket_path,
            "alpha",
            runtime_id=RUNTIME_ID,
        )
        assert launcher.set_mouse_mode(stale_name, "on") == "on"
        assert mouse_for("gamma") == "on"
        assert mouse_for("alpha") == "off"
    finally:
        subprocess.run(
            [tmux_binary, "-S", str(socket_path), "kill-server"],
            check=False,
            text=True,
            capture_output=True,
        )


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
        runtime_id=RUNTIME_ID,
    )
    launcher = RodexRuntimeLauncher("codex", tmux_binary)
    codex_session_id = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
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

    # Keep one session alive while consuming two pane IDs. The managed pane is then
    # `%3`, which tmux must compare as literal identity data inside display-message.
    subprocess.run(
        [
            tmux_binary,
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "pane-id-anchor",
            "sleep 30",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    for dummy_name in ("pane-id-one", "pane-id-two"):
        subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "new-session",
                "-d",
                "-s",
                dummy_name,
                "sleep 30",
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
                "kill-session",
                "-t",
                f"={dummy_name}",
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
            original.tmux_session_name,
            "sleep 30",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    try:
        capability = _registered_capability(socket_path)
        subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "set-option",
                "-s",
                RODEX_SHARED_TMUX_PROTOCOL_OPTION,
                RODEX_SHARED_TMUX_PROTOCOL,
            ],
            check=True,
        )
        subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "set-option",
                "-s",
                RODEX_SHARED_TMUX_SERVER_ID_OPTION,
                capability.tmux_server_id,
            ],
            check=True,
        )
        subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "set-option",
                "-t",
                f"={original.tmux_session_name}:",
                "@rodex_runtime_id",
                str(RUNTIME_ID),
            ],
            check=True,
        )
        primary_pane_id = subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "display-message",
                "-p",
                "-t",
                f"={original.tmux_session_name}:",
                "-F",
                "#{pane_id}",
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        assert primary_pane_id == "%3"
        subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "set-option",
                "-t",
                f"={original.tmux_session_name}:",
                "@rodex_primary_pane_id",
                primary_pane_id,
            ],
            check=True,
        )
        rodex_session_id = RodexSessionId.parse("1111111111111111")
        registry_id = RodexRegistryId.parse("2222222222222222")
        launcher.publish_runtime_control(
            original,
            codex_session_id,
            rodex_session_id,
            registry_id,
        )
        launcher.confirm_runtime_registration(
            original,
            7,
            expected_rodex_session_id=rodex_session_id,
            expected_registry_id=registry_id,
            expected_codex_session_id=codex_session_id,
        )
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
        launcher.initialise_session_ui(renamed)

        assert launcher.session_exists(renamed)
        assert session_option("mouse") == ""
        assert session_option("mouse", inherited=True) == "on"
        assert launcher.set_mouse_mode(renamed, "status") == "on"
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
        ctrl_c_binding = subprocess.run(
            [
                tmux_binary,
                "-S",
                str(socket_path),
                "list-keys",
                "-T",
                "root",
                "C-c",
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        assert "rodex.tmux_shared_ctrl_c" in ctrl_c_binding.stdout
        discovered = launcher.discover_runtime_control(renamed)
        assert discovered.protocol_proxy_socket_path == tmp_path / "proxy.sock"
        assert discovered.protocol_event_socket_path == tmp_path / "events.sock"
        assert discovered.codex_session_id == codex_session_id
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
        assert "Mouse: #{?mouse,ON,OFF}" in shown_status.stdout
        assert "@rodex_context_status" in shown_status.stdout
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
        assert session_option("status-style") == RODEX_STATUS_STYLE
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
        assert session_option("status-style") == RODEX_STATUS_STYLE
        rendered_identity_status = tmux_format("#{T:status-left}")
        assert "Rodex: automatic-beluga" in rendered_identity_status
        assert "Mouse: ON" in rendered_identity_status
        assert "Context: --" in rendered_identity_status
        assert launcher.set_mouse_mode(renamed, "off") == "off"
        assert "Mouse: OFF" in tmux_format("#{T:status-left}")
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
        runtime_module.secrets,
        "token_hex",
        lambda size: "0123456789abcdef" if size == 8 else "0123456789abcdef" * 2,
    )
    runner = RuntimeRunner(tmp_path)
    connector = RecordingConnector(
        [
            "01a00654-f2bc-7a30-834a-a5f886a65f82",
            "01a00656-4c5e-7eb0-ae87-4765725b6f00",
        ]
    )
    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner, connector=connector)
    monkeypatch.setattr(launcher, "_stable_tmux_session_target", lambda _runtime: "$7")

    with pytest.raises(RodexRuntimeError, match="more than one"):
        launcher.start(tmp_path, [], runtime_id=RUNTIME_ID)

    assert runner.calls[-1][3:8] == ["if-shell", "-t", "%9", "-F", runner.calls[-1][7]]
    assert "kill-session -t '$7'" in runner.calls[-1][-2]
    assert str(RUNTIME_ID) in runner.calls[-1][7]


def test_configured_runtime_path_must_fit_a_unix_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_root = tmp_path / ("x" * 100)
    monkeypatch.setenv("RODEX_RUNTIME_DIR", str(long_root))

    launcher = RodexRuntimeLauncher("codex", "tmux")
    with pytest.raises(RodexRuntimeError, match="too long"):
        launcher.start(tmp_path, [], runtime_id=RUNTIME_ID)


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
    monkeypatch: pytest.MonkeyPatch,
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
        runtime_id=RUNTIME_ID,
    )
    codex_session_id = uuid.uuid4()
    rodex_session_id = RodexSessionId.parse("0123456789abcdef")
    registry_id = RodexRegistryId.generate()

    launcher.publish_runtime_control(
        runtime, codex_session_id, rodex_session_id, registry_id
    )
    monkeypatch.setattr(
        launcher,
        "_stable_tmux_session_target",
        lambda _runtime: "$7",
    )
    monkeypatch.setattr(
        launcher,
        "discover_runtime_control",
        lambda _runtime: LiveRodexControl(
            tmp_path / "proxy.sock",
            tmp_path / "events.sock",
            codex_session_id,
            rodex_session_id,
            registry_id,
            "pending",
            RUNTIME_ID,
        ),
    )
    confirmed = TmuxSessionCapability(
        tmp_path / "tmux.sock",
        "0123456789abcdef0123456789abcdef",
        "$7",
        "%9",
        RUNTIME_ID,
        rodex_session_id,
        registry_id,
        41,
        codex_session_id,
    )
    monkeypatch.setattr(
        launcher,
        "_resolve_registered_tmux_capability",
        lambda _runtime: confirmed,
    )
    launcher.confirm_runtime_registration(
        runtime,
        41,
        expected_rodex_session_id=rodex_session_id,
        expected_registry_id=registry_id,
        expected_codex_session_id=codex_session_id,
    )

    options = [command[3:] for command in runner.calls]
    published = next(
        command
        for command in options
        if command[:3] == ["if-shell", "-t", "%9"]
        and "@rodex_protocol_proxy_socket_path" in command[-2]
    )
    publish_action = published[-2]
    publish_condition = published[4]
    assert publish_action.count(";") == 6
    for expected in (
        confirmed.tmux_server_id,
        confirmed.tmux_session_id,
        confirmed.tmux_primary_pane_id,
        str(confirmed.runtime_id),
    ):
        assert expected in publish_condition
    for expected in (
        str(runtime.runtime_id),
        str(rodex_session_id),
        str(registry_id),
        str(codex_session_id),
        "pending",
    ):
        assert expected in publish_action
    registration_cas = options[-1]
    assert registration_cas[:3] == ["if-shell", "-t", "%9"]
    assert str(runtime.runtime_id) in registration_cas[-2]
    assert str(rodex_session_id) in registration_cas[-2]
    assert str(registry_id) in registration_cas[-2]
    assert str(codex_session_id) in registration_cas[-2]
    assert "@rodex_sessions_id 41" in registration_cas[-1]
    assert "@rodex_registration_state registered" in registration_cas[-1]


def test_runtime_discovery_reads_the_complete_current_identity_tuple(
    tmp_path: Path,
) -> None:
    capability = TmuxSessionCapability(
        tmp_path / "tmux.sock",
        "0123456789abcdef0123456789abcdef",
        "$7",
        "%9",
        RUNTIME_ID,
        RodexSessionId.parse("0123456789abcdef"),
        RodexRegistryId.parse("06179a3581264d53"),
        41,
        uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
    )
    snapshot = "\t".join(
        (
            RODEX_SHARED_TMUX_PROTOCOL,
            capability.tmux_server_id,
            capability.tmux_session_id,
            capability.tmux_primary_pane_id,
            str(tmp_path / "proxy.sock"),
            str(tmp_path / "events.sock"),
            str(capability.codex_session_id),
            str(capability.rodex_session_id),
            str(capability.registry_id),
            str(capability.internal_session_id),
            "registered",
            str(capability.runtime_id),
        )
    )

    calls: list[list[str]] = []

    def read_option(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=f"{snapshot}\n", stderr="")

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=read_option)

    control = launcher.discover_runtime_control(
        LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga")
    )

    assert control == LiveRodexControl(
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
        uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
        RodexSessionId.parse("0123456789abcdef"),
        RodexRegistryId.parse("06179a3581264d53"),
        "registered",
        RUNTIME_ID,
        capability,
    )
    assert len(calls) == 1
    assert calls[0][3:8] == [
        "display-message",
        "-p",
        "-t",
        "=automatic-beluga:",
        "-F",
    ]
    identity_format = calls[0][-1]
    for marker in (
        RODEX_SHARED_TMUX_PROTOCOL_OPTION,
        RODEX_SHARED_TMUX_SERVER_ID_OPTION,
        "#{session_id}",
        "@rodex_runtime_id",
        "@rodex_registry_id",
        "@rodex_sessions_id",
        "@rodex_codex_session_id",
    ):
        assert marker in identity_format


def test_full_capability_stop_rejects_a_restarted_tmux_server_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    capability = _registered_capability(tmp_path / "tmux.sock")
    runtime = LiveTmuxSession(
        tmp_path / "tmux.sock",
        "automatic-beluga",
        runtime_id=capability.runtime_id,
        tmux_capability=capability,
    )
    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    monkeypatch.setattr(
        launcher,
        "_read_tmux_server_option",
        lambda _runtime, option: (
            RODEX_SHARED_TMUX_PROTOCOL
            if option == RODEX_SHARED_TMUX_PROTOCOL_OPTION
            else "fedcba9876543210fedcba9876543210"
        ),
    )

    with pytest.raises(RodexRuntimeError, match="server incarnation changed"):
        launcher.stop(runtime)

    assert calls == []


def test_full_capability_stop_rejects_a_changed_registered_tuple_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    capability = _registered_capability(tmp_path / "tmux.sock")
    changed = TmuxSessionCapability(
        capability.tmux_server_socket_path,
        capability.tmux_server_id,
        capability.tmux_session_id,
        capability.tmux_primary_pane_id,
        capability.runtime_id,
        capability.rodex_session_id,
        RodexRegistryId(capability.registry_id.value ^ 1),
        capability.internal_session_id,
        capability.codex_session_id,
    )
    runtime = LiveTmuxSession(
        tmp_path / "tmux.sock",
        "automatic-beluga",
        runtime_id=capability.runtime_id,
        tmux_capability=capability,
    )
    launcher = RodexRuntimeLauncher("codex", "tmux", runner=runner)
    monkeypatch.setattr(
        launcher,
        "_read_tmux_server_option",
        lambda _runtime, option: (
            RODEX_SHARED_TMUX_PROTOCOL
            if option == RODEX_SHARED_TMUX_PROTOCOL_OPTION
            else capability.tmux_server_id
        ),
    )
    monkeypatch.setattr(
        launcher,
        "_list_registered_tmux_capabilities",
        lambda _runtime, _server_id: (changed,),
    )

    with pytest.raises(RodexRuntimeError, match="was not found"):
        launcher.stop(runtime)

    assert calls == []


def test_runtime_discovery_rejects_a_noncanonical_runtime_id(
    tmp_path: Path,
) -> None:
    snapshot = "\t".join(
        (
            RODEX_SHARED_TMUX_PROTOCOL,
            "0123456789abcdef0123456789abcdef",
            "$7",
            "%9",
            str(tmp_path / "proxy.sock"),
            str(tmp_path / "events.sock"),
            "01a00654-f2bc-7a30-834a-a5f886a65f82",
            "",
            "",
            "",
            "pending",
            "0C01EE2EAD7240E1",
        )
    )

    def read_option(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=f"{snapshot}\n", stderr="")

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=read_option)

    with pytest.raises(RodexRuntimeError, match="invalid runtime ID"):
        launcher.discover_runtime_control(
            LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga")
        )


@pytest.mark.parametrize(
    "invalid_session_id",
    ["0123456789abcde", "0123456789abcdeF", "01234567-89abcdef"],
)
def test_runtime_discovery_rejects_a_noncanonical_session_id_marker(
    tmp_path: Path,
    invalid_session_id: str,
) -> None:
    snapshot = "\t".join(
        (
            RODEX_SHARED_TMUX_PROTOCOL,
            "0123456789abcdef0123456789abcdef",
            "$7",
            "%9",
            str(tmp_path / "proxy.sock"),
            str(tmp_path / "events.sock"),
            "01a00654-f2bc-7a30-834a-a5f886a65f82",
            invalid_session_id,
            "06179a3581264d53",
            "",
            "pending",
            str(RUNTIME_ID),
        )
    )

    def read_option(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=f"{snapshot}\n", stderr="")

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=read_option)
    runtime = LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga")

    with pytest.raises(RodexRuntimeError, match="invalid Rodex session ID"):
        launcher.discover_runtime_control(runtime)


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


def test_registered_analytics_config_binds_the_committed_identity_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_session_id = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
    rodex_session_id = RodexSessionId.parse("0123456789abcdef")
    registry_id = RodexRegistryId.parse("06179a3581264d53")
    event_socket = tmp_path / "events.sock"
    pending = AnalyticsWorkerConfig(
        rodex_database_path=tmp_path / "rodex.sqlite3",
        codex_sessions_root=tmp_path / "sessions",
        rodex_session_id=rodex_session_id,
        rodex_registry_id=registry_id,
        runtime_id=RUNTIME_ID,
        protocol_event_socket_path=event_socket,
    )
    output = "\n".join(
        (
            f"@rodex_session_id {rodex_session_id}",
            f"@rodex_registry_id {registry_id}",
            f"@rodex_runtime_id {RUNTIME_ID}",
            f"@rodex_protocol_event_socket_path {event_socket}",
            f"@rodex_codex_session_id {codex_session_id}",
            "@rodex_sessions_id 41",
            "@rodex_registration_state registered",
        )
    )
    observed: list[list[str]] = []

    def run_tmux(
        command: list[str], **_options: object
    ) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(runtime_module.subprocess, "run", run_tmux)

    activated = runtime_module._registered_analytics_worker_config(
        pending,
        "/usr/bin/tmux",
        tmp_path / "tmux.sock",
        "%4",
    )

    assert activated is not None
    assert activated.rodex_sessions_id == 41
    assert activated.codex_session_id == codex_session_id
    assert activated.runtime_id == RUNTIME_ID
    assert observed == [
        [
            "/usr/bin/tmux",
            "-S",
            str(tmp_path / "tmux.sock"),
            "show-options",
            "-t",
            "%4",
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
        (
            ["--model", "gpt-5.6-sol", "Project: CODEX_TMUX_SESSION_MANAGER"],
            False,
        ),
    ],
)
def test_session_host_skips_updater_and_connects_tui_through_protocol_proxy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codex_arguments: list[str],
    captures_stderr: bool,
) -> None:
    initialise_rodex_database(tmp_path / "rodex.sqlite3")
    app_socket = tmp_path / "app.sock"
    proxy_socket = tmp_path / "proxy.sock"
    event_socket = tmp_path / "events.sock"
    tmux_socket = tmp_path / "tmux.sock"
    tui_commands: list[list[str]] = []
    tui_options: list[dict[str, object]] = []
    spawned_environments: list[dict[str, str]] = []
    status_updates: list[int] = []
    proxy_lifecycle: list[str] = []
    protected_paths: tuple[Path, ...] | None = None
    tmux_runtime_capability = TmuxRuntimeCapability(
        tmux_socket,
        "0123456789abcdef0123456789abcdef",
        "$7",
        "%9",
        RUNTIME_ID,
    )

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
            assert args == ("/usr/bin/tmux", tmux_runtime_capability, "%4")

        def update(self, count: int) -> None:
            status_updates.append(count)

    class FakeContextStatus:
        def __init__(self, *args: object) -> None:
            assert args == ("/usr/bin/tmux", tmux_runtime_capability, "%4")

        def update(self, rendered_status: str) -> None:
            assert "Context: --" in rendered_status

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

    class FakeAgentObserver:
        def __init__(self, *args: object) -> None:
            assert args == (
                "/usr/bin/tmux",
                tmux_runtime_capability,
                "%4",
                event_socket,
            )

        def observe_protocol_event(self, _event: object) -> None:
            return None

        def close(self) -> None:
            proxy_lifecycle.append("observer-close")

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
        process_environment = options.get("env")
        assert isinstance(process_environment, dict)
        spawned_environments.append(process_environment.copy())
        if "app-server" not in command:
            tui_commands.append(command)
            tui_options.append(options)
        return FakeProcess(command)

    rodex_virtual_environment = Path(sys.prefix)
    monkeypatch.setenv(
        "PATH",
        f"{rodex_virtual_environment / 'bin'}:/usr/bin",
    )
    monkeypatch.setenv("VIRTUAL_ENV", str(rodex_virtual_environment))
    monkeypatch.setenv("VIRTUAL_ENV_PROMPT", "(rodex)")
    monkeypatch.setenv("UV_RUN_RECURSION_DEPTH", "1")
    monkeypatch.setenv("USER_SETTING", "preserved")
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(runtime_module.subprocess, "Popen", start_process)
    monkeypatch.setattr(
        runtime_module,
        "_wait_for_app_server_socket",
        lambda *_args: app_socket.touch(),
    )
    monkeypatch.setattr(runtime_module, "TmuxToolCallStatus", FakeStatus)
    monkeypatch.setattr(runtime_module, "TmuxContextStatus", FakeContextStatus)
    monkeypatch.setattr(runtime_module, "CodexProtocolEventTap", FakeEventTap)
    monkeypatch.setattr(runtime_module, "CodexProtocolProxy", FakeProxy)
    monkeypatch.setattr(
        runtime_module,
        "AgentObserverCoordinator",
        FakeAgentObserver,
    )
    monkeypatch.setattr(runtime_module, "_RuntimePathKeepalive", FakeKeepalive)
    monkeypatch.setattr(
        runtime_module,
        "_resolve_session_host_tmux_capability",
        lambda *_args: tmux_runtime_capability,
    )
    monkeypatch.setattr(
        runtime_module,
        "_registered_analytics_worker_config",
        lambda *_args: None,
    )
    signal_changes: list[tuple[int, bool]] = []

    def record_signal_change(signum: int, handler: object) -> object:
        signal_changes.append((signum, callable(handler)))
        proxy_lifecycle.append("signal-install" if callable(handler) else "signal-restore")
        return runtime_module.signal.SIG_DFL

    monkeypatch.setattr(runtime_module.signal, "signal", record_signal_change)

    assert (
        run_session_host(
            SessionHostConfig(
                codex_binary="/usr/bin/codex",
                app_server_socket_path=app_socket,
                app_server_log_path=tmp_path / "app.log",
                protocol_proxy_socket_path=proxy_socket,
                protocol_event_socket_path=event_socket,
                tmux_binary="/usr/bin/tmux",
                tmux_server_socket_path=tmux_socket,
                runtime_id=RUNTIME_ID,
                codex_arguments=tuple(codex_arguments),
                analytics=AnalyticsWorkerConfig(
                    rodex_database_path=tmp_path / "rodex.sqlite3",
                    codex_sessions_root=tmp_path / "sessions",
                    rodex_session_id=RodexSessionId(1),
                    rodex_registry_id=RodexRegistryId.parse("0000000000000001"),
                    runtime_id=RUNTIME_ID,
                    protocol_event_socket_path=event_socket,
                ),
            ),
            analytics_supervisor_factory=FailingAnalyticsSupervisor,
        )
        == 0
    )

    assert status_updates == [0]
    assert signal_changes == [
        (runtime_module.signal.SIGHUP, True),
        (runtime_module.signal.SIGTERM, True),
        (runtime_module.signal.SIGINT, True),
        (runtime_module.signal.SIGHUP, False),
        (runtime_module.signal.SIGTERM, False),
        (runtime_module.signal.SIGINT, False),
    ]
    assert proxy_lifecycle == [
        "signal-install",
        "signal-install",
        "event-start",
        "start",
        "keepalive-start",
        "signal-install",
        "keepalive-close",
        "close",
        "observer-close",
        "event-close",
        "signal-restore",
        "signal-restore",
        "signal-restore",
    ]
    assert proxy_lifecycle.index("close") < proxy_lifecycle.index("observer-close")
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
            "--config",
            "check_for_update_on_startup=false",
            "--no-alt-screen",
            "--remote",
            f"unix://{proxy_socket}",
            *codex_arguments,
        ]
    ]
    assert len(tui_options) == 1
    assert len(spawned_environments) == 2
    for process_environment in spawned_environments:
        assert process_environment["PATH"] == "/usr/bin"
        assert process_environment["USER_SETTING"] == "preserved"
        assert "VIRTUAL_ENV" not in process_environment
        assert "VIRTUAL_ENV_PROMPT" not in process_environment
        assert "UV_RUN_RECURSION_DEPTH" not in process_environment
    if captures_stderr:
        captured_stderr = tui_options[0].get("stderr")
        assert captured_stderr is not None
        assert cast(BinaryIO, captured_stderr).closed
        assert not (tmp_path / "app.log").exists()
    else:
        assert set(tui_options[0]) == {"env"}


def test_session_host_retries_exact_resume_during_active_writer_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_codex_session_id = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
    app_socket = tmp_path / "app.sock"
    tui_launches = 0
    retry_waits: list[float] = []
    primary_release_waits: list[float] = []

    class FakeProcess:
        def __init__(self, kind: str, returncode: int | None) -> None:
            self.kind = kind
            self.returncode = returncode

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    def start_process(command: list[str], **options: object) -> FakeProcess:
        nonlocal tui_launches
        if "app-server" in command:
            return FakeProcess("app", None)
        assert options.get("stderr") is not None
        tui_launches += 1
        return FakeProcess("tui", 1 if tui_launches == 1 else 0)

    class FakeStatus:
        def __init__(self, *args: object) -> None:
            return None

        def update(self, count: int) -> None:
            assert count == 0

    class FakeContextStatus:
        def __init__(self, *args: object) -> None:
            return None

        def update(self, rendered_status: str) -> None:
            assert "Context: --" in rendered_status

    class FakeEventTap:
        def __init__(self, _path: Path) -> None:
            return None

        def start(self) -> None:
            return None

        def publish(self, _message: str | bytes) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeProxy:
        def __init__(self, *args: object) -> None:
            return None

        def start(self) -> None:
            return None

        def wait_for_primary_connection_release(self, timeout_seconds: float) -> None:
            primary_release_waits.append(timeout_seconds)

        def close(self) -> None:
            return None

    class FakeKeepalive:
        failure: RodexRuntimeError | None = None

        def __init__(self, _paths: tuple[Path, ...]) -> None:
            return None

        def start(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(runtime_module.subprocess, "Popen", start_process)
    monkeypatch.setattr(
        runtime_module,
        "_wait_for_app_server_socket",
        lambda *_args: app_socket.touch(),
    )
    monkeypatch.setattr(runtime_module, "TmuxToolCallStatus", FakeStatus)
    monkeypatch.setattr(runtime_module, "TmuxContextStatus", FakeContextStatus)
    monkeypatch.setattr(runtime_module, "CodexProtocolEventTap", FakeEventTap)
    monkeypatch.setattr(runtime_module, "CodexProtocolProxy", FakeProxy)
    monkeypatch.setattr(runtime_module, "_RuntimePathKeepalive", FakeKeepalive)
    monkeypatch.setattr(
        runtime_module,
        "_resolve_session_host_tmux_capability",
        lambda *_args: TmuxRuntimeCapability(
            tmp_path / "tmux.sock",
            "0123456789abcdef0123456789abcdef",
            "$7",
            "%9",
            RUNTIME_ID,
        ),
    )
    monkeypatch.setattr(
        runtime_module,
        "_read_runtime_log_since",
        lambda *_args: (
            f"thread-store conflict: thread {requested_codex_session_id} "
            "already has an active writer"
        ),
    )
    monkeypatch.setattr(runtime_module.time, "sleep", retry_waits.append)

    assert (
        run_session_host(
            SessionHostConfig(
                codex_binary="/usr/bin/codex",
                app_server_socket_path=app_socket,
                app_server_log_path=tmp_path / "app.log",
                protocol_proxy_socket_path=tmp_path / "proxy.sock",
                protocol_event_socket_path=tmp_path / "events.sock",
                tmux_binary="/usr/bin/tmux",
                tmux_server_socket_path=tmp_path / "tmux.sock",
                runtime_id=RUNTIME_ID,
                codex_arguments=("resume", str(requested_codex_session_id)),
            )
        )
        == 0
    )

    assert tui_launches == 2
    assert primary_release_waits == [
        runtime_module.CODEX_PRIMARY_CONNECTION_RELEASE_TIMEOUT_SECONDS
    ]
    assert retry_waits == [runtime_module.CODEX_ACTIVE_WRITER_RETRY_INTERVAL_SECONDS]


def test_active_writer_handoff_retry_requires_exact_thread_and_open_window() -> None:
    requested_codex_session_id = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
    detail = (
        f"thread-store conflict: thread {requested_codex_session_id} "
        "already has an active writer"
    )

    assert runtime_module._active_writer_handoff_can_retry(
        detail,
        requested_codex_session_id,
        now=9.9,
        deadline=10.0,
    )
    assert not runtime_module._active_writer_handoff_can_retry(
        detail,
        uuid.UUID(int=requested_codex_session_id.int + 1),
        now=9.9,
        deadline=10.0,
    )
    assert not runtime_module._active_writer_handoff_can_retry(
        detail,
        requested_codex_session_id,
        now=10.0,
        deadline=10.0,
    )


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

    class FakeContextStatus:
        def __init__(self, *args: object) -> None:
            return None

        def update(self, rendered_status: str) -> None:
            assert "Context: --" in rendered_status

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
            callback = getattr(self, "failure_callback", None)
            if callback is not None:
                callback(self.failure)

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
    monkeypatch.setattr(runtime_module, "TmuxContextStatus", FakeContextStatus)
    monkeypatch.setattr(runtime_module, "CodexProtocolEventTap", FakeEventTap)
    monkeypatch.setattr(runtime_module, "CodexProtocolProxy", FakeProxy)
    monkeypatch.setattr(runtime_module, "_RuntimePathKeepalive", FailingKeepalive)
    monkeypatch.setattr(
        runtime_module,
        "_resolve_session_host_tmux_capability",
        lambda *_args: TmuxRuntimeCapability(
            tmp_path / "tmux.sock",
            "0123456789abcdef0123456789abcdef",
            "$7",
            "%9",
            RUNTIME_ID,
        ),
    )

    with pytest.raises(RodexRuntimeError, match=r"lost proxy\.sock"):
        run_session_host(
            SessionHostConfig(
                codex_binary="/usr/bin/codex",
                app_server_socket_path=tmp_path / "app.sock",
                app_server_log_path=tmp_path / "app.log",
                protocol_proxy_socket_path=tmp_path / "proxy.sock",
                protocol_event_socket_path=tmp_path / "events.sock",
                tmux_binary="/usr/bin/tmux",
                tmux_server_socket_path=tmp_path / "tmux.sock",
                runtime_id=RUNTIME_ID,
            )
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
