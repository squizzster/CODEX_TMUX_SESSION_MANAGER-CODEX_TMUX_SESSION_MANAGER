"""Stable hook definition and native, read-only trust admission."""

from __future__ import annotations

import json
import re
import shlex
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from websockets.sync.client import unix_connect

from .app_server_contract import CODEX_APP_SERVER, RODEX_RUNTIME_APP_SERVER_CLIENT
from .process_environment import canonical_python_environment_executable
from .runtime_peer import require_unix_peer_process

POLICY_INACTIVE_SETUP = "Rodex policy inactive: run rodex _policy-setup, then start a new session"
POLICY_INACTIVE_CONFIG = "Rodex policy inactive: native hook settings do not enable it"
POLICY_INACTIVE_UNAVAILABLE = "Rodex policy inactive: native hook trust could not be verified"
POLICY_INACTIVE_REASONS = frozenset({POLICY_INACTIVE_SETUP, POLICY_INACTIVE_CONFIG, POLICY_INACTIVE_UNAVAILABLE})
POLICY_HOOK_TRUST_BYPASS_ARGUMENT: Final = "--dangerously-bypass-hook-trust"
POLICY_HOOK_TIMEOUT_SECONDS: Final = 3


@dataclass(frozen=True, slots=True)
class NativePolicyHookTrust:
    """One native-reported hook identity admitted for this private App Server."""

    key: str
    current_hash: str

    def configuration_override(self) -> str:
        # Override paths split on dots, even inside quotes. Put the native key in
        # the TOML value instead; Codex merges this entry with existing hook state.
        return "hooks.state={" + json.dumps(self.key) + "={trusted_hash=" + json.dumps(self.current_hash) + "}}"


@dataclass(frozen=True, slots=True)
class PolicyHookAdmission:
    """The native policy verdict and any exact private-server trust request."""

    inactive_reason: str | None = None
    temporary_trust: NativePolicyHookTrust | None = None

    def __post_init__(self) -> None:
        if self.inactive_reason is not None and self.temporary_trust is not None:
            raise ValueError("an inactive policy cannot request temporary hook trust")


def policy_helper_command(python_executable: str | None = None) -> str:
    interpreter = canonical_python_environment_executable(python_executable or sys.executable)
    return shlex.join((interpreter, "-I", "-m", "rodex.command_policy_hook"))


def policy_hook_override(python_executable: str | None = None) -> str:
    command = json.dumps(policy_helper_command(python_executable), ensure_ascii=True)
    return (
        'hooks.PreToolUse=[{matcher="^Bash$",hooks=[{type="command",command='
        + command
        + ",timeout="
        + str(POLICY_HOOK_TIMEOUT_SECONDS)
        + "}]}]"
    )


def caller_bypasses_hook_trust(arguments: tuple[str, ...]) -> bool | None:
    """Read only explicit trust intent; never translate or mutate native options."""
    # Runtime composition already selected a managed route. Native resume argv
    # reaches this boundary too; its explicit trust options must retain meaning.
    options = arguments[: arguments.index("--")] if "--" in arguments else arguments
    if POLICY_HOOK_TRUST_BYPASS_ARGUMENT in options:
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


def _workspace_hook_inventory(listed: dict[str, Any], workspace: Path) -> tuple[dict[str, Any], ...]:
    data = listed.get("data")
    if not isinstance(data, list) or len(data) != 1:
        raise ValueError("native hook inventory has an unexpected workspace count")
    entry = data[0]
    if not isinstance(entry, dict) or entry.get("cwd") != str(workspace):
        raise ValueError("native hook inventory has an unexpected workspace")
    if entry.get("errors") not in (None, []) or entry.get("warnings") not in (None, []):
        raise ValueError("native hook inventory reported configuration diagnostics")
    hooks = entry.get("hooks")
    if not isinstance(hooks, list) or not all(
        isinstance(hook, dict) and isinstance(hook.get("enabled"), bool) for hook in hooks
    ):
        raise ValueError("native hook inventory is malformed")
    return tuple(hooks)


