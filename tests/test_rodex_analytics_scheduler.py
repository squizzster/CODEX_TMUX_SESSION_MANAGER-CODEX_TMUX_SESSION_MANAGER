from __future__ import annotations

import json
import queue
import uuid
from pathlib import Path
from threading import Event, Thread

import pytest

from rodex.analytics_scheduler import (
    AnalyticsBurstWindow,
    AnalyticsDirtyBatch,
    AnalyticsEventScheduler,
    AnalyticsEventStreamClosed,
    AnalyticsProtocolEventSubscriber,
    _is_relevant_protocol_event,
)
from rodex.protocol_proxy import CodexProtocolEventTap

THREAD_ID = "01a00654-f2bc-7a30-834a-a5f886a65f82"
SECOND_THREAD_ID = "01a00654-f2bc-7a30-834a-a5f886a65f83"


def test_empty_scheduler_blocks_without_repeated_reconciliation() -> None:
    scheduler = AnalyticsEventScheduler(quiet_seconds=0.01, max_batch_seconds=0.05)
    startup_complete = Event()
    reconciliations = 0

    def reconcile(_batch: AnalyticsDirtyBatch) -> None:
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

    def reconcile(_batch: AnalyticsDirtyBatch) -> None:
        nonlocal reconciliations
        reconciliations += 1
        startup_complete.set()
        if reconciliations == 2:
            reconciled.set()

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    assert startup_complete.wait(timeout=1)
    for _ in range(100):
        scheduler.offer_dirty(THREAD_ID)

    assert reconciled.wait(timeout=1)
    scheduler.close()
    thread.join(timeout=1)
    assert reconciliations == 2


def test_publication_retry_repeats_only_inside_bounded_window_then_blocks() -> None:
    scheduler = AnalyticsEventScheduler(
        quiet_seconds=0.01,
        max_batch_seconds=0.05,
        retry_initial_seconds=0.01,
        max_retry_window_seconds=0.04,
    )
    retried = Event()
    reconciliations = 0

    def reconcile(_batch: AnalyticsDirtyBatch) -> str:
        nonlocal reconciliations
        reconciliations += 1
        if reconciliations == 2:
            retried.set()
        return "publication_retry"

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    assert retried.wait(timeout=1)
    thread.join(timeout=0.1)
    count_after_window = reconciliations
    assert count_after_window >= 3
    Event().wait(0.05)

    assert reconciliations == count_after_window
    scheduler.close()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_degraded_generation_parks_without_a_timed_clean_replay() -> None:
    scheduler = AnalyticsEventScheduler(
        quiet_seconds=0.01,
        max_batch_seconds=0.05,
        retry_initial_seconds=0.01,
        max_retry_window_seconds=0.04,
    )
    reconciled = Event()
    reconciliations = 0

    def reconcile(_batch: AnalyticsDirtyBatch) -> str:
        nonlocal reconciliations
        reconciliations += 1
        reconciled.set()
        return "degraded"

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    assert reconciled.wait(timeout=1)
    Event().wait(0.06)

    assert reconciliations == 1
    scheduler.close()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_queued_terminal_preempts_due_reconciliation_work() -> None:
    scheduler = AnalyticsEventScheduler(
        quiet_seconds=0,
        max_batch_seconds=0.05,
        retry_initial_seconds=0.01,
        max_retry_window_seconds=0.04,
    )
    reconciliations = 0

    def reconcile(_batch: AnalyticsDirtyBatch) -> str:
        nonlocal reconciliations
        reconciliations += 1
        scheduler.offer_dirty(THREAD_ID)
        scheduler.close()
        return "catching_up"

    scheduler.run(reconcile)

    assert reconciliations == 1


def test_catching_up_generation_can_resolve_after_its_first_retry() -> None:
    scheduler = AnalyticsEventScheduler(
        quiet_seconds=0.01,
        max_batch_seconds=0.05,
        retry_initial_seconds=0.01,
        max_retry_window_seconds=0.1,
    )
    startup_complete = Event()
    resolved = Event()
    batches: list[AnalyticsDirtyBatch] = []
    catch_up_attempts = 0

    def reconcile(batch: AnalyticsDirtyBatch) -> str:
        nonlocal catch_up_attempts
        batches.append(batch)
        if batch.full_reconcile:
            startup_complete.set()
            return "up_to_date"
        catch_up_attempts += 1
        if catch_up_attempts < 3:
            return "catching_up"
        resolved.set()
        return "up_to_date"

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    assert startup_complete.wait(timeout=1)
    scheduler.offer_dirty(THREAD_ID)

    assert resolved.wait(timeout=1)
    scheduler.close()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert batches[1].thread_ids == frozenset({uuid.UUID(THREAD_ID)})
    assert batches[2:] == [
        AnalyticsDirtyBatch(frozenset()),
        AnalyticsDirtyBatch(frozenset()),
    ]


