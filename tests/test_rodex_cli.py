from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from typing import Any

import pytest

from rodex.app_server_contract import RodexAppServerCompatibilityError
from rodex.cli import RodexExecutableNotFoundError, RodexLaunchError, main, run
from rodex.control import (
    CodexThreadState,
    CodexTurnResult,
    LiveRodexControl,
    PromptDispatch,
    RodexControlError,
    RodexDispatchIndeterminateError,
    RodexWaitTimeoutError,
)
from rodex.runtime import (
    CurrentTmuxPaneContext,
    LiveRodexRuntime,
    LiveTmuxSession,
    RodexCodexSessionNotFoundError,
    RodexRuntimeError,
)
from rodex_registry import (
    RodexRegistryId,
    RodexSessionError,
    RodexSessionId,
    RodexSessionsUserIdentity,
    assign_a_user_defined_cool_name,
    create_a_rodex_session,
    lookup_codex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_registry_id,
    lookup_rodex_runtime_instance,
    lookup_rodex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_session_names,
    lookup_rodex_sessions_id_from_a_codex_session_id,
    lookup_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_tmux_session,
    open_a_user_defined_cool_name_assignment,
)
from rodex_sql import RodexSQLError

CODEX_SESSION_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
REPLACEMENT_CODEX_SESSION_ID = uuid.UUID(int=CODEX_SESSION_ID.int + 1)
DNA = RodexSessionsUserIdentity(1009, 1010, "dna")
RUNTIME_IDENTIFIER = uuid.UUID("0c01ee2e-ad72-40e1-b337-7202e099c2fe")


class StubLauncher:
    def __init__(self, tmp_path: Path) -> None:
        self.runtime = LiveRodexRuntime(
            tmux_server_socket_path=tmp_path / "tmux.sock",
            tmux_session_name="rodex-example",
            app_server_socket_path=tmp_path / "app.sock",
            app_server_log_path=tmp_path / "app.log",
            protocol_proxy_socket_path=tmp_path / "proxy.sock",
            protocol_event_socket_path=tmp_path / "events.sock",
            runtime_identifier=RUNTIME_IDENTIFIER,
        )
        self.started: list[tuple[Path, list[str]]] = []
        self.analytics_identities: list[tuple[RodexSessionId | None, Path | None]] = []
        self.registry_identities: list[RodexRegistryId | None] = []
        self.renamed: list[tuple[LiveTmuxSession, str]] = []
        self.configured: list[LiveTmuxSession] = []
        self.attached: list[LiveTmuxSession] = []
        self.stopped: list[tuple[LiveTmuxSession, bool]] = []
        self.existing_checks: list[LiveTmuxSession] = []
        self.live = True
        self.observed_codex_session_id = CODEX_SESSION_ID
        self.start_error: RodexRuntimeError | None = None
        self.start_errors: list[RodexRuntimeError] = []
        self.control = LiveRodexControl(
            tmp_path / "proxy.sock",
            tmp_path / "events.sock",
            CODEX_SESSION_ID,
            runtime_identifier=RUNTIME_IDENTIFIER,
        )
        self.control_discoveries: list[LiveTmuxSession] = []
        self.confirmed: list[LiveTmuxSession] = []
        self.session_names: tuple[str, ...] = ()
        self.mouse_state = "off"
        self.current_tmux_session = LiveTmuxSession(
            tmp_path / "tmux.sock", "automatic-beluga"
        )
        self.attached_client_count = 1
        self.tmp_path = tmp_path

    def start(
        self,
        workspace: Path,
        arguments: list[str],
        *,
        rodex_session_id: RodexSessionId | None = None,
        rodex_registry_id: RodexRegistryId | None = None,
        rodex_database_path: Path | None = None,
    ) -> tuple[LiveRodexRuntime, uuid.UUID]:
        self.started.append((workspace, arguments))
        self.analytics_identities.append((rodex_session_id, rodex_database_path))
        self.registry_identities.append(rodex_registry_id)
        if self.start_errors:
            raise self.start_errors.pop(0)
        if self.start_error is not None:
            error = self.start_error
            self.start_error = None
            raise error
        return self.runtime, self.observed_codex_session_id

    def session_exists(self, runtime: LiveTmuxSession) -> bool:
        self.existing_checks.append(runtime)
        return self.live

    def rename(self, runtime: LiveTmuxSession, tmux_session_name: str) -> LiveTmuxSession:
        self.renamed.append((runtime, tmux_session_name))
        return replace(runtime, tmux_session_name=tmux_session_name)

    def configure_identity_status(self, runtime: LiveTmuxSession) -> None:
        self.configured.append(runtime)

    def attach(self, runtime: LiveTmuxSession) -> None:
        self.attached.append(runtime)

    def stop(self, runtime: LiveTmuxSession, *, check: bool = True) -> None:
        self.stopped.append((runtime, check))

    def discover_runtime_control(self, runtime: LiveTmuxSession) -> LiveRodexControl:
        self.control_discoveries.append(runtime)
        if (
            self.control.rodex_session_id is not None
            and self.control.rodex_registry_id is not None
            and self.control.registration_state is not None
        ):
            return self.control
        for database in self.tmp_path.rglob("*.sqlite3"):
            try:
                session_id = lookup_rodex_sessions_id_from_a_codex_session_id(
                    self.control.codex_session_id, database
                )
                if session_id is None:
                    continue
                rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(
                    session_id, database
                )
            except (sqlite3.Error, ValueError):
                continue
            if rodex_session_id is not None:
                self.control = replace(
                    self.control,
                    rodex_session_id=(
                        rodex_session_id
                        if self.control.rodex_session_id is None
                        else self.control.rodex_session_id
                    ),
                    rodex_registry_id=lookup_rodex_registry_id(database),
                    registration_state=(
                        "registered"
                        if self.control.registration_state is None
                        else self.control.registration_state
                    ),
                )
                return self.control
        return self.control

    def confirm_runtime_registration(self, runtime: LiveTmuxSession) -> None:
        self.confirmed.append(runtime)
        if self.control.registration_state == "pending":
            self.control = replace(self.control, registration_state="registered")

    def list_session_names(self, _socket_path: Path) -> tuple[str, ...]:
        return self.session_names

    def discover_current_tmux_pane_context(self) -> CurrentTmuxPaneContext:
        return CurrentTmuxPaneContext(
            tmux_session=self.current_tmux_session,
            tmux_session_id="$0",
            tmux_window_id="@0",
            tmux_pane_id="%4",
            attached_client_count=self.attached_client_count,
        )

    def set_mouse_mode(self, _runtime: LiveTmuxSession, mode: str) -> str:
        if mode == "toggle":
            self.mouse_state = "off" if self.mouse_state == "on" else "on"
        elif mode in {"on", "off"}:
            self.mouse_state = mode
        return self.mouse_state


