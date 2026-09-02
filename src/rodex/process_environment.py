"""Keep Rodex's Python bootstrap environment out of user-facing processes."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

TMUX_OWNED_CHILD_ENVIRONMENT_VARIABLES: Final = frozenset(
    {
        "SHELL",
        "TERM",
        "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION",
        "TMUX",
        "TMUX_PANE",
    }
)


def user_process_environment(
    inherited: Mapping[str, str],
    *,
    rodex_virtual_environment: Path | None = None,
) -> dict[str, str]:
    """Return caller state without a virtualenv used only to bootstrap Rodex."""
    environment = dict(inherited)
    active_virtual_environment = environment.get("VIRTUAL_ENV")
    internal_virtual_environment = (
        Path(sys.prefix) if rodex_virtual_environment is None else rodex_virtual_environment
    )
    if not active_virtual_environment or not _same_path(
        active_virtual_environment,
        internal_virtual_environment,
    ):
        return environment

    environment.pop("VIRTUAL_ENV", None)
    environment.pop("VIRTUAL_ENV_PROMPT", None)
    environment.pop("UV_RUN_RECURSION_DEPTH", None)
    path = environment.get("PATH")
    if path is not None:
        environment["PATH"] = os.pathsep.join(
            entry
            for entry in path.split(os.pathsep)
            if not entry or not _same_path(entry, internal_virtual_environment / "bin")
        )
    return environment


def validated_user_environment_entries(
    environment: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    """Return deterministic caller-owned entries accepted by OS and tmux boundaries."""
    entries: list[tuple[str, str]] = []
    for name, value in environment.items():
        if (
            not isinstance(name, str)
            or not name
            or "=" in name
            or "\x00" in name
            or not isinstance(value, str)
            or "\x00" in value
        ):
            raise ValueError("process environment contains an invalid entry")
        if name not in TMUX_OWNED_CHILD_ENVIRONMENT_VARIABLES:
            entries.append((name, value))
    return tuple(sorted(entries))


def exact_environment_exec_command(
    python_executable: str,
    environment_names: Sequence[str],
    command: Sequence[str],
) -> tuple[str, ...]:
    """Build a value-free argv that drops ambient names before the real process."""
    if (
        not isinstance(python_executable, str)
        or not python_executable
        or not command
        or not isinstance(command[0], str)
        or not command[0]
        or any(not isinstance(argument, str) for argument in command)
    ):
        raise ValueError("exact environment execution requires non-empty executable text")
    names = set(TMUX_OWNED_CHILD_ENVIRONMENT_VARIABLES)
    for name in environment_names:
        if not isinstance(name, str) or not name or "=" in name or "\x00" in name:
            raise ValueError("exact environment execution received an invalid name")
        names.add(name)
    arguments = [python_executable, "-I", "-m", "rodex.environment_exec"]
    for name in sorted(names):
        arguments.append(f"--environment-name={name}")
    return (*arguments, "--", *command)


def select_exact_process_environment(
    inherited: Mapping[str, str],
    environment_names: Sequence[str],
) -> dict[str, str]:
    """Select only named values; absent names remain absent."""
    selected: dict[str, str] = {}
    for name in environment_names:
        if not isinstance(name, str) or not name or "=" in name or "\x00" in name:
            raise ValueError("exact environment selection received an invalid name")
        value = inherited.get(name)
        if value is not None:
            selected[name] = value
    return selected


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
