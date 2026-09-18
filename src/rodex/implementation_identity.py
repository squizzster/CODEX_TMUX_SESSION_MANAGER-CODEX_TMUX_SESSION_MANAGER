"""Exact identity for the first-party code loaded by one Rodex process."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from .version import RODEX_VERSION

_FIRST_PARTY_PACKAGES: Final = (
    "codex_cli_contract",
    "cool_name",
    "rodex",
    "rodex_registry",
    "rodex_sql",
)


def _implementation_digest() -> str:
    source_root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for package_name in _FIRST_PARTY_PACKAGES:
        package_root = source_root / package_name
        source_files = tuple(sorted(package_root.rglob("*.py")))
        if not source_files:
            raise RuntimeError(f"Rodex implementation package is unavailable: {package_name}")
        for source_file in source_files:
            relative_path = source_file.relative_to(source_root).as_posix().encode()
            digest.update(len(relative_path).to_bytes(4, "big"))
            digest.update(relative_path)
            content = source_file.read_bytes()
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    return digest.hexdigest()


# Calculated once per process. A long-running process therefore retains the
# identity of the code it loaded even if an editable checkout changes beneath it.
RODEX_IMPLEMENTATION_ID: Final = f"{RODEX_VERSION}+sha256.{_implementation_digest()}"