class StubControlClient:
    def __init__(self) -> None:
        self.sent: list[tuple[LiveRodexControl, str]] = []
        self.waited: list[LiveRodexControl] = []
        self.tailed: list[LiveRodexControl] = []
        self.started: list[tuple[LiveRodexControl, str]] = []
        self.steered: list[tuple[LiveRodexControl, str, str]] = []
        self.interrupted: list[tuple[LiveRodexControl, str]] = []
        self.exact_waited: list[tuple[LiveRodexControl, str, float | None]] = []
        self.send_error: RodexControlError | None = None
        self.start_error: RodexControlError | None = None
        self.wait_error: RodexControlError | None = None
        self.compatibility_error: RodexAppServerCompatibilityError | None = None
        self.state = CodexThreadState(
            thread_id=str(CODEX_SESSION_ID),
            session_id=str(CODEX_SESSION_ID),
            status="idle",
            active_flags=(),
            active_turn_id=None,
            can_accept_direct_input=True,
        )
        self.turn_result = CodexTurnResult(
            turn_id="turn-1",
            status="completed",
            final_agent_message="done",
            structured_output=None,
            error=None,
            started_at=10,
            completed_at=12,
            duration_ms=2000,
            changed_paths=(),
            changed_paths_truncated=False,
        )

    def inspect(self, _control: LiveRodexControl) -> CodexThreadState:
        return self.state

    def inspect_live(self, _control: LiveRodexControl) -> CodexThreadState:
        return self.state

    def exact_control_version(self, _control: LiveRodexControl) -> str:
        if self.compatibility_error is not None:
            raise self.compatibility_error
        return "0.147.0"

    def send_prompt(
        self,
        control: LiveRodexControl,
        prompt: str,
        *,
        revalidate: Any,
    ) -> PromptDispatch:
        revalidate()
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((control, prompt))
        return PromptDispatch("started", "turn-1")

    def wait_until_idle(self, control: LiveRodexControl, *, revalidate: Any) -> None:
        revalidate()
        self.waited.append(control)

    def start_turn(
        self, control: LiveRodexControl, prompt: str, *, revalidate: Any
    ) -> PromptDispatch:
        revalidate()
        if self.start_error is not None:
            raise self.start_error
        self.started.append((control, prompt))
        return PromptDispatch(
            "started", "turn-1", str(CODEX_SESSION_ID), str(CODEX_SESSION_ID)
        )

    def steer_turn(
        self,
        control: LiveRodexControl,
        turn_id: str,
        prompt: str,
        *,
        revalidate: Any,
    ) -> PromptDispatch:
        revalidate()
        self.steered.append((control, turn_id, prompt))
        return PromptDispatch(
            "steered", turn_id, str(CODEX_SESSION_ID), str(CODEX_SESSION_ID)
        )

    def interrupt_turn(
        self, control: LiveRodexControl, turn_id: str, *, revalidate: Any
    ) -> CodexThreadState:
        revalidate()
        self.interrupted.append((control, turn_id))
        return replace(self.state, status="active", active_turn_id=turn_id)

    def result(
        self, _control: LiveRodexControl, _turn_id: str, *, revalidate: Any
    ) -> tuple[CodexThreadState, CodexTurnResult]:
        revalidate()
        return self.state, self.turn_result

    def wait_for_turn(
        self,
        control: LiveRodexControl,
        turn_id: str,
        *,
        timeout_seconds: float | None,
        revalidate: Any,
    ) -> tuple[CodexThreadState, CodexTurnResult]:
        revalidate()
        if self.wait_error is not None:
            raise self.wait_error
        self.exact_waited.append((control, turn_id, timeout_seconds))
        return self.state, self.turn_result

    def tail(
        self,
        control: LiveRodexControl,
        write_event: Any,
        *,
        revalidate: Any,
    ) -> None:
        revalidate()
        self.tailed.append(control)
        write_event('{"method":"turn/started"}')


class RecordingCodexDelegator:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, codex_binary: str, arguments: list[str]) -> int:
        self.calls.append((codex_binary, arguments))
        return self.returncode


def available_prerequisite(command: str) -> str:
    return f"/usr/bin/{command}"


def create_controlled_session(database: Path, tmp_path: Path) -> None:
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="automatic-beluga",
    )


def create_exact_controlled_session(database: Path, tmp_path: Path) -> None:
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="automatic-beluga",
        runtime_identifier=RUNTIME_IDENTIFIER,
    )


def test_help_prints_rodex_commands_without_codex_tmux_or_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: pytest.fail(f"unexpected prerequisite lookup: {command}"),
    )

    assert run(["_help"], database_path=database, codex_delegator=delegator) == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith("usage: rodex [COMMAND [ARGUMENTS]]\n")
    assert "_help" in output.out
    assert "_create" in output.out
    assert "_running" in output.out
    assert "_context" in output.out
    assert "Every other invocation is passed unchanged to Codex." in output.out
    assert delegator.calls == []
    assert not database.exists()


def test_help_rejects_arguments_without_checking_prerequisites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: pytest.fail(f"unexpected prerequisite lookup: {command}"),
    )

    with pytest.raises(RodexLaunchError, match=r"^usage: rodex _help$"):
        run(["_help", "unexpected"], database_path=tmp_path / "rodex.sqlite3")


@pytest.mark.parametrize("arguments", [["_context"], ["_context", "--json"]])
def test_context_reports_the_verified_current_rodex_session_as_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    launcher.attached_client_count = 2

    assert run(arguments, database_path=database, launcher=launcher) == 0  # type: ignore[arg-type]

    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(1, database)
    assert rodex_session_id is not None
    assert json.loads(capsys.readouterr().out) == {
        "managed_by": "rodex",
        "rodex_session_name": "automatic-beluga",
        "rodex_permanent_name": "automatic-beluga",
        "rodex_user_defined_name": None,
        "rodex_session_id": str(rodex_session_id),
        "rodex_registry_id": str(lookup_rodex_registry_id(database)),
        "rodex_database_path": str(database),
        "codex_session_id": str(CODEX_SESSION_ID),
        "tmux_server_socket_path": str(tmp_path / "tmux.sock"),
        "tmux_session_name": "automatic-beluga",
        "tmux_session_id": "$0",
        "tmux_window_id": "@0",
        "tmux_pane_id": "%4",
        "registration_state": "registered",
        "runtime_identifier": str(RUNTIME_IDENTIFIER),
        "runtime_identity_persisted": False,
        "attached_clients": 2,
        "shared": True,
    }
    assert launcher.control_discoveries == [
        launcher.current_tmux_session,
        launcher.current_tmux_session,
    ]


def test_context_rejects_a_different_registry_before_printing_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(1, database)
    assert rodex_session_id is not None
    launcher = StubLauncher(tmp_path)
    launcher.control = replace(
        launcher.control,
        rodex_session_id=rodex_session_id,
        rodex_registry_id=RodexRegistryId(lookup_rodex_registry_id(database).value ^ 1),
        registration_state="registered",
    )

    with pytest.raises(RodexLaunchError, match="different Rodex registry"):
        run(["_context"], database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert capsys.readouterr().out == ""


def test_context_distinguishes_permanent_and_user_defined_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    create_controlled_session(database, tmp_path)
    assign_a_user_defined_cool_name(
        "automatic-beluga",
        "work",
        database,
        user_identity=DNA,
        renamed_tmux_session_name="work",
    )
    launcher = StubLauncher(tmp_path)
    launcher.current_tmux_session = replace(
        launcher.current_tmux_session,
        tmux_session_name="work",
    )

    assert run(["_context"], database_path=database, launcher=launcher) == 0  # type: ignore[arg-type]

    context = json.loads(capsys.readouterr().out)
    assert context["rodex_session_name"] == "work"
    assert context["rodex_permanent_name"] == "automatic-beluga"
    assert context["rodex_user_defined_name"] == "work"


def test_context_rejects_a_live_name_not_recorded_for_its_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    launcher.current_tmux_session = replace(
        launcher.current_tmux_session,
        tmux_session_name="externally-renamed",
    )

    with pytest.raises(RodexLaunchError, match="does not match its recorded"):
        run(["_context"], database_path=database, launcher=launcher)  # type: ignore[arg-type]


def test_context_rejects_a_registered_session_owned_by_another_posix_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "rodex_registry.lifecycle.current_rodex_sessions_user_identity", lambda: DNA
    )
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=RodexSessionsUserIdentity(2001, 2002, "other"),
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.control = replace(
        launcher.control,
        rodex_session_id=session.rodex_session_id,
        rodex_registry_id=lookup_rodex_registry_id(database),
        registration_state="registered",
    )

    with pytest.raises(RodexLaunchError, match="not owned by the current POSIX user"):
        run(["_context"], database_path=database, launcher=launcher)  # type: ignore[arg-type]


def test_context_rejects_unknown_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    with pytest.raises(RodexLaunchError, match=r"^usage: rodex _context \[--json\]$"):
        run(
            ["_context", "unexpected"],
            database_path=tmp_path / "rodex.sqlite3",
            launcher=StubLauncher(tmp_path),  # type: ignore[arg-type]
        )


def test_cli_does_not_resolve_away_an_explicit_database_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.sqlite3"
    create_a_rodex_session(target, codex_session_id=CODEX_SESSION_ID)
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(target)

    with pytest.raises(RodexSQLError, match="securely open database"):
        run(["_stats-status", "unused"], database_path=linked)


