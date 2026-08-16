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
from threading import Event, Thread
from typing import Any, BinaryIO, Final

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import unix_connect

from .control import LiveRodexControl
from .protocol_proxy import (
    CodexProtocolEventTap,
    CodexProtocolProxy,
    TmuxToolCallStatus,
    ToolCallCounter,
)
from .status_animation import STATUS_ANIMATION_TOKEN_OPTION
from .tmux_status import (
    COMPLETION_TOKEN_OPTION,
    RODEX_STATUS_LEFT_FORMAT,
    RODEX_STATUS_LEFT_LENGTH,
)

SUN_PATH_MAX_BYTES: Final = 107
DEFAULT_STARTUP_TIMEOUT_SECONDS: Final = 15.0
RUNTIME_PATH_KEEPALIVE_INTERVAL_SECONDS: Final = 60.0 * 60.0
RODEX_TMUX_HISTORY_LIMIT_LINES: Final = 50_000
_TUI_SUPERVISION_INTERVAL_SECONDS: Final = 1.0
_POLL_INTERVAL_SECONDS: Final = 0.05
_PROXY_SOCKET_OPTION: Final = "@rodex_protocol_proxy_socket_path"
_EVENT_SOCKET_OPTION: Final = "@rodex_protocol_event_socket_path"
_CODEX_UUID_OPTION: Final = "@rodex_codex_session_uuid"
# One switch owns installation of the tmux `/rodex` bindings and completion pipe.
RODEX_TMUX_SLASH_ENABLED: Final = False
_SHARING_STATUS_FORMAT: Final = (
    "#{?session_many_attached,"
    "#[fg=yellow]#[bold] [Shared with #{e|-:#{session_attached},1} "
    "#{?#{==:#{session_attached},2},other,others}] #[default],"
    "#[fg=green]#[bold] [Private session] #[default]}"
    " | %H:%M %d-%b-%y"
)

Runner = Callable[..., subprocess.CompletedProcess[str]]
Connector = Callable[..., Any]


class RodexRuntimeError(RuntimeError):
    """A live tmux or Codex app-server operation failed."""


class RodexCodexSessionNotFoundError(RodexRuntimeError):
    """Codex explicitly reported that an exact requested identity is not saved."""


