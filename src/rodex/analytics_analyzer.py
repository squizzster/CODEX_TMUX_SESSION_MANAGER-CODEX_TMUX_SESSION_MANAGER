"""Stateful pinned-analyzer boundary for the Rodex analytics spine."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rodex_registry import (
    CodexThreadId,
    SessionStatisticsProjection,
    StatisticsProjectionError,
    parse_session_statistics_snapshot,
)


class RodexAnalyticsError(RuntimeError):
    """The optional analytics subsystem could not satisfy a request."""


@dataclass(frozen=True, slots=True)
class AnalyticsCalculation:
    """Usable session and turn projections from one analyzer calculation."""

    statistics_projection: SessionStatisticsProjection
    coverage_state: str


@dataclass(frozen=True, slots=True)
class AnalyticsAnalyzerSource:
    """One source's initialization bytes and candidate complete-line suffix."""

    codex_thread_id: CodexThreadId
    analyzer_content: bytes
    appended_analyzer_content: bytes


class AnalyticsBoundary(Protocol):
    def analyze_rollouts(
        self, sources: Sequence[AnalyticsAnalyzerSource], user_id: str
    ) -> AnalyticsCalculation: ...

    def accept_batch(self) -> None: ...


AnalyticsBoundaryFactory = Callable[[], AnalyticsBoundary]


class _AnalyzerLibrary(Protocol):
    def create_new_codex_protocol_id(self, user_id: str) -> object: ...

    def load_file(self, protocol_id: str, path: Path) -> object: ...

    def get_stats(
        self, protocol_id: str, *, include_turn_statistics: bool = False
    ) -> object: ...

    def close(self) -> object: ...


class CodexProtocolAnalyticsAdapter:
    """Full-replay adapter retained as the semantic test oracle."""

    def analyze_rollouts(
        self, sources: Sequence[AnalyticsAnalyzerSource], user_id: str
    ) -> AnalyticsCalculation:
        try:
            module = importlib.import_module("codex_protocol_log_analyzer")
            library: _AnalyzerLibrary = module.CodexProtocolLibrary()
        except Exception as error:
            raise RodexAnalyticsError(
                f"could not initialize Codex protocol analytics: {error}"
            ) from error
        try:
            protocol_id = _protocol_id(
                _operation_value(
                    library.create_new_codex_protocol_id(user_id),
                    "create temporary analytics dataset",
                )
            )
            coverage_state = "complete"
            for index, source in enumerate(sources):
                loaded = _load_analyzer_bytes(
                    library, protocol_id, source.analyzer_content, index
                )
                _operation_value(
                    loaded,
                    "load verified Codex rollout",
                    allow_partial=True,
                )
                if getattr(loaded, "status", "ok") != "ok":
                    coverage_state = "gapped"
            stats_result = library.get_stats(protocol_id, include_turn_statistics=True)
            stats = _mapping_value(
                _operation_value(
                    stats_result,
                    "calculate aggregate statistics",
                    allow_partial=True,
                )
            )
            if getattr(stats_result, "status", "ok") != "ok":
                coverage_state = "gapped"
            return AnalyticsCalculation(
                statistics_projection=_parse_projection(stats),
                coverage_state=coverage_state,
            )
        finally:
            with suppress(Exception):
                library.close()

    def accept_batch(self) -> None:
        """Full replay owns no resident candidate state."""


@dataclass(slots=True)
class _SourceState:
    session_id: str
    pending_content: bytes = b""


