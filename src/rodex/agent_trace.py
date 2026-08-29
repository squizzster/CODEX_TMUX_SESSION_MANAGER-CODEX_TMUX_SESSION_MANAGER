"""Normalize authenticated Codex rollout records into typed trace facts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from rodex_registry import (
    CodexThreadId,
    RodexAgentTraceEvent,
    RodexAgentTracePublication,
    TraceCommandExecution,
    TraceContext,
    TraceMessage,
    TraceRateLimits,
    TraceRateLimitWindow,
    TraceSubagentActivity,
    TraceTokenUsage,
    TraceToolCall,
    parse_codex_thread_id,
    parse_codex_turn_id,
)

from .agent_trace_privacy import contains_codex_encrypted_value

AGENT_TRACE_SCHEMA_VERSION = "rodex-agent-trace-v2"
type AgentTraceSource = (
    tuple[CodexThreadId, bytes] | tuple[CodexThreadId, bytes, int | Sequence[int]]
)


class StatefulAgentTraceNormalizer:
    """Retain turn and tool-call context only after the matching SQL commit."""

    def __init__(self) -> None:
        self._accepted_active_turns: dict[CodexThreadId, str | None] = {}
        self._candidate_active_turns: dict[CodexThreadId, str | None] | None = None
        self._accepted_tool_names: dict[tuple[CodexThreadId, str], str] = {}
        self._candidate_tool_names: dict[tuple[CodexThreadId, str], str] | None = None

    def prepare(
        self,
        sources: Sequence[AgentTraceSource],
        *,
        based_on_trace_publication_sequence: int | None,
        calculated_at_utc: str,
        source_coverage_state: str = "complete",
    ) -> RodexAgentTracePublication:
        candidate = dict(self._accepted_active_turns)
        candidate_tool_names = dict(self._accepted_tool_names)
        publication = normalize_rollout_trace(
            sources,
            based_on_trace_publication_sequence=based_on_trace_publication_sequence,
            calculated_at_utc=calculated_at_utc,
            source_coverage_state=source_coverage_state,
            _active_turns=candidate,
            _tool_names=candidate_tool_names,
        )
        self._candidate_active_turns = candidate
        self._candidate_tool_names = candidate_tool_names
        return publication

    def warmup(self, sources: Sequence[AgentTraceSource]) -> None:
        """Restore source-local context from an accepted prefix without publishing it."""
        active_turns = dict(self._accepted_active_turns)
        tool_names = dict(self._accepted_tool_names)
        normalize_rollout_trace(
            sources,
            based_on_trace_publication_sequence=None,
            calculated_at_utc="warmup",
            _active_turns=active_turns,
            _tool_names=tool_names,
        )
        self._accepted_active_turns = active_turns
        self._accepted_tool_names = tool_names

    def accept_batch(self) -> None:
        if self._candidate_active_turns is not None:
            self._accepted_active_turns = self._candidate_active_turns
        if self._candidate_tool_names is not None:
            self._accepted_tool_names = self._candidate_tool_names
        self._candidate_active_turns = None
        self._candidate_tool_names = None

    def require_clean_replay(self) -> None:
        self._accepted_active_turns.clear()
        self._candidate_active_turns = None
        self._accepted_tool_names.clear()
        self._candidate_tool_names = None


def normalize_rollout_trace(
    sources: Sequence[AgentTraceSource],
    *,
    based_on_trace_publication_sequence: int | None,
    calculated_at_utc: str,
    source_coverage_state: str = "complete",
    _active_turns: dict[CodexThreadId, str | None] | None = None,
    _tool_names: dict[tuple[CodexThreadId, str], str] | None = None,
) -> RodexAgentTracePublication:
    """Project exact ordinal-addressed records without retaining sensitive bodies."""
    events: list[RodexAgentTraceEvent] = []
    coverage_gapped = source_coverage_state != "complete"
    active_turns = {} if _active_turns is None else _active_turns
    tool_names = {} if _tool_names is None else _tool_names
    for source in sources:
        thread_id, content = source[:2]
        lines = content.splitlines()
        coordinate_spec = 0 if len(source) == 2 else source[2]
        if isinstance(coordinate_spec, int) and not isinstance(coordinate_spec, bool):
            if coordinate_spec < 0:
                raise ValueError("agent trace source line offset must be non-negative")
            physical_ordinals = tuple(range(coordinate_spec, coordinate_spec + len(lines)))
        else:
            physical_ordinals = tuple(coordinate_spec)
            if len(physical_ordinals) != len(lines) or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in physical_ordinals
            ):
                raise ValueError("agent trace source ordinals must match its records")
        parsed_thread_id = parse_codex_thread_id(thread_id)
        active_turn_id = active_turns.get(parsed_thread_id)
        for physical_ordinal, line in zip(physical_ordinals, lines, strict=True):
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeError, ValueError):
                coverage_gapped = True
                continue
            if not isinstance(record, Mapping):
                coverage_gapped = True
                continue
            ordinal = _integer(record.get("ordinal"))
            if ordinal is None or ordinal < 0:
                ordinal = physical_ordinal
            payload = _mapping(record.get("payload"))
            record_type = _text(record.get("type"))
            record_turn_id = _canonical_record_turn_id(record_type, payload)
            if record_turn_id is not None:
                active_turn_id = record_turn_id
            normalized = _normalize_record(
                parsed_thread_id,
                active_turn_id,
                ordinal,
                _text(record.get("timestamp")),
                record_type,
                payload,
                tool_names,
            )
            events.extend(normalized)
            if payload.get("type") in {"task_complete", "turn_aborted"}:
                active_turn_id = None
        active_turns[parsed_thread_id] = active_turn_id
    coverage = (
        "gapped"
        if coverage_gapped
        or any(event.event_kind == "unrecognized_record" for event in events)
        else "complete"
    )
    return RodexAgentTracePublication(
        based_on_trace_publication_sequence=based_on_trace_publication_sequence,
        trace_schema_version=AGENT_TRACE_SCHEMA_VERSION,
        calculated_at_utc=calculated_at_utc,
        coverage_state=coverage,
        events=tuple(events),
    )


def _canonical_record_turn_id(
    record_type: str | None,
    payload: Mapping[str, Any],
) -> str | None:
    """Resolve one record's turn through the authoritative Codex metadata path."""
    direct_turn_id = _text(_first_present(payload, "turn_id", "turnId"))
    response_metadata = _mapping(
        _first_present(
            payload,
            "internal_chat_message_metadata_passthrough",
            "internalChatMessageMetadataPassthrough",
        )
    )
    response_turn_id = _text(_first_present(response_metadata, "turn_id", "turnId"))
    candidate = (
        response_turn_id or direct_turn_id
        if record_type == "response_item"
        else direct_turn_id or response_turn_id
    )
    return None if candidate is None else str(parse_codex_turn_id(candidate))


