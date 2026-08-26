"""Bounded rollout-source resolution from exact Codex thread identities."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from rodex_registry import CodexThreadId, parse_codex_thread_id

from .app_server_contract import CODEX_APP_SERVER
from .protocol_proxy import EVENT_STREAM_READY_METHOD


class AnalyticsSourceCatalog:
    """Cache exact thread dates and resolve only their expected rollout directories."""

    def __init__(self, sessions_root: Path) -> None:
        self._sessions_root = sessions_root
        self._dates_by_thread: dict[CodexThreadId, set[date]] = {}
        self._resolved_paths: dict[CodexThreadId, Path] = {}
        self._lock = Lock()

    def observe_protocol_event(self, event: Mapping[str, Any]) -> None:
        """Remember thread identity metadata carried by the runtime event stream."""
        method = event.get("method")
        params = event.get("params")
        if not isinstance(params, Mapping):
            return
        if method == CODEX_APP_SERVER.thread_started_method:
            thread = params.get("thread")
            if isinstance(thread, Mapping):
                self._observe_thread(thread)
            return
        if method != EVENT_STREAM_READY_METHOD:
            return
        known_threads = params.get("knownThreads")
        if not isinstance(known_threads, list):
            return
        for thread in known_threads:
            if isinstance(thread, Mapping):
                self._observe_thread(thread)

    def candidate_thread_ids(self) -> frozenset[CodexThreadId]:
        """Return the small exact identity set learned from lifecycle events."""
        with self._lock:
            return frozenset(self._dates_by_thread)

    def candidate_paths(
        self,
        thread_id: CodexThreadId,
        *,
        first_linked_at_utc: str | None = None,
    ) -> tuple[Path, ...]:
        """List filename matches in only the expected date directories."""
        parsed_thread_id = parse_codex_thread_id(thread_id)
        with self._lock:
            cached = self._resolved_paths.get(parsed_thread_id)
            observed_dates = set(self._dates_by_thread.get(parsed_thread_id, ()))
        if cached is not None:
            return (cached,)
        observed_dates.update(_uuid_v7_date_window(parsed_thread_id))
        linked_date = _utc_date(first_linked_at_utc)
        if linked_date is not None:
            observed_dates.add(linked_date)
        suffix = f"{parsed_thread_id}.jsonl"
        candidates: list[Path] = []
        for expected_date in sorted(observed_dates):
            directory = self._sessions_root / expected_date.strftime("%Y/%m/%d")
            try:
                with os.scandir(directory) as entries:
                    candidates.extend(
                        Path(entry.path) for entry in entries if entry.name.endswith(suffix)
                    )
            except OSError:
                continue
        return tuple(sorted(set(candidates)))

    def session_tree_candidate_paths(
        self,
        root_thread_id: CodexThreadId,
        *,
        first_linked_at_utc: str | None = None,
    ) -> tuple[Path, ...]:
        """List regular rollout files in one root's bounded cold-start date window."""
        parsed_root_id = parse_codex_thread_id(root_thread_id)
        expected_dates = _uuid_v7_date_window(parsed_root_id)
        linked_date = _utc_date(first_linked_at_utc)
        if not expected_dates and linked_date is not None:
            expected_dates.update(
                {
                    linked_date - timedelta(days=1),
                    linked_date,
                    linked_date + timedelta(days=1),
                }
            )
        candidates: list[Path] = []
        for expected_date in sorted(expected_dates):
            directory = self._sessions_root / expected_date.strftime("%Y/%m/%d")
            try:
                with os.scandir(directory) as entries:
                    candidates.extend(
                        Path(entry.path)
                        for entry in entries
                        if entry.name.endswith(".jsonl")
                        and entry.is_file(follow_symlinks=False)
                    )
            except OSError:
                continue
        return tuple(sorted(set(candidates)))

    def remember_resolved_path(self, thread_id: CodexThreadId, path: Path) -> None:
        """Cache one metadata-authenticated path for the worker lifetime."""
        parsed_thread_id = parse_codex_thread_id(thread_id)
        with self._lock:
            self._resolved_paths[parsed_thread_id] = path
            self._dates_by_thread.setdefault(parsed_thread_id, set()).update(
                _uuid_v7_date_window(parsed_thread_id)
            )

    def _observe_thread(self, thread: Mapping[str, Any]) -> None:
        try:
            thread_id = parse_codex_thread_id(thread.get("id"))
        except (TypeError, ValueError):
            return
        created_date = _utc_date(thread.get("createdAt"))
        dates = _uuid_v7_date_window(thread_id)
        if created_date is not None:
            dates.add(created_date)
        with self._lock:
            self._dates_by_thread.setdefault(thread_id, set()).update(dates)


def _uuid_v7_date_window(thread_id: CodexThreadId) -> set[date]:
    """Return a timezone-tolerant three-day window encoded by a UUIDv7 ID."""
    if thread_id.version != 7:
        return set()
    milliseconds = int(thread_id.hex[:12], 16)
    created = datetime.fromtimestamp(milliseconds / 1000, UTC).date()
    return {
        created - timedelta(days=1),
        created,
        created + timedelta(days=1),
    }


def _utc_date(value: object) -> date | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 100_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, UTC).date()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).date()