class StatefulCodexProtocolAnalyticsAdapter:
    """Retain the pinned analyzer ledgers and consume only candidate suffixes."""

    def __init__(self) -> None:
        try:
            statistics = importlib.import_module("codex_protocol_log_analyzer.statistics")
            library = importlib.import_module("codex_protocol_log_analyzer.library")
            analyzer_type = statistics._StatisticalAnalyzer
            self._new_event_name = library._new_event_name
            self._analyzer = analyzer_type()
        except Exception as error:
            raise RodexAnalyticsError(
                f"pinned analyzer state contract is unavailable: {error}"
            ) from error
        self._sources: dict[CodexThreadId, _SourceState] = {}
        self._user_id: str | None = None
        self._revision = 0
        self._unknown_event_types_by_source: dict[CodexThreadId, set[str]] = {}
        self._coverage_gapped = False
        self._candidate_calculation: AnalyticsCalculation | None = None
        self._poisoned_reason: str | None = None

    def analyze_rollouts(
        self, sources: Sequence[AnalyticsAnalyzerSource], user_id: str
    ) -> AnalyticsCalculation:
        if self._poisoned_reason is not None:
            raise RodexAnalyticsError(
                f"stateful analyzer requires clean restart: {self._poisoned_reason}"
            )
        if self._user_id is None:
            self._user_id = user_id
        elif self._user_id != user_id:
            raise RodexAnalyticsError("analytics user identity changed")
        by_thread = {source.codex_thread_id: source for source in sources}
        if len(by_thread) != len(sources):
            raise RodexAnalyticsError("analyzer batch contains duplicate thread identity")
        prepared: list[
            tuple[AnalyticsAnalyzerSource, bytes, list[dict[str, Any]], int]
        ] = []
        for source in sources:
            state = self._sources.get(source.codex_thread_id)
            offered = (
                source.analyzer_content
                if state is None
                else source.appended_analyzer_content
            )
            pending = b"" if state is None else state.pending_content
            if pending and not offered.startswith(pending):
                raise RodexAnalyticsError(
                    f"analyzer retry diverged for thread {source.codex_thread_id}"
                )
            new_content = offered[len(pending) :]
            records, malformed = _decode_complete_records(new_content)
            _validate_source_records(source.codex_thread_id, records)
            prepared.append((source, new_content, records, malformed))

        changed = False
        try:
            for source, new_content, records, malformed in prepared:
                state = self._sources.get(source.codex_thread_id)
                if state is None:
                    identity_digest = hashlib.sha256(
                        str(source.codex_thread_id).encode()
                    ).hexdigest()
                    source_key = f"source:{identity_digest}"
                    state = _SourceState(source_key)
                    self._sources[source.codex_thread_id] = state
                    self._analyzer.sessions.add(source_key)
                if malformed:
                    self._analyzer.malformed_lines += malformed
                    self._coverage_gapped = True
                    changed = True
                for record in records:
                    self._consume_record(source.codex_thread_id, state, record)
                    event_name = self._new_event_name(record)
                    if event_name is not None:
                        self._unknown_event_types_by_source.setdefault(
                            source.codex_thread_id, set()
                        ).add(str(event_name))
                        self._coverage_gapped = True
                    changed = True
                state.pending_content += new_content
        except Exception as error:
            self._poisoned_reason = type(error).__name__
            raise RodexAnalyticsError(
                "pinned analyzer failed while consuming a validated append"
            ) from error
        if not changed and self._candidate_calculation is not None:
            return self._candidate_calculation
        if changed:
            self._revision += 1
        report = self._analyzer.report(source="rodex stateful rollout sources")
        snapshot = report.to_dict()
        snapshot.pop("source")
        audit = dict(snapshot["audit"])
        audit["new_event_type_warnings"] = sum(
            len(names) for names in self._unknown_event_types_by_source.values()
        )
        snapshot.update(
            {
                "protocol_id": "rodex_stateful_analyzer",
                "user_id": self._user_id,
                "revision": self._revision,
                "event_count": self._analyzer.records,
                "source_count": len(self._sources),
                "selected_stats": None,
                "audit": audit,
            }
        )
        calculation = AnalyticsCalculation(
            statistics_projection=_parse_projection(snapshot),
            coverage_state="gapped" if self._coverage_gapped else "complete",
        )
        self._candidate_calculation = calculation
        return calculation

    def accept_batch(self) -> None:
        """Acknowledge candidate bytes only after their SQL projection commits."""
        for state in self._sources.values():
            state.pending_content = b""

    def _consume_record(
        self,
        thread_id: CodexThreadId,
        state: _SourceState,
        record: dict[str, Any],
    ) -> None:
        self._analyzer.records += 1
        self._analyzer.sequence += 1
        payload_value = record.get("payload")
        payload = dict(payload_value) if isinstance(payload_value, Mapping) else {}
        if record.get("type") == "session_meta":
            identifier = payload.get("id")
            assert identifier == str(thread_id)
            self._analyzer.sessions.discard(state.session_id)
            state.session_id = identifier
            self._analyzer.sessions.add(identifier)
            return
        self._analyzer._consume(record, payload, state.session_id)


