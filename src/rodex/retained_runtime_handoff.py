"""Run this file with the selected OLD retained interpreter, always with ``-I``.

Only the adapter itself comes from the current installation. Imports, control
handshakes, process receipts and daemon requests use the owning old installation.
The caller holds the durable session transition lock across stop and new resume,
then releases it before interactive attachment. This child must not reacquire it.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from rodex.control import CodexControlClient, LiveRodexControl
from rodex.daemon_client import RODEX_DAEMON_PROTOCOL, RodexDaemonClient
from rodex.implementation_identity import RODEX_IMPLEMENTATION_SHA256
from rodex.installation import retained_installation_interpreter
from rodex.live_runtime import require_durable_runtime_instance, require_live_runtime_identity
from rodex.process_receipts import PROCESS_RECEIPT_PROTOCOL, RuntimeProcessReceipts, _read_process_state
from rodex.runtime import LiveTmuxSession, RodexRuntimeLauncher
from rodex.tmux_session_capability import (
    RODEX_SHARED_TMUX_PROTOCOL,
    TmuxSessionCapability,
    parse_tmux_session_capability,
    runtime_destruction_if_shell_condition,
)
from rodex.tmux_sharing_coordinator import RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION
from rodex_registry import lookup_rodex_registry_id, lookup_rodex_runtime_registration, lookup_rodex_session_names

UPGRADE_PROTOCOL = "rodex-retained-runtime-upgrade-v1"
STOP_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class _SelectedRetainedRuntime:
    interpreter: Path
    capability: TmuxSessionCapability
    runtime: LiveTmuxSession
    database_path: Path
    launcher: RodexRuntimeLauncher
    coordinator_command: str

    @classmethod
    def from_request(cls, request: dict) -> _SelectedRetainedRuntime:
        if (
            request["protocol"] != UPGRADE_PROTOCOL
            or request["implementation"] != RODEX_IMPLEMENTATION_SHA256
            or RODEX_SHARED_TMUX_PROTOCOL != "rodex-isolated-tmux-v5"
            or RODEX_DAEMON_PROTOCOL not in {"rodex-daemon-v3", "rodex-daemon-v4"}
            or PROCESS_RECEIPT_PROTOCOL != "rodex-process-receipt-v3"
        ):
            raise RuntimeError("selected runtime is outside the supported retained-installation contract")
        interpreter = retained_installation_interpreter()
        if interpreter.parents[2].name != request["implementation"]:
            raise RuntimeError("selected retained installation changed")
        identity = request["capability"]
        capability = parse_tmux_session_capability(
            Path(identity["tmux_server_socket_path"]),
            identity["tmux_server_id"],
            identity["tmux_session_id"],
            identity["tmux_primary_pane_id"],
            identity["runtime_id"],
            identity["rodex_session_id"],
            identity["registry_id"],
            str(identity["internal_session_id"]),
            identity["codex_session_id"],
        )
        runtime = LiveTmuxSession(
            capability.tmux_server_socket_path,
            request["tmux_session_name"],
            runtime_id=capability.runtime_id,
            tmux_capability=capability,
        )
        return cls(
            interpreter,
            capability,
            runtime,
            Path(request["database_path"]),
            RodexRuntimeLauncher(request["codex_binary"], request["tmux_binary"]),
            request["coordinator_command"],
        )

    def revalidate(self, *, exclusive: bool = False) -> LiveRodexControl:
        capability, runtime, database_path, launcher = (self.capability, self.runtime, self.database_path, self.launcher)
        if lookup_rodex_registry_id(database_path) != capability.registry_id:
            raise RuntimeError("selected session catalog changed")
        registration = lookup_rodex_runtime_registration(capability.internal_session_id, database_path)
        if (
            registration is None
            or registration.runtime_id != capability.runtime_id
            or registration.codex_session_id != capability.codex_session_id
            or registration.tmux_session.tmux_server_socket_path != str(capability.tmux_server_socket_path)
            or registration.tmux_session.tmux_session_name != runtime.tmux_session_name
        ):
            raise RuntimeError("selected session changed")
        control = launcher.discover_runtime_control(runtime)
        require_live_runtime_identity(
            control,
            expected_rodex_session_id=capability.rodex_session_id,
            expected_registry_id=capability.registry_id,
            expected_codex_session_id=capability.codex_session_id,
        )
        require_durable_runtime_instance(capability.internal_session_id, database_path, control)
        if control.tmux_capability != capability:
            raise RuntimeError("selected runtime capability changed")
        command = launcher._read_tmux_server_option(
            runtime, RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION, expected_server_id=capability.tmux_server_id
        )
        if command != self.coordinator_command:
            raise RuntimeError("selected runtime coordinator changed")
        if exclusive:
            destruction_condition = runtime_destruction_if_shell_condition(capability)
            ownership = launcher._tmux(
                runtime,
                "if-shell",
                "-t",
                capability.pane_target,
                "-F",
                destruction_condition,
                shlex.join(("display-message", "-p", "-t", capability.pane_target, "1")),
                shlex.join(("display-message", "-p", "-t", capability.pane_target, "0")),
            )
            if ownership.stdout.strip() != "1":
                raise RuntimeError("cannot upgrade a runtime with foreign tmux sessions or panes")
        return control


def attach_selected_runtime(request: dict) -> str:
    """Revalidate and attach using the owner, without restarting any runtime."""
    selected = _SelectedRetainedRuntime.from_request(request)
    selected.revalidate()
    outcome = selected.launcher.attach(selected.runtime)
    if str(outcome) not in ("detach", "exited"):
        raise RuntimeError("retained attachment returned an invalid lifecycle outcome")
    return str(outcome)


def _busy_upgrade_message(selected: _SelectedRetainedRuntime, status: str) -> str:
    names = lookup_rodex_session_names(selected.capability.internal_session_id, selected.database_path)
    name = names.display_name if names is not None else selected.runtime.tmux_session_name
    resume = shlex.join(("rodex", name))
    reconnect = shlex.join(("rodex", name, "--force-old"))
    activity = "still working" if status == "active" else "not idle"
    return (
        f"Session {name!r} is {activity} on an older Rodex version.\n"
        f"Upgrading would interrupt its work. Wait until it is idle, then run: {resume}\n"
        f"Reconnect without upgrading: {reconnect}"
    )


def stop_selected_idle_runtime(request: dict) -> dict:
    """Authenticate the old implementation and retire one exact idle reservation."""
    selected = _SelectedRetainedRuntime.from_request(request)
    interpreter, capability, runtime, launcher = (
        selected.interpreter,
        selected.capability,
        selected.runtime,
        selected.launcher,
    )
    control = selected.revalidate(exclusive=True)
    client = CodexControlClient()
    state = client.inspect_live(control)
    if state.status != "idle":
        raise RuntimeError(_busy_upgrade_message(selected, state.status))
    workspace = Path(state.cwd)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise RuntimeError("selected session working directory is unavailable; runtime was left running")
    if not launcher.codex_session_is_persisted(control.codex_session_id):
        raise RuntimeError("selected Codex thread has no saved history; runtime was left running")

    receipts = RuntimeProcessReceipts(runtime.tmux_server_socket_path.parent)
    receipt = receipts._read(receipts._path("app-server", capability.runtime_id))
    if receipt["kind"] != "app-server" or receipt["runtime_id"] != str(capability.runtime_id):
        raise RuntimeError("selected runtime process receipt has an unexpected identity")
    process_state = _read_process_state(receipt["pid"])
    if any(process_state[key] != receipt[key] for key in ("uid", "start_time_ticks", "process_group_id")):
        raise RuntimeError("selected runtime process receipt no longer owns its App Server")
    native = receipts._read(receipts._path("native-tui", capability.runtime_id))
    if (
        native["kind"] != "native-tui"
        or native["runtime_id"] != str(capability.runtime_id)
        or native["operation_id"] != receipt["operation_id"]
    ):
        raise RuntimeError("selected runtime child receipts disagree on their daemon reservation")
    native_state = _read_process_state(native["pid"])
    if any(native_state[key] != native[key] for key in ("uid", "start_time_ticks", "process_group_id")):
        raise RuntimeError("selected runtime process receipt no longer owns its native TUI")
    # Recheck immediately before stop. Managed mutations are serialized by the
    # caller's lock; the old daemon has no atomic stop-if-idle/native-input gate.
    control = selected.revalidate(exclusive=True)
    current = client.inspect_live(control)
    if current.status != "idle":
        raise RuntimeError(_busy_upgrade_message(selected, current.status))
    if current.cwd != state.cwd:
        raise RuntimeError("selected runtime working directory changed before upgrade")
    daemon = RodexDaemonClient(runtime.tmux_server_socket_path.parent, str(interpreter))
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while daemon.stop(receipt["operation_id"], str(capability.runtime_id)) != "terminal":
        if time.monotonic() >= deadline:
            raise RuntimeError("selected old runtime is still stopping; retry resume after shutdown finishes")
        time.sleep(0.05)
    deadline = time.monotonic() + 5
    while launcher.session_exists(runtime):
        if time.monotonic() >= deadline:
            raise RuntimeError("selected old tmux runtime has not exited; retry resume after shutdown finishes")
        time.sleep(0.05)
    return {
        "protocol": UPGRADE_PROTOCOL,
        "implementation": RODEX_IMPLEMENTATION_SHA256,
        "runtime_id": str(capability.runtime_id),
        "state": "terminal",
        "cwd": str(workspace),
    }


def main() -> int:
    interactive = len(sys.argv) > 1
    try:
        if interactive:
            if len(sys.argv) != 3 or sys.argv[1] != "--attach":
                raise RuntimeError("invalid retained attachment request")
            outcome = attach_selected_runtime(json.loads(sys.argv[2]))
            return 0 if outcome == "detach" else 2
        result = stop_selected_idle_runtime(json.load(sys.stdin))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(f"rodex: {error}" if interactive else str(error), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
