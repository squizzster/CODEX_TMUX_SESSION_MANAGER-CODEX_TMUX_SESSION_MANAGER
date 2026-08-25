"""Authenticated append-only reads for exact Codex rollout sources."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from rodex_registry import CodexThreadId, parse_codex_thread_id


class AnalyticsSourceReadError(RuntimeError):
    """An exact rollout source could not provide a stable append prefix."""


@dataclass(frozen=True, slots=True)
class AnalyticsAppendSource:
    """Immutable source identity and filtering rules for one rollout cursor."""

    path: Path
    codex_thread_id: CodexThreadId
    source_kind: str
    subagent_history_start_ordinal: int | None
    allowed_root: Path


@dataclass(frozen=True, slots=True)
class AuthenticatedRolloutPrefix:
    """Filesystem identity and digest of one captured complete-record prefix."""

    path: Path
    source_device: int
    source_inode: int
    source_size_bytes: int
    source_mtime_ns: int
    source_ctime_ns: int
    analyzed_size_bytes: int
    analyzed_prefix_sha256: str


@dataclass(frozen=True, slots=True)
class AnalyticsSourceRead:
    """Immutable analyzer content plus only the newly accepted content."""

    analyzer_content: bytes
    appended_analyzer_content: bytes
    authenticated_source: AuthenticatedRolloutPrefix
    _candidate_cursor: _AppendCursor


@dataclass(slots=True)
class _AppendCursor:
    source: AnalyticsAppendSource
    authenticated_source: AuthenticatedRolloutPrefix
    raw_complete_size: int
    incomplete_tail: bytes
    analyzer_content: bytes
    complete_line_count: int
    digest: object


class AnalyticsSourceReader:
    """Read each source once, then consume only stable newline-complete appends."""

    def __init__(self) -> None:
        self._cursors: dict[CodexThreadId, _AppendCursor] = {}

    def read(self, source: AnalyticsAppendSource) -> AnalyticsSourceRead:
        """Capture the source's current complete prefix with append-only work."""
        normalized = _normalise_source(source)
        resolved = resolve_rollout_path(
            normalized.path, allowed_root=normalized.allowed_root
        )
        descriptor = open_rollout_descriptor(resolved)
        try:
            before = os.fstat(descriptor)
            cursor = self._cursors.get(normalized.codex_thread_id)
            start = (
                0 if cursor is None else _append_start(cursor, normalized, resolved, before)
            )
            added = _pread_exact(descriptor, start, before.st_size - start)
            after = os.fstat(descriptor)
            path_state = os.stat(resolved, follow_symlinks=False)
        except OSError as error:
            raise AnalyticsSourceReadError(
                f"could not read rollout append: {error}"
            ) from error
        finally:
            os.close(descriptor)
        _require_stable_source(before, after, path_state)
        if cursor is None:
            next_cursor, appended = _new_cursor(normalized, resolved, after, added)
        else:
            next_cursor, appended = _advance_cursor(cursor, resolved, after, added)
        return AnalyticsSourceRead(
            analyzer_content=next_cursor.analyzer_content,
            appended_analyzer_content=appended,
            authenticated_source=next_cursor.authenticated_source,
            _candidate_cursor=next_cursor,
        )

    def accept(self, reads: list[AnalyticsSourceRead]) -> None:
        """Commit prepared cursors only after their calculation is accepted."""
        for read in reads:
            candidate = read._candidate_cursor
            self._cursors[candidate.source.codex_thread_id] = candidate

    def verify_captured_prefix(self, captured: AuthenticatedRolloutPrefix) -> bool:
        """Accept later appends but reject replacement, truncation, or mutation."""
        try:
            descriptor = open_rollout_descriptor(captured.path)
            try:
                current = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            path_state = os.stat(captured.path, follow_symlinks=False)
        except OSError as error:
            raise AnalyticsSourceReadError(
                f"could not verify captured rollout prefix: {error}"
            ) from error
        if (current.st_dev, current.st_ino) != (
            captured.source_device,
            captured.source_inode,
        ) or (path_state.st_dev, path_state.st_ino) != (
            captured.source_device,
            captured.source_inode,
        ):
            raise AnalyticsSourceReadError(
                "rollout source identity changed during analysis"
            )
        if current.st_size < captured.source_size_bytes:
            raise AnalyticsSourceReadError("rollout source was truncated during analysis")
        if current.st_size == captured.source_size_bytes and (
            current.st_mtime_ns,
            current.st_ctime_ns,
        ) != (captured.source_mtime_ns, captured.source_ctime_ns):
            raise AnalyticsSourceReadError("rollout source changed during analysis")
        return current.st_size > captured.source_size_bytes


