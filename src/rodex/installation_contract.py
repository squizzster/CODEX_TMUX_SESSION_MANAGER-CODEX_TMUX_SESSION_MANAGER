"""Small shared contract for retained Rodex installations."""

from __future__ import annotations

from typing import Final

FIRST_PARTY_PACKAGES: Final = (
    "codex_cli_contract",
    "cool_name",
    "rodex",
    "rodex_registry",
    "rodex_sql",
)
INSTALLATION_MANIFEST: Final = "rodex-installation.json"
INSTALLATION_SCHEMA: Final = "rodex-installation-v1"


def is_installation_key(value: object) -> bool:
    """Return whether *value* is one opaque retained-installation key."""
    return isinstance(value, str) and len(value) == 64 and not set(value).difference("0123456789abcdef")
