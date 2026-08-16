"""Intercept Rodex-local slash commands before the Codex TUI consumes Enter."""

from __future__ import annotations

import argparse
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_RODEX_COMMAND: Final = "/rodex"
_PROMPT_PREFIX: Final = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} "
_MESSAGE_DURATION_MILLISECONDS: Final = "5000"

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class RodexInputCommand:
    """One Rodex-local command extracted from the visible TUI prompt."""

    arguments: tuple[str, ...]
    parse_error: bool = False


def extract_rodex_input_command(screen_text: str) -> RodexInputCommand | None:
    """Return the last visible `/rodex` prompt, excluding similar prefixes."""
    prompt_text: str | None = None
    for line in reversed(screen_text.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith(_PROMPT_PREFIX):
            prompt_text = stripped.removeprefix(_PROMPT_PREFIX).strip()
            break
    if prompt_text is None:
        return None
    if prompt_text == _RODEX_COMMAND:
        return RodexInputCommand(())
    if not prompt_text.startswith(f"{_RODEX_COMMAND} "):
        return None
    try:
        arguments = tuple(shlex.split(prompt_text[len(_RODEX_COMMAND) :].strip()))
    except ValueError:
        return RodexInputCommand((), parse_error=True)
    return RodexInputCommand(arguments)


def proxy_enter_key(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    pane_id: str,
    session_name: str,
    client_name: str,
    *,
    runner: Runner = subprocess.run,
) -> int:
    """Dispatch `/rodex` locally or forward Enter to the Codex TUI unchanged."""
    tmux_prefix = [tmux_binary, "-S", str(tmux_server_socket_path)]

    def tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
        return runner(
            [*tmux_prefix, *arguments],
            check=False,
            text=True,
            capture_output=True,
        )

    def forward_enter() -> int:
        return tmux("send-keys", "-t", pane_id, "Enter").returncode

    cursor = tmux("display-message", "-p", "-t", pane_id, "-F", "#{cursor_y}")
    cursor_y = cursor.stdout.strip()
    if cursor.returncode != 0 or not cursor_y.isdigit():
        return forward_enter()
    captured = tmux(
        "capture-pane",
        "-p",
        "-t",
        pane_id,
        "-S",
        cursor_y,
        "-E",
        cursor_y,
    )
    if captured.returncode != 0:
        return forward_enter()
    command = extract_rodex_input_command(captured.stdout)
    if command is None:
        return forward_enter()

    cleared = tmux("send-keys", "-t", pane_id, "C-c")
    if cleared.returncode != 0:
        return cleared.returncode
    return _dispatch_rodex_input_command(
        command,
        pane_id,
        session_name,
        client_name,
        tmux,
    )


def _dispatch_rodex_input_command(
    command: RodexInputCommand,
    pane_id: str,
    session_name: str,
    client_name: str,
    tmux: Callable[..., subprocess.CompletedProcess[str]],
) -> int:
    arguments = command.arguments
    if command.parse_error:
        message = "Rodex: command contains an unmatched quote"
    elif not arguments or arguments == ("help",):
        message = "Rodex: /rodex hi | /rodex identity | /rodex detach"
    elif arguments == ("hi",):
        message = f"Rodex: hello from {session_name}"
    elif arguments == ("identity",):
        codex_identity = tmux(
            "show-options",
            "-v",
            "-t",
            pane_id,
            "@rodex_codex_session_uuid",
        )
        codex_uuid = codex_identity.stdout.strip()
        message = (
            f"Rodex: {session_name} -> Codex {codex_uuid}"
            if codex_identity.returncode == 0 and codex_uuid
            else f"Rodex: {session_name}"
        )
    elif arguments == ("detach",):
        if not client_name:
            message = "Rodex: no attached tmux client to detach"
        else:
            return tmux("detach-client", "-t", client_name).returncode
    else:
        message = f"Rodex: unknown command {arguments[0]!r}; try /rodex help"
    return tmux(
        "display-message",
        "-d",
        _MESSAGE_DURATION_MILLISECONDS,
        "-t",
        pane_id,
        message,
    ).returncode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.tmux_input_proxy")
    parser.add_argument("--tmux-binary", required=True)
    parser.add_argument("--tmux-server-socket", required=True, type=Path)
    parser.add_argument("--pane-id", required=True)
    parser.add_argument("--session-name", required=True)
    parser.add_argument("--client-name", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return proxy_enter_key(
        args.tmux_binary,
        args.tmux_server_socket,
        args.pane_id,
        args.session_name,
        args.client_name,
    )


if __name__ == "__main__":
    raise SystemExit(main())
