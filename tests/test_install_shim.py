from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

LAUNCHERS = ["rodex", "usr/local/bin/rodex"]


def test_usr_local_bin_shim_runs_rodex_from_the_project_after_copy(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    source_shim = project_root / "usr" / "local" / "bin" / "rodex"
    installed_shim = tmp_path / "usr" / "local" / "bin" / "rodex"
    installed_shim.parent.mkdir(parents=True)
    shutil.copy2(source_shim, installed_shim)

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    capture_path = tmp_path / "uv-arguments"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$RODEX_TEST_CAPTURE"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    environment = os.environ.copy()
    environment.pop("RODEX_PROJECT_DIR", None)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["RODEX_TEST_CAPTURE"] = str(capture_path)

    subprocess.run(
        [installed_shim, "_running"],
        check=True,
        env=environment,
    )

    assert capture_path.read_text(encoding="utf-8").splitlines() == [
        "run",
        "--project",
        str(project_root.resolve()),
        "rodex",
        "_running",
    ]


@pytest.mark.parametrize("launcher_relative", LAUNCHERS)
def test_launchers_route_bare_rodex_to_managed_tmux_before_database_access(
    tmp_path: Path,
    launcher_relative: str,
) -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / launcher_relative
    database = tmp_path / "rodex.sqlite3"
    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(project_root)
    environment["RODEX_DATABASE_PATH"] = str(database)
    environment["RODEX_CODEX_BINARY"] = "/bin/true"
    environment["RODEX_TMUX_BINARY"] = "rodex-test-missing-tmux"

    result = subprocess.run(
        [launcher],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 127
    assert result.stdout == ""
    assert "tmux executable was not found: rodex-test-missing-tmux" in result.stderr
    assert not database.exists()


@pytest.mark.parametrize("launcher_relative", LAUNCHERS)
def test_launchers_preserve_nonempty_codex_passthrough_without_tmux(
    tmp_path: Path,
    launcher_relative: str,
) -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / launcher_relative
    database = tmp_path / "rodex.sqlite3"
    fake_codex = tmp_path / "codex-probe"
    fake_codex.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    fake_codex.chmod(0o755)
    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(project_root)
    environment["RODEX_DATABASE_PATH"] = str(database)
    environment["RODEX_CODEX_BINARY"] = str(fake_codex)
    environment["RODEX_TMUX_BINARY"] = "rodex-test-missing-tmux"

    result = subprocess.run(
        [launcher, "exec", "--json", "probe"],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["exec", "--json", "probe"]
    assert result.stderr == ""
    assert not database.exists()


@pytest.mark.parametrize("launcher_relative", LAUNCHERS)
def test_launchers_print_rodex_help_without_codex_tmux_or_database(
    tmp_path: Path,
    launcher_relative: str,
) -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / launcher_relative
    database = tmp_path / "rodex.sqlite3"
    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(project_root)
    environment["RODEX_DATABASE_PATH"] = str(database)
    environment["RODEX_CODEX_BINARY"] = "rodex-test-missing-codex"
    environment["RODEX_TMUX_BINARY"] = "rodex-test-missing-tmux"

    result = subprocess.run(
        [launcher, "_help"],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("usage: rodex [COMMAND [ARGUMENTS]]\n")
    assert "_create" in result.stdout
    assert result.stderr == ""
    assert not database.exists()


def test_usr_local_bin_shim_reports_a_missing_project(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    shim = project_root / "usr" / "local" / "bin" / "rodex"
    missing_project = tmp_path / "missing-rodex"
    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(missing_project)

    result = subprocess.run(
        [shim],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert f"project not found at {missing_project}" in result.stderr
    assert "set RODEX_PROJECT_DIR" in result.stderr


def test_usr_local_bin_shim_rejects_group_writable_project_code(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='probe'\n")
    insecure_source = project_root / "probe.py"
    insecure_source.write_text("print('unsafe')\n", encoding="utf-8")
    insecure_source.chmod(0o664)
    shim = Path(__file__).parents[1] / "usr" / "local" / "bin" / "rodex"
    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(project_root)

    result = subprocess.run(
        [shim, "_running"],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "refusing group/world-writable project path" in result.stderr
    assert str(insecure_source) in result.stderr
