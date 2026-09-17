"""Adversarial observer lifecycle checks using only test-owned servers and sockets."""

from __future__ import annotations

import queue
import shlex
import shutil
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest

import rodex.agent_observer as observer_module
from rodex.observer_pane import ObserverPaneController
from rodex.pane_control import TmuxPaneController
from rodex.runtime_endpoint import ExclusiveUnixEndpoint
from rodex.runtime_peer import RuntimePeerIdentity
from rodex.tmux_session_capability import RODEX_SHARED_TMUX_PROTOCOL, TmuxRuntimeCapability
from rodex_registry import RodexRuntimeId


def test_listener_has_one_lifetime_owner(tmp_path: Path) -> None:
    path = tmp_path / "o.sock"
    first, duplicate = ExclusiveUnixEndpoint(path), ExclusiveUnixEndpoint(path)
    listener = first.open()
    try:
        with pytest.raises(BlockingIOError):
            duplicate.open()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            accepted, _ = listener.accept()
            accepted.close()
        assert path.exists()
    finally:
        duplicate.close()
        first.close()
    assert not path.exists()
    assert first.lock_path.exists()
    replacement = ExclusiveUnixEndpoint(path)
    replacement.open()
    replacement.close()


def test_old_cleanup_preserves_replacement(tmp_path: Path) -> None:
    path = tmp_path / "o.sock"
    owner = ExclusiveUnixEndpoint(path)
    owner.open()
    path.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as replacement:
        replacement.bind(str(path))
        replacement.listen()
        owner.close()
        assert path.exists()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            accepted, _ = replacement.accept()
            accepted.close()


def test_external_child_binding_has_one_reserved_lifetime(tmp_path: Path) -> None:
    path = tmp_path / "child.sock"
    owner, duplicate = ExclusiveUnixEndpoint(path), ExclusiveUnixEndpoint(path)
    owner.acquire()
    try:
        with pytest.raises(BlockingIOError):
            duplicate.acquire()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as child:
            child.bind(str(path))
            child.listen()
            owner.retain_bound_path()
            assert path.stat().st_mode & 0o777 == 0o600
    finally:
        duplicate.close()
        owner.close()
    assert not path.exists()


@pytest.mark.parametrize("live", [False, True])
def test_existing_socket_needs_proven_staleness(tmp_path: Path, live: bool) -> None:
    path = tmp_path / "o.sock"
    existing = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    existing.bind(str(path))
    if live:
        existing.listen()
    else:
        existing.close()
    owner = ExclusiveUnixEndpoint(path)
    try:
        if live:
            original = path.stat().st_ino
            with pytest.raises(OSError, match="live listener"):
                owner.open()
            assert path.stat().st_ino == original
        else:
            owner.open()
            assert path.exists()
    finally:
        owner.close()
        existing.close()
    assert path.exists() == live


@pytest.mark.parametrize("symlink", [False, True])
def test_endpoint_never_removes_other_file_types(tmp_path: Path, symlink: bool) -> None:
    path = tmp_path / "o.sock"
    retained = tmp_path / "retained"
    retained.write_text("retained")
    if symlink:
        path.symlink_to(retained)
    else:
        path.write_text("not a socket")
    owner = ExclusiveUnixEndpoint(path)
    with pytest.raises(OSError, match="not a current-user socket"):
        owner.open()
    assert retained.read_text() == "retained"
    assert path.exists()


@pytest.fixture
def owned_tmux(tmp_path: Path):
    tmux = shutil.which("tmux")
    if tmux is None:
        pytest.skip("tmux is not installed")
    path = tmp_path / "t.sock"

    def run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run([tmux, "-S", str(path), *arguments], capture_output=True, text=True, check=check, timeout=3)

    run("-f", "/dev/null", "new-session", "-d", "-s", "owned", "-x", "120", "-y", "50", "sleep 60")
    runtime_id = RodexRuntimeId.parse("1234567890abcdef")
    server_id = "a" * 32
    primary, session = run("display-message", "-p", "-t", "owned", "#{pane_id}|#{session_id}").stdout.strip().split("|")
    for name, value in (
        ("@rodex_shared_tmux_protocol", RODEX_SHARED_TMUX_PROTOCOL),
        ("@rodex_shared_tmux_server_id", server_id),
        ("@rodex_server_runtime_id", str(runtime_id)),
    ):
        run("set-option", "-s", name, value)
    run("set-option", "-t", session, "@rodex_runtime_id", str(runtime_id))
    run("set-option", "-t", session, "@rodex_primary_pane_id", primary)
    run("set-option", "-p", "-t", primary, "@rodex_pane_runtime_id", str(runtime_id))
    capability = TmuxRuntimeCapability(path, server_id, session, primary, runtime_id)
    try:
        yield tmux, capability, run
    finally:
        run("kill-server", check=False)


