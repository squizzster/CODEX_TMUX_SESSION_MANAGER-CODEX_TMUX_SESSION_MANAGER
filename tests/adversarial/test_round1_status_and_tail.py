from __future__ import annotations

import io
import shlex
import subprocess
import time
from pathlib import Path
from threading import Event, Thread, get_ident

import pytest

from rodex import protocol_proxy as protocol_proxy_module
from rodex.protocol_proxy import (
    ROLLOUT_CONTEXT_MAX_IDLE_POLL_INTERVAL_SECONDS,
    ROLLOUT_CONTEXT_POLL_INTERVAL_SECONDS,
    CodexContextStatusObserver,
    CodexProtocolProxy,
    ToolCallCounter,
)
from rodex.runtime import LiveTmuxSession, TmuxScrollbackSnapshot, TmuxScrollbackState
from rodex.session_tail import (
    TAIL_MAX_IDLE_POLL_INTERVAL_SECONDS,
    TAIL_POLL_INTERVAL_SECONDS,
    PlainTailCursor,
    SessionTailRequest,
    follow_session_tail,
)
from rodex.tmux_session_capability import TmuxRuntimeCapability
from rodex.tmux_status import TmuxStatusOption
from rodex_registry import RodexRuntimeId


def _runtime_capability(socket_path: Path) -> TmuxRuntimeCapability:
    return TmuxRuntimeCapability(
        socket_path,
        "0123456789abcdef0123456789abcdef",
        "$7",
        "%9",
        RodexRuntimeId.parse("0123456789abcdef"),
    )


def _compaction_event(method: str) -> dict[str, object]:
    return {
        "method": method,
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {"id": "compact-1", "type": "contextCompaction"},
        },
    }


def test_round1_compaction_resets_on_terminal_turn_when_completion_is_lost() -> None:
    observed: list[str] = []
    first_frame = Event()

    def publish(rendered_status: str) -> None:
        observed.append(rendered_status)
        if "COMPACTING" in rendered_status:
            first_frame.set()

    observer = CodexContextStatusObserver(
        publish,
        animation_interval_seconds=60,
    )
    observer.observe_protocol_event(_compaction_event("item/started"))
    try:
        assert first_frame.wait(1)
        observer.observe_protocol_event(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            }
        )
        observer.observe_rollout_context_percent("thread-1", 10.0)
        assert "Context: 10%" in observed[-1]
        assert "COMPACTING" not in observed[-1]
    finally:
        observer.close()


def test_round1_compaction_animation_has_an_internal_watchdog() -> None:
    class VirtualStop:
        def __init__(self) -> None:
            self.elapsed = 0.0
            self.hard_test_stop = 60.0

        def is_set(self) -> bool:
            return self.elapsed >= self.hard_test_stop

        def wait(self, interval: float) -> bool:
            self.elapsed += interval
            return self.is_set()

    observed: list[str] = []
    stop = VirtualStop()
    observer = CodexContextStatusObserver(observed.append, animation_interval_seconds=0.1)
    observer._animation_generation = 1
    observer._active_compaction_item_ids.add("compact-1")
    observer._monotonic = lambda: stop.elapsed

    observer._animate_compaction(1, stop)  # type: ignore[arg-type]

    assert stop.elapsed < stop.hard_test_stop
    assert len(observed) < 100


def test_round1_primary_disconnect_immediately_resets_compaction(
    tmp_path: Path,
) -> None:
    observed: list[str] = []
    first_frame = Event()
    disconnects = 0

    def publish(rendered_status: str) -> None:
        observed.append(rendered_status)
        if "COMPACTING" in rendered_status:
            first_frame.set()

    observer = CodexContextStatusObserver(publish, animation_interval_seconds=60)

    def reset_compaction() -> None:
        nonlocal disconnects
        disconnects += 1
        observer.reset_after_disconnect()

    proxy = CodexProtocolProxy(
        tmp_path / "proxy.sock",
        tmp_path / "app.sock",
        ToolCallCounter(lambda _count: None),
        on_primary_disconnect=reset_compaction,
    )
    connection = object()
    assert proxy._claim_primary_connection(connection)
    observer.observe_protocol_event(_compaction_event("item/started"))
    try:
        assert first_frame.wait(1)
        proxy._release_primary_connection(connection)
        proxy._release_primary_connection(connection)
        observer.observe_rollout_context_percent("thread-1", 10.0)

        assert disconnects == 1
        assert "Context: 10%" in observed[-1]
        assert "COMPACTING" not in observed[-1]
    finally:
        observer.close()


