"""Placeholder command presentation using the existing message and status pipelines."""

from __future__ import annotations

import shlex
import uuid

from .input_interceptor_config import InputInterceptorRegistration
from .interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from .pane_control import TmuxPaneController
from .tmux_executor import SyncTmuxExecutor
from .tmux_session_capability import (
    TmuxRuntimeCapability,
    primary_pane_capability_if_shell_condition,
    tmux_format_literal,
)
from .tmux_status import StatusPriority, TmuxStatusPipeline, TmuxStatusPresentation


class InputInterceptorPresentation:
    """One handler binding per registration; no command-specific terminal mechanics."""

    def __init__(
        self,
        pipeline: SessionInteractionPipeline,
        registrations: tuple[InputInterceptorRegistration, ...],
        tmux_binary: str,
        capability: TmuxRuntimeCapability,
        pane: TmuxPaneController,
    ) -> None:
        self._pipeline = pipeline
        self._pane = pane
        self._capability = capability
        self._executor = SyncTmuxExecutor(tmux_binary, capability.tmux_server_socket_path)
        self._status = TmuxStatusPipeline(self._bound_tmux, pane.known_pane_id)
        self._token = uuid.uuid4().hex
        self._menu_shown = False
        self._registrations = {entry.target: entry for entry in registrations}
        self._targets = tuple(
            InteractionTarget(
                entry.target,
                str(capability.runtime_id),
                frozenset(
                    {
                        InteractionOperation.INTERACTIVE_INPUT,
                        InteractionOperation.SUBMITTED_COMMAND,
                        InteractionOperation.INPUT_RELEASE,
                    }
                ),
                exists=lambda: True,
                deliver=self._deliver,
            )
            for entry in registrations
        )
        for target in self._targets:
            self._pipeline.register(target)

    def confirm_native_prefix(self, prefix: str) -> bool:
        """A candidate is not editor state: require the exact visible prefix and end cursor."""
        snapshot = self._pane.capture_cursor_line()
        if snapshot is None:
            return False
        line, cursor_x = snapshot
        return line.lstrip() == f"\u203a {prefix}" and cursor_x == len(line)

    def _bound_tmux(self, *arguments: str):
        return self._executor.run(
            (
                "if-shell",
                "-t",
                self._pane.known_pane_id,
                "-F",
                primary_pane_capability_if_shell_condition(self._capability),
                shlex.join(arguments),
                shlex.join(("run-shell", "false")),
            )
        )

    def _deliver(self, request: InteractionRequest) -> InteractionResult:
        entry = self._registrations[request.target]
        if request.operation == InteractionOperation.INPUT_RELEASE:
            self._status.restore_if_token_matches(self._token)
            self._menu_shown = False
        elif request.operation == InteractionOperation.SUBMITTED_COMMAND:
            return self._pipeline.send_message(
                target="main",
                text=f"{entry.command}: placeholder only — no commands are registered yet.",
                source=entry.target,
                start_model_turn=False,
            )
        else:
            assert request.text is not None
            # User text must stay literal even in tmux's formatting language.
            visible_draft = "".join(character if character.isprintable() else " " for character in request.text)
            draft = tmux_format_literal(" ".join(visible_draft.split()))
            shown = self._status.publish_transient(
                publisher="input-interceptor",
                token=self._token,
                priority=StatusPriority.LOCAL_INPUT,
                presentation=TmuxStatusPresentation(
                    status_format=f"#[bold] Rodex local: {draft} #[default] | Enter: local command · Esc: return"
                ),
            )
            if not shown:
                return InteractionResult(DeliveryStatus.REJECTED, "local input presentation is unavailable")
            if request.text == entry.command and not self._menu_shown:
                result = self._pipeline.send_message(
                    target="main",
                    source=entry.target,
                    start_model_turn=False,
                    text=(
                        f"{entry.command} — {entry.description}\nPlaceholder menu: no commands registered. "
                        "Enter handles input locally; Escape returns to Codex."
                    ),
                )
                self._menu_shown = result.accepted
        return InteractionResult(DeliveryStatus.DELIVERED)

    def close(self) -> None:
        try:
            self._status.restore_if_token_matches(self._token)
        finally:
            for target in self._targets:
                self._pipeline.unregister(target)
