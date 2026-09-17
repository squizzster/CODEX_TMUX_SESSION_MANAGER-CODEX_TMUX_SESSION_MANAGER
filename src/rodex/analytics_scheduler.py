"""Shared analytics scheduling contracts with no independent execution path."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from rodex_registry import CodexThreadId, parse_codex_thread_id

from .app_server_contract import CODEX_APP_SERVER
from .protocol_proxy import ANALYTICS_WAKE_EVENT_METHODS

ANALYTICS_QUIET_SECONDS: Final = 0.5
ANALYTICS_MAX_BATCH_SECONDS: Final = 5.0
ANALYTICS_RETRY_INITIAL_SECONDS: Final = 0.5
ANALYTICS_MAX_RETRY_WINDOW_SECONDS: Final = 5.0


@dataclass(frozen=True, slots=True)
class AnalyticsDirtyBatch:
    """Exact source identities coalesced into one coordinator reconciliation."""

    thread_ids: frozenset[CodexThreadId]
    full_reconcile: bool = False


def analytics_event_thread_id(event: Mapping[str, Any]) -> CodexThreadId | None:
    """Return the exact thread identity for an analytics-relevant protocol event."""
    method = event.get("method")
    if method not in ANALYTICS_WAKE_EVENT_METHODS:
        return None
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    if method == CODEX_APP_SERVER.thread_started_method:
        thread = params.get("thread")
        value = thread.get("id") if isinstance(thread, Mapping) else None
    else:
        value = params.get("threadId")
    try:
        return parse_codex_thread_id(value)
    except (TypeError, ValueError):
        return None
