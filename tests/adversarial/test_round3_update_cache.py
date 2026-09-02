from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rodex.codex_update_notice import CodexUpdateNotice


def test_round3_future_dated_update_cache_is_not_fresh(tmp_path: Path) -> None:
    """A future timestamp must not suppress npm refresh until wall time catches up."""
    cache = tmp_path / "latest_codex_npm_version.txt"
    cache.write_text("0.152.0\n", encoding="utf-8")
    os.utime(cache, (1_000_000, 1_000_000))
    calls: list[list[str]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        output = "codex-cli 0.151.0\n" if command[-1] == "--version" else "0.153.0\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    message = CodexUpdateNotice(
        "/usr/bin/codex",
        cache_path=cache,
        runner=runner,
        resolve_executable=lambda _name: "/usr/bin/npm",
        now=lambda: 2_000,
    ).message_if_available()

    assert calls == [
        ["/usr/bin/codex", "--version"],
        ["/usr/bin/npm", "view", "@openai/codex", "version"],
    ]
    assert message == (
        "Rodex: Codex update available: 0.151.0 -> 0.153.0 "
        "(run 'codex update' outside Rodex)"
    )


def test_round3_cache_age_bounds_include_zero_and_the_exact_ttl(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "latest_codex_npm_version.txt"
    cache.write_text("0.152.0\n", encoding="utf-8")
    os.utime(cache, (10_000, 10_000))

    for now in (10_000, 10_000 + 24 * 60 * 60):
        resolver_calls: list[str] = []
        notice = CodexUpdateNotice(
            "/usr/bin/codex",
            cache_path=cache,
            runner=lambda command, **_options: subprocess.CompletedProcess(
                command, 0, "codex-cli 0.151.0\n", ""
            ),
            resolve_executable=lambda name, calls=resolver_calls: (
                calls.append(name) or None
            ),
            now=lambda now=now: now,
        )

        assert notice.message_if_available() is not None
        assert resolver_calls == []
