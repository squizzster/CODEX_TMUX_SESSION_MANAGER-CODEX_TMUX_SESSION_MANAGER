"""Resolve caller-selected Codex source locations without acquiring services."""

import os
from pathlib import Path


def default_codex_sessions_root() -> Path:
    configured = os.environ.get("RODEX_CODEX_SESSIONS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    codex_root = os.environ.get("CODEX_HOME")
    root = Path(codex_root).expanduser() if codex_root else Path.home() / ".codex"
    return (root / "sessions").resolve()
