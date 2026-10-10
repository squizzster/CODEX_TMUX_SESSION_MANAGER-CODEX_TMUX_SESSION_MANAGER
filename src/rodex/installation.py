"""Publish a private, fixed Python installation before launching runtime helpers.

Preparation copies the already-installed environment locally. It never resolves,
downloads or upgrades dependencies. Published installations are never overwritten;
their interpreter paths remain usable by older daemons, hooks and observers.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shlex
import shutil
import stat
import sys
import sysconfig
import tempfile
import venv
from pathlib import Path

from rodex_sql import default_rodex_state_root

from .installation_contract import (
    FIRST_PARTY_PACKAGES,
    INSTALLATION_MANIFEST,
    INSTALLATION_SCHEMA,
    is_installation_key,
)
from .process_environment import user_process_environment

BOOTSTRAP_CACHE_SCHEMA = "rodex-bootstrap-state-v1"


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
    try:
        observed = root.lstat()
        if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.getuid() or observed.st_mode & 0o022:
            raise RodexInstallationError(f"installation directory is not privately owned: {root}")
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


def _shipped_configuration(source_root: Path) -> Path:
    configuration = source_root.parent / "conf/hooks/user_prompt_substitutions.yaml"
    return configuration if configuration.is_file() else Path(sys.prefix) / "hooks/user_prompt_substitutions.yaml"


def _path_state(path: Path, name: str) -> list[str | int]:
    observed = path.lstat()
    return [name, stat.S_IFMT(observed.st_mode), observed.st_size, observed.st_mtime_ns]


def _bootstrap_state(source_root: Path, site_packages: Path, configuration: Path) -> dict[str, object]:
    """Observe ordinary package-manager and editable-source changes without reading contents."""
    sources: list[list[str | int]] = []
    for package_name in FIRST_PARTY_PACKAGES:
        package_root = source_root / package_name
        source_files = sorted(package_root.rglob("*.py"))
        if not source_files:
            raise RodexInstallationError(f"Rodex implementation package is unavailable: {package_name}")
        sources.extend(_path_state(path, path.relative_to(source_root).as_posix()) for path in source_files)
    dependencies = [
        _path_state(path, path.name)
        for path in sorted(site_packages.iterdir(), key=lambda candidate: candidate.name)
        if path.name != "__pycache__" and path.name not in FIRST_PARTY_PACKAGES
    ]
    return {
        "python": sys.version,
        "sources": sources,
        "dependencies": dependencies,
        "configuration": _path_state(configuration, configuration.name),
    }


def _bootstrap_namespace(source_root: Path, site_packages: Path, configuration: Path) -> str:
    locations = "\0".join(
        str(path.resolve()) for path in (source_root, site_packages, configuration, Path(sys.executable))
    )
    return hashlib.sha256(locations.encode()).hexdigest()


def _cached_interpreter(cache_path: Path, store: Path, state: dict[str, object]) -> Path | None:
    try:
        cached = json.loads(cache_path.read_text())
        identity = cached.get("implementation")
    except (OSError, ValueError, AttributeError):
        return None
    if (
        cached.get("schema") != BOOTSTRAP_CACHE_SCHEMA
        or cached.get("state") != state
        or not is_installation_key(identity)
    ):
        return None
    try:
        return _validate(store / identity, identity)
    except RodexInstallationError:
        return None


def _write_bootstrap_cache(cache_path: Path, state: dict[str, object], identity: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".bootstrap-state-", dir=cache_path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {"schema": BOOTSTRAP_CACHE_SCHEMA, "state": state, "implementation": identity},
                stream,
                separators=(",", ":"),
                sort_keys=True,
            )
            stream.write("\n")
        temporary.replace(cache_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def prepare_installation(
    store: Path,
    *,
    source_root: Path | None = None,
    site_packages: Path | None = None,
    configuration_root: Path | None = None,
) -> Path:
    """Publish changed bootstrap state once, then reuse its retained interpreter."""
    source_root = source_root or Path(__file__).resolve().parents[1]
    site_packages = site_packages or Path(sysconfig.get_path("purelib"))
    configuration = (
        configuration_root / "hooks/user_prompt_substitutions.yaml"
        if configuration_root is not None
        else _shipped_configuration(source_root)
    )
    _private_directory(store)
    store = store.resolve()
    namespace = _bootstrap_namespace(source_root, site_packages, configuration)
    cache_path = store / f".bootstrap-{namespace}.json"
    lock_path = store / f".bootstrap-{namespace}.lock"
    state = _bootstrap_state(source_root, site_packages, configuration)
    cached = _cached_interpreter(cache_path, store, state)
    if cached is not None:
        return cached
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        # Another first launcher may have published while this process waited.
        state = _bootstrap_state(source_root, site_packages, configuration)
        cached = _cached_interpreter(cache_path, store, state)
        if cached is not None:
            return cached
        _require_installed_dependencies(site_packages)
        identity = secrets.token_hex(32)
        while (store / identity).exists():  # pragma: no cover - a 256-bit collision is not operationally reproducible.
            identity = secrets.token_hex(32)
        destination = store / identity
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
        interpreter = _validate(destination, identity)
        _write_bootstrap_cache(cache_path, state, identity)
        return interpreter
    finally:
        os.close(descriptor)


def retained_installation_interpreter() -> Path:
    """Publish or validate this implementation's immutable interpreter."""
    project = _project_directory()
    if (project / INSTALLATION_MANIFEST).exists():
        if not is_installation_key(project.name):
            raise RodexInstallationError(f"installation identity is invalid: {project}")
        interpreter = _validate(project, project.name)
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
