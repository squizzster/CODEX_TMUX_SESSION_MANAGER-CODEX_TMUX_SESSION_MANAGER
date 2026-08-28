"""Own the small live boundary between Rodex, tmux, and Codex app-server."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import signal
import stat as stat_module
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Thread
from typing import Any, BinaryIO, Final

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import unix_connect

from rodex_registry.identity import (
    CodexSessionId,
    RodexRegistryId,
    RodexRuntimeId,
    RodexSessionId,
    parse_codex_session_id,
    parse_rodex_registry_id,
    parse_rodex_runtime_id,
)

from .agent_observer import AgentObserverPaneController, observer_control_socket_path
from .analytics import (
    AnalyticsSubprocessSupervisor,
    default_codex_sessions_root,
)
from .app_server_contract import (
    CODEX_APP_SERVER,
    RODEX_RUNTIME_APP_SERVER_CLIENT,
    RODEX_SESSION_CATALOG_APP_SERVER_CLIENT,
)
from .control import LiveRodexControl
from .process_contracts import AnalyticsWorkerConfig, SessionHostConfig
from .protocol_proxy import (
    CodexContextStatusObserver,
    CodexProtocolEventTap,
    CodexProtocolProxy,
    TmuxContextStatus,
    TmuxToolCallStatus,
    ToolCallCounter,
    publish_tui_notice,
)
from .status_bar import context_status_segment
from .tmux_status import (
    TmuxStatusPipeline,
)

SUN_PATH_MAX_BYTES: Final = 107
DEFAULT_STARTUP_TIMEOUT_SECONDS: Final = 15.0
CODEX_ACTIVE_WRITER_HANDOFF_TIMEOUT_SECONDS: Final = 10.0
CODEX_ACTIVE_WRITER_RETRY_INTERVAL_SECONDS: Final = 0.25
CODEX_PRIMARY_CONNECTION_RELEASE_TIMEOUT_SECONDS: Final = 2.5
RUNTIME_PATH_KEEPALIVE_INTERVAL_SECONDS: Final = 60.0 * 60.0
RODEX_REGISTRATION_TIMEOUT_SECONDS: Final = 60.0
RODEX_TMUX_HISTORY_LIMIT_LINES: Final = 50_000
_REGISTRATION_POLL_INTERVAL_SECONDS: Final = 1.0
_POLL_INTERVAL_SECONDS: Final = 0.05
_PROXY_SOCKET_OPTION: Final = "@rodex_protocol_proxy_socket_path"
_EVENT_SOCKET_OPTION: Final = "@rodex_protocol_event_socket_path"
_CODEX_SESSION_ID_OPTION: Final = "@rodex_codex_session_id"
_RODEX_SESSION_ID_OPTION: Final = "@rodex_session_id"
_REGISTRY_ID_OPTION: Final = "@rodex_registry_id"
_REGISTRATION_STATE_OPTION: Final = "@rodex_registration_state"
_RUNTIME_ID_OPTION: Final = "@rodex_runtime_id"
_INTERNAL_SESSION_ID_OPTION: Final = "@rodex_sessions_id"
RODEX_REGISTRATION_PENDING: Final = "pending"
RODEX_REGISTRATION_REGISTERED: Final = "registered"
RODEX_TMUX_REQUIRED_CLIENT_FEATURES: Final = "RGB"
# One switch owns installation of the tmux `/rodex` bindings and completion pipe.
RODEX_TMUX_SLASH_ENABLED: Final = False
Runner = Callable[..., subprocess.CompletedProcess[str]]
Connector = Callable[..., Any]
ProcessSpawner = Callable[..., subprocess.Popen[bytes]]
TuiNoticePublisher = Callable[[Path, str], bool]


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
        self._identities: dict[Path, tuple[int, int, int, int]] = {}
        self.failure_callback: Callable[[RodexRuntimeError], None] | None = None

    @property
    def failure(self) -> RodexRuntimeError | None:
        """Return the periodic refresh failure, if the worker stopped on one."""
        return self._failure

    def start(self) -> None:
        """Refresh synchronously, then protect the paths until closed."""
        if self._thread is not None:
            raise RodexRuntimeError("runtime path keepalive is already running")
        self._identities = {path: _runtime_path_identity(path) for path in self._paths}
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
                callback = self.failure_callback
                if callback is not None:
                    with suppress(Exception):
                        callback(error)
                return

    def _refresh(self) -> None:
        for path in self._paths:
            try:
                expected = self._identities[path]
                if _runtime_path_identity(path) != expected:
                    raise RodexRuntimeError(f"live runtime path identity changed: {path}")
                os.utime(path, None, follow_symlinks=False)
                if _runtime_path_identity(path) != expected:
                    raise RodexRuntimeError(
                        f"live runtime path identity changed while refreshing: {path}"
                    )
            except RodexRuntimeError:
                raise
            except OSError as error:
                raise RodexRuntimeError(
                    f"could not refresh live runtime path {path}: {error}"
                ) from error


def _runtime_path_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        state = path.lstat()
    except OSError as error:
        raise RodexRuntimeError(
            f"could not inspect live runtime path {path}: {error}"
        ) from error
    return (state.st_dev, state.st_ino, state.st_mode & 0o170000, state.st_uid)


@dataclass(frozen=True, slots=True)
class LiveTmuxSession:
    """The exact address of one running tmux session."""

    tmux_server_socket_path: Path
    tmux_session_name: str


@dataclass(frozen=True, slots=True)
class TmuxScrollbackSnapshot:
    """Plain tmux text split between committed history and the visible pane."""

    lines: tuple[str, ...]
    history_line_count: int

    def __post_init__(self) -> None:
        if not 0 <= self.history_line_count <= len(self.lines):
            raise ValueError("history line count must fit the captured pane")

    @property
    def history_lines(self) -> tuple[str, ...]:
        return self.lines[: self.history_line_count]

    @property
    def visible_lines(self) -> tuple[str, ...]:
        return self.lines[self.history_line_count :]


@dataclass(frozen=True, slots=True)
class CurrentTmuxPaneContext:
    """The live tmux session and attachment snapshot inherited by this process."""

    tmux_session: LiveTmuxSession
    tmux_session_id: str
    tmux_window_id: str
    tmux_pane_id: str
    attached_client_count: int


@dataclass(frozen=True, slots=True)
class LiveRodexRuntime(LiveTmuxSession):
    """Addresses for one running Codex TUI hosted by tmux."""

    app_server_socket_path: Path
    app_server_log_path: Path
    protocol_proxy_socket_path: Path
    protocol_event_socket_path: Path
    runtime_id: RodexRuntimeId | None = None


def _exact_tmux_session_target(session_name: str) -> str:
    """Disable tmux prefix/glob matching for a recorded session identity."""
    return f"={session_name}"


def _exact_tmux_pane_target(session_name: str) -> str:
    """Address a session exactly through commands whose target is a pane."""
    return f"={session_name}:"


def _tmux_socket_path_from_environment(tmux_value: str | None) -> Path:
    if not tmux_value:
        raise RodexRuntimeError(
            "rodex _context must run inside the tmux pane it is identifying"
        )
    fields = tmux_value.rsplit(",", 2)
    if (
        len(fields) != 3
        or not fields[0]
        or not fields[1].isdigit()
        or not fields[2].isdigit()
    ):
        raise RodexRuntimeError("the inherited TMUX environment is invalid")
    socket_path = Path(fields[0])
    if not socket_path.is_absolute():
        raise RodexRuntimeError("the inherited tmux socket path is not absolute")
    return socket_path


def _tmux_pane_id_from_environment(tmux_pane_value: str | None) -> str:
    if (
        not tmux_pane_value
        or not tmux_pane_value.startswith("%")
        or not tmux_pane_value[1:].isdigit()
    ):
        raise RodexRuntimeError("the inherited TMUX_PANE identity is invalid")
    return tmux_pane_value


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


def _shared_ctrl_c_binding_command(
    python_executable: str,
    tmux_binary: str,
    runtime: LiveTmuxSession,
) -> str:
    guard_command = shlex.join(
        (
            python_executable,
            "-m",
            "rodex.tmux_shared_ctrl_c",
            "--tmux-binary",
            tmux_binary,
            "--tmux-server-socket",
            str(runtime.tmux_server_socket_path),
            "--pane-id",
            "#{pane_id}",
            "--client-name",
            "#{client_name}",
            "--attached-count",
            "#{session_attached}",
        )
    )
    return f"run-shell -b {shlex.quote(guard_command)}"


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
        process_spawner: ProcessSpawner = subprocess.Popen,
        python_executable: str = sys.executable,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        startup_timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        attach_notice: Callable[[], str | None] | None = None,
        tui_notice_publisher: TuiNoticePublisher = publish_tui_notice,
    ) -> None:
        self._codex_binary = codex_binary
        self._tmux_binary = tmux_binary
        self._run = runner
        self._connect = connector
        self._spawn_process = process_spawner
        self._python_executable = python_executable
        self._monotonic = monotonic
        self._sleep = sleep
        self._startup_timeout_seconds = startup_timeout_seconds
        self._attach_notice = attach_notice
        self._publish_tui_notice = tui_notice_publisher

    def codex_session_is_persisted(self, codex_session_id: CodexSessionId) -> bool:
        """Ask a transient App Server whether an exact thread is resumable."""
        runtime_root = default_runtime_root()
        token = secrets.token_hex(8)
        socket_path = runtime_root / f"catalog-{token}.sock"
        log_path = runtime_root / f"catalog-{token}.log"
        _require_short_unix_socket_path(socket_path)
        with ExitStack() as cleanup:
            cleanup.callback(log_path.unlink, missing_ok=True)
            cleanup.callback(socket_path.unlink, missing_ok=True)
            log = _open_private_runtime_log(log_path)
            cleanup.callback(log.close)
            process = self._spawn_process(
                CODEX_APP_SERVER.command(self._codex_binary, socket_path),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
            cleanup.callback(_stop_child_process, process)
            _wait_for_app_server_socket(process, socket_path)
            return self._read_persisted_codex_session(socket_path, codex_session_id)

    def _read_persisted_codex_session(
        self,
        socket_path: Path,
        codex_session_id: CodexSessionId,
    ) -> bool:
        with self._connect(
            str(socket_path),
            uri=f"ws://localhost{CODEX_APP_SERVER.rpc_connection_path}",
            compression=None,
            open_timeout=1,
            close_timeout=1,
            max_size=None,
        ) as websocket:
            websocket.send(
                json.dumps(
                    CODEX_APP_SERVER.initialize_request(
                        0, RODEX_SESSION_CATALOG_APP_SERVER_CLIENT
                    )
                )
            )
            initialized = _receive_response(websocket, 0)
            CODEX_APP_SERVER.require_supported_version(initialized)
            websocket.send(json.dumps(CODEX_APP_SERVER.initialized_notification()))
            websocket.send(
                json.dumps(
                    CODEX_APP_SERVER.request(
                        1,
                        CODEX_APP_SERVER.thread_read_method,
                        {
                            "threadId": str(codex_session_id),
                            "includeTurns": False,
                        },
                    )
                )
            )
            response = _receive_message_for_request(websocket, 1)

        error = response.get("error")
        if error is not None:
            if _is_missing_persisted_codex_session(error, codex_session_id):
                return False
            raise RodexRuntimeError(f"app-server request failed: {error}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RodexRuntimeError("app-server response did not contain a result")
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise RodexRuntimeError("thread/read returned no Codex thread")
        try:
            observed_id = parse_codex_session_id(thread.get("id"))
        except (TypeError, ValueError) as error:
            raise RodexRuntimeError(
                "thread/read returned an invalid Codex thread ID"
            ) from error
        if observed_id != codex_session_id:
            raise RodexRuntimeError(
                "thread/read returned an unexpected Codex identity: "
                f"requested {codex_session_id}, observed {observed_id}"
            )
        if thread.get("ephemeral") is not False:
            raise RodexRuntimeError(f"Codex thread {codex_session_id} is not persisted")
        return True

    def start(
        self,
        workspace: Path,
        codex_arguments: Sequence[str],
        *,
        runtime_id: RodexRuntimeId,
        rodex_session_id: RodexSessionId | None = None,
        rodex_registry_id: RodexRegistryId | None = None,
        rodex_database_path: Path | None = None,
    ) -> tuple[LiveRodexRuntime, CodexSessionId]:
        """Start tmux and return only after its single Codex session ID is observable."""
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
            runtime_id=runtime_id,
        )
        _require_short_unix_socket_path(runtime.tmux_server_socket_path)
        _require_short_unix_socket_path(runtime.app_server_socket_path)
        _require_short_unix_socket_path(runtime.protocol_proxy_socket_path)
        _require_short_unix_socket_path(runtime.protocol_event_socket_path)
        _require_short_unix_socket_path(
            observer_control_socket_path(runtime.protocol_event_socket_path)
        )

        analytics_config: AnalyticsWorkerConfig | None = None
        if rodex_session_id is not None:
            if rodex_database_path is None or rodex_registry_id is None:
                raise RodexRuntimeError(
                    "Rodex runtime identity requires a registry ID and database path"
                )
            analytics_config = AnalyticsWorkerConfig(
                rodex_database_path=rodex_database_path,
                codex_sessions_root=default_codex_sessions_root(),
                rodex_session_id=rodex_session_id,
                rodex_registry_id=rodex_registry_id,
                runtime_id=runtime_id,
                protocol_event_socket_path=runtime.protocol_event_socket_path,
            )
        host_config = SessionHostConfig(
            codex_binary=self._codex_binary,
            app_server_socket_path=runtime.app_server_socket_path,
            app_server_log_path=runtime.app_server_log_path,
            protocol_proxy_socket_path=runtime.protocol_proxy_socket_path,
            protocol_event_socket_path=runtime.protocol_event_socket_path,
            tmux_binary=self._tmux_binary,
            tmux_server_socket_path=runtime.tmux_server_socket_path,
            codex_arguments=tuple(codex_arguments),
            analytics=analytics_config,
        )
        host_command = shlex.join(host_config.command(self._python_executable))
        self._start_tmux_session(runtime, resolved_workspace, host_command)
        try:
            requested_codex_session_id = _requested_exact_codex_resume(codex_arguments)
            codex_session_id = self._wait_for_single_codex_session_id(
                runtime,
                requested_codex_session_id=requested_codex_session_id,
            )
            if (
                requested_codex_session_id is not None
                and codex_session_id != requested_codex_session_id
            ):
                raise RodexRuntimeError(
                    "Codex resumed an unexpected exact identity: "
                    f"requested {requested_codex_session_id}, observed {codex_session_id}"
                )
            self.publish_runtime_control(
                runtime,
                codex_session_id,
                rodex_session_id,
                rodex_registry_id,
            )
        except BaseException:
            self.stop(runtime, check=False)
            raise
        return runtime, codex_session_id

    def publish_runtime_control(
        self,
        runtime: LiveRodexRuntime,
        codex_session_id: CodexSessionId,
        rodex_session_id: RodexSessionId | None = None,
        rodex_registry_id: RodexRegistryId | None = None,
    ) -> None:
        """Advertise live-only control metadata inside the owning tmux session."""
        if runtime.runtime_id is None:
            raise RodexRuntimeError("a new live runtime requires a runtime ID")
        target = _exact_tmux_pane_target(runtime.tmux_session_name)
        options = [
            (_PROXY_SOCKET_OPTION, str(runtime.protocol_proxy_socket_path)),
            (_EVENT_SOCKET_OPTION, str(runtime.protocol_event_socket_path)),
            (_CODEX_SESSION_ID_OPTION, str(codex_session_id)),
            (_RUNTIME_ID_OPTION, str(runtime.runtime_id)),
        ]
        if rodex_session_id is not None:
            if rodex_registry_id is None:
                raise RodexRuntimeError("Rodex session identity requires a registry ID")
            options.extend(
                (
                    (_RODEX_SESSION_ID_OPTION, str(rodex_session_id)),
                    (_REGISTRY_ID_OPTION, str(rodex_registry_id)),
                    (_REGISTRATION_STATE_OPTION, RODEX_REGISTRATION_PENDING),
                )
            )
        for option_name, value in options:
            self._tmux(runtime, "set-option", "-t", target, option_name, value)

    def confirm_runtime_registration(
        self,
        runtime: LiveTmuxSession,
        rodex_sessions_id: int,
    ) -> None:
        """Mark one exact live runtime usable only after its SQL identity commits."""
        if (
            not isinstance(rodex_sessions_id, int)
            or isinstance(rodex_sessions_id, bool)
            or rodex_sessions_id <= 0
        ):
            raise ValueError("rodex_sessions_id must be a positive integer")
        target = _exact_tmux_pane_target(runtime.tmux_session_name)
        self._tmux(
            runtime,
            "set-option",
            "-t",
            target,
            _INTERNAL_SESSION_ID_OPTION,
            str(rodex_sessions_id),
        )
        self._tmux(
            runtime,
            "set-option",
            "-t",
            target,
            _REGISTRATION_STATE_OPTION,
            RODEX_REGISTRATION_REGISTERED,
        )

    def discover_runtime_control(self, runtime: LiveTmuxSession) -> LiveRodexControl:
        """Read the current control endpoints from one exact live tmux session."""
        target = _exact_tmux_pane_target(runtime.tmux_session_name)
        proxy_path = self._read_tmux_option(runtime, target, _PROXY_SOCKET_OPTION)
        event_path = self._read_tmux_option(runtime, target, _EVENT_SOCKET_OPTION)
        codex_session_id_text = self._read_tmux_option(
            runtime, target, _CODEX_SESSION_ID_OPTION
        )
        rodex_session_id_text = self._read_optional_tmux_option(
            runtime, target, _RODEX_SESSION_ID_OPTION
        )
        registry_id_text = self._read_optional_tmux_option(
            runtime, target, _REGISTRY_ID_OPTION
        )
        registration_state = self._read_optional_tmux_option(
            runtime, target, _REGISTRATION_STATE_OPTION
        )
        runtime_id_text = self._read_optional_tmux_option(
            runtime, target, _RUNTIME_ID_OPTION
        )
        try:
            codex_session_id = parse_codex_session_id(codex_session_id_text)
        except ValueError as error:
            raise RodexRuntimeError(
                "live tmux session advertised an invalid Codex session ID"
            ) from error
        try:
            rodex_session_id = (
                None
                if rodex_session_id_text is None
                else RodexSessionId.parse(rodex_session_id_text)
            )
        except ValueError as error:
            raise RodexRuntimeError(
                "live tmux session advertised an invalid Rodex session ID"
            ) from error
        try:
            rodex_registry_id = (
                None
                if registry_id_text is None
                else parse_rodex_registry_id(registry_id_text)
            )
        except ValueError as error:
            raise RodexRuntimeError(
                "live tmux session advertised an invalid Rodex registry ID"
            ) from error
        try:
            runtime_id = (
                None if runtime_id_text is None else parse_rodex_runtime_id(runtime_id_text)
            )
        except ValueError as error:
            raise RodexRuntimeError(
                "live tmux session advertised an invalid runtime ID"
            ) from error
        return LiveRodexControl(
            Path(proxy_path),
            Path(event_path),
            codex_session_id,
            rodex_session_id,
            rodex_registry_id,
            registration_state,
            runtime_id,
        )

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

    def initialise_session_ui(self, runtime: LiveTmuxSession) -> None:
        """Install a fresh Rodex UI after creating one new tmux runtime."""
        self._configure_static_status(runtime, publish_base_status=True)
        self.refresh_name_bound_hooks(runtime)
        self._install_input_guards(runtime)

    def reconcile_session_ui(self, runtime: LiveTmuxSession) -> None:
        """Refresh static UI configuration without replacing a transient claim."""
        self._configure_static_status(runtime, publish_base_status=False)
        self.refresh_name_bound_hooks(runtime)
        self._install_input_guards(runtime)

    def refresh_name_bound_hooks(self, runtime: LiveTmuxSession) -> None:
        """Refresh only hooks whose command embeds the current tmux session name."""
        target = _exact_tmux_pane_target(runtime.tmux_session_name)
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

    def _configure_static_status(
        self,
        runtime: LiveTmuxSession,
        *,
        publish_base_status: bool,
    ) -> None:
        target = _exact_tmux_pane_target(runtime.tmux_session_name)
        status = TmuxStatusPipeline(lambda *args: self._tmux(runtime, *args), target)
        status.configure_base_status(
            reset_transient_claims=publish_base_status,
        )

    def _install_input_guards(self, runtime: LiveTmuxSession) -> None:
        target = _exact_tmux_pane_target(runtime.tmux_session_name)
        self._install_shared_ctrl_c_guard(runtime)
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
        notice: str | None = None
        if self._attach_notice is not None:
            with suppress(Exception):
                notice = self._attach_notice()
        if notice and isinstance(runtime, LiveRodexRuntime):
            with suppress(Exception):
                self._publish_tui_notice(runtime.protocol_proxy_socket_path, notice)
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        self._tmux(
            runtime,
            "-T",
            RODEX_TMUX_REQUIRED_CLIENT_FEATURES,
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

    def list_session_names(self, tmux_server_socket_path: Path) -> tuple[str, ...]:
        """List exact live names on one existing tmux server socket."""
        runtime = LiveTmuxSession(tmux_server_socket_path, "unused")
        result = self._tmux(
            runtime,
            "list-sessions",
            "-F",
            "#{session_name}",
            check=False,
        )
        if result.returncode != 0:
            return ()
        return tuple(name for line in result.stdout.splitlines() if (name := line.strip()))

    def capture_scrollback(self, runtime: LiveTmuxSession) -> tuple[str, ...]:
        """Read every retained physical line from one exact tmux pane."""
        result = self._tmux(
            runtime,
            "capture-pane",
            "-p",
            "-S",
            "-",
            "-t",
            _exact_tmux_pane_target(runtime.tmux_session_name),
        )
        return tuple(result.stdout.rstrip("\n").splitlines())

    def capture_scrollback_snapshot(
        self, runtime: LiveTmuxSession
    ) -> TmuxScrollbackSnapshot:
        """Capture plain pane text with tmux's committed-history boundary."""
        history_size_text = self._tmux(
            runtime,
            "display-message",
            "-p",
            "-t",
            _exact_tmux_pane_target(runtime.tmux_session_name),
            "-F",
            "#{history_size}",
        ).stdout.strip()
        if not history_size_text.isdigit():
            raise RodexRuntimeError("tmux returned an invalid history size")
        lines = self.capture_scrollback(runtime)
        history_line_count = int(history_size_text)
        if len(lines) < history_line_count:
            lines = (*lines, *("" for _ in range(history_line_count - len(lines))))
        return TmuxScrollbackSnapshot(lines, history_line_count)

    def discover_current_tmux_pane_context(
        self,
        environment: Mapping[str, str] | None = None,
    ) -> CurrentTmuxPaneContext:
        """Resolve the exact session containing the calling process's tmux pane."""
        inherited = os.environ if environment is None else environment
        socket_path = _tmux_socket_path_from_environment(inherited.get("TMUX"))
        pane_id = _tmux_pane_id_from_environment(inherited.get("TMUX_PANE"))
        socket_runtime = LiveTmuxSession(socket_path, "unused")
        result = self._tmux(
            socket_runtime,
            "display-message",
            "-p",
            "-t",
            pane_id,
            "-F",
            (
                "#{session_name}\t#{session_id}\t#{window_id}\t#{pane_id}\t"
                "#{session_attached}"
            ),
        )
        fields = result.stdout.rstrip("\n").split("\t")
        if (
            len(fields) != 5
            or not fields[0]
            or not fields[1].startswith("$")
            or not fields[1][1:].isdigit()
            or not fields[2].startswith("@")
            or not fields[2][1:].isdigit()
            or fields[3] != pane_id
        ):
            raise RodexRuntimeError("tmux returned an invalid current session context")
        try:
            attached_client_count = int(fields[4])
        except ValueError as error:
            raise RodexRuntimeError(
                "tmux returned an invalid attached-client count"
            ) from error
        if attached_client_count < 0:
            raise RodexRuntimeError("tmux returned an invalid attached-client count")
        return CurrentTmuxPaneContext(
            tmux_session=LiveTmuxSession(socket_path, fields[0]),
            tmux_session_id=fields[1],
            tmux_window_id=fields[2],
            tmux_pane_id=pane_id,
            attached_client_count=attached_client_count,
        )

    def set_mouse_mode(self, runtime: LiveTmuxSession, mode: str) -> str:
        """Set, toggle, inherit, or inspect mouse mode for one exact session."""
        if mode not in {"on", "off", "toggle", "inherit", "status"}:
            raise ValueError(f"unsupported tmux mouse mode: {mode}")
        target = _exact_tmux_pane_target(runtime.tmux_session_name)
        if mode == "inherit":
            self._tmux(runtime, "set-option", "-u", "-t", target, "mouse")
        elif mode == "toggle":
            self._tmux(runtime, "set-option", "-t", target, "mouse")
        elif mode != "status":
            self._tmux(runtime, "set-option", "-t", target, "mouse", mode)
        value = self._read_tmux_option(
            runtime,
            target,
            "mouse",
            include_inherited=True,
        )
        if value not in {"on", "off"}:
            raise RodexRuntimeError(f"tmux returned an invalid mouse mode: {value}")
        return value

    def stop(self, runtime: LiveTmuxSession, *, check: bool = True) -> None:
        """Stop exactly one tmux session, allowing its supervisor to clean up."""
        self._tmux(
            runtime,
            "kill-session",
            "-t",
            _exact_tmux_session_target(runtime.tmux_session_name),
            check=check,
        )

    def _wait_for_single_codex_session_id(
        self,
        runtime: LiveRodexRuntime,
        *,
        requested_codex_session_id: CodexSessionId | None = None,
    ) -> CodexSessionId:
        deadline = self._monotonic() + self._startup_timeout_seconds
        last_error: BaseException | None = None
        while self._monotonic() < deadline:
            if not self.session_exists(runtime):
                detail = _read_runtime_error_detail(runtime.app_server_log_path)
                if (
                    requested_codex_session_id is not None
                    and _codex_reports_missing_session(detail, requested_codex_session_id)
                ):
                    raise RodexCodexSessionNotFoundError(
                        "Codex has no saved session for exact identity "
                        f"{requested_codex_session_id}"
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
                            return parse_codex_session_id(loaded[0])
                        except ValueError as error:
                            raise RodexRuntimeError(
                                "app-server returned an invalid Codex session ID: "
                                f"{loaded[0]!r}"
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
            uri=f"ws://localhost{CODEX_APP_SERVER.rpc_connection_path}",
            compression=None,
            open_timeout=1,
            close_timeout=1,
        ) as websocket:
            websocket.send(
                json.dumps(
                    CODEX_APP_SERVER.initialize_request(0, RODEX_RUNTIME_APP_SERVER_CLIENT)
                )
            )
            _receive_response(websocket, 0)
            websocket.send(json.dumps(CODEX_APP_SERVER.initialized_notification()))
            websocket.send(
                json.dumps(
                    CODEX_APP_SERVER.request(
                        1, CODEX_APP_SERVER.thread_loaded_list_method, {}
                    )
                )
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
        *,
        include_inherited: bool = False,
    ) -> str:
        arguments = ["show-options"]
        if include_inherited:
            arguments.append("-A")
        arguments.extend(("-v", "-t", target, option_name))
        result = self._tmux(
            runtime,
            *arguments,
        )
        value = result.stdout.strip()
        if not value:
            raise RodexRuntimeError(f"live tmux session does not advertise {option_name}")
        return value

    def _read_optional_tmux_option(
        self,
        runtime: LiveTmuxSession,
        target: str,
        option_name: str,
    ) -> str | None:
        result = self._tmux(
            runtime,
            "show-options",
            "-v",
            "-t",
            target,
            option_name,
            check=False,
        )
        value = result.stdout.strip()
        return value or None

    def _install_shared_ctrl_c_guard(self, runtime: LiveTmuxSession) -> None:
        existing = self._tmux(
            runtime,
            "list-keys",
            "-T",
            "root",
            "C-c",
            check=False,
        )
        if (
            existing.returncode == 0
            and existing.stdout.strip()
            and "rodex.tmux_shared_ctrl_c" not in existing.stdout
        ):
            return
        self._tmux(
            runtime,
            "bind-key",
            "-n",
            "C-c",
            _shared_ctrl_c_binding_command(
                self._python_executable,
                self._tmux_binary,
                runtime,
            ),
        )


def default_runtime_root_path() -> Path:
    """Resolve the shared runtime root without creating or changing it."""
    configured = os.environ.get("RODEX_RUNTIME_DIR")
    if configured:
        return Path(os.path.abspath(Path(configured).expanduser()))

    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        candidate = Path(os.path.abspath(Path(xdg_runtime).expanduser())) / "rodex"
        if len(os.fsencode(candidate / "app-0000000000000000.sock")) <= SUN_PATH_MAX_BYTES:
            return candidate
    return Path("/tmp") / f"rodex-{os.getuid()}"


def default_tmux_server_socket_path() -> Path:
    """Resolve the default shared tmux socket without mutating runtime state."""
    return default_runtime_root_path() / "tmux.sock"


def default_runtime_root() -> Path:
    """Return a prepared private runtime directory shared by Rodex sessions."""
    return _prepare_runtime_root(default_runtime_root_path())


def run_session_host(
    config: SessionHostConfig,
    *,
    analytics_supervisor_factory: Callable[
        [AnalyticsWorkerConfig], AnalyticsSubprocessSupervisor
    ] = AnalyticsSubprocessSupervisor,
) -> int:
    """Supervise the app-server, protocol proxy, and foreground Codex TUI."""
    codex_binary = config.codex_binary
    app_server_socket_path = config.app_server_socket_path
    app_server_log_path = config.app_server_log_path
    protocol_proxy_socket_path = config.protocol_proxy_socket_path
    protocol_event_socket_path = config.protocol_event_socket_path
    tmux_binary = config.tmux_binary
    tmux_server_socket_path = config.tmux_server_socket_path
    codex_arguments = config.codex_arguments
    analytics_config = config.analytics
    app_server_socket_path.unlink(missing_ok=True)
    protocol_proxy_socket_path.unlink(missing_ok=True)
    protocol_event_socket_path.unlink(missing_ok=True)
    app_server_log_path.parent.mkdir(parents=True, exist_ok=True)
    app_server: subprocess.Popen[bytes] | None = None
    tui: subprocess.Popen[bytes] | None = None
    protocol_proxy: CodexProtocolProxy | None = None
    protocol_event_tap: CodexProtocolEventTap | None = None
    context_status_observer: CodexContextStatusObserver | None = None
    runtime_path_keepalive: _RuntimePathKeepalive | None = None
    analytics_supervisor: AnalyticsSubprocessSupervisor | None = None
    agent_observer_controller: AgentObserverPaneController | None = None
    registration_deadline = (
        None
        if analytics_config is None
        else time.monotonic() + RODEX_REGISTRATION_TIMEOUT_SECONDS
    )
    shutting_down = False

    def stop_on_signal(signum: int, _frame: object) -> None:
        if not shutting_down:
            raise SystemExit(128 + signum)

    def leave_sigint_to_foreground_tui(_signum: int, _frame: object) -> None:
        return None

    previous_handlers = {
        signum: signal.signal(signum, stop_on_signal)
        for signum in (signal.SIGHUP, signal.SIGTERM)
    }
    try:
        with _open_private_runtime_log(app_server_log_path) as log:
            app_server = subprocess.Popen(
                CODEX_APP_SERVER.command(codex_binary, app_server_socket_path),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            _wait_for_app_server_socket(app_server, app_server_socket_path)
            app_server_socket_path.chmod(0o600)
            tmux_pane_target = os.environ.get("TMUX_PANE", "")
            if analytics_config is not None and tmux_pane_target:
                agent_observer_controller = AgentObserverPaneController(
                    tmux_binary,
                    tmux_server_socket_path,
                    tmux_pane_target,
                    protocol_event_socket_path,
                )
            tool_call_status = TmuxToolCallStatus(
                tmux_binary,
                tmux_server_socket_path,
                tmux_pane_target,
            )
            tool_call_status.update(0)
            context_status = TmuxContextStatus(
                tmux_binary,
                tmux_server_socket_path,
                tmux_pane_target,
            )
            context_status.update(context_status_segment(None))
            live_context_observer = CodexContextStatusObserver(
                context_status.update,
                codex_sessions_root=(
                    analytics_config.codex_sessions_root
                    if analytics_config is not None
                    else default_codex_sessions_root()
                ),
            )
            context_status_observer = live_context_observer
            live_event_tap = CodexProtocolEventTap(protocol_event_socket_path)
            protocol_event_tap = live_event_tap
            live_event_tap.start()

            def publish_primary_server_message(
                message: str | bytes,
                event: dict[str, Any] | None,
            ) -> None:
                live_context_observer.observe_protocol_event(event)
                if agent_observer_controller is not None:
                    with suppress(Exception):
                        agent_observer_controller.observe_protocol_event(event)
                live_event_tap.publish_protocol_event(message, event)

            protocol_proxy = CodexProtocolProxy(
                protocol_proxy_socket_path,
                app_server_socket_path,
                ToolCallCounter(tool_call_status.update),
                publish_primary_server_message,
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

            def stop_tui_after_keepalive_failure(_error: RodexRuntimeError) -> None:
                active_tui = tui
                if active_tui is None or active_tui.poll() is not None:
                    return
                _stop_child_process(active_tui)

            runtime_path_keepalive.failure_callback = stop_tui_after_keepalive_failure
            try:
                runtime_path_keepalive.start()
            except RodexRuntimeError as error:
                _record_runtime_path_keepalive_failure(log, error)
                raise
            tui_command = [
                codex_binary,
                # Rodex checks App Server compatibility itself. An interactive
                # updater here would block thread registration before attach.
                "--config",
                "check_for_update_on_startup=false",
                "--no-alt-screen",
                "--remote",
                f"unix://{protocol_proxy_socket_path}",
                *codex_arguments,
            ]
            tui_options: dict[str, object] = {}
            requested_codex_session_id = _requested_exact_codex_resume(codex_arguments)
            if requested_codex_session_id is not None:
                # Startup happens before attach, so preserve exact-resume failures where
                # the outer launcher can report them instead of losing the dead pane.
                tui_options["stderr"] = log
            active_writer_deadline = (
                None
                if requested_codex_session_id is None
                else time.monotonic() + CODEX_ACTIVE_WRITER_HANDOFF_TIMEOUT_SECONDS
            )
            inherited_sigint_handler: object | None = None
            while True:
                attempt_log_offset = os.fstat(log.fileno()).st_size
                if inherited_sigint_handler is not None:
                    signal.signal(signal.SIGINT, inherited_sigint_handler)
                try:
                    tui = subprocess.Popen(tui_command, **tui_options)
                finally:
                    replaced_handler = signal.signal(
                        signal.SIGINT,
                        leave_sigint_to_foreground_tui,
                    )
                    if inherited_sigint_handler is None:
                        inherited_sigint_handler = replaced_handler
                        previous_handlers[signal.SIGINT] = replaced_handler
                while True:
                    if analytics_config is not None and analytics_supervisor is None:
                        activated_analytics = _registered_analytics_worker_config(
                            analytics_config,
                            tmux_binary,
                            tmux_server_socket_path,
                            tmux_pane_target,
                        )
                        if activated_analytics is not None:
                            registration_deadline = None
                            candidate_supervisor: AnalyticsSubprocessSupervisor | None = (
                                None
                            )
                            try:
                                candidate_supervisor = analytics_supervisor_factory(
                                    activated_analytics
                                )
                                candidate_supervisor.start()
                                analytics_supervisor = candidate_supervisor
                                if agent_observer_controller is not None:
                                    with suppress(Exception):
                                        assert (
                                            activated_analytics.rodex_sessions_id
                                            is not None
                                        )
                                        assert (
                                            activated_analytics.codex_session_id is not None
                                        )
                                        agent_observer_controller.activate(
                                            database_path=(
                                                activated_analytics.rodex_database_path
                                            ),
                                            rodex_sessions_id=(
                                                activated_analytics.rodex_sessions_id
                                            ),
                                            rodex_session_id=str(
                                                activated_analytics.rodex_session_id
                                            ),
                                            root_thread_id=(
                                                activated_analytics.codex_session_id
                                            ),
                                        )
                            except Exception:
                                # Persistent statistics are strictly off the interactive
                                # path; a failed sidecar must not break the Codex TUI.
                                if candidate_supervisor is not None:
                                    with suppress(Exception):
                                        candidate_supervisor.close()
                                analytics_config = None
                                analytics_supervisor = None
                        elif (
                            registration_deadline is not None
                            and time.monotonic() >= registration_deadline
                        ):
                            raise RodexRuntimeError(
                                "runtime registration was not confirmed before its deadline"
                            )
                    failure = runtime_path_keepalive.failure
                    if failure is not None:
                        _record_runtime_path_keepalive_failure(log, failure)
                        raise failure
                    try:
                        registration_pending = (
                            analytics_config is not None and analytics_supervisor is None
                        )
                        returncode = tui.wait(
                            timeout=(
                                _REGISTRATION_POLL_INTERVAL_SECONDS
                                if registration_pending
                                else None
                            )
                        )
                    except subprocess.TimeoutExpired:
                        continue
                    break
                if (
                    returncode == 0
                    or requested_codex_session_id is None
                    or active_writer_deadline is None
                    or not _active_writer_handoff_can_retry(
                        _read_runtime_log_since(log, attempt_log_offset),
                        requested_codex_session_id,
                        now=time.monotonic(),
                        deadline=active_writer_deadline,
                    )
                ):
                    break
                protocol_proxy.wait_for_primary_connection_release(
                    CODEX_PRIMARY_CONNECTION_RELEASE_TIMEOUT_SECONDS
                )
                time.sleep(CODEX_ACTIVE_WRITER_RETRY_INTERVAL_SECONDS)
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
                try:
                    if analytics_supervisor is not None:
                        analytics_supervisor.close()
                except Exception:
                    pass
                finally:
                    try:
                        if agent_observer_controller is not None:
                            agent_observer_controller.close()
                    finally:
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
                            if context_status_observer is not None:
                                context_status_observer.close()
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


def _runtime_registration_is_confirmed(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    tmux_pane_target: str,
) -> bool:
    if not tmux_pane_target:
        return False
    result = subprocess.run(
        [
            tmux_binary,
            "-S",
            str(tmux_server_socket_path),
            "show-options",
            "-v",
            "-t",
            tmux_pane_target,
            _REGISTRATION_STATE_OPTION,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    return result.returncode == 0 and result.stdout.strip() == RODEX_REGISTRATION_REGISTERED


def _registered_analytics_worker_config(
    config: AnalyticsWorkerConfig,
    tmux_binary: str,
    tmux_server_socket_path: Path,
    tmux_pane_target: str,
) -> AnalyticsWorkerConfig | None:
    """Read and validate one post-commit analytics activation manifest."""
    if not tmux_pane_target:
        return None
    result = subprocess.run(
        [
            tmux_binary,
            "-S",
            str(tmux_server_socket_path),
            "show-options",
            "-t",
            tmux_pane_target,
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    options: dict[str, str] = {}
    try:
        for line in result.stdout.splitlines():
            fields = shlex.split(line)
            if len(fields) >= 2:
                options[fields[0]] = " ".join(fields[1:])
    except ValueError as error:
        raise RodexRuntimeError("live runtime options could not be parsed") from error
    if options.get(_REGISTRATION_STATE_OPTION) != RODEX_REGISTRATION_REGISTERED:
        return None
    expected = {
        _RODEX_SESSION_ID_OPTION: str(config.rodex_session_id),
        _REGISTRY_ID_OPTION: str(config.rodex_registry_id),
        _RUNTIME_ID_OPTION: str(config.runtime_id),
        _EVENT_SOCKET_OPTION: str(config.protocol_event_socket_path),
    }
    for option_name, expected_value in expected.items():
        if options.get(option_name) != expected_value:
            raise RodexRuntimeError(
                f"registered analytics identity disagrees at {option_name}"
            )
    try:
        rodex_sessions_id = int(options[_INTERNAL_SESSION_ID_OPTION])
        codex_session_id = parse_codex_session_id(options[_CODEX_SESSION_ID_OPTION])
    except (KeyError, ValueError) as error:
        raise RodexRuntimeError(
            "registered analytics identity is missing or invalid"
        ) from error
    return config.activate(
        rodex_sessions_id=rodex_sessions_id,
        codex_session_id=codex_session_id,
    )


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
    message = _receive_message_for_request(websocket, request_id)
    if "error" in message:
        raise RodexRuntimeError(f"app-server request failed: {message['error']}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise RodexRuntimeError("app-server response did not contain a result")
    return result


def _receive_message_for_request(websocket: Any, request_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        message = json.loads(websocket.recv(timeout=max(0.01, deadline - time.monotonic())))
        if not isinstance(message, dict):
            raise RodexRuntimeError("app-server response was not an object")
        if message.get("id") != request_id:
            continue
        return message
    raise TimeoutError(f"timed out waiting for app-server response {request_id}")


def _is_missing_persisted_codex_session(
    error: object, codex_session_id: CodexSessionId
) -> bool:
    return (
        isinstance(error, dict)
        and error.get("code") == -32600
        and error.get("message") == f"thread not loaded: {codex_session_id}"
    )


def _prepare_runtime_root(root: Path) -> Path:
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_secure_runtime_parent(root.parent)
    with suppress(FileExistsError):
        root.mkdir(mode=0o700)
    before = root.lstat()
    if stat_module.S_ISLNK(before.st_mode) or not stat_module.S_ISDIR(before.st_mode):
        raise RodexRuntimeError(f"runtime path is not a real directory: {root}")
    if before.st_uid != os.getuid():
        raise RodexRuntimeError(
            f"runtime directory is not owned by uid {os.getuid()}: {root}"
        )
    os.chmod(root, 0o700, follow_symlinks=False)
    after = root.lstat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RodexRuntimeError(f"runtime directory changed while securing it: {root}")
    return root


def _require_secure_runtime_parent(parent: Path) -> None:
    state = parent.lstat()
    if stat_module.S_ISLNK(state.st_mode):
        raise RodexRuntimeError(f"runtime parent is a symbolic link: {parent}")
    if not stat_module.S_ISDIR(state.st_mode):
        raise RodexRuntimeError(f"runtime parent is not a directory: {parent}")
    if state.st_uid == os.getuid() and state.st_mode & 0o022 == 0:
        return
    if state.st_uid == 0 and state.st_mode & stat_module.S_ISVTX:
        return
    raise RodexRuntimeError(
        f"runtime parent is not private or root-owned sticky storage: {parent}"
    )


def _open_private_runtime_log(path: Path) -> BinaryIO:
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        state = os.fstat(descriptor)
        if not stat_module.S_ISREG(state.st_mode) or state.st_uid != os.getuid():
            raise RodexRuntimeError(f"runtime log is not a private regular file: {path}")
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "a+b", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


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


def _read_runtime_log_since(log: BinaryIO, offset: int) -> str:
    """Read only diagnostics emitted by one TUI launch attempt."""
    log.flush()
    log.seek(offset)
    detail = log.read().decode("utf-8", errors="replace")
    log.seek(0, os.SEEK_END)
    return detail[-1000:]


def _requested_exact_codex_resume(
    codex_arguments: Sequence[str],
) -> CodexSessionId | None:
    """Return the Codex session ID from the exact resume form Rodex launches."""
    if len(codex_arguments) != 2 or codex_arguments[0] != "resume":
        return None
    try:
        return parse_codex_session_id(codex_arguments[1])
    except ValueError:
        return None


def _codex_reports_missing_session(
    detail: str, requested_codex_session_id: CodexSessionId
) -> bool:
    """Recognise Codex's exact-ID failure without weakening identity matching."""
    return f"No saved session found with ID {requested_codex_session_id}" in detail


def _codex_reports_active_writer(
    detail: str, requested_codex_session_id: CodexSessionId
) -> bool:
    """Recognise the bounded shutdown handoff conflict for an exact thread."""
    return (
        "thread-store conflict" in detail
        and f"thread {requested_codex_session_id} already has an active writer" in detail
    )


def _active_writer_handoff_can_retry(
    detail: str,
    requested_codex_session_id: CodexSessionId,
    *,
    now: float,
    deadline: float,
) -> bool:
    """Bound retries to the exact writer and the startup handoff window."""
    return now < deadline and _codex_reports_active_writer(
        detail, requested_codex_session_id
    )
