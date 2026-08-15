from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from rodex.cli import RodexLaunchError, run
from rodex_functions import lookup_id_from_a_rodex_uuid


def test_run_allocates_session_and_forwards_arguments_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr("rodex.cli.shutil.which", lambda command: "/usr/bin/codex")
    observed: dict[str, object] = {}

    def executor(executable: str, arguments: list[str], environment: object) -> None:
        observed.update(executable=executable, arguments=arguments, environment=environment)

    assert run(["--model", "example"], database_path=database, executor=executor) == 0

    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert observed["executable"] == "/usr/bin/codex"
    assert observed["arguments"] == ["codex", "--model", "example"]
    assert environment["RODEX_SESSION_ID"] == "1"
    assert lookup_id_from_a_rodex_uuid(environment["RODEX_SESSION_UUID"], database) == 1
    assert "Rodex session" in capsys.readouterr().out


def test_run_does_not_create_a_session_when_codex_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr("rodex.cli.shutil.which", lambda command: None)

    with pytest.raises(RodexLaunchError, match="not found"):
        run([], database_path=database)

    assert not database.exists()


def test_run_honours_configured_codex_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RODEX_CODEX_BINARY", "codex-test-double")
    monkeypatch.setattr(
        "rodex.cli.shutil.which",
        lambda command: (
            "/tmp/codex-test-double" if command == "codex-test-double" else None
        ),
    )
    observed: list[object] = []

    run(
        ["hello"],
        database_path=tmp_path / "db.sqlite3",
        executor=lambda *arguments: observed.extend(arguments),
    )

    assert observed[0] == "/tmp/codex-test-double"
    assert observed[1] == ["codex-test-double", "hello"]


def test_project_root_launcher_is_executable_and_runs_end_to_end(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / "rodex"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$RODEX_SESSION_ID|$RODEX_SESSION_UUID|$*\"\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    database = tmp_path / "launcher.sqlite3"
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["RODEX_DATABASE_PATH"] = str(database)

    result = subprocess.run(
        [launcher, "first prompt"],
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )

    output_lines = result.stdout.splitlines()
    child_identity = output_lines[-1].split("|")
    assert os.access(launcher, os.X_OK)
    assert child_identity[0] == "1"
    assert child_identity[2] == "first prompt"
    assert lookup_id_from_a_rodex_uuid(child_identity[1], database) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM rodex_sessions").fetchone() == (1,)
