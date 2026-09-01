from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path

import pytest

import rodex.tmux_executor as tmux_executor_module
from rodex.agent_observer import AgentObserverCoordinator, AgentObserverView
from rodex.observer_contract import OBSERVER_SNAPSHOT_EVENT_LIMIT
from rodex.observer_state import ObserverStateReducer
from rodex.primary_connection_lifecycle import (
    PrimaryConnectionLifecycleCoordinator,
)
from rodex.status_animation_admission import status_animation_admission_command
from rodex.tmux_executor import (
    DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    AsyncTmuxExecutor,
    SyncTmuxExecutor,
)
from rodex.tmux_session_capability import (
    RODEX_CODEX_SESSION_ID_OPTION,
    RODEX_INTERNAL_SESSION_ID_OPTION,
    RODEX_REGISTRATION_REGISTERED,
    RODEX_REGISTRATION_STATE_OPTION,
    RODEX_REGISTRY_ID_OPTION,
    RODEX_RUNTIME_ID_OPTION,
    RODEX_SESSION_ID_OPTION,
    RODEX_SHARED_TMUX_PROTOCOL,
    RODEX_SHARED_TMUX_PROTOCOL_OPTION,
    RODEX_SHARED_TMUX_SERVER_ID_OPTION,
    TmuxSessionCapability,
)
from rodex.tmux_sharing_coordinator import (
    RODEX_SHARING_ATTACHED_COUNT_OPTION,
    sharing_coordinator_hook_command,
)
from rodex_registry.identity import RodexRegistryId, RodexRuntimeId, RodexSessionId

ROOT_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")
CHILD_THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f83")


def _capability(socket_path: Path) -> TmuxSessionCapability:
    return TmuxSessionCapability(
        socket_path,
        "0123456789abcdef0123456789abcdef",
        "$7",
        "%9",
        RodexRuntimeId.parse("0123456789abcdef"),
        RodexSessionId.parse("1111111111111111"),
        RodexRegistryId.parse("2222222222222222"),
        7,
        ROOT_THREAD_ID,
    )


def _activity(*, item_id: str, target: uuid.UUID) -> dict[str, object]:
    return {
        "schema": "rodex-agent-observer-v2",
        "kind": "app_server_subagent_activity",
        "method": "item/started",
        "thread_id": str(ROOT_THREAD_ID),
        "turn_id": "root-turn",
        "item": {
            "type": "subAgentActivity",
            "id": item_id,
            "activity_kind": "started",
            "agent_thread_id": str(target),
            "agent_path": f"/root/{item_id}",
        },
    }


def test_architecture_c_admission_is_explicitly_capability_fenced() -> None:
    capability = _capability(Path("/isolated/tmux.sock"))
    admission = status_animation_admission_command(
        "/venv/bin/python",
        "/usr/bin/tmux",
        capability,
        "attached",
    )

    assert "-t '#{session_id}" not in admission
    assert "--tmux-session-id '#{session_id}" not in admission
    assert capability.pane_target in admission
    assert "--tmux-session-id" in admission
    assert "--expected-runtime-id" in admission
    assert "--expected-rodex-session-id" in admission
    assert "--expected-registry-id" in admission
    assert "--expected-internal-session-id" in admission
    assert "--expected-codex-session-id" in admission
    assert admission.count("--admitted") == 1
    assert "@rodex_status_animation_watchdog_token" in admission


def test_architecture_c_observer_tmux_boundary_is_absolutely_bounded() -> None:
    entered = threading.Event()
    release = threading.Event()
    options: list[dict[str, object]] = []

    def blocked_runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        options.append(dict(kwargs))
        entered.set()
        release.wait()
        return subprocess.CompletedProcess(command, 1, "", "")

    controller = AgentObserverCoordinator(
        "/usr/bin/tmux",
        _capability(Path("/isolated/tmux.sock")).runtime_capability,
        "%7",
        Path("/isolated/events.sock"),
        runner=blocked_runner,
        cursor_reader=lambda *_args: None,
        event_sender=lambda *_args: None,
    )
    controller.activate(
        database_path=Path("/isolated/database.sqlite3"),
        rodex_sessions_id=1,
        rodex_session_id="0123456789abcdef",
        root_thread_id=ROOT_THREAD_ID,
    )
    callback = threading.Thread(
        target=controller.observe_protocol_event,
        args=(
            {
                "method": "item/started",
                "params": {
                    "threadId": str(ROOT_THREAD_ID),
                    "turnId": "root-turn",
                    "item": {
                        "type": "subAgentActivity",
                        "id": "call",
                        "agentThreadId": str(CHILD_THREAD_ID),
                        "agentPath": "/root/child",
                        "kind": "started",
                    },
                },
            },
        ),
    )
    try:
        callback.start()
        assert entered.wait(1)
        assert options[0]["timeout"] == DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS
    finally:
        release.set()
        callback.join(timeout=1)
        controller.close()


