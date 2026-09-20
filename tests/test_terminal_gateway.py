"""Real isolated PTYs verify transport, child terminal ownership and restoration."""

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import pyte
import pytest

from rodex.input_interceptor_config import INPUT_INTERCEPTORS
from rodex.input_menu import INPUT_MENU_TARGET, InputMenuStage, InputMenuView
from rodex.interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from rodex.presentation_policy import PresentationSnapshot, PresentationSurface
from rodex.protocol_input_text import PromptTextEdit
from rodex.terminal_gateway import TerminalSessionGateway
from rodex.terminal_surface import TerminalSurfaceRenderer

ECHO_CHILD = """
import fcntl, os, signal, struct, sys, termios, tty
tty.setraw(0)
assert os.tcgetpgrp(0) == os.getpgrp()
controlling_terminal = os.open('/dev/tty', os.O_RDWR)
os.close(controlling_terminal)
signal.signal(signal.SIGINT, lambda *_: os.write(1, b'INTERRUPTED'))
signal.signal(signal.SIGWINCH, lambda *_: os.write(1, b'RESIZED'))
os.write(1, b'READY')
while True:
    data = os.read(0, 16384)
    if data == b'\\x04':
        os.write(1, b'FINAL-OUTPUT')
        sys.exit(23)
    if data == b'?':
        rows, columns, _, _ = struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, bytes(8)))
        os.write(1, f'SIZE={rows},{columns}'.encode())
    else:
        os.write(1, data)
"""


@contextmanager
def outer_terminal():
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 120, 0, 0))
    attributes = termios.tcgetattr(slave)
    flags = fcntl.fcntl(slave, fcntl.F_GETFL)
    try:
        yield master, slave
        assert termios.tcgetattr(slave) == attributes
        assert fcntl.fcntl(slave, fcntl.F_GETFL) == flags
    finally:
        os.close(master)
        os.close(slave)


def read_until(gateway, master, expected):
    output = bytearray()
    deadline = time.monotonic() + 5
    while expected not in output and time.monotonic() < deadline:
        with suppress(subprocess.TimeoutExpired):
            gateway.wait(timeout=0.04)
        while select.select([master], [], [], 0)[0]:
            output.extend(os.read(master, 65536))
    assert expected in output, bytes(output)
    return bytes(output)


def start_gateway(slave, pipeline, registrations=(), presentation=None):
    return TerminalSessionGateway(
        [sys.executable, "-I", "-c", ECHO_CHILD],
        env=dict(os.environ),
        cwd=Path.cwd(),
        pipeline=pipeline,
        runtime_identity="test-owned-runtime",
        registrations=registrations,
        confirm_native_prefix=lambda _prefix: True,
        presentation=presentation,
        input_fd=slave,
        output_fd=slave,
    )


class MutablePresentationSource:
    def __init__(self):
        self.revision = 0
        self.snapshot_calls = 0
        self.revised_snapshot = threading.Event()
        self._listeners = set()

    def snapshot(self):
        self.snapshot_calls += 1
        if self.revision:
            self.revised_snapshot.set()
        return PresentationSnapshot(self.revision, "dark", PresentationSurface.NATIVE, "", (), ())

    def subscribe(self, listener):
        self._listeners.add(listener)

        def unsubscribe():
            self._listeners.discard(listener)

        return unsubscribe

    def advance(self):
        self.revision += 1
        for listener in tuple(self._listeners):
            listener()


