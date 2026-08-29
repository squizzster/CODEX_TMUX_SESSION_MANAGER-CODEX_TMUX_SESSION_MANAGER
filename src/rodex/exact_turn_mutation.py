"""Canonical identity, locking, and policy boundary for exact turn mutations."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from rodex_registry import (
    RodexSessionNames,
    lookup_owned_rodex_sessions_id_from_a_cool_name,
    lookup_rodex_runtime_instance,
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
from .errors import RodexLaunchError
from .live_runtime import (
    rename_tmux_identity,
    resolve_live_control,
    restore_tmux_identity,
    revalidate_live_control,
    session_transition_lock,
)
from .runtime import LiveTmuxSession, RodexRuntimeError, RodexRuntimeLauncher


class ExactRuntimeIdentityRequiredError(RodexLaunchError):
    """A live endpoint lacks the durable incarnation required for mutation."""


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
    ) -> None:
        self._database_path = database_path
        self._launcher = launcher
        self._control_client = control_client

    @contextmanager
    def _locked_selector(self, selector: str) -> Iterator[_LockedSessionSelection]:
        """Lock the initially selected session, then fail if the selector moved."""
        session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(
            selector, self._database_path
        )
        if session_id is None:
            raise RodexLaunchError(f"unknown Rodex session: {selector}")
        with session_transition_lock(self._database_path, session_id):
            locked_session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(
                selector, self._database_path
            )
            if locked_session_id != session_id:
                raise RodexLaunchError(
                    "Rodex session selector changed while waiting for its transition lock"
                )
            yield _LockedSessionSelection(selector, session_id)

    def start(
        self,
        selector: str,
        prompt: str,
        *,
        dispatch_id: str | None,
    ) -> tuple[ExactTurnTarget, PromptDispatch]:
        with self._locked_selector(selector) as selection:
            target = self._resolve_target(selection)
            dispatch = self._control_client._start_turn(
                target.control,
                prompt,
                dispatch_id=dispatch_id,
                revalidate=self._revalidator(target),
            )
            return target, dispatch

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
            dispatch = self._control_client._steer_turn(
                target.control,
                turn_id,
                prompt,
                dispatch_id=dispatch_id,
                revalidate=self._revalidator(target),
            )
            return target, dispatch

    def interrupt(
        self,
        selector: str,
        turn_id: str,
    ) -> tuple[ExactTurnTarget, CodexThreadState]:
        with self._locked_selector(selector) as selection:
            target = self._resolve_target(selection)
            state = self._control_client._interrupt_turn(
                target.control,
                turn_id,
                revalidate=self._revalidator(target),
            )
            return target, state

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
                if (
                    assignment.tmux_session is not None
                    and recorded_tmux is not None
                    and target is not None
                ):
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
            self._launcher.refresh_name_bound_hooks(active_tmux)
        if (
            active_tmux is not None
            and target is not None
            and previous_display_name != assignment.names.display_name
        ):
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
            return self._control_client._start_turn(
                target.control,
                prompt,
                revalidate=revalidate,
            )
        if state.status == "active" and state.active_turn_id is not None:
            return self._control_client._steer_turn(
                target.control,
                state.active_turn_id,
                prompt,
                revalidate=revalidate,
            )
        raise RodexLaunchError(
            f"Codex thread cannot accept Rodex information while {state.status}"
        )

    def _resolve_target(self, selection: _LockedSessionSelection) -> ExactTurnTarget:
        session_id, runtime, control = resolve_live_control(
            selection.selector,
            self._database_path,
            self._launcher,
        )
        if session_id != selection.session_id:
            raise RodexLaunchError(
                "Rodex session selector changed during exact control discovery"
            )
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
                raise RodexLaunchError(
                    "Rodex session selector changed during exact turn mutation"
                )
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


def require_durable_runtime_instance(
    session_id: int,
    database_path: Path,
    control: LiveRodexControl,
) -> None:
    """Fail unless live control belongs to the current durable incarnation."""
    persisted = lookup_rodex_runtime_instance(session_id, database_path)
    if persisted is None or control.runtime_id is None:
        raise ExactRuntimeIdentityRequiredError(
            "live runtime predates exact runtime identity; restart it with this "
            "Rodex version"
        )
    if persisted.runtime_id != control.runtime_id:
        raise RodexLaunchError("live runtime ID does not match its durable Rodex identity")
