from __future__ import annotations

import fcntl
import os
import pty
import shlex
import shutil
import signal
import struct
import subprocess
import termios
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pyte
import pytest

from rodex.tmux_session_capability import (
    RODEX_PANE_RUNTIME_ID_OPTION,
    RODEX_SHARED_TMUX_PROTOCOL,
    TmuxSessionCapability,
)
from rodex.tmux_shared_ctrl_c import (
    CTRL_C_OWNERSHIP_REJECTION,
    shared_ctrl_c_binding_command,
)
from rodex_registry import RodexRegistryId, RodexRuntimeId, RodexSessionId


def _capability(socket_path: Path, session_id: str = "$7", pane_id: str = "%9") -> TmuxSessionCapability:
    return TmuxSessionCapability(
        socket_path,
        "0123456789abcdef0123456789abcdef",
        session_id,
        pane_id,
        RodexRuntimeId.parse("0c01ee2ead7240e1"),
        RodexSessionId.parse("1111111111111111"),
        RodexRegistryId.parse("2222222222222222"),
        7,
        uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
    )


def test_ctrl_c_countdown_uses_native_tmux_without_a_shell_helper() -> None:
    command = shared_ctrl_c_binding_command(_capability(Path("/unused.sock")))

    assert "detach-client" in command
    assert "session_attached" in command
    assert "client_session" in command
    assert "@rodex_pane_runtime_id" in command
    assert "run-shell -b -C -d 4" in command
    assert "rodex-exit-" in command
    for removed_mechanism in (
        "confirm-before",
        "show-options",
        "monotonic",
        "confirmation_claim",
        "python",
    ):
        assert removed_mechanism not in command


def test_binding_preserves_originating_client_admission_and_immediate_shared_detach() -> None:
    command = shared_ctrl_c_binding_command(_capability(Path("/unused.sock")))
    parsed = shlex.split(command)
    assert parsed[:2] == ["if-shell", "-F"]
    assert parsed[3].startswith("if-shell -F '#{==:#{session_attached},1}' { bind-key")
    assert "switch-client -T rodex-exit-" in parsed[3]
    assert "display-message -d 1100" in parsed[3]
    assert parsed[3].endswith("} { detach-client }")
    assert shlex.split(parsed[4]) == ["display-message", CTRL_C_OWNERSHIP_REJECTION]
    assert len(command) < 16_000


@dataclass
class _Client:
    process: subprocess.Popen[bytes]
    master: int
    name: str
    screen: pyte.Screen = field(default_factory=lambda: pyte.Screen(180, 40))

    def __post_init__(self) -> None:
        self.stream = pyte.Stream(self.screen)

    def drain(self) -> str:
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            self.stream.feed(chunk.decode("utf-8", errors="replace"))
        return "\n".join(self.screen.display)

    def send(self, keys: bytes) -> None:
        os.write(self.master, keys)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        os.close(self.master)