def test_new_complete_surface_waits_for_inflight_terminal_bytes_and_coalesces(monkeypatch):
    gateway = TerminalSessionGateway.__new__(TerminalSessionGateway)
    gateway._output_fd = 19
    gateway._display_queue = bytearray(b"\x1b]unfinished-native-control-string")
    gateway._pending_surface_frame = None
    gateway._surface_renderer = TerminalSurfaceRenderer(80, 12)
    gateway._surface_renderer.native_output(b"\x1b[9;1H\xe2\x80\xba prompt\x1b[9;9H")
    first = gateway._surface_renderer.present(
        PresentationSnapshot(1, "first", PresentationSurface.SEMANTIC, "FIRST", (), ())
    )
    second = gateway._surface_renderer.present(
        PresentationSnapshot(2, "second", PresentationSurface.SEMANTIC, "SECOND", (), ())
    )

    gateway._queue_rendered_surface(first, complete_frame=True)
    gateway._queue_rendered_surface(second)
    assert gateway._display_queue == b"\x1b]unfinished-native-control-string"
    assert gateway._pending_surface_frame == second

    monkeypatch.setattr(os, "write", lambda file_descriptor, data: len(data) if file_descriptor == 19 else 0)
    gateway._flush(19, gateway._display_queue)
    assert gateway._display_queue == second
    assert gateway._pending_surface_frame is None


def test_real_child_terminal_pass_through_resize_signal_exit_and_outer_restoration():
    pipeline = SessionInteractionPipeline()
    with outer_terminal() as (master, slave):
        gateway = start_gateway(slave, pipeline)
        try:
            read_until(gateway, master, b"READY")
            os.write(master, "hello 世界".encode())
            read_until(gateway, master, "hello 世界".encode())
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 44, 92, 0, 0))
            gateway.resize()
            read_until(gateway, master, b"RESIZED")
            os.write(master, b"?")
            read_until(gateway, master, b"SIZE=44,92")
            gateway.forward_signal(signal.SIGINT)
            read_until(gateway, master, b"INTERRUPTED")
            os.write(master, b"\x04")
            read_until(gateway, master, b"FINAL-OUTPUT")
            assert gateway.wait(timeout=2) == 23
            assert {record.operation for record in pipeline.records} == {
                InteractionOperation.TERMINAL_INPUT,
                InteractionOperation.TERMINAL_OUTPUT,
            }
        finally:
            gateway.close()


def test_real_pty_rewrites_verified_prompt_before_native_enter():
    calls = []

    def hook(texts):
        calls.append(texts)
        return tuple((PromptTextEdit(0, len(text), "Hello!"),) if text == "Hello" else () for text in texts)

    pipeline = SessionInteractionPipeline(input_text_hook=hook)
    with outer_terminal() as (master, slave):
        gateway = start_gateway(slave, pipeline)
        try:
            read_until(gateway, master, b"READY")
            os.write(master, b"Hello\r")
            rewritten = b"\x7f" * 5 + b"\x1b[200~Hello!\x1b[201~\r"
            output = read_until(gateway, master, rewritten)

            assert output.index(rewritten) >= output.index(b"Hello") + len(b"Hello")
            assert calls == [("Hello",)]
        finally:
            gateway.close()


def test_canonical_editor_output_reaches_outer_pty_before_enter_and_receipt():
    # A native editor fixture, not an echo process: it consumes edit events and
    # emits a new ANSI frame. The assertion reads that actual outer PTY output.
    child = r"""
import os, tty
tty.setraw(0)
text = b''
def render():
    os.write(1, b'\x1b[2J\x1b[H' + '\u203a '.encode() + text)
os.write(1, b'READY')
render()
while True:
    key = os.read(0, 1)
    if key == b'\x1b':
        sequence = key + os.read(0, 5)
        assert sequence == b'\x1b[200~', sequence
        pasted = b''
        while not pasted.endswith(b'\x1b[201~'):
            pasted += os.read(0, 1)
        text += pasted[:-6]
    elif key == b'\x7f':
        text = text[:-1]
    elif key == b'\r':
        os.write(1, b'\r\nSUBMITTED[' + text + b']')
        break
    else:
        text += key
    render()
"""
    calls = []
    pipeline = SessionInteractionPipeline(
        input_text_hook=lambda texts: tuple((PromptTextEdit(0, len(text), "Hello!"),) for text in texts)
    )
    visible = pyte.Screen(120, 36)
    stream = pyte.ByteStream(visible)
    with outer_terminal() as (master, slave):

        def confirm(text):
            while select.select([master], [], [], 0)[0]:
                stream.feed(os.read(master, 65536))
            matches = visible.display[visible.cursor.y].rstrip() == f"\u203a {text}"
            if matches:
                assert not pipeline._prepared_prompts
                assert not any("SUBMITTED" in line for line in visible.display)
                calls.append(text)
            return matches

        gateway = TerminalSessionGateway(
            [sys.executable, "-I", "-c", child],
            env=dict(os.environ),
            cwd=Path.cwd(),
            pipeline=pipeline,
            runtime_identity="test-editor",
            registrations=(),
            confirm_native_prefix=confirm,
            input_fd=slave,
            output_fd=slave,
        )
        try:
            read_until(gateway, master, b"READY")
            os.write(master, b"Hello\r")
            output = read_until(gateway, master, b"SUBMITTED[Hello!]")
            assert b"SUBMITTED[Hello]" not in output
            assert calls == ["Hello", "Hello!"]
            assert list(pipeline._prepared_prompts) == [("Hello!",)]
        finally:
            gateway.close()


