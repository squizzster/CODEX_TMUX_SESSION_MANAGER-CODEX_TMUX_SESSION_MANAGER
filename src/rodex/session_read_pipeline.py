"""One verified pipeline for reading a live Rodex session."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from rodex_registry import record_a_rodex_session_access

from .control import LiveRodexControl
from .live_runtime import resolve_live_control, revalidate_live_control
from .runtime import LiveTmuxSession, RodexRuntimeLauncher

Snapshot = TypeVar("Snapshot")
Revalidate = Callable[[], None]
SnapshotReader = Callable[[LiveTmuxSession], Snapshot]
EventStreamer = Callable[[LiveRodexControl, Revalidate], None]
ScrollbackStreamer = Callable[[LiveTmuxSession, Revalidate], None]


class LiveSessionReadPipeline:
    """Resolve, read, revalidate, and record one owned live session."""

    def __init__(self, database_path: Path, launcher: RodexRuntimeLauncher) -> None:
        self._database_path = database_path
        self._launcher = launcher

    def snapshot(self, session_name: str, reader: SnapshotReader[Snapshot]) -> Snapshot:
        """Return one finite read only after its runtime identity remains verified."""
        session_id, runtime, control = resolve_live_control(
            session_name, self._database_path, self._launcher
        )
        result = reader(runtime)
        revalidate_live_control(self._launcher, runtime, control)
        record_a_rodex_session_access(session_id, self._database_path)
        return result

    def stream_events(self, session_name: str, streamer: EventStreamer) -> None:
        """Start one verified stream and record access before its unbounded read."""
        session_id, runtime, control = resolve_live_control(
            session_name, self._database_path, self._launcher
        )
        record_a_rodex_session_access(session_id, self._database_path)
        streamer(
            control,
            lambda: revalidate_live_control(self._launcher, runtime, control),
        )

    def stream_scrollback(self, session_name: str, streamer: ScrollbackStreamer) -> None:
        """Start one verified terminal stream and revalidate through its reader."""
        session_id, runtime, control = resolve_live_control(
            session_name, self._database_path, self._launcher
        )
        record_a_rodex_session_access(session_id, self._database_path)
        streamer(
            runtime,
            lambda: revalidate_live_control(self._launcher, runtime, control),
        )
