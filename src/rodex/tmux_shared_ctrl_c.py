"""Protect a shared Rodex session from one-client accidental Ctrl-C exit."""

from __future__ import annotations

import argparse
import json
import secrets
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from .status_bar import RODEX_STATUS_COLOURS
from .tmux_executor import SyncTmuxExecutor, SyncTmuxRunner, TmuxCommandResult
from .tmux_session_capability import (
    TmuxSessionCapability,
    parse_tmux_session_capability,
    registered_primary_pane_condition,
)
from .tmux_status import (
    STATUS_PUBLISHER_SHARED_CTRL_C,
    StatusPriority,
    TmuxStatusPipeline,
    TmuxStatusPresentation,
)

_CONFIRMATION_OPTION: Final = "@rodex_shared_ctrl_c_confirmation"
_CONFIRMATION_CLAIM_OPTION: Final = "@rodex_shared_ctrl_c_confirmation_claim"
_CONFIRMATION_WINDOW_NANOSECONDS: Final = 2_000_000_000
_CONFIRMATION_WINDOW_SECONDS: Final = 2.0
_CONFIRMATION_STATUS: Final = (
    f"#[bg={RODEX_STATUS_COLOURS.safety_background}]"
    f"#[fg={RODEX_STATUS_COLOURS.safety_foreground}]"
    "#[bold] CTRL-C ARMED: SHARED session — "
    "CTRL-C again within 2s may END it for everyone; "
    "CTRL-B d detaches only you. #[default]"
)

Runner = SyncTmuxRunner
ExpiryScheduler = Callable[[Callable[[], None]], None]


def _restore_after_confirmation_window(callback: Callable[[], None]) -> None:
    time.sleep(_CONFIRMATION_WINDOW_SECONDS)
    callback()


