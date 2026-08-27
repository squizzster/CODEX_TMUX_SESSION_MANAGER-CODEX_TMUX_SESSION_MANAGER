"""Report newer stable Codex CLI releases without entering its updater UI."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

CODEX_UPDATE_CACHE_TTL_SECONDS: Final = 24 * 60 * 60
CODEX_VERSION_COMMAND_TIMEOUT_SECONDS: Final = 2.0
CODEX_NPM_LOOKUP_TIMEOUT_SECONDS: Final = 3.0
_NPM_PACKAGE: Final = "@openai/codex"
_STABLE_VERSION_PATTERN: Final = re.compile(r"(?P<version>\d+\.\d+\.\d+)")

Runner = Callable[..., subprocess.CompletedProcess[str]]
ExecutableResolver = Callable[[str], str | None]


@dataclass(frozen=True, slots=True, order=True)
class StableCodexVersion:
    """One validated stable Codex release with comparable numeric identity."""

    release: tuple[int, int, int]
    text: str

    @classmethod
    def parse_exact(cls, value: str) -> StableCodexVersion | None:
        match = _STABLE_VERSION_PATTERN.fullmatch(value.strip())
        if match is None:
            return None
        text = match.group("version")
        return cls(tuple(int(part) for part in text.split(".")), text)

    @classmethod
    def parse_codex_version_output(cls, output: str) -> StableCodexVersion | None:
        prefix = "codex-cli "
        value = output.strip()
        if not value.startswith(prefix):
            return None
        return cls.parse_exact(value.removeprefix(prefix))


def default_codex_update_cache_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the current user's cache for the npm-observed latest release."""
    active_environment = os.environ if environment is None else environment
    configured_cache_home = active_environment.get("XDG_CACHE_HOME")
    cache_home = (
        Path(configured_cache_home).expanduser()
        if configured_cache_home
        else Path(active_environment.get("HOME", str(Path.home()))).expanduser() / ".cache"
    )
    return Path(os.path.abspath(cache_home)) / "rodex" / "latest_codex_npm_version.txt"


class CodexUpdateNotice:
    """Own the bounded, cached check shown immediately before a tmux attach."""

    def __init__(
        self,
        codex_binary: str,
        *,
        cache_path: Path | None = None,
        runner: Runner = subprocess.run,
        resolve_executable: ExecutableResolver = shutil.which,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._codex_binary = codex_binary
        self._cache_path = cache_path or default_codex_update_cache_path()
        self._run = runner
        self._resolve_executable = resolve_executable
        self._now = now

    def message_if_available(self) -> str | None:
        """Return an update notice; every lookup and cache failure is non-fatal."""
        try:
            installed = self._installed_version()
            if installed is None:
                return None
            latest = self._latest_version()
            if latest is None or latest.release <= installed.release:
                return None
            return (
                "Rodex: Codex update available: "
                f"{installed.text} -> {latest.text} "
                "(run 'codex update' outside Rodex)"
            )
        except Exception:
            # Update awareness is optional and must never delay or break attachment.
            return None

    def _installed_version(self) -> StableCodexVersion | None:
        result = self._run_version_command(
            [self._codex_binary, "--version"],
            timeout=CODEX_VERSION_COMMAND_TIMEOUT_SECONDS,
        )
        if result is None or result.returncode != 0:
            return None
        return StableCodexVersion.parse_codex_version_output(result.stdout)

    def _latest_version(self) -> StableCodexVersion | None:
        cached = self._read_cached_version()
        if cached is not None and self._cache_is_fresh():
            return cached

        npm_binary = self._resolve_executable("npm")
        if npm_binary is None:
            return cached
        result = self._run_version_command(
            [npm_binary, "view", _NPM_PACKAGE, "version"],
            timeout=CODEX_NPM_LOOKUP_TIMEOUT_SECONDS,
        )
        if result is None or result.returncode != 0:
            return cached
        observed = StableCodexVersion.parse_exact(result.stdout)
        if observed is None:
            return cached
        self._write_cached_version(observed)
        return observed

    def _run_version_command(
        self, command: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return self._run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _read_cached_version(self) -> StableCodexVersion | None:
        try:
            return StableCodexVersion.parse_exact(
                self._cache_path.read_text(encoding="utf-8")
            )
        except OSError:
            return None

    def _cache_is_fresh(self) -> bool:
        try:
            age_seconds = self._now() - self._cache_path.stat().st_mtime
        except OSError:
            return False
        return age_seconds <= CODEX_UPDATE_CACHE_TTL_SECONDS

    def _write_cached_version(self, version: StableCodexVersion) -> None:
        temporary_path: Path | None = None
        try:
            self._cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._cache_path.parent,
                prefix=f".{self._cache_path.name}.",
                delete=False,
            ) as temporary:
                temporary.write(f"{version.text}\n")
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, self._cache_path)
            temporary_path = None
        except OSError:
            return
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
