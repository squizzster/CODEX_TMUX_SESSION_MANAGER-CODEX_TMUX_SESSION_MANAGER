from __future__ import annotations

import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event

import pytest

import rodex.live_runtime as live_module
import rodex.managed_session_lifecycle as managed_module
import rodex_registry.lifecycle as registry_module
from rodex.control import LiveRodexControl
from rodex.runtime import LiveRodexRuntime, LiveTmuxSession, RodexRuntimeError, RodexRuntimeLauncher
from rodex.tmux_session_capability import TmuxSessionCapability
from rodex_registry import (
    RodexRuntimeId,
    RodexRuntimeRegistrationRejectedError,
    RodexSession,
    create_a_rodex_session,
    lookup_rodex_registry_id,
    lookup_rodex_runtime_instance,
    lookup_rodex_runtime_registration,
    lookup_rodex_session_log,
    parse_codex_session_id,
    record_a_rodex_session_runtime_resume,
)

OLD_RUNTIME = RodexRuntimeId.parse("1111111111111111")
NEW_RUNTIME = RodexRuntimeId.parse("2222222222222222")
OTHER_RUNTIME = RodexRuntimeId.parse("3333333333333333")
CODEX_ID = parse_codex_session_id("01a00654-f2bc-7a30-834a-a5f886a65f82")
OTHER_CODEX_ID = parse_codex_session_id("01a00654-f2bc-7a30-834a-a5f886a65f83")
STARTED = datetime(2030, 1, 2, 12, tzinfo=UTC)


@pytest.fixture
def registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, RodexSession]:
    database = tmp_path / "rodex.sqlite3"
    monkeypatch.setattr(registry_module, "_utc_now_timestamp", lambda: "2030-01-02T12:00:00.000000Z")
    session = create_a_rodex_session(
        database,
        codex_session_id=CODEX_ID,
        tmux_server_socket_path=tmp_path / "incumbent.sock",
        tmux_session_name="incumbent",
        runtime_id=OLD_RUNTIME,
    )
    return database, session


def _adopt(
    database: Path,
    session: RodexSession,
    runtime_id: RodexRuntimeId = NEW_RUNTIME,
    *,
    expected_runtime_id: RodexRuntimeId | None = OLD_RUNTIME,
    timestamp: datetime = STARTED,
) -> None:
    record_a_rodex_session_runtime_resume(
        session.rodex_sessions_id,
        database.parent / f"{runtime_id}.sock",
        "candidate",
        database,
        runtime_id=runtime_id,
        codex_session_id=CODEX_ID,
        expected_previous_runtime_id=expected_runtime_id,
        accessed_at_utc=timestamp,
    )


def _snapshot(database: Path, session: RodexSession) -> tuple[object, ...]:
    return (
        lookup_rodex_runtime_registration(session.rodex_sessions_id, database),
        lookup_rodex_runtime_instance(session.rodex_sessions_id, database),
        lookup_rodex_session_log(session.rodex_sessions_id, database),
    )


@pytest.mark.parametrize("offset", [-timedelta(days=365), -timedelta(minutes=1), timedelta(), timedelta(minutes=1)])
def test_expected_incarnation_replacement_is_independent_of_clock_order(
    registered: tuple[Path, RodexSession], offset: timedelta
) -> None:
    database, session = registered
    timestamp = STARTED + offset

    _adopt(database, session, timestamp=timestamp)

    observed = lookup_rodex_runtime_instance(session.rodex_sessions_id, database)
    assert observed is not None
    assert observed.runtime_id == NEW_RUNTIME
    assert observed.started_at_utc == timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


@pytest.mark.parametrize("offset", [-timedelta(days=365), timedelta(), timedelta(days=365)])
def test_stale_expected_incarnation_never_wins_by_clock_value(
    registered: tuple[Path, RodexSession], offset: timedelta
) -> None:
    database, session = registered
    _adopt(database, session)
    before = _snapshot(database, session)

    with pytest.raises(RodexRuntimeRegistrationRejectedError, match="expected previous incarnation"):
        _adopt(database, session, OTHER_RUNTIME, timestamp=STARTED + offset)

    assert _snapshot(database, session) == before


