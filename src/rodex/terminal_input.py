"""Decode terminal input and own configured pass-through/takeover/release transitions.

The candidate is observed keyboard input, not an authoritative copy of Codex's
editor. Admission verifies the already-forwarded prefix at the native boundary.
Escape sequences and bracketed paste are never interpreted as individual keys.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .input_interceptor_config import InputInterceptorRegistration
from .interaction_pipeline import InteractionOperation, InteractionRequest, SessionInteractionPipeline

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
MAX_LOCAL_DRAFT_BYTES = 65536
ESCAPE_WAIT_SECONDS = 0.035
INCOMPLETE_FRAME_WAIT_SECONDS = 0.25


@dataclass(frozen=True)
class TerminalInputEvent:
    kind: str
    raw: bytes
    text: str = ""

    @property
    def key(self) -> bytes:
        """Canonical control identity without changing the original native bytes."""
        return self.text.encode() if self.kind == "control" and self.text else self.raw


class TerminalInputDecoder:
    """Frame UTF-8, control keys, terminal replies and paste across arbitrary reads."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self._last_data_at: float | None = None
        self._streaming_paste = False
        self._streaming_reply = False

    def feed(self, data: bytes, *, now: float | None = None) -> list[TerminalInputEvent]:
        if data:
            self._last_data_at = time.monotonic() if now is None else now
        self._pending.extend(data)
        events: list[TerminalInputEvent] = []
        while self._pending:
            pending = bytes(self._pending)
            if self._streaming_reply or pending[:2] in {b"\x1b]", b"\x1bP", b"\x1b_", b"\x1b^"}:
                terminators = [
                    position + length
                    for marker, length in ((b"\x07", 1), (b"\x1b\\", 2))
                    if (position := pending.find(marker)) >= 0
                ]
                if not terminators:
                    if len(pending) > MAX_LOCAL_DRAFT_BYTES:
                        events.append(TerminalInputEvent("terminal_reply", pending[:-1]))
                        del self._pending[:-1]
                        self._streaming_reply = True
                    break
                amount = min(terminators)
                events.append(TerminalInputEvent("terminal_reply", pending[:amount]))
                del self._pending[:amount]
                self._streaming_reply = False
                continue
            if self._streaming_paste or pending.startswith(PASTE_START):
                end = pending.find(PASTE_END)
                if end < 0:
                    if len(pending) > MAX_LOCAL_DRAFT_BYTES:
                        # Oversized paste remains one opaque stream, never candidate
                        # keystrokes, and does not require unbounded memory.
                        amount = len(pending) - len(PASTE_END)
                        events.append(TerminalInputEvent("opaque_paste", pending[:amount]))
                        del self._pending[:amount]
                        self._streaming_paste = True
                    break
                amount = end + len(PASTE_END)
                raw = pending[:amount]
                kind = "opaque_paste" if self._streaming_paste else "paste"
                text = ""
                if not self._streaming_paste:
                    try:
                        text = raw[len(PASTE_START) : -len(PASTE_END)].decode("utf-8")
                    except UnicodeDecodeError:
                        kind = "opaque_paste"
                events.append(TerminalInputEvent(kind, raw, text))
                del self._pending[:amount]
                self._streaming_paste = False
                continue
            if pending[0] == 27:
                if len(pending) == 1:
                    break
                if PASTE_START.startswith(pending):
                    break
                amount = 2
                if pending[1] in (ord("["), ord("O")):
                    final = next((index for index in range(2, len(pending)) if 0x40 <= pending[index] <= 0x7E), None)
                    if final is None:
                        if len(pending) > MAX_LOCAL_DRAFT_BYTES:
                            events.append(TerminalInputEvent("opaque", pending))
                            self._pending.clear()
                        break
                    amount = final + 1
                events.append(_decode_escape_sequence(pending[:amount]))
                del self._pending[:amount]
                continue
            if pending[0] < 32 or pending[0] == 127:
                events.append(TerminalInputEvent("control", pending[:1]))
                del self._pending[:1]
                continue
            leading_byte = pending[0]
            amount = (
                2
                if 0xC2 <= leading_byte <= 0xDF
                else 3
                if 0xE0 <= leading_byte <= 0xEF
                else 4
                if 0xF0 <= leading_byte <= 0xF4
                else 1
            )
            if leading_byte < 128:
                amount = 1
            if len(pending) < amount:
                break
            raw = pending[:amount]
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                events.append(TerminalInputEvent("opaque", pending[:1]))
                del self._pending[:1]
            else:
                events.append(TerminalInputEvent("text", raw, text))
                del self._pending[:amount]
        return events

    def expire_incomplete(self, now: float) -> list[TerminalInputEvent]:
        if not self._pending or self._last_data_at is None:
            return []
        if self._streaming_paste or self._pending.startswith(PASTE_START):
            return []
        delay = ESCAPE_WAIT_SECONDS if self._pending == b"\x1b" else INCOMPLETE_FRAME_WAIT_SECONDS
        if now - self._last_data_at < delay:
            return []
        raw = bytes(self._pending)
        kind = "control" if raw == b"\x1b" else "opaque"
        if self._streaming_reply or raw[:2] in {b"\x1b]", b"\x1bP", b"\x1b_", b"\x1b^"}:
            kind = "terminal_reply"
            self._streaming_reply = True
            if raw.endswith(b"\x1b"):
                raw = raw[:-1]  # Preserve a possible split string terminator.
        del self._pending[: len(raw)]
        return [TerminalInputEvent(kind, raw)] if raw else []


