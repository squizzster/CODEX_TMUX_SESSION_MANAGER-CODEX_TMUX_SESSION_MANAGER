"""A wrapper timeout must release its native writer without reaching other runtimes."""

from __future__ import annotations

import fcntl
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

import rodex.runtime as runtime_module
from rodex.runtime import _stop_child_process

WRAPPED_WRITER = """
import fcntl
import os
import signal
import subprocess
import sys
from pathlib import Path

lock_path, ready_path, mode = map(Path, sys.argv[1:4])
if sys.argv[4:] == ["native"]:
    if str(mode) == "stalled":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ready_path.write_text(str(os.getpid()))
        while True:
            signal.pause()
else:
    child = subprocess.Popen([sys.executable, __file__, *sys.argv[1:], "native"])
    forwarded = False
    def forward(signum, _frame):
        global forwarded
        if not forwarded:
            forwarded = True
            child.send_signal(signum)
    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, forward)
    child.wait()
"""


@pytest.mark.parametrize("outcome", ["delayed_ready", "exited", "never_ready"])
def test_app_server_cold_start_waits_for_readiness_with_a_bounded_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    clock = [0.0]
    socket_path = tmp_path / "app.sock"

    def advance(seconds: float) -> None:
        clock[0] += seconds
        if outcome == "delayed_ready" and clock[0] >= 17:
            socket_path.touch()

    process = SimpleNamespace(poll=lambda: 1 if outcome == "exited" and clock[0] >= 1 else None)
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(runtime_module.time, "sleep", advance)
    if outcome == "delayed_ready":
        runtime_module._wait_for_app_server_socket(process, socket_path)
        assert 17 <= clock[0] < 18
    else:
        expected = "exited during startup" if outcome == "exited" else "timed out waiting"
        with pytest.raises(runtime_module.RodexRuntimeError, match=expected):
            runtime_module._wait_for_app_server_socket(process, socket_path)
        assert 1 <= clock[0] < 2 if outcome == "exited" else 30 <= clock[0] < 31


@pytest.mark.parametrize("mode", ["graceful", "stalled"])
def test_owned_app_server_shutdown_releases_native_writer_and_preserves_other_processes(
    tmp_path: Path, mode: str
) -> None:
    wrapper = tmp_path / "wrapped_writer.py"
    wrapper.write_text(WRAPPED_WRITER)
    writer_lock = tmp_path / "writer.lock"
    ready = tmp_path / "writer.pid"
    process = subprocess.Popen(
        [sys.executable, str(wrapper), str(writer_lock), str(ready), mode],
        start_new_session=True,
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    native_descriptor = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert process.poll() is None, "wrapped writer exited before taking its lock"
            assert time.monotonic() < deadline, "wrapped writer never became ready"
            time.sleep(0.01)
        native_pid = int(ready.read_text())
        native_descriptor = os.pidfd_open(native_pid)
        assert os.getpgid(native_pid) == process.pid == os.getpgid(process.pid)
        assert os.getpgid(unrelated.pid) != process.pid
        with writer_lock.open("r+") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

            _stop_child_process(process, owns_process_group=True)

            assert process.returncode == (-signal.SIGKILL if mode == "stalled" else 0)
            exited = select.poll()
            exited.register(native_descriptor, select.POLLIN)
            assert exited.poll(3000), "native writer survived its wrapper's cleanup"
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert unrelated.poll() is None
    finally:
        if native_descriptor is not None:
            with suppress(ProcessLookupError):
                signal.pidfd_send_signal(native_descriptor, signal.SIGKILL)
            os.close(native_descriptor)
        for child in (process, unrelated):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
