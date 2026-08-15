"""The single boundary between session intent and the tmux process."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SESSION_PREFIX = "codex-"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")
Runner = Callable[..., subprocess.CompletedProcess[str]]


class SessionError(RuntimeError):
    """A session operation could not be completed."""


@dataclass(frozen=True)
class Session:
    """Observable state for one managed tmux session."""

    name: str
    attached: bool
    windows: int


def tmux_session_name(name: str) -> str:
    """Return the namespaced tmux name for a user-facing session name."""
    if not _SAFE_NAME.fullmatch(name):
        raise SessionError(
            "session name must be 1-50 letters, digits, underscores, or hyphens "
            "and must start with a letter or digit"
        )
    return f"{SESSION_PREFIX}{name}"


def _exact_tmux_target(name: str) -> str:
    return f"={name}"


class TmuxSessions:
    """Create and control Codex processes hosted by tmux."""

    def __init__(self, runner: Runner = subprocess.run) -> None:
        self._run = runner

    def list(self) -> list[Session]:
        result = self._tmux(
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_attached}\t#{session_windows}",
            check=False,
        )
        if result.returncode != 0:
            return []

        sessions: list[Session] = []
        for line in result.stdout.splitlines():
            tmux_name, attached, windows = line.split("\t")
            if tmux_name.startswith(SESSION_PREFIX):
                sessions.append(
                    Session(
                        name=tmux_name.removeprefix(SESSION_PREFIX),
                        attached=attached != "0",
                        windows=int(windows),
                    )
                )
        return sorted(sessions, key=lambda session: session.name)

    def start(self, name: str, cwd: Path, prompt: str | None = None) -> None:
        target = tmux_session_name(name)
        workspace = cwd.expanduser().resolve()
        if not workspace.is_dir():
            raise SessionError(
                f"workspace does not exist or is not a directory: {workspace}"
            )
        if self.exists(name):
            raise SessionError(f"session already exists: {name}")

        self._tmux("new-session", "-d", "-s", target, "-c", str(workspace))
        exact_target = _exact_tmux_target(target)
        command = ["codex"]
        if prompt:
            command.append(prompt)
        try:
            self._tmux("send-keys", "-t", exact_target, "-l", shlex.join(command))
            self._tmux("send-keys", "-t", exact_target, "Enter")
        except SessionError:
            self._tmux("kill-session", "-t", exact_target, check=False)
            raise

    def attach(self, name: str) -> None:
        target = tmux_session_name(name)
        exact_target = _exact_tmux_target(target)
        if not self.exists(name):
            raise SessionError(f"session does not exist: {name}")
        if os.environ.get("TMUX"):
            self._tmux("switch-client", "-t", exact_target, interactive=True)
        else:
            self._tmux("attach-session", "-t", exact_target, interactive=True)

    def stop(self, name: str) -> None:
        target = tmux_session_name(name)
        if not self.exists(name):
            raise SessionError(f"session does not exist: {name}")
        self._tmux("kill-session", "-t", _exact_tmux_target(target))

    def exists(self, name: str) -> bool:
        target = tmux_session_name(name)
        return (
            self._tmux(
                "has-session", "-t", _exact_tmux_target(target), check=False
            ).returncode
            == 0
        )

    def _tmux(
        self, *arguments: str, check: bool = True, interactive: bool = False
    ) -> subprocess.CompletedProcess[str]:
        try:
            options: dict[str, object] = {"check": check, "text": True}
            if not interactive:
                options["capture_output"] = True
            return self._run(["tmux", *arguments], **options)
        except FileNotFoundError as error:
            raise SessionError("tmux is not installed or is not on PATH") from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.strip() or error.stdout.strip() or "tmux command failed"
            raise SessionError(detail) from error
