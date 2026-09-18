"""One-shot tmux pane TTY handoff into the shared Rodex daemon."""

from __future__ import annotations

import argparse
import array
import os
import socket
from pathlib import Path

from rodex_registry.identity import parse_rodex_runtime_id

from .daemon_client import (
    RODEX_DAEMON_IMPLEMENTATION_FIELD,
    RODEX_DAEMON_PROTOCOL,
    encode_daemon_message,
    receive_daemon_message,
)
from .implementation_identity import RODEX_IMPLEMENTATION_ID
from .tmux_session_capability import parse_tmux_server_id


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m rodex.terminal_bridge")
    parser.add_argument("--daemon-socket", required=True, type=Path)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--runtime-id", required=True, type=parse_rodex_runtime_id)
    parser.add_argument("--tmux-server-id", required=True, type=parse_tmux_server_id)
    parser.add_argument("--tmux-pane", required=True)
    arguments = parser.parse_args()
    if len(arguments.operation_id) != 32 or any(
        character not in "0123456789abcdef" for character in arguments.operation_id
    ):
        parser.error("--operation-id must be 32 lowercase hexadecimal characters")
    if not arguments.tmux_pane.startswith("%") or not arguments.tmux_pane[1:].isdigit():
        parser.error("--tmux-pane must be an exact pane target")

    request = encode_daemon_message(
        {
            "protocol": RODEX_DAEMON_PROTOCOL,
            RODEX_DAEMON_IMPLEMENTATION_FIELD: RODEX_IMPLEMENTATION_ID,
            "operation": "bind_terminal",
            "operation_id": arguments.operation_id,
            "runtime_id": str(arguments.runtime_id),
            "tmux_server_id": arguments.tmux_server_id,
            "tmux_pane_target": arguments.tmux_pane,
        }
    )
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(os.fspath(arguments.daemon_socket))
        sent = connection.sendmsg(
            [request],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [0]))],
        )
        if sent != len(request):
            raise RuntimeError("terminal bridge request was only partially sent")
        response = receive_daemon_message(connection)
        if (
            response.get("protocol") != RODEX_DAEMON_PROTOCOL
            or response.get(RODEX_DAEMON_IMPLEMENTATION_FIELD) != RODEX_IMPLEMENTATION_ID
            or response.get("ok") is not True
        ):
            detail = response.get("error")
            raise RuntimeError(detail if isinstance(detail, str) and detail else "terminal bridge was rejected")
        descriptor = connection.detach()
        os.set_inheritable(descriptor, True)
        if descriptor != 0:
            os.dup2(descriptor, 0, inheritable=True)
            os.close(descriptor)
        os.execv("/usr/bin/cat", ["cat"])
    finally:
        connection.close()


if __name__ == "__main__":
    main()