class _RuntimePathKeepalive:
    """Keep the pathnames required by one live Rodex runtime fresh."""

    def __init__(
        self,
        paths: Sequence[Path],
        *,
        interval_seconds: float = RUNTIME_PATH_KEEPALIVE_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("runtime path keepalive interval must be positive")
        self._paths = tuple(dict.fromkeys(paths))
        if not self._paths:
            raise ValueError("runtime path keepalive requires at least one path")
        self._interval_seconds = interval_seconds
        self._stop = Event()
        self._thread: Thread | None = None
        self._failure: RodexRuntimeError | None = None
        self._failure_reported = Event()

    @property
    def failure(self) -> RodexRuntimeError | None:
        """Return the periodic refresh failure, if the worker stopped on one."""
        return self._failure

    def start(self) -> None:
        """Refresh synchronously, then protect the paths until closed."""
        if self._thread is not None:
            raise RodexRuntimeError("runtime path keepalive is already running")
        self._refresh()
        self._thread = Thread(
            target=self._run,
            name="rodex-runtime-path-keepalive",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Stop periodic refreshes before the owning runtime removes its paths."""
        thread = self._thread
        self._stop.set()
        if thread is None:
            return
        thread.join(timeout=5)
        if thread.is_alive():
            raise RodexRuntimeError("runtime path keepalive did not stop")
        self._thread = None

    def wait_for_failure(self, timeout: float) -> RodexRuntimeError | None:
        """Wait briefly for a periodic failure and return the stable result."""
        self._failure_reported.wait(timeout)
        return self._failure

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._refresh()
            except RodexRuntimeError as error:
                self._failure = error
                self._failure_reported.set()
                return

    def _refresh(self) -> None:
        for path in self._paths:
            try:
                os.utime(path, None, follow_symlinks=False)
            except OSError as error:
                raise RodexRuntimeError(
                    f"could not refresh live runtime path {path}: {error}"
                ) from error


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
    protocol_event_socket_path: Path


def _exact_tmux_session_target(session_name: str) -> str:
    """Disable tmux prefix/glob matching for a recorded session identity."""
    return f"={session_name}"


def _exact_tmux_pane_target(session_name: str) -> str:
    """Address a session exactly through commands whose target is a pane."""
    return f"={session_name}:"


def _status_animation_hook_command(
    python_executable: str,
    tmux_binary: str,
    runtime: LiveTmuxSession,
    event: str,
) -> str:
    if event not in {"attached", "detached"}:
        raise ValueError(f"unsupported status animation event: {event}")
    animation_command = shlex.join(
        (
            python_executable,
            "-m",
            "rodex.status_animation",
            "--tmux-binary",
            tmux_binary,
            "--tmux-server-socket",
            str(runtime.tmux_server_socket_path),
            "--tmux-session-name",
            runtime.tmux_session_name,
            "--event",
            event,
        )
    )
    quiet_background_command = f"{animation_command} >/dev/null 2>&1"
    return f"run-shell -b {shlex.quote(quiet_background_command)}"


def _tmux_input_proxy_binding_command(
    python_executable: str,
    tmux_binary: str,
    runtime: LiveTmuxSession,
    key: str,
) -> str:
    if key not in {"Enter", "Tab"}:
        raise ValueError(f"unsupported Rodex input key: {key}")
    proxy_command = shlex.join(
        (
            python_executable,
            "-m",
            "rodex.tmux_input_proxy",
            "--tmux-binary",
            tmux_binary,
            "--tmux-server-socket",
            str(runtime.tmux_server_socket_path),
            "--pane-id",
            "#{pane_id}",
            "--session-name",
            "#{session_name}",
            "--client-name",
            "#{client_name}",
            "--key",
            key,
        )
    )
    return f"run-shell {shlex.quote(proxy_command)}"


def _tmux_completion_observer_command(
    python_executable: str,
    tmux_binary: str,
    runtime: LiveTmuxSession,
) -> str:
    observer_command = shlex.join(
        (
            python_executable,
            "-m",
            "rodex.tmux_completion_observer",
            "--tmux-binary",
            tmux_binary,
            "--tmux-server-socket",
            str(runtime.tmux_server_socket_path),
            "--pane-id",
            "#{pane_id}",
        )
    )
    return f"{observer_command} >/dev/null 2>&1"


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
            protocol_event_socket_path=runtime_root / f"events-{token}.sock",
        )
        _require_short_unix_socket_path(runtime.tmux_server_socket_path)
        _require_short_unix_socket_path(runtime.app_server_socket_path)
        _require_short_unix_socket_path(runtime.protocol_proxy_socket_path)
        _require_short_unix_socket_path(runtime.protocol_event_socket_path)

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
                "--protocol-event-socket",
                str(runtime.protocol_event_socket_path),
                "--tmux-binary",
                self._tmux_binary,
                "--tmux-server-socket",
                str(runtime.tmux_server_socket_path),
                "--",
                *codex_arguments,
            ]
        )
        self._start_tmux_session(runtime, resolved_workspace, host_command)
        try:
            requested_codex_uuid = _requested_exact_codex_resume(codex_arguments)
            codex_uuid = self._wait_for_single_codex_uuid(
                runtime,
                requested_codex_uuid=requested_codex_uuid,
            )
            if requested_codex_uuid is not None and codex_uuid != requested_codex_uuid:
                raise RodexRuntimeError(
                    "Codex resumed an unexpected exact identity: "
                    f"requested {requested_codex_uuid}, observed {codex_uuid}"
                )
            self.publish_runtime_control(runtime, codex_uuid)
        except BaseException:
            self.stop(runtime, check=False)
            raise
        return runtime, codex_uuid

    def publish_runtime_control(
        self, runtime: LiveRodexRuntime, codex_session_uuid: uuid.UUID
    ) -> None:
        """Advertise live-only control metadata inside the owning tmux session."""
        target = _exact_tmux_pane_target(runtime.tmux_session_name)
        for option_name, value in (
            (_PROXY_SOCKET_OPTION, str(runtime.protocol_proxy_socket_path)),
            (_EVENT_SOCKET_OPTION, str(runtime.protocol_event_socket_path)),
            (_CODEX_UUID_OPTION, str(codex_session_uuid)),
        ):
            self._tmux(runtime, "set-option", "-t", target, option_name, value)

    def discover_runtime_control(self, runtime: LiveTmuxSession) -> LiveRodexControl:
        """Read the current control endpoints from one exact live tmux session."""
        target = _exact_tmux_pane_target(runtime.tmux_session_name)
        proxy_path = self._read_tmux_option(runtime, target, _PROXY_SOCKET_OPTION)
        event_path = self._read_tmux_option(runtime, target, _EVENT_SOCKET_OPTION)
        codex_uuid_text = self._read_tmux_option(runtime, target, _CODEX_UUID_OPTION)
        try:
            codex_uuid = uuid.UUID(codex_uuid_text)
        except ValueError as error:
            raise RodexRuntimeError(
                "live tmux session advertised an invalid Codex UUID"
            ) from error
        return LiveRodexControl(Path(proxy_path), Path(event_path), codex_uuid)

    def rename(self, runtime: LiveTmuxSession, tmux_session_name: str) -> LiveTmuxSession:
        """Rename one exact tmux session and return its updated address."""
        session_name = tmux_session_name.strip()
        if not session_name:
            raise ValueError("tmux_session_name must be non-empty")
        self._tmux(
            runtime,
            "rename-session",
            "-t",
            _exact_tmux_session_target(runtime.tmux_session_name),
            session_name,
        )
        return replace(runtime, tmux_session_name=session_name)

    def configure_identity_status(self, runtime: LiveTmuxSession) -> None:
        """Configure Rodex-owned interaction and status for one live session."""
        target = _exact_tmux_pane_target(runtime.tmux_session_name)
        self._tmux(runtime, "set-option", "-t", target, "mouse", "on")
        for transient_option in (
            STATUS_ANIMATION_TOKEN_OPTION,
            COMPLETION_TOKEN_OPTION,
            "status-format",
            "status-style",
        ):
            self._tmux(
                runtime,
                "set-option",
                "-u",
                "-t",
                target,
                transient_option,
            )
        self._tmux(runtime, "set-option", "-t", target, "status", "on")
        self._tmux(
            runtime,
            "set-option",
            "-t",
            target,
            "status-left",
            RODEX_STATUS_LEFT_FORMAT,
        )
        self._tmux(
            runtime,
            "set-option",
            "-t",
            target,
            "status-left-length",
            RODEX_STATUS_LEFT_LENGTH,
        )
        self._tmux(
            runtime,
            "set-option",
            "-t",
            target,
            "status-right",
            _SHARING_STATUS_FORMAT,
        )
        self._tmux(
            runtime,
            "set-option",
            "-t",
            target,
            "status-right-length",
            "64",
        )
        for event in ("attached", "detached"):
            self._tmux(
                runtime,
                "set-hook",
                "-t",
                target,
                f"client-{event}",
                _status_animation_hook_command(
                    self._python_executable,
                    self._tmux_binary,
                    runtime,
                    event,
                ),
            )
        if RODEX_TMUX_SLASH_ENABLED:
            self._tmux(
                runtime,
                "pipe-pane",
                "-O",
                "-t",
                target,
                _tmux_completion_observer_command(
                    self._python_executable,
                    self._tmux_binary,
                    runtime,
                ),
            )
            for key in ("Enter", "Tab"):
                self._tmux(
                    runtime,
                    "bind-key",
                    "-n",
                    key,
                    _tmux_input_proxy_binding_command(
                        self._python_executable,
                        self._tmux_binary,
                        runtime,
                        key,
                    ),
                )
        else:
            # Keep the implementation available while ensuring sessions configured by
            # an older Rodex release no longer intercept input or show completions.
            self._tmux(runtime, "pipe-pane", "-t", target)
            for key in ("Enter", "Tab"):
                self._tmux(runtime, "unbind-key", "-n", key)

    def _start_tmux_session(
        self,
        runtime: LiveTmuxSession,
        workspace: Path,
        host_command: str,
    ) -> None:
        """Set scrollback defaults before tmux allocates the session's first pane."""
        self._tmux(
            runtime,
            "set-option",
            "-g",
            "history-limit",
            str(RODEX_TMUX_HISTORY_LIMIT_LINES),
            ";",
            "set-option",
            "-g",
            "mouse",
            "on",
            ";",
            "new-session",
            "-d",
            "-s",
            runtime.tmux_session_name,
            "-c",
            str(workspace),
            host_command,
        )

    def attach(self, runtime: LiveTmuxSession) -> None:
        """Attach the calling terminal to the live Rodex tmux session."""
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        self._tmux(
            runtime,
            "attach-session",
            "-t",
            _exact_tmux_session_target(runtime.tmux_session_name),
            interactive=True,
            environment=environment,
        )

    def session_exists(self, runtime: LiveTmuxSession) -> bool:
        """Return whether the exact recorded tmux session is still running."""
        result = self._tmux(
            runtime,
            "has-session",
            "-t",
            _exact_tmux_session_target(runtime.tmux_session_name),
            check=False,
        )
        return result.returncode == 0

    def stop(self, runtime: LiveTmuxSession, *, check: bool = True) -> None:
        """Stop exactly one tmux session, allowing its supervisor to clean up."""
        self._tmux(
            runtime,
            "kill-session",
            "-t",
            _exact_tmux_session_target(runtime.tmux_session_name),
            check=check,
        )

    def _wait_for_single_codex_uuid(
        self,
        runtime: LiveRodexRuntime,
        *,
        requested_codex_uuid: uuid.UUID | None = None,
    ) -> uuid.UUID:
        deadline = self._monotonic() + self._startup_timeout_seconds
        last_error: BaseException | None = None
        while self._monotonic() < deadline:
            if not self.session_exists(runtime):
                detail = _read_runtime_error_detail(runtime.app_server_log_path)
                if requested_codex_uuid is not None and _codex_reports_missing_session(
                    detail, requested_codex_uuid
                ):
                    raise RodexCodexSessionNotFoundError(
                        "Codex has no saved session for exact identity "
                        f"{requested_codex_uuid}"
                    )
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

    def _read_tmux_option(
        self,
        runtime: LiveTmuxSession,
        target: str,
        option_name: str,
    ) -> str:
        result = self._tmux(
            runtime,
            "show-options",
            "-v",
            "-t",
            target,
            option_name,
        )
        value = result.stdout.strip()
        if not value:
            raise RodexRuntimeError(f"live tmux session does not advertise {option_name}")
        return value


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
    protocol_event_socket_path: Path,
    tmux_binary: str,
    tmux_server_socket_path: Path,
    codex_arguments: Sequence[str],
) -> int:
    """Supervise the app-server, protocol proxy, and foreground Codex TUI."""
    app_server_socket_path.unlink(missing_ok=True)
    protocol_proxy_socket_path.unlink(missing_ok=True)
    protocol_event_socket_path.unlink(missing_ok=True)
    app_server_log_path.parent.mkdir(parents=True, exist_ok=True)
    app_server: subprocess.Popen[bytes] | None = None
    tui: subprocess.Popen[bytes] | None = None
    protocol_proxy: CodexProtocolProxy | None = None
    protocol_event_tap: CodexProtocolEventTap | None = None
    runtime_path_keepalive: _RuntimePathKeepalive | None = None
    shutting_down = False

    def stop_on_signal(signum: int, _frame: object) -> None:
        if not shutting_down:
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
            protocol_event_tap = CodexProtocolEventTap(protocol_event_socket_path)
            protocol_event_tap.start()
            protocol_proxy = CodexProtocolProxy(
                protocol_proxy_socket_path,
                app_server_socket_path,
                ToolCallCounter(tool_call_status.update),
                protocol_event_tap.publish,
            )
            protocol_proxy.start()

            runtime_path_keepalive = _RuntimePathKeepalive(
                (
                    app_server_log_path.parent,
                    tmux_server_socket_path,
                    app_server_socket_path,
                    app_server_log_path,
                    protocol_proxy_socket_path,
                    protocol_event_socket_path,
                )
            )
            try:
                runtime_path_keepalive.start()
            except RodexRuntimeError as error:
                _record_runtime_path_keepalive_failure(log, error)
                raise
            tui_command = [
                codex_binary,
                "--no-alt-screen",
                "--remote",
                f"unix://{protocol_proxy_socket_path}",
                *codex_arguments,
            ]
            tui_options: dict[str, object] = {}
            if _requested_exact_codex_resume(codex_arguments) is not None:
                # Startup happens before attach, so preserve exact-resume failures where
                # the outer launcher can report them instead of losing the dead pane.
                tui_options["stderr"] = log
            tui = subprocess.Popen(tui_command, **tui_options)
            while True:
                failure = runtime_path_keepalive.failure
                if failure is not None:
                    _record_runtime_path_keepalive_failure(log, failure)
                    raise failure
                try:
                    returncode = tui.wait(timeout=_TUI_SUPERVISION_INTERVAL_SECONDS)
                except subprocess.TimeoutExpired:
                    continue
                break
            try:
                runtime_path_keepalive.close()
            except RodexRuntimeError as error:
                _record_runtime_path_keepalive_failure(log, error)
                raise
            failure = runtime_path_keepalive.failure
            if failure is not None:
                _record_runtime_path_keepalive_failure(log, failure)
                raise failure
            return returncode
    finally:
        shutting_down = True
        try:
            try:
                if runtime_path_keepalive is not None:
                    runtime_path_keepalive.close()
            finally:
                try:
                    if tui is not None:
                        _stop_child_process(tui)
                finally:
                    try:
                        if protocol_proxy is not None:
                            protocol_proxy.close()
                    finally:
                        try:
                            if protocol_event_tap is not None:
                                protocol_event_tap.close()
                        finally:
                            if app_server is not None:
                                _stop_child_process(app_server)
                            app_server_socket_path.unlink(missing_ok=True)
                            protocol_proxy_socket_path.unlink(missing_ok=True)
                            protocol_event_socket_path.unlink(missing_ok=True)
                            if (
                                app_server_log_path.exists()
                                and app_server_log_path.stat().st_size == 0
                            ):
                                app_server_log_path.unlink()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def _record_runtime_path_keepalive_failure(log: BinaryIO, error: RodexRuntimeError) -> None:
    """Persist why a detached runtime could no longer guarantee its paths."""
    log.write(f"Rodex runtime path keepalive failed: {error}\n".encode())
    log.flush()


def _stop_child_process(process: subprocess.Popen[bytes]) -> None:
    """Stop one exact child process, escalating only when it does not exit."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


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


def _requested_exact_codex_resume(
    codex_arguments: Sequence[str],
) -> uuid.UUID | None:
    """Return the UUID only for the exact resume form Rodex itself launches."""
    if len(codex_arguments) != 2 or codex_arguments[0] != "resume":
        return None
    try:
        return uuid.UUID(codex_arguments[1])
    except ValueError:
        return None


def _codex_reports_missing_session(detail: str, requested_uuid: uuid.UUID) -> bool:
    """Recognise Codex's exact-ID failure without weakening identity matching."""
    return f"No saved session found with ID {requested_uuid}" in detail