def test_architecture_c_caught_up_target_cannot_reappear_in_later_snapshot() -> None:
    producer = ObserverStateReducer.producer()
    consumer = ObserverStateReducer.consumer()
    old_activity = _activity(item_id="finished", target=CHILD_THREAD_ID)

    first = consumer.consume_snapshot(producer.observe(old_activity))
    assert first.upserted_events == (old_activity,)

    terminal = consumer.consume_snapshot(producer.prune_target(str(CHILD_THREAD_ID)))
    assert terminal.upserted_events == ()
    assert terminal.removed_target_thread_ids == frozenset({str(CHILD_THREAD_ID)})

    other_target = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f84")
    unrelated = _activity(item_id="unrelated", target=other_target)
    later = consumer.consume_snapshot(producer.observe(unrelated))

    assert later.upserted_events == (unrelated,)
    assert all(
        event.get("item", {}).get("agent_thread_id") != str(CHILD_THREAD_ID)  # type: ignore[union-attr]
        for event in later.active_events
    )


def test_architecture_c_overflow_replaces_stale_presentation_state() -> None:
    producer = ObserverStateReducer.producer()
    view = AgentObserverView(root_thread_id=ROOT_THREAD_ID, initial_event={})
    targets: list[uuid.UUID] = []

    for index in range(OBSERVER_SNAPSHOT_EVENT_LIMIT + 1):
        target = uuid.UUID(int=CHILD_THREAD_ID.int + index)
        targets.append(target)
        snapshot = producer.observe(_activity(item_id=f"activity-{index}", target=target))
        for event in view.accept_observer_state_snapshot(snapshot):
            view.accept_app_server_event(event)

    assert len(view.target_thread_ids) == OBSERVER_SNAPSHOT_EVENT_LIMIT
    assert str(targets[0]) not in view.target_thread_ids
    assert view.target_thread_ids == frozenset(
        str(target) for target in targets[-OBSERVER_SNAPSHOT_EVENT_LIMIT:]
    )


def test_architecture_c_disconnect_attempts_every_reset_and_advances_epoch() -> None:
    calls: list[str] = []

    class Participant:
        def __init__(self, name: str, *, fails: bool = False) -> None:
            self.name = name
            self.fails = fails

        def reset_after_disconnect(self) -> None:
            calls.append(self.name)
            if self.fails:
                raise RuntimeError(self.name)

    coordinator = PrimaryConnectionLifecycleCoordinator(
        (
            Participant("context", fails=True),
            Participant("event-tap"),
            Participant("observer"),
        )
    )

    failures = coordinator.reset_after_disconnect()

    assert calls == ["context", "event-tap", "observer"]
    assert coordinator.epoch == 1
    assert [failure.participant_name for failure in failures] == ["context"]


def test_architecture_c_tmux_executor_has_one_run_entry_and_explicit_modes(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, dict(options)))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    executor = SyncTmuxExecutor("tmux", tmp_path / "tmux.sock", runner=runner)

    assert executor.run(("show-options",)).stdout == "ok\n"
    executor.run(("set-option", "@test", "value"), output="discard")
    executor.run(
        ("new-session", "-d", "probe"),
        environment={"PATH": "/usr/bin"},
    )
    executor.run(
        ("attach-session", "-t", "=exact"),
        mode="interactive",
        environment={"TERM": "xterm"},
    )

    assert {
        name
        for name, value in SyncTmuxExecutor.__dict__.items()
        if callable(value) and not name.startswith("_")
    } == {"run"}
    assert {
        name
        for name, value in AsyncTmuxExecutor.__dict__.items()
        if callable(value) and not name.startswith("_")
    } == {"run"}
    assert calls[0][1] == {
        "check": False,
        "text": True,
        "capture_output": True,
        "timeout": DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    }
    assert calls[1][1] == {
        "check": False,
        "text": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
    }
    assert calls[2][1] == {
        "check": False,
        "text": True,
        "capture_output": True,
        "timeout": DEFAULT_TMUX_COMMAND_TIMEOUT_SECONDS,
        "env": {"PATH": "/usr/bin"},
    }
    assert calls[3][1] == {
        "check": False,
        "text": True,
        "env": {"TERM": "xterm"},
    }


def test_architecture_c_async_tmux_timeout_kills_and_reaps_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed = False
    reaped = False

    class BlockedProcess:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def kill(self) -> None:
            nonlocal killed
            killed = True
            self.returncode = -9

        async def wait(self) -> int:
            nonlocal reaped
            reaped = True
            return -9

    async def create_process(*_args: object, **_kwargs: object) -> BlockedProcess:
        return BlockedProcess()

    monkeypatch.setattr(
        tmux_executor_module.asyncio,
        "create_subprocess_exec",
        create_process,
    )
    result = asyncio.run(
        AsyncTmuxExecutor(
            "tmux",
            tmp_path / "tmux.sock",
            timeout_seconds=0.01,
        ).run(("show-options",))
    )

    assert result.timed_out
    assert killed
    assert reaped


