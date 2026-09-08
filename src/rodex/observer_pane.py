"""Observer presentation adapter beneath the shared interaction pipeline."""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import RLock

from .interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from .observer_contract import OBSERVER_TRANSPORT_METADATA_MAX_BYTES
from .pane_control import TmuxPaneController
from .tmux_executor import SyncTmuxRunner
from .tmux_session_capability import TmuxRuntimeCapability

OBSERVER_TARGET = "agent-observer"


class ObserverPaneController:
    """Submit observer lifecycle and content through the same API as the main chat."""

    def __init__(
        self,
        tmux_binary: str,
        capability: TmuxRuntimeCapability,
        primary_pane_target: str,
        *,
        runner: SyncTmuxRunner,
        python_executable: str = sys.executable,
        pipeline: SessionInteractionPipeline | None = None,
        state_sender: Callable[[dict[str, object]], None] | None = None,
        message_sender: Callable[[str, str | None], None] | None = None,
        snapshot_publisher: Callable[[], None] | None = None,
    ) -> None:
        self._pane = TmuxPaneController(
            tmux_binary,
            capability,
            primary_pane_target,
            runner=runner,
            python_executable=python_executable,
            primary_pane_option="@rodex_agent_observer_pane_id",
            owner_pane_option="@rodex_agent_observer_for",
        )
        self._python_executable = python_executable
        self.pipeline = pipeline if pipeline is not None else SessionInteractionPipeline()
        self._state_sender = state_sender
        self._message_sender = message_sender
        self._snapshot_publisher = snapshot_publisher
        self._open_lock = RLock()
        self._open_request: InteractionRequest | None = None
        self._target = InteractionTarget(
            OBSERVER_TARGET,
            str(capability.runtime_id),
            frozenset(
                {
                    InteractionOperation.MESSAGE,
                    InteractionOperation.DISPLAY_STATE,
                    InteractionOperation.OPEN,
                    InteractionOperation.LOCATE,
                    InteractionOperation.FOCUS,
                    InteractionOperation.RESIZE,
                    InteractionOperation.CLOSE,
                }
            ),
            lambda: self._pane.locate() is not None,
            self._deliver,
            open=self._reopen,
            binding_identity=lambda: self._pane.known_pane_id,
        )
        self.pipeline.register(self._target)

    @staticmethod
    def validate_primary_pane_target(primary_pane_target: str) -> None:
        TmuxPaneController.validate_primary_pane_target(primary_pane_target)

    def locate(self) -> str | None:
        result = self.pipeline.execute(InteractionRequest(OBSERVER_TARGET, InteractionOperation.LOCATE, "observer"))
        return result.value if result.accepted and isinstance(result.value, str) else None

    def create(
        self,
        *,
        database_path: Path,
        rodex_sessions_id: int,
        rodex_session_id: str,
        root_thread_id: uuid.UUID,
        protocol_event_socket_path: Path,
        initial_event: dict[str, object],
    ) -> str | None:
        command = (
            self._python_executable,
            "-m",
            "rodex.agent_observer",
            "--rodex-database",
            str(database_path),
            "--rodex-sessions-id",
            str(rodex_sessions_id),
            "--rodex-session-id",
            rodex_session_id,
            "--root-thread-id",
            str(root_thread_id),
            "--protocol-event-socket",
            str(protocol_event_socket_path),
            "--initial-event",
            json.dumps(initial_event, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )
        request = InteractionRequest(OBSERVER_TARGET, InteractionOperation.OPEN, "observer", payload=json.dumps(command))
        result = self.pipeline.execute(request)
        return result.value if result.accepted and isinstance(result.value, str) else None

    def send_state(self, snapshot: dict[str, object]) -> InteractionResult:
        return self.pipeline.execute(
            InteractionRequest(
                OBSERVER_TARGET,
                InteractionOperation.DISPLAY_STATE,
                "observer-reducer",
                payload=json.dumps(snapshot),
            )
        )

    def _reopen(self) -> InteractionResult:
        if self._open_request is None:
            return InteractionResult(DeliveryStatus.REJECTED, "observer has no registered launch context")
        return self.pipeline.execute(replace(self._open_request, source="observer-reopen", request_id=uuid.uuid4().hex))

    def _deliver(self, request: InteractionRequest) -> InteractionResult:
        operation = request.operation
        if operation == InteractionOperation.OPEN:
            reopening = request.payload is None or request.source == "observer-reopen"
            with self._open_lock:
                pane = self._pane.locate()
                if pane is None:
                    launch = request if request.payload is not None else self._open_request
                    if launch is None or not isinstance(launch.payload, str):
                        return InteractionResult(DeliveryStatus.REJECTED, "observer has no registered launch context")
                    pane = self._pane.create(tuple(json.loads(launch.payload)))
                    if pane is not None:
                        command = json.loads(launch.payload)
                        if "--initial-event" in command:
                            command[command.index("--initial-event") + 1] = "{}"
                        self._open_request = replace(launch, payload=json.dumps(command))
            if pane is not None and reopening and self._snapshot_publisher is not None:
                self._snapshot_publisher()
            return InteractionResult(DeliveryStatus.COMPLETED if pane else DeliveryStatus.FAILED, value=pane)
        if operation == InteractionOperation.LOCATE:
            pane = self._pane.locate()
            return InteractionResult(DeliveryStatus.COMPLETED if pane else DeliveryStatus.REJECTED, value=pane)
        if operation == InteractionOperation.MESSAGE:
            if self._message_sender is None:
                return InteractionResult(DeliveryStatus.REJECTED, "observer message transport is unavailable")
            assert request.text is not None
            self._message_sender(request.text, request.expected_binding)
            return InteractionResult(DeliveryStatus.DELIVERED, "accepted by observer transport, rendering unconfirmed")
        if operation == InteractionOperation.DISPLAY_STATE:
            if self._state_sender is None or not isinstance(request.payload, str):
                return InteractionResult(DeliveryStatus.REJECTED, "observer state transport is unavailable")
            address = {"pane_id": request.expected_binding}
            if len(json.dumps(address).encode()) > OBSERVER_TRANSPORT_METADATA_MAX_BYTES:
                return InteractionResult(DeliveryStatus.REJECTED, "observer pane address exceeds its wire budget")
            self._state_sender(json.loads(request.payload) | address)
            return InteractionResult(DeliveryStatus.QUEUED, "newest-only observer snapshot queued")
        if operation == InteractionOperation.FOCUS:
            assert request.expected_binding is not None
            completed = self._pane.focus(request.expected_binding)
        elif operation == InteractionOperation.RESIZE:
            assert request.size_percent is not None and request.expected_binding is not None
            completed = self._pane.resize(request.expected_binding, request.size_percent)
        elif operation == InteractionOperation.CLOSE:
            assert request.expected_binding is not None
            completed = self._pane.close(request.expected_binding)
        else:
            return InteractionResult(DeliveryStatus.REJECTED, "unsupported observer operation")
        return InteractionResult(DeliveryStatus.COMPLETED if completed else DeliveryStatus.FAILED)
