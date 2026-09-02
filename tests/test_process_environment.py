from __future__ import annotations

import os
from pathlib import Path

import pytest

import rodex.environment_exec as environment_exec_module
from rodex.process_environment import (
    exact_environment_exec_command,
    select_exact_process_environment,
    user_process_environment,
    validated_user_environment_entries,
)


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


def test_exact_environment_command_carries_names_but_no_values() -> None:
    environment = {
        "PATH": "/project/bin:/usr/bin",
        "SECRET_VALUE": "do-not-put-this-in-argv",
        "SHELL": "/bin/private-shell",
        "TERM_PROGRAM": "caller-terminal",
    }

    entries = validated_user_environment_entries(environment)
    command = exact_environment_exec_command(
        "/opt/rodex/bin/python",
        tuple(name for name, _value in entries),
        ("/opt/rodex/bin/python", "-m", "rodex.session_host"),
    )

    assert command[:4] == (
        "/opt/rodex/bin/python",
        "-I",
        "-m",
        "rodex.environment_exec",
    )
    assert command[-4:] == (
        "--",
        "/opt/rodex/bin/python",
        "-m",
        "rodex.session_host",
    )
    assert "do-not-put-this-in-argv" not in command
    assert "--environment-name=SECRET_VALUE" in command
    assert "SHELL" not in dict(entries)
    assert "TERM_PROGRAM" not in dict(entries)
    for tmux_name in ("SHELL", "TERM", "TERM_PROGRAM", "TMUX", "TMUX_PANE"):
        assert f"--environment-name={tmux_name}" in command


def test_exact_environment_command_preserves_empty_argv_and_option_shaped_names() -> None:
    command = exact_environment_exec_command(
        "/usr/bin/python3",
        ("-BAD", "--", "-h"),
        ("/usr/bin/probe", "", "--flag"),
    )

    assert "--environment-name=-BAD" in command
    assert "--environment-name=--" in command
    assert "--environment-name=-h" in command
    assert command[-4:] == ("--", "/usr/bin/probe", "", "--flag")


def test_exact_environment_selection_removes_every_ambient_name() -> None:
    assert select_exact_process_environment(
        {
            "EMPTY": "",
            "PRESERVED": "line one\nline two",
            "STALE": "must disappear",
        },
        ("PRESERVED", "ABSENT", "EMPTY"),
    ) == {
        "EMPTY": "",
        "PRESERVED": "line one\nline two",
    }


def test_environment_exec_replaces_the_process_with_only_authorized_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class Executed(Exception):
        pass

    def execvpe(
        executable: str,
        command: list[str],
        environment: dict[str, str],
    ) -> None:
        observed.update(
            executable=executable,
            command=command,
            environment=environment,
        )
        raise Executed

    monkeypatch.setenv("PRESERVED", "caller-value")
    monkeypatch.setenv("STALE", "shared-server-value")
    monkeypatch.setattr(environment_exec_module.os, "execvpe", execvpe)

    with pytest.raises(Executed):
        environment_exec_module.main(
            (
                "--environment-name",
                "PRESERVED",
                "--environment-name",
                "ABSENT",
                "--",
                "/usr/bin/probe",
                "argument",
            )
        )

    assert observed == {
        "executable": "/usr/bin/probe",
        "command": ["/usr/bin/probe", "argument"],
        "environment": {"PRESERVED": "caller-value"},
    }


def test_environment_exec_accepts_option_shaped_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, str] = {}

    class Executed(Exception):
        pass

    def execvpe(
        _executable: str,
        _command: list[str],
        environment: dict[str, str],
    ) -> None:
        observed.update(environment)
        raise Executed

    for name in ("-BAD", "--", "-h"):
        monkeypatch.setenv(name, f"value-for-{name}")
    monkeypatch.setattr(environment_exec_module.os, "execvpe", execvpe)

    with pytest.raises(Executed):
        environment_exec_module.main(
            (
                "--environment-name=-BAD",
                "--environment-name=--",
                "--environment-name=-h",
                "--",
                "/usr/bin/probe",
                "",
            )
        )

    assert observed == {
        "-BAD": "value-for--BAD",
        "--": "value-for---",
        "-h": "value-for--h",
    }


@pytest.mark.parametrize(
    "environment",
    (
        {"": "value"},
        {"BAD=NAME": "value"},
        {"BAD\x00NAME": "value"},
        {"NAME": "bad\x00value"},
    ),
)
def test_environment_transport_rejects_entries_the_os_cannot_represent(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="invalid entry"):
        validated_user_environment_entries(environment)


def test_exact_environment_command_rejects_a_non_text_name() -> None:
    with pytest.raises(ValueError, match="invalid name"):
        exact_environment_exec_command(
            "/usr/bin/python3",
            ("PATH", 7),  # type: ignore[arg-type]
            ("/usr/bin/probe",),
        )
