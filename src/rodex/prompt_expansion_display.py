"""Bounded, transient display of forwarded primary input; never another submission."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock

from .protocol_input_text import is_user_input_submission, user_input_text_items

PROMPT_EXPANSION_MAX_CHARACTERS = 8192
PROMPT_EXPANSION_VISIBLE_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class PromptExpansion:
    """Bounded before/after text from one successfully forwarded submission."""

    original_text: str
    forwarded_text: str


class PromptExpansionDisplay:
    """Latest-only handoff. Producers never render, write terminal data, or await it."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._source: object | None = None
        self._expansion: PromptExpansion | None = None
        self._expires_at: float | None = None
        self._wake: Callable[[], None] | None = None

    def bind_source(self, source: object) -> None:
        with self._lock:
            self._source = source
            self._expansion = self._expires_at = None
            wake = self._wake
        self._notify(wake)

    def release_source(self, source: object) -> None:
        with self._lock:
            if self._source is not source:
                return
            self._source = self._expansion = self._expires_at = None
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
        before_texts = tuple(item["text"] for item in before)
        after_texts = tuple(item["text"] for item in after)
        if before_texts == after_texts:
            # Native startup can immediately submit an unchanged helper-thread
            # prompt. It must not erase the visible user expansion before attach.
            return
        original_text = _bounded_text(before_texts, 512)
        expansion = PromptExpansion(
            original_text,
            _bounded_text(after_texts, PROMPT_EXPANSION_MAX_CHARACTERS - len(original_text)),
        )
        with self._lock:
            if self._source is not source:
                return
            self._expansion = expansion
            self._expires_at = time.monotonic() + PROMPT_EXPANSION_VISIBLE_SECONDS
            wake = self._wake
        self._notify(wake)

    def snapshot(self, now: float) -> tuple[PromptExpansion | None, float | None]:
        with self._lock:
            if self._expires_at is not None and now >= self._expires_at:
                self._expansion = self._expires_at = None
            return self._expansion, self._expires_at

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


def _bounded_text(texts: tuple[str, ...], limit: int) -> str:
    """Slice before joining; even multipart input stays within the display budget."""
    parts = []
    remaining = limit
    for index, text in enumerate(texts):
        remaining -= bool(index)
        if remaining < 0:
            return "\n".join(parts)[: limit - 1] + "…"
        parts.append(text[:remaining])
        if len(text) > remaining:
            return "\n".join(parts)[: limit - 1] + "…"
        remaining -= len(text)
    return "\n".join(parts)