def test_round1_primary_disconnect_replaces_thread_and_rollout_follower(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    old_rollout = sessions_root / "2026/08/29/rollout-old-thread-old.jsonl"
    new_rollout = sessions_root / "2026/08/29/rollout-new-thread-new.jsonl"
    old_rollout.parent.mkdir(parents=True)
    old_rollout.write_text("", encoding="utf-8")
    new_rollout.write_text("", encoding="utf-8")
    observed: list[str] = []
    observer = CodexContextStatusObserver(
        observed.append,
        codex_sessions_root=sessions_root,
        rollout_poll_interval_seconds=0.01,
    )
    proxy = CodexProtocolProxy(
        tmp_path / "proxy.sock",
        tmp_path / "app.sock",
        ToolCallCounter(lambda _count: None),
        on_primary_disconnect=observer.reset_after_disconnect,
    )
    old_connection = object()
    new_connection = object()
    assert proxy._claim_primary_connection(old_connection)
    observer.observe_protocol_event(
        {
            "method": "thread/started",
            "params": {"thread": {"id": "thread-old", "path": str(old_rollout)}},
        }
    )
    observer.observe_rollout_context_percent("thread-old", 10.0)
    with observer._lock:
        old_follower = observer._rollout_thread
    assert old_follower is not None
    assert old_follower.is_alive()

    try:
        proxy._release_primary_connection(old_connection)
        old_alive_after_disconnect = old_follower.is_alive()

        assert proxy._claim_primary_connection(new_connection)
        observer.observe_protocol_event(
            {
                "method": "thread/started",
                "params": {"thread": {"id": "thread-new", "path": str(new_rollout)}},
            }
        )
        observer.observe_rollout_context_percent("thread-new", 70.0)
        with observer._lock:
            accepted_thread_id = observer._primary_thread_id
            new_follower = observer._rollout_thread
    finally:
        observer.close()

    assert not old_alive_after_disconnect
    assert accepted_thread_id == "thread-new"
    assert new_follower is not None
    assert new_follower is not old_follower
    assert not new_follower.is_alive()
    assert "Context: 70%" in observed[-1]


def test_round1_twenty_idle_rollout_followers_have_bounded_metadata_only_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class VirtualMinuteComplete(Exception):
        pass

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    observer = CodexContextStatusObserver(
        lambda _status: None,
        codex_sessions_root=sessions_root,
    )
    real_fstat = protocol_proxy_module.os.fstat
    real_stat = protocol_proxy_module.os.stat
    real_boundary_hash = protocol_proxy_module._rollout_boundary_sha256
    fstat_calls = 0
    stat_calls = 0
    boundary_hash_calls = 0
    intervals_by_follower: list[tuple[float, ...]] = []

    def counted_fstat(descriptor: int) -> object:
        nonlocal fstat_calls
        fstat_calls += 1
        return real_fstat(descriptor)

    def counted_stat(path: object, **options: object) -> object:
        nonlocal stat_calls
        stat_calls += 1
        return real_stat(path, **options)  # type: ignore[arg-type]

    def counted_boundary_hash(descriptor: int, cursor_offset: int) -> str:
        nonlocal boundary_hash_calls
        boundary_hash_calls += 1
        return real_boundary_hash(descriptor, cursor_offset)

    monkeypatch.setattr(protocol_proxy_module.os, "fstat", counted_fstat)
    monkeypatch.setattr(protocol_proxy_module.os, "stat", counted_stat)
    monkeypatch.setattr(
        protocol_proxy_module,
        "_rollout_boundary_sha256",
        counted_boundary_hash,
    )

    for follower_index in range(20):
        rollout_path = sessions_root / f"rollout-idle-{follower_index}.jsonl"
        rollout_path.write_bytes(b"")
        elapsed = 0.0
        intervals: list[float] = []

        def advance_virtual_time(
            _stop: Event,
            wake_generation: int,
            interval: float,
            _intervals: list[float] = intervals,
        ) -> tuple[bool, int]:
            nonlocal elapsed
            _intervals.append(interval)
            elapsed += interval
            if elapsed >= 60:
                raise VirtualMinuteComplete
            return False, wake_generation

        monkeypatch.setattr(
            observer,
            "_wait_for_rollout_activity",
            advance_virtual_time,
        )
        with pytest.raises(VirtualMinuteComplete):
            observer._follow_rollout_context(
                f"thread-{follower_index}", rollout_path, Event()
            )
        intervals_by_follower.append(tuple(intervals))

    observer.close()

    assert boundary_hash_calls == 20
    assert fstat_calls <= 760
    assert stat_calls <= 700
    assert all(
        intervals[:4]
        == (
            ROLLOUT_CONTEXT_POLL_INTERVAL_SECONDS,
            ROLLOUT_CONTEXT_POLL_INTERVAL_SECONDS * 2,
            ROLLOUT_CONTEXT_POLL_INTERVAL_SECONDS * 4,
            ROLLOUT_CONTEXT_MAX_IDLE_POLL_INTERVAL_SECONDS,
        )
        for intervals in intervals_by_follower
    )
    assert all(
        max(intervals) == ROLLOUT_CONTEXT_MAX_IDLE_POLL_INTERVAL_SECONDS
        for intervals in intervals_by_follower
    )


@pytest.mark.parametrize("invalid_kind", ["missing", "outside_root"])
def test_round1_invalid_rollout_path_retries_with_bounded_idle_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    class RetryBudgetComplete(Exception):
        pass

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    rollout_path = (
        sessions_root / "missing.jsonl"
        if invalid_kind == "missing"
        else tmp_path / "outside.jsonl"
    )
    if invalid_kind == "outside_root":
        rollout_path.write_bytes(b"")
    observer = CodexContextStatusObserver(
        lambda _status: None,
        codex_sessions_root=sessions_root,
    )
    intervals: list[float] = []

    def exhaust_retry_budget(
        _stop: Event,
        wake_generation: int,
        interval: float,
    ) -> tuple[bool, int]:
        intervals.append(interval)
        if len(intervals) == 6:
            raise RetryBudgetComplete
        return False, wake_generation

    monkeypatch.setattr(
        observer,
        "_wait_for_rollout_activity",
        exhaust_retry_budget,
    )
    with pytest.raises(RetryBudgetComplete):
        observer._follow_rollout_context("thread-1", rollout_path, Event())
    observer.close()

    assert intervals == [
        ROLLOUT_CONTEXT_POLL_INTERVAL_SECONDS,
        ROLLOUT_CONTEXT_POLL_INTERVAL_SECONDS * 2,
        ROLLOUT_CONTEXT_POLL_INTERVAL_SECONDS * 4,
        ROLLOUT_CONTEXT_MAX_IDLE_POLL_INTERVAL_SECONDS,
        ROLLOUT_CONTEXT_MAX_IDLE_POLL_INTERVAL_SECONDS,
        ROLLOUT_CONTEXT_MAX_IDLE_POLL_INTERVAL_SECONDS,
    ]


def test_round1_protocol_event_wakes_an_idle_rollout_follower(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    rollout_path = sessions_root / "rollout-thread-1.jsonl"
    rollout_path.parent.mkdir()
    rollout_path.write_text(
        protocol_proxy_module.json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"total_tokens": 25_840},
                        "model_context_window": 258_400,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    restored = Event()
    advanced = Event()

    def record_status(status: str) -> None:
        restored.set() if "Context: 10%" in status else None
        advanced.set() if "Context: 70%" in status else None

    observer = CodexContextStatusObserver(
        record_status,
        codex_sessions_root=sessions_root,
        rollout_poll_interval_seconds=0.01,
        rollout_max_idle_poll_interval_seconds=0.5,
    )
    observer.observe_protocol_event(
        {
            "method": "thread/started",
            "params": {"thread": {"id": "thread-1", "path": str(rollout_path)}},
        }
    )
    try:
        assert restored.wait(1)
        time.sleep(0.4)
        with rollout_path.open("a", encoding="utf-8") as rollout:
            rollout.write(
                protocol_proxy_module.json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {"total_tokens": 180_880},
                                "model_context_window": 258_400,
                            },
                        },
                    }
                )
                + "\n"
            )
        started = time.monotonic()
        observer.observe_protocol_event(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "agentMessage", "id": "message-1"},
                },
            }
        )
        assert advanced.wait(0.3)
        assert time.monotonic() - started < 0.3
    finally:
        observer.close()


