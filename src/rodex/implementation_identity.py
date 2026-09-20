"""Exact identity for executable code, dependencies and shipped configuration."""

from __future__ import annotations

import hashlib
import sys
import sysconfig
from pathlib import Path
from typing import Final

from .version import RODEX_VERSION

FIRST_PARTY_PACKAGES: Final = (
    "codex_cli_contract",
    "cool_name",
    "rodex",
    "rodex_registry",
    "rodex_sql",
)


def shipped_configuration(source_root: Path) -> Path:
    """Locate the canonical defaults in either a checkout or an installed wheel."""
    configuration = source_root.parent / "conf/hooks/user_prompt_substitutions.yaml"
    return configuration if configuration.is_file() else Path(sys.prefix) / "hooks/user_prompt_substitutions.yaml"


def implementation_digest(source_root: Path, site_packages: Path, configuration: Path | None = None) -> str:
    """Identify executable source and dependencies independently of installation paths."""
    digest = hashlib.sha256()
    digest.update(sys.version.encode())
    configuration_content = (configuration or shipped_configuration(source_root)).read_bytes()
    digest.update(len(configuration_content).to_bytes(8, "big"))
    digest.update(configuration_content)
    for package_name in FIRST_PARTY_PACKAGES:
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
    for path in sorted(site_packages.rglob("*")):
        relative = path.relative_to(site_packages)
        if (
            not path.is_file()
            or "__pycache__" in relative.parts
            or path.suffix == ".pyc"
            or relative.parts[0] in FIRST_PARTY_PACKAGES
            or relative.parts[0].startswith("codex_tmux_session_manager")
        ):
            continue
        name, content = relative.as_posix().encode(), path.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


# Calculated once per process. A long-running process therefore retains the
# identity of the code it loaded even if an editable checkout changes beneath it.
RODEX_IMPLEMENTATION_SHA256: Final = implementation_digest(
    Path(__file__).resolve().parents[1], Path(sysconfig.get_path("purelib"))
)
RODEX_IMPLEMENTATION_ID: Final = f"{RODEX_VERSION}+sha256.{RODEX_IMPLEMENTATION_SHA256}"
