"""One publisher-aware pipeline for the complete Rodex tmux status presentation."""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from threading import Condition, Thread
from typing import Final

from .status_bar import (
    RODEX_STATUS_COLOURS,
    RODEX_STATUS_LEFT_FORMAT,
    RODEX_STATUS_LEFT_LENGTH,
    RODEX_STATUS_RIGHT_FORMAT,
    RODEX_STATUS_RIGHT_LENGTH,
    RODEX_STATUS_STYLE,
    RODEX_WINDOW_STATUS_FORMAT,
)
from .tmux_executor import SyncTmuxExecutor, SyncTmuxRunner

TMUX_STATUS_COMMAND_TIMEOUT_SECONDS: Final = 1.0
TMUX_STATUS_FAILURE_BACKOFF_SECONDS: Final = 1.0
TMUX_STATUS_WORKER_IDLE_SECONDS: Final = 0.25


class TmuxStatusOption:
    """Publish one rendered status value through one pane-stable tmux option."""

    def __init__(
        self,
        tmux_binary: str,
        tmux_server_socket_path: Path,
        tmux_pane_target: str,
        option_name: str,
        *,
        runner: SyncTmuxRunner = subprocess.run,
        command_timeout_seconds: float = TMUX_STATUS_COMMAND_TIMEOUT_SECONDS,
        failure_backoff_seconds: float = TMUX_STATUS_FAILURE_BACKOFF_SECONDS,
    ) -> None:
        if not tmux_pane_target.strip() or not option_name.startswith("@"):
            raise ValueError("tmux pane target and user option must be valid")
        if command_timeout_seconds <= 0 or failure_backoff_seconds <= 0:
            raise ValueError("tmux status timeouts must be positive")
        self._command_arguments = (
            "set-option",
            "-t",
            tmux_pane_target,
            option_name,
        )
        self._tmux_executor = SyncTmuxExecutor(
            tmux_binary,
            tmux_server_socket_path,
            runner=runner,
            timeout_seconds=command_timeout_seconds,
        )
        self._failure_backoff_seconds = failure_backoff_seconds
        self._condition = Condition()
        self._pending_value: str | None = None
        self._inflight_value: str | None = None
        self._published_value: str | None = None
        self._retry_not_before = 0.0
        self._worker: Thread | None = None
        self._closed = False

    def publish(self, value: str) -> None:
        """Queue one non-empty value without delaying the protocol receiver."""
        if not isinstance(value, str) or not value:
            raise ValueError("tmux status option value must be non-empty")
        worker: Thread | None = None
        with self._condition:
            if self._closed:
                raise RuntimeError("tmux status option is closed")
            if value == self._pending_value:
                return
            if self._pending_value is None and value == self._inflight_value:
                return
            if (
                self._pending_value is None
                and self._inflight_value is None
                and value == self._published_value
            ):
                return
            self._pending_value = value
            if self._worker is None:
                worker = Thread(
                    target=self._publish_pending_values,
                    name="rodex-tmux-status-publisher",
                    daemon=True,
                )
                self._worker = worker
            self._condition.notify_all()
        if worker is not None:
            worker.start()

    def close(self) -> None:
        """Discard queued work and wake a publisher waiting in circuit backoff."""
        with self._condition:
            self._closed = True
            self._pending_value = None
            self._condition.notify_all()

    def _publish_pending_values(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    self._worker = None
                    return
                if self._pending_value is None:
                    self._condition.wait(TMUX_STATUS_WORKER_IDLE_SECONDS)
                    if self._pending_value is None:
                        self._worker = None
                        return
                    continue
                retry_delay = self._retry_not_before - time.monotonic()
                if retry_delay > 0:
                    self._condition.wait(retry_delay)
                    continue
                value = self._pending_value
                self._pending_value = None
                self._inflight_value = value
            published = False
            try:
                result = self._tmux_executor.run(
                    (*self._command_arguments, value),
                    output="discard",
                )
                published = result.returncode == 0
            except Exception:
                # A wedged or missing tmux must neither block the protocol stream nor
                # create one replacement process per incoming status frame. The runner
                # is injected at this boundary, so even an unexpected implementation
                # failure must leave the single publisher worker usable.
                pass
            with self._condition:
                self._inflight_value = None
                if published:
                    self._published_value = value
                    self._retry_not_before = 0.0
                else:
                    self._retry_not_before = (
                        time.monotonic() + self._failure_backoff_seconds
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
            ("window-status-format", RODEX_WINDOW_STATUS_FORMAT),
            ("window-status-current-format", RODEX_WINDOW_STATUS_FORMAT),
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