def _normalize_record(
    thread_id: CodexThreadId,
    active_turn_id: str | None,
    ordinal: int,
    timestamp: str | None,
    record_type: str | None,
    payload: Mapping[str, Any],
    tool_names: dict[tuple[CodexThreadId, str], str],
) -> tuple[RodexAgentTraceEvent, ...]:
    base = {
        "codex_thread_id": thread_id,
        "codex_turn_id": active_turn_id,
        "source_record_ordinal": ordinal,
        "derived_event_ordinal": 0,
        "event_time_utc": timestamp,
    }
    if record_type == "session_meta":
        return (RodexAgentTraceEvent(**base, event_kind="session_metadata"),)
    if record_type == "turn_context":
        roots = payload.get("workspace_roots") or payload.get("workspaceRoots")
        permission = _mapping(payload.get("permission_profile"))
        sandbox = payload.get("sandbox_policy") or payload.get("sandboxPolicy")
        sandbox_mapping = _mapping(sandbox)
        return (
            RodexAgentTraceEvent(
                **base,
                event_kind="turn_context",
                detail=TraceContext(
                    model=_text(payload.get("model")),
                    reasoning_effort=_text(
                        payload.get("effort") or payload.get("reasoning_effort")
                    ),
                    working_directory=_text(payload.get("cwd")),
                    sandbox_mode=_text(
                        sandbox_mapping.get("type")
                        or sandbox_mapping.get("mode")
                        or sandbox
                    ),
                    approval_policy=_text(
                        payload.get("approval_policy") or payload.get("approvalPolicy")
                    ),
                    permission_profile_type=_text(permission.get("type")),
                    workspace_root_count=(len(roots) if isinstance(roots, list) else 0),
                ),
            ),
        )
    if record_type in {"compacted", "context_compacted"}:
        return (RodexAgentTraceEvent(**base, event_kind="compaction"),)
    if record_type == "response_item":
        return _normalize_response_item(base, payload, tool_names)
    if record_type != "event_msg":
        return (RodexAgentTraceEvent(**base, event_kind="unrecognized_record"),)
    payload_type = _text(payload.get("type"))
    if payload_type == "task_started":
        return (RodexAgentTraceEvent(**base, event_kind="turn_started"),)
    if payload_type == "task_complete":
        return (RodexAgentTraceEvent(**base, event_kind="turn_completed"),)
    if payload_type == "turn_aborted":
        return (RodexAgentTraceEvent(**base, event_kind="turn_aborted"),)
    if payload_type in {"compacted", "context_compacted"}:
        return (RodexAgentTraceEvent(**base, event_kind="compaction"),)
    if payload_type in {"token_count", "token_usage"}:
        return _normalize_token_record(base, payload)
    if payload_type in {"user_message", "agent_message"}:
        content = _first_present(payload, "message", "content", "text")
        return (
            RodexAgentTraceEvent(
                **base,
                event_kind="message",
                detail=_message_detail(
                    payload,
                    content,
                    role="user" if payload_type == "user_message" else "assistant",
                ),
            ),
        )
    if payload_type == "sub_agent_activity":
        return (
            RodexAgentTraceEvent(
                **base,
                event_kind="subagent_activity",
                detail=_subagent_detail(payload),
            ),
        )
    if payload_type in {"item_completed", "item_started"}:
        item = _mapping(payload.get("item"))
        return _normalize_item(base, item)
    if payload_type == "exec_command_end":
        return (
            RodexAgentTraceEvent(
                **base,
                event_kind="command_execution",
                detail=_command_detail(payload),
            ),
        )
    return (RodexAgentTraceEvent(**base, event_kind="unrecognized_record"),)


