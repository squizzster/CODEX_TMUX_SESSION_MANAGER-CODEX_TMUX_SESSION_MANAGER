"""Session-owned PTY transport: one keyboard/output pipeline, independent of attachers.

The host's main thread pumps this gateway while supervising registration and exit.
No fork-with-Python child work, proxy worker, or per-client input owner is introduced.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import BinaryIO

from .input_interceptor_config import InputInterceptorRegistration
from .input_menu import InputMenuView
from .interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from .presentation_policy import PresentationSnapshot, PresentationSurface
from .runtime_peer import BoundProcessOwner
from .terminal_input import TerminalInputDecoder, TerminalInputInterceptor
from .terminal_surface import TerminalSurfaceRenderer

QUEUE_LIMIT_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 16384
HANDOFF_TIMEOUT_SECONDS = 0.3
EXIT_DRAIN_TIMEOUT_SECONDS = 0.5


class TerminalSessionGateway:
    """Own one native child terminal and restore the outer terminal on every exit."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: dict[str, str],
        pipeline: SessionInteractionPipeline,
        runtime_identity: str,
        registrations: tuple[InputInterceptorRegistration, ...],
        confirm_native_prefix: Callable[[str], bool],
        presentation_snapshot: Callable[[], PresentationSnapshot] | None = None,
        stderr: BinaryIO | None = None,
        input_fd: int = 0,
        output_fd: int = 1,
        process_owner: BoundProcessOwner | None = None,
        on_process_started: Callable[[subprocess.Popen[bytes]], None] = lambda _process: None,
        on_process_stopped: Callable[[subprocess.Popen[bytes]], None] = lambda _process: None,
        close_outer_fds: bool = False,
    ) -> None:
        self._pipeline = pipeline
        self._input_fd = input_fd
        self._output_fd = output_fd
        self._master = -1
        self._slave = -1
        self._closed = False
        self._native_eof = False
        self._resize_pending = True
        self._last_dimensions: bytes | None = None
        self._close_outer_fds = close_outer_fds
        self._on_process_stopped = on_process_stopped
        self._process_announced = False
        self._native_queue = bytearray()
        self._display_queue = bytearray()
        self._pending_surface_frame: bytes | None = None
        self._saved_flags: dict[int, int] = {}
        self._saved_attributes: list | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self._decoder = TerminalInputDecoder()
        self._surface_renderer = TerminalSurfaceRenderer()
        self._presentation_snapshot = presentation_snapshot
        self._presentation_revision: int | None = None
        self._confirm_presentation = confirm_native_prefix
        self._interceptor = TerminalInputInterceptor(
            registrations, pipeline, self._native_queue.extend, self._confirm_prefix
        )
        self._target = InteractionTarget(
            "terminal",
            runtime_identity,
            frozenset(
                {
                    InteractionOperation.TERMINAL_INPUT,
                    InteractionOperation.TERMINAL_OUTPUT,
                    InteractionOperation.DISPLAY_STATE,
                }
            ),
            exists=lambda: not self._closed,
            deliver=self._deliver,
        )
        try:
            self._master, self._slave = pty.openpty()
            self._saved_attributes = termios.tcgetattr(input_fd)
            termios.tcsetattr(self._slave, termios.TCSANOW, self._saved_attributes)
            # Read every original flag before changing descriptors that may share
            # one open-file description (normal stdin/stdout in a tmux pane).
            self._saved_flags = {fd: fcntl.fcntl(fd, fcntl.F_GETFL) for fd in {input_fd, output_fd}}
            self._apply_resize()
            tty.setraw(input_fd, termios.TCSANOW)
            for fd in (*self._saved_flags, self._master):
                os.set_blocking(fd, False)
            self._pipeline.register(self._target)
            gate_read, gate_write = os.pipe2(os.O_CLOEXEC)
            try:
                self.process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "rodex.terminal_exec",
                        "--parent-pid",
                        str(os.getpid()),
                        "--start-gate-fd",
                        str(gate_read),
                        "--",
                        *command,
                    ],
                    stdin=self._slave,
                    stdout=self._slave,
                    stderr=self._slave if stderr is None else stderr,
                    env=env,
                    start_new_session=True,
                    pass_fds=(gate_read,),
                )
                if process_owner is not None:
                    process_owner.bind(self.process)
                on_process_started(self.process)
                self._process_announced = True
                os.write(gate_write, b"1")
            finally:
                os.close(gate_read)
                os.close(gate_write)
            os.close(self._slave)
            self._slave = -1
        except BaseException:
            self.close()
            raise

    def resize(self) -> None:
        """Signal handlers schedule resize; the relay loop alone mutates terminal state."""
        self._resize_pending = True

    def _apply_resize(self) -> None:
        """Copy the actual pane dimensions; TIOCSWINSZ signals the child foreground group."""
        if self._master >= 0:
            dimensions = fcntl.ioctl(self._input_fd, termios.TIOCGWINSZ, bytes(8))
            if not self._resize_pending and dimensions == self._last_dimensions:
                return
            self._resize_pending = False
            self._last_dimensions = dimensions
            rows, columns, _, _ = struct.unpack("HHHH", dimensions)
            self._queue_rendered_surface(self._surface_renderer.resize(max(columns, 1), max(rows, 1)))
            fcntl.ioctl(self._master, termios.TIOCSWINSZ, dimensions)

    def forward_signal(self, signum: int) -> None:
        if self.process is not None and self.process.poll() is None:
            with suppress(ProcessLookupError, OSError):
                foreground_group = os.tcgetpgrp(self._master)
                # Before the helper claims its terminal, tcgetpgrp may return 0.
                # killpg(0, ...) would signal the host/App Server's own group.
                os.killpg(foreground_group if foreground_group > 0 else self.process.pid, signum)

    def wait(self, timeout: float | None = None) -> int:
        """Relay until native exit or the supervisor's next registration check."""
        assert self.process is not None
        deadline = None if timeout is None else time.monotonic() + timeout
        exit_deadline: float | None = None
        while True:
            returncode = self.process.poll()
            now = time.monotonic()
            if returncode is not None:
                if exit_deadline is None:
                    exit_deadline = now + EXIT_DRAIN_TIMEOUT_SECONDS
                if (
                    self._native_eof and not self._display_queue and self._pending_surface_frame is None
                ) or now >= exit_deadline:
                    return returncode
            if deadline is not None and now >= deadline:
                raise subprocess.TimeoutExpired(self.process.args, timeout)
            self._relay_once(allow_input=returncode is None)

    def _deliver(self, request: InteractionRequest) -> InteractionResult:
        if request.operation == InteractionOperation.DISPLAY_STATE:
            if request.payload is not None and not isinstance(request.payload, str):
                return InteractionResult(DeliveryStatus.REJECTED, "completion display requires serialized text state")
            state = InputMenuView.deserialize(request.payload) if request.payload is not None else None
            accepted, rendered = self._surface_renderer.display(state)
            self._queue_rendered_surface(rendered)
            return InteractionResult(DeliveryStatus.DELIVERED if accepted else DeliveryStatus.REJECTED)
        assert isinstance(request.payload, bytes)
        if request.operation == InteractionOperation.TERMINAL_INPUT:
            for event in self._decoder.feed(request.payload):
                self._interceptor.accept(event)
        else:
            self._queue_rendered_surface(self._surface_renderer.native_output(request.payload))
        return InteractionResult(DeliveryStatus.DELIVERED)

    def _dispatch(self, operation: InteractionOperation, data: bytes) -> None:
        result = self._pipeline.execute(InteractionRequest("terminal", operation, "terminal-gateway", payload=data))
        if not result.accepted:
            raise RuntimeError(f"terminal pipeline delivery failed: {result.detail}")

    def _confirm_prefix(self, prefix: str) -> bool:
        # This bounded handoff check is event-driven, never background scraping.
        # Continue draining rendering but admit no more keyboard input while the
        # already-forwarded prefix reaches the native editor.
        self._queue_rendered_surface(self._surface_renderer.suspend())
        try:
            deadline = time.monotonic() + HANDOFF_TIMEOUT_SECONDS
            while time.monotonic() < deadline and not self._native_eof:
                self._relay_once(allow_input=False)
                if not self._native_queue and not self._display_queue and self._confirm_presentation(prefix):
                    return True
            return False
        finally:
            self._queue_rendered_surface(self._surface_renderer.resume())

    def _relay_once(self, *, allow_input: bool) -> None:
        self._sync_presentation()
        self._apply_resize()
        reads: list[int] = []
        writes: list[int] = []
        if not self._native_eof and len(self._display_queue) < QUEUE_LIMIT_BYTES:
            reads.append(self._master)
        if allow_input and len(self._native_queue) < QUEUE_LIMIT_BYTES:
            reads.append(self._input_fd)
        if self._display_queue:
            writes.append(self._output_fd)
        if self._native_queue and not self._native_eof:
            writes.append(self._master)
        readable, writable, _ = select.select(reads, writes, [], 0.02)
        for fd in writable:
            queue = self._native_queue if fd == self._master else self._display_queue
            self._flush(fd, queue)
        for fd in readable:
            try:
                data = os.read(fd, READ_CHUNK_BYTES)
            except BlockingIOError:
                continue
            except OSError as error:
                if fd != self._master or error.errno != errno.EIO:
                    raise
                data = b""
            if not data:
                if fd == self._master:
                    self._native_eof = True
                    self._native_queue.clear()
                else:
                    raise EOFError("the managed runtime terminal closed")
            else:
                operation = (
                    InteractionOperation.TERMINAL_OUTPUT if fd == self._master else InteractionOperation.TERMINAL_INPUT
                )
                self._dispatch(operation, data)
        if allow_input:
            for event in self._decoder.expire_incomplete(time.monotonic()):
                self._interceptor.accept(event)

    def _sync_presentation(self) -> None:
        if self._presentation_snapshot is None:
            return
        snapshot = self._presentation_snapshot()
        if snapshot.revision == self._presentation_revision:
            return
        surface_changed = snapshot.surface != self._surface_renderer.presentation_surface
        rendered = self._surface_renderer.present(snapshot)
        self._queue_rendered_surface(rendered, complete_frame=surface_changed)
        self._presentation_revision = snapshot.revision

    def _queue_rendered_surface(self, rendered: bytes, *, complete_frame: bool = False) -> None:
        """Coalesce complete frames without truncating an in-flight terminal token."""
        if not rendered:
            return
        if complete_frame or self._surface_renderer.presentation_surface == PresentationSurface.SEMANTIC:
            if self._display_queue:
                self._pending_surface_frame = rendered
                return
            self._pending_surface_frame = None
            self._display_queue.extend(rendered)
            return
        if self._pending_surface_frame is not None:
            self._pending_surface_frame = self._surface_renderer.redraw()
            return
        self._display_queue.extend(rendered)

    def _flush(self, fd: int, queue: bytearray) -> None:
        try:
            written = os.write(fd, queue[:READ_CHUNK_BYTES])
        except BlockingIOError:
            return
        except OSError as error:
            if fd != self._master or error.errno != errno.EIO:
                raise
            self._native_eof = True
            queue.clear()
            return
        del queue[:written]
        if fd == self._output_fd and not queue and self._pending_surface_frame is not None:
            queue.extend(self._pending_surface_frame)
            self._pending_surface_frame = None

    def close(self) -> None:
        """Idempotent resource cleanup, including constructor and writer-retry failures."""
        if self._closed:
            return
        self._closed = True
        try:
            with suppress(Exception):
                self._interceptor.release()
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, 9)
                    self.process.wait(timeout=2)
            if self.process is not None and self._process_announced:
                self._on_process_stopped(self.process)
                self._process_announced = False
        finally:
            self._pipeline.unregister(self._target)
            if self._saved_attributes is not None:
                with suppress(OSError, termios.error):
                    termios.tcsetattr(self._input_fd, termios.TCSANOW, self._saved_attributes)
            for fd, flags in self._saved_flags.items():
                with suppress(OSError):
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags)
            for fd in (self._master, self._slave):
                if fd >= 0:
                    with suppress(OSError):
                        os.close(fd)
            if self._close_outer_fds:
                for fd in {self._input_fd, self._output_fd}:
                    with suppress(OSError):
                        os.close(fd)
            self._master = self._slave = -1