class TerminalInputInterceptor:
    """One configured input owner; successful local handling never becomes rejection."""

    def __init__(
        self,
        registrations: tuple[InputInterceptorRegistration, ...],
        pipeline: SessionInteractionPipeline,
        forward: Callable[[bytes], None],
        confirm_native_prefix: Callable[[str], bool],
    ) -> None:
        if len({entry.name for entry in registrations}) != len(registrations):
            raise ValueError("interceptor names must be unique")
        self._registrations = registrations
        self._pipeline = pipeline
        self._forward = forward
        self._confirm_native_prefix = confirm_native_prefix
        self._candidate = ""
        self._active: InputInterceptorRegistration | None = None
        self._draft = ""
        self._forwarded_prefix = ""
        self._discard_paired_lf = False

    @property
    def active(self) -> bool:
        return self._active is not None

    def accept(self, event: TerminalInputEvent) -> None:
        if event.kind == "terminal_reply":
            self._forward(event.raw)
            return
        if self._discard_paired_lf:
            self._discard_paired_lf = False
            if event.key == b"\n":
                return
        if self._active is not None:
            self._accept_local(event)
            return
        if event.kind in {"text", "paste"}:
            candidate = self._candidate + event.text
            matches = [entry for entry in self._registrations if entry.matches(candidate)]
            # An ambiguous registry cannot own input. Leave native input intact.
            if len(matches) == 1 and self._confirm_native_prefix(self._candidate):
                self._active = matches[0]
                self._forwarded_prefix = self._candidate
                self._draft = candidate
                if self._publish(InteractionOperation.INTERACTIVE_INPUT):
                    return
                self.release()
            self._candidate = candidate if len(candidate) <= 128 else ""
        elif event.kind == "control" and event.key in {b"\x7f", b"\x08"}:
            self._candidate = self._candidate[:-1]
        else:
            self._candidate = ""
        self._forward(event.raw)

    def _accept_local(self, event: TerminalInputEvent) -> None:
        assert self._active is not None
        if event.kind in {"text", "paste"}:
            if len((self._draft + event.text).encode()) > MAX_LOCAL_DRAFT_BYTES:
                self._return_to_native(event)
                return
            self._draft += event.text
        elif event.kind == "control":
            if event.key in {b"\x1b", b"\x03"}:
                self.release()
                return
            if event.key in {b"\x7f", b"\x08"}:
                self._draft = self._draft[:-1]
            elif event.key == b"\t":
                if self._active.matches(self._draft):
                    self._draft = self._active.command
            elif event.key in {b"\r", b"\n"}:
                self._discard_paired_lf = event.key == b"\r"
                # Only remove the exact previously-forwarded prefix, never clear
                # an unknown draft or synthesize Enter into the native TUI.
                if self._confirm_native_prefix(self._forwarded_prefix) and self._publish(
                    InteractionOperation.SUBMITTED_COMMAND
                ):
                    self._forward(b"\x7f" * len(self._forwarded_prefix))
                    self.release()
                    self._candidate = ""
                return
            else:
                self._return_to_native(event)
                return
        else:
            self._return_to_native(event)
            return
        if self._draft == self._forwarded_prefix:
            self.release()
        else:
            if not self._publish(InteractionOperation.INTERACTIVE_INPUT):
                self._return_to_native(TerminalInputEvent("opaque", b""))

    def _return_to_native(self, event: TerminalInputEvent) -> None:
        """Unsupported native editing releases a lossless, non-submitting draft."""
        held_suffix = self._draft[len(self._forwarded_prefix) :].encode()
        self._forward(PASTE_START + held_suffix + PASTE_END)
        self.release()
        self._candidate = ""
        self._forward(event.raw)

    def _publish(self, operation: InteractionOperation) -> bool:
        assert self._active is not None
        result = self._pipeline.execute(
            InteractionRequest(self._active.target, operation, "terminal-interceptor", text=self._draft)
        )
        return result.accepted

    def release(self) -> None:
        if self._active is not None:
            self._publish(InteractionOperation.INPUT_RELEASE)
            self._candidate = self._forwarded_prefix
        self._active = None
        self._draft = ""
        self._forwarded_prefix = ""


