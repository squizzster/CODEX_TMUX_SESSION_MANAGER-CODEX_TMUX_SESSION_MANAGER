from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


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
        [installed_shim, "running", "--json"],
        check=True,
        env=environment,
    )

    assert capture_path.read_text(encoding="utf-8").splitlines() == [
        "run",
        "--project",
        str(project_root.resolve()),
        "rodex",
        "running",
        "--json",
    ]


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