def test_handoff_rechecks_tmux_snapshot_without_another_child_output_event(monkeypatch):
    pipeline = SessionInteractionPipeline()
    with outer_terminal() as (master, slave):
        gateway = start_gateway(slave, pipeline)
        try:
            read_until(gateway, master, b"READY")
            snapshots = []
            waits = []

            def confirm(text):
                snapshots.append(text)
                return len(snapshots) == 2

            def relay(*, allow_input, timeout):
                assert not allow_input
                waits.append(timeout)
                return False  # No PTY event; tmux independently consumed output.

            gateway._confirm_presentation = confirm
            monkeypatch.setattr(gateway, "_relay_once", relay)
            assert gateway._confirm_prefix("Hello!")
            assert snapshots == ["Hello!", "Hello!"]
            assert len(waits) == 1 and 0 < waits[0] <= 0.01
        finally:
            gateway.close()


def test_close_waits_for_inflight_wake_before_releasing_descriptor(monkeypatch):
    pipeline = SessionInteractionPipeline()
    entered, release, close_waiting = threading.Event(), threading.Event(), threading.Event()
    with outer_terminal() as (master, slave):
        gateway = start_gateway(slave, pipeline)
        read_until(gateway, master, b"READY")
        gateway.process.terminate()
        gateway.process.wait(2)
        wake_fd = gateway._wake_write
        original_write = os.write
        original_lock = gateway._wake_lock

        class ObservedLock:
            def __enter__(self):
                if threading.current_thread().name == "gateway-close-test":
                    close_waiting.set()
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        def blocked_write(fd, data):
            if fd == wake_fd:
                entered.set()
                assert release.wait(3)
            return original_write(fd, data)

        gateway._wake_lock = ObservedLock()
        monkeypatch.setattr(os, "write", blocked_write)
        notifier = threading.Thread(target=gateway._notify_relay)
        closer = threading.Thread(target=gateway.close, name="gateway-close-test")
        notifier.start()
        try:
            assert entered.wait(1)
            closer.start()
            assert close_waiting.wait(1)
            os.fstat(wake_fd)  # Still owned until the pending write finishes.
            release.set()
            notifier.join(2)
            closer.join(2)
            assert not notifier.is_alive() and not closer.is_alive()
            with pytest.raises(OSError):
                os.fstat(wake_fd)
            gateway._notify_relay()  # Retired callbacks cannot touch a reused number.
        finally:
            release.set()
            notifier.join(3)
            if closer.ident is not None:
                closer.join(3)
            gateway.close()
            gateway.close()