def test_architecture_c_tmux_process_execution_has_no_production_bypass() -> None:
    rodex_source = Path(__file__).parents[2] / "src" / "rodex"
    tmux_process_modules = (
        "tmux_shared_ctrl_c.py",
        "tmux_status.py",
        "status_animation.py",
        "status_animation_admission.py",
        "observer_pane.py",
        "agent_observer.py",
        "protocol_proxy.py",
    )

    for module_name in tmux_process_modules:
        source = (rodex_source / module_name).read_text(encoding="utf-8")
        assert "subprocess.run(" not in source, module_name
        assert "subprocess.Popen(" not in source, module_name
        assert "create_subprocess_exec(" not in source, module_name
    runtime_source = (rodex_source / "runtime.py").read_text(encoding="utf-8")
    assert "subprocess.run(" not in runtime_source
    executor_source = (rodex_source / "tmux_executor.py").read_text(encoding="utf-8")
    assert executor_source.count("create_subprocess_exec(") == 1


def test_shared_tmux_source_destruction_cannot_mutate_survivor_live(
    tmp_path: Path,
) -> None:
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is not installed")
    socket_path = tmp_path / "tmux.sock"
    server_id = "0123456789abcdef0123456789abcdef"
    clients: list[subprocess.Popen[bytes]] = []

    def run_tmux(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [tmux, "-S", str(socket_path), *arguments],
            check=True,
            text=True,
            capture_output=True,
        )

    def set_identity(
        session_name: str,
        *,
        runtime_id: str,
        rodex_session_id: str,
        internal_session_id: str,
        codex_session_id: str,
    ) -> None:
        target = f"={session_name}:"
        for option, value in (
            (RODEX_RUNTIME_ID_OPTION, runtime_id),
            (RODEX_REGISTRATION_STATE_OPTION, RODEX_REGISTRATION_REGISTERED),
            (RODEX_SESSION_ID_OPTION, rodex_session_id),
            (RODEX_REGISTRY_ID_OPTION, "2222222222222222"),
            (RODEX_INTERNAL_SESSION_ID_OPTION, internal_session_id),
            (RODEX_CODEX_SESSION_ID_OPTION, codex_session_id),
            (RODEX_SHARING_ATTACHED_COUNT_OPTION, "1"),
        ):
            run_tmux("set-option", "-t", target, option, value)

    run_tmux("new-session", "-d", "-s", "source", "sleep 30")
    try:
        run_tmux("new-session", "-d", "-s", "survivor", "sleep 30")
        run_tmux(
            "set-option",
            "-s",
            RODEX_SHARED_TMUX_PROTOCOL_OPTION,
            RODEX_SHARED_TMUX_PROTOCOL,
        )
        run_tmux("set-option", "-s", RODEX_SHARED_TMUX_SERVER_ID_OPTION, server_id)
        set_identity(
            "source",
            runtime_id="0123456789abcdef",
            rodex_session_id="1111111111111111",
            internal_session_id="1",
            codex_session_id="01a00654-f2bc-7a30-834a-a5f886a65f82",
        )
        set_identity(
            "survivor",
            runtime_id="1123456789abcdef",
            rodex_session_id="3111111111111111",
            internal_session_id="2",
            codex_session_id="01a00654-f2bc-7a30-834a-a5f886a65f83",
        )
        hook = sharing_coordinator_hook_command(
            str(Path(__file__).parents[2] / ".venv" / "bin" / "python"),
            tmux,
            socket_path,
            server_id,
        )
        for event in ("attached", "detached"):
            run_tmux("set-hook", "-g", f"client-{event}", hook)

        for session_name in ("source", "survivor"):
            clients.append(
                subprocess.Popen(
                    [
                        tmux,
                        "-S",
                        str(socket_path),
                        "-C",
                        "attach-session",
                        "-t",
                        f"={session_name}",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            counts = run_tmux(
                "list-sessions",
                "-F",
                "#{session_name}=#{session_attached}",
            ).stdout.splitlines()
            if set(counts) == {"source=1", "survivor=1"}:
                break
            time.sleep(0.02)
        assert set(counts) == {"source=1", "survivor=1"}

        run_tmux("kill-session", "-t", "=source")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if clients[0].poll() is not None:
                break
            time.sleep(0.02)
        time.sleep(0.3)

        survivor_state = run_tmux(
            "display-message",
            "-p",
            "-t",
            "=survivor:",
            "-F",
            (
                "#{session_attached}\t"
                f"#{{{RODEX_SHARING_ATTACHED_COUNT_OPTION}}}\t"
                "#{@rodex_status_animation_generation}\t"
                "#{@rodex_status_animation_pending_event}\t"
                "#{@rodex_status_animation_owner_token}\t"
                "#{@rodex_status_animation_watchdog_token}"
            ),
        ).stdout.rstrip("\n")
        assert survivor_state == "1\t1\t\t\t\t"
        assert clients[1].poll() is None
    finally:
        for client in clients:
            client.terminate()
            with suppress(subprocess.TimeoutExpired):
                client.wait(timeout=1)
        subprocess.run(
            [tmux, "-S", str(socket_path), "kill-server"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
