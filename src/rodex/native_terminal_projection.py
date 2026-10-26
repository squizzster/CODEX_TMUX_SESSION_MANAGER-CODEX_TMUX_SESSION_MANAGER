"""Frame native output once; separate terminal replies/controls from presentation."""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass

import pyte
from pyte import modes

from .native_terminal_screen import NativeTerminalScreen
from .terminal_cells import TMUX_32_PROFILE, TerminalProfile


class _ProjectionByteStream(pyte.ByteStream):
    """Decode text independently of complete ASCII terminal instructions.

    tmux retains a partial UTF-8 character across ESC instructions, but C0
    actions cancel it. Feeding both through one UTF-8 decode conflates these.
    """

    _instructions = re.compile(rb"(\x1b(?:\[[\x20-\x3f]*[\x40-\x7e]|[\x20-\x2f]*[\x30-\x7e]))")

    def __init__(self, screen: NativeTerminalScreen) -> None:
        super().__init__(screen)
        self.utf8_decoder = codecs.getincrementaldecoder("utf-8")("ignore")

    def feed(self, data: bytes) -> None:
        for index, part in enumerate(self._instructions.split(data)):
            if not part:
                continue
            if index % 2:
                # Pyte supports a smaller grammar. Unknown complete grammar
                # must remain opaque, never leak its tail into screen text.
                if re.fullmatch(rb"\x1b(?:\[\??[0-9;]*[\x40-\x7e]|[#%()][\x30-\x7e]|[\x30-\x7e])", part):
                    pyte.Stream.feed(self, part.decode("ascii"))
            else:
                super().feed(part)


@dataclass(frozen=True, slots=True)
class NativeTerminalOutput:
    native: bytes
    controls: bytes
    replies: bytes


