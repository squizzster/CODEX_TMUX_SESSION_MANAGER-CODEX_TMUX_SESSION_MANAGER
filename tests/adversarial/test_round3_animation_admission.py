from __future__ import annotations

import asyncio
import re
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path

from rodex.runtime import LiveTmuxSession
from rodex.status_animation import AsyncCommandResult, animate_status
from rodex.status_animation_admission import (
    STATUS_ANIMATION_GENERATION_OPTION,
    STATUS_ANIMATION_OWNER_TOKEN_OPTION,
    STATUS_ANIMATION_PENDING_EVENT_OPTION,
    STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION,
    animate_admitted_status,
    run_watchdog_gate,
    status_animation_hook_command,
)
from rodex.tmux_status import (
    STATUS_CLAIM_PRIORITY_OPTION,
    STATUS_CLAIM_PUBLISHER_OPTION,
    STATUS_CLAIM_TOKEN_OPTION,
)

_OPTION_COMPARISON = re.compile(r"#\{==:#\{(@[^}]+)\},([^}]*)\}")


class _OwnerTmux:
    def __init__(self) -> None:
        self.options: dict[str, str] = {}
        self.commands: list[list[str]] = []
        self.spawned_owner_tokens: list[str] = []
        self.before_release: Callable[[], None] | None = None
        self.after_release: Callable[[], None] | None = None
        self.fail_release_once = False

    def submit_hook_event(self, event: str) -> str | None:
        self.options[STATUS_ANIMATION_PENDING_EVENT_OPTION] = event
        generation = str(int(self.options.get(STATUS_ANIMATION_GENERATION_OPTION, "0")) + 1)
        self.options[STATUS_ANIMATION_GENERATION_OPTION] = generation
        if (
            STATUS_ANIMATION_OWNER_TOKEN_OPTION in self.options
            and STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION in self.options
        ):
            return None
        self.options[STATUS_ANIMATION_OWNER_TOKEN_OPTION] = generation
        self.options[STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION] = generation
        self.spawned_owner_tokens.append(generation)
        return generation

    async def __call__(self, command: Sequence[str]) -> AsyncCommandResult:
        recorded = list(command)
        self.commands.append(recorded)
        if "display-message" in recorded:
            return AsyncCommandResult(0, "2\n")
        if "show-options" in recorded:
            value = self.options.get(recorded[-1], "")
            return AsyncCommandResult(0 if value else 1, f"{value}\n")
        if "list-clients" in recorded:
            return AsyncCommandResult(0, "")
        if "if-shell" in recorded:
            condition = recorded[-2]
            if self.fail_release_once and STATUS_ANIMATION_GENERATION_OPTION in condition:
                self.fail_release_once = False
                return AsyncCommandResult(124)
            if (
                STATUS_ANIMATION_GENERATION_OPTION in condition
                and self.before_release is not None
            ):
                callback = self.before_release
                self.before_release = None
                callback()
            if self._condition_is_true(condition):
                for tmux_command in recorded[-1].split(" ; "):
                    if tmux_command.lstrip().startswith("run-shell"):
                        continue
                    self._apply(shlex.split(tmux_command))
                if (
                    STATUS_ANIMATION_GENERATION_OPTION in condition
                    and self.after_release is not None
                ):
                    callback = self.after_release
                    self.after_release = None
                    callback()
        return AsyncCommandResult(0)

    def _condition_is_true(self, condition: str) -> bool:
        if condition.startswith("#{<=:"):
            current = int(self.options.get(STATUS_CLAIM_PRIORITY_OPTION, "0"))
            requested = int(condition.rsplit(",", maxsplit=1)[1].removesuffix("}"))
            return current <= requested
        comparisons = _OPTION_COMPARISON.findall(condition)
        if comparisons:
            return all(
                self.options.get(option, "") == expected for option, expected in comparisons
            )
        raise AssertionError(f"unexpected tmux condition: {condition}")

    def _apply(self, command: list[str]) -> None:
        if command[:2] == ["set-option", "-u"]:
            self.options.pop(command[-1], None)
        elif command[:1] == ["set-option"]:
            self.options[command[-2]] = command[-1]


