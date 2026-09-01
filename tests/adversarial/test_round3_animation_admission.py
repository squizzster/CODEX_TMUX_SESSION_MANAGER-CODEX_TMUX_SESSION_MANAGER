from __future__ import annotations

import asyncio
import re
import shlex
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from rodex.status_animation import AsyncCommandResult, animate_status
from rodex.status_animation_admission import (
    STATUS_ANIMATION_GENERATION_OPTION,
    STATUS_ANIMATION_OWNER_TOKEN_OPTION,
    STATUS_ANIMATION_PENDING_EVENT_OPTION,
    STATUS_ANIMATION_WATCHDOG_TOKEN_OPTION,
    animate_admitted_status,
    run_watchdog_gate,
    status_animation_admission_command,
)
from rodex.tmux_session_capability import (
    RODEX_CODEX_SESSION_ID_OPTION,
    RODEX_INTERNAL_SESSION_ID_OPTION,
    RODEX_PRIMARY_PANE_ID_OPTION,
    RODEX_REGISTRATION_REGISTERED,
    RODEX_REGISTRATION_STATE_OPTION,
    RODEX_REGISTRY_ID_OPTION,
    RODEX_RUNTIME_ID_OPTION,
    RODEX_SESSION_ID_OPTION,
    RODEX_SHARED_TMUX_PROTOCOL,
    RODEX_SHARED_TMUX_PROTOCOL_OPTION,
    RODEX_SHARED_TMUX_SERVER_ID_OPTION,
    TmuxSessionCapability,
)
from rodex.tmux_status import (
    STATUS_CLAIM_PRIORITY_OPTION,
    STATUS_CLAIM_PUBLISHER_OPTION,
    STATUS_CLAIM_TOKEN_OPTION,
)
from rodex_registry.identity import RodexRegistryId, RodexRuntimeId, RodexSessionId

_OPTION_COMPARISON = re.compile(r"#\{==:#\{(@[^}]+)\},(?:#\{l:([^}]*)\}|([^}]*))\}")


def _capability(socket_path: Path) -> TmuxSessionCapability:
    return TmuxSessionCapability(
        socket_path,
        "0123456789abcdef0123456789abcdef",
        "$7",
        "%9",
        RodexRuntimeId.parse("0123456789abcdef"),
        RodexSessionId.parse("1111111111111111"),
        RodexRegistryId.parse("2222222222222222"),
        7,
        uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
    )


class _OwnerTmux:
    def __init__(self) -> None:
        capability = _capability(Path("/isolated/round3/owner.sock"))
        self.options: dict[str, str] = {
            RODEX_SHARED_TMUX_PROTOCOL_OPTION: RODEX_SHARED_TMUX_PROTOCOL,
            RODEX_SHARED_TMUX_SERVER_ID_OPTION: capability.tmux_server_id,
            RODEX_PRIMARY_PANE_ID_OPTION: capability.tmux_primary_pane_id,
            RODEX_RUNTIME_ID_OPTION: str(capability.runtime_id),
            RODEX_REGISTRATION_STATE_OPTION: RODEX_REGISTRATION_REGISTERED,
            RODEX_SESSION_ID_OPTION: str(capability.rodex_session_id),
            RODEX_REGISTRY_ID_OPTION: str(capability.registry_id),
            RODEX_INTERNAL_SESSION_ID_OPTION: str(capability.internal_session_id),
            RODEX_CODEX_SESSION_ID_OPTION: str(capability.codex_session_id),
        }
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
            format_string = recorded[-1]
            if "#{session_attached}" in format_string:
                return AsyncCommandResult(0, "1\t2\n")
            if STATUS_CLAIM_TOKEN_OPTION in format_string:
                token = self.options.get(STATUS_CLAIM_TOKEN_OPTION, "")
                return AsyncCommandResult(0, f"1\t{token}\n")
            return AsyncCommandResult(0, "1\n")
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
        if "#{<=:" in condition:
            current = int(self.options.get(STATUS_CLAIM_PRIORITY_OPTION, "0"))
            match = re.search(r"#\{<=:#\{@rodex_status_claim_priority\},(\d+)\}", condition)
            assert match is not None
            requested = int(match.group(1))
            return current <= requested
        comparisons = _OPTION_COMPARISON.findall(condition)
        if comparisons:
            return all(
                self.options.get(option, "") == (literal_expected or raw_expected)
                for option, literal_expected, raw_expected in comparisons
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
            _capability(Path("/isolated/round3/owner.sock")),
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
    assert len(tmux.commands) == 60


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
            _capability(Path("/isolated/round3/tmux.sock")),
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
    hook = status_animation_admission_command(
        "/venv/bin/python",
        "/usr/bin/tmux",
        _capability(Path("/isolated/round3/tmux.sock")),
        "attached",
    )

    assert "--watchdog-gate" in hook
    assert "/usr/bin/tmux -S" not in hook


def test_round3_hook_burst_has_one_immediate_owner_and_one_delayed_watchdog() -> None:
    capability = _capability(Path("/isolated/round3/tmux.sock"))
    hook = status_animation_admission_command(
        "/venv/bin/python",
        "/usr/bin/tmux",
        capability,
        "attached",
    )
    tmux = _OwnerTmux()

    for _ in range(16):
        tmux.submit_hook_event("attached")

    assert tmux.spawned_owner_tokens == ["1"]
    assert hook.count("--admitted") == 1
    assert hook.count("--watchdog-gate") == 1
    assert hook.count("--tmux-session-id") == 2
    assert "-t '#{session_id}" not in hook
    assert "--tmux-session-id '#{session_id}" not in hook
    assert hook.count(capability.tmux_session_id) >= 2
    assert "run-shell -b -d 15.0" in hook
    assert hook.count(STATUS_ANIMATION_PENDING_EVENT_OPTION) == 1
    assert hook.count(STATUS_ANIMATION_GENERATION_OPTION) >= 4
    assert hook.count(STATUS_ANIMATION_OWNER_TOKEN_OPTION) >= 2


def test_round3_animation_uses_a_rename_stable_tmux_session_id() -> None:
    tmux = _OwnerTmux()

    asyncio.run(
        animate_status(
            "tmux",
            _capability(Path("/isolated/round3/rename.sock")),
            "attached",
            runner=tmux,
            wait_until=_no_wait,
            token_factory=lambda: "stable-target-frame",
        )
    )

    targeted = [
        command[command.index("-t") + 1] for command in tmux.commands if "-t" in command
    ]
    assert targeted
    assert set(targeted) == {"%9"}
    assert any("$7" in argument for command in tmux.commands for argument in command)
    assert not any(target.startswith("=") for target in targeted)
