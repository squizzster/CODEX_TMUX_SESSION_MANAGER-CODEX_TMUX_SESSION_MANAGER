"""Intercept Rodex-local slash commands before the Codex TUI consumes keys."""

from __future__ import annotations

import argparse
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .tmux_status import COMPLETION_TOKEN_OPTION, RODEX_STATUS_LEFT_FORMAT

_RODEX_COMMAND: Final = "/rodex"
_PROMPT_PREFIX: Final = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} "
_MESSAGE_DURATION_MILLISECONDS: Final = "5000"
_COMPLETABLE_PREFIXES: Final = frozenset({"/ro", "/rod", "/rode"})

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class RodexInputCommand:
    """One Rodex-local command extracted from the visible TUI prompt."""

    arguments: tuple[str, ...]
    parse_error: bool = False


def extract_rodex_input_command(screen_text: str) -> RodexInputCommand | None:
    """Return the last visible `/rodex` prompt, excluding similar prefixes."""
    prompt_text = extract_prompt_text(screen_text)
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


def extract_prompt_text(screen_text: str) -> str | None:
    """Return the last visible Codex composer line without its prompt marker."""
    prompt_text = extract_raw_prompt_text(screen_text)
    return prompt_text.strip() if prompt_text is not None else None


def extract_raw_prompt_text(screen_text: str) -> str | None:
    """Return composer text while preserving user-entered boundary whitespace."""
    for line in reversed(screen_text.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith(_PROMPT_PREFIX):
            return stripped.removeprefix(_PROMPT_PREFIX)
    return None


def has_native_slash_completion(screen_text: str, prompt_text: str) -> bool:
    """Return whether Codex currently renders a command matching the prefix."""
    for line in screen_text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("/"):
            continue
        command = stripped.split(maxsplit=1)[0]
        if command != prompt_text and command.startswith(prompt_text):
            return True
    return False


def has_native_no_matches_marker(screen_text: str) -> bool:
    """Return whether Codex positively rendered its empty completion result."""
    return any(line.strip() == "no matches" for line in screen_text.splitlines())


def native_popup_confirms_no_match(screen_text: str, prompt_text: str) -> bool:
    """Require positive empty-popup evidence before Rodex supplements Codex."""
    return has_native_no_matches_marker(screen_text) and not has_native_slash_completion(
        screen_text, prompt_text
    )


def proxy_input_key(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    pane_id: str,
    session_name: str,
    client_name: str,
    key: str,
    *,
    runner: Runner = subprocess.run,
) -> int:
    """Handle one Rodex-aware key or forward it to the Codex TUI unchanged."""
    if key not in {"Enter", "Tab"}:
        raise ValueError(f"unsupported Rodex input key: {key}")
    tmux_prefix = [tmux_binary, "-S", str(tmux_server_socket_path)]

    def tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
        return runner(
            [*tmux_prefix, *arguments],
            check=False,
            text=True,
            capture_output=True,
        )

    def forward_key() -> int:
        return tmux("send-keys", "-t", pane_id, key).returncode

    cursor = tmux(
        "display-message",
        "-p",
        "-t",
        pane_id,
        "-F",
        "#{cursor_y}:#{cursor_x}",
    )
    cursor_position = cursor.stdout.strip().split(":", maxsplit=1)
    if (
        cursor.returncode != 0
        or len(cursor_position) != 2
        or not all(value.isdigit() for value in cursor_position)
    ):
        return forward_key()
    cursor_y, cursor_x_text = cursor_position
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
        return forward_key()
    if key == "Tab":
        prompt_text = extract_raw_prompt_text(captured.stdout)
        if prompt_text not in _COMPLETABLE_PREFIXES:
            return forward_key()
        expected_cursor_x = len(_PROMPT_PREFIX) + len(prompt_text)
        if int(cursor_x_text) != expected_cursor_x:
            return forward_key()
        completion_popup = tmux(
            "capture-pane",
            "-p",
            "-t",
            pane_id,
            "-S",
            cursor_y,
        )
        if completion_popup.returncode != 0 or not native_popup_confirms_no_match(
            completion_popup.stdout, prompt_text
        ):
            return forward_key()
        suffix = _RODEX_COMMAND.removeprefix(prompt_text)
        return tmux("send-keys", "-l", "-t", pane_id, suffix).returncode

    command = extract_rodex_input_command(captured.stdout)
    if command is None:
        return forward_key()

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


def proxy_enter_key(
    tmux_binary: str,
    tmux_server_socket_path: Path,
    pane_id: str,
    session_name: str,
    client_name: str,
    *,
    runner: Runner = subprocess.run,
) -> int:
    """Dispatch or forward Enter through the shared key-aware pipeline."""
    return proxy_input_key(
        tmux_binary,
        tmux_server_socket_path,
        pane_id,
        session_name,
        client_name,
        "Enter",
        runner=runner,
    )


def _dispatch_rodex_input_command(
    command: RodexInputCommand,
    pane_id: str,
    session_name: str,
    client_name: str,
    tmux: Callable[..., subprocess.CompletedProcess[str]],
) -> int:
    tmux("set-option", "-u", "-t", pane_id, COMPLETION_TOKEN_OPTION)
    tmux("set-option", "-t", pane_id, "status-left", RODEX_STATUS_LEFT_FORMAT)
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
    parser.add_argument("--key", required=True, choices=("Enter", "Tab"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return proxy_input_key(
        args.tmux_binary,
        args.tmux_server_socket,
        args.pane_id,
        args.session_name,
        args.client_name,
        args.key,
    )


if __name__ == "__main__":
    raise SystemExit(main())
