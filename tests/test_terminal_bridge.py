"""Foreground kernel resize survives the TTY handoff without consuming user input."""

import array
import fcntl
import os
import pty
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty
from contextlib import suppress

from rodex.daemon_client import RODEX_DAEMON_PROTOCOL, daemon_socket_path, encode_daemon_message, receive_daemon_message
from rodex.implementation_identity import RODEX_IMPLEMENTATION_ID


def test_bridge_forwards_real_sigwinch_and_never_consumes_native_input(tmp_path):
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    from pathlib import Path

    endpoint = daemon_socket_path(Path(f"/proc/{os.getpid()}/fd/{directory_fd}"))
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(endpoint))
    listener.listen(4)
    listener.settimeout(5)
    process_id, outer = pty.fork()
    if process_id == 0:
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-I",
                "-m",
                "rodex.terminal_bridge",
                "--daemon-socket",
                str(endpoint),
                "--operation-id",
                "1" * 32,
                "--runtime-id",
                "0123456789abcdef",
                "--tmux-server-id",
                "a" * 32,
                "--tmux-pane",
                "%0",
            ],
        )
    bridge = None
    transferred = []
    try:
        bridge, _ = listener.accept()
        bridge.settimeout(5)
        request, ancillary, _flags, _address = bridge.recvmsg(65536, socket.CMSG_SPACE(array.array("i").itemsize))
        assert b'"operation":"bind_terminal"' in request
        for level, kind, data in ancillary:
            if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
                descriptors = array.array("i")
                descriptors.frombytes(data)
                transferred.extend(descriptors)
        assert len(transferred) == 1
        tty.setraw(transferred[0])
        reply = encode_daemon_message(
            {"protocol": RODEX_DAEMON_PROTOCOL, "implementation_id": RODEX_IMPLEMENTATION_ID, "ok": True}
        )
        bridge.sendall(reply)

        def receive_resize():
            connection, _ = listener.accept()
            with connection:
                request = receive_daemon_message(connection)
                assert request == {
                    "protocol": RODEX_DAEMON_PROTOCOL,
                    "implementation_id": RODEX_IMPLEMENTATION_ID,
                    "operation": "wake_runtime",
                    "runtime_id": "0123456789abcdef",
                    "cause": "terminal_resize",
                }
                connection.sendall(reply)

        receive_resize()  # Initial handoff reconciliation.
        fcntl.ioctl(outer, termios.TIOCSWINSZ, struct.pack("HHHH", 15, 80, 0, 0))
        receive_resize()  # Real kernel SIGWINCH; no synthetic os.kill notification.
        os.write(outer, b"native input")
        assert select.select([transferred[0]], [], [], 2)[0]
        assert os.read(transferred[0], 100) == b"native input"
        bridge.close()
        deadline = time.monotonic() + 5
        while True:
            reaped, status = os.waitpid(process_id, os.WNOHANG)
            if reaped:
                process_id = None
                assert os.waitstatus_to_exitcode(status) == 0
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        if bridge:
            bridge.close()
        if process_id is not None:
            with suppress(ProcessLookupError):
                os.kill(process_id, signal.SIGKILL)
            os.waitpid(process_id, 0)
        listener.close()
        for descriptor in (outer, directory_fd, *transferred):
            os.close(descriptor)
