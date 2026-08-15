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


def test_attach_switches_clients_when_already_inside_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-server")
    runner = RecordingRunner(returncodes=[0, 0])

    TmuxSessions(runner).attach("alpha")

    assert runner.calls[-1] == ["tmux", "switch-client", "-t", "codex-alpha"]


def test_attach_rejects_a_missing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    runner = RecordingRunner(returncodes=[1])

    with pytest.raises(SessionError, match="does not exist"):
        TmuxSessions(runner).attach("alpha")


def test_stop_kills_an_existing_session() -> None:
    runner = RecordingRunner(returncodes=[0, 0])

    TmuxSessions(runner).stop("alpha")

    assert runner.calls[-1] == ["tmux", "kill-session", "-t", "codex-alpha"]


def test_stop_rejects_a_missing_session() -> None:
    runner = RecordingRunner(returncodes=[1])

    with pytest.raises(SessionError, match="does not exist"):
        TmuxSessions(runner).stop("alpha")


def test_list_filters_unmanaged_tmux_sessions() -> None:
    runner = RecordingRunner(stdout="shell\t1\t2\ncodex-beta\t0\t1\ncodex-alpha\t1\t3\n")

    found = TmuxSessions(runner).list()

    assert [(session.name, session.attached, session.windows) for session in found] == [
        ("alpha", True, 3),
        ("beta", False, 1),
    ]


def test_list_returns_empty_when_tmux_has_no_server() -> None:
    runner = RecordingRunner(returncodes=[1])

    assert TmuxSessions(runner).list() == []


def test_start_rejects_a_missing_workspace(tmp_path: Path) -> None:
    with pytest.raises(SessionError, match="workspace does not exist"):
        TmuxSessions(RecordingRunner()).start("alpha", tmp_path / "missing")


def test_start_rejects_an_existing_session(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=[0])

    with pytest.raises(SessionError, match="already exists"):
        TmuxSessions(runner).start("alpha", tmp_path)


def test_start_without_a_prompt_launches_plain_codex(tmp_path: Path) -> None:
    runner = RecordingRunner(returncodes=[1, 0, 0, 0])

    TmuxSessions(runner).start("alpha", tmp_path)

    assert runner.calls[-2] == ["tmux", "send-keys", "-t", "codex-alpha", "-l", "codex"]


def test_tmux_missing_from_path_becomes_a_session_error() -> None:
    def missing_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    with pytest.raises(SessionError, match="tmux is not installed"):
        TmuxSessions(missing_runner).list()


@pytest.mark.parametrize(
    ("stderr", "stdout", "expected"),
    [
        ("specific stderr", "", "specific stderr"),
        ("", "specific stdout", "specific stdout"),
        ("", "", "tmux command failed"),
    ],
)
def test_tmux_failures_preserve_the_best_available_detail(
    stderr: str, stdout: str, expected: str
) -> None:
    def failing_runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command, output=stdout, stderr=stderr)

    with pytest.raises(SessionError, match=expected):
        TmuxSessions(failing_runner).stop("alpha")
