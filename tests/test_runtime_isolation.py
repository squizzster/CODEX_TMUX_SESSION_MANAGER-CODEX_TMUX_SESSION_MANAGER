"""Native tmux adversaries exercise the runtime boundary, not a format emulator."""

import shutil
import socket
import subprocess
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

import rodex.runtime as runtime_module
from rodex.live_runtime import revalidate_live_control
from rodex.runtime import LiveRodexRuntime, LiveTmuxSession, RodexRuntimeError, RodexRuntimeLauncher
from rodex.tmux_session_capability import runtime_tmux_socket_name
from rodex_registry import RodexRegistryId, RodexRuntimeId, RodexSessionId


@pytest.fixture
def isolated_runtimes():
    binary = shutil.which("tmux")
    if binary is None:
        pytest.fail("runtime isolation acceptance requires tmux")
    with tempfile.TemporaryDirectory(prefix="rdx-v3-") as directory:
        root = Path(directory)
        runtimes = []

        def runner(command, **options):
            return subprocess.run([command[0], "-f", "/dev/null", *command[1:]], **options)

        launcher = RodexRuntimeLauncher("unused", binary, runner=runner)

        def tmux(runtime, *arguments, check=True):
            return subprocess.run(
                [binary, "-N", "-S", str(runtime.tmux_server_socket_path), *arguments],
                text=True,
                capture_output=True,
                check=check,
                timeout=5,
            )

        def create(number=1):
            identity = RodexRuntimeId(number)
            runtime = LiveRodexRuntime(
                root / runtime_tmux_socket_name(identity),
                f"runtime-{number}",
                app_server_socket_path=root / f"app-{identity}.sock",
                app_server_log_path=root / f"app-{identity}.log",
                protocol_proxy_socket_path=root / f"proxy-{identity}.sock",
                protocol_event_socket_path=root / f"events-{identity}.sock",
                runtime_id=identity,
            )
            runtimes.append(runtime)
            launcher._start_tmux_session(runtime, root, ("/bin/cat",))
            launcher.publish_runtime_control(runtime, uuid.UUID(int=number), RodexSessionId(number), RodexRegistryId(1))
            launcher.confirm_runtime_registration(
                runtime,
                number,
                expected_rodex_session_id=RodexSessionId(number),
                expected_registry_id=RodexRegistryId(1),
                expected_codex_session_id=uuid.UUID(int=number),
            )
            control = launcher.discover_runtime_control(runtime)
            return replace(runtime, tmux_capability=control.tmux_capability)

        try:
            yield launcher, create, tmux
        finally:
            for runtime in runtimes:
                tmux(runtime, "kill-server", check=False)


def test_two_runtimes_separate_native_input_output_and_shutdown(isolated_runtimes):
    launcher, create, tmux = isolated_runtimes
    first, second = create(1), create(2)
    assert first.tmux_server_socket_path != second.tmux_server_socket_path
    assert first.tmux_capability.tmux_server_id != second.tmux_capability.tmux_server_id
    # Server-local IDs intentionally collide: authority must include the server.
    assert first.tmux_capability.pane_target == second.tmux_capability.pane_target
    tmux(first, "send-keys", "-t", "%0", "FIRST_RUNTIME_ONLY", "Enter")
    assert "FIRST_RUNTIME_ONLY" in tmux(first, "capture-pane", "-p", "-t", "%0").stdout
    assert "FIRST_RUNTIME_ONLY" not in tmux(second, "capture-pane", "-p", "-t", "%0").stdout
    launcher.stop(first)
    assert not launcher.session_exists(first)
    assert tmux(second, "has-session", "-t", "=runtime-2").returncode == 0
    assert launcher.discover_runtime_control(second).runtime_id == second.runtime_id


@pytest.mark.parametrize("operation", ["swap-pane", "join-pane", "move-pane"])
def test_native_pane_operations_cannot_reach_another_runtime_server(isolated_runtimes, operation):
    launcher, create, tmux = isolated_runtimes
    first, second = create(1), create(2)
    attempted = tmux(first, operation, "-s", "=runtime-2:", "-t", "%0", check=False)
    assert attempted.returncode != 0
    assert launcher.discover_runtime_control(first).runtime_id == first.runtime_id
    assert launcher.discover_runtime_control(second).runtime_id == second.runtime_id