def test_unknown_bare_name_refuses_a_live_unregistered_tmux_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "tmux.sock"
    socket_path.touch()
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr("rodex.cli.default_tmux_server_socket_path", lambda: socket_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "rodex.cli.RodexRuntimeLauncher.session_exists", lambda *_args: True
    )

    with pytest.raises(RodexLaunchError, match="not registered"):
        run(
            ["legendary-mink"],
            database_path=tmp_path / "rodex.sqlite3",
            codex_delegator=delegator,
        )

    assert delegator.calls == []


def test_pending_runtime_with_exact_durable_identity_is_recovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(1, database)
    assert rodex_session_id is not None
    launcher = StubLauncher(tmp_path)
    launcher.control = replace(
        launcher.control,
        rodex_session_id=rodex_session_id,
        registration_state="pending",
    )

    assert run(["automatic-beluga"], database_path=database, launcher=launcher) == 0  # type: ignore[arg-type]

    assert launcher.confirmed == [
        LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga")
    ]
    assert launcher.attached


def test_named_attach_rejects_a_wrong_live_rodex_identity_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    launcher.control = replace(
        launcher.control,
        rodex_session_id=RodexSessionId(0xFFFFFFFFFFFFFFFF),
        registration_state="registered",
    )

    with pytest.raises(RodexLaunchError, match="unexpected Rodex identity"):
        run(["automatic-beluga"], database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert launcher.renamed == []
    assert launcher.configured == []
    assert launcher.attached == []
    assert launcher.stopped == []


def test_named_attach_rejects_a_runtime_from_another_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(1, database)
    assert rodex_session_id is not None
    launcher = StubLauncher(tmp_path)
    launcher.control = replace(
        launcher.control,
        rodex_session_id=rodex_session_id,
        rodex_registry_id=RodexRegistryId(lookup_rodex_registry_id(database).value ^ 1),
        registration_state="registered",
    )

    with pytest.raises(RodexLaunchError, match="registry identity"):
        run(["automatic-beluga"], database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert launcher.attached == []


@pytest.mark.parametrize(
    ("missing_field", "expected_diagnostic"),
    [
        ("registration_state", "not durably registered"),
        ("rodex_session_id", "unexpected Rodex identity"),
        ("rodex_registry_id", "unexpected Rodex registry identity"),
    ],
)
def test_named_attach_fails_closed_when_a_runtime_identity_marker_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
    expected_diagnostic: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(1, database)
    assert rodex_session_id is not None
    launcher = StubLauncher(tmp_path)
    complete = replace(
        launcher.control,
        rodex_session_id=rodex_session_id,
        rodex_registry_id=lookup_rodex_registry_id(database),
        registration_state="registered",
    )
    launcher.control = replace(complete, **{missing_field: None})
    launcher.discover_runtime_control = lambda _runtime: launcher.control  # type: ignore[method-assign]

    with pytest.raises(RodexLaunchError, match=expected_diagnostic):
        run(["automatic-beluga"], database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert launcher.attached == []
    assert launcher.renamed == []


def test_named_attach_fails_closed_when_the_codex_identity_marker_is_wrong(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(1, database)
    assert rodex_session_id is not None
    launcher = StubLauncher(tmp_path)
    launcher.control = replace(
        launcher.control,
        codex_session_id=REPLACEMENT_CODEX_SESSION_ID,
        rodex_session_id=rodex_session_id,
        rodex_registry_id=lookup_rodex_registry_id(database),
        registration_state="registered",
    )
    launcher.discover_runtime_control = lambda _runtime: launcher.control  # type: ignore[method-assign]

    with pytest.raises(RodexLaunchError, match="unexpected Codex identity"):
        run(["automatic-beluga"], database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert launcher.attached == []


def test_named_attach_recovers_one_externally_renamed_exact_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(1, database)
    assert rodex_session_id is not None
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.session_names = ("renamed-outside-rodex",)
    launcher.control = replace(
        launcher.control,
        rodex_session_id=rodex_session_id,
        registration_state="registered",
    )

    assert run(["automatic-beluga"], database_path=database, launcher=launcher) == 0  # type: ignore[arg-type]

    assert launcher.started == []
    assert launcher.renamed == [
        (
            LiveTmuxSession(tmp_path / "tmux.sock", "renamed-outside-rodex"),
            "automatic-beluga",
        )
    ]
    assert launcher.attached


def test_named_attach_recovers_one_relocated_pending_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(1, database)
    assert rodex_session_id is not None
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.session_names = ("interrupted-resume",)
    launcher.control = replace(
        launcher.control,
        rodex_session_id=rodex_session_id,
        registration_state="pending",
    )

    assert run(["automatic-beluga"], database_path=database, launcher=launcher) == 0  # type: ignore[arg-type]

    assert launcher.confirmed == [
        LiveTmuxSession(tmp_path / "tmux.sock", "interrupted-resume")
    ]
    assert launcher.started == []
    assert launcher.renamed == [
        (
            LiveTmuxSession(tmp_path / "tmux.sock", "interrupted-resume"),
            "automatic-beluga",
        )
    ]
    persisted_runtime = lookup_rodex_runtime_instance(1, database)
    assert persisted_runtime is not None
    assert persisted_runtime.runtime_identifier == RUNTIME_IDENTIFIER


def test_named_attach_refuses_multiple_relocated_exact_runtimes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(1, database)
    assert rodex_session_id is not None
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.session_names = ("first-copy", "second-copy")
    launcher.control = replace(
        launcher.control,
        rodex_session_id=rodex_session_id,
        registration_state="registered",
    )

    with pytest.raises(RodexLaunchError, match="multiple live runtimes"):
        run(["automatic-beluga"], database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert launcher.started == []
    assert launcher.attached == []


def test_running_reports_an_unregistered_live_tmux_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    socket_path = tmp_path / "tmux.sock"
    socket_path.touch()
    launcher = StubLauncher(tmp_path)
    launcher.session_names = ("orphan-name",)
    monkeypatch.setattr("rodex.cli.default_tmux_server_socket_path", lambda: socket_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    assert (
        run(["_running"], database_path=tmp_path / "rodex.sqlite3", launcher=launcher) == 0
    )  # type: ignore[arg-type]

    output = capsys.readouterr().out
    assert "Unregistered live tmux sessions: 1" in output
    assert f"orphan-name on {socket_path}" in output


@pytest.mark.parametrize("command", ["_send"])
def test_send_command_targets_the_verified_named_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    assert (
        run(
            [command, "automatic-beluga", "run", "tests"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )
        == 0
    )

    assert control.sent == [(launcher.control, "run tests")]
    assert launcher.control_discoveries == [
        LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga"),
        LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga"),
    ]
    assert "started Codex turn turn-1" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["_wait"])
def test_wait_command_waits_for_the_verified_named_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    assert (
        run(
            [command, "automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )
        == 0
    )

    assert control.waited == [launcher.control]


def test_machine_start_reads_stdin_and_emits_the_versioned_identity_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(sys, "stdin", io.StringIO("run focused tests\n"))
    create_exact_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    status = run(
        ["_start", "automatic-beluga", "--stdin", "--json"],
        database_path=database,
        launcher=launcher,  # type: ignore[arg-type]
        control_client=control,  # type: ignore[arg-type]
    )

    assert status == 0
    assert control.started == [(launcher.control, "run focused tests\n")]
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["operation"] == "turn.start"
    assert payload["ok"] is True
    assert payload["runtime"] == {
        "identifier": str(RUNTIME_IDENTIFIER),
        "state": "running",
    }
    assert payload["codex"] == {
        "thread_id": str(CODEX_SESSION_ID),
        "session_id": str(CODEX_SESSION_ID),
        "turn_id": "turn-1",
    }


def test_machine_exact_control_requires_a_persisted_runtime_identifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(sys, "stdin", io.StringIO("run tests"))
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    status = run(
        ["_start", "automatic-beluga", "--stdin", "--json"],
        database_path=database,
        launcher=launcher,  # type: ignore[arg-type]
        control_client=control,  # type: ignore[arg-type]
    )

    assert status == 3
    assert control.started == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "runtime_upgrade_required"
    assert payload["error"]["retryable"] is False


def test_machine_steer_targets_only_the_supplied_turn_and_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(sys, "stdin", io.StringIO("also lint"))
    create_exact_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    status = run(
        [
            "_steer",
            "automatic-beluga",
            "--turn",
            "turn-active",
            "--stdin",
            "--json",
        ],
        database_path=database,
        launcher=launcher,  # type: ignore[arg-type]
        control_client=control,  # type: ignore[arg-type]
    )

    assert status == 0
    assert control.steered == [(launcher.control, "turn-active", "also lint")]
    assert json.loads(capsys.readouterr().out)["codex"]["turn_id"] == "turn-active"


def test_machine_wait_parses_timeout_and_reports_exact_terminal_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_exact_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    status = run(
        [
            "_wait",
            "automatic-beluga",
            "--turn",
            "turn-1",
            "--timeout",
            "2m",
            "--json",
        ],
        database_path=database,
        launcher=launcher,  # type: ignore[arg-type]
        control_client=control,  # type: ignore[arg-type]
    )

    assert status == 0
    assert control.exact_waited == [(launcher.control, "turn-1", 120.0)]
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "turn.wait"
    assert payload["data"]["turn"]["status"] == "completed"


def test_machine_wait_timeout_is_retryable_and_does_not_call_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_exact_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()
    control.wait_error = RodexWaitTimeoutError("timed out waiting for turn-1")

    status = run(
        ["_wait", "automatic-beluga", "--turn", "turn-1", "--json"],
        database_path=database,
        launcher=launcher,  # type: ignore[arg-type]
        control_client=control,  # type: ignore[arg-type]
    )

    assert status == 4
    assert control.interrupted == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == {
        "code": "wait_timeout",
        "message": "timed out waiting for turn-1",
        "retryable": True,
    }
    assert payload["codex"]["turn_id"] == "turn-1"


def test_machine_command_reports_missing_tmux_in_its_json_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("rodex.cli.shutil.which", lambda _binary: None)

    status = run(
        ["_result", "automatic-beluga", "--turn", "turn-1", "--json"],
        database_path=tmp_path / "rodex.sqlite3",
    )

    assert status == 3
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert payload["operation"] == "turn.result"
    assert payload["error"] == {
        "code": "runtime_unavailable",
        "message": "tmux executable was not found: tmux",
        "retryable": True,
    }


def test_machine_indeterminate_dispatch_is_explicitly_not_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(sys, "stdin", io.StringIO("run tests"))
    create_exact_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()
    control.start_error = RodexDispatchIndeterminateError(
        "turn/start was sent but its acceptance is unknown"
    )

    status = run(
        ["_start", "automatic-beluga", "--stdin", "--json"],
        database_path=database,
        launcher=launcher,  # type: ignore[arg-type]
        control_client=control,  # type: ignore[arg-type]
    )

    assert status == 7
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == {
        "code": "dispatch_indeterminate",
        "message": "turn/start was sent but its acceptance is unknown",
        "retryable": False,
    }


def test_machine_wait_distinguishes_a_failed_turn_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_exact_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()
    control.turn_result = replace(
        control.turn_result,
        status="failed",
        error={"message": "model failed"},
    )

    status = run(
        ["_wait", "automatic-beluga", "--turn", "turn-1", "--json"],
        database_path=database,
        launcher=launcher,  # type: ignore[arg-type]
        control_client=control,  # type: ignore[arg-type]
    )

    assert status == 6
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "turn_failed"
    assert payload["data"]["turn"]["error"] == {"message": "model failed"}


def test_machine_inspect_is_available_for_a_legacy_runtime_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()
    control.state = replace(
        control.state,
        status="active",
        active_turn_id="turn-active",
    )

    status = run(
        ["_inspect", "automatic-beluga", "--json"],
        database_path=database,
        launcher=launcher,  # type: ignore[arg-type]
        control_client=control,  # type: ignore[arg-type]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["exact_control_available"] is False
    assert payload["data"]["runtime_identity_persisted"] is False
    assert payload["codex"]["turn_id"] == "turn-active"


def test_machine_inspect_reports_incompatible_exact_control_without_hiding_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_exact_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()
    control.compatibility_error = RodexAppServerCompatibilityError(
        "exact control supports 0.147.0; live server is 0.148.0"
    )

    status = run(
        ["_inspect", "automatic-beluga", "--json"],
        database_path=database,
        launcher=launcher,  # type: ignore[arg-type]
        control_client=control,  # type: ignore[arg-type]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["thread"]["status"] == "idle"
    assert payload["data"]["exact_control_available"] is False
    assert payload["data"]["app_server"] == {
        "compatible_version": None,
        "exact_control_compatible": False,
        "compatibility_error": "exact control supports 0.147.0; live server is 0.148.0",
    }


def test_machine_result_returns_live_final_message_and_bounded_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_exact_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()
    control.turn_result = replace(
        control.turn_result,
        final_agent_message="finished",
        changed_paths=("src/rodex/cli.py",),
    )

    status = run(
        ["_result", "automatic-beluga", "--turn", "turn-1", "--json"],
        database_path=database,
        launcher=launcher,  # type: ignore[arg-type]
        control_client=control,  # type: ignore[arg-type]
    )

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["turn"]["final_agent_message"] == "finished"
    assert payload["data"]["turn"]["changes"] == {
        "paths": ["src/rodex/cli.py"],
        "truncated": False,
    }


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("on", "on"), ("off", "off"), ("toggle", "on"), ("inherit", "off")],
)
def test_mouse_command_targets_only_the_verified_named_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    expected: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)

    assert (
        run(
            ["_mouse", "automatic-beluga", mode],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert capsys.readouterr().out == f"Rodex automatic-beluga mouse: {expected}\n"
    assert launcher.attached == []


@pytest.mark.parametrize("command", ["_tail"])
def test_tail_command_streams_json_events_for_the_verified_named_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    assert (
        run(
            [command, "automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )
        == 0
    )

    captured = capsys.readouterr()
    assert control.tailed == [launcher.control]
    assert captured.out == '{"method":"turn/started"}\n'
    assert "following live Codex protocol events" in captured.err


@pytest.mark.parametrize("arguments", [[], ["_create"]], ids=["bare", "explicit"])
def test_default_and_explicit_create_link_identities_before_attach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda word_count: "automatic-beluga" if word_count == 2 else "unused-name-here",
    )

    assert (
        run(
            arguments,
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), [])]
    planned_session_id, planned_database = launcher.analytics_identities[0]
    assert planned_session_id == lookup_rodex_session_id_from_a_rodex_sessions_id(
        1, database
    )
    assert planned_database == database
    assert launcher.registry_identities == [lookup_rodex_registry_id(database)]
    assert launcher.renamed == [(launcher.runtime, "automatic-beluga")]
    assert launcher.configured[0].tmux_session_name == "automatic-beluga"
    assert launcher.attached == launcher.configured
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert lookup_codex_session_id_from_a_rodex_sessions_id(1, database) == CODEX_SESSION_ID
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(
        launcher.runtime.tmux_server_socket_path
    )
    assert tmux_link.tmux_session_name == "automatic-beluga"
    output = capsys.readouterr().out
    assert f"-> Codex {CODEX_SESSION_ID}" in output
    assert "Rodex automatic-beluga" in output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["-h"],
        ["--version"],
        ["-V"],
        ["exec"],
        ["review"],
        ["login"],
        ["logout"],
        ["mcp"],
        ["plugin"],
        ["mcp-server"],
        ["app-server"],
        ["remote-control"],
        ["completion"],
        ["update"],
        ["doctor"],
        ["sandbox"],
        ["debug"],
        ["apply"],
        ["resume"],
        ["archive"],
        ["delete"],
        ["unarchive"],
        ["fork"],
        ["cloud"],
        ["exec-server"],
        ["features"],
        ["help"],
        ["exec", "--json", "run tests"],
        ["review", "--uncommitted"],
        ["features", "list"],
        ["mcp", "list"],
        ["completion", "bash"],
        ["doctor"],
        ["resume", "--last"],
        ["fork", "--last"],
        ["--remote", "unix:///tmp/codex.sock"],
        ["--remote=unix:///tmp/codex.sock"],
        ["--future-codex-option"],
        ["--model", "example"],
        ["future-codex-command", "argument"],
        ["--create", "project_1234"],
        ["--detach"],
        ["--force"],
        ["running"],
    ],
)
def test_non_rodex_invocations_delegate_unchanged_without_tmux_or_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    delegator = RecordingCodexDelegator(returncode=23)
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: "/usr/bin/codex" if command == "codex" else None,
    )

    assert (
        run(arguments, database_path=database, codex_delegator=delegator)
        == delegator.returncode
    )

    assert delegator.calls == [("/usr/bin/codex", arguments)]
    assert not database.exists()


def test_bare_invocation_never_delegates_to_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = StubLauncher(tmp_path)
    delegator = RecordingCodexDelegator(returncode=23)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            [],
            database_path=tmp_path / "rodex.sqlite3",
            launcher=launcher,  # type: ignore[arg-type]
            codex_delegator=delegator,
        )
        == 0
    )

    assert delegator.calls == []
    assert launcher.started == [(Path.cwd(), [])]
    assert launcher.attached == launcher.configured


def test_bare_invocation_is_observably_equivalent_to_explicit_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    observed: list[tuple[object, ...]] = []

    for label, arguments in (("bare", []), ("explicit", ["_create"])):
        case_root = tmp_path / label
        launcher = StubLauncher(case_root)
        database = case_root / "rodex.sqlite3"

        assert run(arguments, database_path=database, launcher=launcher) == 0  # type: ignore[arg-type]

        tmux_link = lookup_rodex_tmux_session(1, database)
        names = lookup_rodex_session_names(1, database)
        assert tmux_link is not None
        assert names is not None
        observed.append(
            (
                launcher.started,
                [runtime.tmux_session_name for runtime in launcher.configured],
                [runtime.tmux_session_name for runtime in launcher.attached],
                names.display_name,
                tmux_link.tmux_session_name,
                lookup_codex_session_id_from_a_rodex_sessions_id(1, database),
            )
        )

    assert observed[0] == observed[1]


def test_create_command_forwards_interactive_codex_arguments_to_managed_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = StubLauncher(tmp_path)
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            ["_create", "--model", "review"],
            database_path=tmp_path / "rodex.sqlite3",
            launcher=launcher,  # type: ignore[arg-type]
            codex_delegator=delegator,
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), ["--model", "review"])]
    assert delegator.calls == []


@pytest.mark.parametrize("create_flag", ["_create"])
def test_explicit_create_assigns_the_requested_display_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    create_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            [create_flag, "project_1234"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), [])]
    assert launcher.renamed == [(launcher.runtime, "project_1234")]
    assert launcher.attached[0].tmux_session_name == "project_1234"
    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.cool_name == "automatic-beluga"
    assert names.user_defined_cool_name == "project_1234"
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "project_1234"