def _decode_escape_sequence(raw: bytes) -> TerminalInputEvent:
    """Normalize CSI-u/modifyOtherKeys identities; pass unknown native encodings intact."""
    if raw in {b"\x1b[I", b"\x1b[O"} or (
        raw.startswith(b"\x1b[") and (raw[-1:] in {b"R", b"c", b"n", b"t"} or raw.endswith(b"$y"))
    ):
        return TerminalInputEvent("terminal_reply", raw)
    try:
        fields = raw[2:-1].split(b";")
        if raw.startswith(b"\x1b[") and raw.endswith(b"u") and 1 <= len(fields) <= 3:
            codepoints = list(map(int, fields[0].split(b":")))
            modifier_fields = list(map(int, fields[1].split(b":"))) if len(fields) >= 2 else [1]
            modifiers = modifier_fields[0] - 1
            if len(modifier_fields) > 1 and modifier_fields[1] == 3:
                return TerminalInputEvent("terminal_reply", raw)  # A key release, not another press.
            codepoint = codepoints[1] if modifiers & 1 and len(codepoints) > 1 else codepoints[0]
        elif raw.startswith(b"\x1b[27;") and raw.endswith(b"~") and len(fields) == 3:
            modifiers = int(fields[1]) - 1
            codepoint = int(fields[2])
        else:
            return TerminalInputEvent("escape_sequence", raw)
        if modifiers not in {0, 1, 4, 5} or not 0 <= codepoint < 0xE000 or 0xD800 <= codepoint <= 0xDFFF:
            return TerminalInputEvent("escape_sequence", raw)
        if modifiers and (codepoint < 32 or codepoint == 127):
            return TerminalInputEvent("escape_sequence", raw)  # Shift-Enter is not submission.
        text = chr(codepoint)
        if modifiers & 4 and ("a" <= text.lower() <= "z" or text in "@[\\]^_"):
            text = chr(ord(text.upper()) & 31)
        elif modifiers & 1 and text.isascii():
            text = text.upper()
        return TerminalInputEvent("control" if ord(text) < 32 or text == "\x7f" else "text", raw, text)
    except (ValueError, IndexError):
        return TerminalInputEvent("escape_sequence", raw)