def test_second_runtime_cannot_claim_an_existing_server(isolated_runtimes):
    launcher, create, tmux = isolated_runtimes
    first = create(1)
    intruder = LiveTmuxSession(first.tmux_server_socket_path, "intruder", runtime_id=RodexRuntimeId(2))
    with pytest.raises(RodexRuntimeError):
        launcher._start_tmux_session(intruder, first.tmux_server_socket_path.parent, ("/bin/cat",))
    assert tmux(first, "list-sessions", "-F", "#{session_name}").stdout.strip() == first.tmux_session_name
    assert launcher.discover_runtime_control(first).runtime_id == first.runtime_id


def test_creation_collision_cannot_acquire_incumbent_cleanup_authority(isolated_runtimes, monkeypatch):
    launcher, create, tmux = isolated_runtimes
    incumbent = create(1)
    root = incumbent.tmux_server_socket_path.parent
    tmux(incumbent, "rename-session", "-t", "$0", f"rodex-{incumbent.runtime_id}")
    before = tmux(incumbent, "list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}").stdout
    monkeypatch.setattr(runtime_module, "default_runtime_root", lambda: root)
    with pytest.raises(RodexRuntimeError):
        launcher.start(
            root,
            (),
            runtime_id=incumbent.runtime_id,
            rodex_session_id=RodexSessionId(2),
            rodex_registry_id=RodexRegistryId(1),
            rodex_database_path=root / "unused.sqlite3",
        )
    assert tmux(incumbent, "list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}").stdout == before
    assert launcher.discover_runtime_control(incumbent).tmux_capability == incumbent.tmux_capability


def test_lost_creation_acknowledgement_cleans_only_its_own_receipt(isolated_runtimes, monkeypatch):
    launcher, create, tmux = isolated_runtimes
    incumbent = create(1)
    root = incumbent.tmux_server_socket_path.parent
    monkeypatch.setattr(runtime_module, "default_runtime_root", lambda: root)
    runner = launcher._run
    dispatched = False

    def lose_creation_reply(command, **options):
        nonlocal dispatched
        result = runner(command, **options)
        if "start-server" in command and not dispatched:
            dispatched = True
            raise subprocess.TimeoutExpired(command, 5)
        return result

    launcher._run = lose_creation_reply
    with pytest.raises(RodexRuntimeError, match="timed out"):
        launcher.start(
            root,
            (),
            runtime_id=RodexRuntimeId(2),
            rodex_session_id=RodexSessionId(2),
            rodex_registry_id=RodexRegistryId(1),
            rodex_database_path=root / "unused.sqlite3",
        )
    rejected = LiveTmuxSession(root / runtime_tmux_socket_name(RodexRuntimeId(2)), "unused")
    try:
        assert tmux(rejected, "list-sessions", check=False).returncode != 0
        assert launcher.discover_runtime_control(incumbent).tmux_capability == incumbent.tmux_capability
    finally:
        tmux(rejected, "kill-server", check=False)


@pytest.mark.parametrize(
    "option,value",
    [
        ("@rodex_protocol_proxy_socket_path", "/tmp/proxy-other.sock"),
        ("@rodex_protocol_event_socket_path", "/tmp/events-other.sock"),
        ("@rodex_pane_runtime_id", "0000000000000002"),
        ("@rodex_server_runtime_id", "0000000000000002"),
    ],
)
def test_discovery_rejects_cross_runtime_metadata_and_ownership(isolated_runtimes, option, value):
    launcher, create, tmux = isolated_runtimes
    runtime = create()
    if option == "@rodex_pane_runtime_id":
        tmux(runtime, "set-option", "-p", "-t", "%0", option, value)
    elif option == "@rodex_server_runtime_id":
        tmux(runtime, "set-option", "-s", option, value)
    else:
        tmux(runtime, "set-option", "-t", "%0", option, value)
    with pytest.raises(RodexRuntimeError):
        launcher.discover_runtime_control(runtime)


