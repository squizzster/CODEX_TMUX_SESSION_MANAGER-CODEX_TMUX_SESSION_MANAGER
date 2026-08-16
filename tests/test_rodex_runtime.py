from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import uuid
from pathlib import Path
from threading import Event, Lock

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

    def start_process(command: list[str], **options: object) -> FakeProcess:
        if "app-server" not in command:
            tui_commands.append(command)
            assert options == {}
        return FakeProcess(command)

    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(runtime_module.subprocess, "Popen", start_process)
    monkeypatch.setattr(runtime_module, "_wait_for_app_server_socket", lambda *args: None)
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
            ["resume", "codex-uuid"],
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
            "--remote",
            f"unix://{proxy_socket}",
            "resume",
            "codex-uuid",
        ]
    ]


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
    monkeypatch.setattr(runtime_module, "_wait_for_app_server_socket", lambda *args: None)
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