def pane_controller(tmux: str, capability: TmuxRuntimeCapability, runner=subprocess.run) -> TmuxPaneController:
    return TmuxPaneController(
        tmux,
        capability,
        capability.pane_target,
        runner=runner,
        python_executable=sys.executable,
        primary_pane_option="@rodex_agent_observer_pane_id",
        owner_pane_option="@rodex_agent_observer_for",
    )


def test_lost_split_reply_recovers_one_pane(owned_tmux) -> None:
    tmux, capability, run = owned_tmux
    splits = 0

    def runner(command, **options):
        nonlocal splits
        response = subprocess.run(command, **options)
        if "split-window" in command[-2]:
            splits += 1
            assert response.returncode == 0
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return response

    controller = pane_controller(tmux, capability, runner)
    candidate = controller.create(("sleep", "60"))
    assert candidate is not None
    assert controller.create(("sleep", "60")) == candidate
    assert splits == 1
    assert len(run("list-panes", "-a").stdout.splitlines()) == 2
    assert run("show-options", "-p", "-v", "-t", candidate, "@rodex_pane_runtime_id").stdout.strip() == str(
        capability.runtime_id
    )


def test_delayed_split_cannot_be_resubmitted(owned_tmux) -> None:
    tmux, capability, run = owned_tmux
    pending = []

    def runner(command, **options):
        if "split-window" in command[-2]:
            pending.append((command, options))
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return subprocess.run(command, **options)

    controller = pane_controller(tmux, capability, runner)
    assert controller.create(("sleep", "60")) is None
    assert controller.creation_pending
    assert controller.create(("sleep", "60")) is None
    assert len(pending) == 2
    responses = [subprocess.run(command, **options) for command, options in reversed(pending)]
    assert all(response.returncode == 0 for response in responses)
    admitted = [response.stdout.strip() for response in responses if response.stdout.startswith("%")]
    assert len(admitted) == 1
    candidate = controller.create(("sleep", "60"))
    assert candidate == admitted[0]
    assert not controller.creation_pending
    assert len(run("list-panes", "-a").stdout.splitlines()) == 2


@pytest.mark.parametrize("stage", ["split", "publication"])
def test_moved_candidate_is_never_claimed_or_killed(owned_tmux, stage: str) -> None:
    tmux, capability, run = owned_tmux
    run("new-session", "-d", "-s", "foreign", "sleep 60")
    candidate = None
    moved = False

    def runner(command, **options):
        nonlocal candidate, moved
        response = subprocess.run(command, **options)
        action = shlex.split(command[-2])
        if "split-window" in command[-2]:
            candidate = response.stdout.strip()
        trigger = (
            "split-window" in command[-2]
            if stage == "split"
            else (action[0] == "if-shell" and "registered:" in command[-2])
        )
        if trigger and candidate is not None and not moved:
            run("move-pane", "-s", candidate, "-t", "foreign", "-d")
            moved = True
        return response

    controller = pane_controller(tmux, capability, runner)
    assert controller.create(("sleep", "60")) is None
    assert candidate is not None
    assert run("display-message", "-p", "-t", candidate, "#{session_name}").stdout.strip() == "foreign"
    if stage == "split":
        assert run("show-options", "-p", "-v", "-t", candidate, "@rodex_pane_runtime_id").stdout.strip() == ""
        assert run("show-options", "-p", "-v", "-t", candidate, "@rodex_agent_observer_for").stdout.strip() == ""


def test_failed_lookup_is_not_absence(owned_tmux) -> None:
    tmux, capability, _run = owned_tmux
    controller = pane_controller(tmux, capability)
    candidate = controller.create(("sleep", "60"))
    assert candidate is not None
    assert not controller.confirm_absent(candidate)
    controller._tmux_executor._runner = lambda command, **_options: subprocess.CompletedProcess(
        command, 1, "", "failed read"
    )
    assert not controller.confirm_absent(candidate)


