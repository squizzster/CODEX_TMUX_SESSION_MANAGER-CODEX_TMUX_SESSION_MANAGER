from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread

import pytest

from rodex.analytics_scheduler import (
    AnalyticsBurstWindow,
    AnalyticsEventScheduler,
    AnalyticsEventStreamClosed,
    AnalyticsProtocolEventSubscriber,
    _is_relevant_protocol_event,
)
from rodex.protocol_proxy import CodexProtocolEventTap


def test_empty_scheduler_blocks_without_repeated_reconciliation() -> None:
    scheduler = AnalyticsEventScheduler(quiet_seconds=0.01, max_batch_seconds=0.05)
    startup_complete = Event()
    reconciliations = 0

    def reconcile() -> None:
        nonlocal reconciliations
        reconciliations += 1
        startup_complete.set()

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    assert startup_complete.wait(timeout=1)

    assert reconciliations == 1
    scheduler.close()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert reconciliations == 1


def test_many_dirty_signals_coalesce_into_one_reconciliation() -> None:
    scheduler = AnalyticsEventScheduler(quiet_seconds=0.01, max_batch_seconds=0.05)
    startup_complete = Event()
    reconciled = Event()
    reconciliations = 0

    def reconcile() -> None:
        nonlocal reconciliations
        reconciliations += 1
        startup_complete.set()
        if reconciliations == 2:
            reconciled.set()

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    assert startup_complete.wait(timeout=1)
    for _ in range(100):
        scheduler.offer_dirty()

    assert reconciled.wait(timeout=1)
    scheduler.close()
    thread.join(timeout=1)
    assert reconciliations == 2


def test_degraded_generation_gets_one_retry_then_blocks() -> None:
    scheduler = AnalyticsEventScheduler(
        quiet_seconds=0.01,
        max_batch_seconds=0.05,
        one_shot_retry_seconds=0.01,
    )
    twice = Event()
    third = Event()
    reconciliations = 0

    def reconcile() -> str:
        nonlocal reconciliations
        reconciliations += 1
        if reconciliations == 2:
            twice.set()
        if reconciliations == 3:
            third.set()
        return "degraded"

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    assert twice.wait(timeout=1)
    assert not third.wait(timeout=0.05)

    assert reconciliations == 2
    scheduler.close()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_new_dirty_generation_restores_one_retry() -> None:
    scheduler = AnalyticsEventScheduler(
        quiet_seconds=0.01,
        max_batch_seconds=0.05,
        one_shot_retry_seconds=0.01,
    )
    twice = Event()
    fourth = Event()
    reconciliations = 0

    def reconcile() -> str:
        nonlocal reconciliations
        reconciliations += 1
        if reconciliations == 2:
            twice.set()
        if reconciliations == 4:
            fourth.set()
        return "degraded"

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    assert twice.wait(timeout=1)
    scheduler.offer_dirty()

    assert fourth.wait(timeout=1)
    scheduler.close()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_subscriber_start_delivers_ready_snapshot_before_return(
    tmp_path: Path,
) -> None:
    event_socket = tmp_path / "events.sock"
    thread_id = "01a00654-f2bc-7a30-834a-a5f886a65f82"
    observed: list[dict[str, object]] = []
    scheduler = AnalyticsEventScheduler(event_observer=observed.append)
    tap = CodexProtocolEventTap(event_socket)
    tap.start()
    tap.publish(
        json.dumps(
            {
                "method": "thread/started",
                "params": {"thread": {"id": thread_id, "createdAt": 1_787_692_800}},
            }
        )
    )
    subscriber = AnalyticsProtocolEventSubscriber(event_socket, scheduler)
    try:
        subscriber.start()

        assert observed[-1]["method"] == "rodex/event-stream/ready"
        assert observed[-1]["params"] == {
            "activeTurns": {},
            "knownThreads": [{"id": thread_id, "createdAt": 1_787_692_800}],
        }
    finally:
        subscriber.close()
        tap.close()


def test_subscriber_start_reports_ready_snapshot_observer_failure(
    tmp_path: Path,
) -> None:
    event_socket = tmp_path / "events.sock"

    def fail_observer(_event: object) -> None:
        raise RuntimeError("observer failed")

    scheduler = AnalyticsEventScheduler(event_observer=fail_observer)
    tap = CodexProtocolEventTap(event_socket)
    tap.start()
    subscriber = AnalyticsProtocolEventSubscriber(event_socket, scheduler)
    try:
        with pytest.raises(AnalyticsEventStreamClosed, match="failed during startup"):
            subscriber.start()
    finally:
        subscriber.close()
        tap.close()


def test_burst_hard_deadline_never_moves_after_the_first_event() -> None:
    burst = AnalyticsBurstWindow.start(0.0, quiet_seconds=0.5, max_batch_seconds=5.0)

    for now in (0.5, 1.0, 2.0, 4.5, 5.0, 9.0):
        burst.observe(now)

    assert burst.deadline == 5.0


def test_only_authoritative_lifecycle_messages_mark_analytics_dirty() -> None:
    assert _is_relevant_protocol_event('{"method":"turn/completed","params":{}}')
    assert _is_relevant_protocol_event(b'{"method":"thread/started","params":{}}')
    assert not _is_relevant_protocol_event(
        '{"method":"item/agentMessage/delta","params":{}}'
    )
    assert not _is_relevant_protocol_event("not-json")
