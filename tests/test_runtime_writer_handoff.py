from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from test_managed_startup import (
    RodexTerminalClient,
    _require_startup_prerequisite,
    _retained_standalone_codex_thread,
    _stop_fixture_daemon,
)
from test_rodex_runtime import RUNTIME_ID, _create_socket_path, _mock_terminal_gateway

import rodex.runtime as runtime_module
from rodex.process_contracts import AnalyticsRuntimeConfig, RuntimeServiceConfig
from rodex.tmux_session_capability import RODEX_TMUX_SOCKET_PATTERN, TmuxRuntimeCapability
from rodex_registry import RodexRegistryId, RodexSessionId

THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
CONFLICT = f"thread-store conflict: thread {THREAD_ID} already has an active writer\n"


@pytest.mark.parametrize(
    "scenario,expected_launches,expected_result",
    [
        ("current_exact", 2, 0),
        ("other_thread", 1, 0),
        ("no_marker", 1, 0),
        ("stale_attempt", 1, 0),
        ("registered", 1, 0),
        ("nonexact_arguments", 1, 0),
        ("expired", 1, 1),
        ("persistent", 3, 1),
    ],
)
def test_host_retires_only_exact_unregistered_writer_conflicts_within_its_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, expected_launches: int, expected_result: int
) -> None:
    gateways = _mock_terminal_gateway(monkeypatch)
    app_socket = tmp_path / "app.sock"
    processes = []
    operations = []
    clock = [0.0]
    app_log = None

    class Process:
        def __init__(self, kind: str, attempt: int = 0) -> None:
            self.kind = kind
            self.attempt = attempt
            self.returncode = None
            self.waits = 0

        def poll(self):
            return self.returncode

        def terminate(self):
            operations.append(("terminate", self.kind, self.attempt))
            # The Node wrapper may report success after its signalled child exits.
            # The host must still treat this as a failed resume attempt.
            self.returncode = 0

        def kill(self):
            pytest.fail("fixture processes honor termination")

        def wait(self, timeout=None):
            if self.returncode is not None:
                return self.returncode
            self.waits += 1
            if self.kind == "tui" and self.waits == 1 and (self.attempt == 1 or scenario == "persistent"):
                clock[0] += 11 if scenario == "expired" else 4 if scenario == "persistent" else 0.1
                raise subprocess.TimeoutExpired("still-live-tui", timeout)
            if scenario in {"current_exact", "expired", "persistent"} and self.attempt == 1:
                pytest.fail("host waited again on the live TUI after its exact startup rejection")
            self.returncode = 0
            return self.returncode

    def popen(command, **options):
        nonlocal app_log
        if "app-server" in command:
            app_log = options["stdout"]
            if scenario == "stale_attempt":
                app_log.write(CONFLICT.encode())
                app_log.flush()
            process = Process("app")
        else:
            attempt = 1 + sum(p.kind == "tui" for p in processes)
            process = Process("tui", attempt)
            text = (
                CONFLICT.replace(str(THREAD_ID), str(uuid.UUID(int=THREAD_ID.int + 1)))
                if scenario == "other_thread"
                else "unrelated startup diagnostic\n"
                if scenario in {"no_marker", "stale_attempt"}
                else CONFLICT
            )
            if attempt == 1 or scenario == "persistent":
                app_log.write(text.encode())
                app_log.flush()
        processes.append(process)
        operations.append(("launch", process.kind, process.attempt))
        return process

    class Component:
        failure = None

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def update(self, _value):
            pass

        def activate(self, **_options):
            pass

        def close(self):
            pass

        def wait_for_primary_connection_release(self, _timeout):
            operations.append(("release", "proxy", 0))

    analytics = AnalyticsRuntimeConfig(
        tmux_server_id="a" * 32,
        rodex_database_path=tmp_path / "rodex.sqlite3",
        codex_sessions_root=tmp_path / "sessions",
        rodex_session_id=RodexSessionId(1),
        rodex_registry_id=RodexRegistryId(1),
        runtime_id=RUNTIME_ID,
        protocol_event_socket_path=tmp_path / f"events-{RUNTIME_ID}.sock",
    )
    monkeypatch.setattr(runtime_module.subprocess, "Popen", popen)
    monkeypatch.setattr(runtime_module, "_wait_for_app_server_socket", lambda *_: _create_socket_path(app_socket))
    for name in (
        "TmuxToolCallStatus",
        "TmuxContextStatus",
        "CodexProtocolEventTap",
        "CodexProtocolProxy",
        "_RuntimePathKeepalive",
        "AgentObserverCoordinator",
    ):
        monkeypatch.setattr(runtime_module, name, Component)
    monkeypatch.setattr(
        runtime_module,
        "_resolve_runtime_service_tmux_capability",
        lambda *_: TmuxRuntimeCapability(tmp_path / "tmux.sock", "a" * 32, "$7", "%9", RUNTIME_ID),
    )
    monkeypatch.setattr(
        runtime_module,
        "_registered_analytics_runtime_config",
        lambda *_: (
            replace(analytics, rodex_sessions_id=1, codex_session_id=THREAD_ID) if scenario == "registered" else None
        ),
    )
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime_module.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setenv("TMUX_PANE", "%9")
    arguments = (
        ("resume", str(THREAD_ID), "--unrelated") if scenario == "nonexact_arguments" else ("resume", str(THREAD_ID))
    )
    result = runtime_module.run_runtime_service(
        RuntimeServiceConfig(
            workspace=tmp_path,
            codex_binary="/usr/bin/codex",
            app_server_socket_path=app_socket,
            app_server_log_path=tmp_path / "app.log",
            protocol_proxy_socket_path=tmp_path / f"proxy-{RUNTIME_ID}.sock",
            protocol_event_socket_path=analytics.protocol_event_socket_path,
            tmux_binary="/usr/bin/tmux",
            tmux_server_socket_path=tmp_path / "tmux.sock",
            tmux_pane_target="%9",
            runtime_id=RUNTIME_ID,
            codex_arguments=arguments,
            analytics=analytics,
            user_environment=(("PATH", "/usr/bin"),),
        ),
        terminal_fd=os.dup(0),
        terminal_environment={"TERM": "xterm-256color", "TMUX": f"{tmp_path / 'tmux.sock'},1,0", "TMUX_PANE": "%9"},
        stop=Event(),
    )
    assert result == expected_result
    assert len(gateways) == expected_launches
    assert all(gateway.closed for gateway in gateways)
    releases = [item for item in operations if item[0] == "release"]
    assert len(releases) == expected_launches - 1
    for attempt in range(1, expected_launches):
        assert operations.index(("terminate", "tui", attempt)) < operations.index(("launch", "tui", attempt + 1))
    tui_stops = [item for item in operations if item[:2] == ("terminate", "tui")]
    assert len(tui_stops) == (
        expected_launches if scenario in {"expired", "persistent"} else 1 if scenario == "current_exact" else 0
    )


