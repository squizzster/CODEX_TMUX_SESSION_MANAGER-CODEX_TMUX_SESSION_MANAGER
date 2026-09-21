"""Publish a private, fixed Python installation before launching runtime helpers.

Preparation copies the already-installed environment locally. It never resolves,
downloads or upgrades dependencies. Published installations are never overwritten;
their interpreter paths remain usable by older daemons, hooks and observers.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import stat
import sys
import sysconfig
import tempfile
import venv
from pathlib import Path

from rodex_sql import default_rodex_state_root

from .implementation_identity import (
    FIRST_PARTY_PACKAGES,
    RODEX_IMPLEMENTATION_SHA256,
    implementation_digest,
    shipped_configuration,
)
from .process_environment import user_process_environment

INSTALLATION_MANIFEST = "rodex-installation.json"
INSTALLATION_SCHEMA = "rodex-installation-v1"


class RodexInstallationError(RuntimeError):
    """A complete installation could not be prepared or verified."""


def _project_directory() -> Path:
    return Path(__file__).resolve().parents[2]


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    observed = path.lstat()
    if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.getuid() or observed.st_mode & 0o022:
        raise RodexInstallationError(f"installation directory is not privately owned: {path}")


def _validate(root: Path, identity: str) -> Path:
    _private_directory(root)
    try:
        manifest = json.loads((root / INSTALLATION_MANIFEST).read_text())
    except (OSError, ValueError) as error:
        raise RodexInstallationError(f"installation is incomplete: {root}") from error
    if manifest != {"schema": INSTALLATION_SCHEMA, "implementation": identity}:
        raise RodexInstallationError(f"installation identity does not match: {root}")
    interpreter = root / ".venv/bin/python"
    if not interpreter.is_file():
        raise RodexInstallationError(f"installation interpreter is missing: {root}")
    return interpreter


def _require_installed_dependencies(site_packages: Path) -> None:
    """External editable dependencies would escape the fixed environment copy."""
    for path in site_packages.iterdir():
        if path.name == "codex_tmux_session_manager.pth":
            continue
        if path.suffix == ".egg-link" or path.name.startswith("__editable__"):
            raise RodexInstallationError(f"install dependencies normally before launching Rodex: {path}")
        if path.suffix == ".pth":
            for line in path.read_text().splitlines():
                if line.strip() and not line.startswith(("#", "import ", "import\t")):
                    raise RodexInstallationError(f"external dependency paths cannot be pinned: {path}")


def prepare_installation(
    store: Path,
    *,
    source_root: Path | None = None,
    site_packages: Path | None = None,
    configuration_root: Path | None = None,
) -> Path:
    """Atomically publish one source/environment identity and return its interpreter."""
    source_root = source_root or Path(__file__).resolve().parents[1]
    site_packages = site_packages or Path(sysconfig.get_path("purelib"))
    _require_installed_dependencies(site_packages)
    configuration = (
        configuration_root / "hooks/user_prompt_substitutions.yaml"
        if configuration_root is not None
        else shipped_configuration(source_root)
    )
    identity = implementation_digest(source_root, site_packages, configuration)
    _private_directory(store)
    store = store.resolve()
    destination = store / identity
    descriptor = os.open(store / f"{identity}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if destination.exists():
            return _validate(destination, identity)
        stage = Path(tempfile.mkdtemp(prefix=".preparing-", dir=store))
        try:
            for package in FIRST_PARTY_PACKAGES:
                shutil.copytree(
                    source_root / package, stage / "src" / package, ignore=shutil.ignore_patterns("__pycache__")
                )
            (stage / "conf/hooks").mkdir(parents=True)
            shutil.copy2(configuration, stage / "conf/hooks/user_prompt_substitutions.yaml")
            venv.EnvBuilder(with_pip=False, symlinks=True).create(stage / ".venv")
            library_relative = Path(sysconfig.get_path("purelib")).relative_to(sys.prefix)
            library = stage / ".venv" / library_relative
            for entry in site_packages.iterdir():
                if entry.name in FIRST_PARTY_PACKAGES or entry.name == "codex_tmux_session_manager.pth":
                    continue
                target = library / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, target, ignore=shutil.ignore_patterns("__pycache__"))
                else:
                    shutil.copy2(entry, target)
            (library / "codex_tmux_session_manager.pth").write_text(str(destination / "src") + "\n")
            if implementation_digest(stage / "src", library) != identity:
                raise RodexInstallationError("source or dependencies changed while preparing installation; retry")
            (stage / INSTALLATION_MANIFEST).write_text(
                json.dumps({"schema": INSTALLATION_SCHEMA, "implementation": identity}) + "\n"
            )
            # The shell entrypoint avoids Linux's length limit for interpreter shebangs.
            executable = stage / ".venv/bin/rodex"
            executable.write_text(
                "#!/bin/sh\nexec "
                + shlex.quote(str(destination / ".venv/bin/python"))
                + " -I -c 'from rodex import main; main()' \"$@\"\n"
            )
            executable.chmod(0o700)
            stage.rename(destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return _validate(destination, identity)
    finally:
        os.close(descriptor)


def retained_installation_interpreter() -> Path:
    """Publish or validate this implementation's immutable interpreter."""
    project = _project_directory()
    if (project / INSTALLATION_MANIFEST).exists():
        interpreter = _validate(project, RODEX_IMPLEMENTATION_SHA256)
        if Path(sys.prefix).resolve() != interpreter.parent.parent:
            raise RodexInstallationError("installation was loaded by a different Python environment")
        return interpreter
    configured = os.environ.get("RODEX_INSTALLATIONS_ROOT")
    if configured:
        store = Path(configured)
    else:
        state_root = default_rodex_state_root()
        _private_directory(state_root)
        store = state_root / "implementations"
    if not store.is_absolute():
        raise RodexInstallationError("RODEX_INSTALLATIONS_ROOT must be an absolute path")
    return prepare_installation(store)


def enter_fixed_installation() -> None:
    """Re-exec once, before CLI composition, so every descendant inherits one version."""
    interpreter = retained_installation_interpreter()
    if Path(sys.prefix).resolve() == interpreter.parent.parent:
        return
    # Remove only the checkout environment used to bootstrap Rodex. Caller-owned
    # environments and cwd remain unchanged for Codex and its tools.
    environment = user_process_environment(os.environ)
    os.execve(
        interpreter,
        [str(interpreter), "-I", "-c", "from rodex import main; main()", *sys.argv[1:]],
        environment,
    )
