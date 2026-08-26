"""Operator commands for durable agent lineage and trace following."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from rodex_registry import (
    list_rodex_session_codex_rollout_sources,
    list_rodex_session_codex_threads,
    lookup_owned_rodex_sessions_id_from_a_cool_name,
    read_rodex_agent_trace,
)

from .agent_trace_privacy import redact_codex_encrypted_text
from .analytics_source_reader import open_rollout_descriptor
from .command_contract import AGENTS_COMMAND, TRACE_COMMAND
from .errors import RodexLaunchError

TRACE_FOLLOW_POLL_SECONDS = 1.0
TRACE_DEFAULT_LIMIT = 200
TRACE_USAGE = (
    "usage: rodex _trace SESSION_NAME [--follow] [--after EVENT_ID] "
    "[--limit NUM] [--include-bodies] [--json]"
)


def execute_agent_trace_command(arguments: list[str], database_path: Path) -> None:
    """Execute one direct, database-backed agent observability command."""
    if not arguments or arguments[0] not in {AGENTS_COMMAND, TRACE_COMMAND}:
        raise AssertionError("application pipeline selected an invalid trace command")
    if len(arguments) < 2 or arguments[1].startswith("-"):
        raise RodexLaunchError(
            "usage: rodex _agents SESSION_NAME [--json]"
            if arguments and arguments[0] == AGENTS_COMMAND
            else TRACE_USAGE
        )
    session_name = arguments[1]
    session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(
        session_name, database_path
    )
    if session_id is None:
        raise RodexLaunchError(f"unknown Rodex session: {session_name}")
    if arguments[0] == AGENTS_COMMAND:
        _show_agents(arguments, session_name, session_id, database_path)
        return
    request = _parse_trace_arguments(arguments)
    _show_or_follow_trace(
        session_name,
        session_id,
        database_path,
        **request,
    )


def _show_agents(
    arguments: list[str], session_name: str, session_id: int, database_path: Path
) -> None:
    if arguments[2:] not in ([], ["--json"]):
        raise RodexLaunchError("usage: rodex _agents SESSION_NAME [--json]")
    sources = list_rodex_session_codex_threads(session_id, database_path)
    thread_ids_by_row_id = {source.id: str(source.codex_thread_id) for source in sources}
    payload = {
        "rodex_session_name": session_name,
        "agent_count": len(sources),
        "agents": [
            {
                "codex_thread_id": str(source.codex_thread_id),
                "source_kind": source.source_kind,
                "parent_codex_thread_id": thread_ids_by_row_id.get(
                    source.parent_rodex_sessions_codex_threads_id
                ),
                "thread_depth": source.thread_depth,
                "agent_path": source.agent_path,
                "agent_nickname": source.agent_nickname,
                "history_inheritance_kind": source.history_inheritance_kind,
                "spawning_codex_turn_id": source.spawning_codex_turn_id,
                "first_linked_at_utc": source.first_linked_at_utc,
                "rollout_file_path": source.rollout_file_path,
                "verified_at_utc": source.verified_at_utc,
            }
            for source in sources
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)


def _parse_trace_arguments(arguments: list[str]) -> dict[str, Any]:
    follow = False
    include_bodies = False
    as_json = False
    after_event_id: str | None = None
    limit = TRACE_DEFAULT_LIMIT
    index = 2
    while index < len(arguments):
        option = arguments[index]
        if option in {"-f", "--follow"} and not follow:
            follow = True
            index += 1
        elif option == "--include-bodies" and not include_bodies:
            include_bodies = True
            index += 1
        elif option == "--json" and not as_json:
            as_json = True
            index += 1
        elif option in {"--after", "--limit"} and index + 1 < len(arguments):
            value = arguments[index + 1]
            if option == "--after" and after_event_id is None:
                try:
                    parsed_event_id = uuid.UUID(value)
                except ValueError as error:
                    raise RodexLaunchError(TRACE_USAGE) from error
                if str(parsed_event_id) != value:
                    raise RodexLaunchError(TRACE_USAGE)
                after_event_id = value
            elif option == "--limit":
                try:
                    parsed_limit = int(value)
                except ValueError as error:
                    raise RodexLaunchError(TRACE_USAGE) from error
                if parsed_limit <= 0:
                    raise RodexLaunchError(TRACE_USAGE)
                limit = parsed_limit
            else:
                raise RodexLaunchError(TRACE_USAGE)
            index += 2
        else:
            raise RodexLaunchError(TRACE_USAGE)
    if as_json and follow:
        raise RodexLaunchError("--json snapshot envelopes cannot be combined with --follow")
    return {
        "follow": follow,
        "include_bodies": include_bodies,
        "as_json": as_json,
        "after_event_id": after_event_id,
        "limit": limit,
    }


def _show_or_follow_trace(
    session_name: str,
    session_id: int,
    database_path: Path,
    *,
    follow: bool,
    include_bodies: bool,
    as_json: bool,
    after_event_id: str | None,
    limit: int,
) -> None:
    cursor = after_event_id
    first = True
    while True:
        snapshot = read_rodex_agent_trace(
            session_id,
            database_path,
            after_event_id=cursor,
            limit=limit,
        )
        events = [dict(event) for event in snapshot.events]
        if include_bodies and events:
            _attach_authenticated_rollout_bodies(
                events,
                session_id=session_id,
                database_path=database_path,
            )
        if as_json:
            print(
                json.dumps(
                    {
                        "rodex_session_name": session_name,
                        "trace_publication_sequence": snapshot.trace_publication_sequence,
                        "trace_schema_version": snapshot.trace_schema_version,
                        "calculated_at_utc": snapshot.calculated_at_utc,
                        "coverage_state": snapshot.coverage_state,
                        "durable_event_count": snapshot.durable_event_count,
                        "unrecognized_record_count": snapshot.unrecognized_record_count,
                        "events": events,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                flush=True,
            )
        else:
            for event in events:
                print(
                    json.dumps(event, ensure_ascii=False, separators=(",", ":")),
                    flush=True,
                )
        if events:
            cursor = str(events[-1]["event_id"])
        if not follow:
            return
        if first and snapshot.trace_publication_sequence is None:
            print(
                json.dumps(
                    {
                        "status": "waiting_for_first_trace_publication",
                        "rodex_session_name": session_name,
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
        first = False
        time.sleep(TRACE_FOLLOW_POLL_SECONDS)


def _attach_authenticated_rollout_bodies(
    events: list[dict[str, Any]],
    *,
    session_id: int,
    database_path: Path,
) -> None:
    sources = {
        str(source.codex_thread_id): source
        for source in list_rodex_session_codex_rollout_sources(session_id, database_path)
    }
    records_by_thread: dict[str, dict[int, dict[str, Any]]] = {}
    for event in events:
        thread_id = str(event["codex_thread_id"])
        records = records_by_thread.get(thread_id)
        if records is None:
            source = sources.get(thread_id)
            records = {} if source is None else _authenticated_source_records(source)
            records_by_thread[thread_id] = records
        record = records.get(int(event["source_record_ordinal"]))
        event["body"] = (
            {"capture_state": "unavailable"}
            if record is None
            else _safe_event_body(str(event["event_kind"]), record)
        )


def _authenticated_source_records(source: Any) -> dict[int, dict[str, Any]]:
    if (
        source.rollout_file_path is None
        or source.analyzed_size_bytes is None
        or source.analyzed_prefix_sha256 is None
    ):
        return {}
    descriptor = open_rollout_descriptor(Path(source.rollout_file_path))
    try:
        content = _pread_exact_prefix(descriptor, source.analyzed_size_bytes)
    finally:
        os.close(descriptor)
    if (
        len(content) != source.analyzed_size_bytes
        or hashlib.sha256(content).hexdigest() != source.analyzed_prefix_sha256
    ):
        raise RodexLaunchError(
            f"authenticated rollout prefix changed for thread {source.codex_thread_id}"
        )
    records: dict[int, dict[str, Any]] = {}
    for physical_ordinal, line in enumerate(content.splitlines()):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        ordinal = record.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            ordinal = physical_ordinal
        if ordinal in records and records[ordinal] != record:
            raise RodexLaunchError(
                f"duplicate rollout ordinal for thread {source.codex_thread_id}: {ordinal}"
            )
        records[ordinal] = record
    return records


def _pread_exact_prefix(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(size - offset, 1024 * 1024), offset)
        if not chunk:
            raise RodexLaunchError("authenticated rollout ended during body lookup")
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _safe_event_body(event_kind: str, record: dict[str, Any]) -> dict[str, Any]:
    if event_kind not in {"message", "command_execution", "tool_call"}:
        return {"capture_state": "redacted", "reason": "metadata_only_event"}
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return {"capture_state": "unavailable"}
    item = payload.get("item")
    item = item if isinstance(item, dict) else payload
    if payload.get("type") in {"reasoning", "reasoning_content"} or item.get("type") in {
        "Reasoning",
        "ReasoningContent",
    }:
        return {"capture_state": "redacted", "reason": "hidden_reasoning"}
    if event_kind == "message":
        content = next(
            (
                item[key]
                for key in ("content", "message", "text")
                if key in item and item[key] is not None
            ),
            None,
        )
        selected = {
            "content": _redact_message_content(content),
            "phase": item.get("phase"),
        }
    elif event_kind == "command_execution":
        selected = {
            key: item.get(key)
            for key in (
                "command",
                "cwd",
                "status",
                "duration",
                "duration_ms",
                "exit_code",
                "stdout",
                "stderr",
                "aggregated_output",
                "aggregatedOutput",
            )
            if key in item
        }
    elif event_kind == "tool_call":
        selected = {
            key: payload.get(key)
            for key in (
                "name",
                "tool",
                "call_id",
                "arguments",
                "input",
                "output",
                "status",
            )
            if key in payload
        }
        if item is not payload:
            selected["item"] = item
    return {"capture_state": "rollout_reference", "value": _redact_encrypted(selected)}


def _redact_encrypted(value: Any) -> Any:
    if isinstance(value, str):
        return redact_codex_encrypted_text(value)
    if isinstance(value, list):
        return [_redact_encrypted(item) for item in value]
    if isinstance(value, dict):
        value_type = value.get("type")
        if isinstance(value_type, str) and "reasoning" in value_type.lower():
            return {"capture_state": "redacted", "reason": "hidden_reasoning"}
        return {key: _redact_encrypted(item) for key, item in value.items()}
    return value


def _redact_message_content(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_encrypted(value)
    if isinstance(value, list):
        return [_redact_message_content(item) for item in value]
    if isinstance(value, dict):
        value_type = value.get("type")
        normalized_type = value_type.lower() if isinstance(value_type, str) else None
        if normalized_type is not None and "reasoning" in normalized_type:
            return {"capture_state": "redacted", "reason": "hidden_reasoning"}
        if normalized_type is not None and normalized_type not in {
            "input_text",
            "output_text",
            "text",
        }:
            return {"capture_state": "redacted", "reason": "unknown_message_block"}
        if normalized_type is None and set(value).difference({"text"}):
            return {"capture_state": "redacted", "reason": "unknown_message_block"}
        return _redact_encrypted(value)
    if value is None:
        return None
    return {"capture_state": "redacted", "reason": "unknown_message_block"}
