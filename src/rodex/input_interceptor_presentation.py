"""Configured live completion and submitted placeholders through one interaction pipeline."""

from __future__ import annotations

from wcwidth import wcswidth

from .input_interceptor_config import InputInterceptorRegistration
from .input_menu import INPUT_MENU_TARGET, InputMenuView
from .interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from .pane_control import TmuxPaneController
from .tmux_session_capability import TmuxRuntimeCapability


class InputInterceptorPresentation:
    """One handler binding per registration; no command-specific terminal mechanics."""

    def __init__(
        self,
        pipeline: SessionInteractionPipeline,
        registrations: tuple[InputInterceptorRegistration, ...],
        capability: TmuxRuntimeCapability,
        pane: TmuxPaneController,
    ) -> None:
        self._pipeline = pipeline
        self._pane = pane
        self._registrations = {entry.target: entry for entry in registrations}
        self._targets = (
            InteractionTarget(
                INPUT_MENU_TARGET,
                str(capability.runtime_id),
                frozenset({InteractionOperation.INTERACTIVE_INPUT, InteractionOperation.INPUT_RELEASE}),
                exists=lambda: True,
                deliver=self._deliver,
            ),
            *(
                InteractionTarget(
                    entry.target,
                    str(capability.runtime_id),
                    frozenset({InteractionOperation.SUBMITTED_COMMAND}),
                    exists=lambda: True,
                    deliver=self._deliver,
                )
                for entry in registrations
            ),
        )
        for target in self._targets:
            self._pipeline.register(target)

    def confirm_native_prefix(self, prefix: str) -> bool:
        """A candidate is not editor state: require the exact visible prefix and end cursor."""
        snapshot = self._pane.capture_cursor_line()
        if snapshot is None:
            return False
        line, cursor_x = snapshot
        indentation = line[: len(line) - len(line.lstrip())]
        expected_line = f"{indentation}\u203a {prefix}"
        return line.rstrip() == expected_line.rstrip() and cursor_x == wcswidth(expected_line)

    def _deliver(self, request: InteractionRequest) -> InteractionResult:
        if request.operation == InteractionOperation.INPUT_RELEASE:
            return self._display(None)
        elif request.operation == InteractionOperation.SUBMITTED_COMMAND:
            entry = self._registrations[request.target]
            assert request.text is not None
            if not entry.on_enter.matches(request.text):
                return InteractionResult(DeliveryStatus.REJECTED, "submitted input does not match its configured rule")
            return self._pipeline.send_message(
                target="main",
                text=f"{request.text}: placeholder only — no action performed.",
                source=entry.target,
                start_model_turn=False,
            )
        else:
            assert request.text is not None
            if not isinstance(request.payload, str):
                return InteractionResult(DeliveryStatus.REJECTED, "interactive input requires its menu view")
            view = InputMenuView.deserialize(request.payload)
            if request.text != view.draft:
                return InteractionResult(DeliveryStatus.REJECTED, "menu draft and presentation must agree")
            return self._display(view)

    def _display(self, state: InputMenuView | None) -> InteractionResult:
        return self._pipeline.execute(
            InteractionRequest(
                "terminal",
                InteractionOperation.DISPLAY_STATE,
                INPUT_MENU_TARGET,
                payload=state.serialize() if state else None,
            )
        )

    def close(self) -> None:
        try:
            self._display(None)
        finally:
            for target in self._targets:
                self._pipeline.unregister(target)
