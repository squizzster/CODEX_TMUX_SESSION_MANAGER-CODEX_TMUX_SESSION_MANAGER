"""Tmux-only lifecycle for the dedicated agent observer pane."""

from __future__ import annotations

import json
import re
import shlex
import sys
import uuid
from pathlib import Path
from typing import Final

from .tmux_executor import SyncTmuxExecutor, SyncTmuxRunner

OBSERVER_PRIMARY_PANE_OPTION: Final = "@rodex_agent_observer_pane_id"
OBSERVER_OWNER_PANE_OPTION: Final = "@rodex_agent_observer_for"
_PANE_ID_PATTERN: Final = re.compile(r"%[0-9]+")


class ObserverPaneController:
    """Locate or create one validated observer pane through bounded tmux calls."""

    def __init__(
        self,
        tmux_binary: str,
        tmux_server_socket_path: Path,
        primary_pane_target: str,
        *,
        runner: SyncTmuxRunner,
        python_executable: str = sys.executable,
    ) -> None:
        self.validate_primary_pane_target(primary_pane_target)
        self._primary_pane_target = primary_pane_target
        self._python_executable = python_executable
        self._observer_pane_target: str | None = None
        self._tmux_executor = SyncTmuxExecutor(
            tmux_binary,
            tmux_server_socket_path,
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
            (
                "display-message",
                "-p",
                "-t",
                candidate,
                "-F",
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
            (
                "display-message",
                "-p",
                "-t",
                self._primary_pane_target,
                "-F",
                "#{pane_current_path}",
            )
        )
        if cwd.returncode != 0 or not cwd.stdout.rstrip("\n"):
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
        split = self._tmux_executor.run(
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
                cwd.stdout.rstrip("\n"),
                "-P",
                "-F",
                "#{pane_id}",
                f"exec {shlex.join(command)}",
            )
        )
        pane_target = split.stdout.strip()
        if split.returncode != 0 or _PANE_ID_PATTERN.fullmatch(pane_target) is None:
            return None
        self._tmux_executor.run(
            (
                "set-option",
                "-p",
                "-t",
                self._primary_pane_target,
                OBSERVER_PRIMARY_PANE_OPTION,
                pane_target,
            )
        )
        self._tmux_executor.run(
            (
                "set-option",
                "-p",
                "-t",
                pane_target,
                OBSERVER_OWNER_PANE_OPTION,
                self._primary_pane_target,
            )
        )
        self._tmux_executor.run(("select-pane", "-d", "-t", pane_target))
        self._tmux_executor.run(("select-pane", "-t", self._primary_pane_target))
        self._observer_pane_target = pane_target
        return pane_target
