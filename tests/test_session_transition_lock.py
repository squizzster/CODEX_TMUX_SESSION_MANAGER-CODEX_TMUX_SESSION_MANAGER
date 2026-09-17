from __future__ import annotations

import os
import select
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident

import pytest

import rodex.live_runtime as live_module
from rodex_registry import RodexSessionId

SESSION_ID = RodexSessionId.parse("0123456789abcdef")


@pytest.mark.parametrize("symlink_alias", [False, True])
def test_nested_canonical_session_transition_uses_one_lock_and_releases_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symlink_alias: bool
) -> None:
    database = tmp_path / "rodex.sqlite3"
    alias = tmp_path / "unneeded" / ".." / database.name
    if symlink_alias:
        alias = tmp_path / "alias.sqlite3"
        alias.symlink_to(database)
    operations: list[int] = []
    flock = live_module.fcntl.flock

    def observe(descriptor: int, operation: int) -> None:
        operations.append(operation)
        flock(descriptor, operation)

    monkeypatch.setattr(live_module.fcntl, "flock", observe)
    with (
        pytest.raises(RuntimeError, match="intentional"),
        live_module.session_transition_lock(database, SESSION_ID),
        live_module.session_transition_lock(alias, SESSION_ID),
    ):
        raise RuntimeError("intentional")

    assert operations == [live_module.fcntl.LOCK_EX, live_module.fcntl.LOCK_UN]
    with live_module.session_transition_lock(database, SESSION_ID):
        pass
    assert operations == [live_module.fcntl.LOCK_EX, live_module.fcntl.LOCK_UN] * 2


@pytest.mark.parametrize("nested_failure", [False, True])
def test_other_thread_cannot_inherit_nested_transition_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested_failure: bool
) -> None:
    database = tmp_path / "rodex.sqlite3"
    owner_thread = get_ident()
    attempted = Event()
    entered = Event()
    flock = live_module.fcntl.flock

    def observe(descriptor: int, operation: int) -> None:
        if get_ident() != owner_thread and operation == live_module.fcntl.LOCK_EX:
            attempted.set()
        flock(descriptor, operation)

    def contender() -> None:
        with live_module.session_transition_lock(database, SESSION_ID):
            entered.set()

    monkeypatch.setattr(live_module.fcntl, "flock", observe)
    with ThreadPoolExecutor(max_workers=1) as worker:
        with live_module.session_transition_lock(database, SESSION_ID):
            if nested_failure:
                with pytest.raises(RuntimeError), live_module.session_transition_lock(database, SESSION_ID):
                    raise RuntimeError("nested failure preserves outer lock")
            future = worker.submit(contender)
            assert attempted.wait(timeout=5)
            assert not entered.wait(timeout=0.05)
        future.result(timeout=5)
    assert entered.is_set()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Linux fork ownership boundary")
@pytest.mark.parametrize("release_inherited_context", [False, True])
def test_forked_child_must_acquire_its_own_transition_lock(tmp_path: Path, release_inherited_context: bool) -> None:
    database = tmp_path / "rodex.sqlite3"
    read_pipe, write_pipe = os.pipe()
    child_pid: int | None = None
    try:
        transition = live_module.session_transition_lock(database, SESSION_ID)
        with transition:
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_pipe)
                flock = live_module.fcntl.flock

                def observe(descriptor: int, operation: int) -> None:
                    if operation == live_module.fcntl.LOCK_EX:
                        os.write(write_pipe, b"attempt")
                    flock(descriptor, operation)

                live_module.fcntl.flock = observe
                try:
                    if release_inherited_context:
                        transition.__exit__(None, None, None)
                    with live_module.session_transition_lock(database, SESSION_ID):
                        os.write(write_pipe, b"entered")
                except BaseException:
                    os._exit(1)
                os._exit(0)
            os.close(write_pipe)
            write_pipe = -1
            assert select.select([read_pipe], [], [], 5)[0]
            assert os.read(read_pipe, 7) == b"attempt"
            assert not select.select([read_pipe], [], [], 0.05)[0]
        assert select.select([read_pipe], [], [], 5)[0]
        assert os.read(read_pipe, 7) == b"entered"
        exit_status = os.waitpid(child_pid, 0)[1]
        child_pid = None
        assert exit_status == 0
    finally:
        if child_pid:
            os.kill(child_pid, 9)
            os.waitpid(child_pid, 0)
        os.close(read_pipe)
        if write_pipe >= 0:
            os.close(write_pipe)
