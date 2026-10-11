"""Stable native PreToolUse helper. An unchanged allow emits no hook output."""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
from pathlib import Path
from typing import Any, BinaryIO

from .command_policy import (
    POLICY_MESSAGE_LIMIT,
    POLICY_SOCKET_ENV,
    POLICY_TIMEOUT_SECONDS,
    CommandPolicyDecision,
    CommandPolicyRequest,
    encode_policy_message,
    receive_policy_message,
)


def native_hook_response(request: CommandPolicyRequest, decision: CommandPolicyDecision) -> dict[str, Any] | None:
    if decision.action == "allow":
        return None
    output: dict[str, Any] = {"hookEventName": "PreToolUse"}
    if decision.action == "deny":
        output.update(permissionDecision="deny", permissionDecisionReason=decision.reason)
    else:
        output.update(permissionDecision="allow", updatedInput={**request.tool_input, "command": decision.command})
    return {"hookSpecificOutput": output}


def run_hook(stream: BinaryIO, socket_path: str | None) -> dict[str, Any] | None:
    if not socket_path:
        return
    try:
        path = Path(socket_path)
        state = path.lstat()
        if not path.is_absolute() or not stat.S_ISSOCK(state.st_mode) or state.st_uid != os.getuid():
            return
        if stat.S_IMODE(state.st_mode) != 0o600 or not path.name.startswith("policy-") or path.suffix != ".sock":
            return
        runtime_id = path.stem.removeprefix("policy-")
        content = stream.read(POLICY_MESSAGE_LIMIT + 1)
        if len(content) > POLICY_MESSAGE_LIMIT:
            return
        event = json.loads(content)
        if not isinstance(event, dict):
            return
        request = CommandPolicyRequest.from_hook(runtime_id, event)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(POLICY_TIMEOUT_SECONDS)
            connection.connect(socket_path)
            connection.sendall(encode_policy_message(request.to_payload()))
            response = receive_policy_message(connection)
            decision = CommandPolicyDecision.from_payload(response, runtime_id)
            return native_hook_response(request, decision)
    except Exception:
        # Never expose event contents, including through Python tracebacks. Native
        # Codex enforcement remains authoritative when this advisory path fails.
        return


def main() -> None:
    response = run_hook(sys.stdin.buffer, os.environ.get(POLICY_SOCKET_ENV))
    if response is not None:
        sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
