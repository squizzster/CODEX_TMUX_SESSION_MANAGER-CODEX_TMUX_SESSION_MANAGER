"""Small value-normalisation contracts shared inside the registry."""

from __future__ import annotations

from datetime import UTC, datetime


def _utc_now_timestamp() -> str:
    return _normalise_utc_datetime(datetime.now(UTC))


def _normalise_required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _normalise_utc_timestamp_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("calculated_at_utc must be a non-empty UTC timestamp")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("calculated_at_utc must be a valid UTC timestamp") from error
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("calculated_at_utc must be timezone-aware")
    return _normalise_utc_datetime(instant)


def _normalise_utc_datetime(value: datetime | None) -> str:
    instant = datetime.now(UTC) if value is None else value
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("UTC timestamps must be timezone-aware")
    return instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_session_id(session_id: int) -> None:
    _validate_positive_id(session_id, "session_id")


def _validate_positive_id(value: int, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