class _IsolatedTmux:
    """Own a disposable server; its socket directory stays inside pytest state."""

    def __init__(self, binary: str, directory: Path) -> None:
        # The project path itself can exceed sockaddr_un.sun_path. Pin the
        # directory with an fd and use its short /proc address, without creating
        # any shared /tmp alias or touching the user's tmux socket.
        self.directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        self.socket = Path(f"/proc/{os.getpid()}/fd/{self.directory_fd}/s")
        self.prefix = [binary, "-S", str(self.socket)]
        self.environment = {key: value for key, value in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}
        self.environment["TERM"] = "xterm-256color"
        self.clients: list[_Client] = []
        self.command("-f", "/dev/null", "new-session", "-d", "-s", "managed", "-x", "180", "-y", "40", "sleep 120")
        identities = self.command("display-message", "-p", "-t", "=managed:", "#{session_id} #{pane_id}").stdout.split()
        self.capability = _capability(self.socket, *identities)
        self.command("set-option", "-s", "@rodex_shared_tmux_protocol", RODEX_SHARED_TMUX_PROTOCOL)
        self.command("set-option", "-s", "@rodex_shared_tmux_server_id", self.capability.tmux_server_id)
        self.command("set-option", "-s", "@rodex_server_runtime_id", str(self.capability.runtime_id))
        for option, value in (
            ("@rodex_primary_pane_id", self.capability.pane_target),
            ("@rodex_runtime_id", str(self.capability.runtime_id)),
            ("@rodex_registration_state", "registered"),
            ("@rodex_session_id", str(self.capability.rodex_session_id)),
            ("@rodex_registry_id", str(self.capability.registry_id)),
            ("@rodex_sessions_id", str(self.capability.internal_session_id)),
            ("@rodex_codex_session_id", str(self.capability.codex_session_id)),
        ):
            self.command("set-option", "-t", self.capability.session_target, option, value)
        self.command(
            "set-option",
            "-p",
            "-t",
            self.capability.pane_target,
            RODEX_PANE_RUNTIME_ID_OPTION,
            str(self.capability.runtime_id),
        )
        self.command("set-option", "-t", self.capability.session_target, "status-left", "application-status")
        self.command("set-option", "-t", self.capability.session_target, "status-right", "")
        self.command("set-option", "-t", self.capability.session_target, "assume-paste-time", "0")
        installed = self.command("bind-key", "-n", "C-c", shared_ctrl_c_binding_command(self.capability), check=False)
        if installed.returncode:
            self.close()
            pytest.fail(f"failed to install test binding: {installed.stderr}")

    def command(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        for client in self.clients:
            client.drain()
        return subprocess.run(
            [*self.prefix, *arguments], check=check, capture_output=True, text=True, timeout=3, env=self.environment
        )

    def wait(self, predicate: Callable[[], bool], reason: str) -> None:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            for client in self.clients:
                client.drain()
            if predicate():
                return
            time.sleep(0.01)
        pytest.fail(reason + "\n" + "\n".join(client.drain() for client in self.clients))

    def attach(self) -> _Client:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 180, 0, 0))
        name = os.ttyname(slave)
        process = subprocess.Popen(
            [*self.prefix, "attach-session", "-t", self.capability.session_target],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=self.environment,
            start_new_session=True,
        )
        os.close(slave)
        os.set_blocking(master, False)
        client = _Client(process, master, name)
        self.clients.append(client)
        self.wait(
            lambda: name in self.command("list-clients", "-F", "#{client_name}").stdout.splitlines(),
            "client did not attach",
        )
        self.wait(lambda: "applicatio" in client.drain(), "client did not render its initial terminal")
        return client

    def exists(self) -> bool:
        return self.command("has-session", "-t", self.capability.session_target, check=False).returncode == 0

    def arm_exit(self, client: _Client) -> None:
        client.send(b"\x03")
        self.wait(lambda: "in 4 seconds:" in client.drain(), "private Ctrl-C did not show the exit warning")

    def attached(self) -> set[str]:
        return set(self.command("list-clients", "-F", "#{client_name}").stdout.splitlines())

    def block_queue(self, client: _Client, gate: str) -> None:
        self.command("bind-key", "-n", "C-x", f"wait-for {gate}")
        client.send(b"\x18")
        self.wait(
            lambda: f"wait-for {gate}" in self.command("show-messages", "-t", client.name).stdout,
            "client command queue was not blocked",
        )

    def reattach_same_terminal(self, client: _Client) -> _Client:
        client.process.wait(timeout=2)
        self.wait(lambda: client.name not in self.attached(), "old client incarnation did not detach")
        # O_NOCTTY prevents the test process from acquiring this terminal as its
        # controlling tty and receiving a hangup when the replacement exits.
        slave = os.open(client.name, os.O_RDWR | os.O_NOCTTY)
        try:
            process = subprocess.Popen(
                [*self.prefix, "attach-session", "-t", self.capability.session_target],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=self.environment,
                start_new_session=True,
            )
        finally:
            os.close(slave)
        replacement = _Client(process, client.master, client.name)
        self.clients[self.clients.index(client)] = replacement
        self.wait(lambda: "applicatio" in replacement.drain(), "replacement incarnation did not render")
        return replacement

    def assert_quiet(self) -> None:
        # Let the PTY input reach the native server queue before asserting that a
        # rejected event had no effect. This path has no helper jobs.
        time.sleep(0.03)
        assert self.exists()
        assert (
            self.command("display-message", "-p", "-t", self.capability.pane_target, "#{pane_in_mode}").stdout.strip()
            == "0"
        )
        options = self.command("show-options", "-t", self.capability.session_target).stdout
        assert "@rodex_shared_ctrl_c_confirmation" not in options
        assert "@rodex_status_claim" not in options
        assert (
            self.command("show-options", "-v", "-t", self.capability.session_target, "status-left").stdout.strip()
            == "application-status"
        )

    def close(self) -> None:
        # This exact proc-fd socket belongs exclusively to this fixture.
        self.command("kill-server", check=False)
        for client in self.clients:
            client.close()
        os.close(self.directory_fd)