def test_round1_rollout_replacement_is_detected_within_the_idle_poll_ceiling(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    rollout_path = sessions_root / "rollout-thread-1.jsonl"
    replacement_path = sessions_root / "replacement.jsonl"
    rollout_path.parent.mkdir()

    def token_record(total_tokens: int) -> str:
        return (
            protocol_proxy_module.json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"total_tokens": total_tokens},
                            "model_context_window": 258_400,
                        },
                    },
                }
            )
            + "\n"
        )

    rollout_path.write_text(token_record(25_840), encoding="utf-8")
    restored = Event()
    replaced = Event()

    def record_status(status: str) -> None:
        restored.set() if "Context: 10%" in status else None
        replaced.set() if "Context: 90%" in status else None

    observer = CodexContextStatusObserver(
        record_status,
        codex_sessions_root=sessions_root,
        rollout_poll_interval_seconds=0.005,
        rollout_max_idle_poll_interval_seconds=0.05,
    )
    observer.observe_protocol_event(
        {
            "method": "thread/started",
            "params": {"thread": {"id": "thread-1", "path": str(rollout_path)}},
        }
    )
    try:
        assert restored.wait(1)
        time.sleep(0.1)
        replacement_path.write_text(token_record(232_560), encoding="utf-8")
        started = time.monotonic()
        replacement_path.replace(rollout_path)
        assert replaced.wait(0.3)
        assert time.monotonic() - started < 0.3
    finally:
        observer.close()


