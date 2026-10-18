from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

LAUNCHERS = ["rodex", "usr/local/bin/rodex"]
PREPARATION_MESSAGES = (
    "rodex: Preparing a new local installation; copying code and dependencies...\nrodex: Local installation ready.\n"
)


def _write_successful_python(environment_root: Path) -> Path:
    python = environment_root / "bin" / "python3"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(
        '#!/bin/sh\nif [ "${1-}" = -I ]; then\n    exit 0\nfi\nexec /bin/sh "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    return python


def _write_fake_uv(uv: Path, tool_root: Path) -> Path:
    uv.parent.mkdir(parents=True, exist_ok=True)
    uv.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$RODEX_TEST_UV_CALLS"\n'
        'if [ "${1-}" = --version ]; then\n'
        "    printf 'uv 1.0.0-test\\n'\n"
        "    exit 0\n"
        "fi\n"
        'if [ "${1-} ${2-}" = "tool dir" ]; then\n'
        f"    printf '%s\\n' '{tool_root}'\n"
        "    exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return uv


def _write_importable_python(python: Path, label: str) -> Path:
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(
        "#!/bin/sh\n"
        'case "${4-}" in\n'
        "    *'sys.argv[0] = \"rodex\"'*)\n"
        "        shift 4\n"
        f"        printf '{label}:%s\\n' \"$*\"\n"
        "        ;;\n"
        "    *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return python


def _write_python_without_rodex(python: Path) -> Path:
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(
        "#!/bin/sh\n"
        'case "${4-}" in\n'
        "    *'sys.argv[0] = \"rodex\"'*)\n"
        "        printf \"ModuleNotFoundError: No module named 'rodex'\\n\" >&2\n"
        "        exit 1\n"
        "        ;;\n"
        "    *'from rodex import main'*) exit 1 ;;\n"
        "    *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return python


def test_project_root_launcher_does_not_create_a_rodex_virtualenv_context(
    tmp_path: Path,
) -> None:
    project = tmp_path / "rodex-project"
    project.mkdir()
    launcher = project / "rodex"
    shutil.copy2(Path(__file__).parents[1] / "rodex", launcher)
    entrypoint = project / ".venv" / "bin" / "rodex"
    entrypoint.parent.mkdir(parents=True)
    capture_path = tmp_path / "environment"
    entrypoint.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n%s\\n%s\\n\' "${VIRTUAL_ENV-unset}" "$PATH" '
        '"${UV_RUN_RECURSION_DEPTH-unset}" > "$RODEX_TEST_CAPTURE"\n',
        encoding="utf-8",
    )
    entrypoint.chmod(0o755)
    user_virtual_environment = tmp_path / "project-xyz" / ".venv"
    caller_path = f"{user_virtual_environment}/bin:/usr/local/bin:/usr/bin"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": caller_path,
            "RODEX_TEST_CAPTURE": str(capture_path),
            "UV_RUN_RECURSION_DEPTH": "4",
            "VIRTUAL_ENV": str(user_virtual_environment),
        }
    )

    subprocess.run([launcher, "_running"], check=True, env=environment)

    assert capture_path.read_text(encoding="utf-8").splitlines() == [
        str(user_virtual_environment),
        caller_path,
        "4",
    ]


def test_usr_local_bin_shim_runs_rodex_from_the_project_after_copy(
    tmp_path: Path,
) -> None:
    source_shim = Path(__file__).parents[1] / "usr" / "local" / "bin" / "rodex"
    installed_shim = tmp_path / "usr" / "local" / "bin" / "rodex"
    installed_shim.parent.mkdir(parents=True)
    shutil.copy2(source_shim, installed_shim)

    capture_path = tmp_path / "uv-arguments"
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='probe'\n")
    entrypoint = project / ".venv" / "bin" / "rodex"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$RODEX_TEST_CAPTURE"\n',
        encoding="utf-8",
    )
    entrypoint.chmod(0o755)
    _write_successful_python(project / ".venv")

    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(project)
    environment["RODEX_TEST_CAPTURE"] = str(capture_path)

    subprocess.run(
        [installed_shim, "_running"],
        check=True,
        env=environment,
    )

    assert capture_path.read_text(encoding="utf-8").splitlines() == [
        "_running",
    ]


