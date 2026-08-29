from __future__ import annotations

import asyncio
import fcntl
import os
import shlex
import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest

import rodex.exact_turn_mutation as mutation_module
from rodex.agent_observer import (
    AgentObserverCoordinator,
    AgentObserverView,
    RodexAgentTraceSnapshot,
)
from rodex.analytics_source_catalog import AnalyticsSourceCatalog
from rodex.control import PromptDispatch
from rodex.exact_turn_mutation import ExactTurnMutationCoordinator
from rodex.protocol_proxy import CodexProtocolEventTap, ToolCallCounter
from rodex.status_animation import AsyncCommandResult, animate_status, status_frames
from rodex.tmux_status import (
    STATUS_CLAIM_PRIORITY_OPTION,
    STATUS_CLAIM_PUBLISHER_OPTION,
    STATUS_CLAIM_TOKEN_OPTION,
)
from rodex_registry import RodexSessionId

ROOT_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
CHILD_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f83")


def test_round3_supported_start_dispatches_under_the_runtime_transition_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutation must not race the same lock used for resume/replacement."""
    database = tmp_path / "registry.sqlite3"
    session_id = 7
    transition_identity = RodexSessionId(7)
    lock_path = database.parent / f".{database.name}.session-{transition_identity}.lock"
    lock_was_held = False
    runtime = object()
    control = type("Control", (), {"runtime_id": "runtime-7"})()

    class LockProbingControlClient:
        def _start_turn(self, *_args: object, **_kwargs: object) -> PromptDispatch:
            nonlocal lock_was_held
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lock_was_held = True
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            return PromptDispatch(
                "started",
                "turn-1",
                "dispatch-1",
                thread_id=str(ROOT_THREAD_ID),
                session_id=str(ROOT_THREAD_ID),
            )

    monkeypatch.setattr(
        mutation_module,
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        lambda *_args: session_id,
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_session_id_from_a_rodex_sessions_id",
        lambda *_args: transition_identity,
    )
    monkeypatch.setattr(
        mutation_module,
        "resolve_live_control",
        lambda *_args: (session_id, runtime, control),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_session_names",
        lambda *_args: type("Names", (), {"display_name": "round3"})(),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_runtime_instance",
        lambda *_args: type("Runtime", (), {"runtime_id": "runtime-7"})(),
    )

    target, _dispatch = ExactTurnMutationCoordinator(
        database,
        object(),  # type: ignore[arg-type]
        LockProbingControlClient(),  # type: ignore[arg-type]
    ).start("round3", "hello", dispatch_id=None)

    assert target.session_id == session_id
    assert lock_was_held, (
        "_start reached its mutating control operation outside the session transition "
        "lock used by resume and runtime replacement"
    )


class _AdmissionTmux:
    def __init__(self) -> None:
        self.options: dict[str, str] = {}
        self.commands: list[list[str]] = []

    async def __call__(self, command: Sequence[str]) -> AsyncCommandResult:
        recorded = list(command)
        self.commands.append(recorded)
        if "display-message" in recorded:
            return AsyncCommandResult(0, "2\n")
        if "show-options" in recorded:
            value = self.options.get(recorded[-1], "")
            return AsyncCommandResult(0 if value else 1, f"{value}\n")
        if "list-clients" in recorded:
            return AsyncCommandResult(0, "")
        if "if-shell" in recorded and self._condition_is_true(recorded[-2]):
            for tmux_command in recorded[-1].split(" ; "):
                self._apply(shlex.split(tmux_command))
        return AsyncCommandResult(0)

    def _condition_is_true(self, condition: str) -> bool:
        if condition.startswith("#{<=:"):
            current = int(self.options.get(STATUS_CLAIM_PRIORITY_OPTION, "0"))
            requested = int(condition.rsplit(",", maxsplit=1)[1].removesuffix("}"))
            return current <= requested
        expected = condition.rsplit(",", maxsplit=1)[1].removesuffix("}")
        if STATUS_CLAIM_TOKEN_OPTION in condition:
            return self.options.get(STATUS_CLAIM_TOKEN_OPTION) == expected
        if STATUS_CLAIM_PUBLISHER_OPTION in condition:
            return self.options.get(STATUS_CLAIM_PUBLISHER_OPTION) == expected
        raise AssertionError(f"unexpected tmux condition: {condition}")

    def _apply(self, command: list[str]) -> None:
        if command[:2] == ["set-option", "-u"]:
            self.options.pop(command[-1], None)
        elif command[:1] == ["set-option"]:
            self.options[command[-2]] = command[-1]


def test_round3_transition_renderer_has_one_worker_and_fixed_command_budget() -> None:
    """Native admission owns bursts; one admitted renderer has a fixed budget."""

    async def exercise_transition() -> tuple[int, int, int]:
        tmux = _AdmissionTmux()
        release_waiters = asyncio.Event()
        first_admitted = asyncio.Event()
        waiting = 0
        peak_waiting = 0
        wait_calls = 0

        async def hold_first_frame(_deadline: float) -> None:
            nonlocal peak_waiting, wait_calls, waiting
            waiting += 1
            wait_calls += 1
            peak_waiting = max(peak_waiting, waiting)
            first_admitted.set()
            try:
                await release_waiters.wait()
            finally:
                waiting -= 1

        task = asyncio.create_task(
            animate_status(
                "tmux",
                Path("/isolated/round3/tmux.sock"),
                "round3-session",
                "attached",
                runner=tmux,
                wait_until=hold_first_frame,
                token_factory=lambda: "round3-token",
            )
        )
        try:
            await asyncio.wait_for(first_admitted.wait(), timeout=2)
            for _ in range(10):
                await asyncio.sleep(0)
        finally:
            release_waiters.set()
            await task
        return peak_waiting, wait_calls, len(tmux.commands)

    peak_workers, frame_waits, command_count = asyncio.run(exercise_transition())

    assert peak_workers == 1
    assert frame_waits == len(status_frames("attached", 2))
    assert command_count <= 55


def test_round3_blocked_renderer_and_protocol_churn_stay_bounded() -> None:
    """One admitted renderer stays bounded during unrelated protocol churn."""

    async def exercise_burst() -> tuple[int, int, int, int]:
        active_runners = 0
        peak_runners = 0
        runner_calls = 0
        never_ready = asyncio.Event()

        async def blocked_runner(_command: Sequence[str]) -> AsyncCommandResult:
            nonlocal active_runners, peak_runners, runner_calls
            runner_calls += 1
            active_runners += 1
            peak_runners = max(peak_runners, active_runners)
            try:
                await never_ready.wait()
            finally:
                active_runners -= 1
            return AsyncCommandResult(0)

        task = asyncio.create_task(
            animate_status(
                "tmux",
                Path("/isolated/round3/blocked.sock"),
                "round3-blocked",
                "attached",
                runner=blocked_runner,
                command_timeout_seconds=0.01,
            )
        )
        await asyncio.sleep(0)
        tap = CodexProtocolEventTap(Path("/isolated/round3/events.sock"))
        counter = ToolCallCounter(lambda _count: None)
        for index in range(128):
            thread_id = f"burst-thread-{index}"
            tap.publish_protocol_event(
                "{}",
                {
                    "method": "turn/started",
                    "params": {"threadId": thread_id, "turn": {"id": "turn"}},
                },
            )
            tap.publish_protocol_event(
                "{}",
                {
                    "method": "turn/completed",
                    "params": {"threadId": thread_id, "turn": {"id": "turn"}},
                },
            )
            for method in ("item/started", "item/completed"):
                counter.observe_protocol_event(
                    {
                        "method": method,
                        "params": {
                            "item": {
                                "type": "commandExecution",
                                "id": f"burst-item-{index}",
                            }
                        },
                    }
                )
        await asyncio.wait_for(
            task,
            timeout=0.5,
        )
        return (
            peak_runners,
            runner_calls,
            len(tap._known_threads),
            len(counter._item_ids),
        )

    peak_runners, runner_calls, known_threads, active_tool_ids = asyncio.run(
        exercise_burst()
    )

    assert peak_runners == 1
    assert runner_calls <= 2
    assert known_threads == 0
    assert active_tool_ids == 0


def _spawn_event(
    *,
    method: str = "item/started",
    item_id: str = "call-spawn-1",
    child_thread_id: uuid.UUID = CHILD_THREAD_ID,
) -> dict[str, object]:
    return {
        "method": method,
        "params": {
            "threadId": str(ROOT_THREAD_ID),
            "turnId": "turn-1",
            "item": {
                "type": "subAgentActivity",
                "id": item_id,
                "agentPath": "/root/round3",
                "agentThreadId": str(child_thread_id),
                "kind": "started" if method == "item/started" else "completed",
            },
        },
    }


def test_round3_closed_observer_is_inert_to_a_late_proxy_callback(
    tmp_path: Path,
) -> None:
    """Closing a downstream observer must make queued proxy callbacks harmless."""
    tmux_calls: list[list[str]] = []

    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        tmux_calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "")

    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        tmp_path / "tmux.sock",
        "%7",
        tmp_path / "events.sock",
        runner=runner,
        cursor_reader=lambda *_args: None,
        event_sender=lambda *_args: None,
    )
    controller.activate(
        database_path=tmp_path / "registry.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )
    controller.close()

    controller.observe_protocol_event(_spawn_event())

    assert tmux_calls == []
    assert controller._observer_state.tracked_target_thread_ids == frozenset()
    assert controller._observer_state.active_events == ()


def test_round3_runtime_identity_state_is_bounded_after_terminal_events(
    tmp_path: Path,
) -> None:
    """Terminal runtime state is pruned; the durable source catalog remains complete."""
    event_count = 128
    tap = CodexProtocolEventTap(tmp_path / "unused-events.sock")
    catalog = AnalyticsSourceCatalog(tmp_path / "sessions")
    counter = ToolCallCounter(lambda _count: None)

    for index in range(event_count):
        thread_id = str(uuid.UUID(int=ROOT_THREAD_ID.int + index))
        thread_event = {
            "method": "thread/started",
            "params": {"thread": {"id": thread_id}},
        }
        tap.publish_protocol_event("{}", thread_event)
        catalog.observe_protocol_event(thread_event)
        tap.publish_protocol_event(
            "{}",
            {
                "method": "turn/started",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": f"turn-{index}"},
                },
            },
        )
        tap.publish_protocol_event(
            "{}",
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": f"turn-{index}"},
                },
            },
        )
        counter.observe_protocol_event(
            {
                "method": "item/started",
                "params": {"item": {"type": "commandExecution", "id": f"item-{index}"}},
            }
        )
        counter.observe_protocol_event(
            {
                "method": "item/completed",
                "params": {"item": {"type": "commandExecution", "id": f"item-{index}"}},
            }
        )

    assert tap._active_turns == {}
    assert tap._known_threads == {}
    assert len(catalog.candidate_thread_ids()) == event_count
    assert counter._item_ids == set()
    assert counter.count == event_count


def test_round3_primary_disconnect_prunes_connection_scoped_event_state(
    tmp_path: Path,
) -> None:
    tap = CodexProtocolEventTap(tmp_path / "unused-events.sock")
    tap.publish_protocol_event(
        "{}",
        {
            "method": "thread/started",
            "params": {"thread": {"id": str(ROOT_THREAD_ID), "createdAt": 17}},
        },
    )
    tap.publish_protocol_event(
        "{}",
        {
            "method": "turn/started",
            "params": {
                "threadId": str(ROOT_THREAD_ID),
                "turn": {"id": "turn-live"},
            },
        },
    )

    tap.reset_after_disconnect()

    assert tap._active_turns == {}
    assert tap._known_threads == {}


def test_round3_observer_controller_prunes_completed_activity_lifetimes(
    tmp_path: Path,
) -> None:
    def runner(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        operation = command[3]
        if operation == "show-options":
            return subprocess.CompletedProcess(command, 0, "%9\n", "")
        if operation == "display-message":
            return subprocess.CompletedProcess(command, 0, "%9|%7|0\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        tmp_path / "tmux.sock",
        "%7",
        tmp_path / "events.sock",
        runner=runner,
        cursor_reader=lambda *_args: None,
        event_sender=lambda *_args: None,
    )
    controller.activate(
        database_path=tmp_path / "registry.sqlite3",
        rodex_sessions_id=3,
        rodex_session_id="1234567890abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )

    for index in range(64):
        child_id = uuid.UUID(int=CHILD_THREAD_ID.int + index)
        controller.observe_protocol_event(
            _spawn_event(item_id=f"call-{index}", child_thread_id=child_id)
        )
        controller.observe_protocol_event(
            _spawn_event(
                method="item/completed",
                item_id=f"call-{index}",
                child_thread_id=child_id,
            )
        )

    assert controller._observer_state.tracked_target_thread_ids == frozenset()
    snapshot = controller._observer_state.snapshot()
    state = snapshot["state"]
    assert isinstance(state, dict)
    assert len(state["events"]) + len(state["tombstones"]) <= 64  # type: ignore[arg-type]
    controller.close()


def test_round3_observer_view_releases_a_flushed_terminal_turn() -> None:
    initial = {
        "schema": "rodex-agent-observer-v2",
        "kind": "app_server_subagent_activity",
        "method": "item/started",
        "thread_id": str(ROOT_THREAD_ID),
        "turn_id": "root-turn",
        "item": {
            "id": "call-1",
            "agent_thread_id": str(CHILD_THREAD_ID),
            "agent_path": "/root/round3",
            "activity_kind": "started",
        },
    }
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event=initial)
    view.accept_trace_snapshot(
        RodexAgentTraceSnapshot(
            trace_publication_sequence=1,
            trace_schema_version="rodex-agent-trace-v2",
            calculated_at_utc="2026-08-29T00:00:01Z",
            coverage_state="complete",
            durable_event_count=2,
            unrecognized_record_count=0,
            events=(
                {
                    "event_id": str(uuid.UUID(int=40_001)),
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "child-turn",
                    "event_kind": "turn_started",
                    "event_time_utc": "2026-08-29T00:00:00Z",
                    "detail": {},
                },
                {
                    "event_id": str(uuid.UUID(int=40_002)),
                    "codex_thread_id": str(CHILD_THREAD_ID),
                    "codex_turn_id": "child-turn",
                    "event_kind": "turn_completed",
                    "event_time_utc": "2026-08-29T00:00:01Z",
                    "detail": {},
                },
            ),
        )
    )

    assert view.flush_pending_terminal_events()
    view.accept_trace_publication_wake(2, caught_up=True)

    assert view._turn_presentations == {}
    assert view._activity_turn_keys == {}
    assert view._activity_items == {}
    assert view._seen_activity_item_ids == set()
    assert view._target_states == {}
    assert view._target_paths == {}
