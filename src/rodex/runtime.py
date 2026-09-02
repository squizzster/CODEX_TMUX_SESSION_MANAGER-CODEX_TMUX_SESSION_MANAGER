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
from dataclasses import dataclass, field, replace
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

from .agent_observer import AgentObserverCoordinator, observer_control_socket_path
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
from .primary_connection_lifecycle import PrimaryConnectionLifecycleCoordinator
from .process_contracts import AnalyticsWorkerConfig, SessionHostConfig
from .process_environment import (
    TMUX_OWNED_CHILD_ENVIRONMENT_VARIABLES,
    exact_environment_exec_command,
    user_process_environment,
    validated_user_environment_entries,
)
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
from .tmux_executor import SyncTmuxExecutor, TmuxCommandResult
from .tmux_session_capability import (
    RODEX_CODEX_SESSION_ID_OPTION,
    RODEX_INTERNAL_SESSION_ID_OPTION,
    RODEX_PRIMARY_PANE_ID_OPTION,
    RODEX_PROTOCOL_EVENT_SOCKET_OPTION,
    RODEX_PROTOCOL_PROXY_SOCKET_OPTION,
    RODEX_REGISTRATION_PENDING,
    RODEX_REGISTRATION_REGISTERED,
    RODEX_REGISTRATION_STATE_OPTION,
    RODEX_REGISTRY_ID_OPTION,
    RODEX_RUNTIME_ID_OPTION,
    RODEX_SESSION_ID_OPTION,
    RODEX_SHARED_TMUX_PROTOCOL,
    RODEX_SHARED_TMUX_PROTOCOL_OPTION,
    RODEX_SHARED_TMUX_SERVER_ID_OPTION,
    RODEX_SHARED_TMUX_SOCKET_NAME,
    TmuxRuntimeCapability,
    TmuxSessionCapability,
    combine_tmux_if_shell_conditions,
    parse_tmux_server_id,
    parse_tmux_session_capability,
    primary_pane_capability_if_shell_condition,
    registered_primary_pane_if_shell_condition,
    server_identity_if_shell_condition,
    tmux_format_literal,
)
from .tmux_sharing_coordinator import (
    RODEX_SHARING_ATTACHED_COUNT_OPTION,
    sharing_coordinator_hook_command,
)
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
RODEX_TMUX_SCROLLBACK_STATE_LINES: Final = 256
RODEX_TMUX_COMMAND_TIMEOUT_SECONDS: Final = 5.0
RODEX_TMUX_RENAME_TIMEOUT_SECONDS: Final = 5.0
_REGISTRATION_POLL_INTERVAL_SECONDS: Final = 1.0
_POLL_INTERVAL_SECONDS: Final = 0.05
RODEX_TMUX_REQUIRED_CLIENT_FEATURES: Final = "RGB"
RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION: Final = (
    "@rodex_shared_tmux_coordinator_command"
)
RODEX_SHARED_TMUX_CTRL_C_COMMAND_OPTION: Final = "@rodex_shared_tmux_ctrl_c_command"
RODEX_SHARED_TMUX_HOOK_INDEX: Final = 731
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
    runtime_id: RodexRuntimeId | None = field(default=None, kw_only=True)
    tmux_capability: TmuxSessionCapability | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        capability = self.tmux_capability
        if capability is None:
            return
        if capability.tmux_server_socket_path != self.tmux_server_socket_path:
            raise ValueError("tmux capability belongs to a different server socket")
        if self.runtime_id != capability.runtime_id:
            raise ValueError("tmux capability belongs to a different runtime incarnation")


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
class TmuxScrollbackState:
    """Bounded pane content plus the identity needed for cheap tail continuity."""

    history_line_count: int
    history_tail_lines: tuple[str, ...]
    visible_lines: tuple[str, ...]
    runtime_identity: str

    def __post_init__(self) -> None:
        if self.history_line_count < len(self.history_tail_lines):
            raise ValueError("history tail cannot exceed tmux's retained history")
        if not self.runtime_identity:
            raise ValueError("scrollback state requires a runtime identity")


def _split_tmux_capture(output: str) -> tuple[str, str]:
    metadata, separator, captured = output.partition("\n")
    if not separator:
        raise RodexRuntimeError("tmux omitted scrollback capture metadata")
    return metadata, captured


def _captured_tmux_lines(captured: str) -> tuple[str, ...]:
    return tuple(captured.rstrip("\n").splitlines())


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


def _shared_ctrl_c_binding_command(
    python_executable: str,
    tmux_binary: str,
    runtime: LiveTmuxSession,
    tmux_server_id: str,
) -> str:
    guard_command = shlex.join(
        (
            tmux_format_literal(python_executable),
            "-m",
            "rodex.tmux_shared_ctrl_c",
            "--tmux-binary",
            tmux_format_literal(tmux_binary),
            "--tmux-server-socket",
            tmux_format_literal(str(runtime.tmux_server_socket_path)),
            "--expected-server-id",
            tmux_server_id,
        )
    )
    guard_command += _quoted_tmux_format_arguments(
        ("--tmux-session-id", "session_id"),
        ("--tmux-primary-pane-id", RODEX_PRIMARY_PANE_ID_OPTION),
        ("--expected-runtime-id", RODEX_RUNTIME_ID_OPTION),
        ("--expected-rodex-session-id", RODEX_SESSION_ID_OPTION),
        ("--expected-registry-id", RODEX_REGISTRY_ID_OPTION),
        ("--expected-internal-session-id", RODEX_INTERNAL_SESSION_ID_OPTION),
        ("--expected-codex-session-id", RODEX_CODEX_SESSION_ID_OPTION),
        ("--pane-id", "pane_id"),
        ("--client-name", "client_name"),
    )
    return f"run-shell -b {shlex.quote(guard_command)}"


def _quoted_tmux_format_arguments(*arguments: tuple[str, str]) -> str:
    """Append shell-safe values that tmux expands only when a key is invoked."""
    return "".join(
        f" {shlex.quote(option)} #{{q:{format_name}}}" for option, format_name in arguments
    )


def _shell_commands_are_equivalent(left: str, right: str) -> bool:
    """Compare tmux-normalized shell commands without trusting textual quoting."""
    if not left or not right:
        return False
    try:
        return shlex.split(left) == shlex.split(right)
    except ValueError:
        return False


def _tmux_command_queue(arguments: Sequence[str]) -> str:
    """Serialize an argv-style tmux command queue without quoting separators."""
    commands: list[list[str]] = [[]]
    for argument in arguments:
        if argument == ";":
            if not commands[-1]:
                raise ValueError("tmux command queue contains an empty command")
            commands.append([])
        else:
            commands[-1].append(argument)
    if not commands[-1]:
        raise ValueError("tmux command queue ends with an empty command")
    return " ; ".join(shlex.join(command) for command in commands)


