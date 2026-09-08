"""Primary workflow gate: real CLI, Codex, tmux, attachment, and shared-server reuse.

No model prompts are submitted. SQL, tmux, and Codex history are isolated; copied
login/configuration files bring up the ordinary TUI and are removed after the test.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from websockets.sync.client import unix_connect

from rodex.app_server_contract import CODEX_APP_SERVER, AppServerClientInfo
from rodex.interaction_pipeline import DeliveryStatus, InteractionOperation, InteractionRequest
from rodex.interaction_transport import publish_session_interaction
from rodex.tmux_session_capability import RODEX_SHARED_TMUX_SOCKET_NAME


@dataclass
class RodexTerminalClient:
    """Own only the terminal client launched by this test, never an existing host."""

    command: list[str]
    environment: dict[str, str]
    workspace: Path
    process_id: int | None = field(init=False, default=None)
    terminal: int = field(init=False, default=-1)
    output: bytes = field(init=False, default=b"")
    exit_code: int | None = field(init=False, default=None)

    def __enter__(self):
        process_id, terminal = pty.fork()
        if process_id == 0:
            try:
                os.chdir(self.workspace)
                os.execvpe(self.command[0], self.command, self.environment)
            except BaseException as error:
                with suppress(OSError):
                    os.write(2, f"Rodex test client could not execute: {error}\n"[:512].encode())
                os._exit(127)
        self.process_id = process_id
        self.terminal = terminal
        fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 120, 0, 0))
        return self

    def poll(self) -> None:
        if select.select([self.terminal], [], [], 0.05)[0]:
            with suppress(OSError):
                self.output += os.read(self.terminal, 65536)
        if self.process_id is not None:
            reaped, status = os.waitpid(self.process_id, os.WNOHANG)
            if reaped:
                self.process_id = None
                self.exit_code = os.waitstatus_to_exitcode(status)

    def wait_for_attach(self) -> str:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.poll()
            match = re.search(rb"Rodex attach \[([^\]]+)\]", self.output)
            if match is not None and self.exit_code is None:
                return match.group(1).decode()
            assert self.exit_code is None, self.output.decode(errors="replace")
        pytest.fail(f"Rodex never attached:\n{self.output.decode(errors='replace')}")

    def detach(self) -> None:
        os.write(self.terminal, b"\x04")
        deadline = time.monotonic() + 10
        while self.process_id is not None and time.monotonic() < deadline:
            self.poll()
        assert self.exit_code == 0, self.output.decode(errors="replace")

    def __exit__(self, *_exception):
        if self.process_id is not None:
            os.kill(self.process_id, signal.SIGTERM)
            deadline = time.monotonic() + 3
            while self.process_id is not None and time.monotonic() < deadline:
                self.poll()
            if self.process_id is not None:
                os.kill(self.process_id, signal.SIGKILL)
                os.waitpid(self.process_id, 0)
                self.process_id = None
        os.close(self.terminal)


def _require_startup_prerequisite(request: pytest.FixtureRequest, condition: bool, explanation: str) -> None:
    if condition:
        return
    if request.config.getoption("--require-live-startup"):
        pytest.fail(explanation)
    pytest.skip(explanation)


def test_terminal_client_reports_exec_failure_without_reentering_pytest(tmp_path: Path) -> None:
    with RodexTerminalClient([str(tmp_path / "missing-rodex")], dict(os.environ), tmp_path) as client:
        with pytest.raises(AssertionError, match="could not execute"):
            client.wait_for_attach()
        assert client.exit_code == 127


def _create_standalone_codex_thread(codex: str, socket_path: Path, environment: dict[str, str], workspace: Path) -> str:
    """Create a real saved Codex thread without Rodex registration or a model turn."""
    process = subprocess.Popen(
        CODEX_APP_SERVER.command(codex, socket_path),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not socket_path.exists():
            assert process.poll() is None, "Standalone Codex fixture exited before binding"
            assert time.monotonic() < deadline, "Standalone Codex fixture did not bind"
            time.sleep(0.02)
        with unix_connect(
            str(socket_path),
            uri=f"ws://localhost{CODEX_APP_SERVER.rpc_connection_path}",
            compression=None,
            open_timeout=2,
            close_timeout=1,
        ) as websocket:

            def response(request: dict) -> dict:
                websocket.send(json.dumps(request))
                deadline = time.monotonic() + 10
                while True:
                    payload = json.loads(websocket.recv(timeout=max(0, deadline - time.monotonic())))
                    if payload.get("id") == request["id"]:
                        assert "error" not in payload, payload
                        return payload["result"]

            response(
                CODEX_APP_SERVER.initialize_request(
                    "startup:initialize", AppServerClientInfo("rodex-startup-test", "Rodex startup test", "1")
                )
            )
            websocket.send(json.dumps(CODEX_APP_SERVER.initialized_notification()))
            started = response(
                CODEX_APP_SERVER.request("startup:thread", "thread/start", {"ephemeral": False, "cwd": str(workspace)})
            )
            # The documented injection API persists fixture history without running a model.
            # https://learn.chatgpt.com/docs/app-server#inject-items-into-a-thread
            response(
                CODEX_APP_SERVER.request(
                    "startup:history",
                    "thread/inject_items",
                    {
                        "threadId": started["thread"]["id"],
                        "items": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "Rodex standalone-resume test fixture."}],
                            }
                        ],
                    },
                )
            )
            return started["thread"]["id"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.live_startup
def test_installed_rodex_starts_reuses_and_adopts_sessions(
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> None:
    codex = shutil.which("codex")
    tmux_binary = shutil.which("tmux")
    _require_startup_prerequisite(request, codex is not None and tmux_binary is not None, "Codex and tmux are required")
    assert codex is not None and tmux_binary is not None
    project = Path(__file__).parents[1]
    isolated = tmp_path_factory.mktemp("startup")
    installed_codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    isolated_codex_home = isolated / "codex"
    isolated_codex_home.mkdir(mode=0o700)
    installed_shim = isolated / "rodex"
    shutil.copy2(project / "usr/local/bin/rodex", installed_shim)
    environment = {
        **os.environ,
        "TERM": "xterm-256color",
        "RODEX_PROJECT_DIR": str(project),
        "RODEX_CODEX_BINARY": codex,
        "RODEX_TMUX_BINARY": tmux_binary,
        "RODEX_RUNTIME_DIR": str(isolated / "r"),
        "XDG_STATE_HOME": str(isolated / "state"),
        "CODEX_HOME": str(isolated_codex_home),
    }
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    socket_path = isolated / "r" / RODEX_SHARED_TMUX_SOCKET_NAME
    inspected_codex_ids: dict[str, str] = {}

    def tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [tmux_binary, "-S", str(socket_path), *arguments],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def stop_fixture_session(name: str) -> None:
        host = tmux("display-message", "-p", "-t", f"={name}:", "#{pane_pid}")
        assert host.returncode == 0 and host.stdout.strip().isdigit(), host.stdout + host.stderr
        host_stat = Path(f"/proc/{host.stdout.strip()}/stat")
        stopped = tmux("kill-session", "-t", f"={name}")
        assert stopped.returncode == 0, stopped.stderr
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                process_state = host_stat.read_text().rsplit(") ", 1)[1].split()[0]
            except FileNotFoundError:
                return
            if process_state == "Z":
                return
            time.sleep(0.02)
        pytest.fail(f"Test-owned Rodex host for {name} did not finish shutting down")

    def exercise_terminal_interception(client: RodexTerminalClient, name: str) -> None:
        def await_surface(arguments: tuple[str, ...], expected: str, *, absent: bool = False) -> str:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                surface = tmux(*arguments)
                if surface.returncode == 0 and (expected in surface.stdout) != absent:
                    return surface.stdout
                client.poll()
            pytest.fail(f"Expected {'absence of ' if absent else ''}{expected!r}: {surface.stdout!r} {surface.stderr}")

        capture = ("capture-pane", "-p", "-t", f"={name}:")
        os.write(client.terminal, b"/")
        await_surface(capture, "/model")
        os.write(client.terminal, b"r")
        await_surface(capture, "/review")
        os.write(client.terminal, b"o")
        await_surface(capture, "\u203a /ro")
        await_surface(capture, "/rodex   issue a rodex command")
        await_surface(capture, "/review", absent=True)
        draft = "/ro"
        for character in "dex":
            os.write(client.terminal, character.encode())
            draft += character
            await_surface(capture, f"\u203a {draft}")
            await_surface(capture, "/rodex   issue a rodex command")
        # Another attacher shares this one input owner. Arrival/departure animation
        # must not conceal a currently owned draft or create a second interceptor.
        with RodexTerminalClient([str(installed_shim), name], environment, project) as peer:
            assert peer.wait_for_attach() == name
            await_surface(("display-message", "-p", "-t", f"={name}:", "#{session_attached}"), "2")
            await_surface(capture, "\u203a /rodex")
            peer.detach()
        await_surface(("display-message", "-p", "-t", f"={name}:", "#{session_attached}"), "1")
        await_surface(capture, "\u203a /rodex")
        os.write(client.terminal, b" example\r")
        await_surface(capture, "placeholder only")
        await_surface(capture, "/rodex   issue a rodex command", absent=True)
        await_surface(capture, "\u203a /r", absent=True)
        # Exercise an ordinary draft after local submission, but never submit it.
        os.write(client.terminal, b"ordinary native draft")
        await_surface(capture, "\u203a ordinary native draft")
        os.write(client.terminal, b"\x15")
        await_surface(capture, "\u203a ordinary native draft", absent=True)
        os.write(client.terminal, b"/ro")
        await_surface(capture, "\u203a /ro")
        os.write(client.terminal, b"\x1b")
        await_surface(capture, "/rodex   issue a rodex command", absent=True)
        await_surface(capture, "\u203a /r")
        os.write(client.terminal, b"\x7f\x7f")
        await_surface(capture, "\u203a /r", absent=True)

    def exercise_client(
        command: list[str], *, expected_codex_id: str | None = None, exercise_inputs: bool = False
    ) -> tuple[str, str]:
        with RodexTerminalClient(command, environment, project) as client:
            name = client.wait_for_attach()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                attached = tmux("display-message", "-p", "-t", f"={name}:", "#{session_attached}")
                if attached.returncode == 0 and attached.stdout.strip() == "1":
                    break
                client.poll()
            else:
                pytest.fail(f"No attached TUI for {name}: {client.output.decode(errors='replace')}")
            inspected = subprocess.run(
                [str(installed_shim), "_inspect", name, "--json"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert inspected.returncode == 0, inspected.stdout + inspected.stderr
            envelope = json.loads(inspected.stdout)
            assert envelope["schema_version"] == 3
            assert envelope["ok"] is True
            assert envelope["data"]["thread"]["status"] == "idle"
            assert envelope["data"]["thread"]["cwd"] == str(project)
            assert envelope["data"]["thread"]["can_accept_direct_input"] is not False
            assert envelope["codex"]["turn_id"] is None
            inspected_codex_ids[name] = envelope["codex"]["session_id"]
            if expected_codex_id is not None:
                assert inspected_codex_ids[name] == expected_codex_id
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                pane = tmux("capture-pane", "-p", "-t", f"={name}:")
                if "OpenAI Codex" in pane.stdout:
                    break
                client.poll()
            else:
                pytest.fail(f"Codex TUI did not render for {name}: {pane.stdout}")
            runtime_id = envelope["runtime"]["runtime_id"]
            assert runtime_id
            endpoint = tmux("display-message", "-p", "-t", f"={name}:", "#{@rodex_protocol_proxy_socket_path}")
            assert endpoint.returncode == 0 and endpoint.stdout.strip()
            notice = f"Rodex pipeline display check {runtime_id}"
            delivery = publish_session_interaction(
                Path(endpoint.stdout.strip()),
                InteractionRequest("main", InteractionOperation.MESSAGE, "startup-test", text=notice),
            )
            assert delivery.status == DeliveryStatus.DELIVERED, delivery
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                displayed = tmux("capture-pane", "-p", "-t", f"={name}:")
                if notice in displayed.stdout:
                    break
                client.poll()
            else:
                pytest.fail(f"Pipeline notice did not render in the real Codex TUI: {displayed.stdout}")
            if exercise_inputs:
                exercise_terminal_interception(client, name)
            after_notice = subprocess.run(
                [str(installed_shim), "_inspect", name, "--json"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert after_notice.returncode == 0, after_notice.stdout + after_notice.stderr
            assert json.loads(after_notice.stdout)["codex"]["turn_id"] is None
            client.detach()
        assert tmux("has-session", "-t", f"={name}").returncode == 0
        return name, runtime_id

    try:
        # Preserve installed authentication and workspace trust without sharing
        # history, indexes, or other mutable Codex state with the user's sessions.
        for filename in ("auth.json", "config.toml"):
            source = installed_codex_home / filename
            if source.is_file():
                shutil.copy2(source, isolated_codex_home / filename)
        login = subprocess.run([codex, "login", "status"], env=environment, capture_output=True, text=True, timeout=10)
        _require_startup_prerequisite(request, login.returncode == 0, "An authenticated Codex CLI is required")
        # The first host deliberately starts through python; the installed shim's
        # console entry point may use python3 after a routine uv sync.
        first = exercise_client(
            [str(Path(sys.prefix) / "bin/python"), str(project / ".venv/bin/rodex")], exercise_inputs=True
        )
        assert exercise_client([str(installed_shim), first[0]]) == first
        assert exercise_client([str(installed_shim), "resume", first[0]]) == first
        assert exercise_client([str(installed_shim), "resume", inspected_codex_ids[first[0]]]) == first
        second = exercise_client([str(installed_shim)])
        assert second[0] != first[0]
        assert second[1] != first[1]
        assert len(tmux("list-sessions").stdout.splitlines()) == 2
        standalone_codex_id = _create_standalone_codex_thread(codex, isolated / "standalone.sock", environment, project)
        assert standalone_codex_id not in inspected_codex_ids.values()
        adopted = exercise_client(
            [str(installed_shim), "resume", standalone_codex_id], expected_codex_id=standalone_codex_id
        )
        assert adopted[0] not in {first[0], second[0]}
        # A live alias change announces itself through a model turn. Assign the
        # fixture alias while stopped so this startup matrix remains model-free.
        stop_fixture_session(adopted[0])
        alias = "startup-resume-alias"
        aliased = subprocess.run(
            [str(installed_shim), "_alias", adopted[0], alias],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert aliased.returncode == 0, aliased.stdout + aliased.stderr
        selectors = (adopted[0], alias, standalone_codex_id)
        restarted = exercise_client([str(installed_shim), alias], expected_codex_id=standalone_codex_id)
        assert restarted[0] == alias
        assert restarted[1] != adopted[1]
        current_runtime_id = restarted[1]
        for prefix in ((), ("resume",)):
            for selector in selectors:
                assert exercise_client(
                    [str(installed_shim), *prefix, selector], expected_codex_id=standalone_codex_id
                ) == (alias, current_runtime_id)
        for prefix in ((), ("resume",)):
            for selector in selectors:
                # Stop only this fixture on the private test server, then prove
                # every spelling resumes its saved Codex identity in a new runtime.
                stop_fixture_session(alias)
                resumed = exercise_client([str(installed_shim), *prefix, selector], expected_codex_id=standalone_codex_id)
                assert resumed[0] == alias
                assert resumed[1] != current_runtime_id
                current_runtime_id = resumed[1]
        assert len(tmux("list-sessions").stdout.splitlines()) == 3
    finally:
        # This fresh per-test socket can contain only the runtimes created above.
        try:
            tmux("kill-server")
        finally:
            for filename in ("auth.json", "config.toml"):
                (isolated_codex_home / filename).unlink(missing_ok=True)