def test_idle_gateway_blocks_once_until_supervisor_deadline(monkeypatch):
    pipeline = SessionInteractionPipeline()
    with outer_terminal() as (master, slave):
        gateway = start_gateway(slave, pipeline)
        try:
            read_until(gateway, master, b"READY")
            select_calls = []
            size_probes = []
            real_select = select.select
            real_ioctl = fcntl.ioctl

            def counted_select(reads, writes, errors, timeout):
                select_calls.append(timeout)
                return real_select(reads, writes, errors, timeout)

            def counted_ioctl(fd, request, argument=0, mutate_flag=True):
                if request == termios.TIOCGWINSZ:
                    size_probes.append(fd)
                return real_ioctl(fd, request, argument, mutate_flag)

            monkeypatch.setattr("rodex.terminal_gateway.select.select", counted_select)
            monkeypatch.setattr("rodex.terminal_gateway.fcntl.ioctl", counted_ioctl)
            with pytest.raises(subprocess.TimeoutExpired):
                gateway.wait(timeout=0.12)

            assert len(select_calls) == 1
            assert select_calls[0] == pytest.approx(0.12, abs=0.02)
            assert size_probes == []
        finally:
            gateway.close()


def test_supervisor_control_event_wakes_an_indefinite_idle_gateway(monkeypatch):
    pipeline = SessionInteractionPipeline()
    with outer_terminal() as (master, slave):
        gateway = start_gateway(slave, pipeline)
        publisher = None
        try:
            read_until(gateway, master, b"READY")
            relay_blocked = threading.Event()
            select_calls = []
            real_select = select.select

            def observed_select(reads, writes, errors, timeout):
                select_calls.append(timeout)
                relay_blocked.set()
                return real_select(reads, writes, errors, timeout)

            monkeypatch.setattr("rodex.terminal_gateway.select.select", observed_select)

            def wake_supervisor():
                assert relay_blocked.wait(1)
                gateway.request_supervisor_check()

            publisher = threading.Thread(target=wake_supervisor)
            publisher.start()
            assert gateway.wait() is None
            publisher.join(timeout=1)

            assert not publisher.is_alive()
            assert select_calls == [None]
        finally:
            if publisher is not None:
                publisher.join(timeout=1)
            gateway.close()


def test_presentation_change_wakes_blocked_gateway_without_polling(monkeypatch):
    pipeline = SessionInteractionPipeline()
    presentation = MutablePresentationSource()
    with outer_terminal() as (master, slave):
        gateway = start_gateway(slave, pipeline, presentation=presentation)
        publisher_errors = []
        publisher = None
        try:
            read_until(gateway, master, b"READY")
            initial_snapshot_calls = presentation.snapshot_calls
            relay_blocked = threading.Event()
            real_select = select.select

            def observed_select(reads, writes, errors, timeout):
                relay_blocked.set()
                return real_select(reads, writes, errors, timeout)

            monkeypatch.setattr("rodex.terminal_gateway.select.select", observed_select)

            def publish_and_exit():
                try:
                    if not relay_blocked.wait(1):
                        raise AssertionError("gateway did not enter its blocking relay")
                    presentation.advance()
                    if not presentation.revised_snapshot.wait(0.2):
                        raise AssertionError("presentation change did not wake the relay")
                except BaseException as error:
                    publisher_errors.append(error)
                finally:
                    os.write(master, b"\x04")

            publisher = threading.Thread(target=publish_and_exit)
            publisher.start()
            assert gateway.wait(timeout=2) == 23
            publisher.join(timeout=1)
            assert not publisher.is_alive()
            assert publisher_errors == []
            assert presentation.snapshot_calls == initial_snapshot_calls + 1
        finally:
            if publisher is not None:
                publisher.join(timeout=1)
            gateway.close()


