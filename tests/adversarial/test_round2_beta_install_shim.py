from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _write_sealed_entrypoint(project: Path) -> Path:
    entrypoint = project / ".venv" / "bin" / "rodex"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text('#!/bin/sh\nprintf "sealed-environment\\n"\n', encoding="utf-8")
    entrypoint.chmod(0o755)
    (project / ".venv" / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    return entrypoint


def test_round2_installed_shim_prunes_only_nonexecuting_tool_caches(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname='probe'\n",
        encoding="utf-8",
    )
    _write_sealed_entrypoint(project)
    for relative_cache in (".pytest_cache", ".ruff_cache"):
        cache = project / relative_cache
        cache.mkdir(parents=True)
        generated = cache / "generated-entry"
        generated.write_text("not trusted project source\n", encoding="utf-8")
        generated.chmod(0o666)

    repository = Path(__file__).resolve().parents[2]
    shim = repository / "usr" / "local" / "bin" / "rodex"
    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(project)

    result = subprocess.run(
        [shim, "_running"],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "sealed-environment\n"


def test_round2_installed_shim_uses_one_complete_execution_boundary_scan() -> None:
    repository = Path(__file__).resolve().parents[2]
    contents = (repository / "usr" / "local" / "bin" / "rodex").read_text(encoding="utf-8")

    assert contents.count('/usr/bin/find "$RODEX_PROJECT_DIR"') == 1
    for generated_tree in (".venv", ".pytest_cache", ".ruff_cache"):
        assert generated_tree in contents
    assert '-path "$RODEX_PROJECT_DIR/.venv/*" -prune' not in contents
    assert "-path '*/__pycache__/*' -prune" not in contents


def test_round2_installed_shim_rejects_writable_site_or_bytecode(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    shim = repository / "usr" / "local" / "bin" / "rodex"
    for relative_path in (
        ".venv/lib/python3.12/site-packages/probe.py",
        "src/probe/__pycache__/probe.cpython-312.pyc",
    ):
        project = tmp_path / relative_path.replace("/", "_")
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]\nname='probe'\n")
        _write_sealed_entrypoint(project)
        insecure = project / relative_path
        insecure.parent.mkdir(parents=True, exist_ok=True)
        insecure.write_bytes(b"untrusted executable content")
        insecure.chmod(0o666)
        environment = os.environ.copy()
        environment["RODEX_PROJECT_DIR"] = str(project)

        result = subprocess.run(
            [shim, "_running"],
            check=False,
            env=environment,
            text=True,
            capture_output=True,
        )

        assert result.returncode == 1
        assert "refusing group/world-writable project path" in result.stderr
        assert str(insecure) in result.stderr


def test_round2_installed_shim_rejects_a_writable_interpreter(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='probe'\n")
    interpreter = tmp_path / "python"
    interpreter.write_text('#!/bin/sh\nexec /bin/sh "$@"\n', encoding="utf-8")
    interpreter.chmod(0o777)
    entrypoint = project / ".venv" / "bin" / "rodex"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text(f"#!{interpreter}\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    shim = Path(__file__).resolve().parents[2] / "usr" / "local" / "bin" / "rodex"
    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(project)

    result = subprocess.run(
        [shim, "_running"],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert str(interpreter) in result.stderr
