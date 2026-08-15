from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from rodex.cli import RodexLaunchError, run
from rodex.runtime import LiveRodexRuntime
from rodex_functions import (
    lookup_codex_uuid_from_a_rodex_session_id,
    lookup_rodex_tmux_session,
)

CODEX_UUID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")


class StubLauncher:
    def __init__(self, tmp_path: Path) -> None:
        self.runtime = LiveRodexRuntime(
            tmux_server_socket_path=tmp_path / "tmux.sock",
            tmux_session_name="rodex-example",
            app_server_socket_path=tmp_path / "app.sock",
            app_server_log_path=tmp_path / "app.log",
        )
        self.started: list[tuple[Path, list[str]]] = []
        self.attached: list[LiveRodexRuntime] = []
        self.stopped: list[tuple[LiveRodexRuntime, bool]] = []

    def start(
        self, workspace: Path, arguments: list[str]
    ) -> tuple[LiveRodexRuntime, uuid.UUID]:
        self.started.append((workspace, arguments))
        return self.runtime, CODEX_UUID

    def attach(self, runtime: LiveRodexRuntime) -> None:
        self.attached.append(runtime)

    def stop(self, runtime: LiveRodexRuntime, *, check: bool = True) -> None:
        self.stopped.append((runtime, check))


def available_prerequisite(command: str) -> str:
    return f"/usr/bin/{command}"


def test_run_links_real_codex_and_tmux_identities_before_attach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "rodex.sqlite3"
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    assert (
        run(
            ["--model", "example"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), ["--model", "example"])]
    assert launcher.attached == [launcher.runtime]
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert lookup_codex_uuid_from_a_rodex_session_id(1, database) == CODEX_UUID
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(
        launcher.runtime.tmux_server_socket_path
    )
    assert tmux_link.tmux_session_name == "rodex-example"
    output = capsys.readouterr().out
    assert f"-> Codex {CODEX_UUID}" in output


@pytest.mark.parametrize(
    ("missing", "message"),
    [("codex", "Codex executable"), ("tmux", "tmux executable")],
)
def test_run_does_not_create_a_session_when_a_prerequisite_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    message: str,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: None if command == missing else f"/usr/bin/{command}",
    )

    with pytest.raises(RodexLaunchError, match=message):
        run([], database_path=database)

    assert not database.exists()


def test_database_failure_stops_the_unregistered_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = StubLauncher(tmp_path)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)
    monkeypatch.setattr(
        "rodex.cli.create_a_rodex_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database failed")),
    )

    with pytest.raises(RuntimeError, match="database failed"):
        run([], database_path=tmp_path / "db.sqlite3", launcher=launcher)  # type: ignore[arg-type]

    assert launcher.stopped == [(launcher.runtime, False)]
    assert launcher.attached == []


def test_project_root_launcher_is_executable_and_uses_the_project_environment() -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / "rodex"

    assert os.access(launcher, os.X_OK)
    contents = launcher.read_text(encoding="utf-8")
    assert 'uv run --project "$RODEX_PROJECT_DIR" rodex "$@"' in contents
