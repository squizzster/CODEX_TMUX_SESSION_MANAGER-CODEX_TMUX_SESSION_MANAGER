"""One publisher-aware pipeline for the complete Rodex tmux status presentation."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Final

from .status_bar import (
    RODEX_STATUS_COLOURS,
    RODEX_STATUS_LEFT_FORMAT,
    RODEX_STATUS_LEFT_LENGTH,
    RODEX_STATUS_RIGHT_FORMAT,
    RODEX_STATUS_RIGHT_LENGTH,
    RODEX_STATUS_STYLE,
)


class TmuxStatusOption:
    """Publish one rendered status value through one pane-stable tmux option."""

    def __init__(
        self,
        tmux_binary: str,
        tmux_server_socket_path: Path,
        tmux_pane_target: str,
        option_name: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    ) -> None:
        if not tmux_pane_target.strip() or not option_name.startswith("@"):
            raise ValueError("tmux pane target and user option must be valid")
        self._command_prefix = (
            tmux_binary,
            "-S",
            str(tmux_server_socket_path),
            "set-option",
            "-t",
            tmux_pane_target,
            option_name,
        )
        self._run = runner

    def publish(self, value: str) -> None:
        """Publish one non-empty value without exposing tmux mechanics to its owner."""
        if not isinstance(value, str) or not value:
            raise ValueError("tmux status option value must be non-empty")
        self._run(
            [*self._command_prefix, value],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


STATUS_CLAIM_PRIORITY_OPTION: Final = "@rodex_status_claim_priority"
STATUS_CLAIM_PUBLISHER_OPTION: Final = "@rodex_status_claim_publisher"
STATUS_CLAIM_TOKEN_OPTION: Final = "@rodex_status_claim_token"
STATUS_ANIMATION_FRAME_INTERVAL_SECONDS: Final = 0.2
STATUS_PUBLISHER_COMPLETION: Final = "completion"
STATUS_PUBLISHER_SHARED_CTRL_C: Final = "shared-ctrl-c"
STATUS_PUBLISHER_SHARING_ANIMATION: Final = "sharing-animation"

BoundTmuxCommand = Callable[..., subprocess.CompletedProcess[str]]


class StatusPriority(IntEnum):
    """Stable precedence for every competing Rodex status presentation."""

    COMPLETION = 10
    SHARING_ANIMATION = 20
    SAFETY_WARNING = 100


@dataclass(frozen=True, slots=True)
class TmuxStatusPresentation:
    """One complete transient presentation on one or more tmux status surfaces."""

    status_left: str | None = None
    status_style: str | None = None
    status_format: str | None = None

    def __post_init__(self) -> None:
        values = (self.status_left, self.status_style, self.status_format)
        if not any(values) or any(value is not None and not value for value in values):
            raise ValueError("status presentation requires non-empty configured surfaces")


RODEX_BASE_STATUS_PRESENTATION: Final = TmuxStatusPresentation(
    status_left=RODEX_STATUS_LEFT_FORMAT,
    status_style=RODEX_STATUS_STYLE,
)


class TmuxStatusClaimCommands:
    """Build the authoritative atomic tmux commands for status claim transitions."""

    def __init__(self, pane_target: str) -> None:
        if not pane_target:
            raise ValueError("status claim commands require a pane target")
        self._pane_target = pane_target

    def priority_allows(self, priority: StatusPriority) -> str:
        return f"#{{<=:#{{{STATUS_CLAIM_PRIORITY_OPTION}}},{int(priority):03d}}}"

    def token_matches(self, token: str) -> str:
        if not token:
            raise ValueError("status claim token must be non-empty")
        return f"#{{==:#{{{STATUS_CLAIM_TOKEN_OPTION}}},{token}}}"

    def publisher_matches(self, publisher: str) -> str:
        if not publisher:
            raise ValueError("status claim publisher must be non-empty")
        return f"#{{==:#{{{STATUS_CLAIM_PUBLISHER_OPTION}}},{publisher}}}"

    def no_claim_exists(self) -> str:
        return f"#{{?{STATUS_CLAIM_TOKEN_OPTION},0,1}}"

    def claim_and_present(
        self,
        *,
        publisher: str,
        token: str,
        priority: StatusPriority,
        presentation: TmuxStatusPresentation,
    ) -> str:
        if not publisher or not token:
            raise ValueError("status publisher and token must be non-empty")
        return _tmux_command_sequence(
            (
                "set-option",
                "-t",
                self._pane_target,
                STATUS_CLAIM_PUBLISHER_OPTION,
                publisher,
            ),
            (
                "set-option",
                "-t",
                self._pane_target,
                STATUS_CLAIM_TOKEN_OPTION,
                token,
            ),
            (
                "set-option",
                "-t",
                self._pane_target,
                STATUS_CLAIM_PRIORITY_OPTION,
                f"{int(priority):03d}",
            ),
            *self._presentation_commands(presentation, reset_surfaces=True),
        )

    def present(self, presentation: TmuxStatusPresentation) -> str:
        return _tmux_command_sequence(*self._presentation_commands(presentation))

    def restore_base(self) -> str:
        return _tmux_command_sequence(
            *(
                ("set-option", "-u", "-t", self._pane_target, option)
                for option in (
                    STATUS_CLAIM_PUBLISHER_OPTION,
                    STATUS_CLAIM_TOKEN_OPTION,
                    STATUS_CLAIM_PRIORITY_OPTION,
                )
            ),
            *self._presentation_commands(
                RODEX_BASE_STATUS_PRESENTATION,
                reset_surfaces=True,
            ),
        )

    def set_base_status(self) -> str:
        return _tmux_command_sequence(
            *self._presentation_commands(RODEX_BASE_STATUS_PRESENTATION)
        )

    def _presentation_commands(
        self,
        presentation: TmuxStatusPresentation,
        *,
        reset_surfaces: bool = False,
    ) -> tuple[tuple[str, ...], ...]:
        commands: list[tuple[str, ...]] = []
        if reset_surfaces:
            commands.append(("set-option", "-u", "-t", self._pane_target, "status-format"))
            if presentation.status_left is None:
                commands.append(
                    (
                        "set-option",
                        "-t",
                        self._pane_target,
                        "status-left",
                        RODEX_BASE_STATUS_PRESENTATION.status_left,
                    )
                )
            if presentation.status_style is None:
                commands.append(
                    (
                        "set-option",
                        "-t",
                        self._pane_target,
                        "status-style",
                        RODEX_BASE_STATUS_PRESENTATION.status_style,
                    )
                )
        for option_name, value in (
            ("status-left", presentation.status_left),
            ("status-style", presentation.status_style),
            ("status-format[0]", presentation.status_format),
        ):
            if value is not None:
                commands.append(("set-option", "-t", self._pane_target, option_name, value))
        return tuple(commands)


class TmuxStatusPipeline:
    """Configure the bar and atomically publish, arbitrate, and restore claims."""

    def __init__(self, tmux: BoundTmuxCommand, pane_target: str) -> None:
        self._tmux = tmux
        self._pane_target = pane_target
        self._commands = TmuxStatusClaimCommands(pane_target)

    def configure_base_status(self, *, reset_transient_claims: bool) -> None:
        """Install the complete Rodex bar without trampling a retained live claim."""
        self._tmux("set-option", "-t", self._pane_target, "status", "on")
        if reset_transient_claims:
            self.reset_to_base_status()
        else:
            self.reconcile_base_status()
        for option_name, value in (
            ("status-left-length", RODEX_STATUS_LEFT_LENGTH),
            ("status-right", RODEX_STATUS_RIGHT_FORMAT),
            ("status-right-length", RODEX_STATUS_RIGHT_LENGTH),
        ):
            self._tmux("set-option", "-t", self._pane_target, option_name, value)

    def publish_transient(
        self,
        *,
        publisher: str,
        token: str,
        priority: StatusPriority,
        presentation: TmuxStatusPresentation,
    ) -> bool:
        """Atomically claim and render unless a higher priority is present."""
        result = self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            self._commands.priority_allows(priority),
            self._commands.claim_and_present(
                publisher=publisher,
                token=token,
                priority=priority,
                presentation=presentation,
            ),
        )
        if result.returncode != 0:
            return False
        advertised = self._tmux(
            "show-options",
            "-v",
            "-t",
            self._pane_target,
            STATUS_CLAIM_TOKEN_OPTION,
        )
        return advertised.returncode == 0 and advertised.stdout.strip() == token

    def restore_if_token_matches(self, token: str) -> None:
        """Atomically restore only when the exact publisher token still matches."""
        self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            self._commands.token_matches(token),
            self._commands.restore_base(),
        )

    def restore_if_publisher_matches(self, publisher: str) -> None:
        """Atomically restore only when the named subsystem still publishes."""
        self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            self._commands.publisher_matches(publisher),
            self._commands.restore_base(),
        )

    def reconcile_base_status(self) -> None:
        """Publish the base left status only when no transient currently owns it."""
        self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            self._commands.no_claim_exists(),
            self._commands.set_base_status(),
        )

    def reset_to_base_status(self) -> bool:
        """Clear every transient claim and publish the normal Rodex status."""
        result = self._tmux(
            "if-shell",
            "-t",
            self._pane_target,
            "-F",
            "1",
            self._commands.restore_base(),
        )
        return result.returncode == 0


def _tmux_command_sequence(*commands: tuple[str, ...]) -> str:
    return " ; ".join(shlex.join(command) for command in commands)


def completion_status_left_format(message: str) -> str:
    """Render a passive Rodex completion hint in the ordinary status line."""
    return f"#[fg={RODEX_STATUS_COLOURS.completion},bold] {message} #[default]"