def test_round1_primary_reset_finishes_before_replacement_admission(
    tmp_path: Path,
) -> None:
    reset_entered = Event()
    allow_reset_to_finish = Event()
    replacement_claim_returned = Event()
    replacement_claims: list[bool] = []

    def reset_connection_state() -> None:
        reset_entered.set()
        assert allow_reset_to_finish.wait(2)

    proxy = CodexProtocolProxy(
        tmp_path / "proxy.sock",
        tmp_path / "app.sock",
        ToolCallCounter(lambda _count: None),
        on_primary_disconnect=reset_connection_state,
    )
    old_connection = object()
    new_connection = object()
    assert proxy._claim_primary_connection(old_connection)

    release_thread = Thread(
        target=proxy._release_primary_connection,
        args=(old_connection,),
    )

    def claim_replacement() -> None:
        replacement_claims.append(proxy._claim_primary_connection(new_connection))
        replacement_claim_returned.set()

    claim_thread = Thread(target=claim_replacement)
    release_thread.start()
    assert reset_entered.wait(1)
    claim_thread.start()
    try:
        assert not replacement_claim_returned.wait(0.05)
        assert not proxy._primary_connection_released.is_set()
    finally:
        allow_reset_to_finish.set()
        release_thread.join(2)
        claim_thread.join(2)

    assert not release_thread.is_alive()
    assert not claim_thread.is_alive()
    assert replacement_claims == [True]