@pytest.mark.parametrize("expected", [None, OLD_RUNTIME, NEW_RUNTIME])
def test_exact_accepted_tuple_retry_is_idempotent_even_after_an_acknowledgement_loss(
    registered: tuple[Path, RodexSession], expected: RodexRuntimeId | None
) -> None:
    database, session = registered
    _adopt(database, session)
    before = _snapshot(database, session)

    _adopt(database, session, expected_runtime_id=expected, timestamp=STARTED - timedelta(days=365))

    assert _snapshot(database, session) == before


@pytest.mark.parametrize("changed", ["socket", "name", "codex"])
def test_same_incarnation_cannot_rewrite_an_already_accepted_tuple(
    registered: tuple[Path, RodexSession], changed: str
) -> None:
    database, session = registered
    _adopt(database, session)
    before = _snapshot(database, session)

    with pytest.raises(RodexRuntimeRegistrationRejectedError, match="different identity or endpoint"):
        record_a_rodex_session_runtime_resume(
            session.rodex_sessions_id,
            database.parent / ("foreign.sock" if changed == "socket" else f"{NEW_RUNTIME}.sock"),
            "changed-name" if changed == "name" else "candidate",
            database,
            runtime_id=NEW_RUNTIME,
            codex_session_id=OTHER_CODEX_ID if changed == "codex" else CODEX_ID,
            expected_previous_runtime_id=NEW_RUNTIME,
        )

    assert _snapshot(database, session) == before


def test_none_expected_incarnation_is_an_absence_assertion(registered: tuple[Path, RodexSession]) -> None:
    database, session = registered
    before = _snapshot(database, session)

    with pytest.raises(RodexRuntimeRegistrationRejectedError):
        _adopt(database, session, expected_runtime_id=None)

    assert _snapshot(database, session) == before


@pytest.mark.parametrize("offset", [timedelta(), -timedelta(minutes=1), timedelta(days=365)])
def test_competing_candidates_share_one_compare_and_set_winner(
    registered: tuple[Path, RodexSession], offset: timedelta
) -> None:
    database, session = registered
    ready = Barrier(2)

    def compete(runtime_id: RodexRuntimeId, timestamp: datetime) -> bool:
        ready.wait(timeout=5)
        try:
            _adopt(database, session, runtime_id, timestamp=timestamp)
        except RodexRuntimeRegistrationRejectedError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(compete, NEW_RUNTIME, STARTED)
        second = workers.submit(compete, OTHER_RUNTIME, STARTED + offset)
        winners = first.result(timeout=10), second.result(timeout=10)

    assert sum(winners) == 1
    winner = NEW_RUNTIME if winners[0] else OTHER_RUNTIME
    observed = lookup_rodex_runtime_registration(session.rodex_sessions_id, database)
    assert observed is not None
    assert observed.runtime_id == winner
    assert observed.tmux_session.tmux_server_socket_path == str(database.parent / f"{winner}.sock")
    assert observed.codex_session_id == CODEX_ID


def _candidate(database: Path, runtime_id: RodexRuntimeId = NEW_RUNTIME) -> LiveRodexRuntime:
    return LiveRodexRuntime(
        database.parent / f"{runtime_id}.sock",
        "candidate",
        database.parent / "app.sock",
        database.parent / "app.log",
        database.parent / "proxy.sock",
        database.parent / "events.sock",
        runtime_id=runtime_id,
    )