def _tmux_source_command_queue(arguments: Sequence[str]) -> str:
    """Serialize a tmux source queue as ASCII with every argument byte escaped."""
    commands: list[list[str]] = [[]]
    for argument in arguments:
        if argument == ";":
            if not commands[-1]:
                raise ValueError("tmux source queue contains an empty command")
            commands.append([])
        elif not isinstance(argument, str) or "\x00" in argument:
            raise ValueError("tmux source queue contains invalid command text")
        else:
            commands[-1].append(argument)
    if not commands[-1]:
        raise ValueError("tmux source queue ends with an empty command")
    return " ; ".join(
        " ".join(_tmux_source_byte_string(argument) for argument in command)
        for command in commands
    )


def _tmux_source_byte_string(value: str) -> str:
    """Encode one Linux text value without tmux format or config interpolation."""
    return '"' + "".join(f"\\{byte:03o}" for byte in os.fsencode(value)) + '"'


def _parse_tmux_shell_environment_names(shell_commands: str) -> frozenset[str]:
    """Parse tmux's repeated assignment/export records without evaluating values."""
    names: set[str] = set()
    offset = 0
    while offset < len(shell_commands):
        assignment = _parse_tmux_shell_environment_assignment(shell_commands, offset)
        if assignment is not None:
            name, offset = assignment
            names.add(name)
            continue
        if shell_commands.startswith("unset ", offset):
            record_end = shell_commands.find(";\n", offset)
            if record_end >= 0:
                offset = record_end + 2
                continue
        raise ValueError("tmux returned a malformed global environment")
    return frozenset(names)


