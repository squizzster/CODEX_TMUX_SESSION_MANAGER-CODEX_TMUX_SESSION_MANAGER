from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rodex.codex_update_notice import (
    CODEX_NPM_LOOKUP_TIMEOUT_SECONDS,
    CODEX_UPDATE_CACHE_TTL_SECONDS,
    CODEX_VERSION_COMMAND_TIMEOUT_SECONDS,
    CodexUpdateNotice,
    StableCodexVersion,
    default_codex_update_cache_path,
)


class VersionCommandRunner:
    def __init__(self, *, installed: str, latest: str | BaseException) -> None:
        self.installed = installed
        self.latest = latest
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self, command: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, options))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout=self.installed, stderr="")
        if isinstance(self.latest, BaseException):
            raise self.latest
        return subprocess.CompletedProcess(command, 0, stdout=self.latest, stderr="")


def test_stable_codex_versions_require_exact_three_part_releases() -> None:
    assert StableCodexVersion.parse_exact("0.150.1\n") == StableCodexVersion(
        (0, 150, 1), "0.150.1"
    )
    assert StableCodexVersion.parse_codex_version_output(
        "codex-cli 0.149.1\n"
    ) == StableCodexVersion((0, 149, 1), "0.149.1")
    assert StableCodexVersion.parse_exact("v0.150.1") is None
    assert StableCodexVersion.parse_exact("0.150.1-beta.1") is None
    assert StableCodexVersion.parse_codex_version_output("Codex 0.149.1") is None


def test_fresh_npm_cache_reports_update_without_a_network_lookup(tmp_path: Path) -> None:
    cache_path = tmp_path / "latest_codex_npm_version.txt"
    cache_path.write_text("0.150.1\n", encoding="utf-8")
    os.utime(cache_path, (1_000, 1_000))
    runner = VersionCommandRunner(installed="codex-cli 0.149.1\n", latest="unused")
    resolver_calls: list[str] = []

    message = CodexUpdateNotice(
        "/usr/bin/codex",
        cache_path=cache_path,
        runner=runner,
        resolve_executable=lambda name: resolver_calls.append(name) or "/usr/bin/npm",
        now=lambda: 1_000 + CODEX_UPDATE_CACHE_TTL_SECONDS,
    ).message_if_available()

    assert message == (
        "Rodex: Codex update available: 0.149.1 -> 0.150.1 "
        "(run 'codex update' outside Rodex)"
    )
    assert resolver_calls == []
    assert runner.calls == [
        (
            ["/usr/bin/codex", "--version"],
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": CODEX_VERSION_COMMAND_TIMEOUT_SECONDS,
            },
        )
    ]


def test_stale_cache_refreshes_from_npm_and_suppresses_current_release(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "latest_codex_npm_version.txt"
    cache_path.write_text("0.149.1\n", encoding="utf-8")
    os.utime(cache_path, (1, 1))
    runner = VersionCommandRunner(installed="codex-cli 0.150.1\n", latest="0.150.1\n")

    message = CodexUpdateNotice(
        "/usr/bin/codex",
        cache_path=cache_path,
        runner=runner,
        resolve_executable=lambda _name: "/usr/bin/npm",
        now=lambda: 1_000_000,
    ).message_if_available()

    assert message is None
    assert cache_path.read_text(encoding="utf-8") == "0.150.1\n"
    assert runner.calls[1] == (
        ["/usr/bin/npm", "view", "@openai/codex", "version"],
        {
            "check": False,
            "capture_output": True,
            "text": True,
            "timeout": CODEX_NPM_LOOKUP_TIMEOUT_SECONDS,
        },
    )


def test_failed_npm_refresh_uses_valid_stale_cache_and_never_breaks_attach_notice(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "latest_codex_npm_version.txt"
    cache_path.write_text("0.150.1\n", encoding="utf-8")
    os.utime(cache_path, (1, 1))
    runner = VersionCommandRunner(
        installed="codex-cli 0.149.1\n",
        latest=subprocess.TimeoutExpired(["npm"], 3),
    )

    message = CodexUpdateNotice(
        "/usr/bin/codex",
        cache_path=cache_path,
        runner=runner,
        resolve_executable=lambda _name: "/usr/bin/npm",
        now=lambda: 1_000_000,
    ).message_if_available()

    assert message == (
        "Rodex: Codex update available: 0.149.1 -> 0.150.1 "
        "(run 'codex update' outside Rodex)"
    )


def test_invalid_or_unavailable_version_evidence_is_silent(tmp_path: Path) -> None:
    cache_path = tmp_path / "latest_codex_npm_version.txt"
    cache_path.write_text("not-a-version\n", encoding="utf-8")
    runner = VersionCommandRunner(installed="codex-cli unknown\n", latest="bad\n")

    message = CodexUpdateNotice(
        "/usr/bin/codex",
        cache_path=cache_path,
        runner=runner,
        resolve_executable=lambda _name: None,
    ).message_if_available()

    assert message is None


def test_default_cache_path_names_npm_version_provenance(tmp_path: Path) -> None:
    assert (
        default_codex_update_cache_path(
            {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "cache")}
        )
        == tmp_path / "cache" / "rodex" / "latest_codex_npm_version.txt"
    )