def resolve_rollout_path(path: str | Path, *, allowed_root: Path | None = None) -> Path:
    """Resolve one non-symlink rollout beneath its configured sessions root."""
    candidate = Path(path).expanduser()
    state = candidate.lstat()
    if stat.S_ISLNK(state.st_mode):
        raise AnalyticsSourceReadError(f"rollout source is a symbolic link: {candidate}")
    resolved = candidate.resolve()
    if allowed_root is not None:
        root = allowed_root.expanduser().resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise AnalyticsSourceReadError(
                f"rollout source escapes the configured sessions root: {candidate}"
            ) from error
    return resolved


def open_rollout_descriptor(path: Path) -> int:
    """Open one owned regular rollout without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        state = os.fstat(descriptor)
        if not stat.S_ISREG(state.st_mode):
            raise AnalyticsSourceReadError(f"rollout source is not a regular file: {path}")
        if state.st_uid != os.getuid():
            raise AnalyticsSourceReadError(
                f"rollout source is not owned by uid {os.getuid()}: {path}"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _normalise_source(source: AnalyticsAppendSource) -> AnalyticsAppendSource:
    if source.source_kind not in {"root", "subagent"}:
        raise AnalyticsSourceReadError("rollout source kind is invalid")
    if source.source_kind == "subagent":
        cutoff = source.subagent_history_start_ordinal
        if isinstance(cutoff, bool) or not isinstance(cutoff, int) or cutoff < 0:
            raise AnalyticsSourceReadError("sub-agent rollout has no history cutoff")
    elif source.subagent_history_start_ordinal is not None:
        raise AnalyticsSourceReadError("root rollout cannot have a history cutoff")
    return AnalyticsAppendSource(
        path=source.path,
        codex_thread_id=parse_codex_thread_id(source.codex_thread_id),
        source_kind=source.source_kind,
        subagent_history_start_ordinal=source.subagent_history_start_ordinal,
        allowed_root=source.allowed_root,
    )


def _append_start(
    cursor: _AppendCursor,
    source: AnalyticsAppendSource,
    resolved: Path,
    current: os.stat_result,
) -> int:
    prior = cursor.authenticated_source
    if (
        cursor.source.codex_thread_id != source.codex_thread_id
        or cursor.source.source_kind != source.source_kind
        or cursor.source.subagent_history_start_ordinal
        != source.subagent_history_start_ordinal
    ):
        raise AnalyticsSourceReadError("rollout cursor metadata changed")
    if prior.path != resolved or (prior.source_device, prior.source_inode) != (
        current.st_dev,
        current.st_ino,
    ):
        raise AnalyticsSourceReadError("rollout source identity changed")
    if current.st_size < prior.source_size_bytes:
        raise AnalyticsSourceReadError("rollout source was truncated")
    if current.st_size == prior.source_size_bytes and (
        current.st_mtime_ns,
        current.st_ctime_ns,
    ) != (
        prior.source_mtime_ns,
        prior.source_ctime_ns,
    ):
        raise AnalyticsSourceReadError("rollout source was rewritten in place")
    return prior.source_size_bytes


def _new_cursor(
    source: AnalyticsAppendSource,
    resolved: Path,
    state: os.stat_result,
    content: bytes,
) -> tuple[_AppendCursor, bytes]:
    raw_complete, tail = _split_complete_prefix(content)
    if not _content_declares_thread(raw_complete, source.codex_thread_id):
        raise AnalyticsSourceReadError("rollout has an unexpected Codex identity")
    analyzer_content = _filter_analyzer_lines(source, raw_complete, line_offset=0)
    if source.source_kind == "subagent" and analyzer_content.count(b"\n") == 1:
        raise AnalyticsSourceReadError(
            "sub-agent rollout contains no child history records"
        )
    digest = hashlib.sha256(raw_complete)
    authenticated = _authentication(
        resolved,
        state,
        analyzed_size_bytes=len(raw_complete),
        digest=digest,
    )
    cursor = _AppendCursor(
        source=source,
        authenticated_source=authenticated,
        raw_complete_size=len(raw_complete),
        incomplete_tail=tail,
        analyzer_content=analyzer_content,
        complete_line_count=raw_complete.count(b"\n"),
        digest=digest,
    )
    return cursor, analyzer_content


def _advance_cursor(
    cursor: _AppendCursor,
    resolved: Path,
    state: os.stat_result,
    added: bytes,
) -> tuple[_AppendCursor, bytes]:
    combined = cursor.incomplete_tail + added
    complete_addition, tail = _split_optional_complete_prefix(combined)
    analyzer_addition = _filter_analyzer_lines(
        cursor.source,
        complete_addition,
        line_offset=cursor.complete_line_count,
    )
    digest = cursor.digest.copy()  # type: ignore[attr-defined]
    digest.update(complete_addition)
    raw_complete_size = cursor.raw_complete_size + len(complete_addition)
    authenticated = _authentication(
        resolved,
        state,
        analyzed_size_bytes=raw_complete_size,
        digest=digest,
    )
    return (
        _AppendCursor(
            source=cursor.source,
            authenticated_source=authenticated,
            raw_complete_size=raw_complete_size,
            incomplete_tail=tail,
            analyzer_content=cursor.analyzer_content + analyzer_addition,
            complete_line_count=(
                cursor.complete_line_count + complete_addition.count(b"\n")
            ),
            digest=digest,
        ),
        analyzer_addition,
    )


def _authentication(
    path: Path,
    state: os.stat_result,
    *,
    analyzed_size_bytes: int,
    digest: object,
) -> AuthenticatedRolloutPrefix:
    return AuthenticatedRolloutPrefix(
        path=path,
        source_device=state.st_dev,
        source_inode=state.st_ino,
        source_size_bytes=state.st_size,
        source_mtime_ns=state.st_mtime_ns,
        source_ctime_ns=state.st_ctime_ns,
        analyzed_size_bytes=analyzed_size_bytes,
        analyzed_prefix_sha256=digest.hexdigest(),  # type: ignore[attr-defined]
    )


def _split_complete_prefix(content: bytes) -> tuple[bytes, bytes]:
    final_newline = content.rfind(b"\n")
    if final_newline < 0:
        raise AnalyticsSourceReadError(
            "rollout contains no complete newline-terminated record"
        )
    return content[: final_newline + 1], content[final_newline + 1 :]


def _split_optional_complete_prefix(content: bytes) -> tuple[bytes, bytes]:
    final_newline = content.rfind(b"\n")
    if final_newline < 0:
        return b"", content
    return content[: final_newline + 1], content[final_newline + 1 :]


def _filter_analyzer_lines(
    source: AnalyticsAppendSource,
    content: bytes,
    *,
    line_offset: int,
) -> bytes:
    if source.source_kind == "root" or not content:
        return content
    cutoff = source.subagent_history_start_ordinal
    assert cutoff is not None
    retained: list[bytes] = []
    for local_index, line in enumerate(content.splitlines(keepends=True)):
        if line_offset + local_index == 0:
            retained.append(line)
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeError):
            continue
        ordinal = record.get("ordinal") if isinstance(record, dict) else None
        if isinstance(ordinal, int) and not isinstance(ordinal, bool) and ordinal > cutoff:
            retained.append(line)
    return b"".join(retained)


def _content_declares_thread(content: bytes, expected: CodexThreadId) -> bool:
    for _, line in zip(range(32), io.BytesIO(content), strict=False):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        return isinstance(payload, dict) and payload.get("id") == str(expected)
    return False


def _pread_exact(descriptor: int, offset: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.pread(descriptor, min(remaining, 1024 * 1024), offset)
        if not chunk:
            raise AnalyticsSourceReadError("rollout ended during append capture")
        chunks.append(chunk)
        offset += len(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _require_stable_source(
    before: os.stat_result,
    after: os.stat_result,
    path_state: os.stat_result,
) -> None:
    before_state = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_state = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    path_identity = (path_state.st_dev, path_state.st_ino)
    if before_state != after_state or path_identity != before_state[:2]:
        raise AnalyticsSourceReadError("rollout source changed during append capture")