@pytest.mark.parametrize("launcher_relative", LAUNCHERS)
def test_launchers_route_bare_rodex_to_managed_tmux_before_database_access(
    tmp_path: Path,
    launcher_relative: str,
) -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / launcher_relative
    state_home = tmp_path / "state"
    database = state_home / "rodex" / "rodex-v22.sqlite3"
    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(project_root)
    environment["XDG_STATE_HOME"] = str(state_home)
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
def test_launchers_route_an_initial_prompt_to_managed_tmux(
    tmp_path: Path,
    launcher_relative: str,
) -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / launcher_relative
    state_home = tmp_path / "state"
    database = state_home / "rodex" / "rodex-v22.sqlite3"
    fake_codex = tmp_path / "codex-must-not-run-directly"
    fake_codex.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8")
    fake_codex.chmod(0o755)
    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(project_root)
    environment["XDG_STATE_HOME"] = str(state_home)
    environment["RODEX_CODEX_BINARY"] = str(fake_codex)
    environment["RODEX_TMUX_BINARY"] = "rodex-test-missing-tmux"

    result = subprocess.run(
        [launcher, "Project: CODEX_TMUX_SESSION_MANAGER"],
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
def test_launchers_preserve_codex_subcommand_passthrough_without_tmux(
    tmp_path: Path,
    launcher_relative: str,
) -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / launcher_relative
    state_home = tmp_path / "state"
    database = state_home / "rodex" / "rodex-v22.sqlite3"
    fake_codex = tmp_path / "codex-probe"
    fake_codex.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    fake_codex.chmod(0o755)
    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(project_root)
    environment["XDG_STATE_HOME"] = str(state_home)
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
    assert result.stderr == PREPARATION_MESSAGES
    assert not database.exists()


@pytest.mark.parametrize("launcher_relative", LAUNCHERS)
def test_launchers_print_rodex_help_without_codex_tmux_or_database(
    tmp_path: Path,
    launcher_relative: str,
) -> None:
    project_root = Path(__file__).parents[1]
    launcher = project_root / launcher_relative
    state_home = tmp_path / "state"
    database = state_home / "rodex" / "rodex-v22.sqlite3"
    environment = os.environ.copy()
    environment["RODEX_PROJECT_DIR"] = str(project_root)
    environment["XDG_STATE_HOME"] = str(state_home)
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
    assert result.stdout.startswith("usage: rodex [CODEX_OPTIONS] [PROMPT]\n")
    assert "_create" in result.stdout
    assert result.stderr == PREPARATION_MESSAGES
    assert not database.exists()


def test_usr_local_bin_shim_surfaces_missing_rodex_module_when_checkout_is_missing(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    shim = project_root / "usr" / "local" / "bin" / "rodex"
    missing_project = tmp_path / "missing-rodex"
    executable_directory = tmp_path / "bin"
    _write_python_without_rodex(executable_directory / "python3.11")
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(tmp_path / "empty-home"),
            "PATH": str(executable_directory),
            "RODEX_PROJECT_DIR": str(missing_project),
            "RODEX_UV": str(tmp_path / "missing-uv"),
        }
    )
    environment.pop("RODEX_ENVIRONMENT", None)
    environment.pop("RODEX_PYTHON", None)
    environment.pop("UV_TOOL_DIR", None)
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("VIRTUAL_ENV_PROMPT", None)
    environment.pop("XDG_DATA_HOME", None)

    result = subprocess.run(
        [shim],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "ModuleNotFoundError: No module named 'rodex'\n"


def test_usr_local_bin_shim_does_not_scan_unrelated_project_content(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='probe'\n")
    unrelated_source = project_root / "probe.py"
    unrelated_source.write_text("print('ordinary editable checkout')\n", encoding="utf-8")
    unrelated_source.chmod(0o666)
    entrypoint = project_root / ".venv/bin/rodex"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text('#!/bin/sh\nprintf "direct-entrypoint\\n"\n', encoding="utf-8")
    entrypoint.chmod(0o755)
    _write_successful_python(project_root / ".venv")
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

    assert result.returncode == 0
    assert result.stdout == "direct-entrypoint\n"
    assert result.stderr == ""


def test_usr_local_bin_shim_uses_an_existing_uv_tool_environment(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "shared-project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='probe'\n", encoding="utf-8")
    checkout_entrypoint = project_root / ".venv/bin/rodex"
    checkout_entrypoint.parent.mkdir(parents=True)
    checkout_entrypoint.write_text('#!/bin/sh\nprintf "wrong-environment\\n"\n', encoding="utf-8")
    checkout_entrypoint.chmod(0o755)
    checkout_python = project_root / ".venv/bin/python3"
    checkout_python.write_text("#!/bin/sh\nexit 126\n", encoding="utf-8")
    checkout_python.chmod(0o755)

    tool_root = tmp_path / "uv-tools"
    tool_environment = tool_root / "codex-tmux-session-manager"
    tool_entrypoint = tool_environment / "bin/rodex"
    tool_entrypoint.parent.mkdir(parents=True)
    tool_entrypoint.write_text('#!/bin/sh\nprintf "uv-tool:%s\\n" "$*"\n', encoding="utf-8")
    tool_entrypoint.chmod(0o755)
    _write_successful_python(tool_environment)
    home = tmp_path / "home"
    _write_fake_uv(home / ".local/bin/uv", tool_root)
    uv_calls = tmp_path / "uv-calls"
    shim = Path(__file__).parents[1] / "usr/local/bin/rodex"
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "RODEX_PROJECT_DIR": str(project_root),
            "RODEX_TEST_UV_CALLS": str(uv_calls),
        }
    )
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("VIRTUAL_ENV_PROMPT", None)

    result = subprocess.run(
        [shim, "named-session"],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "uv-tool:named-session\n"
    assert result.stderr == ""
    assert uv_calls.read_text(encoding="utf-8").splitlines() == ["--version", "tool dir"]


def test_usr_local_bin_shim_uses_an_explicit_existing_environment_without_uv(
    tmp_path: Path,
) -> None:
    missing_project = tmp_path / "missing-shared-project"

    user_environment = tmp_path / "user-environment"
    user_entrypoint = user_environment / "bin/rodex"
    user_entrypoint.parent.mkdir(parents=True)
    user_entrypoint.write_text('#!/bin/sh\nprintf "reused:%s\\n" "$*"\n', encoding="utf-8")
    user_entrypoint.chmod(0o755)
    _write_successful_python(user_environment)

    shim = Path(__file__).parents[1] / "usr/local/bin/rodex"
    environment = os.environ.copy()
    environment.update(
        {
            "RODEX_PROJECT_DIR": str(missing_project),
            "RODEX_UV": str(tmp_path / "must-not-run-uv"),
            "RODEX_ENVIRONMENT": str(user_environment),
        }
    )

    result = subprocess.run(
        [shim, "existing-session"],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "reused:existing-session\n"
    assert result.stderr == ""


def test_usr_local_bin_shim_uses_an_existing_explicit_python(
    tmp_path: Path,
) -> None:
    missing_project = tmp_path / "missing-shared-project"
    python = _write_importable_python(tmp_path / "system-python/python3.12", "python")
    shim = Path(__file__).parents[1] / "usr/local/bin/rodex"
    environment = os.environ.copy()
    environment.update(
        {
            "RODEX_PROJECT_DIR": str(missing_project),
            "RODEX_PYTHON": str(python),
            "RODEX_UV": str(tmp_path / "must-not-run-uv"),
        }
    )
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("VIRTUAL_ENV_PROMPT", None)

    result = subprocess.run(
        [shim, "system-session"],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "python:system-session\n"
    assert result.stderr == ""


def test_usr_local_bin_shim_surfaces_module_failure_without_modifying_user_files(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "shared-project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname='probe'\n", encoding="utf-8")
    home = tmp_path / "home"
    tool_root = tmp_path / "empty-uv-tools"
    uv_calls = tmp_path / "uv-calls"
    _write_fake_uv(home / ".local/bin/uv", tool_root)
    executable_directory = tmp_path / "bin"
    _write_python_without_rodex(executable_directory / "python3.11")
    sentinel = home / "sentinel"
    sentinel.write_text("unchanged\n", encoding="utf-8")

    def home_snapshot() -> list[tuple[str, int, bytes | None]]:
        return [
            (
                str(path.relative_to(home)),
                path.lstat().st_mode,
                path.read_bytes() if path.is_file() else None,
            )
            for path in sorted(home.rglob("*"))
        ]

    before = home_snapshot()
    shim = Path(__file__).parents[1] / "usr/local/bin/rodex"
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "PATH": str(executable_directory),
            "RODEX_PROJECT_DIR": str(project_root),
            "RODEX_TEST_UV_CALLS": str(uv_calls),
        }
    )
    environment.pop("RODEX_ENVIRONMENT", None)
    environment.pop("RODEX_PYTHON", None)
    environment.pop("VIRTUAL_ENV", None)
    environment.pop("VIRTUAL_ENV_PROMPT", None)

    result = subprocess.run(
        [shim, "unsupported-session"],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "ModuleNotFoundError: No module named 'rodex'\n"
    assert home_snapshot() == before
    assert uv_calls.read_text(encoding="utf-8").splitlines() == ["--version", "tool dir"]
