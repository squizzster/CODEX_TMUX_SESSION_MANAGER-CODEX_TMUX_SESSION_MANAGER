from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Protocol

import pytest

import rodex.protocol_proxy as protocol_proxy_module
from rodex.protocol_proxy import CodexContextStatusObserver
from rodex_registry import (
    RodexRuntimeId,
    create_a_rodex_session,
    lookup_rodex_runtime_instance,
    lookup_rodex_session_log,
    record_a_rodex_session_access,
    record_a_rodex_session_runtime_resume,
)


def _token_count_line(percent: int) -> str:
    return json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": percent},
                    "model_context_window": 100,
                },
            },
        },
        separators=(",", ":"),
    )


def _rewrite_with_distinct_metadata(path: Path, content: str) -> None:
    """Rewrite in place while advancing metadata on coarse-clock filesystems."""
    before = path.stat()
    path.write_text(content, encoding="utf-8")
    after = path.stat()
    os.utime(
        path,
        ns=(
            after.st_atime_ns,
            max(before.st_mtime_ns, after.st_mtime_ns) + 1_000_000_000,
        ),
    )


class _Mutation(Protocol):
    def __call__(self) -> None: ...


class _MutatingStop:
    def __init__(self, mutation: _Mutation) -> None:
        self._mutation = mutation
        self._waits = 0
        self._stopped = False

    def is_set(self) -> bool:
        return self._stopped

    def wait(self, _delay: float) -> bool:
        self._waits += 1
        if self._waits == 1:
            self._mutation()
        if self._waits >= 4:
            self._stopped = True
        return self._stopped


class _SequencedMutatingStop:
    def __init__(self, mutations: tuple[_Mutation, ...]) -> None:
        self._mutations = mutations
        self._waits = 0
        self._stopped = False

    def is_set(self) -> bool:
        return self._stopped

    def wait(self, _delay: float) -> bool:
        if self._waits < len(self._mutations):
            self._mutations[self._waits]()
        self._waits += 1
        if self._waits >= len(self._mutations) + 3:
            self._stopped = True
        return self._stopped


class _CountingStop:
    def __init__(self, wait_limit: int) -> None:
        self.wait_limit = wait_limit
        self.waits = 0

    def is_set(self) -> bool:
        return self.waits >= self.wait_limit

    def wait(self, _delay: float) -> bool:
        self.waits += 1
        return self.is_set()


class _DeterministicRolloutObserver(CodexContextStatusObserver):
    """Drive follower mutations at the production rollout-wait boundary."""

    def _wait_for_rollout_activity(
        self,
        stop: Event,
        wake_generation: int,
        timeout_seconds: float,
    ) -> tuple[bool, int]:
        return stop.wait(timeout_seconds), wake_generation


