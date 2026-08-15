from __future__ import annotations

import os
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from rodex.cli import RodexLaunchError, run
from rodex.runtime import LiveRodexRuntime, LiveTmuxSession
from rodex_functions import (
    RodexSessionsUserIdentity,
    create_a_rodex_session,
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
        self.renamed: list[tuple[LiveTmuxSession, str]] = []
        self.configured: list[LiveTmuxSession] = []
        self.attached: list[LiveTmuxSession] = []
        self.stopped: list[tuple[LiveTmuxSession, bool]] = []
        self.existing_checks: list[LiveTmuxSession] = []
        self.live = True
        self.observed_codex_uuid = CODEX_UUID

    def start(
        self, workspace: Path, arguments: list[str]
    ) -> tuple[LiveRodexRuntime, uuid.UUID]:
        self.started.append((workspace, arguments))
        return self.runtime, self.observed_codex_uuid

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
    monkeypatch.setattr(
        "cool_name.functions.coolname.generate_slug",
        lambda word_count: "automatic-beluga" if word_count == 2 else "unused-name-here",
    )

    assert (
        run(
            ["--model", "example"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )
        == 0
    )

    assert launcher.started == [(Path.cwd(), ["--model", "example"])]
    assert launcher.renamed == [(launcher.runtime, "automatic-beluga")]
    assert launcher.configured[0].tmux_session_name == "automatic-beluga"
    assert launcher.attached == launcher.configured
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert lookup_codex_uuid_from_a_rodex_session_id(1, database) == CODEX_UUID
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(
        launcher.runtime.tmux_server_socket_path
    )
    assert tmux_link.tmux_session_name == "automatic-beluga"
    output = capsys.readouterr().out
    assert f"-> Codex {CODEX_UUID}" in output
    assert "Rodex automatic-beluga" in output


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
        codex_session_uuid=CODEX_UUID,
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
        codex_session_uuid=CODEX_UUID,
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

    assert launcher.started == [(Path.cwd(), ["resume", str(CODEX_UUID)])]
    assert launcher.renamed == [(launcher.runtime, "automatic-beluga")]
    assert launcher.attached[0].tmux_session_name == "automatic-beluga"
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(
        launcher.runtime.tmux_server_socket_path
    )
    assert tmux_link.tmux_session_name == "automatic-beluga"
    assert f"Resumed Rodex automatic-beluga -> Codex {CODEX_UUID}" in (
        capsys.readouterr().out
    )


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
        codex_session_uuid=CODEX_UUID,
        tmux_server_socket_path=tmp_path / "stale.sock",
        tmux_session_name="automatic-beluga",
    )
    launcher = StubLauncher(tmp_path)
    launcher.live = False
    launcher.observed_codex_uuid = uuid.UUID(int=CODEX_UUID.int + 1)
    monkeypatch.setattr("rodex.cli.shutil.which", available_prerequisite)

    with pytest.raises(RodexLaunchError, match="unexpected session"):
        run(
            ["automatic-beluga"],
            database_path=database,
            launcher=launcher,  # type: ignore[arg-type]
        )

    assert launcher.stopped == [(launcher.runtime, False)]
    tmux_link = lookup_rodex_tmux_session(1, database)
    assert tmux_link is not None
    assert tmux_link.tmux_server_socket_path == str(tmp_path / "stale.sock")


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
