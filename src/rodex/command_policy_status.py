"""Coalesced, volatile command-policy status through the tmux claim owner."""

from __future__ import annotations

import shlex
import subprocess

from .tmux_executor import SyncTmuxExecutor, TmuxCommandResult
from .tmux_session_capability import TmuxRuntimeCapability, primary_pane_capability_if_shell_condition
from .tmux_status import StatusPriority, TimedStatusPublisher, TmuxStatusPipeline, TmuxStatusPresentation

POLICY_STATUS_SECONDS = 5.0
POLICY_STATUS_WIDTH = 140
POLICY_STATUS_PUBLISHER = "command-policy"
POLICY_INACTIVE_OPTION = "@rodex_command_policy_inactive"


class CommandPolicyStatus:
    """Adapt policy decisions and inactive metadata to the shared status library."""

    def __init__(
        self,
        tmux_binary: str,
        capability: TmuxRuntimeCapability,
        pane_target: str,
        *,
        duration: float = POLICY_STATUS_SECONDS,
    ) -> None:
        self._executor = SyncTmuxExecutor(tmux_binary, capability.tmux_server_socket_path, runner=subprocess.run)
        self._capability = capability
        self._pane_target = pane_target
        self._pipeline = TmuxStatusPipeline(self._tmux, pane_target)
        self._publisher = TimedStatusPublisher(
            self._pipeline,
            publisher=POLICY_STATUS_PUBLISHER,
            priority=StatusPriority.COMMAND_POLICY,
            duration=duration,
        )

    def _tmux(self, *arguments: str) -> TmuxCommandResult:
        return self._executor.run(
            (
                "if-shell",
                "-t",
                self._pane_target,
                "-F",
                primary_pane_capability_if_shell_condition(self._capability),
                shlex.join(arguments),
            )
        )

    def allowed(self, command: str) -> None:
        self.publish(f"Rodex policy allowed: {command}")

    def inactive(self, reason: str) -> None:
        """Retain fixed setup metadata until the registered UI can present it."""
        self._tmux("set-option", "-t", self._pane_target, POLICY_INACTIVE_OPTION, reason)

    def active(self) -> None:
        """Clear stale inactive metadata and only this subsystem's presentation."""
        self._tmux("set-option", "-u", "-t", self._pane_target, POLICY_INACTIVE_OPTION)
        self._pipeline.restore_if_publisher_matches(POLICY_STATUS_PUBLISHER)

    def publish(self, message: str) -> None:
        # Sanitize before retaining the pending value: no hidden full-command queue.
        self._publisher.publish(TmuxStatusPresentation.message(message, width=POLICY_STATUS_WIDTH))

    def close(self) -> None:
        self._publisher.close()
