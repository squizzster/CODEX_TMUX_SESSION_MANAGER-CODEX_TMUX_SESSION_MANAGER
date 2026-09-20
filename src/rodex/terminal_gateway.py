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
from pathlib import Path
from threading import Lock, RLock
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
from .presentation_policy import PresentationSnapshotSource, PresentationSurface
from .runtime_peer import BoundProcessOwner
from .terminal_input import TerminalInputDecoder, TerminalInputInterceptor
from .terminal_surface import TerminalSurfaceRenderer

QUEUE_LIMIT_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 16384
HANDOFF_TIMEOUT_SECONDS = 0.3
HANDOFF_RECHECK_SECONDS = 0.01
EXIT_DRAIN_TIMEOUT_SECONDS = 0.5


class TerminalSessionGateway:
    """Own one native child terminal and restore the outer terminal on every exit."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: dict[str, str],
        cwd: Path,
        pipeline: SessionInteractionPipeline,
        runtime_identity: str,
        registrations: tuple[InputInterceptorRegistration, ...],
        confirm_native_prefix: Callable[[str], bool],
        presentation: PresentationSnapshotSource | None = None,
        stderr: BinaryIO | int | None = None,
        input_fd: int = 0,
        output_fd: int = 1,
        process_owner: BoundProcessOwner | None = None,
        on_process_started: Callable[[subprocess.Popen[bytes]], None] = lambda _process: None,
        on_process_stopped: Callable[[subprocess.Popen[bytes]], None] = lambda _process: None,
        close_outer_fds: bool = False,
        read_terminal_size: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._input_fd = input_fd
        self._output_fd = output_fd
        self._read_terminal_size = read_terminal_size
        self._master = -1
        self._slave = -1
        self._wake_read = -1
        self._wake_write = -1
        self._wake_lock = RLock()
        self._process_pidfd = -1
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
        self._presentation = presentation
        self._presentation_revision: int | None = None
        self._unsubscribe_presentation: Callable[[], None] | None = None
        self._supervisor_lock = Lock()
        self._supervisor_requests = 0
        self._consumed_supervisor_requests = 0
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
            self._wake_read, self._wake_write = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
            if self._presentation is not None:
                self._unsubscribe_presentation = self._presentation.subscribe(self._notify_relay)
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
                    cwd=cwd,
                    start_new_session=True,
                    pass_fds=(gate_read,),
                )
                if process_owner is not None:
                    process_owner.bind(self.process)
                on_process_started(self.process)
                self._process_announced = True
                self._process_pidfd = os.pidfd_open(self.process.pid)
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
        self._notify_relay()

    def request_supervisor_check(self) -> None:
        """Wake a blocked relay so its owner can revalidate external state."""
        with self._supervisor_lock:
            self._supervisor_requests += 1
        self._notify_relay()

    def _take_supervisor_check(self) -> bool:
        with self._supervisor_lock:
            if self._supervisor_requests == self._consumed_supervisor_requests:
                return False
            self._consumed_supervisor_requests = self._supervisor_requests
            return True

    def _apply_resize(self) -> None:
        """Apply authoritative geometry; tmux may defer its outer kernel PTY resize."""
        if self._master >= 0:
            # Claim this wake before the read. A callback during the tmux read
            # must remain pending for the next pass, not be cleared afterward.
            self._resize_pending = False
            if self._read_terminal_size is None:
                dimensions = fcntl.ioctl(self._input_fd, termios.TIOCGWINSZ, bytes(8))
            else:
                columns, rows = self._read_terminal_size()
                dimensions = struct.pack("HHHH", rows, columns, 0, 0)
            if dimensions == self._last_dimensions:
                return
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

    def wait(self, timeout: float | None = None) -> int | None:
        """Relay until native exit, an explicit supervisor event, or a deadline."""
        assert self.process is not None
        deadline = None if timeout is None else time.monotonic() + timeout
        exit_deadline: float | None = None
        if self._take_supervisor_check():
            return None
        self._sync_presentation()
        while True:
            returncode = self.process.returncode
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
            wake_deadlines = [value for value in (deadline, exit_deadline) if value is not None]
            if returncode is None:
                incomplete_deadline = self._decoder.incomplete_deadline()
                if incomplete_deadline is not None:
                    wake_deadlines.append(incomplete_deadline)
            relay_timeout = None if not wake_deadlines else max(0.0, min(wake_deadlines) - now)
            process_exited = self._relay_once(
                allow_input=returncode is None,
                timeout=relay_timeout,
            )
            if process_exited and self.process.returncode is None:
                self.process.poll()
            if self._resize_pending:
                self._apply_resize()
            self._sync_presentation()
            if self._take_supervisor_check():
                return None

    def _deliver(self, request: InteractionRequest) -> InteractionResult:
        if request.operation == InteractionOperation.DISPLAY_STATE:
            if request.payload is not None and not isinstance(request.payload, str):
                return InteractionResult(DeliveryStatus.REJECTED, "completion display requires serialized text state")
            state = InputMenuView.deserialize(request.payload) if request.payload is not None else None
            accepted, rendered = self._surface_renderer.display(state)
            self._queue_rendered_surface(rendered)
            self._notify_relay()
            return InteractionResult(DeliveryStatus.DELIVERED if accepted else DeliveryStatus.REJECTED)
        assert isinstance(request.payload, bytes)
        if request.operation == InteractionOperation.TERMINAL_INPUT:
            for event in self._decoder.feed(request.payload):
                self._interceptor.accept(event)
        else:
            rendered = self._surface_renderer.native_output(request.payload)
            # Native replies are transport, never keyboard activity or prompt input.
            self._native_queue.extend(rendered.replies)
            if rendered.stream:
                # A replaceable frame may precede new stream bytes, but cannot
                # replace them. Finish it before appending this next exact token.
                if self._pending_surface_frame is not None:
                    self._display_queue.extend(self._pending_surface_frame)
                    self._pending_surface_frame = None
                self._display_queue.extend(rendered.stream)
            self._queue_rendered_surface(rendered.frame, complete_frame=True)
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
                if (
                    not self._native_queue
                    and not self._display_queue
                    and self._pending_surface_frame is None
                    and self._surface_renderer.native.paintable
                    and self._confirm_presentation(prefix)
                ):
                    return True
                remaining = max(0.0, deadline - time.monotonic())
                # tmux consumes our flushed output independently. A failed snapshot
                # can become valid without another child output event. Recheck only
                # during this bounded handoff; ordinary idle relay remains blocking.
                process_exited = self._relay_once(allow_input=False, timeout=min(remaining, HANDOFF_RECHECK_SECONDS))
                if process_exited and self.process is not None and self.process.returncode is None:
                    self.process.poll()
                if self._resize_pending:
                    self._apply_resize()
                self._sync_presentation()
            return False
        finally:
            self._queue_rendered_surface(self._surface_renderer.resume())

    def _relay_once(self, *, allow_input: bool, timeout: float | None) -> bool:
        """Block for one real relay event and report exact native-child exit readiness."""
        reads: list[int] = []
        writes: list[int] = []
        if (
            not self._native_eof
            and len(self._display_queue) < QUEUE_LIMIT_BYTES
            and len(self._native_queue) < QUEUE_LIMIT_BYTES
        ):
            reads.append(self._master)
        if allow_input and len(self._native_queue) < QUEUE_LIMIT_BYTES:
            reads.append(self._input_fd)
        if self._wake_read >= 0:
            reads.append(self._wake_read)
        if self.process is not None and self.process.returncode is None and self._process_pidfd >= 0:
            reads.append(self._process_pidfd)
        if self._display_queue:
            writes.append(self._output_fd)
        if self._native_queue and not self._native_eof:
            writes.append(self._master)
        readable, writable, _ = select.select(reads, writes, [], timeout)
        process_exited = self._process_pidfd >= 0 and self._process_pidfd in readable
        if self._wake_read >= 0 and self._wake_read in readable:
            self._drain_relay_wake()
        for fd in writable:
            queue = self._native_queue if fd == self._master else self._display_queue
            self._flush(fd, queue)
        for fd in readable:
            if fd in {self._wake_read, self._process_pidfd}:
                continue
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
        return process_exited

    def _notify_relay(self) -> None:
        """Wake a blocked relay; repeated hints safely coalesce in the non-blocking pipe."""
        with self._wake_lock:
            if self._wake_write >= 0:
                with suppress(BlockingIOError, OSError):
                    os.write(self._wake_write, b"1")

    def _drain_relay_wake(self) -> None:
        while True:
            try:
                if not os.read(self._wake_read, READ_CHUNK_BYTES):
                    return
            except BlockingIOError:
                return
            except OSError:
                return

    def _sync_presentation(self) -> None:
        if self._presentation is None:
            return
        if self._presentation.revision == self._presentation_revision:
            return
        snapshot = self._presentation.snapshot()
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
            if self._unsubscribe_presentation is not None:
                self._unsubscribe_presentation()
                self._unsubscribe_presentation = None
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
            for fd in (
                self._master,
                self._slave,
                self._process_pidfd,
            ):
                if fd >= 0:
                    with suppress(OSError):
                        os.close(fd)
            with self._wake_lock:
                for fd in (self._wake_read, self._wake_write):
                    if fd >= 0:
                        with suppress(OSError):
                            os.close(fd)
                self._wake_read = self._wake_write = -1
            if self._close_outer_fds:
                for fd in {self._input_fd, self._output_fd}:
                    with suppress(OSError):
                        os.close(fd)
            self._master = self._slave = self._wake_read = self._wake_write = self._process_pidfd = -1