def handle_shared_ctrl_c(
    tmux_binary: str,
    capability: TmuxSessionCapability,
    pane_id: str,
    client_name: str,
    *,
    monotonic_nanoseconds: Callable[[], int] = time.monotonic_ns,
    confirmation_token: Callable[[], str] = lambda: secrets.token_hex(8),
    expiry_scheduler: ExpiryScheduler = _restore_after_confirmation_window,
    runner: Runner = subprocess.run,
) -> int:
    """Forward Ctrl-C privately, or require same-client confirmation when shared."""
    executor = SyncTmuxExecutor(
        tmux_binary,
        capability.tmux_server_socket_path,
        runner=runner,
    )

    def raw_tmux(*arguments: str) -> TmuxCommandResult:
        return executor.run(arguments)

    identity = raw_tmux(
        "display-message",
        "-p",
        "-t",
        pane_id,
        "-F",
        f"{registered_primary_pane_condition(capability)}\t#{{pane_id}}",
    )
    if identity.returncode != 0 or identity.stdout.strip() != f"1\t{pane_id}":
        return 1
    primary_pane_id = capability.pane_target

    def tmux(*arguments: str) -> TmuxCommandResult:
        return raw_tmux(
            "if-shell",
            "-t",
            primary_pane_id,
            "-F",
            registered_primary_pane_condition(capability),
            shlex.join(arguments),
        )

    def clear_confirmation() -> None:
        tmux("set-option", "-u", "-t", primary_pane_id, _CONFIRMATION_OPTION)

    def clear_confirmation_claim() -> None:
        tmux("set-option", "-u", "-t", primary_pane_id, _CONFIRMATION_CLAIM_OPTION)

    status = TmuxStatusPipeline(tmux, primary_pane_id)

    attached = tmux(
        "display-message",
        "-p",
        "-t",
        primary_pane_id,
        "-F",
        "#{session_attached}",
    )
    if attached.returncode != 0:
        return attached.returncode
    try:
        current_attached_client_count = int(attached.stdout.strip())
    except ValueError:
        return 1
    if current_attached_client_count < 1:
        return 1

    advertised = tmux(
        "show-options",
        "-v",
        "-t",
        primary_pane_id,
        _CONFIRMATION_OPTION,
    )
    current_confirmation = _parse_confirmation(advertised.stdout.strip())

    if current_attached_client_count == 1:
        forwarded = tmux(
            "if-shell",
            "-t",
            primary_pane_id,
            "-F",
            "#{==:#{session_attached},1}",
            _tmux_command_sequence(
                ("set-option", "-u", "-t", primary_pane_id, _CONFIRMATION_OPTION),
                (
                    "set-option",
                    "-u",
                    "-t",
                    primary_pane_id,
                    _CONFIRMATION_CLAIM_OPTION,
                ),
                ("send-keys", "-t", primary_pane_id, "C-c"),
            ),
            _tmux_command_sequence(
                ("set-option", "-u", "-t", primary_pane_id, _CONFIRMATION_OPTION),
                (
                    "set-option",
                    "-u",
                    "-t",
                    primary_pane_id,
                    _CONFIRMATION_CLAIM_OPTION,
                ),
            ),
        )
        if current_confirmation is not None:
            status.restore_if_token_matches(current_confirmation[2])
        return forwarded.returncode

    now = monotonic_nanoseconds()
    if _is_current_confirmation(current_confirmation, client_name, now):
        assert current_confirmation is not None
        claim = tmux(
            "set-option",
            "-o",
            "-t",
            primary_pane_id,
            _CONFIRMATION_CLAIM_OPTION,
            advertised.stdout.strip(),
        )
        if claim.returncode != 0:
            return 0
        confirmed = tmux(
            "if-shell",
            "-t",
            primary_pane_id,
            "-F",
            (
                f"#{{&&:#{{?{_CONFIRMATION_CLAIM_OPTION},1,0}},"
                f"#{{==:#{{{_CONFIRMATION_OPTION}}},"
                f"#{{{_CONFIRMATION_CLAIM_OPTION}}}}}}}"
            ),
            _tmux_command_sequence(
                ("set-option", "-u", "-t", primary_pane_id, _CONFIRMATION_OPTION),
                (
                    "set-option",
                    "-u",
                    "-t",
                    primary_pane_id,
                    _CONFIRMATION_CLAIM_OPTION,
                ),
                ("send-keys", "-t", primary_pane_id, "C-c"),
            ),
            _tmux_command_sequence(
                (
                    "set-option",
                    "-u",
                    "-t",
                    primary_pane_id,
                    _CONFIRMATION_CLAIM_OPTION,
                ),
            ),
        )
        status.restore_if_token_matches(current_confirmation[2])
        return confirmed.returncode

    if current_confirmation is not None:
        clear_confirmation()
        status.restore_if_token_matches(current_confirmation[2])
    clear_confirmation_claim()
    status_token = confirmation_token()
    confirmation = json.dumps(
        {
            "armed_at_monotonic_ns": now,
            "client_name": client_name,
            "status_token": status_token,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    armed = tmux(
        "set-option",
        "-t",
        primary_pane_id,
        _CONFIRMATION_OPTION,
        confirmation,
    )
    if armed.returncode != 0:
        return armed.returncode
    if not status.publish_transient(
        publisher=STATUS_PUBLISHER_SHARED_CTRL_C,
        token=status_token,
        priority=StatusPriority.SAFETY_WARNING,
        presentation=TmuxStatusPresentation(status_left=_CONFIRMATION_STATUS),
    ):
        clear_confirmation()
        return 1

    def expire_confirmation() -> None:
        latest = tmux(
            "show-options",
            "-v",
            "-t",
            primary_pane_id,
            _CONFIRMATION_OPTION,
        )
        if latest.stdout.strip() != confirmation:
            return
        clear_confirmation()
        clear_confirmation_claim()
        status.restore_if_token_matches(status_token)

    expiry_scheduler(expire_confirmation)
    return 0


def _parse_confirmation(value: str) -> tuple[str, int, str] | None:
    try:
        confirmation = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(confirmation, dict):
        return None
    armed_client = confirmation.get("client_name")
    armed_at = confirmation.get("armed_at_monotonic_ns")
    status_token = confirmation.get("status_token")
    if (
        not isinstance(armed_client, str)
        or type(armed_at) is not int
        or not isinstance(status_token, str)
    ):
        return None
    return armed_client, armed_at, status_token


def _is_current_confirmation(
    confirmation: tuple[str, int, str] | None,
    client_name: str,
    now: int,
) -> bool:
    if confirmation is None or confirmation[0] != client_name:
        return False
    elapsed = now - confirmation[1]
    return 0 <= elapsed <= _CONFIRMATION_WINDOW_NANOSECONDS


def _tmux_command_sequence(*commands: tuple[str, ...]) -> str:
    return " ; ".join(shlex.join(command) for command in commands)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.tmux_shared_ctrl_c")
    parser.add_argument("--tmux-binary", required=True)
    parser.add_argument("--tmux-server-socket", required=True, type=Path)
    parser.add_argument("--expected-server-id", required=True)
    parser.add_argument("--tmux-session-id", required=True)
    parser.add_argument("--tmux-primary-pane-id", required=True)
    parser.add_argument("--expected-runtime-id", required=True)
    parser.add_argument("--expected-rodex-session-id", required=True)
    parser.add_argument("--expected-registry-id", required=True)
    parser.add_argument("--expected-internal-session-id", required=True)
    parser.add_argument("--expected-codex-session-id", required=True)
    parser.add_argument("--pane-id", required=True)
    parser.add_argument("--client-name", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    capability = parse_tmux_session_capability(
        args.tmux_server_socket,
        args.expected_server_id,
        args.tmux_session_id,
        args.tmux_primary_pane_id,
        args.expected_runtime_id,
        args.expected_rodex_session_id,
        args.expected_registry_id,
        args.expected_internal_session_id,
        args.expected_codex_session_id,
    )
    return handle_shared_ctrl_c(
        args.tmux_binary,
        capability,
        args.pane_id,
        args.client_name,
    )


if __name__ == "__main__":
    raise SystemExit(main())
