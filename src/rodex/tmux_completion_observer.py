"""Show Rodex-owned slash completion without modifying Codex terminal output."""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from .tmux_input_proxy import (
    extract_raw_prompt_text,
    native_popup_confirms_no_match,
)
from .tmux_status import (
    STATUS_PUBLISHER_COMPLETION,
    StatusPriority,
    TmuxStatusPipeline,
    TmuxStatusPresentation,
    completion_status_left_format,
)

_COMPLETION_MESSAGES: Final = {
    "/": "Rodex completion: /rodex  manage this Rodex session",
    "/r": "Rodex completion: /rodex  [type o to narrow]",
    "/ro": "Rodex completion: /rodex  [Tab to complete]",
    "/rod": "Rodex completion: /rodex  [Tab to complete]",
    "/rode": "Rodex completion: /rodex  [Tab to complete]",
    "/rodex": "Rodex command ready: /rodex  [Enter for help]",
}
_REDRAW_COALESCE_SECONDS: Final = 0.025
_RIBBON_DURATION_SECONDS: Final = 5.0
_NATIVE_COLLISION_PREFIXES: Final = frozenset({"/ro", "/rod", "/rode", "/rodex"})

Runner = Callable[..., subprocess.CompletedProcess[str]]
TokenFactory = Callable[[], str]


def completion_message(prompt_text: str | None) -> str | None:
    """Return the Rodex ribbon for one exact composer prefix."""
    return _COMPLETION_MESSAGES.get(prompt_text)


def output_may_affect_completion(output: bytes, *, completion_visible: bool) -> bool:
    """Avoid screen inspection until output can start or change a completion."""
    return completion_visible or b"/" in output


class TmuxCompletionObserver:
    """Inspect redraws and maintain a transient pane-scoped completion ribbon."""

    def __init__(
        self,
        tmux_binary: str,
        tmux_server_socket_path: Path,
        pane_id: str,
        *,
        runner: Runner = subprocess.run,
        token_factory: TokenFactory = lambda: secrets.token_hex(8),
    ) -> None:
        self._tmux_prefix = [tmux_binary, "-S", str(tmux_server_socket_path)]
        self._pane_id = pane_id
        self._run = runner
        self._token_factory = token_factory
        self._token: str | None = None
        self._status = TmuxStatusPipeline(self._tmux, pane_id)
        self.completion_visible = False

    def inspect_redraw(self) -> None:
        """Refresh the ribbon from the active cursor line, failing open."""
        cursor = self._tmux(
            "display-message", "-p", "-t", self._pane_id, "-F", "#{cursor_y}"
        )
        cursor_y = cursor.stdout.strip()
        if cursor.returncode != 0 or not cursor_y.isdigit():
            self.clear()
            return
        captured = self._tmux(
            "capture-pane",
            "-p",
            "-t",
            self._pane_id,
            "-S",
            cursor_y,
        )
        if captured.returncode != 0:
            self.clear()
            return

        prompt_text = extract_raw_prompt_text(captured.stdout)
        message = completion_message(prompt_text)
        if prompt_text in _NATIVE_COLLISION_PREFIXES and (
            not native_popup_confirms_no_match(captured.stdout, prompt_text)
        ):
            message = None
        if message is None:
            self.clear()
            return
        token = self._token_factory()
        if not self._status.publish_transient(
            publisher=STATUS_PUBLISHER_COMPLETION,
            token=token,
            priority=StatusPriority.COMPLETION,
            presentation=TmuxStatusPresentation(
                status_left=completion_status_left_format(message)
            ),
        ):
            self.completion_visible = False
            self._token = None
            return
        self._token = token
        self.completion_visible = True

    def clear(self) -> None:
        """Restore the normal status only while this observer's claim remains current."""
        if not self.completion_visible:
            return
        token = self._token
        self.completion_visible = False
        self._token = None
        if token:
            self._status.restore_if_token_matches(token)

    def _tmux(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._run(
            [*self._tmux_prefix, *arguments],
            check=False,
            text=True,
            capture_output=True,
        )


class _AsyncPaneOutputWakeup:
    """Coalesce pane writes without polling or delaying tmux input."""

    def __init__(
        self,
        observer: TmuxCompletionObserver,
        loop: asyncio.AbstractEventLoop,
        input_fd: int,
    ) -> None:
        self._observer = observer
        self._loop = loop
        self._input_fd = input_fd
        self._pending: asyncio.TimerHandle | None = None
        self._expiry: asyncio.TimerHandle | None = None

    def start(self) -> None:
        os.set_blocking(self._input_fd, False)
        self._loop.add_reader(self._input_fd, self._read_ready)

    def _read_ready(self) -> None:
        try:
            output = os.read(self._input_fd, 65536)
        except BlockingIOError:
            return
        if not output:
            self._loop.remove_reader(self._input_fd)
            if self._pending is not None:
                self._pending.cancel()
            if self._expiry is not None:
                self._expiry.cancel()
            self._observer.clear()
            self._loop.stop()
            return
        if not output_may_affect_completion(
            output,
            completion_visible=self._observer.completion_visible,
        ):
            return
        if self._pending is not None:
            self._pending.cancel()
        self._pending = self._loop.call_later(
            _REDRAW_COALESCE_SECONDS,
            self._inspect_redraw,
        )

    def _inspect_redraw(self) -> None:
        self._pending = None
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        self._observer.inspect_redraw()
        if self._observer.completion_visible:
            self._expiry = self._loop.call_later(
                _RIBBON_DURATION_SECONDS,
                self._expire_completion,
            )

    def _expire_completion(self) -> None:
        self._expiry = None
        self._observer.clear()


def observe_pane_output(observer: TmuxCompletionObserver, input_fd: int) -> None:
    """Run the pane-output observer until tmux closes its pipe."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        _AsyncPaneOutputWakeup(observer, loop, input_fd).start()
        loop.run_forever()
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m rodex.tmux_completion_observer")
    parser.add_argument("--tmux-binary", required=True)
    parser.add_argument("--tmux-server-socket", required=True, type=Path)
    parser.add_argument("--pane-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    observer = TmuxCompletionObserver(
        args.tmux_binary,
        args.tmux_server_socket,
        args.pane_id,
    )
    observe_pane_output(observer, sys.stdin.buffer.fileno())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