def test_timed_out_close_can_be_confirmed(owned_tmux) -> None:
    tmux, capability, _run = owned_tmux

    def runner(command, **options):
        response = subprocess.run(command, **options)
        if "kill-pane" in command[-2]:
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return response

    controller = pane_controller(tmux, capability, runner)
    candidate = controller.create(("sleep", "60"))
    assert candidate is not None
    assert not controller.close(candidate)
    assert controller.confirm_absent(candidate)


def test_independent_coordinators_share_one_creation_receipt(owned_tmux) -> None:
    tmux, capability, run = owned_tmux
    admission = Barrier(2)

    def runner(command, **options):
        if "split-window" in command[-2]:
            admission.wait(timeout=3)
        return subprocess.run(command, **options)

    controllers = [pane_controller(tmux, capability, runner) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda controller: controller.create(("sleep", "60")), controllers))
    assert outcomes[0] is not None and outcomes[1] == outcomes[0]
    assert len(run("list-panes", "-a").stdout.splitlines()) == 2


def test_replacement_coordinator_recovers_split_after_creator_disappears(owned_tmux) -> None:
    tmux, capability, run = owned_tmux

    def interrupted(command, **options):
        response = subprocess.run(command, **options)
        if "split-window" in command[-2]:
            raise KeyboardInterrupt("creator process exited after split")
        return response

    original = pane_controller(tmux, capability, interrupted)
    with pytest.raises(KeyboardInterrupt):
        original.create(("sleep", "60"))
    replacement = pane_controller(tmux, capability)
    candidate = replacement.create(("sleep", "60"))
    assert candidate is not None
    assert len(run("list-panes", "-a").stdout.splitlines()) == 2
    assert replacement.create(("sleep", "60")) == candidate


def test_creator_failure_cannot_roll_back_another_coordinators_registration(owned_tmux) -> None:
    tmux, capability, run = owned_tmux
    replacement = pane_controller(tmux, capability)
    adopted = None

    def interrupted(command, **options):
        nonlocal adopted
        response = subprocess.run(command, **options)
        if adopted is None and command[-2].startswith("set-option") and "@rodex_pane_runtime_id" in command[-2]:
            adopted = replacement.create(("sleep", "60"))
            return subprocess.CompletedProcess(command, 1, "", "lost creator mutation response")
        return response

    original = pane_controller(tmux, capability, interrupted)
    candidate = original.create(("sleep", "60"))
    assert candidate is not None and candidate == adopted
    assert run("display-message", "-p", "-t", candidate, "#{pane_id}").stdout.strip() == candidate
    assert len(run("list-panes", "-a").stdout.splitlines()) == 2


def test_lost_final_registration_reply_is_reconciled(owned_tmux) -> None:
    tmux, capability, run = owned_tmux

    def lost_reply(command, **options):
        response = subprocess.run(command, **options)
        if command[-2].startswith("if-shell") and "registered:" in command[-2]:
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return response

    controller = pane_controller(tmux, capability, lost_reply)
    candidate = controller.create(("sleep", "60"))
    assert candidate is not None and controller.locate() == candidate
    assert len(run("list-panes", "-a").stdout.splitlines()) == 2


@pytest.mark.parametrize("native_close", [False, True])
def test_retired_pane_receipt_allows_one_new_generation(owned_tmux, native_close: bool) -> None:
    tmux, capability, run = owned_tmux
    original = pane_controller(tmux, capability)
    old_pane = original.create(("sleep", "60"))
    assert old_pane is not None
    if native_close:
        run("kill-pane", "-t", old_pane)
    else:
        assert original.close(old_pane)
    replacement = pane_controller(tmux, capability)
    new_pane = replacement.create(("sleep", "60"))
    assert new_pane is not None and new_pane != old_pane
    assert original.confirm_absent(old_pane)
    assert replacement.locate() == new_pane
    assert pane_controller(tmux, capability).create(("sleep", "60")) == new_pane
    assert len(run("list-panes", "-a").stdout.splitlines()) == 2


