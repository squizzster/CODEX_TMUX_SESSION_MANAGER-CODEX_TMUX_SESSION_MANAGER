"""Canonical ID types and lossless SQLite BIGINT codecs for Rodex."""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from typing import ClassVar, Final, Self

RODEX_ID_BITS: Final = 64
RODEX_ID_HEX_CHARACTERS: Final = 16
_UNSIGNED_64_LIMIT: Final = 1 << RODEX_ID_BITS
_SIGNED_64_SIGN_BIT: Final = 1 << (RODEX_ID_BITS - 1)
_SIGNED_64_MIN: Final = -_SIGNED_64_SIGN_BIT
_SIGNED_64_MAX: Final = _SIGNED_64_SIGN_BIT - 1
_CANONICAL_RODEX_ID = re.compile(r"^[0-9a-f]{16}$")

type CodexSessionId = uuid.UUID
type CodexThreadId = uuid.UUID
type CodexTurnId = uuid.UUID
type CodexItemId = uuid.UUID


class RodexIdError(ValueError):
    """A Rodex ID violated its canonical 64-bit contract."""


@dataclass(frozen=True, order=True, slots=True)
class _RodexId:
    """Shared exact representation for distinct Rodex-owned ID domains."""

    value: int
    _domain_name: ClassVar[str] = "Rodex"

    def __post_init__(self) -> None:
        if type(self.value) is not int or not 0 <= self.value < _UNSIGNED_64_LIMIT:
            raise RodexIdError(f"{self._domain_name} ID must be an unsigned 64-bit integer")

    @classmethod
    def generate(cls) -> Self:
        """Generate one cryptographically secure random 64-bit ID candidate."""
        return cls(secrets.randbits(RODEX_ID_BITS))

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse only the exact 16-character lowercase hexadecimal wire form."""
        if not isinstance(text, str) or _CANONICAL_RODEX_ID.fullmatch(text) is None:
            raise RodexIdError(
                f"{cls._domain_name} ID must be exactly 16 lowercase hexadecimal characters"
            )
        return cls(int(text, 16))

    @classmethod
    def from_signed_bigint(cls, stored_value: int) -> Self:
        """Restore all 64 ID bits from one SQLite signed BIGINT value."""
        if (
            type(stored_value) is not int
            or not _SIGNED_64_MIN <= stored_value <= _SIGNED_64_MAX
        ):
            raise RodexIdError(
                f"stored {cls._domain_name} ID is outside SQLite's signed 64-bit range"
            )
        return cls(stored_value % _UNSIGNED_64_LIMIT)

    def as_signed_bigint(self) -> int:
        """Map all 64 ID bits into SQLite's signed BIGINT range."""
        return (
            self.value if self.value <= _SIGNED_64_MAX else self.value - _UNSIGNED_64_LIMIT
        )

    def __str__(self) -> str:
        return f"{self.value:016x}"


class RodexSessionId(_RodexId):
    """One opaque public Rodex session ID."""

    __slots__ = ()
    _domain_name = "Rodex session"


class RodexRuntimeId(_RodexId):
    """One opaque Rodex runtime-incarnation ID."""

    __slots__ = ()
    _domain_name = "Rodex runtime"


class RodexRegistryId(_RodexId):
    """One opaque Rodex database-registry ID."""

    __slots__ = ()
    _domain_name = "Rodex registry"


@dataclass(frozen=True, slots=True)
class RodexAnalyticsIdentityFence:
    """Immutable durable identity of one live analytics worker incarnation."""

    rodex_sessions_id: int
    rodex_session_id: RodexSessionId
    rodex_registry_id: RodexRegistryId
    runtime_id: RodexRuntimeId
    codex_session_id: CodexSessionId

    def __post_init__(self) -> None:
        if (
            not isinstance(self.rodex_sessions_id, int)
            or isinstance(self.rodex_sessions_id, bool)
            or self.rodex_sessions_id <= 0
        ):
            raise ValueError("rodex_sessions_id must be a positive integer")
        object.__setattr__(
            self,
            "rodex_session_id",
            parse_rodex_session_id(self.rodex_session_id),
        )
        object.__setattr__(
            self,
            "rodex_registry_id",
            parse_rodex_registry_id(self.rodex_registry_id),
        )
        object.__setattr__(
            self,
            "runtime_id",
            parse_rodex_runtime_id(self.runtime_id),
        )
        object.__setattr__(
            self,
            "codex_session_id",
            parse_codex_session_id(self.codex_session_id),
        )


def parse_rodex_session_id(value: RodexSessionId | str) -> RodexSessionId:
    """Accept an existing session ID or parse its canonical wire representation."""
    if isinstance(value, RodexSessionId):
        return value
    return RodexSessionId.parse(value)


def parse_rodex_runtime_id(value: RodexRuntimeId | str) -> RodexRuntimeId:
    """Accept an existing runtime ID or parse its canonical wire representation."""
    if isinstance(value, RodexRuntimeId):
        return value
    return RodexRuntimeId.parse(value)


