from __future__ import annotations

import fcntl
import os
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import rodex.exact_turn_mutation as mutation_module
from rodex.control import LiveRodexControl
from rodex.errors import RodexLaunchError
from rodex.exact_turn_mutation import ExactTurnMutationCoordinator
from rodex.runtime import LiveTmuxSession
from rodex_registry import RodexSessionId


@pytest.fixture(autouse=True)
def stable_session_transition_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep coordinator unit doubles on the production immutable lock identity."""
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_session_id_from_a_rodex_sessions_id",
        lambda session_id, _database: RodexSessionId(session_id),
    )


class _NoFrameControlClient:
    def __init__(self) -> None:
        self.frames = 0

    def _start_turn(self, *_args: object, **_kwargs: object) -> object:
        self.frames += 1
        raise AssertionError("a stale target emitted a control frame")


class _BarrierControlClient(_NoFrameControlClient):
    def __init__(self, cross_barrier: Callable[[], None]) -> None:
        super().__init__()
        self._cross_barrier = cross_barrier

    def _start_turn(self, *_args: object, **kwargs: object) -> object:
        self._cross_barrier()
        revalidate = kwargs["revalidate"]
        assert callable(revalidate)
        revalidate()
        self.frames += 1
        raise AssertionError("a stale target emitted a control frame")


def test_start_fails_before_transport_when_selector_changes_while_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = {"worker": 1}

    @contextmanager
    def remapping_lock(_database: Path, _session_id: int):
        mapping["worker"] = 2
        yield

    monkeypatch.setattr(
        mutation_module,
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        lambda selector, _database: mapping.get(selector),
    )
    monkeypatch.setattr(mutation_module, "session_transition_lock", remapping_lock)
    monkeypatch.setattr(
        mutation_module,
        "resolve_live_control",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("stale selector must fail before live control discovery")
        ),
    )
    control = _NoFrameControlClient()

    with pytest.raises(RodexLaunchError, match="changed while waiting"):
        ExactTurnMutationCoordinator(
            tmp_path / "registry.sqlite3",
            object(),
            control,  # type: ignore[arg-type]
        ).start("worker", "hello", dispatch_id="dispatch")

    assert control.frames == 0


def test_start_fails_before_transport_when_runtime_changes_while_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_control = SimpleNamespace(runtime_id="old-runtime")
    runtime = object()

    @contextmanager
    def replacing_lock(_database: Path, _session_id: int):
        yield

    monkeypatch.setattr(
        mutation_module,
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        lambda *_args: 1,
    )
    monkeypatch.setattr(mutation_module, "session_transition_lock", replacing_lock)
    monkeypatch.setattr(
        mutation_module,
        "resolve_live_control",
        lambda *_args: (1, runtime, old_control),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_runtime_instance",
        lambda *_args: SimpleNamespace(runtime_id="new-runtime"),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_session_names",
        lambda *_args: SimpleNamespace(display_name="worker"),
    )
    control = _NoFrameControlClient()

    with pytest.raises(RodexLaunchError, match="runtime ID"):
        ExactTurnMutationCoordinator(
            tmp_path / "registry.sqlite3",
            object(),
            control,  # type: ignore[arg-type]
        ).start("worker", "hello", dispatch_id="dispatch")

    assert control.frames == 0


def test_mouse_fails_before_tmux_when_selector_changes_while_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = {"worker": 1}
    mouse_calls: list[tuple[object, str]] = []

    @contextmanager
    def remapping_lock(_database: Path, _session_id: int):
        mapping["worker"] = 2
        yield

    class Launcher:
        def set_mouse_mode(self, runtime: object, mode: str) -> str:
            mouse_calls.append((runtime, mode))
            return "on"

    monkeypatch.setattr(
        mutation_module,
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        lambda selector, _database: mapping.get(selector),
    )
    monkeypatch.setattr(mutation_module, "session_transition_lock", remapping_lock)
    monkeypatch.setattr(
        mutation_module,
        "resolve_live_control",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("stale selector must fail before live control discovery")
        ),
    )

    with pytest.raises(RodexLaunchError, match="changed while waiting"):
        ExactTurnMutationCoordinator(
            tmp_path / "registry.sqlite3",
            Launcher(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        ).mouse_mode("worker", "on")

    assert mouse_calls == []


def test_mouse_fails_before_tmux_when_durable_runtime_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = LiveTmuxSession(tmp_path / "tmux.sock", "worker")
    live_control = SimpleNamespace(runtime_id="old-runtime")
    mouse_calls: list[tuple[object, str]] = []

    @contextmanager
    def lock(_database: Path, _session_id: int):
        yield

    class Launcher:
        def set_mouse_mode(self, observed: object, mode: str) -> str:
            mouse_calls.append((observed, mode))
            return "on"

    monkeypatch.setattr(
        mutation_module,
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        lambda *_args: 1,
    )
    monkeypatch.setattr(mutation_module, "session_transition_lock", lock)
    monkeypatch.setattr(
        mutation_module,
        "resolve_live_control",
        lambda *_args: (1, runtime, live_control),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_runtime_instance",
        lambda *_args: SimpleNamespace(runtime_id="replacement-runtime"),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_session_names",
        lambda *_args: SimpleNamespace(display_name="worker"),
    )

    with pytest.raises(RodexLaunchError, match="runtime ID"):
        ExactTurnMutationCoordinator(
            tmp_path / "registry.sqlite3",
            Launcher(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        ).mouse_mode("worker", "on")

    assert mouse_calls == []


def test_mouse_holds_transition_lock_through_mutation_and_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "registry.sqlite3"
    runtime = LiveTmuxSession(tmp_path / "tmux.sock", "worker")
    live_control = SimpleNamespace(runtime_id="runtime")
    revalidations: list[tuple[object, object, object]] = []

    class Launcher:
        def set_mouse_mode(self, observed: LiveTmuxSession, mode: str) -> str:
            lock_path = (
                database.parent / f".{database.name}.session-{RodexSessionId(1)}.lock"
            )
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            assert observed.runtime_id == live_control.runtime_id
            assert mode == "toggle"
            return "on"

    monkeypatch.setattr(
        mutation_module,
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        lambda *_args: 1,
    )
    monkeypatch.setattr(
        mutation_module,
        "resolve_live_control",
        lambda *_args: (1, runtime, live_control),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_runtime_instance",
        lambda *_args: SimpleNamespace(runtime_id="runtime"),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_session_names",
        lambda *_args: SimpleNamespace(display_name="worker"),
    )
    monkeypatch.setattr(
        mutation_module,
        "revalidate_live_control",
        lambda *args: revalidations.append(args),
    )

    launcher = Launcher()
    target, state = ExactTurnMutationCoordinator(
        database,
        launcher,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    ).mouse_mode("worker", "toggle")

    assert state == "on"
    assert target.runtime.runtime_id == live_control.runtime_id
    assert revalidations == [(launcher, target.runtime, live_control)]


def test_mouse_revalidates_selector_after_tmux_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = {"worker": 1}
    runtime = LiveTmuxSession(tmp_path / "tmux.sock", "worker")
    live_control = SimpleNamespace(runtime_id="runtime")

    @contextmanager
    def lock(_database: Path, _session_id: int):
        yield

    class Launcher:
        def set_mouse_mode(self, _runtime: LiveTmuxSession, _mode: str) -> str:
            mapping["worker"] = 2
            return "on"

    monkeypatch.setattr(
        mutation_module,
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        lambda selector, _database: mapping.get(selector),
    )
    monkeypatch.setattr(mutation_module, "session_transition_lock", lock)
    monkeypatch.setattr(
        mutation_module,
        "resolve_live_control",
        lambda *_args: (1, runtime, live_control),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_runtime_instance",
        lambda *_args: SimpleNamespace(runtime_id="runtime"),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_session_names",
        lambda *_args: SimpleNamespace(display_name="worker"),
    )

    with pytest.raises(RodexLaunchError, match="selector changed"):
        ExactTurnMutationCoordinator(
            tmp_path / "registry.sqlite3",
            Launcher(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        ).mouse_mode("worker", "on")


def test_start_revalidates_selector_after_transport_wait_before_first_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = {"worker": 1}
    runtime = object()
    live_control = SimpleNamespace(runtime_id="runtime")

    @contextmanager
    def lock(_database: Path, _session_id: int):
        yield

    monkeypatch.setattr(
        mutation_module,
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        lambda selector, _database: mapping.get(selector),
    )
    monkeypatch.setattr(mutation_module, "session_transition_lock", lock)
    monkeypatch.setattr(
        mutation_module,
        "resolve_live_control",
        lambda *_args: (1, runtime, live_control),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_runtime_instance",
        lambda *_args: SimpleNamespace(runtime_id="runtime"),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_session_names",
        lambda *_args: SimpleNamespace(display_name="worker"),
    )
    control = _BarrierControlClient(lambda: mapping.__setitem__("worker", 2))

    with pytest.raises(RodexLaunchError, match="selector changed"):
        ExactTurnMutationCoordinator(
            tmp_path / "registry.sqlite3",
            object(),
            control,  # type: ignore[arg-type]
        ).start("worker", "hello", dispatch_id="dispatch")

    assert control.frames == 0


def test_start_revalidates_runtime_after_transport_wait_before_first_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted = {"runtime_id": "runtime"}
    runtime = object()
    live_control = SimpleNamespace(runtime_id="runtime")

    @contextmanager
    def lock(_database: Path, _session_id: int):
        yield

    monkeypatch.setattr(
        mutation_module,
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        lambda *_args: 1,
    )
    monkeypatch.setattr(mutation_module, "session_transition_lock", lock)
    monkeypatch.setattr(
        mutation_module,
        "resolve_live_control",
        lambda *_args: (1, runtime, live_control),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_runtime_instance",
        lambda *_args: SimpleNamespace(runtime_id=persisted["runtime_id"]),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_session_names",
        lambda *_args: SimpleNamespace(display_name="worker"),
    )
    monkeypatch.setattr(mutation_module, "revalidate_live_control", lambda *_args: None)
    control = _BarrierControlClient(
        lambda: persisted.__setitem__("runtime_id", "replacement")
    )

    with pytest.raises(RodexLaunchError, match="runtime ID"):
        ExactTurnMutationCoordinator(
            tmp_path / "registry.sqlite3",
            object(),
            control,  # type: ignore[arg-type]
        ).start("worker", "hello", dispatch_id="dispatch")

    assert control.frames == 0


def test_alias_transition_holds_the_lock_and_steers_the_observed_active_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "registry.sqlite3"
    runtime = LiveTmuxSession(tmp_path / "tmux.sock", "worker")
    live_control = LiveRodexControl(
        tmp_path / "proxy.sock",
        tmp_path / "events.sock",
        uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
        rodex_session_id="rodex-session",
        runtime_id="runtime",
    )
    lock_was_held = False

    @contextmanager
    def assignment(*_args: object, **_kwargs: object):
        nonlocal lock_was_held
        lock_path = database.parent / f".{database.name}.session-{RodexSessionId(1)}.lock"
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                lock_was_held = True
            yield SimpleNamespace(
                names=SimpleNamespace(display_name="renamed"),
                tmux_session=object(),
                renamed_tmux_session_name=None,
            )
        finally:
            os.close(descriptor)

    class Launcher:
        def session_exists(self, observed: LiveTmuxSession) -> bool:
            return observed == runtime

        def rename(
            self, observed: LiveTmuxSession, tmux_session_name: str
        ) -> LiveTmuxSession:
            assert observed == runtime
            return LiveTmuxSession(observed.tmux_server_socket_path, tmux_session_name)

        def initialise_session_ui(self, _runtime: LiveTmuxSession) -> None:
            return None

        def refresh_name_bound_hooks(self, _runtime: LiveTmuxSession) -> None:
            return None

    class ActiveControlClient:
        def __init__(self) -> None:
            self.started = 0
            self.steered: list[tuple[object, str, str]] = []

        def inspect_live(self, observed: object) -> SimpleNamespace:
            assert observed is live_control
            return SimpleNamespace(status="active", active_turn_id="turn-active")

        def _start_turn(self, *_args: object, **_kwargs: object) -> object:
            self.started += 1
            raise AssertionError("active alias information must not start another turn")

        def _steer_turn(
            self,
            observed: object,
            turn_id: str,
            prompt: str,
            *,
            revalidate: Callable[[], None],
        ) -> object:
            revalidate()
            self.steered.append((observed, turn_id, prompt))
            return object()

    monkeypatch.setattr(
        mutation_module,
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        lambda *_args: 1,
    )
    monkeypatch.setattr(
        mutation_module,
        "open_a_user_defined_cool_name_assignment",
        assignment,
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_tmux_session",
        lambda *_args: SimpleNamespace(
            tmux_server_socket_path=runtime.tmux_server_socket_path,
            tmux_session_name=runtime.tmux_session_name,
        ),
    )
    monkeypatch.setattr(
        mutation_module,
        "resolve_live_control",
        lambda *_args: (1, runtime, live_control),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_session_names",
        lambda *_args: SimpleNamespace(display_name="worker"),
    )
    monkeypatch.setattr(
        mutation_module,
        "lookup_rodex_runtime_instance",
        lambda *_args: SimpleNamespace(runtime_id="runtime"),
    )
    monkeypatch.setattr(mutation_module, "revalidate_live_control", lambda *_args: None)
    client = ActiveControlClient()
    display_name = ExactTurnMutationCoordinator(
        database,
        Launcher(),  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
    ).alias_transition("worker", "renamed", force=False)

    assert display_name == "renamed"
    assert lock_was_held
    assert client.started == 0
    assert client.steered == [
        (
            live_control,
            "turn-active",
            "RODEX_AUTO_INFO: Rodex session rodex-session is now named 'renamed'.",
        )
    ]