def test_pending_creation_without_a_discoverable_candidate_never_blindly_splits(owned_tmux) -> None:
    tmux, capability, run = owned_tmux
    run(
        "set-option",
        "-p",
        "-t",
        capability.pane_target,
        "@rodex_agent_observer_pane_id_creation",
        "pending:" + "c" * 32,
    )
    for _ in range(2):
        controller = pane_controller(tmux, capability)
        assert controller.create(("sleep", "60")) is None
        assert controller.creation_pending
    assert len(run("list-panes", "-a").stdout.splitlines()) == 1


def test_delayed_old_admission_cannot_cross_retirement_or_replace_new_generation(owned_tmux) -> None:
    tmux, capability, run = owned_tmux
    admitted_commands = []

    def runner(command, **options):
        if "split-window" in command[-2]:
            admitted_commands.append((command, options))
        return subprocess.run(command, **options)

    original = pane_controller(tmux, capability, runner)
    old_pane = original.create(("sleep", "60"))
    assert old_pane is not None
    old_command, old_options = admitted_commands[0]
    assert original.close(old_pane)
    assert subprocess.run(old_command, **old_options).stdout.startswith("retired:")
    assert len(run("list-panes", "-a").stdout.splitlines()) == 1
    new_pane = pane_controller(tmux, capability).create(("sleep", "60"))
    assert new_pane is not None and new_pane != old_pane
    assert subprocess.run(old_command, **old_options).stdout.endswith(f":{new_pane}\n")
    assert len(run("list-panes", "-a").stdout.splitlines()) == 2


@pytest.mark.parametrize("admitted", [False, True])
def test_final_close_reconciles_a_pending_creation_without_another_split(owned_tmux, admitted: bool) -> None:
    tmux, capability, run = owned_tmux
    delayed = []

    def runner(command, **options):
        if "split-window" in command[-2]:
            delayed.append((command, options))
            if admitted:
                subprocess.run(command, **options)
                raise KeyboardInterrupt("creator stopped after admission")
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return subprocess.run(command, **options)

    observer = ObserverPaneController(tmux, capability, capability.pane_target, runner=runner)
    if admitted:
        with pytest.raises(KeyboardInterrupt):
            observer._pane.create(("sleep", "60"))
    else:
        assert observer._pane.create(("sleep", "60")) is None
    assert observer.close().accepted
    assert len(run("list-panes", "-a").stdout.splitlines()) == 1
    command, options = delayed[0]
    assert subprocess.run(command, **options).stdout.startswith("retired:")
    assert len(run("list-panes", "-a").stdout.splitlines()) == 1


@pytest.mark.parametrize("kind", ["display_message", "observer_state_snapshot", "trace_published"])
@pytest.mark.parametrize("identity_change", [None, "runtime_id", "tmux_server_id", "missing"])
def test_all_observer_control_frames_require_the_exact_runtime_peer(kind: str, identity_change: str | None) -> None:
    peer = RuntimePeerIdentity("1234567890abcdef", "a" * 32)
    frame_event = {
        "schema": observer_module.OBSERVER_SCHEMA,
        "kind": kind,
        "pane_id": "%1",
        "runtime_id": str(peer.runtime_id),
        "tmux_server_id": peer.tmux_server_id,
    }
    if identity_change == "missing":
        del frame_event["runtime_id"]
        del frame_event["tmux_server_id"]
    elif identity_change is not None:
        frame_event[identity_change] = "b" * len(frame_event[identity_change])

    class MemoryConnection:
        payload = observer_module._observer_event_frame(frame_event)
        acknowledgement = b""

        def __enter__(self):
            return self

        def __exit__(self, *_arguments):
            return None

        def settimeout(self, _timeout):
            return None

        def recv(self, size):
            result, self.payload = self.payload[:size], self.payload[size:]
            return result

        def sendall(self, value):
            self.acknowledgement = value

    connection = MemoryConnection()

    class MemoryListener:
        consumed = False

        def accept(self):
            if self.consumed:
                raise OSError("listener closed")
            self.consumed = True
            return connection, None

    events = queue.Queue()
    observer_module._observer_control_receiver(
        MemoryListener(), events, Event(), peer_identity=peer, expected_pane_id="%1"
    )
    accepted = identity_change is None
    assert events.empty() != accepted
    if accepted:
        assert events.get_nowait() == frame_event
    if kind == "display_message":
        assert connection.acknowledgement == (b"1" if accepted else b"0")