@pytest.mark.parametrize("intrusion", ["pane", "window", "session"])
def test_shutdown_refuses_unowned_topology(isolated_runtimes, intrusion):
    launcher, create, tmux = isolated_runtimes
    runtime = create()
    if intrusion == "pane":
        tmux(runtime, "split-window", "-d", "-t", "%0", "sleep 30")
    elif intrusion == "window":
        tmux(runtime, "new-window", "-d", "-t", "=runtime-1:", "sleep 30")
    else:
        tmux(runtime, "new-session", "-d", "-s", "foreign", "sleep 30")
    before = tmux(runtime, "list-panes", "-a", "-F", "#{pane_id}").stdout
    with pytest.raises(RodexRuntimeError):
        launcher.stop(runtime)
    assert tmux(runtime, "list-panes", "-a", "-F", "#{pane_id}").stdout == before


def test_discovery_and_control_revalidation_reject_swapped_primary(isolated_runtimes):
    launcher, create, tmux = isolated_runtimes
    runtime = create()
    control = launcher.discover_runtime_control(runtime)
    tmux(runtime, "new-session", "-d", "-s", "foreign", "sleep 30")
    tmux(runtime, "swap-pane", "-s", "%0", "-t", "=foreign:")
    with pytest.raises(RodexRuntimeError, match="ownership"):
        launcher.discover_runtime_control(runtime)
    with pytest.raises(RodexRuntimeError, match="ownership"):
        revalidate_live_control(launcher, runtime, control)


def test_exact_registered_confirmation_is_idempotent(isolated_runtimes):
    launcher, create, tmux = isolated_runtimes
    runtime = create()
    before = tmux(runtime, "show-options", "-t", "%0").stdout
    launcher.confirm_runtime_registration(
        runtime,
        1,
        expected_rodex_session_id=RodexSessionId(1),
        expected_registry_id=RodexRegistryId(1),
        expected_codex_session_id=uuid.UUID(int=1),
    )
    assert tmux(runtime, "show-options", "-t", "%0").stdout == before
    with pytest.raises(RodexRuntimeError):
        launcher.confirm_runtime_registration(
            runtime,
            2,
            expected_rodex_session_id=RodexSessionId(1),
            expected_registry_id=RodexRegistryId(1),
            expected_codex_session_id=uuid.UUID(int=1),
        )


@pytest.mark.parametrize(
    "returncode,output", [(1, ""), (0, "invalid"), (0, "$0\t0000000000000001\n$1\t0000000000000001")]
)
def test_unknown_or_ambiguous_liveness_never_becomes_absence(tmp_path, returncode, output):
    launcher = RodexRuntimeLauncher(
        "unused",
        "tmux",
        runner=lambda command, **options: subprocess.CompletedProcess(command, returncode, output, ""),
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(tmp_path / "s"))
        listener.listen()
        with pytest.raises(RodexRuntimeError):
            launcher.session_exists(LiveTmuxSession(tmp_path / "s", "name", runtime_id=RodexRuntimeId(1)))


def test_stale_socket_is_unreachable_but_not_cleanup_authority(tmp_path):
    path = tmp_path / "s"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as closed_listener:
        closed_listener.bind(str(path))
    launcher = RodexRuntimeLauncher("unused", shutil.which("tmux"))
    runtime = LiveTmuxSession(path, "name", runtime_id=RodexRuntimeId(1))
    assert not launcher.session_exists(runtime)
    with pytest.raises(RodexRuntimeError, match="creation receipt"):
        launcher.stop(runtime)
    assert path.is_socket()


def test_non_socket_endpoint_never_becomes_absence(tmp_path):
    path = tmp_path / "s"
    path.touch()
    launcher = RodexRuntimeLauncher("unused", shutil.which("tmux"))
    with pytest.raises(RodexRuntimeError, match="current-user socket"):
        launcher.session_exists(LiveTmuxSession(path, "name", runtime_id=RodexRuntimeId(1)))


@pytest.mark.parametrize("failure", [FileNotFoundError("tmux missing"), subprocess.TimeoutExpired("tmux", 5)])
def test_executor_failure_never_uses_missing_socket_as_absence(tmp_path, failure):
    def fail(*_args, **_options):
        raise failure

    launcher = RodexRuntimeLauncher("unused", "tmux", runner=fail)
    with pytest.raises(RodexRuntimeError):
        launcher.session_exists(LiveTmuxSession(tmp_path / "absent", "name", runtime_id=RodexRuntimeId(1)))