def _normalize_item(
    base: dict[str, Any], item: Mapping[str, Any]
) -> tuple[RodexAgentTraceEvent, ...]:
    item_type = _text(item.get("type"))
    if item_type == "CommandExecution":
        return (
            RodexAgentTraceEvent(
                **base,
                event_kind="command_execution",
                detail=_command_detail(item),
            ),
        )
    if item_type in {"AgentMessage", "UserMessage"}:
        content = item.get("content")
        return (
            RodexAgentTraceEvent(
                **base,
                event_kind="message",
                detail=_message_detail(
                    item,
                    content,
                    role="user" if item_type == "UserMessage" else "assistant",
                ),
            ),
        )
    if item_type == "SubAgentActivity":
        target = _optional_thread_id(
            item.get("agent_thread_id") or item.get("agentThreadId")
        )
        return (
            RodexAgentTraceEvent(
                **base,
                event_kind="subagent_activity",
                detail=_subagent_detail(item, target=target),
            ),
        )
    if item_type and item_type.endswith(("ToolCall", "ToolExecution")):
        return (
            RodexAgentTraceEvent(
                **base,
                event_kind="tool_call",
                detail=_tool_detail(item, item_type),
            ),
        )
    return (RodexAgentTraceEvent(**base, event_kind="unrecognized_record"),)


