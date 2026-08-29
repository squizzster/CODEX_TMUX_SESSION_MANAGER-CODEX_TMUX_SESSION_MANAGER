from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import threading
import time
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


def _run_contended_update_notice(
    cache_path: Path,
    npm_counter_path: Path,
    result_path: Path,
    start_barrier: object,
) -> None:
    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            return subprocess.CompletedProcess(
                command, 0, stdout="codex-cli 0.149.1\n", stderr=""
            )
        descriptor = os.open(
            npm_counter_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(descriptor, b"npm\n")
        finally:
            os.close(descriptor)
        time.sleep(0.3)
        return subprocess.CompletedProcess(command, 0, stdout="0.150.1\n", stderr="")

    before_threads = sorted(
        (thread.name, thread.daemon) for thread in threading.enumerate()
    )
    wait = start_barrier.wait
    wait()
    started_at = time.monotonic()
    message = CodexUpdateNotice(
        "/usr/bin/codex",
        cache_path=cache_path,
        runner=runner,
        resolve_executable=lambda _name: "/usr/bin/npm",
    ).message_if_available()
    duration = time.monotonic() - started_at
    open_cache_targets: list[str] = []
    for descriptor_name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{descriptor_name}")
        except OSError:
            continue
        if os.fspath(cache_path.parent) in target:
            open_cache_targets.append(target)
    result_path.write_text(
        json.dumps(
            {
                "message": message,
                "duration": duration,
                "threads_before": before_threads,
                "threads_after": sorted(
                    (thread.name, thread.daemon) for thread in threading.enumerate()
                ),
                "open_cache_targets": open_cache_targets,
            }
        ),
        encoding="utf-8",
    )


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


def test_contending_stale_cache_reader_does_not_wait_for_the_refresh_owner(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "latest_codex_npm_version.txt"
    cache_path.write_text("0.150.1\n", encoding="utf-8")
    os.utime(cache_path, (1, 1))
    npm_entered = threading.Event()
    release_npm = threading.Event()
    contender_finished = threading.Event()
    calls_lock = threading.Lock()
    npm_calls = 0
    messages: dict[str, str | None] = {}
    errors: list[BaseException] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        nonlocal npm_calls
        if command[-1] == "--version":
            return subprocess.CompletedProcess(
                command, 0, stdout="codex-cli 0.149.1\n", stderr=""
            )
        with calls_lock:
            npm_calls += 1
        npm_entered.set()
        assert release_npm.wait(5)
        return subprocess.CompletedProcess(command, 0, stdout="0.151.1\n", stderr="")

    def check(label: str) -> None:
        try:
            messages[label] = CodexUpdateNotice(
                "/usr/bin/codex",
                cache_path=cache_path,
                runner=runner,
                resolve_executable=lambda _name: "/usr/bin/npm",
                now=lambda: 1_000_000,
            ).message_if_available()
        except BaseException as error:
            errors.append(error)
        finally:
            if label == "contender":
                contender_finished.set()

    owner = threading.Thread(target=check, args=("owner",))
    contender = threading.Thread(target=check, args=("contender",))
    owner.start()
    assert npm_entered.wait(5), errors
    contender.start()
    try:
        contender_was_responsive = contender_finished.wait(0.25)
    finally:
        release_npm.set()
    owner.join(5)
    contender.join(5)

    assert contender_was_responsive
    assert not owner.is_alive()
    assert not contender.is_alive()
    assert errors == []
    assert npm_calls == 1
    assert messages["contender"] == (
        "Rodex: Codex update available: 0.149.1 -> 0.150.1 "
        "(run 'codex update' outside Rodex)"
    )
    assert messages["owner"] == (
        "Rodex: Codex update available: 0.149.1 -> 0.151.1 "
        "(run 'codex update' outside Rodex)"
    )
    assert cache_path.read_text(encoding="utf-8") == "0.151.1\n"


def test_twenty_processes_perform_one_atomic_cache_refresh_without_leaks(
    tmp_path: Path,
) -> None:
    process_count = 20
    cache_path = tmp_path / "latest_codex_npm_version.txt"
    npm_counter_path = tmp_path / "npm-calls.txt"
    result_paths = tuple(
        tmp_path / f"result-{index}.json" for index in range(process_count)
    )
    process_context = multiprocessing.get_context("fork")
    start_barrier = process_context.Barrier(process_count)
    processes = [
        process_context.Process(
            target=_run_contended_update_notice,
            args=(cache_path, npm_counter_path, result_path, start_barrier),
        )
        for result_path in result_paths
    ]

    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
        assert all(not process.is_alive() for process in processes)
        assert [process.exitcode for process in processes] == [0] * process_count
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
            process.close()

    assert npm_counter_path.read_text(encoding="utf-8").splitlines() == ["npm"]
    assert cache_path.read_text(encoding="utf-8") == "0.150.1\n"
    results = [json.loads(path.read_text(encoding="utf-8")) for path in result_paths]
    assert any(result["message"] is not None for result in results)
    assert all(result["threads_after"] == result["threads_before"] for result in results)
    assert all(result["open_cache_targets"] == [] for result in results)
    refresh_lock_name = f".{cache_path.name}.lock"
    assert [
        path.name
        for path in tmp_path.iterdir()
        if path.name.startswith(f".{cache_path.name}.") and path.name != refresh_lock_name
    ] == []


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
