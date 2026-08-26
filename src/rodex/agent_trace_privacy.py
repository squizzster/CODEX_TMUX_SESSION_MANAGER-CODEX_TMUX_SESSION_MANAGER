"""Shared privacy classification for rollout-addressable trace bodies."""

from __future__ import annotations

import json

CODEX_ENCRYPTED_VALUE_MARKER = "gAAAA"


def contains_codex_encrypted_value(value: object) -> bool:
    """Recognize Codex encrypted values consistently before storage or display."""
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    return CODEX_ENCRYPTED_VALUE_MARKER in rendered


def redact_codex_encrypted_text(value: str) -> str:
    """Redact a complete text field when it contains a Codex encrypted value."""
    return "<encrypted>" if contains_codex_encrypted_value(value) else value
