"""Tmux-only lifecycle for the dedicated agent observer pane."""

from __future__ import annotations

import json
import re
import shlex
import sys
import uuid
from pathlib import Path
from typing import Final

from .tmux_executor import SyncTmuxExecutor, SyncTmuxRunner, TmuxCommandResult
from .tmux_session_capability import (
    TmuxRuntimeCapability,
    capability_identity_if_shell_condition,
    capability_pane_read_arguments,
    combine_tmux_if_shell_conditions,
    primary_pane_capability_if_shell_condition,
    primary_pane_capability_read_arguments,
)

OBSERVER_PRIMARY_PANE_OPTION: Final = "@rodex_agent_observer_pane_id"
OBSERVER_OWNER_PANE_OPTION: Final = "@rodex_agent_observer_for"
_PANE_ID_PATTERN: Final = re.compile(r"%[0-9]+")


class ObserverPaneController:
    """Locate or create one validated observer pane through bounded tmux calls."""

    def __init__(
        self,
        tmux_binary: str,
        capability: TmuxRuntimeCapability,
        primary_pane_target: str,
        *,
        runner: SyncTmuxRunner,
        python_executable: str = sys.executable,
    ) -> None:
        self.validate_primary_pane_target(primary_pane_target)
        self._capability = capability
        self._primary_pane_target = primary_pane_target
        self._python_executable = python_executable
        self._observer_pane_target: str | None = None
        self._tmux_executor = SyncTmuxExecutor(
            tmux_binary,
            capability.tmux_server_socket_path,
            runner=runner,
        )

    @staticmethod
    def validate_primary_pane_target(primary_pane_target: str) -> None:
        if _PANE_ID_PATTERN.fullmatch(primary_pane_target) is None:
            raise ValueError("primary pane target must be an exact tmux pane ID")

    def locate(self) -> str | None:
        candidate = self._observer_pane_target
        if candidate is None:
            shown = self._tmux_executor.run(
                (
                    "show-options",
                    "-p",
                    "-v",
                    "-t",
                    self._primary_pane_target,
                    OBSERVER_PRIMARY_PANE_OPTION,
                )
            )
            if shown.returncode != 0:
                return None
            candidate = shown.stdout.strip()
        if _PANE_ID_PATTERN.fullmatch(candidate) is None:
            return None
        identity = self._tmux_executor.run(
            capability_pane_read_arguments(
                self._capability,
                candidate,
                f"#{{pane_id}}|#{{{OBSERVER_OWNER_PANE_OPTION}}}|#{{pane_dead}}",
            )
        )
        if (
            identity.returncode != 0
            or identity.stdout.strip() != f"{candidate}|{self._primary_pane_target}|0"
        ):
            self._observer_pane_target = None
            return None
        self._observer_pane_target = candidate
        return candidate

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
        cwd = self._tmux_executor.run(
            primary_pane_capability_read_arguments(
                self._capability,
                "#{pane_id}|#{pane_current_path}",
            )
        )
        cwd_fields = cwd.stdout.rstrip("\n").split("|", maxsplit=1)
        if (
            cwd.returncode != 0
            or len(cwd_fields) != 2
            or cwd_fields[0] != self._primary_pane_target
            or not cwd_fields[1]
        ):
            return None
        command = [
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
            json.dumps(
                initial_event,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        ]
        split = self._mutate(
            (
                "split-window",
                "-v",
                "-b",
                "-d",
                "-p",
                "33",
                "-t",
                self._primary_pane_target,
                "-c",
                cwd_fields[1],
                "-P",
                "-F",
                "#{pane_id}",
                f"exec {shlex.join(command)}",
            )
        )
        pane_target = split.stdout.strip()
        if split.returncode != 0 or _PANE_ID_PATTERN.fullmatch(pane_target) is None:
            return None
        registration_steps = (
            (
                "set-option",
                "-p",
                "-t",
                self._primary_pane_target,
                OBSERVER_PRIMARY_PANE_OPTION,
                pane_target,
            ),
            (
                "set-option",
                "-p",
                "-t",
                pane_target,
                OBSERVER_OWNER_PANE_OPTION,
                self._primary_pane_target,
            ),
            ("select-pane", "-d", "-t", pane_target),
            ("select-pane", "-t", self._primary_pane_target),
        )
        for step in registration_steps:
            if self._mutate(step).returncode != 0:
                self._discard_failed_candidate(pane_target)
                return None
        self._observer_pane_target = pane_target
        return pane_target

    def _discard_failed_candidate(self, pane_target: str) -> None:
        """Best-effort rollback of one exact pane that never became an observer."""
        self._mutate(
            (
                "if-shell",
                "-t",
                self._primary_pane_target,
                "-F",
                f"#{{==:#{{{OBSERVER_PRIMARY_PANE_OPTION}}},{pane_target}}}",
                shlex.join(
                    (
                        "set-option",
                        "-pu",
                        "-t",
                        self._primary_pane_target,
                        OBSERVER_PRIMARY_PANE_OPTION,
                    )
                ),
            )
        )
        candidate_condition = combine_tmux_if_shell_conditions(
            capability_identity_if_shell_condition(self._capability),
            f"#{{==:#{{pane_id}},{pane_target}}}",
        )
        self._tmux_executor.run(
            (
                "if-shell",
                "-t",
                pane_target,
                "-F",
                candidate_condition,
                shlex.join(("kill-pane", "-t", pane_target)),
                shlex.join(("run-shell", "false")),
            )
        )

    def _mutate(self, arguments: tuple[str, ...]) -> TmuxCommandResult:
        return self._tmux_executor.run(
            (
                "if-shell",
                "-t",
                self._primary_pane_target,
                "-F",
                primary_pane_capability_if_shell_condition(self._capability),
                shlex.join(arguments),
                shlex.join(("run-shell", "false")),
            )
        )