async def _no_wait(_deadline: float) -> None:
    await asyncio.sleep(0)


def _run_owner(
    tmux: _OwnerTmux,
    owner_token: str,
    *,
    watchdog: bool = False,
) -> None:
    if watchdog:
        tmux.options.pop(STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION, None)
    asyncio.run(
        animate_admitted_status(
            "tmux",
            Path("/isolated/round3/owner.sock"),
            "round3-owner",
            "attached",
            owner_token,
            watchdog=watchdog,
            runner=tmux,
            wait_until=_no_wait,
            token_factory=lambda: "frame-or-recovery-token",
        )
    )


def test_round3_owner_drains_one_transition_and_clears_transient_options() -> None:
    tmux = _OwnerTmux()
    owner_token = tmux.submit_hook_event("attached")
    assert owner_token == "1"

    _run_owner(tmux, owner_token)

    assert STATUS_ANIMATION_PENDING_EVENT_OPTION not in tmux.options
    assert tmux.options[STATUS_ANIMATION_GENERATION_OPTION] == "1"
    assert STATUS_ANIMATION_OWNER_TOKEN_OPTION not in tmux.options
    assert STATUS_CLAIM_TOKEN_OPTION not in tmux.options
    assert STATUS_CLAIM_PUBLISHER_OPTION not in tmux.options
    assert len(tmux.commands) == 59


def test_round3_owner_aba_coalesces_to_latest_without_spawning_a_loser() -> None:
    tmux = _OwnerTmux()
    owner_token = tmux.submit_hook_event("attached")
    assert owner_token == "1"

    def submit_aba_before_release() -> None:
        assert tmux.submit_hook_event("detached") is None
        assert tmux.submit_hook_event("attached") is None

    tmux.before_release = submit_aba_before_release
    _run_owner(tmux, owner_token)

    claims = [
        command
        for command in tmux.commands
        if "if-shell" in command
        and STATUS_CLAIM_TOKEN_OPTION in command[-1]
        and "status-format[0]" in command[-1]
    ]
    assert len(claims) == 2
    assert tmux.spawned_owner_tokens == ["1"]
    assert STATUS_ANIMATION_OWNER_TOKEN_OPTION not in tmux.options
    assert tmux.options[STATUS_ANIMATION_GENERATION_OPTION] == "3"


def test_round3_release_then_aba_hands_off_to_one_new_owner() -> None:
    tmux = _OwnerTmux()
    owner_token = tmux.submit_hook_event("attached")
    assert owner_token == "1"

    def submit_aba_after_release() -> None:
        assert tmux.submit_hook_event("detached") == "2"
        assert tmux.submit_hook_event("attached") is None

    tmux.after_release = submit_aba_after_release
    _run_owner(tmux, owner_token)
    assert tmux.spawned_owner_tokens == ["1", "2"]

    _run_owner(tmux, "2")

    assert tmux.spawned_owner_tokens == ["1", "2"]
    assert STATUS_ANIMATION_OWNER_TOKEN_OPTION not in tmux.options
    assert tmux.options[STATUS_ANIMATION_GENERATION_OPTION] == "3"


def test_round3_stale_owner_watchdog_performs_one_bounded_recovery() -> None:
    tmux = _OwnerTmux()
    stale_token = tmux.submit_hook_event("attached")
    assert stale_token == "1"

    _run_owner(tmux, stale_token, watchdog=True)

    owner_writes = [
        command
        for command in tmux.commands
        if "if-shell" in command and STATUS_ANIMATION_OWNER_TOKEN_OPTION in command[-1]
    ]
    assert len(owner_writes) >= 1
    assert STATUS_ANIMATION_OWNER_TOKEN_OPTION not in tmux.options
    assert tmux.options[STATUS_ANIMATION_GENERATION_OPTION] == "1"


