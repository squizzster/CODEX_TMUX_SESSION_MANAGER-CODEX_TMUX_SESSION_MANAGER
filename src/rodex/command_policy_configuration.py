"""Stable hook definition and native, read-only trust admission."""

from __future__ import annotations

import json
import shlex
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from websockets.sync.client import unix_connect

from .app_server_contract import CODEX_APP_SERVER, RODEX_RUNTIME_APP_SERVER_CLIENT
from .process_environment import canonical_python_environment_executable
from .runtime_peer import require_unix_peer_process

POLICY_INACTIVE_SETUP = "Rodex policy inactive: run rodex _policy-setup, then start a new session"
POLICY_INACTIVE_CONFIG = "Rodex policy inactive: native hook settings do not enable it"
POLICY_INACTIVE_UNAVAILABLE = "Rodex policy inactive: native hook trust could not be verified"
POLICY_INACTIVE_REASONS = frozenset({POLICY_INACTIVE_SETUP, POLICY_INACTIVE_CONFIG, POLICY_INACTIVE_UNAVAILABLE})


def policy_helper_command(python_executable: str | None = None) -> str:
    interpreter = canonical_python_environment_executable(python_executable or sys.executable)
    return shlex.join((interpreter, "-I", "-m", "rodex.command_policy_hook"))


def policy_hook_override(python_executable: str | None = None) -> str:
    command = json.dumps(policy_helper_command(python_executable), ensure_ascii=True)
    return 'hooks.PreToolUse=[{matcher="^Bash$",hooks=[{type="command",command=' + command + ",timeout=3}]}]"


def caller_bypasses_hook_trust(arguments: tuple[str, ...]) -> bool | None:
    """Read only explicit trust intent; never translate or mutate native options."""
    # Runtime composition already selected a managed route. Native resume argv
    # reaches this boundary too; its explicit trust options must retain meaning.
    options = arguments[: arguments.index("--")] if "--" in arguments else arguments
    if "--dangerously-bypass-hook-trust" in options:
        return True
    bypass = None
    for index, token in enumerate(options):
        value: str | None = None
        if token in {"-c", "--config"} and index + 1 < len(options):
            value = options[index + 1]
        elif token.startswith("--config="):
            value = token.removeprefix("--config=")
        elif token.startswith("-c") and token != "-c":
            value = token[2:]
        if value is not None:
            try:
                selected = tomllib.loads(value).get("bypass_hook_trust")
            except tomllib.TOMLDecodeError:
                continue
            if isinstance(selected, bool):
                bypass = selected
    return bypass


def policy_hook_inactive_reason(
    socket_path: Path,
    process: Any,
    workspace: Path,
    arguments: tuple[str, ...],
) -> str | None:
    """Use Codex's hash/trust verdict, never compute or persist trust ourselves.

    An untrusted injected hook would block the remote TUI before thread creation.
    Runtime composition therefore removes only its own injection on this result.
    """
    try:
        with unix_connect(
            str(socket_path),
            uri=f"ws://localhost{CODEX_APP_SERVER.rpc_connection_path}",
            open_timeout=2,
            close_timeout=1,
            compression=None,
        ) as connection:
            require_unix_peer_process(connection, process)

            def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
                request_id = f"rodex-policy:{method}"
                connection.send(json.dumps(CODEX_APP_SERVER.request(request_id, method, params)))
                deadline = time.monotonic() + 3
                while True:
                    response = json.loads(connection.recv(timeout=max(0, deadline - time.monotonic())))
                    if response.get("id") == request_id:
                        if "error" in response:
                            raise ValueError("native hook admission unavailable")
                        return response["result"]

            request(
                CODEX_APP_SERVER.initialize_method,
                CODEX_APP_SERVER.initialize_params(
                    RODEX_RUNTIME_APP_SERVER_CLIENT,
                    experimental_api=True,
                ),
            )
            connection.send(json.dumps(CODEX_APP_SERVER.initialized_notification()))
            listed = request(CODEX_APP_SERVER.hooks_list_method, {"cwds": [str(workspace)]})
            hooks = [
                hook
                for entry in listed["data"]
                for hook in entry["hooks"]
                if entry["cwd"] == str(workspace)
                and hook.get("command") == policy_helper_command()
                and hook.get("source") == "sessionFlags"
                and hook.get("eventName") == "preToolUse"
                and hook.get("matcher") == "^Bash$"
            ]
            if len(hooks) != 1 or hooks[0].get("enabled") is not True:
                return POLICY_INACTIVE_CONFIG
            if hooks[0].get("trustStatus") == "trusted":
                return None
            explicit_bypass = caller_bypasses_hook_trust(arguments)
            if explicit_bypass is not None:
                return None if explicit_bypass else POLICY_INACTIVE_SETUP
            effective = request(CODEX_APP_SERVER.config_read_method, {"cwd": str(workspace), "includeLayers": False})
            if effective.get("config", {}).get("bypass_hook_trust") is True:
                return None
            return POLICY_INACTIVE_SETUP
    except Exception:
        return POLICY_INACTIVE_UNAVAILABLE
