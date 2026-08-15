from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codex_tmux_session_manager.tmux import SessionError, TmuxSessions, tmux_session_name


class RecordingRunner:
    def __init__(self, returncodes: list[int] | None = None, stdout: str = "") -> None:
        self.calls: list[list[str]] = []
        self.options: list[dict[str, object]] = []
        self.returncodes = iter(returncodes or [])
        self.stdout = stdout

    def __call__(
        self, command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        self.options.append(options)
        return subprocess.CompletedProcess(
            command, next(self.returncodes, 0), stdout=self.stdout, stderr=""
        )


def test_session_names_are_namespaced_and_validated() -> None:
    assert tmux_session_name("filings_1") == "codex-filings_1"
    with pytest.raises(SessionError, match="session name"):
        tmux_session_name("spaces are unsafe")


def test_start_creates_session_in_workspace_and_launches_codex(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=[1, 0, 0, 0])
    sessions = TmuxSessions(runner)

    sessions.start("alpha", tmp_path, "inspect this repo")

    assert runner.calls == [
        ["tmux", "has-session", "-t", "codex-alpha"],
        ["tmux", "new-session", "-d", "-s", "codex-alpha", "-c", str(tmp_path)],
        ["tmux", "send-keys", "-t", "codex-alpha", "-l", "codex 'inspect this repo'"],
        ["tmux", "send-keys", "-t", "codex-alpha", "Enter"],
    ]


def test_attach_uses_the_live_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    runner = RecordingRunner(returncodes=[0, 0])

    TmuxSessions(runner).attach("alpha")

    assert runner.calls[-1] == ["tmux", "attach-session", "-t", "codex-alpha"]
    assert "capture_output" not in runner.options[-1]


def test_list_filters_unmanaged_tmux_sessions() -> None:
    runner = RecordingRunner(
        stdout="shell\t1\t2\ncodex-beta\t0\t1\ncodex-alpha\t1\t3\n"
    )

    found = TmuxSessions(runner).list()

    assert [(session.name, session.attached, session.windows) for session in found] == [
        ("alpha", True, 3),
        ("beta", False, 1),
    ]
