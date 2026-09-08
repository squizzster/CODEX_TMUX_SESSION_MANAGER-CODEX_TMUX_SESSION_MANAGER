"""Configured live completion and submitted placeholders through one interaction pipeline."""

from __future__ import annotations

from wcwidth import wcswidth

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
from .terminal_completion import TerminalCompletionState
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
        indentation = line[: len(line) - len(line.lstrip())]
        expected_line = f"{indentation}\u203a {prefix}"
        return line.rstrip() == expected_line.rstrip() and cursor_x == wcswidth(expected_line)

    def _deliver(self, request: InteractionRequest) -> InteractionResult:
        entry = self._registrations[request.target]
        if request.operation == InteractionOperation.INPUT_RELEASE:
            return self._display(entry.target, None)
        elif request.operation == InteractionOperation.SUBMITTED_COMMAND:
            assert request.text is not None
            if not entry.on_enter.matches(request.text):
                return InteractionResult(DeliveryStatus.REJECTED, "submitted input does not match its configured rule")
            commands = "\n".join(f"  {command.name} — {command.helper_text}" for command in entry.command_list)
            return self._pipeline.send_message(
                target="main",
                text=(
                    f"{entry.completion_text}: placeholder only — no command execution is registered.\n{commands}"
                    if commands
                    else f"{entry.completion_text}: placeholder only — no commands are registered yet."
                ),
                source=entry.target,
                start_model_turn=False,
            )
        else:
            assert request.text is not None
            if not isinstance(request.payload, str):
                return InteractionResult(DeliveryStatus.REJECTED, "interactive input requires its native prefix")
            matched = entry.live.matches(request.text)
            return self._display(
                entry.target,
                TerminalCompletionState(
                    draft=request.text,
                    native_prefix=request.payload,
                    completion_text=entry.completion_text if matched else "",
                    helper_text=entry.live.helper_text if matched else "",
                ),
            )

    def _display(self, source: str, state: TerminalCompletionState | None) -> InteractionResult:
        return self._pipeline.execute(
            InteractionRequest(
                "terminal", InteractionOperation.DISPLAY_STATE, source, payload=state.serialize() if state else None
            )
        )

    def close(self) -> None:
        try:
            self._display("input-interceptor", None)
        finally:
            for target in self._targets:
                self._pipeline.unregister(target)