def _parse_tmux_shell_environment_assignment(
    shell_commands: str,
    offset: int,
) -> tuple[str, int] | None:
    assignment_index = shell_commands.find("=", offset)
    if assignment_index < 0:
        return None
    name = shell_commands[offset:assignment_index]
    value_start = assignment_index + 1
    if not name or "\x00" in name or shell_commands[value_start : value_start + 1] != '"':
        return None
    cursor = value_start + 1
    while cursor < len(shell_commands):
        character = shell_commands[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == '"':
            break
        cursor += 1
    else:
        return None
    suffix = f"; export {name};"
    if not shell_commands.startswith(suffix, cursor + 1):
        return None
    record_end = cursor + 1 + len(suffix)
    if record_end == len(shell_commands):
        return name, record_end
    if shell_commands[record_end] != "\n":
        return None
    return name, record_end + 1


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
        environment: Mapping[str, str] | None = None,
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
        self._user_process_environment = user_process_environment(
            os.environ if environment is None else environment
        )

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
                env=self._user_process_environment,
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
            CODEX_APP_SERVER.require_minimum_version(initialized)
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
            tmux_server_socket_path=default_tmux_server_socket_path(),
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
            runtime_id=runtime_id,
            codex_arguments=tuple(codex_arguments),
            analytics=analytics_config,
        )
        try:
            self._start_tmux_session(
                runtime,
                resolved_workspace,
                host_config.command(self._python_executable),
            )
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
        except BaseException as failure:
            self._stop_startup_runtime(runtime, failure=failure)
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
        capability = self._resolve_bootstrap_tmux_capability(runtime)
        target = capability.pane_target
        options = [
            (RODEX_PROTOCOL_PROXY_SOCKET_OPTION, str(runtime.protocol_proxy_socket_path)),
            (RODEX_PROTOCOL_EVENT_SOCKET_OPTION, str(runtime.protocol_event_socket_path)),
            (RODEX_CODEX_SESSION_ID_OPTION, str(codex_session_id)),
            (RODEX_RUNTIME_ID_OPTION, str(runtime.runtime_id)),
        ]
        if rodex_session_id is not None:
            if rodex_registry_id is None:
                raise RodexRuntimeError("Rodex session identity requires a registry ID")
            options.extend(
                (
                    (RODEX_SESSION_ID_OPTION, str(rodex_session_id)),
                    (RODEX_REGISTRY_ID_OPTION, str(rodex_registry_id)),
                    (RODEX_REGISTRATION_STATE_OPTION, RODEX_REGISTRATION_PENDING),
                )
            )
        publication_action = " ; ".join(
            shlex.join(("set-option", "-t", target, option_name, value))
            for option_name, value in options
        )
        self._tmux(
            runtime,
            "if-shell",
            "-t",
            target,
            "-F",
            primary_pane_capability_if_shell_condition(capability),
            publication_action,
            shlex.join(("run-shell", "false")),
        )

    def confirm_runtime_registration(
        self,
        runtime: LiveTmuxSession,
        rodex_sessions_id: int,
        *,
        expected_rodex_session_id: RodexSessionId,
        expected_registry_id: RodexRegistryId,
        expected_codex_session_id: CodexSessionId,
    ) -> None:
        """Mark one exact live runtime usable only after its SQL identity commits."""
        if (
            not isinstance(rodex_sessions_id, int)
            or isinstance(rodex_sessions_id, bool)
            or rodex_sessions_id <= 0
        ):
            raise ValueError("rodex_sessions_id must be a positive integer")
        if runtime.runtime_id is None:
            raise RodexRuntimeError("runtime registration requires a runtime ID")
        bootstrap_capability = self._resolve_bootstrap_tmux_capability(runtime)
        target = bootstrap_capability.pane_target
        control = self.discover_runtime_control(runtime)
        if (
            control.runtime_id != runtime.runtime_id
            or control.registration_state != RODEX_REGISTRATION_PENDING
            or control.rodex_session_id != expected_rodex_session_id
            or control.rodex_registry_id != expected_registry_id
            or control.codex_session_id != expected_codex_session_id
        ):
            raise RodexRuntimeError(
                "runtime registration pending identity disagrees with durable identity"
            )
        condition = combine_tmux_if_shell_conditions(
            primary_pane_capability_if_shell_condition(bootstrap_capability),
            (
                f"#{{==:#{{{RODEX_REGISTRATION_STATE_OPTION}}},"
                f"{RODEX_REGISTRATION_PENDING}}}"
            ),
            f"#{{==:#{{{RODEX_SESSION_ID_OPTION}}},{expected_rodex_session_id}}}",
            f"#{{==:#{{{RODEX_REGISTRY_ID_OPTION}}},{expected_registry_id}}}",
            f"#{{==:#{{{RODEX_CODEX_SESSION_ID_OPTION}}},{expected_codex_session_id}}}",
            f"#{{==:#{{{RODEX_INTERNAL_SESSION_ID_OPTION}}},}}",
        )
        action = " ; ".join(
            (
                shlex.join(
                    (
                        "set-option",
                        "-t",
                        target,
                        RODEX_INTERNAL_SESSION_ID_OPTION,
                        str(rodex_sessions_id),
                    )
                ),
                shlex.join(
                    (
                        "set-option",
                        "-t",
                        target,
                        RODEX_REGISTRATION_STATE_OPTION,
                        RODEX_REGISTRATION_REGISTERED,
                    )
                ),
            )
        )
        self._tmux(
            runtime,
            "if-shell",
            "-t",
            target,
            "-F",
            condition,
            action,
        )
        confirmed = self._resolve_registered_tmux_capability(runtime)
        if (
            confirmed.internal_session_id != rodex_sessions_id
            or confirmed.rodex_session_id != expected_rodex_session_id
            or confirmed.registry_id != expected_registry_id
            or confirmed.codex_session_id != expected_codex_session_id
        ):
            raise RodexRuntimeError("runtime registration CAS did not commit exactly")

    def discover_runtime_control(self, runtime: LiveTmuxSession) -> LiveRodexControl:
        """Read one coherent server/session/control capability snapshot."""
        tmux_session_id = self._stable_tmux_session_target(runtime)
        target = f"{tmux_session_id}:"
        record_format = "\t".join(
            (
                f"#{{{RODEX_SHARED_TMUX_PROTOCOL_OPTION}}}",
                f"#{{{RODEX_SHARED_TMUX_SERVER_ID_OPTION}}}",
                "#{session_id}",
                f"#{{{RODEX_PRIMARY_PANE_ID_OPTION}}}",
                f"#{{{RODEX_PROTOCOL_PROXY_SOCKET_OPTION}}}",
                f"#{{{RODEX_PROTOCOL_EVENT_SOCKET_OPTION}}}",
                f"#{{{RODEX_CODEX_SESSION_ID_OPTION}}}",
                f"#{{{RODEX_SESSION_ID_OPTION}}}",
                f"#{{{RODEX_REGISTRY_ID_OPTION}}}",
                f"#{{{RODEX_INTERNAL_SESSION_ID_OPTION}}}",
                f"#{{{RODEX_REGISTRATION_STATE_OPTION}}}",
                f"#{{{RODEX_RUNTIME_ID_OPTION}}}",
            )
        )
        observed = self._tmux(
            runtime,
            "display-message",
            "-p",
            "-t",
            target,
            "-F",
            record_format,
            check=False,
        )
        fields = observed.stdout.rstrip("\n").split("\t")
        if observed.returncode != 0 or len(fields) != 12:
            raise RodexRuntimeError("live tmux control capability snapshot is unavailable")
        (
            protocol,
            server_id_text,
            observed_tmux_session_id,
            tmux_primary_pane_id,
            proxy_path_text,
            event_path_text,
            codex_session_id_text,
            rodex_session_id_text,
            registry_id_text,
            internal_session_id_text,
            registration_state_text,
            runtime_id_text,
        ) = fields
        if protocol != RODEX_SHARED_TMUX_PROTOCOL:
            raise RodexRuntimeError("live tmux session protocol does not match Rodex")
        try:
            server_id = parse_tmux_server_id(server_id_text)
        except ValueError as error:
            raise RodexRuntimeError("live tmux server identity is invalid") from error
        if (
            not observed_tmux_session_id.startswith("$")
            or not observed_tmux_session_id[1:].isdigit()
            or (
                tmux_session_id.startswith("$")
                and observed_tmux_session_id != tmux_session_id
            )
        ):
            raise RodexRuntimeError("live tmux session identity changed during discovery")
        proxy_path = Path(proxy_path_text)
        event_path = Path(event_path_text)
        if not proxy_path.is_absolute() or not event_path.is_absolute():
            raise RodexRuntimeError("live tmux control socket paths must be absolute")
        try:
            codex_session_id = parse_codex_session_id(codex_session_id_text)
        except ValueError as error:
            raise RodexRuntimeError(
                "live tmux session advertised an invalid Codex session ID"
            ) from error
        try:
            rodex_session_id = (
                None
                if not rodex_session_id_text
                else RodexSessionId.parse(rodex_session_id_text)
            )
        except ValueError as error:
            raise RodexRuntimeError(
                "live tmux session advertised an invalid Rodex session ID"
            ) from error
        try:
            rodex_registry_id = (
                None if not registry_id_text else parse_rodex_registry_id(registry_id_text)
            )
        except ValueError as error:
            raise RodexRuntimeError(
                "live tmux session advertised an invalid Rodex registry ID"
            ) from error
        try:
            runtime_id = (
                None if not runtime_id_text else parse_rodex_runtime_id(runtime_id_text)
            )
        except ValueError as error:
            raise RodexRuntimeError(
                "live tmux session advertised an invalid runtime ID"
            ) from error
        if runtime.runtime_id is not None and runtime.runtime_id != runtime_id:
            raise RodexRuntimeError(
                "live tmux runtime incarnation changed during discovery"
            )
        registration_state = registration_state_text or None
        capability: TmuxSessionCapability | None = None
        if registration_state == RODEX_REGISTRATION_REGISTERED:
            try:
                capability = parse_tmux_session_capability(
                    runtime.tmux_server_socket_path,
                    server_id,
                    observed_tmux_session_id,
                    tmux_primary_pane_id,
                    runtime_id_text,
                    rodex_session_id_text,
                    registry_id_text,
                    internal_session_id_text,
                    codex_session_id_text,
                )
            except (TypeError, ValueError) as error:
                raise RodexRuntimeError(
                    "registered live tmux capability is malformed"
                ) from error
        if runtime.tmux_capability is not None and runtime.tmux_capability != capability:
            raise RodexRuntimeError("live tmux capability changed during discovery")
        return LiveRodexControl(
            proxy_path,
            event_path,
            codex_session_id,
            rodex_session_id,
            rodex_registry_id,
            registration_state,
            runtime_id,
            capability,
        )

    def rename(self, runtime: LiveTmuxSession, tmux_session_name: str) -> LiveTmuxSession:
        """Rename one exact tmux session and return its updated address."""
        session_name = tmux_session_name.strip()
        if not session_name:
            raise ValueError("tmux_session_name must be non-empty")
        if runtime.runtime_id is None:
            raise RodexRuntimeError("tmux rename requires a runtime incarnation")
        capability = self._resolve_registered_tmux_capability(runtime)
        action = shlex.join(
            ("rename-session", "-t", capability.session_target, session_name)
        )
        self._tmux(
            runtime,
            "if-shell",
            "-t",
            capability.pane_target,
            "-F",
            registered_primary_pane_if_shell_condition(capability),
            action,
            shlex.join(("run-shell", "false")),
            timeout_seconds=RODEX_TMUX_RENAME_TIMEOUT_SECONDS,
        )
        return replace(
            runtime,
            tmux_session_name=session_name,
            tmux_capability=capability,
        )

    def _resolve_bootstrap_tmux_capability(
        self,
        runtime: LiveTmuxSession,
    ) -> TmuxRuntimeCapability:
        """Resolve startup's random name once, then return immutable pane authority."""
        if runtime.runtime_id is None:
            raise RodexRuntimeError("tmux bootstrap requires a runtime incarnation")
        result = self._tmux(
            runtime,
            "display-message",
            "-p",
            "-t",
            _exact_tmux_pane_target(runtime.tmux_session_name),
            "-F",
            "\t".join(
                (
                    f"#{{{RODEX_SHARED_TMUX_PROTOCOL_OPTION}}}",
                    f"#{{{RODEX_SHARED_TMUX_SERVER_ID_OPTION}}}",
                    "#{session_name}",
                    "#{session_id}",
                    f"#{{{RODEX_PRIMARY_PANE_ID_OPTION}}}",
                    f"#{{{RODEX_RUNTIME_ID_OPTION}}}",
                )
            ),
            check=False,
        )
        fields = result.stdout.rstrip("\n").split("\t")
        if (
            result.returncode != 0
            or len(fields) != 6
            or fields[0] != RODEX_SHARED_TMUX_PROTOCOL
            or fields[2] != runtime.tmux_session_name
            or fields[5] != str(runtime.runtime_id)
        ):
            raise RodexRuntimeError("startup tmux capability could not be verified")
        try:
            return TmuxRuntimeCapability(
                runtime.tmux_server_socket_path,
                parse_tmux_server_id(fields[1]),
                fields[3],
                fields[4],
                parse_rodex_runtime_id(fields[5]),
            )
        except ValueError as error:
            raise RodexRuntimeError("startup tmux capability is malformed") from error

    def _read_tmux_server_option(
        self,
        runtime: LiveTmuxSession,
        option_name: str,
        *,
        expected_server_id: str | None = None,
    ) -> str:
        arguments = ("show-options", "-s", "-v", option_name)
        result = (
            self._tmux(runtime, *arguments, check=False)
            if expected_server_id is None
            else self._server_capability_tmux(
                runtime,
                expected_server_id,
                *arguments,
                check=False,
            )
        )
        value = result.stdout.strip()
        return value if result.returncode == 0 else ""

    def _read_tmux_global_hook_command(
        self,
        runtime: LiveTmuxSession,
        hook_name: str,
        *,
        expected_server_id: str | None = None,
    ) -> str:
        arguments = ("show-hooks", "-g", hook_name)
        result = (
            self._tmux(runtime, *arguments, check=False)
            if expected_server_id is None
            else self._server_capability_tmux(
                runtime,
                expected_server_id,
                *arguments,
                check=False,
            )
        )
        if result.returncode != 0:
            return ""
        prefix, separator, command = result.stdout.strip().partition(" ")
        if prefix != hook_name or not separator:
            return ""
        return command.strip()

    def _read_tmux_root_key_command(
        self,
        runtime: LiveTmuxSession,
        key: str,
        *,
        expected_server_id: str | None = None,
    ) -> str:
        arguments = ("list-keys", "-T", "root", key)
        result = (
            self._tmux(runtime, *arguments, check=False)
            if expected_server_id is None
            else self._server_capability_tmux(
                runtime,
                expected_server_id,
                *arguments,
                check=False,
            )
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        try:
            fields = shlex.split(result.stdout.strip())
        except ValueError:
            return ""
        if fields[:4] != ["bind-key", "-T", "root", key] or len(fields) < 5:
            return ""
        return shlex.join(fields[4:])

    def _list_registered_tmux_capabilities(
        self,
        runtime: LiveTmuxSession,
        tmux_server_id: str,
    ) -> tuple[TmuxSessionCapability, ...]:
        """Validate the complete registered roster before returning any authority."""
        record_format = "\t".join(
            (
                "#{session_id}",
                f"#{{{RODEX_PRIMARY_PANE_ID_OPTION}}}",
                f"#{{{RODEX_RUNTIME_ID_OPTION}}}",
                f"#{{{RODEX_REGISTRATION_STATE_OPTION}}}",
                f"#{{{RODEX_SESSION_ID_OPTION}}}",
                f"#{{{RODEX_REGISTRY_ID_OPTION}}}",
                f"#{{{RODEX_INTERNAL_SESSION_ID_OPTION}}}",
                f"#{{{RODEX_CODEX_SESSION_ID_OPTION}}}",
            )
        )
        listed = self._tmux(
            runtime,
            "list-sessions",
            "-F",
            record_format,
            check=False,
        )
        if listed.returncode != 0:
            raise RodexRuntimeError("shared tmux roster is unavailable")
        capabilities: list[TmuxSessionCapability] = []
        for line in listed.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) != 8:
                raise RodexRuntimeError("shared tmux roster is malformed")
            if fields[3] != RODEX_REGISTRATION_REGISTERED:
                continue
            try:
                capabilities.append(
                    parse_tmux_session_capability(
                        runtime.tmux_server_socket_path,
                        tmux_server_id,
                        fields[0],
                        fields[1],
                        fields[2],
                        fields[4],
                        fields[5],
                        fields[6],
                        fields[7],
                    )
                )
            except (TypeError, ValueError) as error:
                raise RodexRuntimeError(
                    "registered shared tmux capability is malformed"
                ) from error
        for projection in (
            tuple(item.tmux_session_id for item in capabilities),
            tuple(item.tmux_primary_pane_id for item in capabilities),
            tuple(item.runtime_id for item in capabilities),
            tuple((item.registry_id, item.rodex_session_id) for item in capabilities),
            tuple((item.registry_id, item.internal_session_id) for item in capabilities),
            tuple((item.registry_id, item.codex_session_id) for item in capabilities),
        ):
            if len(projection) != len(set(projection)):
                raise RodexRuntimeError("registered shared tmux roster is ambiguous")
        return tuple(capabilities)

    def _resolve_registered_tmux_capability(
        self,
        runtime: LiveTmuxSession,
    ) -> TmuxSessionCapability:
        """Resolve one expected incarnation to its complete registered authority."""
        if runtime.runtime_id is None:
            raise RodexRuntimeError("registered tmux operation requires a runtime ID")
        expected_capability = runtime.tmux_capability
        protocol = self._read_tmux_server_option(
            runtime,
            RODEX_SHARED_TMUX_PROTOCOL_OPTION,
        )
        server_id_text = self._read_tmux_server_option(
            runtime,
            RODEX_SHARED_TMUX_SERVER_ID_OPTION,
        )
        if protocol != RODEX_SHARED_TMUX_PROTOCOL:
            raise RodexRuntimeError("registered tmux server protocol changed")
        try:
            server_id = parse_tmux_server_id(server_id_text)
        except ValueError as error:
            raise RodexRuntimeError("registered tmux server identity is invalid") from error
        if (
            expected_capability is not None
            and expected_capability.tmux_server_id != server_id
        ):
            raise RodexRuntimeError("registered tmux server incarnation changed")
        matches = tuple(
            capability
            for capability in self._list_registered_tmux_capabilities(
                runtime,
                server_id,
            )
            if (
                capability == expected_capability
                if expected_capability is not None
                else capability.runtime_id == runtime.runtime_id
            )
        )
        if len(matches) != 1:
            detail = "not found" if not matches else "ambiguous"
            raise RodexRuntimeError(
                f"registered runtime {runtime.runtime_id} was {detail} on shared tmux"
            )
        return matches[0]

    def _capability_tmux(
        self,
        runtime: LiveTmuxSession,
        capability: TmuxSessionCapability,
        *arguments: str,
    ) -> TmuxCommandResult:
        """Atomically fence every session-local read or mutation."""
        if not arguments:
            raise ValueError("tmux command arguments must be non-empty")
        return self._tmux(
            runtime,
            "if-shell",
            "-t",
            capability.pane_target,
            "-F",
            registered_primary_pane_if_shell_condition(capability),
            _tmux_command_queue(arguments),
            shlex.join(("run-shell", "false")),
        )

    def _server_capability_tmux(
        self,
        runtime: LiveTmuxSession,
        tmux_server_id: str,
        *arguments: str,
        check: bool = True,
    ) -> TmuxCommandResult:
        """Atomically fence one server-global action to its expected incarnation."""
        if not arguments:
            raise ValueError("tmux command arguments must be non-empty")
        return self._tmux(
            runtime,
            "if-shell",
            "-F",
            server_identity_if_shell_condition(tmux_server_id),
            _tmux_command_queue(arguments),
            shlex.join(("run-shell", "false")),
            check=check,
        )

    def initialise_session_ui(self, runtime: LiveTmuxSession) -> None:
        """Install a fresh Rodex UI after creating one new tmux runtime."""
        capability = self._resolve_registered_tmux_capability(runtime)
        self._install_shared_tmux_coordination(runtime, capability)
        self._configure_static_status(
            runtime,
            capability,
            publish_base_status=True,
        )
        self._install_input_guards(runtime, capability)

    def reconcile_session_ui(self, runtime: LiveTmuxSession) -> None:
        """Refresh static UI configuration without replacing a transient claim."""
        capability = self._resolve_registered_tmux_capability(runtime)
        self._install_shared_tmux_coordination(runtime, capability)
        self._configure_static_status(
            runtime,
            capability,
            publish_base_status=False,
        )
        self._install_input_guards(runtime, capability)

    def refresh_shared_tmux_coordination(self, runtime: LiveTmuxSession) -> None:
        """Reconcile the global coordinator for one verified registered runtime."""
        capability = self._resolve_registered_tmux_capability(runtime)
        self._install_shared_tmux_coordination(runtime, capability)

    def _install_shared_tmux_coordination(
        self,
        runtime: LiveTmuxSession,
        capability: TmuxSessionCapability,
    ) -> None:
        """Install only Rodex-owned server-global wake-hook slots."""
        command = sharing_coordinator_hook_command(
            self._python_executable,
            self._tmux_binary,
            runtime.tmux_server_socket_path,
            capability.tmux_server_id,
        )
        self._server_capability_tmux(
            runtime,
            capability.tmux_server_id,
            "set-option",
            "-so",
            RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION,
            command,
            check=False,
        )
        installed_command = self._read_tmux_server_option(
            runtime,
            RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION,
            expected_server_id=capability.tmux_server_id,
        )
        if installed_command != command:
            raise RodexRuntimeError(
                "shared tmux coordinator belongs to a different Rodex installation"
            )

        snapshot_action = shlex.join(
            (
                "set-option",
                "-F",
                "-t",
                capability.pane_target,
                RODEX_SHARING_ATTACHED_COUNT_OPTION,
                "#{session_attached}",
            )
        )
        self._capability_tmux(
            runtime,
            capability,
            *shlex.split(snapshot_action),
        )
        for event in ("attached", "detached"):
            hook_name = f"client-{event}[{RODEX_SHARED_TMUX_HOOK_INDEX}]"
            self._server_capability_tmux(
                runtime,
                capability.tmux_server_id,
                "set-option",
                "-go",
                hook_name,
                command,
                check=False,
            )
            verified_hook = self._read_tmux_global_hook_command(
                runtime,
                hook_name,
                expected_server_id=capability.tmux_server_id,
            )
            if not _shell_commands_are_equivalent(verified_hook, command):
                raise RodexRuntimeError(
                    f"shared tmux hook slot {hook_name} was not installed exactly"
                )

    def _configure_static_status(
        self,
        runtime: LiveTmuxSession,
        capability: TmuxSessionCapability,
        *,
        publish_base_status: bool,
    ) -> None:
        status = TmuxStatusPipeline(
            lambda *args: self._capability_tmux(runtime, capability, *args),
            capability.pane_target,
        )
        status.configure_base_status(
            reset_transient_claims=publish_base_status,
        )

    def _install_input_guards(
        self,
        runtime: LiveTmuxSession,
        capability: TmuxSessionCapability,
    ) -> None:
        self._install_shared_ctrl_c_guard(runtime, capability)

    def _start_tmux_session(
        self,
        runtime: LiveTmuxSession,
        workspace: Path,
        host_command: Sequence[str],
    ) -> None:
        """Stage one inert pane, install caller state, then start the real host."""
        if runtime.runtime_id is None:
            raise RodexRuntimeError("tmux creation requires a runtime incarnation")
        if (
            not host_command
            or not isinstance(host_command[0], str)
            or not host_command[0]
            or any(not isinstance(argument, str) for argument in host_command)
        ):
            raise ValueError("tmux host command must start with non-empty executable text")
        session_environment = self._user_process_environment.copy()
        session_environment["PWD"] = str(workspace)
        try:
            environment_entries = validated_user_environment_entries(session_environment)
        except ValueError as error:
            raise RodexRuntimeError("caller process environment is invalid") from error
        caller_environment_names = frozenset(name for name, _value in environment_entries)
        candidate_server_id = secrets.token_hex(16)
        new_session_arguments = [
            "new-session",
            "-d",
            "-E",
            "-s",
            runtime.tmux_session_name,
            "-c",
            str(workspace),
            "/usr/bin/sleep",
            "30",
        ]
        new_session_arguments.extend(
            (
                ";",
                "set-option",
                "-p",
                "-t",
                _exact_tmux_pane_target(runtime.tmux_session_name),
                "remain-on-exit",
                "on",
                ";",
                "set-option",
                "-t",
                _exact_tmux_pane_target(runtime.tmux_session_name),
                RODEX_RUNTIME_ID_OPTION,
                str(runtime.runtime_id),
                ";",
                "set-option",
                "-F",
                "-t",
                _exact_tmux_pane_target(runtime.tmux_session_name),
                RODEX_PRIMARY_PANE_ID_OPTION,
                "#{pane_id}",
            )
        )
        creation_action = _tmux_command_queue(
            (
                "set-option",
                "-g",
                "history-limit",
                str(RODEX_TMUX_HISTORY_LIMIT_LINES),
                ";",
                *new_session_arguments,
            )
        )
        server_matches_current_protocol = combine_tmux_if_shell_conditions(
            (
                f"#{{==:#{{{RODEX_SHARED_TMUX_PROTOCOL_OPTION}}},"
                f"{RODEX_SHARED_TMUX_PROTOCOL}}}"
            ),
            (f"#{{m/r:^{'[0-9a-f]' * 32}$,#{{{RODEX_SHARED_TMUX_SERVER_ID_OPTION}}}}}"),
        )
        server_is_unclaimed = combine_tmux_if_shell_conditions(
            f"#{{==:#{{{RODEX_SHARED_TMUX_PROTOCOL_OPTION}}},}}",
            f"#{{==:#{{{RODEX_SHARED_TMUX_SERVER_ID_OPTION}}},}}",
            "#{==:#{session_id},}",
        )
        claim_action = _tmux_command_queue(
            (
                "set-option",
                "-s",
                RODEX_SHARED_TMUX_PROTOCOL_OPTION,
                RODEX_SHARED_TMUX_PROTOCOL,
                ";",
                "set-option",
                "-s",
                RODEX_SHARED_TMUX_SERVER_ID_OPTION,
                candidate_server_id,
            )
        )
        self._tmux(
            runtime,
            "start-server",
            ";",
            "if-shell",
            "-F",
            server_is_unclaimed,
            claim_action,
            shlex.join(("run-shell", "true")),
            ";",
            "if-shell",
            "-F",
            server_matches_current_protocol,
            creation_action,
            shlex.join(("run-shell", "false")),
            environment=session_environment,
        )
        capability = self._resolve_bootstrap_tmux_capability(runtime)
        global_environment_names = self._read_tmux_global_environment_names(
            runtime,
            capability.tmux_server_id,
        )
        stale_global_environment_names = tuple(
            sorted(
                global_environment_names
                - caller_environment_names
                - TMUX_OWNED_CHILD_ENVIRONMENT_VARIABLES
            )
        )
        environment_action: list[str] = []
        for name, value in environment_entries:
            environment_action.extend(
                (
                    "set-environment",
                    "-t",
                    capability.session_target,
                    "--",
                    name,
                    value,
                    ";",
                )
            )
        for name in stale_global_environment_names:
            environment_action.extend(
                (
                    "set-environment",
                    "-r",
                    "-t",
                    capability.session_target,
                    "--",
                    name,
                    ";",
                )
            )
        environment_action.extend(
            (
                "set-option",
                "-t",
                capability.pane_target,
                "update-environment",
                "",
            )
        )
        install_action = _tmux_source_command_queue(environment_action)
        source = _tmux_source_command_queue(
            (
                "if-shell",
                "-t",
                capability.pane_target,
                "-F",
                primary_pane_capability_if_shell_condition(capability),
                install_action,
                shlex.join(("run-shell", "false")),
            )
        )
        self._tmux(
            runtime,
            "source-file",
            "-",
            environment=session_environment,
            input_text=f"{source}\n",
        )
        respawn_action = _tmux_command_queue(
            (
                "respawn-pane",
                "-k",
                "-t",
                capability.pane_target,
                *exact_environment_exec_command(
                    self._python_executable,
                    tuple(caller_environment_names),
                    host_command,
                ),
                ";",
                "set-option",
                "-p",
                "-t",
                capability.pane_target,
                "remain-on-exit",
                "off",
            )
        )
        self._tmux(
            runtime,
            "if-shell",
            "-t",
            capability.pane_target,
            "-F",
            primary_pane_capability_if_shell_condition(capability),
            respawn_action,
            shlex.join(("run-shell", "false")),
            environment=session_environment,
        )

    def _read_tmux_global_environment_names(
        self,
        runtime: LiveTmuxSession,
        expected_server_id: str,
    ) -> frozenset[str]:
        arguments = ("show-environment", "-g", "-s")
        result = self._server_capability_tmux(
            runtime,
            expected_server_id,
            *arguments,
            check=False,
        )
        if result.returncode != 0:
            raise RodexRuntimeError(
                "shared tmux global environment could not be read exactly"
            )
        try:
            return _parse_tmux_shell_environment_names(result.stdout)
        except ValueError as error:
            raise RodexRuntimeError(
                "shared tmux global environment is malformed"
            ) from error

    def attach(self, runtime: LiveTmuxSession) -> None:
        """Attach the calling terminal to the live Rodex tmux session."""
        notice: str | None = None
        if self._attach_notice is not None:
            with suppress(Exception):
                notice = self._attach_notice()
        if notice and isinstance(runtime, LiveRodexRuntime):
            with suppress(Exception):
                self._publish_tui_notice(runtime.protocol_proxy_socket_path, notice)
        environment = self._user_process_environment.copy()
        environment.pop("TMUX", None)
        capability = self._resolve_registered_tmux_capability(runtime)
        attach_action = shlex.join(
            ("attach-session", "-E", "-t", capability.session_target)
        )
        self._tmux(
            runtime,
            "-T",
            RODEX_TMUX_REQUIRED_CLIENT_FEATURES,
            "if-shell",
            "-t",
            capability.pane_target,
            "-F",
            registered_primary_pane_if_shell_condition(capability),
            attach_action,
            shlex.join(("run-shell", "false")),
            interactive=True,
            environment=environment,
        )

    def _stable_tmux_session_target(self, runtime: LiveTmuxSession) -> str:
        """Resolve a managed runtime to tmux's immutable server-local session ID."""
        if runtime.runtime_id is None:
            return _exact_tmux_session_target(runtime.tmux_session_name)
        result = self._tmux(
            runtime,
            "list-sessions",
            "-F",
            f"#{{session_id}}\t#{{{RODEX_RUNTIME_ID_OPTION}}}",
            check=False,
        )
        if result.returncode != 0:
            raise RodexRuntimeError("Rodex runtime ended before stable tmux resolution")
        expected_runtime_id = str(runtime.runtime_id)
        matches: list[str] = []
        for line in result.stdout.splitlines():
            tmux_session_id, separator, runtime_id = line.partition("\t")
            if (
                separator
                and runtime_id == expected_runtime_id
                and tmux_session_id.startswith("$")
                and tmux_session_id[1:].isdigit()
            ):
                matches.append(tmux_session_id)
        if len(matches) != 1:
            detail = "not found" if not matches else "advertised by multiple tmux sessions"
            raise RodexRuntimeError(f"Rodex runtime {expected_runtime_id} was {detail}")
        return matches[0]

    def session_exists(self, runtime: LiveTmuxSession) -> bool:
        """Return whether the exact recorded tmux session is still running."""
        if runtime.runtime_id is not None:
            try:
                self._stable_tmux_session_target(runtime)
            except RodexRuntimeError:
                return False
            return True
        return self._bootstrap_session_exists(runtime)

    def _bootstrap_session_exists(self, runtime: LiveTmuxSession) -> bool:
        """Check the unique random startup name before identity publication."""
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
        """Read retained lines only from the capability's immutable primary pane."""
        capability = self._resolve_registered_tmux_capability(runtime)
        result = self._capability_tmux(
            runtime,
            capability,
            "capture-pane",
            "-p",
            "-S",
            "-",
            "-t",
            capability.pane_target,
        )
        return tuple(result.stdout.rstrip("\n").splitlines())

    def capture_scrollback_snapshot(
        self, runtime: LiveTmuxSession
    ) -> TmuxScrollbackSnapshot:
        """Capture the primary pane text and boundary through one full capability."""
        capability = self._resolve_registered_tmux_capability(runtime)
        target = capability.pane_target
        result = self._capability_tmux(
            runtime,
            capability,
            "display-message",
            "-p",
            "-t",
            target,
            "-F",
            "#{history_size}",
            ";",
            "capture-pane",
            "-p",
            "-S",
            "-",
            "-t",
            target,
        )
        history_size_text, captured = _split_tmux_capture(result.stdout)
        if not history_size_text.isdigit():
            raise RodexRuntimeError("tmux returned an invalid history size")
        lines = _captured_tmux_lines(captured)
        history_line_count = int(history_size_text)
        if len(lines) < history_line_count:
            lines = (*lines, *("" for _ in range(history_line_count - len(lines))))
        return TmuxScrollbackSnapshot(lines, history_line_count)

    def capture_scrollback_state(self, runtime: LiveTmuxSession) -> TmuxScrollbackState:
        """Capture bounded state from the immutable primary pane and capability."""
        capability = self._resolve_registered_tmux_capability(runtime)
        target = capability.pane_target
        identity_format = "\t".join(
            (
                "#{history_size}",
                "#{session_id}",
                "#{window_id}",
                "#{pane_id}",
                "#{pane_pid}",
                f"#{{{RODEX_PROTOCOL_PROXY_SOCKET_OPTION}}}",
                f"#{{{RODEX_PROTOCOL_EVENT_SOCKET_OPTION}}}",
                f"#{{{RODEX_CODEX_SESSION_ID_OPTION}}}",
                f"#{{{RODEX_SESSION_ID_OPTION}}}",
                f"#{{{RODEX_REGISTRY_ID_OPTION}}}",
                f"#{{{RODEX_REGISTRATION_STATE_OPTION}}}",
                f"#{{{RODEX_RUNTIME_ID_OPTION}}}",
            )
        )
        result = self._capability_tmux(
            runtime,
            capability,
            "display-message",
            "-p",
            "-t",
            target,
            "-F",
            identity_format,
            ";",
            "capture-pane",
            "-p",
            "-S",
            f"-{RODEX_TMUX_SCROLLBACK_STATE_LINES}",
            "-t",
            target,
        )
        metadata, captured = _split_tmux_capture(result.stdout)
        history_size_text, separator, runtime_identity = metadata.partition("\t")
        if not separator or not history_size_text.isdigit() or not runtime_identity:
            raise RodexRuntimeError("tmux returned invalid scrollback state metadata")
        history_line_count = int(history_size_text)
        history_tail_count = min(
            history_line_count,
            RODEX_TMUX_SCROLLBACK_STATE_LINES,
        )
        lines = _captured_tmux_lines(captured)
        if len(lines) < history_tail_count:
            lines = (*lines, *("" for _ in range(history_tail_count - len(lines))))
        return TmuxScrollbackState(
            history_line_count=history_line_count,
            history_tail_lines=lines[:history_tail_count],
            visible_lines=lines[history_tail_count:],
            runtime_identity=runtime_identity,
        )

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
        capability = self._resolve_registered_tmux_capability(runtime)
        target = capability.pane_target
        if mode == "inherit":
            self._capability_tmux(
                runtime,
                capability,
                "set-option",
                "-u",
                "-t",
                target,
                "mouse",
            )
        elif mode == "toggle":
            self._capability_tmux(
                runtime,
                capability,
                "set-option",
                "-t",
                target,
                "mouse",
            )
        elif mode != "status":
            self._capability_tmux(
                runtime,
                capability,
                "set-option",
                "-t",
                target,
                "mouse",
                mode,
            )
        readback = self._capability_tmux(
            runtime,
            capability,
            "show-options",
            "-A",
            "-v",
            "-t",
            target,
            "mouse",
        )
        value = readback.stdout.strip()
        if value not in {"on", "off"}:
            raise RodexRuntimeError(f"tmux returned an invalid mouse mode: {value}")
        return value

    def stop(self, runtime: LiveTmuxSession, *, check: bool = True) -> None:
        """Stop exactly one tmux session, allowing its supervisor to clean up."""
        if runtime.runtime_id is None:
            raise RodexRuntimeError("tmux stop requires a runtime incarnation")
        if runtime.tmux_capability is not None:
            capability = self._resolve_registered_tmux_capability(runtime)
            tmux_session_id = capability.session_target
            pane_target = capability.pane_target
            condition = registered_primary_pane_if_shell_condition(capability)
        else:
            bootstrap_capability = self._resolve_bootstrap_tmux_capability(runtime)
            tmux_session_id = bootstrap_capability.session_target
            pane_target = bootstrap_capability.pane_target
            condition = primary_pane_capability_if_shell_condition(bootstrap_capability)
        self._tmux(
            runtime,
            "if-shell",
            "-t",
            pane_target,
            "-F",
            condition,
            shlex.join(("kill-session", "-t", tmux_session_id)),
            shlex.join(("run-shell", "false")),
            check=check,
        )

    def _stop_startup_runtime(
        self,
        runtime: LiveTmuxSession,
        *,
        failure: BaseException | None = None,
    ) -> None:
        """Clean up only the runtime incarnation published during tmux creation."""
        try:
            self.stop(runtime, check=False)
        except BaseException as cleanup_failure:
            if failure is not None:
                failure.add_note(f"tmux startup cleanup also failed: {cleanup_failure}")
            elif not isinstance(cleanup_failure, RodexRuntimeError):
                raise

    def _wait_for_single_codex_session_id(
        self,
        runtime: LiveRodexRuntime,
        *,
        requested_codex_session_id: CodexSessionId | None = None,
    ) -> CodexSessionId:
        deadline = self._monotonic() + self._startup_timeout_seconds
        last_error: BaseException | None = None
        while self._monotonic() < deadline:
            if not self._bootstrap_session_exists(runtime):
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
            initialized = _receive_response(websocket, 0)
            CODEX_APP_SERVER.require_minimum_version(initialized)
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
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> TmuxCommandResult:
        deadline = (
            RODEX_TMUX_COMMAND_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        executor = SyncTmuxExecutor(
            self._tmux_binary,
            runtime.tmux_server_socket_path,
            runner=self._run,
            timeout_seconds=deadline,
        )
        result = executor.run(
            arguments,
            mode="interactive" if interactive else "captured",
            environment=environment,
            input_text=input_text,
        )
        if result.timed_out:
            raise RodexRuntimeError(
                f"tmux command timed out after {deadline:g}s: {arguments[0]}"
            )
        if result.unavailable:
            raise RodexRuntimeError(result.stderr or "tmux command is unavailable")
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "tmux command failed").strip()
            raise RodexRuntimeError(detail)
        return result

    def _install_shared_ctrl_c_guard(
        self,
        runtime: LiveTmuxSession,
        capability: TmuxSessionCapability,
    ) -> None:
        command = _shared_ctrl_c_binding_command(
            self._python_executable,
            self._tmux_binary,
            runtime,
            capability.tmux_server_id,
        )
        existing_command = self._read_tmux_root_key_command(
            runtime,
            "C-c",
            expected_server_id=capability.tmux_server_id,
        )
        owned_command = self._read_tmux_server_option(
            runtime,
            RODEX_SHARED_TMUX_CTRL_C_COMMAND_OPTION,
            expected_server_id=capability.tmux_server_id,
        )
        if owned_command and not _shell_commands_are_equivalent(owned_command, command):
            raise RodexRuntimeError(
                "shared Ctrl-C command is owned by a different Rodex installation"
            )
        if existing_command and not _shell_commands_are_equivalent(
            existing_command,
            command,
        ):
            if owned_command:
                raise RodexRuntimeError(
                    "shared Ctrl-C binding changed after Rodex claimed it"
                )
            raise RodexRuntimeError(
                "cannot install the shared Ctrl-C safety guard: root C-c already "
                "has a non-Rodex binding"
            )
        self._server_capability_tmux(
            runtime,
            capability.tmux_server_id,
            "set-option",
            "-so",
            RODEX_SHARED_TMUX_CTRL_C_COMMAND_OPTION,
            command,
            check=False,
        )
        installed_command = self._read_tmux_server_option(
            runtime,
            RODEX_SHARED_TMUX_CTRL_C_COMMAND_OPTION,
            expected_server_id=capability.tmux_server_id,
        )
        if installed_command != command:
            raise RodexRuntimeError(
                "shared Ctrl-C binding belongs to a different Rodex installation"
            )
        if not existing_command:
            claimed_binding = self._read_tmux_root_key_command(
                runtime,
                "C-c",
                expected_server_id=capability.tmux_server_id,
            )
            if claimed_binding and not _shell_commands_are_equivalent(
                claimed_binding,
                command,
            ):
                raise RodexRuntimeError(
                    "shared Ctrl-C binding changed while Rodex claimed it"
                )
            self._server_capability_tmux(
                runtime,
                capability.tmux_server_id,
                "bind-key",
                "-n",
                "C-c",
                command,
            )
        verified_command = self._read_tmux_root_key_command(
            runtime,
            "C-c",
            expected_server_id=capability.tmux_server_id,
        )
        if not _shell_commands_are_equivalent(verified_command, command):
            raise RodexRuntimeError("shared Ctrl-C binding was not installed exactly")


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
    return default_runtime_root_path() / RODEX_SHARED_TMUX_SOCKET_NAME


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
    user_environment = user_process_environment(os.environ)
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
    agent_observer_controller: AgentObserverCoordinator | None = None
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
                env=user_environment,
            )
            _wait_for_app_server_socket(app_server, app_server_socket_path)
            app_server_socket_path.chmod(0o600)
            tmux_pane_target = os.environ.get("TMUX_PANE", "")
            tmux_runtime_capability = _resolve_session_host_tmux_capability(
                tmux_binary,
                tmux_server_socket_path,
                tmux_pane_target,
                config.runtime_id,
            )
            if analytics_config is not None and tmux_pane_target:
                agent_observer_controller = AgentObserverCoordinator(
                    tmux_binary,
                    tmux_runtime_capability,
                    tmux_pane_target,
                    protocol_event_socket_path,
                )
            tool_call_status = TmuxToolCallStatus(
                tmux_binary,
                tmux_runtime_capability,
                tmux_pane_target,
            )
            tool_call_status.update(0)
            context_status = TmuxContextStatus(
                tmux_binary,
                tmux_runtime_capability,
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

            lifecycle_participants = [live_context_observer, live_event_tap]
            if agent_observer_controller is not None:
                lifecycle_participants.append(agent_observer_controller)
            primary_connection_lifecycle = PrimaryConnectionLifecycleCoordinator(
                lifecycle_participants
            )

            protocol_proxy = CodexProtocolProxy(
                protocol_proxy_socket_path,
                app_server_socket_path,
                ToolCallCounter(tool_call_status.update),
                publish_primary_server_message,
                primary_connection_lifecycle,
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
                # Rodex enforces the minimum App Server version itself. An interactive
                # updater here would block thread registration before attach.
                "--config",
                "check_for_update_on_startup=false",
                "--no-alt-screen",
                "--remote",
                f"unix://{protocol_proxy_socket_path}",
                *codex_arguments,
            ]
            tui_options: dict[str, object] = {"env": user_environment}
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
                            if agent_observer_controller is not None:
                                agent_observer_controller.close()
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


def _resolve_session_host_tmux_capability(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    tmux_pane_target: str,
    expected_runtime_id: RodexRuntimeId,
) -> TmuxRuntimeCapability:
    """Bind a session host to the server/session/runtime incarnation that spawned it."""
    if not tmux_pane_target:
        raise RodexRuntimeError("session host has no exact tmux pane identity")
    result = SyncTmuxExecutor(
        tmux_binary,
        tmux_server_socket_path,
        runner=subprocess.run,
    ).run(
        (
            "display-message",
            "-p",
            "-t",
            tmux_pane_target,
            "-F",
            (
                f"#{{{RODEX_SHARED_TMUX_PROTOCOL_OPTION}}}\t"
                f"#{{{RODEX_SHARED_TMUX_SERVER_ID_OPTION}}}\t"
                "#{session_id}\t#{pane_id}\t"
                f"#{{{RODEX_PRIMARY_PANE_ID_OPTION}}}\t"
                f"#{{{RODEX_RUNTIME_ID_OPTION}}}"
            ),
        )
    )
    fields = result.stdout.rstrip("\n").split("\t")
    if (
        result.returncode != 0
        or len(fields) != 6
        or fields[0] != RODEX_SHARED_TMUX_PROTOCOL
        or fields[3] != tmux_pane_target
        or fields[4] != tmux_pane_target
        or fields[5] != str(expected_runtime_id)
    ):
        raise RodexRuntimeError("session host tmux incarnation could not be verified")
    try:
        server_id = parse_tmux_server_id(fields[1])
        runtime_id = parse_rodex_runtime_id(fields[5])
        return TmuxRuntimeCapability(
            tmux_server_socket_path,
            server_id,
            fields[2],
            fields[3],
            runtime_id,
        )
    except ValueError as error:
        raise RodexRuntimeError("session host tmux capability is malformed") from error


def _runtime_registration_is_confirmed(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    tmux_pane_target: str,
) -> bool:
    if not tmux_pane_target:
        return False
    result = SyncTmuxExecutor(
        tmux_binary,
        tmux_server_socket_path,
        runner=subprocess.run,
    ).run(
        (
            "show-options",
            "-v",
            "-t",
            tmux_pane_target,
            RODEX_REGISTRATION_STATE_OPTION,
        )
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
    result = SyncTmuxExecutor(
        tmux_binary,
        tmux_server_socket_path,
        runner=subprocess.run,
    ).run(("show-options", "-t", tmux_pane_target))
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
    if options.get(RODEX_REGISTRATION_STATE_OPTION) != RODEX_REGISTRATION_REGISTERED:
        return None
    expected = {
        RODEX_SESSION_ID_OPTION: str(config.rodex_session_id),
        RODEX_REGISTRY_ID_OPTION: str(config.rodex_registry_id),
        RODEX_RUNTIME_ID_OPTION: str(config.runtime_id),
        RODEX_PROTOCOL_EVENT_SOCKET_OPTION: str(config.protocol_event_socket_path),
    }
    for option_name, expected_value in expected.items():
        if options.get(option_name) != expected_value:
            raise RodexRuntimeError(
                f"registered analytics identity disagrees at {option_name}"
            )
    try:
        rodex_sessions_id = int(options[RODEX_INTERNAL_SESSION_ID_OPTION])
        codex_session_id = parse_codex_session_id(options[RODEX_CODEX_SESSION_ID_OPTION])
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
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
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
