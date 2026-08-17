"""Finite retry policy for replaceable values protected by unique indexes."""

from __future__ import annotations

from typing import Final

INDEX_RE_TRY_ATTEMPTS: Final = 10


def index_re_try_attempt_numbers() -> range:
    """Return every permitted one-based index retry attempt exactly once."""
    return range(1, INDEX_RE_TRY_ATTEMPTS + 1)
