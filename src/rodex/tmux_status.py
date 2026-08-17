"""One publisher-aware pipeline for Rodex tmux status-left rendering."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from enum import IntEnum
from typing import Final

STATUS_LEFT_CLAIM_PRIORITY_OPTION: Final = "@rodex_status_left_claim_priority"
STATUS_LEFT_CLAIM_PUBLISHER_OPTION: Final = "@rodex_status_left_claim_publisher"
STATUS_LEFT_CLAIM_TOKEN_OPTION: Final = "@rodex_status_left_claim_token"
STATUS_ANIMATION_TOKEN_OPTION: Final = "@rodex_status_animation_token"
STATUS_LEFT_PUBLISHER_COMPLETION: Final = "completion"
STATUS_LEFT_PUBLISHER_SHARED_CTRL_C: Final = "shared-ctrl-c"
RODEX_BASE_STATUS_LEFT_FORMAT: Final = (
    "#[fg=green]#[bold] Rodex: #S "
    "#[fg=cyan]#[bold]| Tools: #{@rodex_tool_calls} "
    "#[fg=yellow]#[bold]| Mouse: #{?mouse,ON,OFF} #[default]"
)
PREFIX_MODE_STATUS_FORMAT: Final = "#[bg=colour24]#[fg=white]#[bold] CTRL-B MODE #[default]"
RODEX_STATUS_LEFT_FORMAT: Final = (
    "#{?#{&&:#{client_prefix},#{==:#{prefix},C-b}},"
    f"{PREFIX_MODE_STATUS_FORMAT},"
    f"{RODEX_BASE_STATUS_LEFT_FORMAT}"
    "}"
)
RODEX_STATUS_LEFT_LENGTH: Final = "160"

BoundTmuxCommand = Callable[..., subprocess.CompletedProcess[str]]


class StatusLeftPriority(IntEnum):
    """Stable precedence for competing passive status-left messages."""

    COMPLETION = 10
    SAFETY_WARNING = 100


class TmuxStatusLeftPipeline:
    """Atomically publish and restore pane status-left claims."""

    def __init__(self, tmux: BoundTmuxCommand, pane_target: str) -> None:
        self._tmux = tmux
        self._pane_target = pane_target

    def publish_transient(
        self,
        *,
        publisher: str,
        token: str,
        priority: StatusLeftPriority,
        status_format: str,
    ) -> bool:
        """Atomically claim and render unless a higher priority is present."""
        if not publisher or not token or not status_format:
            raise ValueError("status publisher, token, and format must be non-empty")
        claim_and_render = _tmux_command_sequence(
            (
                "set-option",
                "-t",
                self._pane_target,
                STATUS_LEFT_CLAIM_PUBLISHER_OPTION,
                publisher,
            ),
            (
                "set-option",
                "-t",
                self._pane_target,
                STATUS_LEFT_CLAIM_TOKEN_OPTION,
                token,
            ),
            (
                "set-option",
                "-t",
                self._pane_target,
                STATUS_LEFT_CLAIM_PRIORITY_OPTION,
                str(int(priority)),
            ),
            (
                "set-option",
                "-t",
                self._pane_target,
                "status-left",
                status_format,
            ),
        )
        result = self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            f"#{{<=:#{{{STATUS_LEFT_CLAIM_PRIORITY_OPTION}}},{int(priority)}}}",
            claim_and_render,
        )
        if result.returncode != 0:
            return False
        advertised = self._tmux(
            "show-options",
            "-v",
            "-t",
            self._pane_target,
            STATUS_LEFT_CLAIM_TOKEN_OPTION,
        )
        return advertised.returncode == 0 and advertised.stdout.strip() == token

    def restore_if_token_matches(self, token: str) -> None:
        """Atomically restore only when the exact publisher token still matches."""
        self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            f"#{{==:#{{{STATUS_LEFT_CLAIM_TOKEN_OPTION}}},{token}}}",
            self._restore_base_status_command(),
        )

    def restore_if_publisher_matches(self, publisher: str) -> None:
        """Atomically restore only when the named subsystem still publishes."""
        self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            f"#{{==:#{{{STATUS_LEFT_CLAIM_PUBLISHER_OPTION}}},{publisher}}}",
            self._restore_base_status_command(),
        )

    def reset_to_base_status(self) -> bool:
        """Clear every transient claim and publish the normal Rodex status."""
        result = self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            "1",
            self._restore_base_status_command(),
        )
        return result.returncode == 0

    def _restore_base_status_command(self) -> str:
        return _tmux_command_sequence(
            *(
                (
                    "set-option",
                    "-u",
                    "-t",
                    self._pane_target,
                    option,
                )
                for option in (
                    STATUS_LEFT_CLAIM_PUBLISHER_OPTION,
                    STATUS_LEFT_CLAIM_TOKEN_OPTION,
                    STATUS_LEFT_CLAIM_PRIORITY_OPTION,
                )
            ),
            (
                "set-option",
                "-t",
                self._pane_target,
                "status-left",
                RODEX_STATUS_LEFT_FORMAT,
            ),
        )


def _tmux_command_sequence(*commands: tuple[str, ...]) -> str:
    return " ; ".join(shlex.join(command) for command in commands)


def completion_status_left_format(message: str) -> str:
    """Render a passive Rodex completion hint in the ordinary status line."""
    return f"#[fg=magenta,bold] {message} #[default]"
