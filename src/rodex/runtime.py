"""Own the small live boundary between Rodex, tmux, and Codex app-server."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import unix_connect

from .protocol_proxy import CodexProtocolProxy, TmuxToolCallStatus, ToolCallCounter

SUN_PATH_MAX_BYTES: Final = 107
DEFAULT_STARTUP_TIMEOUT_SECONDS: Final = 15.0
_POLL_INTERVAL_SECONDS: Final = 0.05

Runner = Callable[..., subprocess.CompletedProcess[str]]
Connector = Callable[..., Any]


class RodexRuntimeError(RuntimeError):
    """A live tmux or Codex app-server operation failed."""


@dataclass(frozen=True, slots=True)
class LiveTmuxSession:
    """The exact address of one running tmux session."""

    tmux_server_socket_path: Path
    tmux_session_name: str


@dataclass(frozen=True, slots=True)
class LiveRodexRuntime(LiveTmuxSession):
    """Addresses for one running Codex TUI hosted by tmux."""

    app_server_socket_path: Path
    app_server_log_path: Path
    protocol_proxy_socket_path: Path


class RodexRuntimeLauncher:
    """Start one private app-server/TUI pair and attach the user's terminal."""

    def __init__(
        self,
        codex_binary: str,
        tmux_binary: str,
        *,
        runner: Runner = subprocess.run,
        connector: Connector = unix_connect,
        python_executable: str = sys.executable,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self._codex_binary = codex_binary
        self._tmux_binary = tmux_binary
        self._run = runner
        self._connect = connector
        self._python_executable = python_executable
        self._monotonic = monotonic
        self._sleep = sleep
        self._startup_timeout_seconds = startup_timeout_seconds

    def start(
        self, workspace: Path, codex_arguments: Sequence[str]
    ) -> tuple[LiveRodexRuntime, uuid.UUID]:
        """Start tmux and return only after its single Codex UUID is observable."""
        resolved_workspace = workspace.expanduser().resolve()
        if not resolved_workspace.is_dir():
            raise RodexRuntimeError(f"workspace is not a directory: {resolved_workspace}")

        runtime_root = default_runtime_root()
        token = secrets.token_hex(8)
        runtime = LiveRodexRuntime(
            tmux_server_socket_path=runtime_root / "tmux.sock",
            tmux_session_name=f"rodex-{token}",
            app_server_socket_path=runtime_root / f"app-{token}.sock",
            app_server_log_path=runtime_root / f"app-{token}.log",
            protocol_proxy_socket_path=runtime_root / f"proxy-{token}.sock",
        )
        _require_short_unix_socket_path(runtime.tmux_server_socket_path)
        _require_short_unix_socket_path(runtime.app_server_socket_path)
        _require_short_unix_socket_path(runtime.protocol_proxy_socket_path)

        host_command = shlex.join(
            [
                self._python_executable,
                "-m",
                "rodex.session_host",
                "--codex-binary",
                self._codex_binary,
                "--app-server-socket",
                str(runtime.app_server_socket_path),
                "--app-server-log",
                str(runtime.app_server_log_path),
                "--protocol-proxy-socket",
                str(runtime.protocol_proxy_socket_path),
                "--tmux-binary",
                self._tmux_binary,
                "--tmux-server-socket",
                str(runtime.tmux_server_socket_path),
                "--",
                *codex_arguments,
            ]
        )
        self._tmux(
            runtime,
            "new-session",
            "-d",
            "-s",
            runtime.tmux_session_name,
            "-c",
            str(resolved_workspace),
            host_command,
        )
        try:
            codex_uuid = self._wait_for_single_codex_uuid(runtime)
        except BaseException:
            self.stop(runtime, check=False)
            raise
        return runtime, codex_uuid

    def rename(self, runtime: LiveTmuxSession, tmux_session_name: str) -> LiveTmuxSession:
        """Rename one exact tmux session and return its updated address."""
        session_name = tmux_session_name.strip()
        if not session_name:
            raise ValueError("tmux_session_name must be non-empty")
        self._tmux(
            runtime,
            "rename-session",
            "-t",
            runtime.tmux_session_name,
            session_name,
        )
        return replace(runtime, tmux_session_name=session_name)

    def configure_identity_status(self, runtime: LiveTmuxSession) -> None:
        """Show the Rodex cool name prominently in the tmux status line."""
        self._tmux(runtime, "set-option", "-t", runtime.tmux_session_name, "status", "on")
        self._tmux(
            runtime,
            "set-option",
            "-t",
            runtime.tmux_session_name,
            "status-left",
            "#[fg=green,bold] Rodex: #S #[fg=cyan,bold]| Tools: "
            "#{@rodex_tool_calls} #[default]",
        )
        self._tmux(
            runtime,
            "set-option",
            "-t",
            runtime.tmux_session_name,
            "status-left-length",
            "68",
        )

    def attach(self, runtime: LiveTmuxSession) -> None:
        """Attach the calling terminal to the live Rodex tmux session."""
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        self._tmux(
            runtime,
            "attach-session",
            "-t",
            runtime.tmux_session_name,
            interactive=True,
            environment=environment,
        )

    def session_exists(self, runtime: LiveTmuxSession) -> bool:
        """Return whether the exact recorded tmux session is still running."""
        result = self._tmux(
            runtime,
            "has-session",
            "-t",
            runtime.tmux_session_name,
            check=False,
        )
        return result.returncode == 0

    def stop(self, runtime: LiveTmuxSession, *, check: bool = True) -> None:
        """Stop exactly one tmux session, allowing its supervisor to clean up."""
        self._tmux(
            runtime,
            "kill-session",
            "-t",
            runtime.tmux_session_name,
            check=check,
        )

    def _wait_for_single_codex_uuid(self, runtime: LiveRodexRuntime) -> uuid.UUID:
        deadline = self._monotonic() + self._startup_timeout_seconds
        last_error: BaseException | None = None
        while self._monotonic() < deadline:
            if not self.session_exists(runtime):
                detail = _read_runtime_error_detail(runtime.app_server_log_path)
                suffix = f": {detail}" if detail else ""
                raise RodexRuntimeError(
                    f"Codex exited before its session was ready{suffix}"
                )
            if runtime.app_server_socket_path.exists():
                try:
                    loaded = self._list_loaded_codex_threads(runtime.app_server_socket_path)
                except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError) as error:
                    last_error = error
                else:
                    if len(loaded) == 1:
                        try:
                            return uuid.UUID(loaded[0])
                        except ValueError as error:
                            raise RodexRuntimeError(
                                f"app-server returned an invalid Codex UUID: {loaded[0]!r}"
                            ) from error
                    if len(loaded) > 1:
                        raise RodexRuntimeError(
                            "private app-server loaded more than one Codex thread"
                        )
            self._sleep(_POLL_INTERVAL_SECONDS)

        detail = _read_runtime_error_detail(runtime.app_server_log_path)
        if not detail and last_error is not None:
            detail = str(last_error)
        suffix = f": {detail}" if detail else ""
        raise RodexRuntimeError(f"timed out waiting for the Codex session{suffix}")

    def _list_loaded_codex_threads(self, socket_path: Path) -> list[str]:
        with self._connect(
            str(socket_path),
            uri="ws://localhost/rpc",
            compression=None,
            open_timeout=1,
            close_timeout=1,
        ) as websocket:
            websocket.send(
                json.dumps(
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
                    }
                )
            )
            _receive_response(websocket, 0)
            websocket.send(json.dumps({"method": "initialized", "params": {}}))
            websocket.send(
                json.dumps({"method": "thread/loaded/list", "id": 1, "params": {}})
            )
            result = _receive_response(websocket, 1)
        data = result.get("data")
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise RodexRuntimeError("app-server returned an invalid loaded-thread result")
        return data

    def _tmux(
        self,
        runtime: LiveTmuxSession,
        *arguments: str,
        check: bool = True,
        interactive: bool = False,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            self._tmux_binary,
            "-S",
            str(runtime.tmux_server_socket_path),
            *arguments,
        ]
        options: dict[str, object] = {
            "check": check,
            "text": True,
            "env": environment,
        }
        if not interactive:
            options["capture_output"] = True
        try:
            return self._run(command, **options)
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "tmux command failed").strip()
            raise RodexRuntimeError(detail) from error


