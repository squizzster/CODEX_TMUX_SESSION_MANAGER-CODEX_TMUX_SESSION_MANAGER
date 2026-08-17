"""Canonical identity types and lossless SQLite integer codecs for Rodex."""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Final

RODEX_SESSION_IDENTIFIER_BITS: Final = 64
RODEX_SESSION_IDENTIFIER_HEX_CHARACTERS: Final = 16
_UNSIGNED_64_LIMIT: Final = 1 << RODEX_SESSION_IDENTIFIER_BITS
_SIGNED_64_SIGN_BIT: Final = 1 << (RODEX_SESSION_IDENTIFIER_BITS - 1)
_SIGNED_64_MIN: Final = -_SIGNED_64_SIGN_BIT
_SIGNED_64_MAX: Final = _SIGNED_64_SIGN_BIT - 1
_CANONICAL_IDENTIFIER = re.compile(r"^[0-9a-f]{16}$")


class RodexSessionIdentifierError(ValueError):
    """A Rodex session identifier violated its canonical 64-bit contract."""


@dataclass(frozen=True, order=True, slots=True)
class RodexSessionIdentifier:
    """One opaque 64-bit Rodex session identity with canonical lowercase rendering."""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int or not 0 <= self.value < _UNSIGNED_64_LIMIT:
            raise RodexSessionIdentifierError(
                "Rodex session identifier must be an unsigned 64-bit integer"
            )

    @classmethod
    def generate(cls) -> RodexSessionIdentifier:
        """Generate one cryptographically random 64-bit identifier candidate."""
        return cls(secrets.randbits(RODEX_SESSION_IDENTIFIER_BITS))

    @classmethod
    def parse(cls, text: str) -> RodexSessionIdentifier:
        """Parse only the exact 16-character lowercase hexadecimal wire form."""
        if not isinstance(text, str) or _CANONICAL_IDENTIFIER.fullmatch(text) is None:
            raise RodexSessionIdentifierError(
                "Rodex session identifier must be exactly 16 lowercase "
                "hexadecimal characters"
            )
        return cls(int(text, 16))

    @classmethod
    def from_signed_bigint(cls, stored_value: int) -> RodexSessionIdentifier:
        """Restore all 64 bits from one SQLite signed integer value."""
        if (
            type(stored_value) is not int
            or not _SIGNED_64_MIN <= stored_value <= _SIGNED_64_MAX
        ):
            raise RodexSessionIdentifierError(
                "stored Rodex session identifier is outside SQLite's signed 64-bit range"
            )
        return cls(stored_value % _UNSIGNED_64_LIMIT)

    def as_signed_bigint(self) -> int:
        """Map all 64 identity bits into SQLite's signed integer range."""
        return (
            self.value if self.value <= _SIGNED_64_MAX else self.value - _UNSIGNED_64_LIMIT
        )

    def __str__(self) -> str:
        return f"{self.value:016x}"


def parse_rodex_session_identifier(
    value: RodexSessionIdentifier | str,
) -> RodexSessionIdentifier:
    """Accept an existing domain value or parse its canonical wire representation."""
    if isinstance(value, RodexSessionIdentifier):
        return value
    return RodexSessionIdentifier.parse(value)


def split_codex_session_uuid_into_signed_bigints(
    value: uuid.UUID | str,
) -> tuple[int, int]:
    """Map one Codex UUID into its two lossless SQLite signed integers."""
    return _split_uuid_into_signed_bigints(_parse_uuid(value, "Codex session UUID"))


def parse_codex_session_uuid(value: uuid.UUID | str) -> uuid.UUID:
    """Parse the exact Codex-owned UUID domain."""
    return _parse_uuid(value, "Codex session UUID")


def join_signed_bigints_into_a_codex_session_uuid(
    high_signed: int, low_signed: int
) -> uuid.UUID:
    """Restore one Codex UUID from its two SQLite signed integers."""
    return _join_signed_bigints_into_uuid(high_signed, low_signed)


def split_rodex_registry_uuid_into_signed_bigints(
    value: uuid.UUID | str,
) -> tuple[int, int]:
    """Map one Rodex registry UUID into its two lossless SQLite signed integers."""
    return _split_uuid_into_signed_bigints(_parse_uuid(value, "Rodex registry UUID"))


def parse_rodex_registry_uuid(value: uuid.UUID | str) -> uuid.UUID:
    """Parse the exact Rodex registry UUID domain."""
    return _parse_uuid(value, "Rodex registry UUID")


def join_signed_bigints_into_a_rodex_registry_uuid(
    high_signed: int, low_signed: int
) -> uuid.UUID:
    """Restore one Rodex registry UUID from its two SQLite signed integers."""
    return _join_signed_bigints_into_uuid(high_signed, low_signed)


def _parse_uuid(value: uuid.UUID | str, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a UUID or string")
    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid UUID") from error


def _split_uuid_into_signed_bigints(value: uuid.UUID) -> tuple[int, int]:
    high_unsigned = value.int >> 64
    low_unsigned = value.int & (_UNSIGNED_64_LIMIT - 1)
    return _unsigned_64_to_signed(high_unsigned), _unsigned_64_to_signed(low_unsigned)


def _join_signed_bigints_into_uuid(high_signed: int, low_signed: int) -> uuid.UUID:
    return uuid.UUID(
        int=(_signed_64_to_unsigned(high_signed) << 64) | _signed_64_to_unsigned(low_signed)
    )


def _unsigned_64_to_signed(value: int) -> int:
    if type(value) is not int or not 0 <= value < _UNSIGNED_64_LIMIT:
        raise ValueError("UUID half must be an unsigned 64-bit integer")
    return value if value <= _SIGNED_64_MAX else value - _UNSIGNED_64_LIMIT


def _signed_64_to_unsigned(value: int) -> int:
    if type(value) is not int or not _SIGNED_64_MIN <= value <= _SIGNED_64_MAX:
        raise ValueError("stored UUID half is outside SQLite's signed 64-bit range")
    return value % _UNSIGNED_64_LIMIT
