"""Allocate a Rodex identity and then hand the terminal to Codex."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from rodex_functions import (
    RodexSessionError,
    create_a_rodex_session,
    default_rodex_database_path,
)

ProcessExecutor = Callable[[str, list[str], Mapping[str, str]], object]


class RodexLaunchError(RuntimeError):
    """Rodex could not launch its underlying Codex process."""


def run(
    argv: Sequence[str] | None = None,
    *,
    database_path: str | os.PathLike[str] | None = None,
    executor: ProcessExecutor = os.execvpe,
) -> int:
    """Create a registered session and replace Rodex with the Codex CLI."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    configured_binary = os.environ.get("RODEX_CODEX_BINARY", "codex")
    codex_binary = shutil.which(configured_binary)
    if codex_binary is None:
        raise RodexLaunchError(f"Codex executable was not found: {configured_binary}")

    resolved_database = (
        Path(database_path).expanduser().resolve()
        if database_path is not None
        else default_rodex_database_path()
    )
    session = create_a_rodex_session(resolved_database)
    environment = os.environ.copy()
    environment["RODEX_SESSION_ID"] = str(session.id)
    environment["RODEX_SESSION_UUID"] = str(session.rodex_uuid)
    environment["RODEX_DATABASE_PATH"] = str(resolved_database)

    print(f"Rodex session {session.rodex_uuid} (id {session.id})", flush=True)
    executor(codex_binary, [configured_binary, *arguments], environment)
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (RodexLaunchError, RodexSessionError, OSError) as error:
        print(f"rodex: {error}", file=sys.stderr)
        raise SystemExit(127) from error
