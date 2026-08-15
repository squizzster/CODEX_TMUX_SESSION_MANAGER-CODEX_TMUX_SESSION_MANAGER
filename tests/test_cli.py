from __future__ import annotations

from codex_tmux_session_manager.cli import run
from codex_tmux_session_manager.tmux import Session


class StubSessions:
    def list(self) -> list[Session]:
        return [Session(name="research", attached=False, windows=1)]


def test_list_renders_managed_sessions(capsys: object) -> None:
    assert run(["list"], sessions=StubSessions()) == 0  # type: ignore[arg-type]
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "research" in output
    assert "detached" in output
