from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]


def _wait_for[T](
    description: str,
    probe: Callable[[], T | None],
    *,
    timeout: float = 20.0,
) -> T:
    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            pytest.fail(f"timed out waiting for {description}")
        time.sleep(0.02)


def _tmux(
    tmux_binary: str,
    socket_path: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [tmux_binary, "-S", os.fspath(socket_path), *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _process_command(pid: int) -> tuple[str, ...] | None:
    try:
        payload = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    command = tuple(
        argument.decode(errors="replace") for argument in payload.split(b"\0") if argument
    )
    return command or None


def _child_process_ids(pid: int) -> tuple[int, ...]:
    try:
        payload = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
    except OSError:
        return ()
    return tuple(int(value) for value in payload.split())


def _descendant_commands(root_pid: int) -> dict[int, tuple[str, ...]]:
    pending = [root_pid]
    commands: dict[int, tuple[str, ...]] = {}
    while pending:
        pid = pending.pop()
        if pid in commands:
            continue
        command = _process_command(pid)
        if command is None:
            continue
        commands[pid] = command
        pending.extend(_child_process_ids(pid))
    return commands


def _isolated_codex_process_ids(codex_home: Path) -> tuple[int, ...]:
    expected = b"CODEX_HOME=" + os.fsencode(codex_home)
    process_ids: list[int] = []
    for process_path in Path("/proc").glob("[0-9]*"):
        try:
            environment = (process_path / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if expected in environment:
            process_ids.append(int(process_path.name))
    return tuple(process_ids)


def _stop_isolated_codex_processes(codex_home: Path) -> None:
    for signum, timeout in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
        process_ids = _isolated_codex_process_ids(codex_home)
        if not process_ids:
            return
        for pid in process_ids:
            with suppress(ProcessLookupError):
                os.kill(pid, signum)
        deadline = time.monotonic() + timeout
        while _isolated_codex_process_ids(codex_home):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)


@pytest.mark.evolutionary_regression
def test_fresh_detached_launcher_keeps_the_registered_session_host_alive() -> None:
    """Exercise the real CLI/tmux/session-host process boundary from empty storage."""
    codex_binary = shutil.which("codex")
    tmux_binary = shutil.which("tmux")
    if codex_binary is None:
        pytest.skip("Codex CLI is not installed")
    if tmux_binary is None:
        pytest.skip("tmux is not installed")
    source_codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    source_auth = source_codex_home / "auth.json"
    if not source_auth.is_file():
        pytest.skip("an isolated live Codex launch requires an existing auth.json")

    integration_root = Path(tempfile.mkdtemp(prefix="rodex-launch-", dir="/tmp"))
    integration_root.chmod(0o700)
    database = integration_root / "database" / "rodex.sqlite3"
    runtime_root = integration_root / "runtime"
    tmux_socket = runtime_root / "tmux.sock"
    codex_home = integration_root / "codex-home"
    workspace = integration_root / "workspace"
    codex_home.mkdir(mode=0o700)
    workspace.mkdir(mode=0o700)
    isolated_auth = codex_home / "auth.json"
    shutil.copyfile(source_auth, isolated_auth)
    isolated_auth.chmod(0o600)

    environment = os.environ.copy()
    environment.update(
        {
            "CODEX_HOME": os.fspath(codex_home),
            "RODEX_CODEX_BINARY": codex_binary,
            "RODEX_DATABASE_PATH": os.fspath(database),
            "RODEX_RUNTIME_DIR": os.fspath(runtime_root),
            "RODEX_TMUX_BINARY": tmux_binary,
            "TERM": "xterm-256color",
        }
    )
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)

    session_name: str | None = None
    session_host_pid: int | None = None
    try:
        assert not database.exists()
        assert not database.parent.exists()
        launched = subprocess.run(
            [os.fspath(PROJECT_ROOT / "rodex"), "_detach"],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert launched.returncode == 0, launched.stderr
        launch_result = json.loads(launched.stdout)
        assert launch_result["status"] == "running"
        session_name = launch_result["rodex_session_name"]
        assert isinstance(session_name, str) and session_name
        assert database.is_file()

        def registered_live_pane() -> int | None:
            if not tmux_socket.exists():
                return None
            registration = _tmux(
                tmux_binary,
                tmux_socket,
                "show-options",
                "-v",
                "-t",
                f"={session_name}:",
                "@rodex_registration_state",
                check=False,
            )
            pane = _tmux(
                tmux_binary,
                tmux_socket,
                "display-message",
                "-p",
                "-t",
                f"={session_name}:",
                "-F",
                "#{pane_dead}\t#{pane_pid}",
                check=False,
            )
            if registration.returncode != 0 or registration.stdout.strip() != "registered":
                return None
            fields = pane.stdout.strip().split("\t")
            if pane.returncode != 0 or len(fields) != 2 or fields[0] != "0":
                return None
            return int(fields[1]) if fields[1].isdigit() else None

        pane_pid = _wait_for("a registered live Rodex pane", registered_live_pane)

        def live_session_host() -> int | None:
            for pid, command in _descendant_commands(pane_pid).items():
                if "rodex.session_host" in command:
                    return pid
            return None

        session_host_pid = _wait_for("the live session-host process", live_session_host)

        def analytics_started_after_registration() -> bool | None:
            commands = _descendant_commands(session_host_pid)
            return (
                True
                if any("rodex.analytics_worker" in command for command in commands.values())
                else None
            )

        _wait_for(
            "analytics activation after registration",
            analytics_started_after_registration,
        )
        os.kill(session_host_pid, 0)

        running = subprocess.run(
            [os.fspath(PROJECT_ROOT / "rodex"), "_running"],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert running.returncode == 0, running.stderr
        assert "Running Rodex sessions: 1" in running.stdout
        assert f"{session_name} -> Codex {launch_result['codex_session_id']}" in (
            running.stdout
        )

        assert _process_command(session_host_pid) is not None
        _tmux(
            tmux_binary,
            tmux_socket,
            "kill-session",
            "-t",
            f"={session_name}",
        )

        def host_has_exited() -> bool | None:
            return True if _process_command(session_host_pid) is None else None

        _wait_for("the exact session host to exit during cleanup", host_has_exited)
        _stop_isolated_codex_processes(codex_home)
        assert _isolated_codex_process_ids(codex_home) == ()
        assert (
            _tmux(
                tmux_binary,
                tmux_socket,
                "has-session",
                "-t",
                f"={session_name}",
                check=False,
            ).returncode
            != 0
        )
        session_name = None
    finally:
        if tmux_socket.exists():
            sessions = _tmux(
                tmux_binary,
                tmux_socket,
                "list-sessions",
                "-F",
                "#{session_name}",
                check=False,
            )
            if sessions.returncode == 0:
                for isolated_session in sessions.stdout.splitlines():
                    _tmux(
                        tmux_binary,
                        tmux_socket,
                        "kill-session",
                        "-t",
                        f"={isolated_session}",
                        check=False,
                    )
            _tmux(tmux_binary, tmux_socket, "kill-server", check=False)
        remaining_host = (
            None if session_host_pid is None else _process_command(session_host_pid)
        )
        if remaining_host is not None and "rodex.session_host" in remaining_host:
            with suppress(ProcessLookupError):
                os.kill(session_host_pid, signal.SIGTERM)
        _stop_isolated_codex_processes(codex_home)
        shutil.rmtree(integration_root)
