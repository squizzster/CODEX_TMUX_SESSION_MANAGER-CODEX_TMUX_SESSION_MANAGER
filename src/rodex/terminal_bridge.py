"""Transfer TTY ownership and retain its foreground resize signal boundary."""

from __future__ import annotations

import argparse
import array
import os
import select
import signal
import socket
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from rodex_registry.identity import parse_rodex_runtime_id

from .daemon_client import (
    RODEX_DAEMON_IMPLEMENTATION_FIELD,
    RODEX_DAEMON_PROTOCOL,
    RODEX_RUNTIME_WAKE_TERMINAL_RESIZE,
    RodexDaemonClient,
    RodexDaemonError,
    encode_daemon_message,
    receive_daemon_message,
)
from .implementation_identity import RODEX_IMPLEMENTATION_ID
from .tmux_session_capability import parse_tmux_server_id


def wait_for_runtime(connection: socket.socket, notify_resize: Callable[[], object]) -> None:
    """Keep native SIGWINCH observable without ever reading or writing the pane TTY.

    Some tmux operations (notably unequal pane swaps) have no layout hook. The
    kernel's foreground notification is their authoritative completion event.
    Signal handlers only wake this loop; daemon I/O runs outside the handler.
    """
    wake_read, wake_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    previous_handler = signal.signal(signal.SIGWINCH, lambda *_: None)
    previous_wakeup = signal.set_wakeup_fd(wake_write, warn_on_full_buffer=False)
    try:
        pending_resize = True  # Cover a size change during the initial handoff.
        while True:
            if pending_resize:
                # A concurrent runtime exit closes the retained connection. It
                # may retire the reservation before this final hint is handled.
                with suppress(OSError, TimeoutError, RodexDaemonError):
                    notify_resize()
                pending_resize = False
            readable, _, _ = select.select((connection, wake_read), (), ())
            if connection in readable:
                if connection.recv(1):
                    raise RuntimeError("terminal bridge received unexpected data after handoff")
                return
            if wake_read in readable:
                signals = os.read(wake_read, 4096)
                pending_resize = signal.SIGWINCH in signals
    finally:
        signal.set_wakeup_fd(previous_wakeup)
        signal.signal(signal.SIGWINCH, previous_handler)
        os.close(wake_read)
        os.close(wake_write)


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
        client = RodexDaemonClient(arguments.daemon_socket.parent, sys.executable)
        wait_for_runtime(
            connection,
            lambda: client.notify_runtime(str(arguments.runtime_id), RODEX_RUNTIME_WAKE_TERMINAL_RESIZE),
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