def test_local_input_and_submission_share_pipeline_but_never_send_native_enter():
    delivered = []
    pipeline = SessionInteractionPipeline()
    pipeline.register(
        InteractionTarget(
            INPUT_MENU_TARGET,
            "runtime",
            frozenset({InteractionOperation.INTERACTIVE_INPUT, InteractionOperation.INPUT_RELEASE}),
            exists=lambda: True,
            deliver=lambda request: delivered.append(request) or InteractionResult(DeliveryStatus.DELIVERED),
        )
    )
    pipeline.register(
        InteractionTarget(
            INPUT_INTERCEPTORS[0].target,
            "runtime",
            frozenset(
                {
                    InteractionOperation.INTERACTIVE_INPUT,
                    InteractionOperation.SUBMITTED_COMMAND,
                    InteractionOperation.INPUT_RELEASE,
                }
            ),
            exists=lambda: True,
            deliver=lambda request: delivered.append(request) or InteractionResult(DeliveryStatus.DELIVERED),
        )
    )
    with outer_terminal() as (master, slave):
        gateway = start_gateway(slave, pipeline, INPUT_INTERCEPTORS)
        try:
            read_until(gateway, master, b"READY")
            os.write(master, b"/rodex argument\r")
            output = read_until(gateway, master, b"/r\x7f\x7f")
            assert b"\r" not in output and b"\n" not in output and b"odex" not in output
            assert [
                request.text for request in delivered if request.operation == InteractionOperation.SUBMITTED_COMMAND
            ] == ["/rodex argument"]
            assert all(not request.start_model_turn for request in delivered)
        finally:
            gateway.close()


def test_one_escape_goes_back_without_another_key_or_native_output():
    delivered = []
    pipeline = SessionInteractionPipeline()
    pipeline.register(
        InteractionTarget(
            INPUT_MENU_TARGET,
            "runtime",
            frozenset({InteractionOperation.INTERACTIVE_INPUT, InteractionOperation.INPUT_RELEASE}),
            exists=lambda: True,
            deliver=lambda request: delivered.append(request) or InteractionResult(DeliveryStatus.DELIVERED),
        )
    )
    with outer_terminal() as (master, slave):
        gateway = start_gateway(slave, pipeline, INPUT_INTERCEPTORS)

        def send_and_wait_for_menu_event(data):
            previous_count = len(delivered)
            os.write(master, data)
            deadline = time.monotonic() + 2
            while len(delivered) == previous_count and time.monotonic() < deadline:
                with suppress(subprocess.TimeoutExpired):
                    gateway.wait(timeout=0.04)
            assert len(delivered) > previous_count
            return delivered[-1]

        try:
            read_until(gateway, master, b"READY")
            send_and_wait_for_menu_event(b"/rod")
            request = send_and_wait_for_menu_event(b"\r")
            assert InputMenuView.deserialize(request.payload).stage == InputMenuStage.ARGUMENTS
            # No follow-up byte is sent to flush a pending Escape: the real relay
            # must expire it and return to the command menu on its own.
            request = send_and_wait_for_menu_event(b"\x1b")
            view = InputMenuView.deserialize(request.payload)
            assert view.stage == InputMenuStage.COMMANDS and view.draft == "/rod"
            request = send_and_wait_for_menu_event(b"\x1b")
            assert request.operation == InteractionOperation.INPUT_RELEASE
            assert not gateway._interceptor.active
            # Escape intentionally leaves the native /r prefix. Use text which
            # does not form a fresh configured /ro match with that prefix.
            os.write(master, b"hello")
            read_until(gateway, master, b"hello")
        finally:
            gateway.close()


def test_spawn_failure_restores_outer_terminal_and_unregisters_pipeline(monkeypatch):
    pipeline = SessionInteractionPipeline()

    def fail_spawn(*_args, **_kwargs):
        raise OSError("test spawn failure")

    monkeypatch.setattr("rodex.terminal_gateway.subprocess.Popen", fail_spawn)
    with outer_terminal() as (_master, slave), pytest.raises(OSError, match="test spawn failure"):
        start_gateway(slave, pipeline)
    assert not pipeline._targets


def test_signal_before_child_claims_terminal_never_targets_host_group(monkeypatch):
    with outer_terminal() as (_master, slave):
        gateway = start_gateway(slave, SessionInteractionPipeline())
        try:
            sent = []
            monkeypatch.setattr(os, "tcgetpgrp", lambda _fd: 0)
            monkeypatch.setattr(os, "killpg", lambda group, signum: sent.append((group, signum)))
            gateway.forward_signal(signal.SIGINT)
            assert sent == [(gateway.process.pid, signal.SIGINT)]
        finally:
            gateway.close()
