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
from threading import Event, Thread

import pytest

from rodex.daemon_client import (
    RODEX_DAEMON_INSTANCE_FIELD,
    RODEX_DAEMON_PROTOCOL,
    daemon_instance_path,
    daemon_socket_path,
    encode_daemon_message,
    receive_daemon_message,
)
from rodex.implementation_identity import RODEX_IMPLEMENTATION_ID
from rodex.terminal_bridge import wait_for_runtime


def test_bridge_resizes_and_observes_eof_with_high_connection_and_wake_fds(monkeypatch):
    original_pipe = os.pipe2
    previous_handler = signal.getsignal(signal.SIGWINCH)
    set_wakeup_fd = signal.set_wakeup_fd
    wakeup_changes = []
    retained_wake_descriptors = []

    def observed_wakeup_fd(fd, **options):
        previous = set_wakeup_fd(fd, **options)
        wakeup_changes.append((fd, previous))
        return previous

    def high_pipe(flags):
        originals = original_pipe(flags)
        high = []
        try:
            for fd in originals:
                high.append(fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 1100))
        except BaseException:
            for fd in high:
                os.close(fd)
            raise
        finally:
            for fd in originals:
                os.close(fd)
        retained_wake_descriptors.extend(high)
        return high

    monkeypatch.setattr("rodex.terminal_bridge.os.pipe2", high_pipe)
    monkeypatch.setattr("rodex.terminal_bridge.signal.set_wakeup_fd", observed_wakeup_fd)
    initial, resized = Event(), Event()

    def notify_resize():
        (resized if initial.is_set() else initial).set()

    sender, receiver = socket.socketpair()
    with (
        sender,
        receiver,
        socket.socket(fileno=fcntl.fcntl(receiver.fileno(), fcntl.F_DUPFD_CLOEXEC, 1100)) as high_connection,
    ):

        def resize_then_close():
            try:
                if initial.wait(1):
                    os.kill(os.getpid(), signal.SIGWINCH)
                    resized.wait(1)
            finally:
                sender.shutdown(socket.SHUT_WR)

        thread = Thread(target=resize_then_close)
        thread.start()
        try:
            wait_for_runtime(high_connection, notify_resize)
        finally:
            thread.join(2)
        assert resized.is_set()
        assert not thread.is_alive()
    assert signal.getsignal(signal.SIGWINCH) is previous_handler
    assert len(wakeup_changes) == 2
    assert wakeup_changes[1] == (wakeup_changes[0][1], wakeup_changes[0][0])
    for fd in retained_wake_descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_bridge_forwards_real_sigwinch_and_never_consumes_native_input(tmp_path):
    daemon_instance_id = "d" * 64
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    from pathlib import Path

    endpoint = daemon_socket_path(Path(f"/proc/{os.getpid()}/fd/{directory_fd}"))
    capability = daemon_instance_path(tmp_path)
    capability.write_text(f"{daemon_instance_id}\n")
    capability.chmod(0o600)
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
        discovery, _ = listener.accept()
        with discovery:
            assert receive_daemon_message(discovery) == {
                "protocol": RODEX_DAEMON_PROTOCOL,
                "implementation_id": RODEX_IMPLEMENTATION_ID,
                RODEX_DAEMON_INSTANCE_FIELD: daemon_instance_id,
                "operation": "ping",
            }
            discovery.sendall(
                encode_daemon_message(
                    {
                        "protocol": RODEX_DAEMON_PROTOCOL,
                        "implementation_id": RODEX_IMPLEMENTATION_ID,
                        RODEX_DAEMON_INSTANCE_FIELD: daemon_instance_id,
                        "ok": True,
                    }
                )
            )
        bridge, _ = listener.accept()
        bridge.settimeout(5)
        request, ancillary, _flags, _address = bridge.recvmsg(65536, socket.CMSG_SPACE(array.array("i").itemsize))
        assert b'"operation":"bind_terminal"' in request
        assert daemon_instance_id.encode() in request
        for level, kind, data in ancillary:
            if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
                descriptors = array.array("i")
                descriptors.frombytes(data)
                transferred.extend(descriptors)
        assert len(transferred) == 1
        tty.setraw(transferred[0])
        reply = encode_daemon_message(
            {
                "protocol": RODEX_DAEMON_PROTOCOL,
                "implementation_id": RODEX_IMPLEMENTATION_ID,
                RODEX_DAEMON_INSTANCE_FIELD: daemon_instance_id,
                "ok": True,
            }
        )
        bridge.sendall(reply)

        def receive_resize():
            connection, _ = listener.accept()
            with connection:
                request = receive_daemon_message(connection)
                assert request == {
                    "protocol": RODEX_DAEMON_PROTOCOL,
                    "implementation_id": RODEX_IMPLEMENTATION_ID,
                    RODEX_DAEMON_INSTANCE_FIELD: daemon_instance_id,
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