def default_runtime_root() -> Path:
    """Return a short, private Linux runtime directory shared by Rodex sessions."""
    configured = os.environ.get("RODEX_RUNTIME_DIR")
    if configured:
        root = Path(configured).expanduser().resolve()
        return _prepare_runtime_root(root)

    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        candidate = Path(xdg_runtime).expanduser().resolve() / "rodex"
        if len(os.fsencode(candidate / "app-0000000000000000.sock")) <= SUN_PATH_MAX_BYTES:
            return _prepare_runtime_root(candidate)
    return _prepare_runtime_root(Path("/tmp") / f"rodex-{os.getuid()}")


def run_session_host(
    codex_binary: str,
    app_server_socket_path: Path,
    app_server_log_path: Path,
    protocol_proxy_socket_path: Path,
    tmux_binary: str,
    tmux_server_socket_path: Path,
    codex_arguments: Sequence[str],
) -> int:
    """Supervise the app-server, protocol proxy, and foreground Codex TUI."""
    app_server_socket_path.unlink(missing_ok=True)
    protocol_proxy_socket_path.unlink(missing_ok=True)
    app_server_log_path.parent.mkdir(parents=True, exist_ok=True)
    app_server: subprocess.Popen[bytes] | None = None
    protocol_proxy: CodexProtocolProxy | None = None

    def stop_on_signal(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    previous_handlers = {
        signum: signal.signal(signum, stop_on_signal)
        for signum in (signal.SIGHUP, signal.SIGTERM)
    }
    try:
        with app_server_log_path.open("ab", buffering=0) as log:
            app_server = subprocess.Popen(
                [
                    codex_binary,
                    "app-server",
                    "--listen",
                    f"unix://{app_server_socket_path}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            _wait_for_app_server_socket(app_server, app_server_socket_path)
            tmux_pane_target = os.environ.get("TMUX_PANE", "")
            tool_call_status = TmuxToolCallStatus(
                tmux_binary,
                tmux_server_socket_path,
                tmux_pane_target,
            )
            tool_call_status.update(0)
            protocol_proxy = CodexProtocolProxy(
                protocol_proxy_socket_path,
                app_server_socket_path,
                ToolCallCounter(tool_call_status.update),
            )
            protocol_proxy.start()
            completed = subprocess.run(
                [
                    codex_binary,
                    "--remote",
                    f"unix://{protocol_proxy_socket_path}",
                    *codex_arguments,
                ],
                check=False,
            )
            return completed.returncode
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        try:
            if protocol_proxy is not None:
                protocol_proxy.close()
        finally:
            if app_server is not None and app_server.poll() is None:
                app_server.terminate()
                try:
                    app_server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    app_server.kill()
                    app_server.wait(timeout=3)
            app_server_socket_path.unlink(missing_ok=True)
            protocol_proxy_socket_path.unlink(missing_ok=True)
            if app_server_log_path.exists() and app_server_log_path.stat().st_size == 0:
                app_server_log_path.unlink()


def _receive_response(websocket: Any, request_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        message = json.loads(websocket.recv(timeout=max(0.01, deadline - time.monotonic())))
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise RodexRuntimeError(f"app-server request failed: {message['error']}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RodexRuntimeError("app-server response did not contain a result")
        return result
    raise TimeoutError(f"timed out waiting for app-server response {request_id}")


def _prepare_runtime_root(root: Path) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = root.stat()
    if stat.st_uid != os.getuid():
        raise RodexRuntimeError(
            f"runtime directory is not owned by uid {os.getuid()}: {root}"
        )
    if not root.is_dir():
        raise RodexRuntimeError(f"runtime path is not a directory: {root}")
    root.chmod(0o700)
    return root


def _require_short_unix_socket_path(path: Path) -> None:
    if len(os.fsencode(path)) > SUN_PATH_MAX_BYTES:
        raise RodexRuntimeError(f"Unix socket path is too long: {path}")


def _wait_for_app_server_socket(
    process: subprocess.Popen[bytes], socket_path: Path
) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            raise RodexRuntimeError(
                f"Codex app-server exited during startup with status {returncode}"
            )
        if socket_path.exists():
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise RodexRuntimeError("timed out waiting for the Codex app-server socket")


def _read_runtime_error_detail(log_path: Path) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-1000:]