@pytest.mark.parametrize("create_flag", ["_create"])
def test_explicit_create_forwards_ordinary_codex_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    create_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            [create_flag, "project_1234", "--model", "gpt-5.6-luna"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [
        (Path.cwd(), ["--model", "gpt-5.6-luna"]),
    ]
    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.display_name == "project_1234"


def test_codex_short_config_passes_through_without_starting_rodex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    assert (
        run(
            ["-c", 'model="gpt-5.6-luna"'],
            database_path=database,
            codex_delegator=delegator,
        )
        == 0
    )

    assert delegator.calls == [("/usr/bin/codex", ["-c", 'model="gpt-5.6-luna"'])]
    assert not database.exists()


@pytest.mark.parametrize(
    "codex_argument",
    ["--create", "--c", "-create", "-d", "--d", "-detach", "--detach"],
)
def test_codex_end_of_options_preserves_rodex_looking_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    codex_argument: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    assert (
        run(
            ["exec", "--", codex_argument],
            database_path=database,
            codex_delegator=delegator,
        )
        == 0
    )

    assert delegator.calls == [("/usr/bin/codex", ["exec", "--", codex_argument])]
    assert not database.exists()


def test_rodex_launch_options_are_consumed_only_from_the_command_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    arguments = ["exec", "-d", "--create", "report"]

    assert run(arguments, codex_delegator=delegator) == 0

    assert delegator.calls == [("/usr/bin/codex", arguments)]


def test_explicit_create_can_forward_codex_arguments_after_end_of_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            ["_create", "project_1234", "--", "--create"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), ["--create"])]
    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.display_name == "project_1234"


