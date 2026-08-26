from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

from rodex.analytics_source_reader import (
    AnalyticsAppendSource,
    AnalyticsSourceReader,
    AnalyticsSourceReadError,
)

THREAD_ID = uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82")


def _root_source(path: Path, root: Path) -> AnalyticsAppendSource:
    return AnalyticsAppendSource(
        path=path,
        codex_thread_id=THREAD_ID,
        source_kind="root",
        subagent_history_start_ordinal=None,
        allowed_root=root,
    )


def _root_content() -> bytes:
    return (
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": str(THREAD_ID), "session_id": str(THREAD_ID)},
            }
        ).encode()
        + b"\n"
    )


def test_committed_cursor_reads_only_appended_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "16" / f"rollout-{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(_root_content())
    source = _root_source(path, root)
    reader = AnalyticsSourceReader()
    initial = reader.read(source)
    reader.accept([initial])
    calls: list[tuple[int, int]] = []
    real_pread = os.pread

    def recording_pread(descriptor: int, size: int, offset: int) -> bytes:
        calls.append((size, offset))
        return real_pread(descriptor, size, offset)

    monkeypatch.setattr("rodex.analytics_source_reader.os.pread", recording_pread)
    addition = b'{"type":"event_msg"}\n'
    with path.open("ab") as output:
        output.write(addition)

    prepared = reader.read(source)

    assert calls == [(len(addition), len(_root_content()))]
    assert prepared.appended_analyzer_content == addition
    assert prepared.analyzer_content == addition


def test_unaccepted_cursor_rereads_pending_append(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "16" / f"rollout-{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(_root_content())
    source = _root_source(path, root)
    reader = AnalyticsSourceReader()
    initial = reader.read(source)
    reader.accept([initial])
    addition = b"{}\n"
    with path.open("ab") as output:
        output.write(addition)

    first_attempt = reader.read(source)
    second_attempt = reader.read(source)

    assert first_attempt.appended_analyzer_content == addition
    assert second_attempt.appended_analyzer_content == addition
    reader.accept([second_attempt])
    assert reader.read(source).appended_analyzer_content == b""


def test_clean_replay_reads_full_history_again_without_forgetting_cursor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "16" / f"rollout-{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(_root_content())
    source = _root_source(path, root)
    reader = AnalyticsSourceReader()
    initial = reader.read(source)
    reader.accept([initial])
    addition = b'{"type":"event_msg"}\n'
    with path.open("ab") as output:
        output.write(addition)

    reader.require_clean_replay()
    replay = reader.read(source)

    assert replay.analyzer_content == _root_content() + addition
    assert replay.appended_analyzer_content == _root_content() + addition


def test_clean_replay_rejects_changed_accepted_prefix_even_after_growth(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "16" / f"rollout-{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    original = _root_content() + b'{"marker":"one"}\n'
    path.write_bytes(original)
    source = _root_source(path, root)
    reader = AnalyticsSourceReader()
    initial = reader.read(source)
    reader.accept([initial])
    path.write_bytes(original.replace(b'"one"', b'"two"') + b"{}\n")

    reader.require_clean_replay()

    with pytest.raises(AnalyticsSourceReadError, match="accepted prefix changed"):
        reader.read(source)


def test_incomplete_tail_is_buffered_until_newline_completion(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "16" / f"rollout-{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(_root_content())
    source = _root_source(path, root)
    reader = AnalyticsSourceReader()
    initial = reader.read(source)
    reader.accept([initial])
    with path.open("ab") as output:
        output.write(b'{"partial"')
    partial = reader.read(source)
    reader.accept([partial])

    assert partial.appended_analyzer_content == b""
    assert partial.authenticated_source.analyzed_size_bytes == len(_root_content())

    with path.open("ab") as output:
        output.write(b":true}\n")
    completed = reader.read(source)
    assert completed.appended_analyzer_content == b'{"partial":true}\n'
    assert (
        completed.authenticated_source.analyzed_prefix_sha256
        == hashlib.sha256(_root_content() + b'{"partial":true}\n').hexdigest()
    )


def test_committed_cursor_rejects_same_inode_rewrite(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "16" / f"rollout-{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(_root_content() + b'{"marker":"one"}\n')
    source = _root_source(path, root)
    reader = AnalyticsSourceReader()
    initial = reader.read(source)
    reader.accept([initial])
    path.write_bytes(path.read_bytes().replace(b'"one"', b'"two"'))
    state = path.stat()
    os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 1_000_000_000))

    with pytest.raises(AnalyticsSourceReadError, match="rewritten in place"):
        reader.read(source)


def test_subagent_cursor_filters_inherited_and_malformed_suffix_records(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "16" / f"rollout-{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    records = (
        {"type": "session_meta", "payload": {"id": str(THREAD_ID)}},
        {"ordinal": 1, "inherited": True},
        {"ordinal": 2, "inherited": True},
        {"ordinal": 3, "child": True},
    )
    initial_content = b"".join(json.dumps(item).encode() + b"\n" for item in records)
    path.write_bytes(initial_content)
    source = AnalyticsAppendSource(
        path=path,
        codex_thread_id=THREAD_ID,
        source_kind="subagent",
        subagent_history_start_ordinal=2,
        allowed_root=root,
    )
    reader = AnalyticsSourceReader()
    initial = reader.read(source)
    reader.accept([initial])
    suffix = (
        json.dumps({"ordinal": 2, "still_inherited": True}).encode()
        + b"\n"
        + b'{"malformed_for_child":true}\n'
        + json.dumps({"ordinal": 4, "child": True}).encode()
        + b"\n"
    )
    with path.open("ab") as output:
        output.write(suffix)

    appended = reader.read(source)

    assert b'"ordinal": 2' not in appended.appended_analyzer_content
    assert b"malformed_for_child" not in appended.appended_analyzer_content
    assert b'"ordinal": 4' in appended.appended_analyzer_content
    assert (
        appended.authenticated_source.analyzed_prefix_sha256
        == hashlib.sha256(initial_content + suffix).hexdigest()
    )


@pytest.mark.parametrize("mutation", ["truncate", "replace"])
def test_committed_cursor_rejects_non_append_source_identity(
    tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / "sessions"
    path = root / "2026" / "08" / "16" / f"rollout-{THREAD_ID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(_root_content() + b"{}\n")
    source = _root_source(path, root)
    reader = AnalyticsSourceReader()
    initial = reader.read(source)
    reader.accept([initial])
    if mutation == "truncate":
        path.write_bytes(_root_content())
        expected = "truncated"
    else:
        replacement = tmp_path / "replacement.jsonl"
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        expected = "identity changed"

    with pytest.raises(AnalyticsSourceReadError, match=expected):
        reader.read(source)
