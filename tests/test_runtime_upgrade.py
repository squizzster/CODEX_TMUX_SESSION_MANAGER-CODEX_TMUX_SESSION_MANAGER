"""Upgrade admission and exact old-reservation retirement."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import rodex.retained_runtime_handoff as handoff
from rodex.control import CodexThreadState, LiveRodexControl
from rodex.errors import RodexLaunchError
from rodex.runtime_upgrade import UPGRADE_PROTOCOL, RetainedRuntimeUpgrade, retained_coordinator_interpreter
from rodex.tmux_executor import TmuxCommandResult
from rodex.tmux_session_capability import TmuxSessionCapability
from rodex.tmux_sharing_coordinator import sharing_coordinator_hook_command
from rodex_registry import RodexRegistryId, RodexRuntimeId, RodexSessionId, parse_codex_session_id


@pytest.fixture
def selected(tmp_path):
    return TmuxSessionCapability(
        tmp_path / "tmux-v5-1111111111111111.sock",
        "a" * 32,
        "$0",
        "%0",
        RodexRuntimeId.parse("1111111111111111"),
        RodexSessionId.parse("2222222222222222"),
        RodexRegistryId.parse("3333333333333333"),
        1,
        parse_codex_session_id("01a00654-f2bc-7a30-834a-a5f886a65f82"),
    )


@pytest.fixture
def retained(tmp_path):
    root = tmp_path / ("b" * 64)
    root.mkdir(mode=0o700)
    interpreter = root / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("fixture interpreter")
    (root / "rodex-installation.json").write_text(
        json.dumps({"schema": "rodex-installation-v1", "implementation": root.name})
    )
    return interpreter


def hook(interpreter, selected):
    return sharing_coordinator_hook_command(
        str(interpreter), "/usr/bin/tmux", selected.tmux_server_socket_path, selected.tmux_server_id
    )


def test_coordinator_parser_retains_the_environment_path(selected, retained):
    assert (
        retained_coordinator_interpreter(hook(retained, selected), tmux_binary="/usr/bin/tmux", capability=selected)
        == retained
    )


@pytest.mark.parametrize("change", ["shell", "server", "mutable", "manifest", "permissions", "missing"])
def test_untrusted_or_unavailable_owner_cannot_authorize_upgrade(selected, retained, change):
    command = hook(retained, selected)
    root = retained.parents[2]
    if change == "shell":
        command += "; echo bad"
    elif change == "server":
        command = hook(retained, replace(selected, tmux_server_id="c" * 32))
    elif change == "mutable":
        command = hook(Path("/editable/.venv/bin/python"), selected)
    elif change == "manifest":
        (root / "rodex-installation.json").write_text("{}")
    elif change == "permissions":
        root.chmod(0o777)
    else:
        retained.unlink()
    with pytest.raises(RodexLaunchError, match="cannot upgrade this session"):
        retained_coordinator_interpreter(command, tmux_binary="/usr/bin/tmux", capability=selected)


def test_upgrade_helper_receipt_must_be_terminal_and_match_the_selected_runtime(selected, retained, tmp_path):
    for changed in ({"state": "stopping"}, {"runtime_id": "0" * 16}, {"implementation": "c" * 64}, {"cwd": "relative"}):
        reply = {
            "protocol": UPGRADE_PROTOCOL,
            "implementation": retained.parents[2].name,
            "runtime_id": str(selected.runtime_id),
            "state": "terminal",
            "cwd": str(tmp_path),
            **changed,
        }
        upgrade = RetainedRuntimeUpgrade(
            retained,
            selected,
            "worker",
            hook(retained, selected),
            "codex",
            "/usr/bin/tmux",
            {},
            lambda command, reply=reply, **_kw: subprocess.CompletedProcess(command, 0, json.dumps(reply), ""),
        )
        with pytest.raises(RodexLaunchError, match="invalid receipt"):
            upgrade.stop(tmp_path / "catalog.sqlite3")


@pytest.fixture
def admitted_handoff(tmp_path, monkeypatch, selected, retained):
    capability = {
        field.name: selected.internal_session_id
        if field.name == "internal_session_id"
        else str(getattr(selected, field.name))
        for field in fields(selected)
    }
    request = {
        "protocol": UPGRADE_PROTOCOL,
        "implementation": handoff.RODEX_IMPLEMENTATION_SHA256,
        "capability": capability,
        "tmux_session_name": "worker",
        "coordinator_command": hook(retained, selected),
        "database_path": str(tmp_path / "catalog.sqlite3"),
        "codex_binary": "codex",
        "tmux_binary": "/usr/bin/tmux",
    }
    interpreter = tmp_path / handoff.RODEX_IMPLEMENTATION_SHA256 / ".venv/bin/python"
    monkeypatch.setattr(handoff, "retained_installation_interpreter", lambda: interpreter)
    control = LiveRodexControl(
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
        selected.codex_session_id,
        selected.rodex_session_id,
        selected.registry_id,
        "registered",
        selected.runtime_id,
        selected,
    )
    state = CodexThreadState(
        str(selected.codex_session_id), str(selected.codex_session_id), str(tmp_path), "idle", (), None, True
    )
    observed = SimpleNamespace(
        states=[state, state],
        stops=[],
        ownership="1",
        persisted=True,
        receipt_operation="1" * 32,
        native_operation="1" * 32,
        live=True,
        stop_states=["stopping", "terminal"],
        reads=0,
        attached=[],
        attach_outcome="detach",
        coordinator=request["coordinator_command"],
    )

    class Launcher:
        def __init__(self, *_args):
            pass

        def discover_runtime_control(self, _runtime):
            return control

        def _read_tmux_server_option(self, *_args, **_kwargs):
            return observed.coordinator

        def _tmux(self, *_args):
            return TmuxCommandResult(0, observed.ownership)

        def codex_session_is_persisted(self, _identity):
            return observed.persisted

        def session_exists(self, _runtime):
            return observed.live

        def attach(self, runtime):
            observed.attached.append(runtime)
            return observed.attach_outcome

    class Client:
        def inspect_live(self, _control):
            observed.reads += 1
            return observed.states.pop(0)

    class Receipts:
        def __init__(self, _root):
            pass

        def _path(self, kind, _runtime_id):
            return kind

        def _read(self, kind):
            return dict(
                kind=kind,
                runtime_id=str(selected.runtime_id),
                operation_id=observed.receipt_operation if kind == "app-server" else observed.native_operation,
                pid=123,
                uid=os.getuid(),
                process_group_id=123,
                start_time_ticks=456,
            )

    class Daemon:
        def __init__(self, *_args):
            pass

        def stop(self, operation, runtime_id):
            observed.stops.append((operation, runtime_id))
            state = observed.stop_states.pop(0)
            if state == "terminal":
                observed.live = False
            return state

    monkeypatch.setattr(handoff, "RodexRuntimeLauncher", Launcher)
    monkeypatch.setattr(handoff, "CodexControlClient", Client)
    monkeypatch.setattr(handoff, "RuntimeProcessReceipts", Receipts)
    monkeypatch.setattr(handoff, "RodexDaemonClient", Daemon)
    monkeypatch.setattr(handoff, "lookup_rodex_registry_id", lambda _path: selected.registry_id)
    monkeypatch.setattr(handoff, "lookup_rodex_session_names", lambda *_args: SimpleNamespace(display_name="worker"))
    monkeypatch.setattr(
        handoff,
        "lookup_rodex_runtime_registration",
        lambda *_args: SimpleNamespace(
            runtime_id=selected.runtime_id,
            codex_session_id=selected.codex_session_id,
            tmux_session=SimpleNamespace(
                tmux_server_socket_path=str(selected.tmux_server_socket_path), tmux_session_name="worker"
            ),
        ),
    )
    monkeypatch.setattr(handoff, "require_durable_runtime_instance", lambda *_args: None)
    monkeypatch.setattr(
        handoff, "_read_process_state", lambda _pid: dict(uid=os.getuid(), process_group_id=123, start_time_ticks=456)
    )
    monkeypatch.setattr(handoff.time, "sleep", lambda _seconds: None)
    return request, observed, state


def test_idle_handoff_waits_for_exact_terminal_shutdown(admitted_handoff):
    request, observed, state = admitted_handoff
    result = handoff.stop_selected_idle_runtime(request)
    assert result["state"] == "terminal"
    assert result["cwd"] == state.cwd
    assert observed.reads == 2
    assert observed.stops == [("1" * 32, request["capability"]["runtime_id"])] * 2


@pytest.mark.parametrize("which_check", [0, 1])
def test_busy_turn_is_refused_without_stopping(admitted_handoff, which_check):
    request, observed, state = admitted_handoff
    observed.states[which_check] = replace(state, status="active", active_turn_id="turn-1")
    with pytest.raises(RuntimeError) as raised:
        handoff.stop_selected_idle_runtime(request)
    assert str(raised.value) == (
        "Session 'worker' is still working on an older Rodex version.\n"
        "Upgrading would interrupt its work. Wait until it is idle, then run: rodex worker\n"
        "Reconnect without upgrading: rodex worker --force-old"
    )
    assert observed.stops == []


@pytest.mark.parametrize("outcome", ["detach", "exited"])
def test_retained_attachment_keeps_busy_runtime_unchanged(admitted_handoff, outcome):
    request, observed, state = admitted_handoff
    observed.states = [replace(state, status="active", active_turn_id="multi-hour-turn")]
    observed.attach_outcome = outcome
    # Attachment needs neither saved history nor authority to destroy the server.
    observed.persisted = False
    observed.ownership = "0"
    assert handoff.attach_selected_runtime(request) == outcome
    assert len(observed.attached) == 1
    assert str(observed.attached[0].runtime_id) == request["capability"]["runtime_id"]
    assert observed.reads == 0
    assert observed.stops == []
    assert observed.live


@pytest.mark.parametrize("change", ["catalog", "incarnation", "coordinator", "implementation"])
def test_retained_attachment_rejects_changed_ownership_without_touching_work(admitted_handoff, monkeypatch, change):
    request, observed, _state = admitted_handoff
    if change == "catalog":
        monkeypatch.setattr(handoff, "lookup_rodex_registry_id", lambda _path: None)
    elif change == "incarnation":
        monkeypatch.setattr(handoff, "lookup_rodex_runtime_registration", lambda *_args: None)
    elif change == "coordinator":
        request["coordinator_command"] = "changed"
    else:
        request["implementation"] = "c" * 64
    with pytest.raises(RuntimeError):
        handoff.attach_selected_runtime(request)
    assert observed.attached == observed.stops == []


@pytest.mark.parametrize("code, outcome", [(0, "detach"), (2, "exited"), (1, None)])
def test_interactive_retained_helper_inherits_tty_and_has_no_upgrade_timeout(selected, retained, tmp_path, code, outcome):
    calls = []

    def runner(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, code)

    upgrade = RetainedRuntimeUpgrade(
        retained, selected, "worker", hook(retained, selected), "codex", "/usr/bin/tmux", {"TMUX": "outer"}, runner
    )
    if outcome is None:
        with pytest.raises(RodexLaunchError, match="runtime was left running"):
            upgrade.attach(tmp_path / "catalog.sqlite3")
    else:
        assert upgrade.attach(tmp_path / "catalog.sqlite3") == outcome
    command, options = calls[0]
    assert command[:2] == [str(retained), "-I"]
    assert command[3] == "--attach"
    assert json.loads(command[4])["capability"]["runtime_id"] == str(selected.runtime_id)
    assert options == {"check": False, "env": {}}


@pytest.mark.parametrize("reason", ["history", "foreign-pane", "receipt", "process", "incarnation"])
def test_unproven_upgrade_is_refused_without_stopping(admitted_handoff, monkeypatch, reason):
    request, observed, _state = admitted_handoff
    if reason == "history":
        observed.persisted = False
    elif reason == "foreign-pane":
        observed.ownership = "0"
    elif reason == "receipt":
        observed.native_operation = "2" * 32
    elif reason == "process":
        monkeypatch.setattr(
            handoff, "_read_process_state", lambda _pid: dict(uid=os.getuid(), process_group_id=123, start_time_ticks=789)
        )
    else:
        monkeypatch.setattr(handoff, "lookup_rodex_runtime_registration", lambda *_args: None)
    with pytest.raises(RuntimeError):
        handoff.stop_selected_idle_runtime(request)
    assert observed.stops == []