@pytest.mark.parametrize("detach_flag", ["_detach"])
def test_detach_starts_without_attaching_and_prints_compact_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    detach_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            [detach_flag],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), [])]
    assert launcher.attached == []
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "status": "running",
        "rodex_session_name": "automatic-beluga",
        "rodex_session_id": str(
            lookup_rodex_session_id_from_a_rodex_sessions_id(1, database)
        ),
        "codex_session_id": str(CODEX_SESSION_ID),
    }
    assert output == f"{json.dumps(payload, indent=2)}\n"


@pytest.mark.parametrize("detach_flag", ["_detach"])
def test_detach_command_forwards_interactive_codex_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    detach_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )

    assert (
        run(
            [detach_flag, "--model", "gpt-5.6-terra"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.attached == []
    assert launcher.started == [(Path.cwd(), ["--model", "gpt-5.6-terra"])]
    assert json.loads(capsys.readouterr().out)["rodex_session_name"] == ("automatic-beluga")


@pytest.mark.parametrize("detach_flag", ["_detach"])
def test_detach_existing_name_resolves_without_attaching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    detach_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_controlled_session(database, tmp_path)
    launcher = StubLauncher(tmp_path)

    assert (
        run(
            [detach_flag, "automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == []
    assert launcher.attached == []
    assert json.loads(capsys.readouterr().out)["rodex_session_name"] == ("automatic-beluga")


def test_detach_ended_name_resumes_exact_codex_session_without_attaching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False

    assert (
        run(
            ["_detach", "automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert launcher.started == [(Path.cwd(), ["resume", str(CODEX_SESSION_ID)])]
    assert launcher.analytics_identities == [(session.rodex_session_id, database)]
    assert launcher.renamed == [(launcher.runtime, "automatic-beluga")]
    assert launcher.configured[0].tmux_session_name == "automatic-beluga"
    assert launcher.attached == []
    assert output == {
        "status": "running",
        "rodex_session_name": "automatic-beluga",
        "rodex_session_id": str(session.rodex_session_id),
        "codex_session_id": str(CODEX_SESSION_ID),
    }


def test_detach_unsaved_name_recovers_identity_without_attaching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.start_error = RodexCodexSessionNotFoundError(
        f"Codex has no saved session for exact identity {CODEX_SESSION_ID}"
    )
    launcher.observed_codex_session_id = REPLACEMENT_CODEX_SESSION_ID

    assert (
        run(
            ["_detach", "automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert launcher.started == [
        (Path.cwd(), ["resume", str(CODEX_SESSION_ID)]),
        (Path.cwd(), []),
    ]
    assert launcher.analytics_identities == [
        (session.rodex_session_id, database),
        (session.rodex_session_id, database),
    ]
    assert launcher.configured[0].tmux_session_name == "automatic-beluga"
    assert launcher.attached == []
    assert (
        lookup_rodex_session_id_from_a_rodex_sessions_id(
            session.rodex_sessions_id, database
        )
        == session.rodex_session_id
    )
    assert (
        lookup_codex_session_id_from_a_rodex_sessions_id(
            session.rodex_sessions_id, database
        )
        == REPLACEMENT_CODEX_SESSION_ID
    )
    assert output == {
        "status": "running",
        "rodex_session_name": "automatic-beluga",
        "rodex_session_id": str(session.rodex_session_id),
        "codex_session_id": str(REPLACEMENT_CODEX_SESSION_ID),
    }


def test_unknown_underscore_command_passes_through_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delegator = RecordingCodexDelegator()
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    arguments = ["_future-command", "argument"]

    assert run(arguments, codex_delegator=delegator) == 0

    assert delegator.calls == [("/usr/bin/codex", arguments)]


def test_existing_name_wins_over_a_possible_future_codex_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "future-command"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=RodexSessionsUserIdentity(1009, 1010, "dna"),
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="rodex-token",
    )
    launcher = StubLauncher(tmp_path)
    delegator = RecordingCodexDelegator()

    assert (
        run(
            ["future-command"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            codex_delegator=delegator,
        )
        == 0
    )

    assert delegator.calls == []
    assert launcher.started == []
    assert launcher.attached[0].tmux_session_name == "future-command"


def test_live_cool_name_argument_renames_configures_and_reattaches_without_starting_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda word_count: "automatic-beluga" if word_count == 2 else "unused-name-here",
    )
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=RodexSessionsUserIdentity(1009, 1010, "dna"),
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="rodex-token",
    )
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: None if command == "codex" else available_prerequisite(command),
    )

    assert (
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == []
    assert launcher.existing_checks == [
        LiveTmuxSession(tmp_path / "tmux.sock", "rodex-token")
    ]
    assert len(launcher.renamed) == 1
    assert launcher.renamed[0][0].tmux_session_name == "rodex-token"
    assert launcher.renamed[0][1] == "automatic-beluga"
    assert launcher.configured[0].tmux_session_name == "automatic-beluga"
    assert launcher.attached == launcher.configured
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "automatic-beluga"
    assert "Reattaching Rodex automatic-beluga" in capsys.readouterr().out


def test_ended_cool_name_argument_transparently_resumes_its_codex_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda word_count: "automatic-beluga" if word_count == 2 else "unused-name-here",
    )
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=RodexSessionsUserIdentity(1009, 1010, "dna"),
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    assert (
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), ["resume", str(CODEX_SESSION_ID)])]
    assert launcher.renamed == [(launcher.runtime, "automatic-beluga")]
    assert launcher.attached[0].tmux_session_name == "automatic-beluga"
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(
        launcher.runtime.tmux_server_socket_path
    )
    assert tmux_link.tmux_session_name == "automatic-beluga"
    assert f"Resumed Rodex automatic-beluga -> Codex {CODEX_SESSION_ID}" in (
        capsys.readouterr().out
    )


def test_concurrent_ended_session_opens_start_only_one_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr(
        "rodex_registry.lifecycle.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    registry_id = lookup_rodex_registry_id(database)
    state_lock = Lock()
    live_name: list[str | None] = [None]
    start_calls = 0
    first_start_entered = Event()
    allow_first_start = Event()
    second_attempt_entered = Event()
    second_finished = Event()

    class ConcurrentResumeLauncher(StubLauncher):
        def __init__(self, *, first: bool) -> None:
            super().__init__(tmp_path)
            self.first = first
            self.control = replace(
                self.control,
                rodex_session_id=session.rodex_session_id,
                rodex_registry_id=registry_id,
                registration_state="registered",
            )

        def session_exists(self, runtime: LiveTmuxSession) -> bool:
            with state_lock:
                return runtime.tmux_session_name == live_name[0]

        def list_session_names(self, _socket_path: Path) -> tuple[str, ...]:
            with state_lock:
                return () if live_name[0] is None else (live_name[0],)

        def start(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            nonlocal start_calls
            with state_lock:
                start_calls += 1
            if self.first:
                first_start_entered.set()
                assert allow_first_start.wait(5)
            with state_lock:
                live_name[0] = self.runtime.tmux_session_name
            return self.runtime, CODEX_SESSION_ID

        def rename(
            self, runtime: LiveTmuxSession, tmux_session_name: str
        ) -> LiveTmuxSession:
            renamed = super().rename(runtime, tmux_session_name)
            with state_lock:
                live_name[0] = tmux_session_name
            return renamed

    errors: list[BaseException] = []
    first_launcher = ConcurrentResumeLauncher(first=True)
    second_launcher = ConcurrentResumeLauncher(first=False)

    def open_session(launcher: ConcurrentResumeLauncher, *, second: bool) -> None:
        if second:
            second_attempt_entered.set()
        try:
            run(["automatic-beluga"], database_path=database, launcher=launcher)
        except BaseException as error:
            errors.append(error)
        finally:
            if second:
                second_finished.set()

    first_thread = Thread(
        target=open_session, args=(first_launcher,), kwargs={"second": False}
    )
    second_thread = Thread(
        target=open_session,
        args=(second_launcher,),
        kwargs={"second": True},
    )
    first_thread.start()
    assert first_start_entered.wait(5), errors
    second_thread.start()
    assert second_attempt_entered.wait(5)
    try:
        second_was_serialized = not second_finished.wait(0.25)
    finally:
        allow_first_start.set()
    first_thread.join(5)
    second_thread.join(5)

    assert second_was_serialized
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert start_calls == 1
    assert first_launcher.attached
    assert second_launcher.attached
    lock_path = tmp_path / ".rodex.sqlite3.session-1.lock"
    assert lock_path.stat().st_mode & 0o777 == 0o600


def test_named_session_transition_lock_rejects_a_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    lock_target = tmp_path / "lock-target"
    lock_target.touch()
    (tmp_path / ".rodex.sqlite3.session-1.lock").symlink_to(lock_target)
    launcher = StubLauncher(tmp_path)

    with pytest.raises(OSError):
        run(["automatic-beluga"], database_path=database, launcher=launcher)

    assert launcher.attached == []


def test_unsaved_codex_session_starts_fresh_and_relinks_the_rodex_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.start_error = RodexCodexSessionNotFoundError(
        f"Codex has no saved session for exact identity {CODEX_SESSION_ID}"
    )
    launcher.observed_codex_session_id = REPLACEMENT_CODEX_SESSION_ID

    assert (
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert (
        lookup_rodex_session_id_from_a_rodex_sessions_id(
            session.rodex_sessions_id, database
        )
        == session.rodex_session_id
    )
    assert (
        lookup_codex_session_id_from_a_rodex_sessions_id(
            session.rodex_sessions_id, database
        )
        == REPLACEMENT_CODEX_SESSION_ID
    )
    assert launcher.started == [
        (Path.cwd(), ["resume", str(CODEX_SESSION_ID)]),
        (Path.cwd(), []),
    ]
    assert launcher.renamed == [(launcher.runtime, "automatic-beluga")]
    assert launcher.configured[0].tmux_session_name == "automatic-beluga"
    assert launcher.attached == launcher.configured
    assert launcher.stopped == []
    tmux_link = lookup_rodex_tmux_session(session.rodex_sessions_id, database)
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(
        launcher.runtime.tmux_server_socket_path
    )
    assert f"Recovered Rodex automatic-beluga -> Codex {REPLACEMENT_CODEX_SESSION_ID}" in (
        capsys.readouterr().out
    )


def test_non_missing_history_resume_failure_does_not_start_a_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.start_error = RodexRuntimeError("Codex transport failed")
    recorded_tmux = lookup_rodex_tmux_session(1, database)

    with pytest.raises(RodexLaunchError, match="Codex transport failed"):
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert lookup_codex_session_id_from_a_rodex_sessions_id(1, database) == CODEX_SESSION_ID
    assert lookup_rodex_tmux_session(1, database) == recorded_tmux
    assert launcher.started == [(Path.cwd(), ["resume", str(CODEX_SESSION_ID)])]


def test_unsaved_session_remains_linked_to_the_stored_codex_session_id_if_recovery_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.start_errors = [
        RodexCodexSessionNotFoundError(
            f"Codex has no saved session for exact identity {CODEX_SESSION_ID}"
        ),
        RodexRuntimeError("fresh Codex startup failed"),
    ]
    recorded_tmux = lookup_rodex_tmux_session(1, database)

    with pytest.raises(RodexLaunchError, match="replacement Codex runtime") as raised:
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert "fresh Codex startup failed" in str(raised.value)
    assert lookup_codex_session_id_from_a_rodex_sessions_id(1, database) == CODEX_SESSION_ID
    assert lookup_rodex_tmux_session(1, database) == recorded_tmux
    assert launcher.started == [
        (Path.cwd(), ["resume", str(CODEX_SESSION_ID)]),
        (Path.cwd(), []),
    ]
    assert launcher.renamed == []
    assert launcher.configured == []
    assert launcher.attached == []


@pytest.mark.parametrize("lookup_name", ["black-sawfly", "work"])
def test_either_name_route_displays_the_user_defined_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    lookup_name: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda _word_count: "black-sawfly",
    )
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="black-sawfly",
    )
    assign_a_user_defined_cool_name("black-sawfly", "work", database, user_identity=DNA)
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: None if command == "codex" else available_prerequisite(command),
    )

    assert run([lookup_name], database_path=database, launcher=launcher) == 0  # type: ignore[arg-type]

    assert launcher.renamed == [
        (LiveTmuxSession(tmp_path / "tmux.sock", "black-sawfly"), "work")
    ]
    assert launcher.configured == [LiveTmuxSession(tmp_path / "tmux.sock", "work")]
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "work"
    assert "Reattaching Rodex work" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["_alias"])
@pytest.mark.parametrize("force_flag", ["--force"])
def test_alias_command_accepts_force_without_starting_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    force_flag: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda _word_count: "black-sawfly",
    )
    monkeypatch.setattr(
        "rodex_registry.lifecycle.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda name: None if name == "codex" else available_prerequisite(name),
    )
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="black-sawfly",
    )
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    assert (
        run(
            [command, "black-sawfly", "first"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )
        == 0
    )
    assert (
        run(
            [command, force_flag, "black-sawfly", "replacement"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == []
    assert [name for _, name in launcher.renamed] == ["first", "replacement"]
    assert launcher.configured[-1].tmux_session_name == "replacement"
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "replacement"
    assert [prompt for _live_control, prompt in control.sent] == [
        (
            f"RODEX_AUTO_INFO: Rodex session {session.rodex_session_id} "
            "is now named 'first'."
        ),
        (
            f"RODEX_AUTO_INFO: Rodex session {session.rodex_session_id} "
            "is now named 'replacement'."
        ),
    ]
    assert "Rodex name: replacement" in capsys.readouterr().out


def test_alias_does_not_send_auto_info_when_the_session_is_not_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "black-sawfly"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="black-sawfly",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    control = StubControlClient()

    assert (
        run(
            ["_alias", "black-sawfly", "work"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )
        == 0
    )

    assert control.sent == []


def test_alias_does_not_send_auto_info_when_the_display_name_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "black-sawfly"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="black-sawfly",
    )
    assign_a_user_defined_cool_name(
        "black-sawfly",
        "work",
        database,
        user_identity=DNA,
        renamed_tmux_session_name="work",
    )
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()

    assert (
        run(
            ["_alias", "work", "work"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )
        == 0
    )

    assert control.sent == []


def test_alias_reports_auto_info_failure_without_rolling_back_the_new_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "black-sawfly"
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="black-sawfly",
    )
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()
    control.send_error = RodexControlError("delivery unavailable")

    with pytest.raises(
        RodexLaunchError,
        match=r"name changed to 'work'.*RODEX_AUTO_INFO delivery failed",
    ):
        run(
            ["_alias", "black-sawfly", "work"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )

    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.display_name == "work"
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "work"


def test_alias_replacement_without_force_is_reported_on_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda _word_count: "black-sawfly",
    )
    monkeypatch.setattr(
        "rodex_registry.lifecycle.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda name: None if name == "codex" else available_prerequisite(name),
    )
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="black-sawfly",
    )
    assign_a_user_defined_cool_name("black-sawfly", "first", database, user_identity=DNA)
    monkeypatch.setenv("RODEX_DATABASE_PATH", str(database))
    monkeypatch.setattr(sys, "argv", ["rodex", "_alias", "black-sawfly", "replacement"])

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 1
    assert "use --force" in capsys.readouterr().err


@pytest.mark.parametrize("unsupported_force", ["-f", "--f", "-force"])
def test_alias_rejects_removed_force_spellings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsupported_force: str,
) -> None:
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    with pytest.raises(RodexLaunchError, match="unknown alias option"):
        run(
            ["_alias", unsupported_force, "session", "replacement"],
            database_path=tmp_path / "rodex.sqlite3",
            launcher=launcher,  # type: ignore[arg-type]
        )


def test_empty_alias_is_a_concise_stderr_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    monkeypatch.setattr(
        "rodex_registry.lifecycle.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="safe-name",
    )
    monkeypatch.setenv("RODEX_DATABASE_PATH", str(database))
    monkeypatch.setattr(sys, "argv", ["rodex", "_alias", "safe-name", ""])

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 1
    error_output = capsys.readouterr().err
    assert error_output.startswith("rodex: ")
    assert "non-empty" in error_output
    assert "Traceback" not in error_output


def test_sqlite_operational_error_is_a_concise_stderr_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "rodex.cli.run",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database unavailable")),
    )

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 1
    assert capsys.readouterr().err == "rodex: database unavailable\n"


def test_missing_executable_retains_command_not_found_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "rodex.cli.run",
        lambda: (_ for _ in ()).throw(
            RodexExecutableNotFoundError("tmux executable was not found: tmux")
        ),
    )

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 127
    assert "tmux executable" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["_running"])
def test_running_commands_show_only_the_current_users_live_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda _word_count: "black-sawfly",
    )
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "dna.sock",
        tmux_session_name="black-sawfly",
    )
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda _word_count: "silver-otter",
    )
    create_a_rodex_session(
        database,
        codex_session_id=uuid.UUID(int=CODEX_SESSION_ID.int + 1),
        user_identity=RodexSessionsUserIdentity(2001, 2002, "other"),
        tmux_server_socket_path=tmp_path / "other.sock",
        tmux_session_name="silver-otter",
    )
    assign_a_user_defined_cool_name("black-sawfly", "work", database, user_identity=DNA)
    monkeypatch.setattr(
        "rodex_registry.lifecycle.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda name: None if name == "codex" else available_prerequisite(name),
    )
    launcher = StubLauncher(tmp_path)

    assert run([command], database_path=database, launcher=launcher) == 0  # type: ignore[arg-type]

    assert launcher.started == []
    assert launcher.existing_checks == [
        LiveTmuxSession(tmp_path / "dna.sock", "black-sawfly")
    ]
    output = capsys.readouterr().out
    assert "Running Rodex sessions: 1" in output
    assert f"work -> Codex {CODEX_SESSION_ID}" in output
    assert "black-sawfly" not in output
    assert "silver-otter" not in output