def test_round1_status_publication_deduplicates_identical_values(tmp_path: Path) -> None:
    calls: list[str] = []
    first_call = Event()
    duplicate_call = Event()

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        calls.append(shlex.split(command[-1])[-1])
        if len(calls) == 1:
            first_call.set()
        else:
            duplicate_call.set()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    status = TmuxStatusOption(
        "tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%1",
        "@rodex_test_status",
        runner=runner,
    )
    status.publish("steady")
    assert first_call.wait(1)

    for _ in range(20):
        status.publish("steady")

    assert not duplicate_call.wait(0.05)
    assert calls == ["steady"]


def test_round1_blocked_tmux_status_runner_is_nonblocking_and_coalesces_latest(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    runner_threads: set[int] = set()
    first_runner_entered = Event()
    release_runner = Event()
    first_publish_returned = Event()
    latest_published = Event()

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        value = shlex.split(command[-1])[-1]
        calls.append(value)
        runner_threads.add(get_ident())
        if len(calls) == 1:
            first_runner_entered.set()
            assert release_runner.wait(2)
        if value == "latest":
            latest_published.set()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    status = TmuxStatusOption(
        "tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%1",
        "@rodex_test_status",
        runner=runner,
    )

    def publish_first() -> None:
        status.publish("first")
        first_publish_returned.set()

    producer = Thread(target=publish_first)
    producer.start()
    assert first_runner_entered.wait(1)
    returned_before_release = first_publish_returned.wait(0.1)

    status.publish("middle")
    status.publish("latest")
    release_runner.set()
    producer.join(2)
    assert not producer.is_alive()
    assert latest_published.wait(1)

    assert returned_before_release
    assert calls == ["first", "latest"]
    assert len(runner_threads) == 1


def test_round1_tmux_status_aba_coalescing_preserves_latest_desired_value(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    runner_threads: set[int] = set()
    first_runner_entered = Event()
    release_first_runner = Event()
    final_value_published = Event()

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        value = shlex.split(command[-1])[-1]
        calls.append(value)
        runner_threads.add(get_ident())
        if len(calls) == 1:
            first_runner_entered.set()
            assert release_first_runner.wait(2)
        else:
            final_value_published.set()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    status = TmuxStatusOption(
        "tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%1",
        "@rodex_test_status",
        runner=runner,
    )
    worker: Thread | None = None
    try:
        status.publish("A")
        assert first_runner_entered.wait(1)

        status.publish("B")
        status.publish("A")
        release_first_runner.set()

        assert final_value_published.wait(1)
        assert calls == ["A", "A"]
        assert len(runner_threads) == 1
    finally:
        release_first_runner.set()
        status.close()
        with status._condition:
            worker = status._worker
        if worker is not None:
            worker.join(1)

    assert worker is None or not worker.is_alive()


def test_round1_tmux_status_timeout_opens_a_bounded_latest_value_circuit(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    first_failed = Event()
    latest_published = Event()

    def runner(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        value = shlex.split(command[-1])[-1]
        calls.append(value)
        assert options["timeout"] == 0.01
        if len(calls) == 1:
            first_failed.set()
            raise subprocess.TimeoutExpired(command, options["timeout"])
        if value == "latest":
            latest_published.set()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    status = TmuxStatusOption(
        "tmux",
        _runtime_capability(tmp_path / "tmux.sock"),
        "%1",
        "@rodex_test_status",
        runner=runner,
        command_timeout_seconds=0.01,
        failure_backoff_seconds=0.1,
    )
    try:
        status.publish("first")
        assert first_failed.wait(1)
        status.publish("middle")
        status.publish("latest")

        assert not latest_published.wait(0.04)
        assert latest_published.wait(1)
        assert calls == ["first", "latest"]
    finally:
        status.close()


def test_round1_tail_history_count_disambiguates_consecutive_identical_append() -> None:
    cursor = PlainTailCursor(TmuxScrollbackSnapshot(("same",), 1))

    assert cursor.try_advance_state(
        TmuxScrollbackState(
            history_line_count=2,
            history_tail_lines=("same", "same"),
            visible_lines=(),
            runtime_identity="round1-runtime",
        )
    ) == ("same",)


def test_round1_tail_repeated_block_rollover_consumes_visible_rows_in_order() -> None:
    cursor = PlainTailCursor(
        TmuxScrollbackSnapshot(("same", "same", "same", "same", "next"), 3)
    )

    assert (
        cursor.try_advance_state(
            TmuxScrollbackState(
                history_line_count=3,
                history_tail_lines=("same", "same", "next"),
                visible_lines=("later",),
                runtime_identity="round1-runtime",
            )
        )
        == ()
    )
    assert cursor.try_advance_state(
        TmuxScrollbackState(
            history_line_count=3,
            history_tail_lines=("same", "next", "later"),
            visible_lines=("latest",),
            runtime_identity="round1-runtime",
        )
    ) == ("later",)


def test_round1_tail_count_growth_does_not_hide_rollover_at_history_limit() -> None:
    cursor = PlainTailCursor(TmuxScrollbackSnapshot(("same", "same", "same", "next"), 2))

    assert (
        cursor.try_advance_state(
            TmuxScrollbackState(
                history_line_count=3,
                history_tail_lines=("same", "same", "next"),
                visible_lines=("later",),
                runtime_identity="round1-runtime",
            )
        )
        == ()
    )


def test_round1_idle_full_history_tail_stays_within_process_and_byte_budgets() -> None:
    history = tuple(f"history-{index:05d}-" + "x" * 80 for index in range(50_000))
    snapshot = TmuxScrollbackSnapshot((*history, "prompt"), len(history))
    bytes_per_capture = sum(len(line.encode()) + 1 for line in snapshot.lines)
    state = TmuxScrollbackState(
        history_line_count=len(history),
        history_tail_lines=history[-256:],
        visible_lines=("prompt",),
        runtime_identity="round1-runtime",
    )
    bytes_per_state = sum(
        len(line.encode()) + 1 for line in (*state.history_tail_lines, *state.visible_lines)
    )
    process_calls = 0
    bytes_captured = 0
    ticks = 0
    intervals: list[float] = []

    def capture(_runtime: LiveTmuxSession) -> TmuxScrollbackSnapshot:
        nonlocal process_calls, bytes_captured
        process_calls += 1
        bytes_captured += bytes_per_capture
        return snapshot

    def capture_state(_runtime: LiveTmuxSession) -> TmuxScrollbackState:
        nonlocal process_calls, bytes_captured
        process_calls += 1
        bytes_captured += bytes_per_state
        return state

    def revalidate() -> None:
        nonlocal process_calls
        process_calls += 8

    def advance_idle_clock(interval: float) -> None:
        nonlocal ticks
        intervals.append(interval)
        ticks += 1
        if ticks > 8:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        follow_session_tail(
            SessionTailRequest("worker", 0),
            LiveTmuxSession(Path("/tmp/round1-tail.sock"), "worker"),
            capture,
            capture_state,
            revalidate,
            output=io.StringIO(),
            sleep=advance_idle_clock,
        )

    assert process_calls == 18
    assert bytes_captured == bytes_per_capture + bytes_per_state * 9
    assert intervals == [
        TAIL_POLL_INTERVAL_SECONDS,
        TAIL_POLL_INTERVAL_SECONDS * 2,
        TAIL_POLL_INTERVAL_SECONDS * 4,
        TAIL_MAX_IDLE_POLL_INTERVAL_SECONDS,
        TAIL_MAX_IDLE_POLL_INTERVAL_SECONDS,
        TAIL_MAX_IDLE_POLL_INTERVAL_SECONDS,
        TAIL_MAX_IDLE_POLL_INTERVAL_SECONDS,
        TAIL_MAX_IDLE_POLL_INTERVAL_SECONDS,
        TAIL_MAX_IDLE_POLL_INTERVAL_SECONDS,
    ]
