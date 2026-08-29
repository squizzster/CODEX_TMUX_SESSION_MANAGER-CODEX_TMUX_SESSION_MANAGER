from __future__ import annotations

import io
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import rodex.machine_commands as machine_commands_module
from rodex.command_contract import MACHINE_COMMAND_SPECS, START_COMMAND
from rodex.control import LiveRodexControl, PromptDispatch
from rodex.machine_commands import execute_machine_command
from rodex.runtime import LiveTmuxSession

CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")


def test_round1_post_success_access_telemetry_failure_is_only_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = LiveTmuxSession(tmp_path / "tmux.sock", "worker")
    control = LiveRodexControl(
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
        CODEX_SESSION_ID,
    )
    dispatches = 0

    class Launcher:
        def session_exists(self, observed: LiveTmuxSession) -> bool:
            return observed == runtime

        def discover_runtime_control(self, observed: LiveTmuxSession) -> LiveRodexControl:
            assert observed == runtime
            return control

    class ControlClient:
        def start_turn(
            self,
            observed_control: LiveRodexControl,
            prompt: str,
            *,
            dispatch_id: str | None,
            revalidate: object,
        ) -> PromptDispatch:
            nonlocal dispatches
            assert observed_control == control
            assert prompt == "perform once\n"
            assert dispatch_id == "round1-telemetry"
            revalidate()  # type: ignore[operator]
            dispatches += 1
            return PromptDispatch(
                "started",
                "turn-1",
                "round1-telemetry",
                thread_id=str(CODEX_SESSION_ID),
                session_id=str(CODEX_SESSION_ID),
            )

    class Coordinator:
        def __init__(self, *_args: object) -> None:
            pass

        def start(
            self,
            _selector: str,
            prompt: str,
            *,
            dispatch_id: str | None,
        ) -> tuple[SimpleNamespace, PromptDispatch]:
            client = ControlClient()
            dispatch = client.start_turn(
                control,
                prompt,
                dispatch_id=dispatch_id,
                revalidate=lambda: None,
            )
            target = SimpleNamespace(
                session_id=1,
                display_name="worker",
                control=control,
            )
            return target, dispatch

    monkeypatch.setattr(
        machine_commands_module,
        "ExactTurnMutationCoordinator",
        Coordinator,
    )
    monkeypatch.setattr(
        machine_commands_module,
        "record_a_rodex_session_access",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("access telemetry unavailable")
        ),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("perform once\n"))

    status = execute_machine_command(
        [
            "_start",
            "worker",
            "--dispatch",
            "round1-telemetry",
            "--stdin",
            "--json",
        ],
        MACHINE_COMMAND_SPECS[START_COMMAND],
        tmp_path / "rodex.sqlite3",
        Launcher(),  # type: ignore[arg-type]
        ControlClient(),  # type: ignore[arg-type]
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert dispatches == 1
    assert payload["ok"] is True
    assert payload["data"]["warnings"] == [
        {
            "code": "access_record_failed",
            "message": "access telemetry unavailable",
        }
    ]
