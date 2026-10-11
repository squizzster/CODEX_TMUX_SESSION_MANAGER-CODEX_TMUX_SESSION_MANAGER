"""Coalesced, volatile command-policy status through the tmux claim owner."""

from __future__ import annotations

import shlex
import subprocess
import time
import unicodedata
import uuid
from contextlib import suppress
from threading import Condition, Thread

from wcwidth import wcwidth

from .status_bar import prefix_indicated_status_format
from .tmux_executor import SyncTmuxExecutor, TmuxCommandResult
from .tmux_session_capability import TmuxRuntimeCapability, primary_pane_capability_if_shell_condition
from .tmux_status import StatusPriority, TmuxStatusPipeline, TmuxStatusPresentation

POLICY_STATUS_SECONDS = 5.0
POLICY_STATUS_WIDTH = 140
POLICY_STATUS_PUBLISHER = "command-policy"
POLICY_INACTIVE_OPTION = "@rodex_command_policy_inactive"


def policy_status_format(message: str) -> str:
    """Remove controls, collapse whitespace, bound cell width, escape tmux formats."""
    text = "".join(
        " " if char.isspace() else char
        for char in message
        if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    text = " ".join(text.split())
    cells = 0
    bounded: list[str] = []
    for char in text:
        width = max(0, wcwidth(char))
        if cells + width > POLICY_STATUS_WIDTH - 1 or len(bounded) >= POLICY_STATUS_WIDTH * 2:
            bounded.append("…")
            break
        bounded.append(char)
        cells += width
    literal = "".join(bounded).replace("#", "##")
    return f" {literal} "


def policy_status_presentation(message: str) -> str:
    """Keep tmux's prefix indicator reachable while policy status owns the row."""
    return prefix_indicated_status_format(policy_status_format(message))


class CommandPolicyStatus:
    """One sleeping publisher, one pending message, no protocol or disk writes."""

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
        self._duration = duration
        self._condition = Condition()
        self._pending: str | None = None
        self._closed = False
        self._thread: Thread | None = None

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
        formatted = policy_status_presentation(message)
        with self._condition:
            if self._closed:
                return
            self._pending = formatted
            if self._thread is None:
                self._thread = Thread(target=self._run, name="rodex-policy-status", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def _run(self) -> None:
        token: str | None = None
        expires = 0.0
        try:
            while True:
                with self._condition:
                    while not self._closed and self._pending is None:
                        remaining = expires - time.monotonic() if token else None
                        if remaining is not None and remaining <= 0:
                            break
                        self._condition.wait(remaining)
                    if self._closed:
                        return
                    pending, self._pending = self._pending, None
                if token:
                    with suppress(Exception):
                        self._pipeline.restore_if_token_matches(token)
                    token = None
                if pending is not None:
                    candidate = uuid.uuid4().hex
                    with suppress(Exception):
                        # Retain the token even if publication's confirmation fails:
                        # cleanup still restores only this exact owned presentation.
                        token = candidate
                        self._pipeline.publish_transient(
                            publisher=POLICY_STATUS_PUBLISHER,
                            token=candidate,
                            priority=StatusPriority.COMMAND_POLICY,
                            presentation=TmuxStatusPresentation(status_format=pending),
                        )
                    expires = time.monotonic() + self._duration
        finally:
            if token:
                with suppress(Exception):
                    self._pipeline.restore_if_token_matches(token)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)
