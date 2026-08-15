from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from codex_tmux_session_manager import cli
from codex_tmux_session_manager.cli import run
from codex_tmux_session_manager.tmux import Session, SessionError, TmuxSessions


class StubSessions:
    def __init__(self, sessions: list[Session] | None = None) -> None:
        self.sessions = sessions or []
        self.started: list[tuple[str, Path, str | None]] = []
        self.attached: list[str] = []
        self.stopped: list[str] = []
        self.error: SessionError | None = None

    def list(self) -> list[Session]:
        return self.sessions

    def start(self, name: str, cwd: Path, prompt: str | None) -> None:
        self.started.append((name, cwd, prompt))

    def attach(self, name: str) -> None:
        if self.error:
            raise self.error
        self.attached.append(name)

    def stop(self, name: str) -> None:
        self.stopped.append(name)


def as_manager(stub: StubSessions) -> TmuxSessions:
    return cast(TmuxSessions, stub)


def test_doctor_reports_available_prerequisites(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "prerequisite_status", lambda: [("tmux", "/bin/tmux"), ("codex", "/bin/codex")]
    )

    assert run(["doctor"]) == 0
    assert "tmux  ok" in capsys.readouterr().out


def test_doctor_fails_when_a_prerequisite_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "prerequisite_status", lambda: [("tmux", "/bin/tmux"), ("codex", None)]
    )

    assert run(["doctor"]) == 1
    assert "codex missing" in capsys.readouterr().out


def test_list_renders_managed_sessions(capsys: pytest.CaptureFixture[str]) -> None:
    sessions = StubSessions([Session(name="research", attached=False, windows=1)])

    assert run(["list"], sessions=as_manager(sessions)) == 0
    output = capsys.readouterr().out
    assert "research" in output
    assert "detached" in output


def test_list_reports_an_empty_inventory(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["list"], sessions=as_manager(StubSessions())) == 0
    assert capsys.readouterr().out == "No managed sessions.\n"


def test_start_forwards_options_and_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sessions = StubSessions()
    monkeypatch.setattr(
        cli, "prerequisite_status", lambda: [("tmux", "/bin/tmux"), ("codex", "/bin/codex")]
    )

    assert (
        run(
            ["start", "alpha", "--cwd", str(tmp_path), "--prompt", "hello", "--attach"],
            sessions=as_manager(sessions),
        )
        == 0
    )
    assert sessions.started == [("alpha", tmp_path, "hello")]
    assert sessions.attached == ["alpha"]
    assert "Started 'alpha'" in capsys.readouterr().out


def test_start_reports_missing_prerequisites(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "prerequisite_status", lambda: [("tmux", None), ("codex", None)]
    )

    assert run(["start", "alpha"], sessions=as_manager(StubSessions())) == 2
    assert "missing prerequisite(s): tmux, codex" in capsys.readouterr().err


def test_attach_forwards_the_session_name() -> None:
    sessions = StubSessions()

    assert run(["attach", "alpha"], sessions=as_manager(sessions)) == 0
    assert sessions.attached == ["alpha"]


def test_stop_forwards_name_and_reports_success(capsys: pytest.CaptureFixture[str]) -> None:
    sessions = StubSessions()

    assert run(["stop", "alpha"], sessions=as_manager(sessions)) == 0
    assert sessions.stopped == ["alpha"]
    assert capsys.readouterr().out == "Stopped 'alpha'\n"


def test_session_errors_are_reported_without_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sessions = StubSessions()
    sessions.error = SessionError("cannot attach")

    assert run(["attach", "alpha"], sessions=as_manager(sessions)) == 2
    assert capsys.readouterr().err == "error: cannot attach\n"
