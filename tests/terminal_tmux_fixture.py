"""Real tmux test terminal, driven over a private socket without keyboard injection."""

from __future__ import annotations

import fcntl
import json
import os
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest


class TmuxTerminal:
    def __init__(self, socket_path: str, control: socket.socket, stream, *, gateway: bool) -> None:
        self.socket_path = socket_path
        self.control = control
        self.stream = stream
        self.gateway = gateway
        self.last_size = None

    def tmux(self, *arguments: str) -> str:
        return subprocess.run(
            ["tmux", "-S", self.socket_path, *arguments], capture_output=True, text=True, check=True, timeout=5
        ).stdout

    def write(self, data: bytes) -> bytes:
        # CPR acknowledges consumption by the native model (by tmux itself in
        # the reference fixture). Gateway display writes may still be queued.
        self.control.sendall(json.dumps({"data": data.hex()}).encode() + b"\n")
        response = json.loads(self.stream.readline())
        assert "error" not in response, response
        self.last_size = response["size"]
        return bytes.fromhex(response["reply"])

    def size(self, columns: int, rows: int) -> None:
        self.tmux("resize-window", "-x", str(columns), "-y", str(rows), "-t", "fixture")
        if self.gateway:
            # Model the production daemon's external resize callback independently
            # of the foreground pane's SIGWINCH.
            with socket.socket(socket.AF_UNIX) as notification:
                notification.settimeout(5)
                notification.connect(self.control.getpeername() + ".resize")
                notification.sendall(b"resize")
            deadline = time.monotonic() + 2
            while True:
                self.write(b"")
                if self.last_size == [rows, columns]:
                    break
                assert time.monotonic() < deadline, (self.last_size, rows, columns)
                time.sleep(0.01)

    def screen(self) -> tuple[list[str], tuple[int, int]]:
        display = self.tmux("capture-pane", "-p", "-t", "%0").splitlines()
        cursor = tuple(map(int, self.tmux("display-message", "-p", "-t", "%0", "#{cursor_x},#{cursor_y}").split(",")))
        return display, cursor


@contextmanager
def tmux_terminal(tmp_path: Path, columns=80, rows=23, *, mode: str | None = None, foreground_resize=False):
    if shutil.which("tmux") is None:
        pytest.skip("real tmux required for terminal equivalence")
    tmp_path.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    short_root = f"/proc/{os.getpid()}/fd/{directory_fd}"
    socket_path, control_path = f"{short_root}/tmux.sock", f"{short_root}/control.sock"
    configuration = tmp_path / "tmux.conf"
    configuration.write_text("set -g status off\nset -g history-limit 50000\n")
    connection = socket.socket(socket.AF_UNIX)
    connection.settimeout(5)
    command = [sys.executable, str(Path(__file__).resolve()), "--gateway" if mode else "--child", control_path]
    if mode:
        command.append(mode)
        if foreground_resize:
            command.append("--foreground-resize")
    try:
        subprocess.run(
            [
                "tmux",
                "-S",
                socket_path,
                "-f",
                str(configuration),
                "new-session",
                "-d",
                "-x",
                str(columns),
                "-y",
                str(rows),
                "-s",
                "fixture",
                *command,
            ],
            check=True,
            capture_output=True,
            timeout=5,
        )
        deadline = time.monotonic() + 5
        while True:
            try:
                connection.connect(control_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                assert time.monotonic() < deadline, "terminal fixture failed to start"
                time.sleep(0.01)
        with connection.makefile("rb") as stream:
            terminal = TmuxTerminal(socket_path, connection, stream, gateway=mode is not None)
            terminal.size(columns, rows)
            yield terminal
    finally:
        connection.close()
        subprocess.run(["tmux", "-S", socket_path, "kill-server"], capture_output=True, timeout=5)
        os.close(directory_fd)


def child(path: str) -> None:
    tty.setraw(0)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(path)
        listener.listen(1)
        connection, _ = listener.accept()
        with connection, connection.makefile("rb") as stream:
            for line in stream:
                data = bytes.fromhex(json.loads(line)["data"])
                os.write(1, data + b"\x1b[6n")
                reply = bytearray()
                expected = 1 + len(re.findall(rb"\x1b\[[0-9;?]*[cn]", data))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if select.select([0], [], [], 0.02)[0]:
                        reply.extend(os.read(0, 16384))
                    if len(re.findall(rb"\x1b\[[0-9;?]*[Rcn]", reply)) >= expected:
                        break
                response = {"reply": bytes(reply).hex()}
                response["size"] = list(struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8)))[:2])
                if len(re.findall(rb"\x1b\[[0-9;?]*[Rcn]", reply)) < expected:
                    response["error"] = "missing terminal reply"
                connection.sendall(json.dumps(response).encode() + b"\n")


def gateway(path: str, mode: str, *, foreground_resize: bool) -> None:
    from rodex.interaction_pipeline import SessionInteractionPipeline
    from rodex.presentation_policy import SessionPresentationPipeline
    from rodex.terminal_gateway import TerminalSessionGateway

    presentation = SessionPresentationPipeline()
    presentation.select_policy(mode)

    def read_terminal_size():
        observed = subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", os.environ["TMUX_PANE"], "#{pane_width}|#{pane_height}"],
            text=True,
            timeout=5,
        )
        return tuple(map(int, observed.strip().split("|")))

    terminal = TerminalSessionGateway(
        [sys.executable, str(Path(__file__).resolve()), "--child", path],
        env=dict(os.environ),
        cwd=Path.cwd(),
        pipeline=SessionInteractionPipeline(),
        runtime_identity="test-terminal",
        registrations=(),
        confirm_native_prefix=lambda _: False,
        presentation=presentation,
        read_terminal_size=read_terminal_size,
    )
    signal.signal(signal.SIGWINCH, (lambda *_: terminal.resize()) if foreground_resize else signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: terminal.forward_signal(signal.SIGTERM))
    signal.signal(signal.SIGUSR1, lambda *_: presentation.select_policy("light"))
    signal.signal(signal.SIGUSR2, lambda *_: presentation.select_policy("dark"))
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(path + ".resize")
        listener.listen(4)

        def notifications():
            while True:
                try:
                    connection, _ = listener.accept()
                except OSError:
                    return
                with connection:
                    if connection.recv(16) == b"resize":
                        terminal.resize()

        Thread(target=notifications, daemon=True).start()
        try:
            terminal.wait()
        finally:
            terminal.close()


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        child(sys.argv[2])
    else:
        gateway(sys.argv[2], sys.argv[3], foreground_resize="--foreground-resize" in sys.argv)
