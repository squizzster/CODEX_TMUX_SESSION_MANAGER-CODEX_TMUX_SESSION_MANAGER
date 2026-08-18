from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

import rodex.session_read_pipeline as pipeline_module
from rodex.control import LiveRodexControl
from rodex.runtime import LiveTmuxSession
from rodex.session_read_pipeline import LiveSessionReadPipeline


def _resolved_read(tmp_path: Path) -> tuple[int, LiveTmuxSession, LiveRodexControl]:
    runtime = LiveTmuxSession(tmp_path / "tmux.sock", "automatic-beluga")
    control = LiveRodexControl(
        tmp_path / "control.sock",
        tmp_path / "events.sock",
        uuid.UUID("01a00654-f2bc-7a30-834a-a5f886a65f82"),
    )
    return 7, runtime, control


def test_snapshot_pipeline_has_one_authoritative_success_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session_id, runtime, control = _resolved_read(tmp_path)
    observed: list[str] = []
    launcher = object()

    def resolve(*args: Any) -> tuple[int, LiveTmuxSession, LiveRodexControl]:
        assert args == ("automatic-beluga", database, launcher)
        observed.append("resolve")
        return session_id, runtime, control

    def revalidate(*args: Any) -> None:
        assert args == (launcher, runtime, control)
        observed.append("revalidate")

    def record(*args: Any) -> None:
        assert args == (session_id, database)
        observed.append("record_access")

    monkeypatch.setattr(pipeline_module, "resolve_live_control", resolve)
    monkeypatch.setattr(pipeline_module, "revalidate_live_control", revalidate)
    monkeypatch.setattr(pipeline_module, "record_a_rodex_session_access", record)
    pipeline = LiveSessionReadPipeline(database, launcher)  # type: ignore[arg-type]

    result = pipeline.snapshot(
        "automatic-beluga",
        lambda observed_runtime: (
            (
                observed.append("read"),
                ("first", "second"),
            )[1]
            if observed_runtime == runtime
            else pytest.fail("snapshot received the wrong runtime")
        ),
    )

    assert result == ("first", "second")
    assert observed == ["resolve", "read", "revalidate", "record_access"]


def test_snapshot_pipeline_does_not_record_a_failed_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    resolved = _resolved_read(tmp_path)
    observed: list[str] = []
    monkeypatch.setattr(
        pipeline_module,
        "resolve_live_control",
        lambda *_args: (observed.append("resolve"), resolved)[1],
    )
    monkeypatch.setattr(
        pipeline_module,
        "revalidate_live_control",
        lambda *_args: observed.append("unexpected_revalidate"),
    )
    monkeypatch.setattr(
        pipeline_module,
        "record_a_rodex_session_access",
        lambda *_args: observed.append("unexpected_access"),
    )
    pipeline = LiveSessionReadPipeline(database, object())  # type: ignore[arg-type]

    def fail(_runtime: LiveTmuxSession) -> tuple[str, ...]:
        observed.append("read")
        raise RuntimeError("capture failed")

    with pytest.raises(RuntimeError, match="capture failed"):
        pipeline.snapshot("automatic-beluga", fail)

    assert observed == ["resolve", "read"]


def test_snapshot_pipeline_does_not_return_or_record_after_failed_revalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    resolved = _resolved_read(tmp_path)
    observed: list[str] = []
    monkeypatch.setattr(
        pipeline_module,
        "resolve_live_control",
        lambda *_args: (observed.append("resolve"), resolved)[1],
    )

    def reject(*_args: Any) -> None:
        observed.append("revalidate")
        raise RuntimeError("runtime changed")

    monkeypatch.setattr(pipeline_module, "revalidate_live_control", reject)
    monkeypatch.setattr(
        pipeline_module,
        "record_a_rodex_session_access",
        lambda *_args: observed.append("unexpected_access"),
    )
    pipeline = LiveSessionReadPipeline(database, object())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="runtime changed"):
        pipeline.snapshot(
            "automatic-beluga",
            lambda _runtime: (observed.append("read"), ("untrusted",))[1],
        )

    assert observed == ["resolve", "read", "revalidate"]


def test_event_pipeline_records_access_before_its_unbounded_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session_id, runtime, control = _resolved_read(tmp_path)
    observed: list[str] = []
    launcher = object()
    monkeypatch.setattr(
        pipeline_module,
        "resolve_live_control",
        lambda *_args: (observed.append("resolve"), (session_id, runtime, control))[1],
    )
    monkeypatch.setattr(
        pipeline_module,
        "revalidate_live_control",
        lambda *_args: observed.append("revalidate"),
    )
    monkeypatch.setattr(
        pipeline_module,
        "record_a_rodex_session_access",
        lambda *_args: observed.append("record_access"),
    )
    pipeline = LiveSessionReadPipeline(database, launcher)  # type: ignore[arg-type]

    def stream(observed_control: LiveRodexControl, revalidate: Any) -> None:
        assert observed_control == control
        observed.append("stream")
        revalidate()
        observed.extend(("event-1", "event-2", "event-3"))

    pipeline.stream_events("automatic-beluga", stream)

    assert observed == [
        "resolve",
        "record_access",
        "stream",
        "revalidate",
        "event-1",
        "event-2",
        "event-3",
    ]


def test_scrollback_pipeline_records_access_before_its_unbounded_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "rodex.sqlite3"
    session_id, runtime, control = _resolved_read(tmp_path)
    observed: list[str] = []
    launcher = object()
    monkeypatch.setattr(
        pipeline_module,
        "resolve_live_control",
        lambda *_args: (observed.append("resolve"), (session_id, runtime, control))[1],
    )
    monkeypatch.setattr(
        pipeline_module,
        "revalidate_live_control",
        lambda *_args: observed.append("revalidate"),
    )
    monkeypatch.setattr(
        pipeline_module,
        "record_a_rodex_session_access",
        lambda *_args: observed.append("record_access"),
    )
    pipeline = LiveSessionReadPipeline(database, launcher)  # type: ignore[arg-type]

    def stream(observed_runtime: LiveTmuxSession, revalidate: Any) -> None:
        assert observed_runtime == runtime
        observed.append("stream")
        revalidate()
        observed.extend(("terminal-1", "terminal-2"))

    pipeline.stream_scrollback("automatic-beluga", stream)

    assert observed == [
        "resolve",
        "record_access",
        "stream",
        "revalidate",
        "terminal-1",
        "terminal-2",
    ]