@pytest.mark.parametrize("arguments", [[], ["_create"]], ids=["bare", "explicit"])
def test_new_launch_cleans_up_the_renamed_runtime_when_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    monkeypatch.setattr(
        "rodex.cli.update_rodex_tmux_session_name",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("persist failed")),
    )

    with pytest.raises(RuntimeError, match="persist failed"):
        run(arguments, database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert launcher.renamed == [(launcher.runtime, "safe-name")]
    assert launcher.stopped == [
        (replace(launcher.runtime, tmux_session_name="safe-name"), False)
    ]
    assert launcher.attached == []


def test_live_reattach_restores_the_recorded_name_when_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="rodex-token",
    )
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "rodex.cli.update_rodex_tmux_session_name",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("persist failed")),
    )

    with pytest.raises(RuntimeError, match="persist failed"):
        run(["safe-name"], database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert [name for _, name in launcher.renamed] == ["safe-name", "rodex-token"]
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "rodex-token"
    assert launcher.attached == []


def test_alias_rename_failure_preserves_the_previous_name_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    monkeypatch.setattr(
        "rodex_registry.lifecycle.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="safe-name",
    )
    launcher = StubLauncher(tmp_path)
    control = StubControlClient()
    assert (
        run(
            ["_alias", "safe-name", "first"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )
        == 0
    )

    def fail_rename(*args: object, **kwargs: object) -> LiveTmuxSession:
        raise RodexRuntimeError("rename failed")

    monkeypatch.setattr(launcher, "rename", fail_rename)
    with pytest.raises(RodexRuntimeError, match="rename failed"):
        run(
            ["_alias", "--force", "safe-name", "replacement"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
            control_client=control,  # type: ignore[arg-type]
        )

    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.display_name == "first"
    assert lookup_rodex_sessions_id_from_a_cool_name("replacement", database) is None
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "first"


def test_alias_database_failure_renames_tmux_back_and_leaves_no_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    monkeypatch.setattr(
        "rodex_registry.lifecycle.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="safe-name",
    )
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr(
        "rodex_registry.lifecycle.reserve_specific_cool_name",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database failed")
        ),
    )

    with pytest.raises(sqlite3.OperationalError, match="database failed"):
        run(
            ["_alias", "safe-name", "work"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert [name for _, name in launcher.renamed] == ["work", "safe-name"]
    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.user_defined_cool_name is None
    assert lookup_rodex_sessions_id_from_a_cool_name("work", database) is None
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "safe-name"


def test_concurrent_alias_commands_serialize_across_tmux_and_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "safe-name"
    )
    monkeypatch.setattr(
        "rodex_registry.lifecycle.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=DNA,
        tmux_server_socket_path=tmp_path / "tmux.sock",
        tmux_session_name="safe-name",
    )
    registry_id = lookup_rodex_registry_id(database)
    state_lock = Lock()
    live_name = ["safe-name"]
    first_rename_started = Event()
    allow_first_commit = Event()
    second_attempt_started = Event()
    second_finished = Event()

    class ConcurrentAliasLauncher:
        def __init__(self, *, pause_after_rename: bool) -> None:
            self.pause_after_rename = pause_after_rename

        def session_exists(self, runtime: LiveTmuxSession) -> bool:
            with state_lock:
                return runtime.tmux_session_name == live_name[0]

        def rename(
            self, runtime: LiveTmuxSession, tmux_session_name: str
        ) -> LiveTmuxSession:
            with state_lock:
                assert runtime.tmux_session_name == live_name[0]
                live_name[0] = tmux_session_name
            if self.pause_after_rename:
                first_rename_started.set()
                assert allow_first_commit.wait(5)
            return replace(runtime, tmux_session_name=tmux_session_name)

        def configure_identity_status(self, runtime: LiveTmuxSession) -> None:
            return None

        def discover_runtime_control(self, _runtime: LiveTmuxSession) -> LiveRodexControl:
            return LiveRodexControl(
                tmp_path / "proxy.sock",
                tmp_path / "events.sock",
                CODEX_SESSION_ID,
                session.rodex_session_id,
                registry_id,
                "registered",
            )

    first_errors: list[BaseException] = []
    second_errors: list[BaseException] = []
    control = StubControlClient()

    def assign_name(
        name: str,
        launcher: ConcurrentAliasLauncher,
        errors: list[BaseException],
        finished: Event | None = None,
    ) -> None:
        try:
            run(
                ["_alias", "safe-name", name],
                database_path=database,
                launcher=launcher,  # type: ignore[arg-type]
                control_client=control,  # type: ignore[arg-type]
            )
        except BaseException as error:
            errors.append(error)
        finally:
            if finished is not None:
                finished.set()

    first_thread = Thread(
        target=assign_name,
        args=("first", ConcurrentAliasLauncher(pause_after_rename=True), first_errors),
    )
    second_thread = Thread(
        target=assign_name,
        args=(
            "second",
            ConcurrentAliasLauncher(pause_after_rename=False),
            second_errors,
            second_finished,
        ),
    )

    def observe_assignment_attempt(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if current_thread() is second_thread:
            second_attempt_started.set()
        return open_a_user_defined_cool_name_assignment(*args, **kwargs)

    monkeypatch.setattr(
        "rodex.cli.open_a_user_defined_cool_name_assignment",
        observe_assignment_attempt,
    )
    first_thread.start()
    assert first_rename_started.wait(5), first_errors
    second_thread.start()
    try:
        second_was_serialized = not second_finished.wait(0.25)
    finally:
        allow_first_commit.set()
    assert second_was_serialized
    assert second_attempt_started.wait(5)
    first_thread.join(5)
    second_thread.join(5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert first_errors == []
    assert len(second_errors) == 1
    assert isinstance(second_errors[0], RodexSessionError)
    assert "use --force" in str(second_errors[0])
    names = lookup_rodex_session_names(1, database)
    assert names is not None
    assert names.display_name == "first"
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_session_name == "first"
    assert live_name == ["first"]
    assert [prompt for _live_control, prompt in control.sent] == [
        (f"RODEX_AUTO_INFO: Rodex session {session.rodex_session_id} is now named 'first'.")
    ]


def test_named_open_rejects_a_session_owned_by_another_posix_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _word_count: "other-work"
    )
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        user_identity=RodexSessionsUserIdentity(2001, 2002, "other"),
        tmux_server_socket_path=tmp_path / "other.sock",
        tmux_session_name="other-work",
    )
    monkeypatch.setattr(
        "rodex_registry.lifecycle.current_rodex_sessions_user_identity", lambda: DNA
    )
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    launcher = StubLauncher(tmp_path)

    with pytest.raises(RodexSessionError, match="not owned"):
        run(["other-work"], database_path=database, launcher=launcher)  # type: ignore[arg-type]

    assert launcher.started == []
    assert launcher.existing_checks == []
    assert launcher.attached == []


def test_resume_stops_new_runtime_if_codex_reports_a_different_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda word_count: "automatic-beluga" if word_count == 2 else "unused-name-here",
    )
    create_a_rodex_session(
        database,
        codex_session_id=CODEX_SESSION_ID,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.observed_codex_session_id = uuid.UUID(int=CODEX_SESSION_ID.int + 1)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    with pytest.raises(RodexLaunchError, match="unexpected session"):
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert launcher.stopped == [(launcher.runtime, False)]
    assert lookup_codex_session_id_from_a_rodex_sessions_id(1, database) == CODEX_SESSION_ID
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(tmp_path / "stale.sock")
    assert launcher.renamed == []
    assert launcher.configured == []
    assert launcher.attached == []


@pytest.mark.parametrize(
    ("missing", "message"),
    [("codex", "Codex executable"), ("tmux", "tmux executable")],
)
@pytest.mark.parametrize("arguments", [[], ["_create"]], ids=["bare", "explicit"])
def test_run_does_not_create_a_session_when_a_prerequisite_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    message: str,
    arguments: list[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: None if command == missing else f"/usr/bin/{command}",
    )

    with pytest.raises(RodexLaunchError, match=message):
        run(arguments, database_path=database)

    assert not database.exists()


@pytest.mark.parametrize("arguments", [[], ["_create"]], ids=["bare", "explicit"])
def test_database_failure_stops_the_unregistered_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "rodex.cli.create_a_rodex_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database failed")),
    )

    with pytest.raises(RuntimeError, match="database failed"):
        run(
            arguments,
            database_path=tmp_path / "db.sqlite3",
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert launcher.stopped == [(launcher.runtime, False)]
    assert launcher.attached == []


def test_registration_confirmation_failure_stops_the_pending_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug", lambda _count: "automatic-beluga"
    )
    monkeypatch.setattr(
        launcher,
        "confirm_runtime_registration",
        lambda _runtime: (_ for _ in ()).throw(RodexRuntimeError("confirm failed")),
    )

    with pytest.raises(RodexRuntimeError, match="confirm failed"):
        run(
            ["_create"],
            database_path=tmp_path / "rodex.sqlite3",
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert launcher.stopped == [(launcher.runtime, False)]
    assert launcher.attached == []


def test_project_root_launcher_is_executable_and_uses_the_project_environment() -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / "rodex"

    assert os.access(launcher, os.X_OK)
    contents = launcher.read_text(encoding="utf-8")
    assert 'uv run --project "$RODEX_PROJECT_DIR" rodex "$@"' in contents


def test_main_prints_actionable_multiline_resume_guidance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "rodex.cli.run",
        lambda: (_ for _ in ()).throw(
            RodexSessionError(
                "Codex session already belongs to Rodex sturdy-warthog.\n"
                "Resume with: rodex sturdy-warthog"
            )
        ),
    )

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 1
    assert capsys.readouterr().err == (
        "rodex: Codex session already belongs to Rodex sturdy-warthog.\n"
        "Resume with: rodex sturdy-warthog\n"
    )