def _normalize_response_item(
    base: dict[str, Any],
    payload: Mapping[str, Any],
    tool_names: dict[tuple[CodexThreadId, str], str],
) -> tuple[RodexAgentTraceEvent, ...]:
    payload_type = _text(payload.get("type"))
    request_types = {"function_call", "custom_tool_call"}
    output_types = {"function_call_output", "custom_tool_call_output"}
    if payload_type in request_types | output_types:
        call_id = _text(payload.get("call_id"))
        thread_id = parse_codex_thread_id(base["codex_thread_id"])
        tool_key = None if call_id is None else (thread_id, call_id)
        tool_name = _qualified_tool_name(payload)
        if payload_type in request_types and tool_key is not None:
            tool_names[tool_key] = tool_name
        elif payload_type in output_types and tool_key is not None:
            tool_name = tool_names.pop(tool_key, tool_name)
        body = (
            _first_present(payload, "arguments", "input")
            if payload_type in request_types
            else payload.get("output")
        )
        detail = TraceToolCall(
            item_id=_text(payload.get("id")),
            call_id=call_id,
            tool_name=tool_name,
            tool_status=_text(payload.get("status")) or payload_type,
            request_utf8_bytes=_utf8_bytes(body) if payload_type in request_types else 0,
            response_utf8_bytes=_utf8_bytes(body) if payload_type in output_types else 0,
            payload_capture_state=_capture_state(body),
            activity_kind="request" if payload_type in request_types else "output",
        )
        return (RodexAgentTraceEvent(**base, event_kind="tool_call", detail=detail),)
    if payload_type in {"message", "agent_message"}:
        content = _first_present(payload, "content", "message", "text")
        detail = _message_detail(
            payload,
            content,
            role="assistant" if payload_type == "agent_message" else payload.get("role"),
        )
        return (RodexAgentTraceEvent(**base, event_kind="message", detail=detail),)
    return (RodexAgentTraceEvent(**base, event_kind="unrecognized_record"),)


def _qualified_tool_name(payload: Mapping[str, Any]) -> str:
    name = _text(_first_present(payload, "name", "tool")) or "unknown"
    namespace = _text(payload.get("namespace"))
    if namespace is None or name == "unknown" or name.startswith(f"{namespace}."):
        return name
    return f"{namespace}.{name}"


def _normalize_token_record(
    base: dict[str, Any], payload: Mapping[str, Any]
) -> tuple[RodexAgentTraceEvent, ...]:
    info = _mapping(payload.get("info"))
    usage = _mapping(
        payload.get("usage")
        or info.get("total_token_usage")
        or info.get("last_token_usage")
    )
    context = _first_present(payload, "context_used_percent")
    if context is None:
        context = _first_present(info, "context_used_percent")
    events = [
        RodexAgentTraceEvent(
            **base,
            event_kind="token_usage",
            detail=TraceTokenUsage(
                input_tokens=_integer(usage.get("input_tokens")),
                cached_input_tokens=_integer(usage.get("cached_input_tokens")),
                output_tokens=_integer(usage.get("output_tokens")),
                reasoning_output_tokens=_integer(usage.get("reasoning_output_tokens")),
                total_tokens=_integer(usage.get("total_tokens")),
                context_used_percent=_percentage(context),
            ),
        )
    ]
    limits = _first_present(payload, "rate_limits", "rateLimits")
    if limits is None:
        limits = _first_present(info, "rate_limits")
    windows = _rate_limit_windows(limits)
    if windows:
        rate_base = dict(base)
        rate_base["derived_event_ordinal"] = 1
        events.append(
            RodexAgentTraceEvent(
                **rate_base,
                event_kind="rate_limit",
                detail=TraceRateLimits(tuple(windows)),
            )
        )
    return tuple(events)


def _command_detail(item: Mapping[str, Any]) -> TraceCommandExecution:
    command = item.get("command")
    arguments = command if isinstance(command, list) else [command] if command else []
    stdout = item.get("stdout")
    stderr = item.get("stderr")
    aggregated = _first_present(item, "aggregated_output", "aggregatedOutput")
    return TraceCommandExecution(
        item_id=_text(item.get("id") or item.get("call_id")),
        command_argument_count=len(arguments),
        working_directory=_text(item.get("cwd")),
        command_status=_text(item.get("status")),
        duration_ms=_duration_ms(_first_present(item, "duration_ms", "duration")),
        exit_code=_integer(item.get("exit_code")),
        stdout_utf8_bytes=_utf8_bytes(stdout),
        stderr_utf8_bytes=_utf8_bytes(stderr),
        aggregated_output_utf8_bytes=_utf8_bytes(aggregated),
        payload_capture_state=_capture_state((command, stdout, stderr, aggregated)),
    )


