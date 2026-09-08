"""Primary workflow gate: real CLI, Codex, tmux, attachment, and shared-server reuse.

No model prompts are submitted. SQL and tmux are isolated; the installed Codex login
is used only to bring up the ordinary TUI. Every test-owned runtime is cleaned up.
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


@pytest.mark.live_startup
def test_installed_rodex_starts_detaches_reopens_and_starts_again(
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> None:
    codex = shutil.which("codex")
    tmux_binary = shutil.which("tmux")
    _require_startup_prerequisite(request, codex is not None and tmux_binary is not None, "Codex and tmux are required")
    assert codex is not None and tmux_binary is not None
    login = subprocess.run([codex, "login", "status"], capture_output=True, text=True, timeout=10)
    _require_startup_prerequisite(request, login.returncode == 0, "An authenticated Codex CLI is required")
    project = Path(__file__).parents[1]
    isolated = tmp_path_factory.mktemp("startup")
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
    }
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    socket_path = isolated / "r" / RODEX_SHARED_TMUX_SOCKET_NAME

    def tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [tmux_binary, "-S", str(socket_path), *arguments],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def exercise_client(command: list[str]) -> tuple[str, str]:
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
            client.detach()
        assert tmux("has-session", "-t", f"={name}").returncode == 0
        return name, runtime_id

    try:
        # The first host deliberately starts through python; the installed shim's
        # console entry point may use python3 after a routine uv sync.
        first = exercise_client([str(Path(sys.prefix) / "bin/python"), str(project / ".venv/bin/rodex")])
        assert exercise_client([str(installed_shim), first[0]]) == first
        second = exercise_client([str(installed_shim)])
        assert second[0] != first[0]
        assert second[1] != first[1]
        assert len(tmux("list-sessions").stdout.splitlines()) == 2
    finally:
        # This fresh per-test socket can contain only the runtimes created above.
        tmux("kill-server")