@pytest.fixture
def tmux(tmp_path: Path) -> Iterator[_IsolatedTmux]:
    binary = shutil.which("tmux")
    if binary is None:
        pytest.skip("tmux is not installed")
    server = _IsolatedTmux(binary, tmp_path)
    try:
        yield server
    finally:
        server.close()


def test_private_ctrl_c_ends_the_actual_sole_clients_session(tmux: _IsolatedTmux) -> None:
    client = tmux.attach()
    started = time.monotonic()
    tmux.arm_exit(client)
    assert "'d' to detach, 'q' to quit now, other key to abort." in client.drain()
    for remaining in (3, 2, 1):
        tmux.wait(lambda remaining=remaining: f"in {remaining} seconds:" in client.drain(), "countdown did not update")
        assert tmux.exists()
    tmux.wait(lambda: not tmux.exists(), "private Ctrl-C did not terminate")
    assert time.monotonic() - started >= 3.8


def test_q_exits_immediately_during_warning(tmux: _IsolatedTmux) -> None:
    client = tmux.attach()
    tmux.arm_exit(client)
    started = time.monotonic()
    client.send(b"q")
    tmux.wait(lambda: not tmux.exists(), "q did not exit immediately")
    assert time.monotonic() - started < 2


def test_d_detaches_and_old_countdown_cannot_exit_reused_terminal(tmux: _IsolatedTmux) -> None:
    client = tmux.attach()
    tmux.arm_exit(client)
    client.send(b"d")
    replacement = tmux.reattach_same_terminal(client)
    time.sleep(4.2)
    tmux.assert_quiet()
    assert tmux.attached() == {replacement.name}
    assert "Rodex will" not in replacement.drain()


def test_other_keys_consume_and_abort_the_warning(tmux: _IsolatedTmux) -> None:
    client = tmux.attach()
    for key in (b"x", b"\x03", b"\x04", b"\r", b"\x1b[A", b"\x1b"):
        tmux.arm_exit(client)
        client.send(key)
        tmux.wait(lambda: "Rodex will" not in client.drain(), "abort did not restore the task bar")
        assert tmux.command("list-clients", "-F", "#{client_key_table}").stdout.strip() == "root"
    time.sleep(4.2)
    tmux.assert_quiet()
    assert tmux.attached() == {client.name}
    assert not tmux.command("capture-pane", "-p", "-t", tmux.capability.pane_target).stdout.strip()


def test_cancel_then_rearm_gets_a_fresh_four_seconds(tmux: _IsolatedTmux) -> None:
    client = tmux.attach()
    tmux.arm_exit(client)
    time.sleep(1.2)
    client.send(b"x")
    tmux.wait(lambda: "Rodex will" not in client.drain(), "abort did not clear warning")
    tmux.arm_exit(client)
    time.sleep(3.1)
    assert tmux.exists(), "old timer shortened the new countdown"
    tmux.wait(lambda: not tmux.exists(), "new countdown did not exit")


@pytest.mark.parametrize("key", [b"q", b""])
def test_new_shared_client_prevents_countdown_exit(tmux: _IsolatedTmux, key: bytes) -> None:
    first = tmux.attach()
    tmux.arm_exit(first)
    second = tmux.attach()
    first.send(key)
    tmux.wait(lambda: first.name not in tmux.attached(), "now-shared client did not detach")
    tmux.assert_quiet()
    assert tmux.attached() == {second.name}
    assert "Rodex will" not in second.drain()


