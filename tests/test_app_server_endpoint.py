"""The child-owned App Server endpoint may be a socket or a guarded alias."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from rodex.runtime import RodexRuntimeError, _RuntimePathKeepalive
from rodex.runtime_endpoint import BoundUnixAlias, ExclusiveUnixEndpoint
from rodex.runtime_peer import RuntimePeerIdentityError, require_unix_listener_process


def _private_target(tmp_path: Path) -> Path:
    directory = tmp_path / "codex-owned"
    directory.mkdir(mode=0o700)
    return directory / "physical.sock"


def test_child_socket_alias_is_pinned_refreshed_and_only_its_alias_is_cleaned(tmp_path: Path) -> None:
    requested = tmp_path / "app.sock"
    physical = _private_target(tmp_path)
    endpoint = ExclusiveUnixEndpoint(requested, allow_child_alias=True)
    endpoint.acquire()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(physical))
        listener.listen()
        physical.chmod(0o600)
        requested.symlink_to(physical)

        endpoint.retain_bound_path()
        assert endpoint.bound_alias == BoundUnixAlias(requested, physical)
        require_unix_listener_process(requested, SimpleNamespace(pid=os.getpid(), poll=lambda: None))
        with pytest.raises(RuntimePeerIdentityError, match="not the retained live"):
            require_unix_listener_process(requested, SimpleNamespace(pid=os.getpid(), poll=lambda: 1))

        old_timestamp = 946684800
        os.utime(requested, (old_timestamp, old_timestamp), follow_symlinks=False)
        os.utime(physical, (old_timestamp, old_timestamp))
        os.utime(physical.parent, (old_timestamp, old_timestamp))
        keepalive = _RuntimePathKeepalive((endpoint.bound_alias,))
        keepalive.start()
        keepalive.close()

        assert requested.lstat().st_mtime > old_timestamp
        assert physical.stat().st_mtime > old_timestamp
        assert physical.parent.stat().st_mtime > old_timestamp
        endpoint.close()
        assert not requested.is_symlink()
        assert stat.S_ISSOCK(physical.lstat().st_mode)


def test_child_alias_cleanup_preserves_replacement(tmp_path: Path) -> None:
    requested = tmp_path / "app.sock"
    physical = _private_target(tmp_path)
    endpoint = ExclusiveUnixEndpoint(requested, allow_child_alias=True)
    endpoint.acquire()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(physical))
        listener.listen()
        physical.chmod(0o600)
        requested.symlink_to(physical)
        endpoint.retain_bound_path()
        requested.unlink()
        replacement = tmp_path / "replacement"
        replacement.write_text("different owner")
        requested.symlink_to(replacement)
        endpoint.close()
        assert requested.is_symlink()
        assert requested.resolve() == replacement


def test_child_alias_rejects_a_non_socket_target_and_leaves_it_untouched(tmp_path: Path) -> None:
    requested = tmp_path / "app.sock"
    physical = _private_target(tmp_path)
    physical.write_text("not a socket")
    endpoint = ExclusiveUnixEndpoint(requested, allow_child_alias=True)
    endpoint.acquire()
    requested.symlink_to(physical)
    with pytest.raises(OSError, match="private current-user socket"):
        endpoint.retain_bound_path()
    endpoint.close()
    assert requested.is_symlink()
    assert physical.read_text() == "not a socket"


def test_child_alias_rejects_a_non_private_target_directory(tmp_path: Path) -> None:
    requested = tmp_path / "app.sock"
    physical = _private_target(tmp_path)
    physical.parent.chmod(0o755)
    endpoint = ExclusiveUnixEndpoint(requested, allow_child_alias=True)
    endpoint.acquire()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(physical))
        physical.chmod(0o600)
        requested.symlink_to(physical)
        with pytest.raises(OSError, match="directory is not private"):
            endpoint.retain_bound_path()
    endpoint.close()
    assert requested.is_symlink()


@pytest.mark.parametrize("live", [False, True])
def test_child_alias_reconciles_only_a_stale_rendezvous(tmp_path: Path, live: bool) -> None:
    requested = tmp_path / "app.sock"
    physical = _private_target(tmp_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(physical))
        if live:
            listener.listen()
        else:
            listener.close()
        requested.symlink_to(physical)
        endpoint = ExclusiveUnixEndpoint(requested, allow_child_alias=True)
        if live:
            with pytest.raises(OSError, match="live listener"):
                endpoint.acquire()
            assert requested.is_symlink()
        else:
            endpoint.acquire()
            assert not requested.is_symlink()
        assert stat.S_ISSOCK(physical.lstat().st_mode)
        endpoint.close()
    finally:
        listener.close()


def test_keepalive_rejects_unrecognized_or_replaced_alias(tmp_path: Path) -> None:
    requested = tmp_path / "app.sock"
    physical = _private_target(tmp_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(physical))
        physical.chmod(0o600)
        requested.symlink_to(physical)
        with pytest.raises(RodexRuntimeError, match="unverified symlink"):
            _RuntimePathKeepalive((requested,)).start()

        keepalive = _RuntimePathKeepalive((BoundUnixAlias(requested, physical),))
        keepalive.start()
        try:
            requested.unlink()
            requested.symlink_to(physical)
            with pytest.raises(RodexRuntimeError, match="identity changed"):
                keepalive._refresh()
        finally:
            keepalive.close()


def test_keepalive_rejects_physical_socket_replacement(tmp_path: Path) -> None:
    requested = tmp_path / "app.sock"
    physical = _private_target(tmp_path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(physical))
        physical.chmod(0o600)
        requested.symlink_to(physical)
        keepalive = _RuntimePathKeepalive((BoundUnixAlias(requested, physical),))
        keepalive.start()
        try:
            physical.unlink()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as replacement:
                replacement.bind(str(physical))
                with pytest.raises(RodexRuntimeError, match="identity changed"):
                    keepalive._refresh()
        finally:
            keepalive.close()


@pytest.mark.live_startup
def test_installed_codex_app_server_endpoint_is_retained_and_reachable() -> None:
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("installed Codex CLI is required")
    with tempfile.TemporaryDirectory(prefix="rdx-app-", dir="/tmp") as directory:
        requested = Path(directory) / "app.sock"
        endpoint = ExclusiveUnixEndpoint(requested, allow_child_alias=True)
        endpoint.acquire()
        process = subprocess.Popen(
            [codex, "app-server", "--listen", f"unix://{requested}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not requested.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert process.poll() is None and requested.exists(), "installed Codex did not expose its Unix endpoint"
            endpoint.retain_bound_path()
            require_unix_listener_process(requested, process)
            keepalive = _RuntimePathKeepalive((endpoint.bound_alias or requested,))
            keepalive.start()
            keepalive.close()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
            endpoint.close()