def test_round3_stale_watchdog_is_a_noop_after_owner_generation_changes() -> None:
    tmux = _OwnerTmux()
    stale_token = tmux.submit_hook_event("attached")
    assert stale_token == "1"
    tmux.options[STATUS_ANIMATION_OWNER_TOKEN_OPTION] = "new-owner"

    _run_owner(tmux, stale_token, watchdog=True)

    assert tmux.options[STATUS_ANIMATION_OWNER_TOKEN_OPTION] == "new-owner"
    assert not any("if-shell" in command for command in tmux.commands)


def test_round3_release_timeout_remains_covered_by_the_native_watchdog() -> None:
    tmux = _OwnerTmux()
    owner_token = tmux.submit_hook_event("attached")
    assert owner_token == "1"
    tmux.fail_release_once = True

    _run_owner(tmux, owner_token)

    assert tmux.options[STATUS_ANIMATION_OWNER_TOKEN_OPTION] == owner_token
    assert tmux.options[STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION] == owner_token

    _run_owner(tmux, owner_token, watchdog=True)

    assert STATUS_ANIMATION_OWNER_TOKEN_OPTION not in tmux.options
    assert STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION not in tmux.options


def test_round3_watchdog_gate_exposes_crashes_before_starting_recovery() -> None:
    tmux = _OwnerTmux()
    owner_token = tmux.submit_hook_event("attached")
    assert owner_token == "1"

    asyncio.run(
        run_watchdog_gate(
            "/usr/bin/tmux",
            Path("/isolated/round3/tmux.sock"),
            "round3-owner",
            "attached",
            owner_token,
            runner=tmux,
            python_executable="/venv/bin/python",
        )
    )

    gate_action = tmux.commands[-1][-1]
    watchdog_marker_clear = gate_action.index("set-option -u -t ")
    recovery_process = gate_action.index("--watchdog")

    assert watchdog_marker_clear < recovery_process
    assert STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION not in tmux.options


def test_round3_delayed_gate_uses_the_canonical_python_executor_boundary() -> None:
    hook = status_animation_hook_command(
        "/venv/bin/python",
        "/usr/bin/tmux",
        LiveTmuxSession(Path("/isolated/round3/tmux.sock"), "round3-owner"),
        "attached",
    )

    assert "--watchdog-gate" in hook
    assert "/usr/bin/tmux -S" not in hook


def test_round3_hook_burst_has_one_immediate_owner_and_one_delayed_watchdog() -> None:
    runtime = LiveTmuxSession(Path("/isolated/round3/tmux.sock"), "round3-owner")
    hook = status_animation_hook_command(
        "/venv/bin/python",
        "/usr/bin/tmux",
        runtime,
        "attached",
    )
    tmux = _OwnerTmux()

    for _ in range(16):
        tmux.submit_hook_event("attached")

    assert tmux.spawned_owner_tokens == ["1"]
    assert hook.count("--admitted") == 1
    assert hook.count("--watchdog-gate") == 1
    assert hook.count("--tmux-session-target") == 2
    assert hook.count("#{session_id}") >= 2
    assert "run-shell -b -d 15.0" in hook
    assert hook.count(STATUS_ANIMATION_PENDING_EVENT_OPTION) == 1
    assert hook.count(STATUS_ANIMATION_GENERATION_OPTION) >= 4
    assert hook.count(STATUS_ANIMATION_OWNER_TOKEN_OPTION) >= 2


def test_round3_animation_uses_a_rename_stable_tmux_session_id() -> None:
    tmux = _OwnerTmux()

    asyncio.run(
        animate_status(
            "tmux",
            Path("/isolated/round3/rename.sock"),
            "$7",
            "attached",
            runner=tmux,
            wait_until=_no_wait,
            token_factory=lambda: "stable-target-frame",
        )
    )

    targeted = [
        command[command.index("-t") + 1] for command in tmux.commands if "-t" in command
    ]
    assert "$7:" in targeted
    assert "$7" in targeted
    assert not any(target.startswith("=") for target in targeted)
