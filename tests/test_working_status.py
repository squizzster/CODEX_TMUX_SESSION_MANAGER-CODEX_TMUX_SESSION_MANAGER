from __future__ import annotations

import fcntl
import os
import re
import select
import shutil
import struct
import subprocess
import termios
import time
from dataclasses import replace
from pathlib import Path

import pyte
import pytest

from rodex.protocol_proxy import CodexWorkingStatusObserver
from rodex.status_bar import RODEX_STATUS_LEFT_FORMAT, RODEX_WORKING_STATUS_FORMAT
from rodex.tmux_session_capability import (
    RODEX_SHARED_TMUX_PROTOCOL,
    TmuxRuntimeCapability,
)
from rodex.tmux_status import TmuxWorkingStatus
from rodex_registry import RodexRuntimeId


def thread_started(thread_id: str = "root", *, status: str = "idle") -> dict[str, object]:
    return {"method": "thread/started", "params": {"thread": {"id": thread_id, "status": {"type": status}}}}


def turn_event(
    method: str,
    turn_id: str = "turn-1",
    *,
    thread_id: str = "root",
    status: str = "inProgress",
) -> dict[str, object]:
    return {"method": method, "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": status}}}


def thread_status(status: str, *, thread_id: str = "root") -> dict[str, object]:
    return {"method": "thread/status/changed", "params": {"threadId": thread_id, "status": {"type": status}}}


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "interrupted"])
def test_working_projects_only_transitions_and_clears_for_every_terminal_turn(terminal_status: str) -> None:
    observed = []
    observer = CodexWorkingStatusObserver(observed.append)
    observer.observe_protocol_event(thread_started())
    observer.observe_protocol_event(turn_event("turn/started"))
    observer.observe_protocol_event(turn_event("turn/started"))
    observer.observe_protocol_event(thread_status("active"))
    for _ in range(100):
        observer.observe_protocol_event({"method": "item/agentMessage/delta", "params": {"threadId": "root"}})
    observer.observe_protocol_event(turn_event("turn/completed", status=terminal_status))
    observer.observe_protocol_event(thread_status("idle"))
    observer.close()

    assert observed == [True, False]


def test_child_starts_and_completions_cannot_replace_or_clear_the_root_activity() -> None:
    observed = []
    observer = CodexWorkingStatusObserver(observed.append)
    observer.observe_protocol_event(thread_started())
    observer.observe_protocol_event(thread_started("child", status="active"))
    observer.observe_protocol_event(turn_event("turn/started", thread_id="child"))
    assert observed == []
    observer.observe_protocol_event(turn_event("turn/started"))
    observer.observe_protocol_event(turn_event("turn/completed", thread_id="child"))
    observer.observe_protocol_event(thread_status("idle", thread_id="child"))
    assert observed == [True]
    observer.close()
    assert observed == [True, False]


def test_stale_completion_cannot_clear_a_newer_root_turn() -> None:
    observed = []
    observer = CodexWorkingStatusObserver(observed.append)
    observer.bind_root_thread("root")
    observer.observe_protocol_event(turn_event("turn/started", "older"))
    observer.observe_protocol_event(turn_event("turn/started", "newer"))
    observer.observe_protocol_event(turn_event("turn/completed", "older"))
    assert observed == [True]
    observer.observe_protocol_event(turn_event("turn/completed", "newer"))
    assert observed == [True, False]


@pytest.mark.parametrize("terminal_status", ["idle", "systemError", "notLoaded"])
def test_thread_state_restores_resumed_activity_and_clears_missing_completion(terminal_status: str) -> None:
    observed = []
    observer = CodexWorkingStatusObserver(observed.append)
    observer.observe_protocol_event(thread_started(status="active"))
    observer.observe_protocol_event(thread_status(terminal_status))
    assert observed == [True, False]


def test_disconnect_and_close_clear_activity_without_rebinding_to_a_child() -> None:
    observed = []
    observer = CodexWorkingStatusObserver(observed.append)
    observer.bind_root_thread("root")
    observer.observe_protocol_event(turn_event("turn/started"))
    observer.reset_after_disconnect()
    observer.reset_after_disconnect()
    observer.observe_protocol_event(thread_started("child", status="active"))
    observer.observe_protocol_event(thread_status("active"))
    observer.close()
    observer.close()
    observer.observe_protocol_event(turn_event("turn/started"))
    assert observed == [True, False, True, False]


def test_registered_root_replaces_a_provisional_root_and_keeps_same_root_activity() -> None:
    observed = []
    observer = CodexWorkingStatusObserver(observed.append)
    observer.observe_protocol_event(thread_started("provisional", status="active"))
    observer.bind_root_thread("root")
    observer.observe_protocol_event(turn_event("turn/started"))
    observer.bind_root_thread("root")
    assert observed == [True, False, True]
    observer.close()
    assert observed == [True, False, True, False]


def test_new_gateway_reconciliation_publishes_current_activity_without_resetting_it() -> None:
    observed = []
    observer = CodexWorkingStatusObserver(observed.append)
    observer.bind_root_thread("root")
    observer.publish_current_state()
    observer.observe_protocol_event(turn_event("turn/started"))
    observer.publish_current_state()
    observer.close()
    observer.publish_current_state()
    assert observed == [False, True, True, False]


def test_malformed_events_cannot_start_or_stop_the_indicator() -> None:
    observed = []
    observer = CodexWorkingStatusObserver(observed.append)
    observer.bind_root_thread("root")
    for event in (
        None,
        {},
        {"method": []},
        {"method": "turn/started", "params": []},
        {"method": "turn/started", "params": {"threadId": "root", "turn": {"id": ""}}},
        {"method": "thread/status/changed", "params": {"status": {"type": "active"}}},
        thread_status("unknown"),
    ):
        observer.observe_protocol_event(event)
    assert observed == []