def _decode_complete_records(content: bytes) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            malformed += 1
            continue
        if not isinstance(record, dict):
            malformed += 1
            continue
        records.append(record)
    return records, malformed


def _validate_source_records(
    thread_id: CodexThreadId, records: Sequence[Mapping[str, Any]]
) -> None:
    """Reject identity errors before the pinned analyzer mutates resident state."""
    for record in records:
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        identifier = payload.get("id") if isinstance(payload, Mapping) else None
        if identifier != str(thread_id):
            raise RodexAnalyticsError(
                f"analyzer source identity changed for thread {thread_id}"
            )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON number: {value}")


def _parse_projection(stats: Mapping[str, Any]) -> SessionStatisticsProjection:
    try:
        return parse_session_statistics_snapshot(stats)
    except StatisticsProjectionError as error:
        raise RodexAnalyticsError(
            f"analyzer statistics contract mismatch: {error}"
        ) from error


def _operation_value(
    result: object, operation: str, *, allow_partial: bool = False
) -> object:
    value = getattr(result, "value", result)
    status = getattr(result, "status", "ok")
    if status != "fatal" and value is not None and (allow_partial or status != "error"):
        return value
    diagnostics = getattr(result, "diagnostics", ())
    detail = "; ".join(
        str(getattr(diagnostic, "message", diagnostic)) for diagnostic in diagnostics
    )
    raise RodexAnalyticsError(f"could not {operation}" + (f": {detail}" if detail else ""))


def _protocol_id(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        value = value.get("protocol_id")
    else:
        value = getattr(value, "protocol_id", None)
    if not isinstance(value, str) or not value:
        raise RodexAnalyticsError("analyzer returned no temporary protocol identity")
    return value


def _mapping_value(value: object) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        raise RodexAnalyticsError("analyzer returned an invalid statistics snapshot")
    return dict(value)


def _load_analyzer_bytes(
    library: _AnalyzerLibrary,
    protocol_id: str,
    content: bytes,
    source_index: int,
) -> object:
    descriptor = _create_memory_file(f"rodex-analytics-{source_index}")
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise RodexAnalyticsError("could not populate memory-backed analyzer file")
            remaining = remaining[written:]
        _seal_memory_file(descriptor)
        return library.load_file(protocol_id, Path(f"/proc/self/fd/{descriptor}"))
    except OSError as error:
        raise RodexAnalyticsError(
            f"could not prepare memory-backed analyzer file: {error}"
        ) from error
    finally:
        os.close(descriptor)


def _create_memory_file(name: str) -> int:
    flags = 0x0001 | 0x0002
    if hasattr(os, "memfd_create"):
        return os.memfd_create(name, flags)
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        memfd_create = libc.memfd_create
        memfd_create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
        memfd_create.restype = ctypes.c_int
        descriptor = memfd_create(name.encode(), flags)
    except (AttributeError, ImportError) as error:
        raise RodexAnalyticsError("memory-backed analyzer files are unavailable") from error
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise RodexAnalyticsError(
            f"could not create memory-backed analyzer file: {os.strerror(error_number)}"
        )
    return int(descriptor)


def _seal_memory_file(descriptor: int) -> None:
    try:
        import fcntl

        fcntl.fcntl(descriptor, 1033, 0x0001 | 0x0002 | 0x0004 | 0x0008)
    except (ImportError, OSError) as error:
        raise RodexAnalyticsError(
            f"could not seal memory-backed analyzer file: {error}"
        ) from error
