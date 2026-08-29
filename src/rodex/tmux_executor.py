"""One bounded process boundary for Rodex tmux commands."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS: Final = 1.0
TMUX_TIMEOUT_RETURN_CODE: Final = 124
TMUX_UNAVAILABLE_RETURN_CODE: Final = 127


@dataclass(frozen=True, slots=True)
class TmuxCommandResult:
    """Normalized result from either the synchronous or asynchronous boundary."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    unavailable: bool = False


SyncTmuxRunner = Callable[..., subprocess.CompletedProcess[Any]]
AsyncTmuxRunner = Callable[[Sequence[str]], Awaitable[TmuxCommandResult]]
TmuxExecutionMode = Literal["captured", "interactive"]
TmuxOutputPolicy = Literal["capture", "discard"]


class SyncTmuxExecutor:
    """Run exact tmux commands with one absolute subprocess deadline."""

    def __init__(
        self,
        tmux_binary: str,
        tmux_server_socket_path: Path,
        *,
        runner: SyncTmuxRunner = subprocess.run,
        timeout_seconds: float = DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("tmux command timeout must be positive")
        self._prefix = (tmux_binary, "-S", str(tmux_server_socket_path))
        self._runner = runner
        self._timeout_seconds = timeout_seconds

    def run(
        self,
        arguments: Sequence[str],
        *,
        mode: TmuxExecutionMode = "captured",
        output: TmuxOutputPolicy = "capture",
        environment: dict[str, str] | None = None,
    ) -> TmuxCommandResult:
        """Run one normalized command with explicit terminal and output semantics."""
        if mode == "captured":
            if environment is not None:
                raise ValueError("captured tmux execution does not accept an environment")
            options: dict[str, object] = {"check": False, "text": True}
            if output == "capture":
                options["capture_output"] = True
            elif output == "discard":
                options.update(
                    {
                        "stdin": subprocess.DEVNULL,
                        "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL,
                    }
                )
            else:
                raise ValueError(f"unsupported tmux output policy: {output}")
            options["timeout"] = self._timeout_seconds
        elif mode == "interactive":
            if output != "capture":
                raise ValueError("interactive tmux execution owns terminal output")
            options = {
                "check": False,
                "text": True,
                "env": os.environ.copy() if environment is None else environment,
            }
        else:
            raise ValueError(f"unsupported tmux execution mode: {mode}")
        command = [*self._prefix, *arguments]
        try:
            result = self._runner(command, **options)
        except subprocess.TimeoutExpired as error:
            return TmuxCommandResult(
                TMUX_TIMEOUT_RETURN_CODE,
                _text_output(error.stdout),
                _text_output(error.stderr),
                timed_out=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return TmuxCommandResult(
                TMUX_UNAVAILABLE_RETURN_CODE,
                stderr=str(error),
                unavailable=True,
            )
        return TmuxCommandResult(
            int(result.returncode),
            _text_output(result.stdout),
            _text_output(result.stderr),
        )


class AsyncTmuxExecutor:
    """Run exact tmux commands with cancellation-safe timeout cleanup."""

    def __init__(
        self,
        tmux_binary: str,
        tmux_server_socket_path: Path,
        *,
        runner: AsyncTmuxRunner | None = None,
        timeout_seconds: float = DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("tmux command timeout must be positive")
        self._prefix = (tmux_binary, "-S", str(tmux_server_socket_path))
        self._runner = runner or _run_async_command
        self._timeout_seconds = timeout_seconds

    async def run(self, arguments: Sequence[str]) -> TmuxCommandResult:
        """Return a normalized timeout while ensuring the child is reaped."""
        try:
            return await asyncio.wait_for(
                self._runner((*self._prefix, *arguments)),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return TmuxCommandResult(TMUX_TIMEOUT_RETURN_CODE, timed_out=True)
        except (OSError, subprocess.SubprocessError) as error:
            return TmuxCommandResult(
                TMUX_UNAVAILABLE_RETURN_CODE,
                stderr=str(error),
                unavailable=True,
            )


async def _run_async_command(command: Sequence[str]) -> TmuxCommandResult:
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        return TmuxCommandResult(
            TMUX_UNAVAILABLE_RETURN_CODE,
            stderr=str(error),
            unavailable=True,
        )
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return TmuxCommandResult(
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _text_output(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""