def test_status_callback_failure_does_not_interrupt_protocol_observation() -> None:
    def unavailable(_working: bool) -> None:
        raise OSError("tmux unavailable")

    observer = CodexWorkingStatusObserver(unavailable)
    observer.bind_root_thread("root")
    observer.observe_protocol_event(turn_event("turn/started"))
    observer.close()


def test_real_tmux_advances_fixed_width_dots_without_rodex_frame_publications(tmp_path: Path) -> None:
    tmux_binary = shutil.which("tmux")
    if tmux_binary is None:
        pytest.skip("real tmux required for working status rendering")
    socket_path = tmp_path / "tmux.sock"
    runtime_id = RodexRuntimeId.parse("0123456789abcdef")
    server_id = "0123456789abcdef0123456789abcdef"

    def tmux(*arguments: str) -> str:
        return subprocess.run(
            [tmux_binary, "-S", str(socket_path), *arguments],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.rstrip("\n")

    session_id, pane_id = tmux(
        "-f",
        "/dev/null",
        "new-session",
        "-d",
        "-P",
        "-F",
        "#{session_id}|#{pane_id}",
        "-x",
        "180",
        "-y",
        "24",
        "-s",
        "working",
        "sleep 30",
    ).split("|")
    capability = TmuxRuntimeCapability(socket_path, server_id, session_id, pane_id, runtime_id)
    publications = []

    def runner(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        publications.append(command)
        return subprocess.run(command, **options)

    working_status = TmuxWorkingStatus(tmux_binary, capability, pane_id, runner=runner)
    observer = CodexWorkingStatusObserver(working_status.update)
    observer.bind_root_thread("root")
    client = None
    master, slave = os.openpty()
    try:
        for option, value in (
            ("@rodex_shared_tmux_protocol", RODEX_SHARED_TMUX_PROTOCOL),
            ("@rodex_shared_tmux_server_id", server_id),
            ("@rodex_server_runtime_id", str(runtime_id)),
        ):
            tmux("set-option", "-s", option, value)
        tmux("set-option", "-p", "-t", pane_id, "@rodex_pane_runtime_id", str(runtime_id))
        for option, value in (
            ("@rodex_runtime_id", str(runtime_id)),
            ("@rodex_primary_pane_id", pane_id),
            ("status-position", "top"),
            ("status-left-length", "160"),
            ("status-right", ""),
            ("window-status-format", ""),
            ("window-status-current-format", ""),
            ("status-left", RODEX_STATUS_LEFT_FORMAT),
        ):
            tmux("set-option", "-t", pane_id, option, value)
        observer.observe_protocol_event(turn_event("turn/started"))
        # Wait for the asynchronous transition before reading rendered status.
        deadline = time.monotonic() + 2
        while tmux("display-message", "-p", "-t", pane_id, "#{@rodex_working}") != "1":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        frames = [
            tmux("display-message", "-p", "-t", pane_id, RODEX_WORKING_STATUS_FORMAT.replace("%S", str(second)))
            for second in range(8, 12)
        ]
        assert [re.sub(r" +", " ", frame.strip()) for frame in frames] == [
            f"| Working{'.' * count} |" for count in range(1, 5)
        ]
        assert len({len(frame) for frame in frames}) == 1
        assert tmux("show-option", "-v", "-t", pane_id, "status-interval") == "1"

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 180, 0, 0))
        environment = {key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}
        environment["TERM"] = "xterm-256color"
        client = subprocess.Popen(
            [tmux_binary, "-S", str(socket_path), "attach-session", "-t", session_id],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=environment,
        )
        screen = pyte.Screen(180, 24)
        stream = pyte.Stream(screen)
        seen = set()
        deadline = time.monotonic() + 6
        while len(seen) < 4 and time.monotonic() < deadline:
            assert client.poll() is None, "tmux client exited before status rendering"
            if select.select([master], [], [], 0.2)[0]:
                stream.feed(os.read(master, 65536).decode())
                match = re.search(r"Working(\.{1,4}) ", screen.display[0])
                if match:
                    seen.add(match.group(1))
        assert seen == {".", "..", "...", "...."}, screen.display[0]
        assert len(publications) == 1

        observer.observe_protocol_event(turn_event("turn/completed", status="completed"))
        deadline = time.monotonic() + 2
        while "Working" in screen.display[0] and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                stream.feed(os.read(master, 65536).decode())
        assert "Working" not in screen.display[0]
        assert tmux("show-option", "-v", "-t", pane_id, "@rodex_working") == "0"
        assert tmux("show-option", "-v", "-t", pane_id, "status-interval") == "15"
        assert len(publications) == 2
        assert tmux("display-message", "-p", "-t", pane_id, RODEX_WORKING_STATUS_FORMAT) == ""

        # A superseded owner cannot change the option or redraw cadence.
        stale_status = TmuxWorkingStatus(tmux_binary, replace(capability, tmux_server_id="a" * 32), pane_id)
        stale_status.update(True)
        with stale_status._condition:
            worker = stale_status._worker
        if worker is not None:
            worker.join(2)
            assert not worker.is_alive()
        assert tmux("show-option", "-v", "-t", pane_id, "@rodex_working") == "0"
        assert tmux("show-option", "-v", "-t", pane_id, "status-interval") == "15"
    finally:
        observer.close()
        working_status.close()
        if client is not None:
            client.terminate()
            client.wait(timeout=5)
        os.close(master)
        os.close(slave)
        subprocess.run([tmux_binary, "-S", str(socket_path), "kill-server"], capture_output=True, timeout=5)