@pytest.mark.parametrize("key", [b"q", b""])
def test_exit_rechecks_ownership_after_warning(tmux: _IsolatedTmux, key: bytes) -> None:
    client = tmux.attach()
    tmux.arm_exit(client)
    tmux.command("set-option", "-p", "-t", tmux.capability.pane_target, RODEX_PANE_RUNTIME_ID_OPTION, "foreign")
    client.send(key)
    tmux.wait(lambda: CTRL_C_OWNERSHIP_REJECTION in client.drain(), "exit did not recheck ownership")
    tmux.assert_quiet()
    assert tmux.attached() == {client.name}


def test_external_termination_does_not_enter_keyboard_warning(tmux: _IsolatedTmux) -> None:
    client = tmux.attach()
    client.process.terminate()
    tmux.wait(lambda: client.name not in tmux.attached(), "signalled client did not detach")
    assert "@rodex_exit_sequence" not in tmux.command("show-options").stdout
    tmux.assert_quiet()


def test_expiry_rejects_switching_to_an_owned_observer_pane(tmux: _IsolatedTmux) -> None:
    client = tmux.attach()
    tmux.arm_exit(client)
    observer = tmux.command(
        "split-window", "-P", "-F", "#{pane_id}", "-t", tmux.capability.pane_target, "sleep 120"
    ).stdout.strip()
    tmux.command("set-option", "-p", "-t", observer, RODEX_PANE_RUNTIME_ID_OPTION, str(tmux.capability.runtime_id))
    tmux.wait(lambda: CTRL_C_OWNERSHIP_REJECTION in client.drain(), "expiry did not check the client's current pane")
    tmux.assert_quiet()


def test_exiting_client_with_pending_countdown_cannot_exit_replacement(tmux: _IsolatedTmux) -> None:
    first = tmux.attach()
    tmux.arm_exit(first)
    os.kill(first.process.pid, signal.SIGSTOP)
    try:
        tmux.command("detach-client", "-t", first.name)
        second = tmux.attach()
        assert first.name in tmux.attached(), "test missed connected-but-exiting interval"
        time.sleep(4.2)
        tmux.assert_quiet()
        assert second.name in tmux.attached()
        assert "Rodex will" not in second.drain()
    finally:
        os.kill(first.process.pid, signal.SIGCONT)


@pytest.mark.parametrize("client_count", [2, 3])
def test_shared_ctrl_c_detaches_only_originating_client(tmux: _IsolatedTmux, client_count: int) -> None:
    first, *remaining = [tmux.attach() for _ in range(client_count)]
    first.send(b"\x03")
    tmux.wait(lambda: first.name not in tmux.attached(), "shared Ctrl-C did not detach its originating client")
    assert tmux.attached() == {client.name for client in remaining}
    tmux.assert_quiet()
    for client in remaining:
        assert CTRL_C_OWNERSHIP_REJECTION not in client.drain()
        assert "ARMED" not in client.drain()


def test_remaining_client_can_exit_with_its_own_new_private_key(tmux: _IsolatedTmux) -> None:
    first, second = tmux.attach(), tmux.attach()
    first.send(b"\x03")
    tmux.wait(lambda: first.name not in tmux.attached(), "first client did not detach")
    tmux.assert_quiet()
    second.send(b"\x03")
    tmux.wait(lambda: not tmux.exists(), "remaining client's own private key did not terminate")


def test_reused_client_name_cannot_inherit_departed_clients_queued_key(tmux: _IsolatedTmux) -> None:
    first, second = tmux.attach(), tmux.attach()
    tmux.block_queue(first, "reused-client-gate")
    first.send(b"\x03\x03")
    tmux.command("detach-client", "-t", first.name)
    replacement = tmux.reattach_same_terminal(first)
    tmux.command("wait-for", "-S", "reused-client-gate")
    tmux.assert_quiet()
    assert tmux.attached() == {replacement.name, second.name}
    assert CTRL_C_OWNERSHIP_REJECTION not in replacement.drain()
    replacement.send(b"\x03")
    tmux.wait(lambda: replacement.name not in tmux.attached(), "replacement's own key did not detach it")
    assert tmux.attached() == {second.name}
    tmux.assert_quiet()


