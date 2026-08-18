"""Protect a shared Rodex session from one-client accidental Ctrl-C exit."""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from .status_bar import RODEX_STATUS_COLOURS
from .tmux_status import (
    STATUS_PUBLISHER_SHARED_CTRL_C,
    StatusPriority,
    TmuxStatusPipeline,
    TmuxStatusPresentation,
)

_CONFIRMATION_OPTION: Final = "@rodex_shared_ctrl_c_confirmation"
_CONFIRMATION_WINDOW_NANOSECONDS: Final = 2_000_000_000
_CONFIRMATION_WINDOW_SECONDS: Final = 2.0
_CONFIRMATION_STATUS: Final = (
    f"#[bg={RODEX_STATUS_COLOURS.safety_background}]"
    f"#[fg={RODEX_STATUS_COLOURS.safety_foreground}]"
    "#[bold] CTRL-C ARMED: SHARED session — "
    "CTRL-C again within 2s may END it for everyone; "
    "CTRL-B d detaches only you. #[default]"
)

Runner = Callable[..., subprocess.CompletedProcess[str]]
ExpiryScheduler = Callable[[Callable[[], None]], None]


def _restore_after_confirmation_window(callback: Callable[[], None]) -> None:
    time.sleep(_CONFIRMATION_WINDOW_SECONDS)
    callback()


def handle_shared_ctrl_c(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    pane_id: str,
    client_name: str,
    attached_client_count: int,
    *,
    monotonic_nanoseconds: Callable[[], int] = time.monotonic_ns,
    confirmation_token: Callable[[], str] = lambda: secrets.token_hex(8),
    expiry_scheduler: ExpiryScheduler = _restore_after_confirmation_window,
    runner: Runner = subprocess.run,
) -> int:
    """Forward Ctrl-C privately, or require same-client confirmation when shared."""
    if attached_client_count < 1:
        raise ValueError("attached_client_count must be positive")
    tmux_prefix = [tmux_binary, "-S", str(tmux_server_socket_path)]

    def tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
        return runner(
            [*tmux_prefix, *arguments],
            check=False,
            text=True,
            capture_output=True,
        )

    def clear_confirmation() -> None:
        tmux("set-option", "-u", "-t", pane_id, _CONFIRMATION_OPTION)

    status = TmuxStatusPipeline(tmux, pane_id)

    advertised = tmux(
        "show-options",
        "-v",
        "-t",
        pane_id,
        _CONFIRMATION_OPTION,
    )
    current_confirmation = _parse_confirmation(advertised.stdout.strip())

    if attached_client_count == 1:
        clear_confirmation()
        if current_confirmation is not None:
            status.restore_if_token_matches(current_confirmation[2])
        return tmux("send-keys", "-t", pane_id, "C-c").returncode

    now = monotonic_nanoseconds()
    if _is_current_confirmation(current_confirmation, client_name, now):
        clear_confirmation()
        assert current_confirmation is not None
        status.restore_if_token_matches(current_confirmation[2])
        return tmux("send-keys", "-t", pane_id, "C-c").returncode

    if current_confirmation is not None:
        clear_confirmation()
        status.restore_if_token_matches(current_confirmation[2])
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
        pane_id,
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
            pane_id,
            _CONFIRMATION_OPTION,
        )
        if latest.stdout.strip() != confirmation:
            return
        clear_confirmation()
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.tmux_shared_ctrl_c")
    parser.add_argument("--tmux-binary", required=True)
    parser.add_argument("--tmux-server-socket", required=True, type=Path)
    parser.add_argument("--pane-id", required=True)
    parser.add_argument("--client-name", required=True)
    parser.add_argument(
        "--attached-count",
        dest="attached_client_count",
        required=True,
        type=int,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return handle_shared_ctrl_c(
        args.tmux_binary,
        args.tmux_server_socket,
        args.pane_id,
        args.client_name,
        args.attached_client_count,
    )


if __name__ == "__main__":
    raise SystemExit(main())
