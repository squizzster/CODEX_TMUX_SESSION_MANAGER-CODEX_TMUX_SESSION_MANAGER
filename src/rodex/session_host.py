"""Internal executable entry point for the process hosted inside tmux."""

from __future__ import annotations

from collections.abc import Sequence

from .process_contracts import SessionHostConfig
from .runtime import run_session_host


def main(argv: Sequence[str] | None = None) -> int:
    return run_session_host(SessionHostConfig.parse(None if argv is None else list(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
