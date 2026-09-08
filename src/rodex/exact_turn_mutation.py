"""Canonical identity, locking, and policy boundary for exact turn mutations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from rodex_registry import (
    RodexSessionNames,
    lookup_owned_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_session_id_from_a_rodex_sessions_id,
    lookup_rodex_session_names,
    lookup_rodex_tmux_session,
    open_a_user_defined_cool_name_assignment,
)

from .control import (
    CodexControlClient,
    CodexThreadState,
    LiveRodexControl,
    PromptDispatch,
    RodexControlError,
)
from .errors import ExactRuntimeIdentityRequiredError, RodexLaunchError
from .interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)
from .live_runtime import (
    rename_tmux_identity,
    require_durable_runtime_instance,
    resolve_live_control,
    restore_tmux_identity,
    revalidate_live_control,
    session_transition_lock,
)
from .runtime import LiveTmuxSession, RodexRuntimeError, RodexRuntimeLauncher


@dataclass(frozen=True, slots=True)
class _LockedSessionSelection:
    """One selector proven stable while its session transition lock is held."""

    selector: str
    session_id: int


@dataclass(frozen=True, slots=True)
class ExactTurnTarget:
    """One live, durable runtime incarnation returned by an exact mutation."""

    selector: str
    session_id: int
    display_name: str
    runtime: LiveTmuxSession
    control: LiveRodexControl


class ExactTurnMutationCoordinator:
    """Own selector stability, incarnation checks, and exact mutation policy."""

    def __init__(
        self,
        database_path: Path,
        launcher: RodexRuntimeLauncher,
        control_client: CodexControlClient,
        *,
        interaction_pipeline: SessionInteractionPipeline | None = None,
    ) -> None:
        self._database_path = database_path
        self._launcher = launcher
        self._control_client = control_client
        self.interactions = interaction_pipeline if interaction_pipeline is not None else SessionInteractionPipeline()

    @contextmanager
    def _locked_selector(self, selector: str) -> Iterator[_LockedSessionSelection]:
        """Lock the initially selected session, then fail if the selector moved."""
        session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(selector, self._database_path)
        if session_id is None:
            raise RodexLaunchError(f"unknown Rodex session: {selector}")
        rodex_session_id = lookup_rodex_session_id_from_a_rodex_sessions_id(session_id, self._database_path)
        if rodex_session_id is None:
            raise RodexLaunchError(f"Rodex session disappeared: {selector}")
        with session_transition_lock(self._database_path, rodex_session_id):
            locked_session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(selector, self._database_path)
            if locked_session_id != session_id:
                raise RodexLaunchError("Rodex session selector changed while waiting for its transition lock")
            yield _LockedSessionSelection(selector, session_id)

    def start(
        self,
        selector: str,
        prompt: str,
        *,
        dispatch_id: str | None,
        expected_runtime_id: str | None = None,
        expected_thread_id: str | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ) -> tuple[ExactTurnTarget, PromptDispatch]:
        with self._locked_selector(selector) as selection:
            target = self._resolve_target(selection)
            if expected_runtime_id is not None and str(target.control.runtime_id) != expected_runtime_id:
                raise RodexControlError("model message runtime changed before dispatch")
            if expected_thread_id is not None and str(target.control.codex_session_id) != expected_thread_id:
                raise RodexControlError("model message thread changed before dispatch")
            exact_revalidator = self._revalidator(target)

            def revalidate() -> None:
                exact_revalidator()
                if before_dispatch is not None:
                    before_dispatch()

            dispatch = self._dispatch_turn_operation(
                target,
                InteractionOperation.MESSAGE,
                prompt=prompt,
                dispatch_id=dispatch_id,
                revalidate=revalidate,
            )
            return target, cast(PromptDispatch, dispatch)

    def steer(
        self,
        selector: str,
        turn_id: str,
        prompt: str,
        *,
        dispatch_id: str | None,
    ) -> tuple[ExactTurnTarget, PromptDispatch]:
        with self._locked_selector(selector) as selection:
            target = self._resolve_target(selection)
            dispatch = self._dispatch_turn_operation(
                target,
                InteractionOperation.STEER,
                prompt=prompt,
                turn_id=turn_id,
                dispatch_id=dispatch_id,
                revalidate=self._revalidator(target),
            )
            return target, cast(PromptDispatch, dispatch)

    def interrupt(
        self,
        selector: str,
        turn_id: str,
    ) -> tuple[ExactTurnTarget, CodexThreadState]:
        with self._locked_selector(selector) as selection:
            target = self._resolve_target(selection)
            state = self._dispatch_turn_operation(
                target,
                InteractionOperation.INTERRUPT,
                turn_id=turn_id,
                revalidate=self._revalidator(target),
            )
            return target, cast(CodexThreadState, state)

    def mouse_mode(
        self,
        selector: str,
        mode: str,
    ) -> tuple[ExactTurnTarget, str]:
        """Inspect or mutate mouse state on one locked, immutable runtime target."""
        with self._locked_selector(selector) as selection:
            target = self._resolve_target(selection)
            runtime_id = target.control.runtime_id
            if runtime_id is None:
                raise ExactRuntimeIdentityRequiredError("live runtime lacks the current exact runtime identity")
            target = replace(
                target,
                runtime=replace(target.runtime, runtime_id=runtime_id),
            )
            mouse_state = self._launcher.set_mouse_mode(target.runtime, mode)
            self._revalidator(target)()
            return target, mouse_state

    def alias_transition(
        self,
        selector: str,
        requested_name: str,
        *,
        force: bool,
    ) -> str:
        """Serialize, rename, finalize, and announce one exact alias transition."""
        with self._locked_selector(selector) as selection:
            return self._alias_transition_locked(selection, requested_name, force=force)

    def _alias_transition_locked(
        self,
        selection: _LockedSessionSelection,
        requested_name: str,
        *,
        force: bool,
    ) -> str:
        target: ExactTurnTarget | None = None
        recorded_tmux: LiveTmuxSession | None = None
        active_tmux: LiveTmuxSession | None = None
        previous_display_name: str | None = None
        try:
            with open_a_user_defined_cool_name_assignment(
                selection.selector,
                requested_name,
                self._database_path,
                force=force,
            ) as assignment:
                previous_display_name = self._locked_session_names(selection).display_name
                if previous_display_name != assignment.names.display_name:
                    target = self._resolve_locked_live_target(selection)
                    if target is not None:
                        recorded_tmux = target.runtime
                if assignment.tmux_session is not None and recorded_tmux is not None and target is not None:
                    self._runtime_revalidator(
                        target.session_id,
                        target.runtime,
                        target.control,
                    )()
                    active_tmux = rename_tmux_identity(
                        self._launcher,
                        recorded_tmux,
                        assignment.names.display_name,
                    )
                    assignment.renamed_tmux_session_name = active_tmux.tmux_session_name
        except BaseException:
            if (
                recorded_tmux is not None
                and active_tmux is not None
                and active_tmux.tmux_session_name != recorded_tmux.tmux_session_name
            ):
                restore_tmux_identity(self._launcher, active_tmux, recorded_tmux)
            raise
        if active_tmux is not None:
            self._launcher.refresh_shared_tmux_coordination(active_tmux)
        if active_tmux is not None and target is not None and previous_display_name != assignment.names.display_name:
            auto_info = (
                f"RODEX_AUTO_INFO: Rodex session {target.control.rodex_session_id} "
                f"is now named {assignment.names.display_name!r}."
            )
            try:
                self._deliver_information(target, active_tmux, auto_info)
            except (RodexControlError, RodexLaunchError, RodexRuntimeError) as error:
                raise RodexLaunchError(
                    f"Rodex name changed to {assignment.names.display_name!r}, but "
                    f"RODEX_AUTO_INFO delivery failed: {error}"
                ) from error
        return assignment.names.display_name

    def _resolve_locked_live_target(
        self,
        selection: _LockedSessionSelection,
    ) -> ExactTurnTarget | None:
        tmux_link = lookup_rodex_tmux_session(
            selection.session_id,
            self._database_path,
        )
        if tmux_link is None:
            return None
        recorded_runtime = LiveTmuxSession(
            Path(tmux_link.tmux_server_socket_path),
            tmux_link.tmux_session_name,
        )
        if not self._launcher.session_exists(recorded_runtime):
            return None
        return self._resolve_target(selection)

    def _locked_session_names(
        self,
        selection: _LockedSessionSelection,
    ) -> RodexSessionNames:
        names = lookup_rodex_session_names(
            selection.session_id,
            self._database_path,
        )
        if names is None:
            raise RodexLaunchError(f"Rodex session disappeared: {selection.selector}")
        return names

    def _deliver_information(
        self,
        target: ExactTurnTarget,
        runtime: LiveTmuxSession,
        prompt: str,
    ) -> PromptDispatch:
        self._require_durable_runtime(target.session_id, target.control)
        revalidate = self._runtime_revalidator(
            target.session_id,
            runtime,
            target.control,
        )
        state = self._control_client.inspect_live(target.control)
        revalidate()
        if state.status == "idle":
            return cast(
                PromptDispatch,
                self._dispatch_turn_operation(
                    target,
                    InteractionOperation.MESSAGE,
                    prompt=prompt,
                    revalidate=revalidate,
                    source="alias-announcement",
                ),
            )
        if state.status == "active" and state.active_turn_id is not None:
            return cast(
                PromptDispatch,
                self._dispatch_turn_operation(
                    target,
                    InteractionOperation.STEER,
                    prompt=prompt,
                    turn_id=state.active_turn_id,
                    revalidate=revalidate,
                    source="alias-announcement",
                ),
            )
        raise RodexLaunchError(f"Codex thread cannot accept Rodex information while {state.status}")

    def _dispatch_turn_operation(
        self,
        target: ExactTurnTarget,
        operation: InteractionOperation,
        *,
        revalidate: Callable[[], None],
        prompt: str | None = None,
        turn_id: str | None = None,
        dispatch_id: str | None = None,
        source: str = "exact-control",
    ) -> PromptDispatch | CodexThreadState:
        """One model-delivery adapter, called only while the existing transition lock is held."""
        request = InteractionRequest(
            target=f"model:{target.control.codex_session_id}",
            operation=operation,
            source=source,
            text=prompt,
            start_model_turn=operation == InteractionOperation.MESSAGE,
            expected_turn_id=turn_id,
            dispatch_id=dispatch_id,
        )

        def deliver(accepted: InteractionRequest) -> InteractionResult:
            if accepted.operation == InteractionOperation.MESSAGE and accepted.start_model_turn:
                assert accepted.text is not None
                dispatched = self._control_client._start_turn(
                    target.control,
                    accepted.text,
                    dispatch_id=accepted.dispatch_id,
                    revalidate=revalidate,
                )
                return InteractionResult(DeliveryStatus.MODEL_TURN_STARTED, value=dispatched)
            if accepted.operation == InteractionOperation.STEER:
                assert accepted.expected_turn_id is not None and accepted.text is not None
                dispatched = self._control_client._steer_turn(
                    target.control,
                    accepted.expected_turn_id,
                    accepted.text,
                    dispatch_id=accepted.dispatch_id,
                    revalidate=revalidate,
                )
                return InteractionResult(DeliveryStatus.COMPLETED, value=dispatched)
            if accepted.operation == InteractionOperation.INTERRUPT:
                assert accepted.expected_turn_id is not None
                state = self._control_client._interrupt_turn(
                    target.control, accepted.expected_turn_id, revalidate=revalidate
                )
                return InteractionResult(DeliveryStatus.COMPLETED, value=state)
            return InteractionResult(DeliveryStatus.REJECTED, "exact model adapter does not display messages")

        binding = InteractionTarget(
            request.target,
            str(target.control.runtime_id),
            frozenset({operation}),
            # The target was resolved under the transition lock above; the control
            # adapter revalidates immediately before its RPC, after transport waits.
            lambda: True,
            deliver,
            model_thread_id=lambda: str(target.control.codex_session_id),
        )
        self.interactions.register(binding)
        try:
            result = self.interactions.execute(request)
            if not result.accepted:
                raise RodexControlError(result.detail)
            return cast(PromptDispatch | CodexThreadState, result.value)
        finally:
            self.interactions.unregister(binding)

    def _resolve_target(self, selection: _LockedSessionSelection) -> ExactTurnTarget:
        session_id, runtime, control = resolve_live_control(
            selection.selector,
            self._database_path,
            self._launcher,
        )
        if session_id != selection.session_id:
            raise RodexLaunchError("Rodex session selector changed during exact control discovery")
        names = lookup_rodex_session_names(session_id, self._database_path)
        if names is None:
            raise RodexLaunchError(f"Rodex session disappeared: {selection.selector}")
        self._require_durable_runtime(session_id, control)
        return ExactTurnTarget(
            selection.selector,
            selection.session_id,
            names.display_name,
            runtime,
            control,
        )

    def _revalidator(self, target: ExactTurnTarget) -> Callable[[], None]:
        def revalidate() -> None:
            current_session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(
                target.selector,
                self._database_path,
            )
            if current_session_id != target.session_id:
                raise RodexLaunchError("Rodex session selector changed during exact turn mutation")
            self._runtime_revalidator(
                target.session_id,
                target.runtime,
                target.control,
            )()

        return revalidate

    def _runtime_revalidator(
        self,
        session_id: int,
        runtime: LiveTmuxSession,
        control: LiveRodexControl,
    ) -> Callable[[], None]:
        def revalidate() -> None:
            revalidate_live_control(self._launcher, runtime, control)
            self._require_durable_runtime(session_id, control)

        return revalidate

    def _require_durable_runtime(
        self,
        session_id: int,
        control: LiveRodexControl,
    ) -> None:
        require_durable_runtime_instance(session_id, self._database_path, control)