def _tool_detail(item: Mapping[str, Any], fallback_name: str) -> TraceToolCall:
    request_keys = ("arguments", "request", "input")
    response_keys = ("result", "response", "output")
    request = _first_present(item, *request_keys)
    response = _first_present(item, *response_keys)
    activity_kind = (
        "request"
        if any(key in item for key in request_keys)
        else "output"
        if any(key in item for key in response_keys)
        else "status"
    )
    return TraceToolCall(
        item_id=_text(item.get("id")),
        call_id=_text(item.get("call_id") or item.get("callId")),
        tool_name=_text(item.get("name") or item.get("tool")) or fallback_name,
        tool_status=_text(item.get("status")),
        request_utf8_bytes=_utf8_bytes(request),
        response_utf8_bytes=_utf8_bytes(response),
        payload_capture_state=_capture_state((request, response)),
        activity_kind=activity_kind,
    )


def _message_detail(
    item: Mapping[str, Any], content: object, *, role: object
) -> TraceMessage:
    blocks = (
        content if isinstance(content, list) else [content] if content is not None else []
    )
    return TraceMessage(
        item_id=_text(item.get("id")),
        message_phase=_message_phase(item.get("phase")),
        message_role=_message_role(role),
        content_block_count=len(blocks),
        body_utf8_bytes=_utf8_bytes(content),
        body_capture_state=_capture_state(content),
    )


def _subagent_detail(
    item: Mapping[str, Any], *, target: CodexThreadId | None = None
) -> TraceSubagentActivity:
    if target is None:
        target = _optional_thread_id(
            _first_present(
                item,
                "agent_thread_id",
                "agentThreadId",
                "target_thread_id",
                "targetThreadId",
            )
        )
    return TraceSubagentActivity(
        target_codex_thread_id=target,
        activity_kind=_text(_first_present(item, "kind", "status", "activity"))
        or "unknown",
        agent_path=_text(_first_present(item, "agent_path", "agentPath")),
        collaboration_call_id=_text(item.get("id")),
    )


def _rate_limit_windows(value: object) -> list[TraceRateLimitWindow]:
    values = (
        value if isinstance(value, list) else [value] if isinstance(value, Mapping) else []
    )
    windows: list[TraceRateLimitWindow] = []
    for raw in values:
        item = _mapping(raw)
        primary = _mapping(item.get("primary"))
        observed = primary or item
        limit_id = _text(item.get("limit_id") or item.get("limitId"))
        if limit_id is None:
            continue
        windows.append(
            TraceRateLimitWindow(
                limit_id=limit_id,
                used_percent=_percentage(
                    _first_present(observed, "used_percent", "usedPercent")
                ),
                window_minutes=_integer(
                    _first_present(observed, "window_minutes", "windowMinutes")
                ),
                resets_at_unix_seconds=_integer(
                    _first_present(observed, "resets_at", "resetsAt")
                ),
                plan_type=_text(item.get("plan_type") or item.get("planType")),
            )
        )
    return windows


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_present(mapping: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _percentage(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if 0 <= number <= 100 else None
    return None


def _duration_ms(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0:
        return round(value * 1000)
    mapping = _mapping(value)
    seconds = _integer(_first_present(mapping, "secs", "seconds"))
    nanoseconds = _integer(_first_present(mapping, "nanos", "nanoseconds")) or 0
    return None if seconds is None else seconds * 1000 + nanoseconds // 1_000_000


def _utf8_bytes(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    try:
        return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _capture_state(value: object) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "unavailable"
    rendered = value if isinstance(value, str) else json.dumps(value, default=str)
    if contains_codex_encrypted_value(rendered):
        return "encrypted"
    return "rollout_reference" if rendered else "unavailable"


def _message_phase(value: object) -> str:
    phase = _text(value)
    return phase if phase in {"commentary", "final_answer", "analysis"} else "unknown"


def _message_role(value: object) -> str:
    role = _text(value)
    return role if role in {"assistant", "user", "system"} else "unknown"


def _optional_thread_id(value: object) -> CodexThreadId | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return parse_codex_thread_id(text)
    except ValueError:
        return None
