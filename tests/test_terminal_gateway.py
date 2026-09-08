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
import time
from contextlib import contextmanager, suppress

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
from rodex.terminal_gateway import TerminalSessionGateway

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


def start_gateway(slave, pipeline, registrations=()):
    return TerminalSessionGateway(
        [sys.executable, "-I", "-c", ECHO_CHILD],
        env=dict(os.environ),
        pipeline=pipeline,
        runtime_identity="test-owned-runtime",
        registrations=registrations,
        confirm_native_prefix=lambda _prefix: True,
        input_fd=slave,
        output_fd=slave,
    )


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
