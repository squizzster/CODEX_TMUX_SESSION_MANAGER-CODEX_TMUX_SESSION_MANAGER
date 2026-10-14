"""One verified pipeline for reading a live Rodex session."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .control import LiveRodexControl
from .errors import RodexLaunchError
from .live_runtime import resolve_live_control, revalidate_live_control
from .runtime import LiveTmuxSession, RodexRuntimeError, RodexRuntimeLauncher

Snapshot = TypeVar("Snapshot")
Revalidate = Callable[[], None]
SnapshotReader = Callable[[LiveTmuxSession], Snapshot]
EventStreamer = Callable[[LiveRodexControl, Revalidate], None]
ScrollbackStreamer = Callable[[LiveTmuxSession, Revalidate], None]
AccessRecorder = Callable[[int, Path], None]


class LiveSessionReadPipeline:
    """Resolve, read, revalidate, and record one owned live session."""

    def __init__(
        self,
        database_path: Path,
        launcher: RodexRuntimeLauncher,
        access_recorder: AccessRecorder,
    ) -> None:
        self._database_path = database_path
        self._launcher = launcher
        self._access_recorder = access_recorder

    def snapshot(self, session_name: str, reader: SnapshotReader[Snapshot]) -> Snapshot:
        """Return one finite read only after its runtime identity remains verified."""
        session_id, runtime, control = resolve_live_control(session_name, self._database_path, self._launcher)
        result = reader(runtime)
        revalidate_live_control(self._launcher, runtime, control)
        self._access_recorder(session_id, self._database_path)
        return result

    def stream_events(self, session_name: str, streamer: EventStreamer) -> None:
        """Start one verified stream and record access before its unbounded read."""
        session_id, runtime, control = resolve_live_control(session_name, self._database_path, self._launcher)
        _refuse_same_session_event_sink(runtime, self._launcher)
        self._access_recorder(session_id, self._database_path)
        streamer(
            control,
            lambda: revalidate_live_control(self._launcher, runtime, control),
        )

    def stream_scrollback(self, session_name: str, streamer: ScrollbackStreamer) -> None:
        """Start one verified terminal stream and revalidate through its reader."""
        session_id, runtime, control = resolve_live_control(session_name, self._database_path, self._launcher)
        self._access_recorder(session_id, self._database_path)
        streamer(
            runtime,
            lambda: revalidate_live_control(self._launcher, runtime, control),
        )


def _refuse_same_session_event_sink(
    runtime: LiveTmuxSession,
    launcher: RodexRuntimeLauncher,
) -> None:
    """Prevent protocol-event output from feeding the exact session it observes."""
    if not os.environ.get("TMUX") or not os.environ.get("TMUX_PANE"):
        return
    capability = runtime.tmux_capability
    if capability is None:
        raise RodexLaunchError(
            "cannot safely open _events from this tmux pane because the target session "
            "identity could not be verified; use another terminal or _trace --follow"
        )
    try:
        current = launcher.discover_current_tmux_pane_context()
    except (OSError, RodexRuntimeError, ValueError) as error:
        raise RodexLaunchError(
            "cannot safely open _events from this tmux pane because its session "
            "identity could not be verified; use another terminal or _trace --follow"
        ) from error
    if (
        current.tmux_session.tmux_server_socket_path == capability.tmux_server_socket_path
        and current.tmux_session_id == capability.tmux_session_id
    ):
        raise RodexLaunchError(
            "_events output cannot use the same Rodex session it observes; "
            "run it from another terminal or use _trace --follow"
        )
