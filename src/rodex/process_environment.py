"""Keep Rodex's Python bootstrap environment out of user-facing processes."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

MANAGED_SESSION_ENVIRONMENT_VARIABLES: Final = (
    "PATH",
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
    "UV_RUN_RECURSION_DEPTH",
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


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