def parse_rodex_registry_id(value: RodexRegistryId | str) -> RodexRegistryId:
    """Accept an existing registry ID or parse its canonical wire representation."""
    if isinstance(value, RodexRegistryId):
        return value
    return RodexRegistryId.parse(value)


def split_codex_session_id_into_signed_bigints(
    value: CodexSessionId | str,
) -> tuple[int, int]:
    """Map one 128-bit Codex session ID into two lossless signed BIGINTs."""
    return _split_128_bit_id_into_signed_bigints(parse_codex_session_id(value))


def split_codex_thread_id_into_signed_bigints(
    value: CodexThreadId | str,
) -> tuple[int, int]:
    """Map one 128-bit Codex thread ID into two lossless signed BIGINTs."""
    return _split_128_bit_id_into_signed_bigints(parse_codex_thread_id(value))


def split_codex_turn_id_into_signed_bigints(
    value: CodexTurnId | str,
) -> tuple[int, int]:
    """Map one 128-bit Codex turn ID into two lossless signed BIGINTs."""
    return _split_128_bit_id_into_signed_bigints(parse_codex_turn_id(value))


def split_codex_item_id_into_signed_bigints(
    value: CodexItemId | str,
) -> tuple[int, int]:
    """Map one 128-bit Codex item ID into two lossless signed BIGINTs."""
    return _split_128_bit_id_into_signed_bigints(parse_codex_item_id(value))


def parse_codex_session_id(value: CodexSessionId | str) -> CodexSessionId:
    """Parse the exact 128-bit Codex-owned session ID domain."""
    return _parse_codex_uuid(value, "session")


def parse_codex_thread_id(value: CodexThreadId | str) -> CodexThreadId:
    """Parse the exact 128-bit Codex-owned thread ID domain."""
    return _parse_codex_uuid(value, "thread")


def parse_codex_turn_id(value: CodexTurnId | str) -> CodexTurnId:
    """Parse the exact 128-bit Codex-owned turn ID domain."""
    return _parse_codex_uuid(value, "turn")


def parse_codex_item_id(value: CodexItemId | str) -> CodexItemId:
    """Parse the exact 128-bit Codex-owned item ID domain."""
    return _parse_codex_uuid(value, "item")


def join_signed_bigints_into_a_codex_session_id(
    high_signed: int, low_signed: int
) -> CodexSessionId:
    """Restore one 128-bit Codex session ID from two signed BIGINTs."""
    return uuid.UUID(
        int=(_signed_64_to_unsigned(high_signed) << 64) | _signed_64_to_unsigned(low_signed)
    )


def join_signed_bigints_into_a_codex_thread_id(
    high_signed: int, low_signed: int
) -> CodexThreadId:
    """Restore one 128-bit Codex thread ID from two signed BIGINTs."""
    return uuid.UUID(
        int=(_signed_64_to_unsigned(high_signed) << 64) | _signed_64_to_unsigned(low_signed)
    )


def join_signed_bigints_into_a_codex_turn_id(
    high_signed: int, low_signed: int
) -> CodexTurnId:
    """Restore one 128-bit Codex turn ID from two signed BIGINTs."""
    return _join_signed_bigints_into_a_uuid(high_signed, low_signed)


def join_signed_bigints_into_a_codex_item_id(
    high_signed: int, low_signed: int
) -> CodexItemId:
    """Restore one 128-bit Codex item ID from two signed BIGINTs."""
    return _join_signed_bigints_into_a_uuid(high_signed, low_signed)


def _parse_codex_uuid(value: uuid.UUID | str, domain: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        raise TypeError(f"Codex {domain} ID must be a 128-bit ID or string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise ValueError(f"Codex {domain} ID must be a valid 128-bit ID") from error
    if str(parsed) != value:
        raise ValueError(f"Codex {domain} ID must use canonical lowercase UUID text")
    return parsed


def _join_signed_bigints_into_a_uuid(high_signed: int, low_signed: int) -> uuid.UUID:
    return uuid.UUID(
        int=(_signed_64_to_unsigned(high_signed) << 64) | _signed_64_to_unsigned(low_signed)
    )


def _split_128_bit_id_into_signed_bigints(value: uuid.UUID) -> tuple[int, int]:
    high_unsigned = value.int >> 64
    low_unsigned = value.int & (_UNSIGNED_64_LIMIT - 1)
    return _unsigned_64_to_signed(high_unsigned), _unsigned_64_to_signed(low_unsigned)


def _unsigned_64_to_signed(value: int) -> int:
    if type(value) is not int or not 0 <= value < _UNSIGNED_64_LIMIT:
        raise ValueError("128-bit ID half must be an unsigned 64-bit integer")
    return value if value <= _SIGNED_64_MAX else value - _UNSIGNED_64_LIMIT


def _signed_64_to_unsigned(value: int) -> int:
    if type(value) is not int or not _SIGNED_64_MIN <= value <= _SIGNED_64_MAX:
        raise ValueError("stored 128-bit ID half is outside SQLite's signed 64-bit range")
    return value % _UNSIGNED_64_LIMIT