@pytest.mark.live_startup
def test_real_managed_resume_retries_a_live_tui_after_an_exact_writer_conflict(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> None:
    """Force the writer overlap that an ordinary stop/resume cannot schedule reliably."""
    codex, tmux = shutil.which("codex"), shutil.which("tmux")
    _require_startup_prerequisite(request, codex is not None and tmux is not None, "Codex and tmux are required")
    assert codex is not None and tmux is not None
    project = Path(__file__).parents[1]
    isolated = tmp_path_factory.mktemp("writer-handoff")
    fixture_home = isolated / "codex"
    fixture_home.mkdir(mode=0o700)
    installed_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    runtime_root = Path(tempfile.mkdtemp(prefix="rodex-writer-", dir="/tmp"))
    environment = {
        **os.environ,
        "CODEX_HOME": str(fixture_home),
        "TERM": "xterm-256color",
        "RODEX_RUNTIME_DIR": str(runtime_root),
        "XDG_STATE_HOME": str(isolated / "state"),
        "RODEX_CODEX_BINARY": codex,
        "RODEX_TMUX_BINARY": tmux,
    }
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    command = [sys.executable, str(project / ".venv/bin/rodex")]
    try:
        for filename in ("auth.json", "config.toml"):
            source = installed_home / filename
            if source.is_file():
                shutil.copy2(source, fixture_home / filename)
        login = subprocess.run([codex, "login", "status"], env=environment, capture_output=True, timeout=10)
        _require_startup_prerequisite(request, login.returncode == 0, "An authenticated Codex CLI is required")
        with (
            _retained_standalone_codex_thread(codex, isolated / "writer.sock", environment, project) as (
                writer,
                thread_id,
            ),
            RodexTerminalClient([*command, "resume", thread_id], environment, project) as client,
        ):
            marker = f"failed to initialize thread persistence: thread-store conflict: thread {thread_id}"
            deadline = time.monotonic() + 8
            repeated_conflict = False
            while time.monotonic() < deadline:
                client.poll()
                assert client.exit_code is None, client.output.decode(errors="replace")
                conflicts = sum(path.read_text(errors="replace").count(marker) for path in runtime_root.glob("app-*.log"))
                if conflicts >= 2:
                    repeated_conflict = True
                    break
            assert repeated_conflict, "live failed TUI was not retried while the exact fixture writer remained"
            assert writer.poll() is None
            writer.terminate()
            writer.wait(timeout=5)
            name = client.wait_for_attach()
            inspected = subprocess.run(
                [*command, "_inspect", name, "--json"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert inspected.returncode == 0, inspected.stderr
            payload = json.loads(inspected.stdout)
            assert payload["codex"]["session_id"] == thread_id
            assert payload["codex"]["turn_id"] is None
            client.detach()
    finally:
        # Only servers minted beneath this fresh fixture directory are eligible.
        for socket_path in runtime_root.glob(RODEX_TMUX_SOCKET_PATTERN):
            subprocess.run([tmux, "-N", "-S", str(socket_path), "kill-server"], capture_output=True, timeout=5)
        _stop_fixture_daemon(runtime_root)
        shutil.rmtree(runtime_root, ignore_errors=True)
        for filename in ("auth.json", "config.toml"):
            (fixture_home / filename).unlink(missing_ok=True)