def _is_exact_rodex_policy_hook(hook: dict[str, Any]) -> bool:
    display_order = hook.get("displayOrder")
    return (
        hook.get("command") == policy_helper_command()
        and hook.get("source") == "sessionFlags"
        and hook.get("sourcePath") == "/<session-flags>/config.toml"
        and hook.get("eventName") == "preToolUse"
        and hook.get("handlerType") == "command"
        and hook.get("matcher") == "^Bash$"
        and hook.get("timeoutSec") == POLICY_HOOK_TIMEOUT_SECONDS
        and hook.get("async") is False
        and hook.get("isManaged") is False
        and hook.get("pluginId") is None
        and hook.get("statusMessage") is None
        and hook.get("additionalContextLimit") is None
        and isinstance(hook.get("key"), str)
        and bool(hook["key"])
        and isinstance(hook.get("currentHash"), str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", hook["currentHash"]) is not None
        and isinstance(display_order, int)
        and not isinstance(display_order, bool)
    )


def policy_hook_admission(
    socket_path: Path,
    process: Any,
    workspace: Path,
    arguments: tuple[str, ...],
    *,
    expected_trust: NativePolicyHookTrust | None = None,
) -> PolicyHookAdmission:
    """Admit the exact Rodex hook and narrowly adapt native trust when safe.

    Rodex never computes or persists native trust. It may supply the native key
    and hash as private App Server configuration for its sole enabled hook, then
    require a second native inventory to verify that exact admission before TUI.
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
            hooks = _workspace_hook_inventory(listed, workspace)
            policy_hooks = tuple(hook for hook in hooks if _is_exact_rodex_policy_hook(hook))
            if len(policy_hooks) != 1 or policy_hooks[0].get("enabled") is not True:
                return PolicyHookAdmission(POLICY_INACTIVE_CONFIG)
            policy_hook = policy_hooks[0]
            trust_status = policy_hook.get("trustStatus")
            native_trust = NativePolicyHookTrust(policy_hook["key"], policy_hook["currentHash"])
            enabled_hooks = tuple(hook for hook in hooks if hook["enabled"])
            sole_policy_hook = len(enabled_hooks) == 1 and enabled_hooks[0] is policy_hook
            if expected_trust is not None:
                if native_trust != expected_trust or trust_status != "trusted":
                    return PolicyHookAdmission(POLICY_INACTIVE_CONFIG)
                if not sole_policy_hook:
                    return PolicyHookAdmission(POLICY_INACTIVE_SETUP)
                return PolicyHookAdmission()
            if trust_status == "trusted":
                return PolicyHookAdmission()
            explicit_bypass = caller_bypasses_hook_trust(arguments)
            if explicit_bypass is False:
                return PolicyHookAdmission(POLICY_INACTIVE_SETUP)
            # A retained-installation upgrade changes the helper path/hash. Native
            # "modified" means old trust no longer matches, not that the current
            # hook failed the exact Rodex definition check above.
            if trust_status not in {"untrusted", "modified"}:
                return PolicyHookAdmission(POLICY_INACTIVE_CONFIG)
            effective = request(CODEX_APP_SERVER.config_read_method, {"cwd": str(workspace), "includeLayers": False})
            effective_config = effective.get("config")
            if not isinstance(effective_config, dict):
                raise ValueError("native effective configuration is malformed")
            configured_bypass = effective_config.get("bypass_hook_trust")
            if configured_bypass not in (None, False, True):
                raise ValueError("native hook trust configuration is malformed")
            if not sole_policy_hook:
                if explicit_bypass is True or configured_bypass is True:
                    return PolicyHookAdmission()
                return PolicyHookAdmission(POLICY_INACTIVE_SETUP)
            return PolicyHookAdmission(temporary_trust=native_trust)
    except Exception:
        return PolicyHookAdmission(POLICY_INACTIVE_UNAVAILABLE)


def policy_hook_inactive_reason(
    socket_path: Path,
    process: Any,
    workspace: Path,
    arguments: tuple[str, ...],
) -> str | None:
    """Compatibility projection of the richer policy admission verdict."""
    return policy_hook_admission(socket_path, process, workspace, arguments).inactive_reason
