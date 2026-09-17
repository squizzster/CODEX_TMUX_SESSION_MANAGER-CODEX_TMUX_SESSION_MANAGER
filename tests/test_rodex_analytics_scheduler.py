from __future__ import annotations

import uuid

import pytest

from rodex.analytics_scheduler import analytics_event_thread_id

THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"method": "unrelated", "params": {"threadId": str(THREAD_ID)}},
        {"method": "turn/completed", "params": None},
        {"method": "turn/completed", "params": {"threadId": "invalid"}},
        {"method": "thread/started", "params": {"thread": None}},
    ],
)
def test_irrelevant_or_malformed_event_has_no_analytics_identity(event: dict[str, object]) -> None:
    assert analytics_event_thread_id(event) is None


@pytest.mark.parametrize(
    "event",
    [
        {"method": "thread/started", "params": {"thread": {"id": str(THREAD_ID)}}},
        {"method": "turn/started", "params": {"threadId": str(THREAD_ID)}},
        {"method": "turn/completed", "params": {"threadId": str(THREAD_ID)}},
    ],
)
def test_relevant_event_returns_its_exact_thread_identity(event: dict[str, object]) -> None:
    assert analytics_event_thread_id(event) == THREAD_ID
