"""Frame native output once; separate terminal replies/controls from presentation."""

from __future__ import annotations

from dataclasses import dataclass

import pyte
from pyte import modes

from .native_terminal_screen import NativeTerminalScreen


@dataclass(frozen=True, slots=True)
class NativeTerminalOutput:
    native: bytes
    controls: bytes
    replies: bytes


class NativeTerminalProjection:
    """Own native coordinates and terminal-query semantics independent of display policy."""

    def __init__(self, columns: int, rows: int) -> None:
        self.screen = NativeTerminalScreen(columns, rows)
        self._stream = pyte.ByteStream(self.screen)
        self._sequence = bytearray()
        self._frame = "ground"
        self._string_escape = False
        self._oversize_csi = False
        self.synchronized_update = False

    @property
    def at_boundary(self) -> bool:
        return self._frame == "ground" and not self._stream.utf8_decoder.getstate()[0] and not self.synchronized_update

    @property
    def paintable(self) -> bool:
        return (
            self.at_boundary
            and modes.DECOM not in self.screen.mode
            and modes.IRM not in self.screen.mode
            and 0 <= self.screen.cursor.x < self.screen.columns
            and 0 <= self.screen.cursor.y < self.screen.lines
        )

    def feed(self, data: bytes) -> NativeTerminalOutput:
        native, controls, replies, plain = bytearray(), bytearray(), bytearray(), bytearray()
        for byte in data:
            if self._frame == "ground":
                if byte == 27:
                    self._stream.feed(bytes(plain))
                    plain.clear()
                    self._sequence = bytearray([byte])
                    self._frame = "escape"
                else:
                    plain.append(byte)
                    native.append(byte)
                    if byte == 7:
                        controls.append(byte)
                continue
            if self._frame == "string":
                # Opaque terminal controls (including OSC/DCS queries) are never
                # screen text, and must survive semantic-frame replacement.
                native.append(byte)
                controls.append(byte)
                if byte == 7 or (self._string_escape and byte == 92):
                    self._frame = "ground"
                self._string_escape = byte == 27
                continue
            self._sequence.append(byte)
            if self._frame == "escape":
                if byte in b"]P_^X":
                    native.extend(self._sequence)
                    controls.extend(self._sequence)
                    self._sequence.clear()
                    self._frame = "string"
                    self._string_escape = False
                    continue
                if byte == ord("["):
                    self._frame = "csi"
                    continue
                if 0x20 <= byte <= 0x2F:
                    self._frame = "intermediate"
                    continue
            elif self._frame == "csi" and not 0x40 <= byte <= 0x7E:
                if len(self._sequence) > 4096:
                    native.extend(self._sequence)
                    controls.extend(self._sequence)
                    self._sequence.clear()
                    self._oversize_csi = True
                continue
            elif self._frame == "intermediate" and not 0x30 <= byte <= 0x7E:
                continue
            sequence = bytes(self._sequence)
            reply = None if self._oversize_csi else self._query_reply(sequence)
            if reply is not None:
                replies.extend(reply)
            else:
                native.extend(sequence)
                if self._oversize_csi or _terminal_control(sequence):
                    controls.extend(sequence)
                elif (synchronized := _synchronized_update(sequence)) is not None:
                    controls.extend(sequence)
                    self.synchronized_update = synchronized
                    self._stream.feed(sequence)  # Preserve other modes in a grouped command.
                else:
                    self._stream.feed(sequence)
            self._sequence.clear()
            self._oversize_csi = False
            self._frame = "ground"
        self._stream.feed(bytes(plain))
        return NativeTerminalOutput(bytes(native), bytes(controls), bytes(replies))

    def _query_reply(self, sequence: bytes) -> bytes | None:
        if not sequence.startswith(b"\x1b[") or not sequence.endswith(b"n"):
            return None
        parameter = sequence[2:-1]
        private = parameter.startswith(b"?")
        parameter = parameter.removeprefix(b"?").lstrip(b"0")
        if parameter == b"5" and not private:
            return b"\x1b[0n"
        if parameter == b"6":
            screen = self.screen
            row = min(max(0, screen.cursor.y), screen.lines - 1)
            column = min(max(0, screen.cursor.x), screen.columns - 1)
            if modes.DECOM in screen.mode and screen.margins is not None:
                row -= screen.margins.top
            prefix = "?" if private else ""
            return f"\x1b[{prefix}{row + 1};{column + 1}R".encode()
        return None


def _synchronized_update(sequence: bytes) -> bool | None:
    if (
        sequence.startswith(b"\x1b[?")
        and sequence.endswith((b"h", b"l"))
        and any(parameter.lstrip(b"0") == b"2026" for parameter in sequence[3:-1].split(b";"))
    ):
        return sequence.endswith(b"h")
    return None


def _terminal_control(sequence: bytes) -> bool:
    """Capabilities stay owned by the actual terminal, not pyte's VT102 defaults."""
    return sequence.startswith(b"\x1b[") and (
        sequence.endswith((b"c", b"n", b"t", b"$p"))
        or sequence.startswith((b"\x1b[>", b"\x1b[="))
        or sequence == b"\x1b[?u"
    )
