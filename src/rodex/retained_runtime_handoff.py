"""Run this file with the selected OLD retained interpreter, always with ``-I``.

Only the adapter itself comes from the current installation. Imports, control
handshakes, process receipts and daemon requests use the owning old installation.
The caller holds the durable session transition lock across stop and new resume;
this child must not reacquire that cross-process lock.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

from rodex.control import CodexControlClient
from rodex.daemon_client import RODEX_DAEMON_PROTOCOL, RodexDaemonClient
from rodex.implementation_identity import RODEX_IMPLEMENTATION_SHA256
from rodex.installation import retained_installation_interpreter
from rodex.live_runtime import require_durable_runtime_instance, require_live_runtime_identity
from rodex.process_receipts import PROCESS_RECEIPT_PROTOCOL, RuntimeProcessReceipts, _read_process_state
from rodex.runtime import LiveTmuxSession, RodexRuntimeLauncher
from rodex.tmux_session_capability import (
    RODEX_SHARED_TMUX_PROTOCOL,
    parse_tmux_session_capability,
    runtime_destruction_if_shell_condition,
)
from rodex.tmux_sharing_coordinator import RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION
from rodex_registry import lookup_rodex_registry_id, lookup_rodex_runtime_registration

UPGRADE_PROTOCOL = "rodex-retained-runtime-upgrade-v1"
STOP_TIMEOUT_SECONDS = 15.0


def stop_selected_idle_runtime(request: dict) -> dict:
    """Authenticate the old implementation and retire one exact idle reservation."""
    if (
        request["protocol"] != UPGRADE_PROTOCOL
        or request["implementation"] != RODEX_IMPLEMENTATION_SHA256
        or RODEX_SHARED_TMUX_PROTOCOL != "rodex-isolated-tmux-v5"
        or RODEX_DAEMON_PROTOCOL != "rodex-daemon-v3"
        or PROCESS_RECEIPT_PROTOCOL != "rodex-process-receipt-v3"
    ):
        raise RuntimeError("selected runtime is outside the supported retained-installation upgrade contract")
    interpreter = retained_installation_interpreter()
    if interpreter.parents[2].name != request["implementation"]:
        raise RuntimeError("selected retained installation changed before upgrade")
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
    database_path = Path(request["database_path"])
    launcher = RodexRuntimeLauncher(request["codex_binary"], request["tmux_binary"])

    def revalidate():
        if lookup_rodex_registry_id(database_path) != capability.registry_id:
            raise RuntimeError("selected session catalog changed before upgrade")
        registration = lookup_rodex_runtime_registration(capability.internal_session_id, database_path)
        if (
            registration is None
            or registration.runtime_id != capability.runtime_id
            or registration.codex_session_id != capability.codex_session_id
            or registration.tmux_session.tmux_server_socket_path != str(capability.tmux_server_socket_path)
            or registration.tmux_session.tmux_session_name != runtime.tmux_session_name
        ):
            raise RuntimeError("selected session changed before upgrade")
        control = launcher.discover_runtime_control(runtime)
        require_live_runtime_identity(
            control,
            expected_rodex_session_id=capability.rodex_session_id,
            expected_registry_id=capability.registry_id,
            expected_codex_session_id=capability.codex_session_id,
        )
        require_durable_runtime_instance(capability.internal_session_id, database_path, control)
        if control.tmux_capability != capability:
            raise RuntimeError("selected runtime capability changed before upgrade")
        command = launcher._read_tmux_server_option(
            runtime, RODEX_SHARED_TMUX_COORDINATOR_COMMAND_OPTION, expected_server_id=capability.tmux_server_id
        )
        if command != request["coordinator_command"]:
            raise RuntimeError("selected runtime coordinator changed before upgrade")
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

    control = revalidate()
    client = CodexControlClient()
    state = client.inspect_live(control)
    if state.status != "idle":
        raise RuntimeError(
            f"session {runtime.tmux_session_name!r} is {state.status}; wait until its turn is idle and retry resume"
        )
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
    control = revalidate()
    current = client.inspect_live(control)
    if current.status != "idle":
        raise RuntimeError(
            f"session {runtime.tmux_session_name!r} is {current.status}; wait until its turn is idle and retry resume"
        )
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
    try:
        request = json.load(sys.stdin)
        result = stop_selected_idle_runtime(request)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