class NativeTerminalProjection:
    """Own native coordinates and terminal-query semantics independent of display policy."""

    def __init__(self, columns: int, rows: int, *, profile: TerminalProfile = TMUX_32_PROFILE) -> None:
        self.screen = NativeTerminalScreen(columns, rows, profile=profile)
        self._stream = _ProjectionByteStream(self.screen)
        self._sequence = bytearray()
        self._frame = "ground"
        self._instruction = bytearray()
        self._control_sequence = False
        self._oversize_csi = False
        self.synchronized_update = False

    @property
    def at_boundary(self) -> bool:
        return self._frame == "ground" and not self._stream.utf8_decoder.getstate()[0] and not self.synchronized_update

    @property
    def paintable(self) -> bool:
        return (
            self.at_boundary
            and self.screen.profile.verified
            and self.screen.columns >= 3
            and modes.DECOM not in self.screen.mode
            and modes.IRM not in self.screen.mode
            and 0 <= self.screen.cursor.x < self.screen.columns
            and 0 <= self.screen.cursor.y < self.screen.lines
        )

    def feed(self, data: bytes) -> NativeTerminalOutput:
        native, controls, replies, projection = bytearray(), bytearray(), bytearray(), bytearray()
        for byte in data:
            if self._frame == "ground":
                if byte == 27:
                    self._begin_escape()
                else:
                    projection.append(byte)
                    native.append(byte)
                    if byte == 7:
                        controls.append(byte)
                continue

            # tmux DCS payloads have their own grammar: even CAN/SUB and BEL
            # are data. An ESC followed by a non-ST byte returns to the body.
            if self._frame in {"dcs_body", "dcs_escape"}:
                native.append(byte)
                controls.append(byte)
                if self._frame == "dcs_escape":
                    self._frame = "ground" if byte == 92 else "dcs_body"
                elif byte == 27:
                    self._frame = "dcs_escape"
                continue

            opaque = self._frame in {"osc", "apc", "consume_st", "rename"} or self._frame.startswith("dcs_")
            if byte in {24, 26, 27}:
                # Never give Pyte an abandoned grammar fragment. It receives
                # only completed instructions and separately executed C0s.
                native.extend(self._sequence)
                controls.extend(self._sequence)
                inherited_control = opaque or self._control_sequence or self._oversize_csi or bool(self._sequence)
                self._clear_sequence()
                if byte == 27:
                    self._begin_escape(control=inherited_control)
                else:
                    native.append(byte)
                    controls.append(byte)
                    projection.append(byte)  # CAN/SUB also cancel pending UTF-8.
                continue

            if opaque:
                native.append(byte)
                controls.append(byte)
                if self._frame == "osc" and byte == 7:
                    self._frame = "ground"
                elif self._frame.startswith("dcs_"):
                    self._advance_dcs(byte)
                continue

            self._sequence.append(byte)
            if len(self._sequence) > 4096:
                native.extend(self._sequence)
                controls.extend(self._sequence)
                self._sequence.clear()
                self._instruction.clear()
                self._oversize_csi = True
            if byte < 32:
                projection.append(byte)  # C0 executes without ending ESC/CSI.
                continue
            if byte >= 127:
                continue
            self._instruction.append(byte)
            if self._frame == "escape":
                strings = {93: "osc", 80: "dcs_enter", 95: "apc", 94: "consume_st", 88: "consume_st", 107: "rename"}
                if byte in strings:
                    native.extend(self._sequence)
                    controls.extend(self._sequence)
                    self._clear_sequence()
                    self._frame = strings[byte]
                    continue
                if byte == 91:
                    self._frame = "csi_enter"
                    continue
                if 0x20 <= byte <= 0x2F:
                    self._frame = "intermediate"
                    continue
            elif self._frame.startswith("csi_"):
                if not 0x40 <= byte <= 0x7E:
                    if (self._frame == "csi_intermediate" and byte >= 0x30) or (
                        self._frame == "csi_parameter" and 0x3C <= byte <= 0x3F
                    ):
                        self._frame = "csi_ignore"
                    elif self._frame != "csi_ignore":
                        self._frame = "csi_intermediate" if byte < 0x30 else "csi_parameter"
                    continue
            elif self._frame == "intermediate" and byte < 0x30:
                continue
            sequence = bytes(self._instruction)
            raw = bytes(self._sequence)
            ignored = self._oversize_csi or self._frame == "csi_ignore"
            encoding_switch = sequence in {b"\x1b%@", b"\x1b%G", b"\x1b%8"}
            if projection and (encoding_switch or (sequence.startswith(b"\x1b[") and sequence.endswith(b"n"))):
                self._stream.feed(bytes(projection))
                projection.clear()
            reply = None if ignored else self._query_reply(sequence)
            if reply is not None:
                # Retain embedded controls even when intercepting a query.
                embedded = bytes(value for value in raw if value < 32 and value != 27)
                if self._control_sequence:
                    # The intercepted query's ESC would cancel this fragment.
                    # Preserve that effect without sending a second query.
                    native.extend(b"\x1b\\")
                    controls.extend(b"\x1b\\")
                native.extend(embedded)
                controls.extend(embedded)
                replies.extend(reply)
            else:
                native.extend(raw)
                terminal_control = ignored or sequence == b"\x1b\\" or _terminal_control(sequence)
                synchronized = _synchronized_update(sequence) if not ignored else None
                if terminal_control or synchronized is not None or self._control_sequence:
                    controls.extend(raw)
                else:
                    controls.extend(value for value in raw if value == 7)
                if synchronized is not None:
                    self.synchronized_update = synchronized
                if not terminal_control:
                    if encoding_switch:
                        self._stream.feed(sequence)
                    else:
                        projection.extend(sequence)
            self._clear_sequence()
        if projection:
            self._stream.feed(bytes(projection))
        return NativeTerminalOutput(bytes(native), bytes(controls), bytes(replies))

    def _begin_escape(self, *, control: bool = False) -> None:
        self._sequence = bytearray(b"\x1b")
        self._instruction = bytearray(b"\x1b")
        self._frame = "escape"
        self._control_sequence = control

    def _clear_sequence(self) -> None:
        self._sequence.clear()
        self._instruction.clear()
        self._oversize_csi = False
        self._control_sequence = False
        self._frame = "ground"

    def _advance_dcs(self, byte: int) -> None:
        if byte < 32 or byte >= 127 or self._frame == "dcs_ignore":
            return
        if 0x40 <= byte <= 0x7E:
            self._frame = "dcs_body"
        elif (
            (self._frame == "dcs_intermediate" and byte >= 0x30)
            or byte == 0x3A
            or (self._frame == "dcs_parameter" and 0x3C <= byte <= 0x3F)
        ):
            self._frame = "dcs_ignore"
        else:
            self._frame = "dcs_intermediate" if byte < 0x30 else "dcs_parameter"

    def _query_reply(self, sequence: bytes) -> bytes | None:
        if not self.screen.profile.verified:
            return None  # Unknown occupancy: the actual terminal owns replies.
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
            column = min(max(0, screen.cursor.x), screen.columns)
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
