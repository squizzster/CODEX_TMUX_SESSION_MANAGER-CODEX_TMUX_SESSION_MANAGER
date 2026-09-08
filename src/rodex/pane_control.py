"""Exact-target tmux adapter for primary and registered presentation panes."""

from __future__ import annotations

import os
import re
import shlex
import sys
from typing import Final

from .process_environment import (
    exact_environment_exec_command,
    validated_user_environment_entries,
)
from .tmux_executor import SyncTmuxExecutor, SyncTmuxRunner, TmuxCommandResult
from .tmux_session_capability import (
    TmuxRuntimeCapability,
    capability_identity_if_shell_condition,
    capability_pane_read_arguments,
    combine_tmux_if_shell_conditions,
    primary_pane_capability_if_shell_condition,
    primary_pane_capability_read_arguments,
)

_PANE_ID_PATTERN: Final = re.compile(r"%[0-9]+")


class TmuxPaneController:
    """Locate, open, focus, resize or close one registered pane through bounded tmux calls."""

    def __init__(
        self,
        tmux_binary: str,
        capability: TmuxRuntimeCapability,
        primary_pane_target: str,
        *,
        runner: SyncTmuxRunner,
        python_executable: str = sys.executable,
        primary: bool = False,
        primary_pane_option: str | None = None,
        owner_pane_option: str | None = None,
    ) -> None:
        self.validate_primary_pane_target(primary_pane_target)
        if not primary and (primary_pane_option is None or owner_pane_option is None):
            raise ValueError("a presentation pane requires explicit registration options")
        self._capability = capability
        self._primary_pane_target = primary_pane_target
        self._python_executable = python_executable
        self._primary = primary
        self._primary_pane_option = primary_pane_option
        self._owner_pane_option = owner_pane_option
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

    @property
    def known_pane_id(self) -> str | None:
        """Identity from the most recent fenced lookup; never resolve a new target here."""
        return self._primary_pane_target if self._primary else self._observer_pane_target

    def locate(self) -> str | None:
        if self._primary:
            identity = self._tmux_executor.run(
                primary_pane_capability_read_arguments(self._capability, "#{pane_id}|#{pane_dead}")
            )
            valid = identity.returncode == 0 and identity.stdout.strip() == f"{self._primary_pane_target}|0"
            return self._primary_pane_target if valid else None
        candidate = self._observer_pane_target
        if candidate is None:
            shown = self._tmux_executor.run(
                (
                    "show-options",
                    "-p",
                    "-v",
                    "-t",
                    self._primary_pane_target,
                    self._primary_pane_option,
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
                f"#{{pane_id}}|#{{{self._owner_pane_option}}}|#{{pane_dead}}",
            )
        )
        if identity.returncode != 0 or identity.stdout.strip() != f"{candidate}|{self._primary_pane_target}|0":
            self._observer_pane_target = None
            return None
        self._observer_pane_target = candidate
        return candidate

    def create(self, pane_command: tuple[str, ...]) -> str | None:
        if self._primary:
            return self.locate()
        cwd = self._tmux_executor.run(
            primary_pane_capability_read_arguments(
                self._capability,
                "#{pane_id}|#{pane_current_path}",
            )
        )
        cwd_fields = cwd.stdout.rstrip("\n").split("|", maxsplit=1)
        if cwd.returncode != 0 or len(cwd_fields) != 2 or cwd_fields[0] != self._primary_pane_target or not cwd_fields[1]:
            return None
        try:
            environment_names = tuple(name for name, _value in validated_user_environment_entries(os.environ))
            command = exact_environment_exec_command(
                self._python_executable,
                environment_names,
                pane_command,
            )
        except ValueError:
            return None
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
                "-e",
                f"PWD={cwd_fields[1]}",
                *command,
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
                self._primary_pane_option,
                pane_target,
            ),
            (
                "set-option",
                "-p",
                "-t",
                pane_target,
                self._owner_pane_option,
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

    def focus(self, pane: str) -> bool:
        return self._mutate_target(pane, ("select-pane", "-t", pane)).returncode == 0

    def resize(self, pane: str, size_percent: int) -> bool:
        if type(size_percent) is not int or not 1 <= size_percent <= 99:
            raise ValueError("pane size must be an integer percentage from 1 to 99")
        return self._mutate_target(pane, ("resize-pane", "-t", pane, "-y", f"{size_percent}%")).returncode == 0

    def close(self, pane: str) -> bool:
        # Primary-pane closure is session termination, owned by session lifecycle.
        if self._primary:
            return False
        if self._mutate_target(pane, ("kill-pane", "-t", pane)).returncode != 0:
            return False
        if self._observer_pane_target == pane:
            self._observer_pane_target = None
        return True

    def _mutate_target(self, pane: str, arguments: tuple[str, ...]) -> TmuxCommandResult:
        self.validate_primary_pane_target(pane)
        conditions = [
            capability_identity_if_shell_condition(self._capability),
            f"#{{==:#{{pane_id}},{pane}}}",
            "#{==:#{pane_dead},0}",
        ]
        if not self._primary:
            conditions.append(f"#{{==:#{{{self._owner_pane_option}}},{self._primary_pane_target}}}")
        return self._tmux_executor.run(
            (
                "if-shell",
                "-t",
                pane,
                "-F",
                combine_tmux_if_shell_conditions(*conditions),
                shlex.join(arguments),
                shlex.join(("run-shell", "false")),
            )
        )

    def _discard_failed_candidate(self, pane_target: str) -> None:
        """Best-effort rollback of one exact pane that never became an observer."""
        self._mutate(
            (
                "if-shell",
                "-t",
                self._primary_pane_target,
                "-F",
                f"#{{==:#{{{self._primary_pane_option}}},{pane_target}}}",
                shlex.join(
                    (
                        "set-option",
                        "-pu",
                        "-t",
                        self._primary_pane_target,
                        self._primary_pane_option,
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
