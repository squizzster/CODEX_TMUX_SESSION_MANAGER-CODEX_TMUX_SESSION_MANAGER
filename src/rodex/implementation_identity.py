"""Opaque identity for the retained installation loaded by this process."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Final

from .installation_contract import FIRST_PARTY_PACKAGES as FIRST_PARTY_PACKAGES
from .installation_contract import INSTALLATION_MANIFEST, INSTALLATION_SCHEMA, is_installation_key
from .version import RODEX_VERSION


def _development_installation_key(source_root: Path) -> str:
    """Namespace direct in-process development use without reading source contents."""
    location = f"{source_root.resolve()}\0{Path(sys.prefix).resolve()}\0{RODEX_VERSION}"
    return hashlib.sha256(location.encode()).hexdigest()


def _loaded_installation_key() -> str:
    source_root = Path(__file__).resolve().parents[1]
    installation_root = source_root.parent
    manifest_path = installation_root / INSTALLATION_MANIFEST
    if not manifest_path.exists():
        return _development_installation_key(source_root)
    try:
        manifest = json.loads(manifest_path.read_text())
        key = manifest.get("implementation")
    except (OSError, ValueError, AttributeError) as error:
        raise RuntimeError(f"retained Rodex installation manifest is invalid: {manifest_path}") from error
    if manifest.get("schema") != INSTALLATION_SCHEMA or not is_installation_key(key) or installation_root.name != key:
        raise RuntimeError(f"retained Rodex installation identity is invalid: {manifest_path}")
    return key


RODEX_IMPLEMENTATION_KEY: Final = _loaded_installation_key()
# Compatibility name used by retained pre-change handoff adapters. The value is
# now an opaque installation key, not a digest of executable contents.
RODEX_IMPLEMENTATION_SHA256: Final = RODEX_IMPLEMENTATION_KEY
RODEX_IMPLEMENTATION_ID: Final = f"{RODEX_VERSION}+installation.{RODEX_IMPLEMENTATION_KEY}"
