"""Final observer rendering adapter, including bootstrap and SQL-recovered content."""

from __future__ import annotations

import sys

from .interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)


def _write_observer_terminal(request: InteractionRequest) -> InteractionResult:
    assert request.text is not None
    sys.stdout.write(request.text)
    sys.stdout.flush()
    return InteractionResult(DeliveryStatus.DELIVERED, "observer stdout flushed; terminal rendering unconfirmed")


def observer_terminal_pipeline() -> SessionInteractionPipeline:
    pipeline = SessionInteractionPipeline()
    pipeline.register(
        InteractionTarget(
            "agent-observer",
            "observer-process",
            frozenset({InteractionOperation.MESSAGE}),
            lambda: not sys.stdout.closed,
            _write_observer_terminal,
        )
    )
    return pipeline


_OBSERVER_TERMINAL = observer_terminal_pipeline()


def render_observer_lines(lines: tuple[str, ...] | list[str]) -> None:
    if lines:
        result = _OBSERVER_TERMINAL.send_message(
            target="agent-observer",
            text="\n".join(lines) + "\n",
            source="observer-renderer",
        )
        if not result.accepted:
            raise OSError(result.detail)
