"""Bounded, transient display of forwarded primary input; never another submission."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import suppress
from threading import Lock

from .protocol_input_text import is_user_input_submission, user_input_text_items

PROMPT_EXPANSION_MAX_CHARACTERS = 8192
PROMPT_EXPANSION_VISIBLE_SECONDS = 15.0


class PromptExpansionDisplay:
    """Latest-only handoff. Producers never render, write terminal data, or await it."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._source: object | None = None
        self._text: str | None = None
        self._expires_at: float | None = None
        self._wake: Callable[[], None] | None = None

    def bind_source(self, source: object) -> None:
        with self._lock:
            self._source = source
            self._text = self._expires_at = None
            wake = self._wake
        self._notify(wake)

    def release_source(self, source: object) -> None:
        with self._lock:
            if self._source is not source:
                return
            self._source = self._text = self._expires_at = None
            wake = self._wake
        self._notify(wake)

    def observe_forwarded(self, source: object, original: str | bytes | None, forwarded: str | bytes | None) -> None:
        """Compare actual wire text after send; do not invoke transformation again."""
        if original == forwarded or not isinstance(forwarded, (str, bytes)):
            return
        try:
            frame = json.loads(forwarded)
            if not is_user_input_submission(frame):
                return
            before = user_input_text_items(json.loads(original)) if isinstance(original, (str, bytes)) else ()
        except (ValueError, UnicodeDecodeError):
            return
        after = user_input_text_items(frame)
        changed = tuple(item["text"] for item in before) != tuple(item["text"] for item in after)
        if not changed:
            # Native startup can immediately submit an unchanged helper-thread
            # prompt. It must not erase the visible user expansion before attach.
            return
        # Slice before joining: neither multipart nor a huge replacement can
        # grow the retained mailbox beyond its explicit character budget.
        remaining = PROMPT_EXPANSION_MAX_CHARACTERS
        parts = []
        for item in after:
            part = item["text"]
            parts.append(part[:remaining])
            remaining -= len(part) + 1
            if remaining < 0:
                parts.append("…")
                break
        text = "\n".join(parts)[: PROMPT_EXPANSION_MAX_CHARACTERS + 2]
        with self._lock:
            if self._source is not source:
                return
            self._text = text
            self._expires_at = time.monotonic() + PROMPT_EXPANSION_VISIBLE_SECONDS
            wake = self._wake
        self._notify(wake)

    def snapshot(self, now: float) -> tuple[str | None, float | None]:
        with self._lock:
            if self._expires_at is not None and now >= self._expires_at:
                self._text = self._expires_at = None
            return self._text, self._expires_at

    def subscribe(self, wake: Callable[[], None]) -> Callable[[], None]:
        """Bind the current gateway's nonblocking wake; preserve startup publications."""
        with self._lock:
            self._wake = wake

        def unsubscribe() -> None:
            with self._lock:
                if self._wake is wake:
                    self._wake = None

        return unsubscribe

    @staticmethod
    def _notify(wake: Callable[[], None] | None) -> None:
        if wake is not None:
            with suppress(Exception):
                wake()
