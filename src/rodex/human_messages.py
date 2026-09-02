"""Canonical human-facing Rodex session messages."""

from __future__ import annotations


def rodex_session_message(
    action: str,
    display_name: str,
    *,
    detail: str | None = None,
) -> str:
    """Place action first and the user-facing session identity in brackets."""
    message = f"Rodex {action} [{display_name}]"
    return f"{message}." if detail is None else f"{message}: {detail}."
