from __future__ import annotations

from threading import Event, Thread

from rodex.analytics_scheduler import (
    AnalyticsBurstWindow,
    AnalyticsEventScheduler,
    _is_relevant_protocol_event,
)


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