def test_round2_rollout_follower_recovers_when_the_path_is_replaced(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    replacement = tmp_path / "rollout.replacement"
    rollout.write_text(_token_count_line(10) + "\n", encoding="utf-8")
    replacement.write_text(_token_count_line(70) + "\n", encoding="utf-8")
    observed: list[str] = []
    observer = _DeterministicRolloutObserver(
        observed.append,
        codex_sessions_root=tmp_path,
    )
    stop = _MutatingStop(lambda: os.replace(replacement, rollout))
    observer._rollout_stop = stop  # type: ignore[assignment]

    observer._follow_rollout_context("thread-1", rollout, stop)  # type: ignore[arg-type]

    assert any("Context: 10% |" in status for status in observed)
    assert any("Context: 70% |" in status for status in observed), (
        "a follower at EOF must notice that the rollout path now names a new inode"
    )


def test_round2_rollout_follower_recovers_when_the_file_is_truncated(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        _token_count_line(10) + "\n" + ("{}\n" * 200),
        encoding="utf-8",
    )
    observed: list[str] = []
    observer = _DeterministicRolloutObserver(
        observed.append,
        codex_sessions_root=tmp_path,
    )

    def truncate() -> None:
        rollout.write_text(_token_count_line(80) + "\n", encoding="utf-8")

    stop = _MutatingStop(truncate)
    observer._rollout_stop = stop  # type: ignore[assignment]

    observer._follow_rollout_context("thread-1", rollout, stop)  # type: ignore[arg-type]

    assert any("Context: 10% |" in status for status in observed)
    assert any("Context: 80% |" in status for status in observed), (
        "a follower whose offset is past the new file size must reopen from a "
        "bounded authenticated baseline"
    )


def test_round2_rollout_follower_preserves_append_after_inode_replacement(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    replacement = tmp_path / "rollout.replacement"
    rollout.write_text(_token_count_line(10) + "\n", encoding="utf-8")
    replacement.write_text(_token_count_line(70) + "\n", encoding="utf-8")
    observed: list[str] = []
    observer = _DeterministicRolloutObserver(
        observed.append,
        codex_sessions_root=tmp_path,
    )

    def replace_rollout() -> None:
        os.replace(replacement, rollout)

    def append_to_replacement() -> None:
        with rollout.open("a", encoding="utf-8") as output:
            output.write(_token_count_line(90) + "\n")

    stop = _SequencedMutatingStop((replace_rollout, lambda: None, append_to_replacement))
    observer._rollout_stop = stop  # type: ignore[assignment]

    observer._follow_rollout_context("thread-1", rollout, stop)  # type: ignore[arg-type]

    assert [
        percent
        for percent in (10, 70, 90)
        if any(f"Context: {percent}% |" in status for status in observed)
    ] == [10, 70, 90]


def test_round2_rollout_follower_completes_a_startup_partial_record(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        _token_count_line(10) + "\n" + _token_count_line(70),
        encoding="utf-8",
    )
    observed: list[str] = []
    observer = _DeterministicRolloutObserver(
        observed.append,
        codex_sessions_root=tmp_path,
    )

    def complete_partial_record() -> None:
        with rollout.open("a", encoding="utf-8") as output:
            output.write("\n")

    stop = _MutatingStop(complete_partial_record)
    observer._rollout_stop = stop  # type: ignore[assignment]

    observer._follow_rollout_context("thread-1", rollout, stop)  # type: ignore[arg-type]

    assert sum("Context: 10% |" in status for status in observed) == 1
    assert sum("Context: 70% |" in status for status in observed) == 1


def test_round2_rollout_follower_completes_a_replacement_partial_record(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    replacement = tmp_path / "rollout.replacement"
    rollout.write_text(_token_count_line(10) + "\n", encoding="utf-8")
    replacement.write_text(_token_count_line(70), encoding="utf-8")
    observed: list[str] = []
    observer = _DeterministicRolloutObserver(
        observed.append,
        codex_sessions_root=tmp_path,
    )

    def replace_rollout() -> None:
        os.replace(replacement, rollout)

    def complete_partial_record() -> None:
        with rollout.open("a", encoding="utf-8") as output:
            output.write("\n")

    stop = _SequencedMutatingStop((replace_rollout, lambda: None, complete_partial_record))
    observer._rollout_stop = stop  # type: ignore[assignment]

    observer._follow_rollout_context("thread-1", rollout, stop)  # type: ignore[arg-type]

    assert sum("Context: 10% |" in status for status in observed) == 1
    assert sum("Context: 70% |" in status for status in observed) == 1


def test_round2_rollout_follower_detects_same_inode_equal_size_rewrite(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    initial = _token_count_line(10) + "\n"
    rewritten = _token_count_line(80) + "\n"
    assert len(initial) == len(rewritten)
    rollout.write_text(initial, encoding="utf-8")
    initial_inode = rollout.stat().st_ino
    observed: list[str] = []
    observer = _DeterministicRolloutObserver(
        observed.append,
        codex_sessions_root=tmp_path,
    )

    def rewrite_same_inode() -> None:
        _rewrite_with_distinct_metadata(rollout, rewritten)
        assert rollout.stat().st_ino == initial_inode

    stop = _MutatingStop(rewrite_same_inode)
    observer._rollout_stop = stop  # type: ignore[assignment]

    observer._follow_rollout_context("thread-1", rollout, stop)  # type: ignore[arg-type]

    assert sum("Context: 10% |" in status for status in observed) == 1
    assert sum("Context: 80% |" in status for status in observed) == 1


def test_round2_rollout_checkpoint_advancement_rejects_a_pre_refresh_rewrite(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    initial = _token_count_line(10) + "\n"
    rewritten = _token_count_line(80) + "\n"
    assert len(initial) == len(rewritten)
    rollout.write_text(initial, encoding="utf-8")

    with protocol_proxy_module._open_rollout_for_following(
        rollout,
        allowed_root=tmp_path,
    ) as source:
        source.seek(0, 2)
        checkpoint = protocol_proxy_module._rollout_follow_checkpoint(source)
        _rewrite_with_distinct_metadata(rollout, rewritten)

        advanced = protocol_proxy_module._advance_rollout_follow_checkpoint(
            rollout,
            source,
            checkpoint,
        )

    assert advanced is None, (
        "checkpoint refresh must validate the preceding trusted boundary before "
        "replacing its fingerprint"
    )


def test_round2_rollout_follower_detects_truncate_regrow_past_old_offset(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    initial = _token_count_line(10) + "\n" + ("{}\n" * 8)
    regrown = _token_count_line(80) + "\n" + ("{}\n" * 32)
    assert len(regrown) > len(initial)
    rollout.write_text(initial, encoding="utf-8")
    initial_inode = rollout.stat().st_ino
    observed: list[str] = []
    observer = _DeterministicRolloutObserver(
        observed.append,
        codex_sessions_root=tmp_path,
    )

    def truncate_and_regrow() -> None:
        rollout.write_text(regrown, encoding="utf-8")
        assert rollout.stat().st_ino == initial_inode

    stop = _MutatingStop(truncate_and_regrow)
    observer._rollout_stop = stop  # type: ignore[assignment]

    observer._follow_rollout_context("thread-1", rollout, stop)  # type: ignore[arg-type]

    assert sum("Context: 10% |" in status for status in observed) == 1
    assert sum("Context: 80% |" in status for status in observed) == 1


def test_round2_rollout_rewrite_then_append_rebaselines_without_interim_publish(
    tmp_path: Path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(_token_count_line(10) + "\n", encoding="utf-8")
    initial_inode = rollout.stat().st_ino
    observed: list[str] = []
    observer = _DeterministicRolloutObserver(
        observed.append,
        codex_sessions_root=tmp_path,
    )

    def rewrite_then_append() -> None:
        rollout.write_text(
            _token_count_line(70) + "\n" + _token_count_line(90) + "\n",
            encoding="utf-8",
        )
        assert rollout.stat().st_ino == initial_inode

    stop = _MutatingStop(rewrite_then_append)
    observer._rollout_stop = stop  # type: ignore[assignment]

    observer._follow_rollout_context("thread-1", rollout, stop)  # type: ignore[arg-type]

    assert sum("Context: 10% |" in status for status in observed) == 1
    assert not any("Context: 70% |" in status for status in observed)
    assert sum("Context: 90% |" in status for status in observed) == 1


def test_round2_rollout_reopen_does_not_publish_a_duplicate_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    replacement = tmp_path / "rollout.replacement"
    content = _token_count_line(10) + "\n"
    rollout.write_text(content, encoding="utf-8")
    replacement.write_text(content, encoding="utf-8")
    observed: list[str] = []
    observer = _DeterministicRolloutObserver(
        observed.append,
        codex_sessions_root=tmp_path,
    )
    actions: list[str] = []
    real_open = protocol_proxy_module.open_rollout_descriptor

    def recorded_open(path: Path) -> int:
        actions.append("open")
        return real_open(path)

    class ReplacingStop(_MutatingStop):
        def wait(self, delay: float) -> bool:
            actions.append("wait")
            return super().wait(delay)

    monkeypatch.setattr(protocol_proxy_module, "open_rollout_descriptor", recorded_open)
    stop = ReplacingStop(lambda: os.replace(replacement, rollout))
    observer._rollout_stop = stop  # type: ignore[assignment]

    observer._follow_rollout_context("thread-1", rollout, stop)  # type: ignore[arg-type]

    assert sum("Context: 10% |" in status for status in observed) == 1
    assert actions[:3] == ["open", "wait", "wait"]
    assert actions[3] == "open"


def test_round2_rollout_symlink_rejection_has_bounded_open_wait_and_byte_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "rollout-target.jsonl"
    rollout = tmp_path / "rollout-thread-1.jsonl"
    target.write_text(_token_count_line(70) + "\n", encoding="utf-8")
    rollout.symlink_to(target)
    opens = 0
    bytes_read = 0
    actions: list[str] = []
    real_open = protocol_proxy_module.open_rollout_descriptor
    real_pread = protocol_proxy_module.os.pread

    def counted_open(path: Path) -> int:
        nonlocal opens
        opens += 1
        actions.append("open")
        return real_open(path)

    def counted_pread(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal bytes_read
        content = real_pread(descriptor, size, offset)
        bytes_read += len(content)
        return content

    monkeypatch.setattr(protocol_proxy_module, "open_rollout_descriptor", counted_open)
    monkeypatch.setattr(protocol_proxy_module.os, "pread", counted_pread)
    observed: list[str] = []
    observer = _DeterministicRolloutObserver(
        observed.append,
        codex_sessions_root=tmp_path,
    )

    class RecordingStop(_CountingStop):
        def wait(self, delay: float) -> bool:
            actions.append("wait")
            return super().wait(delay)

    stop = RecordingStop(4)
    observer._rollout_stop = stop  # type: ignore[assignment]

    observer._follow_rollout_context("thread-1", rollout, stop)  # type: ignore[arg-type]

    assert opens <= stop.waits
    assert stop.waits == 4
    assert actions == ["open", "wait"] * 4
    assert bytes_read == 0
    assert observed == []


def test_round2_access_timestamp_never_regresses_when_commits_arrive_out_of_order(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=uuid.uuid4())
    newest = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    stale_writer = datetime(2030, 1, 1, 11, 0, tzinfo=UTC)

    record_a_rodex_session_access(
        session.rodex_sessions_id,
        database,
        accessed_at_utc=newest,
    )
    record_a_rodex_session_access(
        session.rodex_sessions_id,
        database,
        accessed_at_utc=stale_writer,
    )

    stored = lookup_rodex_session_log(session.rodex_sessions_id, database)
    assert stored is not None
    assert stored.last_accessed_at_utc == "2030-01-01T12:00:00.000000Z", (
        "an older access computed by a delayed writer must not overwrite a newer "
        "committed timestamp"
    )


def test_round2_concurrent_access_writers_preserve_the_maximum_timestamp(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(database, codex_session_id=uuid.uuid4())
    timestamps = [datetime(2031, 1, 1, hour, tzinfo=UTC) for hour in range(1, 9)]

    with ThreadPoolExecutor(max_workers=len(timestamps)) as workers:
        updates = list(
            workers.map(
                lambda timestamp: record_a_rodex_session_access(
                    session.rodex_sessions_id,
                    database,
                    accessed_at_utc=timestamp,
                ),
                reversed(timestamps),
            )
        )

    assert len(updates) == len(timestamps)
    stored = lookup_rodex_session_log(session.rodex_sessions_id, database)
    assert stored is not None
    assert stored.last_accessed_at_utc == "2031-01-01T08:00:00.000000Z"


def test_round2_stale_resume_cannot_regress_log_or_runtime_start_timestamp(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session = create_a_rodex_session(
        database,
        codex_session_id=uuid.uuid4(),
        tmux_server_socket_path="/tmp/rodex/round2.sock",
        tmux_session_name="round2-session",
    )
    newest_runtime_id = RodexRuntimeId.generate()
    stale_runtime_id = RodexRuntimeId.generate()
    record_a_rodex_session_runtime_resume(
        session.rodex_sessions_id,
        "/tmp/rodex/round2.sock",
        "round2-session",
        database,
        runtime_id=newest_runtime_id,
        accessed_at_utc=datetime(2032, 1, 1, 12, tzinfo=UTC),
    )

    record_a_rodex_session_runtime_resume(
        session.rodex_sessions_id,
        "/tmp/rodex/round2.sock",
        "round2-session",
        database,
        runtime_id=stale_runtime_id,
        accessed_at_utc=datetime(2032, 1, 1, 11, tzinfo=UTC),
    )

    log = lookup_rodex_session_log(session.rodex_sessions_id, database)
    runtime = lookup_rodex_runtime_instance(session.rodex_sessions_id, database)
    assert log is not None
    assert runtime is not None
    assert log.last_accessed_at_utc == "2032-01-01T12:00:00.000000Z"
    assert runtime.runtime_id == newest_runtime_id
    assert runtime.started_at_utc == "2032-01-01T12:00:00.000000Z"
