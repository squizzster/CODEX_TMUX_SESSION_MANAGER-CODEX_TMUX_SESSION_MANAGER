from __future__ import annotations

import os
from pathlib import Path

from rodex.process_environment import user_process_environment


def test_rodex_bootstrap_virtualenv_is_removed_from_user_processes(
    tmp_path: Path,
) -> None:
    rodex_virtual_environment = tmp_path / "rodex" / ".venv"
    inherited = {
        "PATH": os.pathsep.join(
            (
                str(rodex_virtual_environment / "bin"),
                "/home/example/.local/bin",
                str(rodex_virtual_environment / "bin"),
                "/usr/bin",
            )
        ),
        "VIRTUAL_ENV": str(rodex_virtual_environment),
        "VIRTUAL_ENV_PROMPT": "(rodex)",
        "UV_RUN_RECURSION_DEPTH": "1",
        "USER_SETTING": "preserved",
    }

    prepared = user_process_environment(
        inherited,
        rodex_virtual_environment=rodex_virtual_environment,
    )

    assert prepared == {
        "PATH": os.pathsep.join(("/home/example/.local/bin", "/usr/bin")),
        "USER_SETTING": "preserved",
    }
    assert inherited["VIRTUAL_ENV"] == str(rodex_virtual_environment)


def test_user_project_virtualenv_is_preserved_unchanged(tmp_path: Path) -> None:
    rodex_virtual_environment = tmp_path / "rodex" / ".venv"
    user_virtual_environment = tmp_path / "project-xyz" / ".venv"
    inherited = {
        "PATH": os.pathsep.join(
            (str(user_virtual_environment / "bin"), "/usr/local/bin", "/usr/bin")
        ),
        "VIRTUAL_ENV": str(user_virtual_environment),
        "VIRTUAL_ENV_PROMPT": "(project-xyz)",
        "UV_RUN_RECURSION_DEPTH": "2",
        "USER_SETTING": "preserved",
    }

    assert (
        user_process_environment(
            inherited,
            rodex_virtual_environment=rodex_virtual_environment,
        )
        == inherited
    )