@pytest.mark.parametrize("queued_keys", [b"\x03", b"\x03\x03"])
def test_departing_clients_queued_ctrl_c_never_becomes_remaining_clients_private_exit(
    tmux: _IsolatedTmux, queued_keys: bytes
) -> None:
    first, second = tmux.attach(), tmux.attach()
    tmux.block_queue(first, "departing-client-gate")
    first.send(queued_keys)
    tmux.command("detach-client", "-t", first.name)
    tmux.wait(lambda: first.name not in tmux.attached(), "first client did not detach")
    tmux.command("wait-for", "-S", "departing-client-gate")
    tmux.assert_quiet()
    assert tmux.attached() == {second.name}
    assert CTRL_C_OWNERSHIP_REJECTION not in second.drain()


def test_exiting_client_still_connected_cannot_dispatch_queued_keys_to_remaining_client(tmux: _IsolatedTmux) -> None:
    first, second = tmux.attach(), tmux.attach()
    tmux.block_queue(first, "exiting-client-gate")
    first.send(b"\x03\x03")
    # Let the client forward its PTY keys, then stop it before asking the server
    # to detach it. Its socket stays open and it cannot complete MSG_EXITING.
    time.sleep(0.05)
    os.kill(first.process.pid, signal.SIGSTOP)
    try:
        tmux.command("detach-client", "-t", first.name)
        assert first.name in tmux.attached(), "test missed the connected-but-exiting client interval"
        tmux.command("wait-for", "-S", "exiting-client-gate")
        tmux.assert_quiet()
        assert second.name in tmux.attached()
        assert CTRL_C_OWNERSHIP_REJECTION not in second.drain()
    finally:
        os.kill(first.process.pid, signal.SIGCONT)
    tmux.wait(lambda: first.name not in tmux.attached(), "originating client did not finish detaching")
    assert tmux.attached() == {second.name}
    tmux.assert_quiet()


@pytest.mark.parametrize("queued_count", [1, 2, 5])
def test_repeated_queued_ctrl_c_detaches_origin_once_and_leaves_remaining_client(
    tmux: _IsolatedTmux, queued_count: int
) -> None:
    first, second = tmux.attach(), tmux.attach()
    tmux.block_queue(first, "repeated-input-gate")
    first.send(b"\x03" * queued_count)
    tmux.command("wait-for", "-S", "repeated-input-gate")
    tmux.wait(lambda: first.name not in tmux.attached(), "queued Ctrl-C did not detach originating client")
    tmux.assert_quiet()
    assert tmux.attached() == {second.name}
    assert CTRL_C_OWNERSHIP_REJECTION not in second.drain()


def test_native_admission_uses_current_membership_of_the_actual_originating_client(tmux: _IsolatedTmux) -> None:
    first, second = tmux.attach(), tmux.attach()
    tmux.block_queue(first, "sole-origin-gate")
    first.send(b"\x03")
    tmux.command("detach-client", "-t", second.name)
    tmux.wait(lambda: second.name not in tmux.attached(), "other client did not detach")
    tmux.command("wait-for", "-S", "sole-origin-gate")
    tmux.wait(lambda: not tmux.exists(), "actual originating sole client could not exit at native admission")