class _Launcher:
    def __init__(self, database: Path, session: RodexSession, runtime: LiveTmuxSession) -> None:
        assert runtime.runtime_id is not None
        self.calls: list[tuple[str, RodexRuntimeId | None]] = []
        self.on_discovery: Callable[[], None] = lambda: None
        self.on_confirmation: Callable[[], None] = lambda: None
        self.capability = TmuxSessionCapability(
            runtime.tmux_server_socket_path,
            "0123456789abcdef0123456789abcdef",
            "$7",
            "%9",
            runtime.runtime_id,
            session.rodex_session_id,
            lookup_rodex_registry_id(database),
            session.rodex_sessions_id,
            session.codex_session_id,
        )
        self.control = LiveRodexControl(
            database.parent / "proxy.sock",
            database.parent / "events.sock",
            session.codex_session_id,
            session.rodex_session_id,
            self.capability.registry_id,
            "pending",
            runtime.runtime_id,
        )

    def session_exists(self, _runtime: LiveTmuxSession) -> bool:
        return False

    def list_session_names(self, _socket: Path) -> tuple[str, ...]:
        return ()

    def discover_runtime_control(self, runtime: LiveTmuxSession) -> LiveRodexControl:
        self.calls.append(("discover", runtime.runtime_id))
        self.on_discovery()
        return self.control

    def confirm_runtime_registration(self, runtime: LiveTmuxSession, *_args: object, **_kwargs: object) -> None:
        self.calls.append(("confirm", runtime.runtime_id))
        self.on_confirmation()
        self.control = replace(self.control, registration_state="registered", tmux_capability=self.capability)

    def rename(self, runtime: LiveTmuxSession, name: str) -> LiveTmuxSession:
        self.calls.append(("rename", runtime.runtime_id))
        return replace(runtime, tmux_session_name=name)

    def initialise_session_ui(self, runtime: LiveTmuxSession) -> None:
        self.calls.append(("initialize", runtime.runtime_id))

    def stop(self, runtime: LiveTmuxSession, *, check: bool) -> None:
        assert check is False
        self.calls.append(("stop", runtime.runtime_id))


def _verify(database: Path, session: RodexSession, launcher: _Launcher, runtime: LiveTmuxSession) -> LiveRodexControl:
    return live_module.verify_live_runtime_identity(
        launcher,  # type: ignore[arg-type]
        runtime,
        session_id=session.rodex_sessions_id,
        database_path=database,
        expected_rodex_session_id=session.rodex_session_id,
        expected_registry_id=lookup_rodex_registry_id(database),
        expected_codex_session_id=session.codex_session_id,
    )


def _prepare(database: Path, session: RodexSession, launcher: _Launcher):
    return managed_module._prepare_selected_session(
        session.rodex_sessions_id,
        session.cool_name,
        database,
        launcher,  # type: ignore[arg-type]
        codex_available=True,
        configured_codex="codex",
    )