def test_new_dirty_generation_restores_a_bounded_retry_window() -> None:
    scheduler = AnalyticsEventScheduler(
        quiet_seconds=0.01,
        max_batch_seconds=0.05,
        retry_initial_seconds=0.01,
        max_retry_window_seconds=0.02,
    )
    twice = Event()
    fourth = Event()
    reconciliations = 0

    def reconcile(_batch: AnalyticsDirtyBatch) -> str:
        nonlocal reconciliations
        reconciliations += 1
        if reconciliations == 2:
            twice.set()
        if reconciliations == 4:
            fourth.set()
        return "publication_retry"

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    assert twice.wait(timeout=1)
    scheduler.offer_dirty(THREAD_ID)

    assert fourth.wait(timeout=1)
    scheduler.close()
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_due_dirty_generation_preempts_an_older_retry_window() -> None:
    scheduler = AnalyticsEventScheduler(
        quiet_seconds=0.005,
        max_batch_seconds=0.02,
        retry_initial_seconds=0.05,
        max_retry_window_seconds=0.1,
    )
    startup_complete = Event()
    dirty_reconciled = Event()
    batches: list[AnalyticsDirtyBatch] = []

    def reconcile(batch: AnalyticsDirtyBatch) -> str:
        batches.append(batch)
        if batch.full_reconcile:
            startup_complete.set()
            return "catching_up"
        if batch.thread_ids:
            dirty_reconciled.set()
            return "up_to_date"
        return "catching_up"

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    assert startup_complete.wait(timeout=1)
    scheduler.offer_dirty(THREAD_ID)

    assert dirty_reconciled.wait(timeout=0.04)
    scheduler.close()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert batches == [
        AnalyticsDirtyBatch(frozenset(), full_reconcile=True),
        AnalyticsDirtyBatch(frozenset({uuid.UUID(THREAD_ID)})),
    ]


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


def test_dirty_quiet_deadline_never_regresses_between_producers() -> None:
    observed_times = iter((2.0, 1.0))
    scheduler = AnalyticsEventScheduler(
        quiet_seconds=0.5,
        max_batch_seconds=5.0,
        monotonic=lambda: next(observed_times),
    )

    scheduler.offer_dirty(THREAD_ID)
    scheduler.offer_dirty(SECOND_THREAD_ID)

    assert scheduler._pending_deadline() == 2.5


def test_continuously_ready_queue_cannot_starve_the_hard_batch_deadline() -> None:
    clock = [0.0]

    class ContinuouslyReadyQueue:
        def put_nowait(self, _signal: object) -> None:
            return

        def get_nowait(self) -> object:
            raise queue.Empty

        def get(self, timeout: float | None = None) -> object:
            clock[0] += 0.01
            scheduler.offer_dirty(THREAD_ID)
            return object()

    scheduler = AnalyticsEventScheduler(
        quiet_seconds=0.5,
        max_batch_seconds=5.0,
        monotonic=lambda: clock[0],
    )
    scheduler._signals = ContinuouslyReadyQueue()  # type: ignore[assignment]
    scheduler.offer_dirty(THREAD_ID)
    reconciliations = 0

    class HardDeadlineObserved(Exception):
        pass

    def reconcile(_batch: AnalyticsDirtyBatch) -> None:
        nonlocal reconciliations
        reconciliations += 1
        if reconciliations == 2:
            raise HardDeadlineObserved

    with pytest.raises(HardDeadlineObserved):
        scheduler.run(reconcile)

    assert 5.0 <= clock[0] <= 5.02


def test_dirty_identities_are_lossless_while_wake_queue_is_full() -> None:
    scheduler = AnalyticsEventScheduler(quiet_seconds=0.01, max_batch_seconds=0.05)
    reconciled = Event()
    batches: list[AnalyticsDirtyBatch] = []

    def reconcile(batch: AnalyticsDirtyBatch) -> None:
        batches.append(batch)
        if not batch.full_reconcile:
            reconciled.set()

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    for _ in range(100):
        scheduler.offer_dirty(THREAD_ID)
    scheduler.offer_dirty(SECOND_THREAD_ID)

    assert reconciled.wait(timeout=1)
    scheduler.close()
    thread.join(timeout=1)
    assert batches[0] == AnalyticsDirtyBatch(frozenset(), full_reconcile=True)
    assert batches[1].thread_ids == frozenset(
        {uuid.UUID(THREAD_ID), uuid.UUID(SECOND_THREAD_ID)}
    )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            {"method": "thread/started", "params": {"thread": {"id": THREAD_ID}}},
            THREAD_ID,
        ),
        (
            {"method": "turn/completed", "params": {"threadId": THREAD_ID}},
            THREAD_ID,
        ),
    ],
)
def test_lifecycle_events_retain_their_exact_dirty_identity(
    event: dict[str, object], expected: str
) -> None:
    scheduler = AnalyticsEventScheduler(quiet_seconds=0.01, max_batch_seconds=0.05)
    reconciled = Event()
    batches: list[AnalyticsDirtyBatch] = []

    def reconcile(batch: AnalyticsDirtyBatch) -> None:
        batches.append(batch)
        if not batch.full_reconcile:
            reconciled.set()

    thread = Thread(target=scheduler.run, args=(reconcile,))
    thread.start()
    scheduler.offer_protocol_event(event)

    assert reconciled.wait(timeout=1)
    scheduler.close()
    thread.join(timeout=1)
    assert batches[1].thread_ids == frozenset({uuid.UUID(expected)})


def test_only_authoritative_lifecycle_messages_mark_analytics_dirty() -> None:
    assert _is_relevant_protocol_event('{"method":"turn/completed","params":{}}')
    assert _is_relevant_protocol_event(b'{"method":"thread/started","params":{}}')
    assert not _is_relevant_protocol_event(
        '{"method":"item/agentMessage/delta","params":{}}'
    )
    assert not _is_relevant_protocol_event("not-json")
