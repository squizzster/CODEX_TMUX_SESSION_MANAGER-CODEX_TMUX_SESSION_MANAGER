"""Registration acceptance cannot turn optional analytics failure into a busy TUI."""

from __future__ import annotations

import os
import pty
import select
import sys
import time
import uuid
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from threading import Event, Thread

import pytest
from test_rodex_runtime import RUNTIME_ID, _create_socket_path, _mock_terminal_gateway

import rodex.runtime as runtime
from rodex.process_contracts import AnalyticsRuntimeConfig, RuntimeServiceConfig
from rodex.terminal_gateway import TerminalSessionGateway
from rodex.tmux_session_capability import TmuxRuntimeCapability
from rodex_registry import RodexRegistryId, RodexSessionId


@pytest.mark.parametrize("activation", ["recovers", "unavailable", "unregistered"])
def test_activation_retries_relay_bidirectional_traffic_past_registration_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, activation: str
) -> None:
    # Replace external Codex/tmux services, retaining the real runtime loop,
    # terminal gateway, PTYs, executable child and monotonic deadlines.
    _mock_terminal_gateway(monkeypatch)
    monkeypatch.setattr(runtime, "TerminalSessionGateway", TerminalSessionGateway)

    class Component:
        failure = None

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def update(self, _value):
            pass

        def activate(self, **_kwargs):
            pass

        def observe_protocol_event(self, _event):
            pass

        def close(self):
            pass

    for name in (
        "TmuxToolCallStatus",
        "TmuxContextStatus",
        "CodexProtocolEventTap",
        "CodexProtocolProxy",
        "_RuntimePathKeepalive",
        "AgentObserverCoordinator",
    ):
        monkeypatch.setattr(runtime, name, Component)
    monkeypatch.setattr(runtime.TmuxPaneController, "terminal_size", lambda _self: (80, 24))
    app_socket = tmp_path / "app.sock"
    monkeypatch.setattr(runtime, "_wait_for_app_server_socket", lambda *_: _create_socket_path(app_socket))
    monkeypatch.setattr(
        runtime,
        "_resolve_runtime_service_tmux_capability",
        lambda *_: TmuxRuntimeCapability(tmp_path / "tmux.sock", "a" * 32, "$7", "%9", RUNTIME_ID),
    )
    monkeypatch.setattr(runtime, "RODEX_REGISTRATION_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(runtime, "ANALYTICS_ACTIVATION_RETRY_SECONDS", 0.1)
    executable = tmp_path / "native-fixture"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys, tty\n"
        "if 'app-server' in sys.argv:\n"
        "    while True: signal.pause()\n"
        "tty.setraw(0)\n"
        "os.write(1, b'READY')\n"
        "while True:\n"
        "    data = os.read(0, 128)\n"
        "    os.write(1, b'ACK:' + data)\n"
    )
    executable.chmod(0o700)
    analytics = AnalyticsRuntimeConfig(
        tmux_server_id="a" * 32,
        rodex_database_path=tmp_path / "rodex.sqlite3",
        codex_sessions_root=tmp_path / "sessions",
        rodex_session_id=RodexSessionId(1),
        rodex_registry_id=RodexRegistryId(1),
        runtime_id=RUNTIME_ID,
        protocol_event_socket_path=tmp_path / "events.sock",
    )
    registration_checks = []

    def registration(*_args):
        registration_checks.append(time.monotonic())
        if activation == "unregistered":
            return None
        return replace(analytics, rodex_sessions_id=1, codex_session_id=uuid.UUID(int=1))

    monkeypatch.setattr(runtime, "_registered_analytics_runtime_config", registration)
    attempts = []

    def activate(_config):
        attempts.append(time.monotonic())
        if activation != "recovers" or len(attempts) < 3:
            raise OSError("statistics temporarily unavailable")

    config = RuntimeServiceConfig(
        workspace=tmp_path,
        codex_binary=str(executable),
        app_server_socket_path=app_socket,
        app_server_log_path=tmp_path / "app.log",
        command_policy_socket_path=tmp_path / "policy.sock",
        protocol_proxy_socket_path=tmp_path / "proxy.sock",
        protocol_event_socket_path=analytics.protocol_event_socket_path,
        tmux_binary="/usr/bin/tmux",
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_pane_target="%9",
        runtime_id=RUNTIME_ID,
        analytics=analytics,
        user_environment=(("PATH", "/usr/bin"),),
    )
    master, slave = pty.openpty()
    stop = Event()
    finished = Event()
    gateway_wake = []
    received = bytearray()
    acknowledgements = []

    def drive_terminal():
        deadline = time.monotonic() + 2
        next_message = 0.0
        try:
            while not finished.is_set() and time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.01)
                if readable:
                    received.extend(os.read(master, 4096))
                if b"READY" in received and time.monotonic() >= next_message:
                    index = len(acknowledgements)
                    if index == 0 or f"ACK:m{index - 1};".encode() in received:
                        acknowledgements.append(time.monotonic())
                        os.write(master, f"m{index};".encode())
                    next_message = time.monotonic() + 0.06
                if len(acknowledgements) >= 10 and b"ACK:m9;" in received:
                    break
        finally:
            stop.set()
            if gateway_wake:
                gateway_wake[0]()

    driver = Thread(target=drive_terminal)
    driver.start()
    started = time.monotonic()
    try:
        options = dict(
            terminal_fd=os.dup(slave),
            terminal_environment={},
            stop=stop,
            on_analytics_activated=activate,
            on_terminal_gateway_ready=lambda wake, _resize: gateway_wake.append(wake),
        )
        if activation == "unregistered":
            with pytest.raises(runtime.RodexRuntimeError, match="registration was not confirmed"):
                runtime.run_runtime_service(config, **options)
        else:
            assert runtime.run_runtime_service(config, **options) == 0
    finally:
        finished.set()
        driver.join(timeout=3)
        os.close(master)
        os.close(slave)
    assert not driver.is_alive()
    if activation == "unregistered":
        assert not attempts
        assert time.monotonic() - started < 1.5
        return
    assert len(registration_checks) == 1
    assert b"ACK:m9;" in received
    assert acknowledgements[-1] - registration_checks[0] > 0.2
    assert all(later - earlier >= 0.1 for earlier, later in pairwise(attempts))
    assert len(attempts) == 3 if activation == "recovers" else 3 <= len(attempts) <= 15