@pytest.mark.parametrize("route", ["read", "resume"])
def test_managed_resolution_retains_durable_incarnation_during_liveness(
    registered: tuple[Path, RodexSession], monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    database, session = registered

    def failed_inventory(command, **_options):
        if "list-sessions" in command:
            raise subprocess.TimeoutExpired(command, 5)
        # A name-only probe cannot establish why tmux rejected the command.
        return subprocess.CompletedProcess(command, 1, "", "query failed")

    launcher = RodexRuntimeLauncher("codex", "tmux", runner=failed_inventory)
    monkeypatch.setattr(
        managed_module, "_start_managed_runtime", lambda *_args, **_kwargs: pytest.fail("unknown liveness cannot resume")
    )
    before = _snapshot(database, session)
    with pytest.raises(RodexRuntimeError, match="timed out"):
        if route == "read":
            live_module.resolve_live_control(session.cool_name, database, launcher)
        else:
            _prepare(database, session, launcher)
    assert _snapshot(database, session) == before


def test_rejected_new_candidate_is_stopped_before_any_registration_or_presentation(
    registered: tuple[Path, RodexSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, session = registered
    candidate = _candidate(database)
    launcher = _Launcher(database, session, candidate)

    def launch_competing(*_args: object, **_kwargs: object):
        _adopt(database, session, OTHER_RUNTIME)
        return candidate, CODEX_ID

    monkeypatch.setattr(managed_module, "_start_managed_runtime", launch_competing)

    with pytest.raises(RodexRuntimeRegistrationRejectedError):
        _prepare(database, session, launcher)

    assert launcher.calls == [("stop", NEW_RUNTIME)]
    assert lookup_rodex_runtime_registration(session.rodex_sessions_id, database).runtime_id == OTHER_RUNTIME


def test_rejected_relocated_candidate_never_confirms_or_kills_a_discovered_runtime(
    registered: tuple[Path, RodexSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, session = registered
    candidate = _candidate(database)
    launcher = _Launcher(database, session, candidate)

    def relocate_competing(*_args: object, **_kwargs: object):
        _adopt(database, session, OTHER_RUNTIME)
        return candidate, launcher.control

    monkeypatch.setattr(managed_module, "find_relocated_live_runtime", relocate_competing)

    with pytest.raises(RodexRuntimeRegistrationRejectedError):
        _prepare(database, session, launcher)

    assert launcher.calls == []
    assert lookup_rodex_runtime_registration(session.rodex_sessions_id, database).runtime_id == OTHER_RUNTIME


def test_pending_reader_can_only_complete_the_already_durable_incarnation(
    registered: tuple[Path, RodexSession],
) -> None:
    database, session = registered
    runtime = LiveTmuxSession(database.parent / "incumbent.sock", "incumbent", runtime_id=NEW_RUNTIME)
    launcher = _Launcher(database, session, runtime)
    before = _snapshot(database, session)

    with pytest.raises(RodexRuntimeRegistrationRejectedError, match="pending reader cannot replace"):
        _verify(database, session, launcher, runtime)

    assert launcher.calls == [("discover", NEW_RUNTIME)]
    assert _snapshot(database, session) == before


def test_pending_reader_rejects_replacement_after_its_coherent_snapshot(registered: tuple[Path, RodexSession]) -> None:
    database, session = registered
    runtime = LiveTmuxSession(database.parent / "incumbent.sock", "incumbent", runtime_id=OLD_RUNTIME)
    launcher = _Launcher(database, session, runtime)
    launcher.on_discovery = lambda: _adopt(database, session)

    with pytest.raises(RodexRuntimeRegistrationRejectedError, match="expected previous incarnation"):
        _verify(database, session, launcher, runtime)

    assert launcher.calls == [("discover", OLD_RUNTIME)]
    assert lookup_rodex_runtime_registration(session.rodex_sessions_id, database).runtime_id == NEW_RUNTIME


def test_stale_endpoint_is_rejected_before_discovery_can_supply_a_new_expectation(
    registered: tuple[Path, RodexSession],
) -> None:
    database, session = registered
    runtime = LiveTmuxSession(database.parent / "incumbent.sock", "incumbent", runtime_id=OLD_RUNTIME)
    launcher = _Launcher(database, session, runtime)
    _adopt(database, session)

    with pytest.raises(RodexRuntimeRegistrationRejectedError, match="durable runtime endpoint"):
        _verify(database, session, launcher, runtime)

    assert launcher.calls == []
    assert lookup_rodex_runtime_registration(session.rodex_sessions_id, database).runtime_id == NEW_RUNTIME


def test_pending_reader_inside_creator_lock_completes_without_rewriting_the_incarnation(
    registered: tuple[Path, RodexSession],
) -> None:
    database, session = registered
    runtime = LiveTmuxSession(database.parent / "incumbent.sock", "incumbent", runtime_id=OLD_RUNTIME)
    launcher = _Launcher(database, session, runtime)
    before = _snapshot(database, session)

    with live_module.session_transition_lock(database, session.rodex_session_id):
        verified = _verify(database, session, launcher, runtime)

    assert verified.registration_state == "registered"
    assert launcher.calls == [("discover", OLD_RUNTIME), ("confirm", OLD_RUNTIME), ("discover", OLD_RUNTIME)]
    assert _snapshot(database, session) == before


def test_direct_resume_with_clock_rollback_only_publishes_its_accepted_candidate(
    registered: tuple[Path, RodexSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, session = registered
    candidate = _candidate(database)
    launcher = _Launcher(database, session, candidate)
    monkeypatch.setattr(managed_module, "_start_managed_runtime", lambda *_args, **_kwargs: (candidate, CODEX_ID))
    normalize = registry_module._normalise_utc_datetime
    monkeypatch.setattr(
        registry_module,
        "_normalise_utc_datetime",
        lambda value: normalize(STARTED - timedelta(minutes=1) if value is None else value),
    )

    prepared = _prepare(database, session, launcher)

    observed = lookup_rodex_runtime_registration(session.rodex_sessions_id, database)
    assert prepared.active_tmux.runtime_id == observed.runtime_id == NEW_RUNTIME
    assert observed.tmux_session.tmux_session_name == prepared.display_name
    assert launcher.calls == [("confirm", NEW_RUNTIME), ("rename", NEW_RUNTIME), ("initialize", NEW_RUNTIME)]


def test_reader_confirmation_and_final_validation_complete_before_resumer_can_replace(
    registered: tuple[Path, RodexSession],
) -> None:
    database, session = registered
    runtime = LiveTmuxSession(database.parent / "incumbent.sock", "incumbent", runtime_id=OLD_RUNTIME)
    launcher = _Launcher(database, session, runtime)
    confirming = Event()
    release_confirmation = Event()
    resumer_started = Event()
    replaced = Event()

    def delayed_confirmation() -> None:
        confirming.set()
        assert release_confirmation.wait(timeout=5)

    def resume() -> None:
        resumer_started.set()
        with live_module.session_transition_lock(database, session.rodex_session_id):
            _adopt(database, session)
            replaced.set()

    launcher.on_confirmation = delayed_confirmation
    with ThreadPoolExecutor(max_workers=2) as workers:
        reader = workers.submit(_verify, database, session, launcher, runtime)
        assert confirming.wait(timeout=5)
        resumer = workers.submit(resume)
        try:
            assert resumer_started.wait(timeout=5)
            assert not replaced.wait(timeout=0.05)
            assert lookup_rodex_runtime_registration(session.rodex_sessions_id, database).runtime_id == OLD_RUNTIME
        finally:
            release_confirmation.set()
        assert reader.result(timeout=5).runtime_id == OLD_RUNTIME
        resumer.result(timeout=5)
    assert replaced.is_set()
    assert lookup_rodex_runtime_registration(session.rodex_sessions_id, database).runtime_id == NEW_RUNTIME


def test_resolver_holds_transition_ownership_across_snapshot_and_discovery(
    registered: tuple[Path, RodexSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, session = registered
    runtime = LiveTmuxSession(database.parent / "incumbent.sock", "incumbent", runtime_id=OLD_RUNTIME)
    launcher = _Launcher(database, session, runtime)
    in_liveness = Event()
    release_reader = Event()
    rename_attempted = Event()
    renamed = Event()

    def exists(_runtime: LiveTmuxSession) -> bool:
        in_liveness.set()
        assert release_reader.wait(5)
        return True

    def rename() -> None:
        rename_attempted.set()
        with live_module.session_transition_lock(database, session.rodex_session_id):
            registry_module.update_rodex_tmux_session_name(session.rodex_sessions_id, "renamed", database)
            renamed.set()

    monkeypatch.setattr(launcher, "session_exists", exists)
    with ThreadPoolExecutor(max_workers=2) as workers:
        reader = workers.submit(live_module.resolve_live_control, session.cool_name, database, launcher)
        assert in_liveness.wait(5)
        writer = workers.submit(rename)
        try:
            assert rename_attempted.wait(5)
            assert not renamed.wait(0.05)
        finally:
            release_reader.set()
        _session_id, resolved, control = reader.result(timeout=5)
        writer.result(timeout=5)
    assert resolved.tmux_session_name == "incumbent"
    assert control.runtime_id == OLD_RUNTIME
    assert renamed.is_set()


def test_resolver_does_not_follow_a_selector_reassigned_while_waiting_for_lock(
    registered: tuple[Path, RodexSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, session = registered
    runtime = LiveTmuxSession(database.parent / "incumbent.sock", "incumbent", runtime_id=OLD_RUNTIME)
    launcher = _Launcher(database, session, runtime)
    selections = iter((session.rodex_sessions_id, session.rodex_sessions_id + 1))
    monkeypatch.setattr(live_module, "lookup_owned_rodex_sessions_id_from_a_cool_name", lambda *_: next(selections))
    with pytest.raises(managed_module.RodexLaunchError, match="selector changed"):
        live_module.resolve_live_control(session.cool_name, database, launcher)  # type: ignore[arg-type]
    assert launcher.calls == []


def test_named_attach_does_not_follow_a_selector_reassigned_while_waiting_for_lock(
    registered: tuple[Path, RodexSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, session = registered
    runtime = LiveTmuxSession(database.parent / "incumbent.sock", "incumbent", runtime_id=OLD_RUNTIME)
    launcher = _Launcher(database, session, runtime)
    selection = managed_module.OwnedSessionSelection(session.cool_name, session.rodex_sessions_id)
    monkeypatch.setattr(managed_module, "_lookup_owned_rodex_session_selector", lambda *_: session.rodex_sessions_id + 1)
    with pytest.raises(managed_module.RodexLaunchError, match="selector changed"):
        managed_module._open_selected_session(
            selection, database, launcher, codex_available=True, configured_codex="codex", detach=False
        )
    assert launcher.calls == []
