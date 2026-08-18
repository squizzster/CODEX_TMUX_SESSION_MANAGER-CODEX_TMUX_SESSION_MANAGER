from __future__ import annotations

import pytest

import rodex_registry.identity as identity_module
from rodex_registry.identity import (
    RODEX_ID_BITS,
    RodexIdError,
    RodexRegistryId,
    RodexRuntimeId,
    RodexSessionId,
    join_signed_bigints_into_a_codex_session_id,
    parse_codex_session_id,
    parse_rodex_registry_id,
    parse_rodex_runtime_id,
    parse_rodex_session_id,
    split_codex_session_id_into_signed_bigints,
)

RODEX_ID_TYPES = (RodexRegistryId, RodexRuntimeId, RodexSessionId)


@pytest.mark.parametrize("id_type", RODEX_ID_TYPES)
@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        (0, "0000000000000000"),
        ((1 << 63) - 1, "7fffffffffffffff"),
        (1 << 63, "8000000000000000"),
        ((1 << 64) - 1, "ffffffffffffffff"),
    ],
)
def test_rodex_ids_have_one_exact_wire_form(
    id_type: type[RodexRegistryId] | type[RodexRuntimeId] | type[RodexSessionId],
    value: int,
    rendered: str,
) -> None:
    rodex_id = id_type(value)

    assert str(rodex_id) == rendered
    assert len(str(rodex_id)) == 16
    assert id_type.parse(rendered) == rodex_id


def test_each_rodex_id_parser_preserves_its_domain() -> None:
    registry_id = RodexRegistryId(1)
    runtime_id = RodexRuntimeId(1)
    session_id = RodexSessionId(1)

    assert registry_id != session_id
    assert runtime_id != registry_id
    assert runtime_id != session_id
    assert parse_rodex_registry_id(registry_id) is registry_id
    assert parse_rodex_registry_id(str(registry_id)) == registry_id
    assert parse_rodex_session_id(session_id) is session_id
    assert parse_rodex_session_id(str(session_id)) == session_id
    assert parse_rodex_runtime_id(runtime_id) is runtime_id
    assert parse_rodex_runtime_id(str(runtime_id)) == runtime_id


@pytest.mark.parametrize("id_type", RODEX_ID_TYPES)
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
def test_rodex_id_parsers_reject_noncanonical_text(
    id_type: type[RodexRegistryId] | type[RodexRuntimeId] | type[RodexSessionId],
    invalid: str,
) -> None:
    with pytest.raises(RodexIdError, match="16 lowercase hexadecimal"):
        id_type.parse(invalid)


@pytest.mark.parametrize("id_type", RODEX_ID_TYPES)
@pytest.mark.parametrize("invalid", [True, False, -1, 1 << 64, 1.5, "0"])
def test_rodex_ids_reject_values_outside_unsigned_64_bits(
    id_type: type[RodexRegistryId] | type[RodexRuntimeId] | type[RodexSessionId],
    invalid: object,
) -> None:
    with pytest.raises(RodexIdError, match="unsigned 64-bit"):
        id_type(invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize("id_type", RODEX_ID_TYPES)
@pytest.mark.parametrize(
    ("unsigned", "signed"),
    [
        (0, 0),
        ((1 << 63) - 1, (1 << 63) - 1),
        (1 << 63, -(1 << 63)),
        ((1 << 64) - 1, -1),
    ],
)
def test_rodex_id_bigint_codec_is_lossless(
    id_type: type[RodexRegistryId] | type[RodexRuntimeId] | type[RodexSessionId],
    unsigned: int,
    signed: int,
) -> None:
    rodex_id = id_type(unsigned)

    assert rodex_id.as_signed_bigint() == signed
    assert id_type.from_signed_bigint(signed) == rodex_id


@pytest.mark.parametrize("id_type", RODEX_ID_TYPES)
@pytest.mark.parametrize("invalid", [True, False, -(1 << 63) - 1, 1 << 63, 1.5, "0"])
def test_rodex_ids_reject_values_outside_sqlite_bigint(
    id_type: type[RodexRegistryId] | type[RodexRuntimeId] | type[RodexSessionId],
    invalid: object,
) -> None:
    with pytest.raises(RodexIdError, match="signed 64-bit range"):
        id_type.from_signed_bigint(invalid)  # type: ignore[arg-type]


def test_each_rodex_id_generation_requests_exactly_64_secure_random_bits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_bits: list[int] = []

    def generate(bits: int) -> int:
        requested_bits.append(bits)
        return 0x0123456789ABCDEF

    monkeypatch.setattr(identity_module.secrets, "randbits", generate)

    assert str(RodexRegistryId.generate()) == "0123456789abcdef"
    assert str(RodexRuntimeId.generate()) == "0123456789abcdef"
    assert str(RodexSessionId.generate()) == "0123456789abcdef"
    assert requested_bits == [RODEX_ID_BITS, RODEX_ID_BITS, RODEX_ID_BITS]


@pytest.mark.parametrize("id_type", RODEX_ID_TYPES)
@pytest.mark.parametrize("value", [0, None, b"0000000000000000"])
def test_rodex_id_parsers_reject_non_string_boundary_values(
    id_type: type[RodexRegistryId] | type[RodexRuntimeId] | type[RodexSessionId],
    value: object,
) -> None:
    with pytest.raises(RodexIdError):
        id_type.parse(value)  # type: ignore[arg-type]


def test_codex_session_id_remains_a_lossless_128_bit_identity() -> None:
    codex_session_id = parse_codex_session_id("01a00e9a-80f4-7ea2-83a5-f6ef25ac5e65")

    stored_parts = split_codex_session_id_into_signed_bigints(codex_session_id)

    assert len(stored_parts) == 2
    assert join_signed_bigints_into_a_codex_session_id(*stored_parts) == codex_session_id


def test_rodex_runtime_id_is_one_lossless_distinct_64_bit_identity() -> None:
    runtime_id = parse_rodex_runtime_id("e6877350da744e32")

    assert isinstance(runtime_id, RodexRuntimeId)
    assert runtime_id.as_signed_bigint() == -1835371532030554574
    assert RodexRuntimeId.from_signed_bigint(runtime_id.as_signed_bigint()) == runtime_id
