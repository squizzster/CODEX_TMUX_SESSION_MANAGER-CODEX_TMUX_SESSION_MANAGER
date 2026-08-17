from __future__ import annotations

import uuid

import pytest

import rodex_registry.identity as identity_module
from rodex_registry.identity import (
    RODEX_SESSION_IDENTIFIER_BITS,
    RodexSessionIdentifier,
    RodexSessionIdentifierError,
    join_signed_bigints_into_a_codex_session_uuid,
    join_signed_bigints_into_a_rodex_registry_uuid,
    parse_rodex_session_identifier,
    split_codex_session_uuid_into_signed_bigints,
    split_rodex_registry_uuid_into_signed_bigints,
)


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        (0, "0000000000000000"),
        ((1 << 63) - 1, "7fffffffffffffff"),
        (1 << 63, "8000000000000000"),
        ((1 << 64) - 1, "ffffffffffffffff"),
    ],
)
def test_identifier_has_one_exact_canonical_wire_form(value: int, rendered: str) -> None:
    identifier = RodexSessionIdentifier(value)

    assert str(identifier) == rendered
    assert len(str(identifier)) == 16
    assert RodexSessionIdentifier.parse(rendered) == identifier
    assert parse_rodex_session_identifier(identifier) is identifier
    assert parse_rodex_session_identifier(rendered) == identifier


@pytest.mark.parametrize(
    "invalid",
    [
        "000000000000000",
        "00000000000000000",
        "000000000000000G",
        "000000000000000A",
        " 0000000000000000",
        "0000000000000000 ",
        "00000000-00000000",
        "00000000-0000-0000-0000-000000000000",
        "",
    ],
)
def test_identifier_parser_rejects_every_noncanonical_text(invalid: str) -> None:
    with pytest.raises(RodexSessionIdentifierError, match="16 lowercase hexadecimal"):
        RodexSessionIdentifier.parse(invalid)


@pytest.mark.parametrize("invalid", [True, False, -1, 1 << 64, 1.5, "0"])
def test_identifier_value_rejects_non_unsigned_64_bit_integers(invalid: object) -> None:
    with pytest.raises(RodexSessionIdentifierError, match="unsigned 64-bit"):
        RodexSessionIdentifier(invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("unsigned", "signed"),
    [
        (0, 0),
        ((1 << 63) - 1, (1 << 63) - 1),
        (1 << 63, -(1 << 63)),
        ((1 << 64) - 1, -1),
    ],
)
def test_identifier_signed_bigint_codec_is_lossless(unsigned: int, signed: int) -> None:
    identifier = RodexSessionIdentifier(unsigned)

    assert identifier.as_signed_bigint() == signed
    assert RodexSessionIdentifier.from_signed_bigint(signed) == identifier


@pytest.mark.parametrize("invalid", [True, False, -(1 << 63) - 1, 1 << 63, 1.5, "0"])
def test_identifier_rejects_values_outside_sqlite_signed_bigint(
    invalid: object,
) -> None:
    with pytest.raises(RodexSessionIdentifierError, match="signed 64-bit range"):
        RodexSessionIdentifier.from_signed_bigint(invalid)  # type: ignore[arg-type]


def test_identifier_generation_requests_exactly_64_random_bits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_bits: list[int] = []

    def generate(bits: int) -> int:
        requested_bits.append(bits)
        return 0x0123456789ABCDEF

    monkeypatch.setattr(identity_module.secrets, "randbits", generate)

    assert str(RodexSessionIdentifier.generate()) == "0123456789abcdef"
    assert requested_bits == [RODEX_SESSION_IDENTIFIER_BITS]


@pytest.mark.parametrize(
    "value",
    [
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        0,
        None,
        b"0000000000000000",
    ],
)
def test_identifier_parser_rejects_non_string_boundary_values(value: object) -> None:
    with pytest.raises(RodexSessionIdentifierError):
        RodexSessionIdentifier.parse(value)  # type: ignore[arg-type]


def test_codex_and_registry_uuid_codecs_remain_explicit_and_lossless() -> None:
    codex_uuid = uuid.UUID("01a00e9a-80f4-7ea2-83a5-f6ef25ac5e65")
    registry_uuid = uuid.UUID("06179a35-8126-4d53-9a42-e1042bfc1cb0")

    codex_halves = split_codex_session_uuid_into_signed_bigints(codex_uuid)
    registry_halves = split_rodex_registry_uuid_into_signed_bigints(registry_uuid)

    assert join_signed_bigints_into_a_codex_session_uuid(*codex_halves) == codex_uuid
    assert join_signed_bigints_into_a_rodex_registry_uuid(*registry_halves) == registry_uuid
    assert codex_halves != registry_halves