@pytest.mark.parametrize(
    "ownership_change",
    [
        "server",
        "server_runtime",
        "protocol",
        "runtime",
        "primary",
        "pane_owner",
        "rodex_session",
        "registry",
        "sql_session",
        "codex_session",
        "extra_session",
        "foreign_pane",
        "foreign_window",
        "changed_pane",
    ],
)
def test_queued_key_rejects_changed_ownership_or_topology_without_rerouting_diagnostic(
    tmux: _IsolatedTmux, ownership_change: str
) -> None:
    first, second = tmux.attach(), tmux.attach()
    tmux.block_queue(first, "ownership-change-gate")
    first.send(b"\x03")
    server_changes = {
        "server": ("@rodex_shared_tmux_server_id", "fedcba9876543210fedcba9876543210"),
        "server_runtime": ("@rodex_server_runtime_id", "1234567890abcdef"),
        "protocol": ("@rodex_shared_tmux_protocol", "obsolete-protocol"),
    }
    session_changes = {
        "runtime": ("@rodex_runtime_id", "1234567890abcdef"),
        "primary": ("@rodex_primary_pane_id", "%999"),
        "rodex_session": ("@rodex_session_id", "1234567890abcdef"),
        "registry": ("@rodex_registry_id", "1234567890abcdef"),
        "sql_session": ("@rodex_sessions_id", "999"),
        "codex_session": ("@rodex_codex_session_id", "01a00654-f2bc-7a30-834a-a5f886a65f83"),
    }
    if ownership_change in server_changes:
        tmux.command("set-option", "-s", *server_changes[ownership_change])
    elif ownership_change in session_changes:
        tmux.command("set-option", "-t", tmux.capability.session_target, *session_changes[ownership_change])
    elif ownership_change == "pane_owner":
        tmux.command(
            "set-option", "-p", "-t", tmux.capability.pane_target, RODEX_PANE_RUNTIME_ID_OPTION, "1234567890abcdef"
        )
    elif ownership_change == "extra_session":
        tmux.command("new-session", "-d", "-s", "unrelated", "sleep 120")
    elif ownership_change == "foreign_window":
        tmux.command("new-window", "-d", "-t", tmux.capability.session_target, "sleep 120")
    else:
        pane = tmux.command(
            "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", tmux.capability.pane_target, "sleep 120"
        ).stdout.strip()
        if ownership_change == "changed_pane":
            tmux.command("set-option", "-p", "-t", pane, RODEX_PANE_RUNTIME_ID_OPTION, str(tmux.capability.runtime_id))
            tmux.command("select-pane", "-t", pane)
    tmux.command("wait-for", "-S", "ownership-change-gate")
    tmux.wait(lambda: CTRL_C_OWNERSHIP_REJECTION in first.drain(), "changed ownership was not explicitly rejected")
    assert tmux.attached() == {first.name, second.name}
    assert CTRL_C_OWNERSHIP_REJECTION not in second.drain()
    tmux.assert_quiet()


def test_foreign_pane_rejects_private_exit_and_keeps_other_client_display_untouched(tmux: _IsolatedTmux) -> None:
    first = tmux.attach()
    foreign = tmux.command(
        "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", tmux.capability.pane_target, "sleep 120"
    ).stdout.strip()
    first.send(b"\x03")
    tmux.wait(lambda: CTRL_C_OWNERSHIP_REJECTION in first.drain(), "foreign pane did not block private exit")
    tmux.assert_quiet()
    assert tmux.command("display-message", "-p", "-t", foreign, "#{pane_in_mode}").stdout.strip() == "0"


def test_ctrl_c_from_owned_observer_pane_does_not_terminate_primary(tmux: _IsolatedTmux) -> None:
    first = tmux.attach()
    pane = tmux.command(
        "split-window", "-P", "-F", "#{pane_id}", "-t", tmux.capability.pane_target, "sleep 120"
    ).stdout.strip()
    tmux.command("set-option", "-p", "-t", pane, RODEX_PANE_RUNTIME_ID_OPTION, str(tmux.capability.runtime_id))
    first.send(b"\x03")
    tmux.wait(lambda: CTRL_C_OWNERSHIP_REJECTION in first.drain(), "observer Ctrl-C was not rejected")
    tmux.assert_quiet()


def test_primary_private_exit_admits_owned_observers_and_owned_extra_window(tmux: _IsolatedTmux) -> None:
    first = tmux.attach()
    observer = tmux.command(
        "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", tmux.capability.pane_target, "sleep 120"
    ).stdout.strip()
    additional = tmux.command(
        "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", tmux.capability.session_target, "sleep 120"
    ).stdout.strip()
    for pane in (observer, additional):
        tmux.command("set-option", "-p", "-t", pane, RODEX_PANE_RUNTIME_ID_OPTION, str(tmux.capability.runtime_id))
    first.send(b"\x03")
    tmux.wait(lambda: not tmux.exists(), "owned observer topology incorrectly blocked private exit")
