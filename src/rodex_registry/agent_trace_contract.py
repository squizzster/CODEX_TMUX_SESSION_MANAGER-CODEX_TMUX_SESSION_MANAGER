"""Immutable, completely validated contracts for agent-trace publication."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
import weakref
from dataclasses import asdict, dataclass, field, replace
from threading import Lock
from typing import Any

from .identity import (
    CodexThreadId,
    parse_codex_thread_id,
    parse_codex_turn_id,
)
from .validation import _normalise_required_text, _normalise_utc_timestamp_text

TRACE_EVENT_KINDS = frozenset(
    {
        "session_metadata",
        "turn_context",
        "turn_started",
        "turn_completed",
        "turn_aborted",
        "message",
        "tool_call",
        "command_execution",
        "subagent_activity",
        "token_usage",
        "rate_limit",
        "compaction",
        "unrecognized_record",
    }
)

_CAPTURE_STATES = frozenset({"rollout_reference", "encrypted", "redacted", "unavailable"})
_PREPARED_TRACE_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True)
class TraceMessage:
    item_id: str | None
    message_phase: str
    message_role: str
    content_block_count: int
    body_utf8_bytes: int
    body_capture_state: str


@dataclass(frozen=True, slots=True)
class TraceToolCall:
    item_id: str | None
    call_id: str | None
    tool_name: str
    tool_status: str | None
    request_utf8_bytes: int
    response_utf8_bytes: int
    payload_capture_state: str
    activity_kind: str


@dataclass(frozen=True, slots=True)
class TraceCommandExecution:
    item_id: str | None
    command_argument_count: int
    working_directory: str | None
    command_status: str | None
    duration_ms: int | None
    exit_code: int | None
    stdout_utf8_bytes: int
    stderr_utf8_bytes: int
    aggregated_output_utf8_bytes: int
    payload_capture_state: str


@dataclass(frozen=True, slots=True)
class TraceContext:
    model: str | None
    reasoning_effort: str | None
    working_directory: str | None
    sandbox_mode: str | None
    approval_policy: str | None
    permission_profile_type: str | None
    workspace_root_count: int


@dataclass(frozen=True, slots=True)
class TraceTokenUsage:
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None
    context_used_percent: float | None


@dataclass(frozen=True, slots=True)
class TraceRateLimitWindow:
    limit_id: str
    used_percent: float | None
    window_minutes: int | None
    resets_at_unix_seconds: int | None
    plan_type: str | None


@dataclass(frozen=True, slots=True)
class TraceRateLimits:
    windows: tuple[TraceRateLimitWindow, ...]


@dataclass(frozen=True, slots=True)
class TraceSubagentActivity:
    target_codex_thread_id: CodexThreadId | None
    activity_kind: str
    agent_path: str | None
    collaboration_call_id: str | None = None


type TraceDetail = (
    TraceMessage
    | TraceToolCall
    | TraceCommandExecution
    | TraceContext
    | TraceTokenUsage
    | TraceRateLimits
    | TraceSubagentActivity
    | None
)

_TRACE_DETAIL_TYPES: dict[str, type[object] | None] = {
    "session_metadata": None,
    "turn_context": TraceContext,
    "turn_started": None,
    "turn_completed": None,
    "turn_aborted": None,
    "message": TraceMessage,
    "tool_call": TraceToolCall,
    "command_execution": TraceCommandExecution,
    "subagent_activity": TraceSubagentActivity,
    "token_usage": TraceTokenUsage,
    "rate_limit": TraceRateLimits,
    "compaction": None,
    "unrecognized_record": None,
}


@dataclass(frozen=True, slots=True)
class RodexAgentTraceEvent:
    codex_thread_id: CodexThreadId
    codex_turn_id: str | None
    source_record_ordinal: int
    derived_event_ordinal: int
    event_kind: str
    event_time_utc: str | None
    detail: TraceDetail = None


@dataclass(frozen=True, slots=True)
class RodexAgentTracePublication:
    based_on_trace_publication_sequence: int | None
    trace_schema_version: str
    calculated_at_utc: str
    coverage_state: str
    events: tuple[RodexAgentTraceEvent, ...]


@dataclass(frozen=True, slots=True, init=False)
class PreparedAgentTraceEvent:
    event: RodexAgentTraceEvent
    codex_thread_id: CodexThreadId
    source_key: tuple[CodexThreadId, int, int]
    event_kind: str
    detail_sha256: str
    _contract_token: object = field(init=False, repr=False, compare=False)

    def __init__(
        self,
        *,
        event: RodexAgentTraceEvent,
        codex_thread_id: CodexThreadId,
        source_key: tuple[CodexThreadId, int, int],
        event_kind: str,
        detail_sha256: str,
        _contract_token: object | None = None,
    ) -> None:
        if _contract_token is not _PREPARED_TRACE_FACTORY_TOKEN:
            raise TypeError("trace events must be contract-prepared")
        object.__setattr__(self, "event", event)
        object.__setattr__(self, "codex_thread_id", codex_thread_id)
        object.__setattr__(self, "source_key", source_key)
        object.__setattr__(self, "event_kind", event_kind)
        object.__setattr__(self, "detail_sha256", detail_sha256)
        object.__setattr__(self, "_contract_token", _contract_token)


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class PreparedAgentTracePublication:
    based_on_trace_publication_sequence: int | None
    trace_schema_version: str
    calculated_at_utc: str
    coverage_state: str
    events: tuple[PreparedAgentTraceEvent, ...]
    source_thread_ids: frozenset[CodexThreadId]
    _contract_token: object = field(init=False, repr=False, compare=False)

    def __init__(
        self,
        *,
        based_on_trace_publication_sequence: int | None,
        trace_schema_version: str,
        calculated_at_utc: str,
        coverage_state: str,
        events: tuple[PreparedAgentTraceEvent, ...],
        source_thread_ids: frozenset[CodexThreadId],
        _contract_token: object | None = None,
    ) -> None:
        if _contract_token is not _PREPARED_TRACE_FACTORY_TOKEN:
            raise TypeError("trace publications must be contract-prepared")
        object.__setattr__(
            self,
            "based_on_trace_publication_sequence",
            based_on_trace_publication_sequence,
        )
        object.__setattr__(self, "trace_schema_version", trace_schema_version)
        object.__setattr__(self, "calculated_at_utc", calculated_at_utc)
        object.__setattr__(self, "coverage_state", coverage_state)
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "source_thread_ids", source_thread_ids)
        object.__setattr__(self, "_contract_token", _contract_token)


_PREPARED_PUBLICATIONS: weakref.WeakValueDictionary[int, PreparedAgentTracePublication] = (
    weakref.WeakValueDictionary()
)
_PREPARED_PUBLICATIONS_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class RodexAgentTracePublishReceipt:
    trace_publication_sequence: int
    durable_event_count: int
    unrecognized_record_count: int


@dataclass(frozen=True, slots=True)
class RodexAgentTraceSnapshot:
    trace_publication_sequence: int | None
    trace_schema_version: str | None
    calculated_at_utc: str | None
    coverage_state: str | None
    durable_event_count: int
    unrecognized_record_count: int
    events: tuple[dict[str, Any], ...]


def prepare_agent_trace_publication(
    publication: RodexAgentTracePublication,
) -> PreparedAgentTracePublication:
    """Normalize, validate, and hash the complete immutable input before SQL."""
    if not isinstance(publication, RodexAgentTracePublication):
        raise TypeError("publication must be a RodexAgentTracePublication")
    based_on = publication.based_on_trace_publication_sequence
    if based_on is not None:
        _positive_integer(based_on, "publication sequence")
    schema_version = _normalise_required_text(
        publication.trace_schema_version, "trace_schema_version"
    )
    calculated_at = _normalise_utc_timestamp_text(publication.calculated_at_utc)
    coverage = _normalise_required_text(publication.coverage_state, "coverage_state")
    if coverage not in {"complete", "gapped"}:
        raise ValueError(f"unsupported agent trace coverage state: {coverage}")
    if not isinstance(publication.events, tuple):
        raise TypeError("agent trace events must be an immutable tuple")
    seen_keys: set[tuple[CodexThreadId, int, int]] = set()
    prepared_events: list[PreparedAgentTraceEvent] = []
    source_thread_ids: set[CodexThreadId] = set()
    for source_event in publication.events:
        event = _validate_event(source_event)
        key = (
            event.codex_thread_id,
            event.source_record_ordinal,
            event.derived_event_ordinal,
        )
        if key in seen_keys:
            raise ValueError("agent trace batch contains a duplicate source event key")
        seen_keys.add(key)
        source_thread_ids.add(event.codex_thread_id)
        prepared_events.append(
            PreparedAgentTraceEvent(
                event=event,
                codex_thread_id=event.codex_thread_id,
                source_key=key,
                event_kind=event.event_kind,
                detail_sha256=_trace_detail_sha256(event.event_kind, event.detail),
                _contract_token=_PREPARED_TRACE_FACTORY_TOKEN,
            )
        )
    prepared = PreparedAgentTracePublication(
        based_on_trace_publication_sequence=based_on,
        trace_schema_version=schema_version,
        calculated_at_utc=calculated_at,
        coverage_state=coverage,
        events=tuple(prepared_events),
        source_thread_ids=frozenset(source_thread_ids),
        _contract_token=_PREPARED_TRACE_FACTORY_TOKEN,
    )
    with _PREPARED_PUBLICATIONS_LOCK:
        _PREPARED_PUBLICATIONS[id(prepared)] = prepared
    return prepared


def require_contract_prepared_agent_trace_publication(
    publication: object,
) -> PreparedAgentTracePublication:
    """Accept only an immutable value issued by this contract's preparation step."""
    if not isinstance(publication, PreparedAgentTracePublication):
        raise TypeError("trace writer requires a contract-prepared publication")
    with _PREPARED_PUBLICATIONS_LOCK:
        registered = _PREPARED_PUBLICATIONS.get(id(publication))
    if registered is not publication:
        raise TypeError("trace writer requires a contract-prepared publication")
    return publication


def _validate_event(event: RodexAgentTraceEvent) -> RodexAgentTraceEvent:
    if not isinstance(event, RodexAgentTraceEvent):
        raise TypeError("agent trace events must be RodexAgentTraceEvent values")
    thread_id = parse_codex_thread_id(event.codex_thread_id)
    turn_id = None
    if event.codex_turn_id is not None:
        turn_id = str(parse_codex_turn_id(event.codex_turn_id))
        if turn_id != event.codex_turn_id:
            raise ValueError("codex_turn_id must use canonical lowercase UUID text")
    source_ordinal = _nonnegative_integer(
        event.source_record_ordinal, "source_record_ordinal"
    )
    derived_ordinal = _nonnegative_integer(
        event.derived_event_ordinal, "derived_event_ordinal"
    )
    kind = _normalise_required_text(event.event_kind, "event_kind")
    if kind not in TRACE_EVENT_KINDS:
        raise ValueError(f"unsupported agent trace event kind: {kind}")
    event_time = (
        None
        if event.event_time_utc is None
        else _normalise_utc_timestamp_text(event.event_time_utc)
    )
    detail = _validate_detail(kind, event.detail)
    return replace(
        event,
        codex_thread_id=thread_id,
        codex_turn_id=turn_id,
        source_record_ordinal=source_ordinal,
        derived_event_ordinal=derived_ordinal,
        event_kind=kind,
        event_time_utc=event_time,
        detail=detail,
    )


def _validate_detail(kind: str, detail: TraceDetail) -> TraceDetail:
    expected_type = _TRACE_DETAIL_TYPES[kind]
    if expected_type is None:
        if detail is not None:
            raise ValueError(f"agent trace {kind} event cannot have typed detail")
        return None
    if not isinstance(detail, expected_type):
        raise ValueError(
            f"agent trace {kind} event requires {expected_type.__name__} detail"
        )
    if isinstance(detail, TraceMessage):
        return replace(
            detail,
            item_id=_optional_item_id(detail.item_id),
            message_phase=_one_of(
                detail.message_phase,
                "message_phase",
                {"commentary", "final_answer", "analysis", "unknown"},
            ),
            message_role=_one_of(
                detail.message_role,
                "message_role",
                {"assistant", "user", "system", "unknown"},
            ),
            content_block_count=_nonnegative_integer(
                detail.content_block_count, "content_block_count"
            ),
            body_utf8_bytes=_nonnegative_integer(detail.body_utf8_bytes, "body_utf8_bytes"),
            body_capture_state=_one_of(
                detail.body_capture_state, "body_capture_state", _CAPTURE_STATES
            ),
        )
    elif isinstance(detail, TraceToolCall):
        return replace(
            detail,
            item_id=_optional_item_id(detail.item_id),
            call_id=_optional_text(detail.call_id, "call_id"),
            tool_name=_normalise_required_text(detail.tool_name, "tool_name"),
            tool_status=_optional_text(detail.tool_status, "tool_status"),
            request_utf8_bytes=_nonnegative_integer(
                detail.request_utf8_bytes, "request_utf8_bytes"
            ),
            response_utf8_bytes=_nonnegative_integer(
                detail.response_utf8_bytes, "response_utf8_bytes"
            ),
            payload_capture_state=_one_of(
                detail.payload_capture_state, "payload_capture_state", _CAPTURE_STATES
            ),
            activity_kind=_one_of(
                detail.activity_kind,
                "activity_kind",
                {"request", "output", "status"},
            ),
        )
    elif isinstance(detail, TraceCommandExecution):
        return replace(
            detail,
            item_id=_optional_item_id(detail.item_id),
            command_argument_count=_nonnegative_integer(
                detail.command_argument_count, "command_argument_count"
            ),
            working_directory=_optional_text(detail.working_directory, "working_directory"),
            command_status=_optional_text(detail.command_status, "command_status"),
            duration_ms=_optional_nonnegative_integer(detail.duration_ms, "duration_ms"),
            exit_code=_optional_integer(detail.exit_code, "exit_code"),
            stdout_utf8_bytes=_nonnegative_integer(
                detail.stdout_utf8_bytes, "stdout_utf8_bytes"
            ),
            stderr_utf8_bytes=_nonnegative_integer(
                detail.stderr_utf8_bytes, "stderr_utf8_bytes"
            ),
            aggregated_output_utf8_bytes=_nonnegative_integer(
                detail.aggregated_output_utf8_bytes,
                "aggregated_output_utf8_bytes",
            ),
            payload_capture_state=_one_of(
                detail.payload_capture_state, "payload_capture_state", _CAPTURE_STATES
            ),
        )
    elif isinstance(detail, TraceContext):
        return replace(
            detail,
            model=_optional_text(detail.model, "model"),
            reasoning_effort=_optional_text(detail.reasoning_effort, "reasoning_effort"),
            working_directory=_optional_text(detail.working_directory, "working_directory"),
            sandbox_mode=_optional_text(detail.sandbox_mode, "sandbox_mode"),
            approval_policy=_optional_text(detail.approval_policy, "approval_policy"),
            permission_profile_type=_optional_text(
                detail.permission_profile_type, "permission_profile_type"
            ),
            workspace_root_count=_nonnegative_integer(
                detail.workspace_root_count, "workspace_root_count"
            ),
        )
    elif isinstance(detail, TraceTokenUsage):
        replacements: dict[str, int | float | None] = {}
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        ):
            replacements[name] = _optional_nonnegative_integer(getattr(detail, name), name)
        replacements["context_used_percent"] = _optional_percentage(
            detail.context_used_percent, "context_used_percent"
        )
        return replace(detail, **replacements)
    elif isinstance(detail, TraceRateLimits):
        if not isinstance(detail.windows, tuple):
            raise TypeError("rate-limit windows must be an immutable tuple")
        windows: list[TraceRateLimitWindow] = []
        for window in detail.windows:
            if not isinstance(window, TraceRateLimitWindow):
                raise TypeError("rate-limit windows must be TraceRateLimitWindow values")
            windows.append(
                replace(
                    window,
                    limit_id=_normalise_required_text(window.limit_id, "limit_id"),
                    used_percent=_optional_percentage(window.used_percent, "used_percent"),
                    window_minutes=_optional_positive_integer(
                        window.window_minutes, "window_minutes"
                    ),
                    resets_at_unix_seconds=_optional_nonnegative_integer(
                        window.resets_at_unix_seconds, "resets_at_unix_seconds"
                    ),
                    plan_type=_optional_text(window.plan_type, "plan_type"),
                )
            )
        return replace(detail, windows=tuple(windows))
    elif isinstance(detail, TraceSubagentActivity):
        target_thread_id = (
            None
            if detail.target_codex_thread_id is None
            else parse_codex_thread_id(detail.target_codex_thread_id)
        )
        return replace(
            detail,
            target_codex_thread_id=target_thread_id,
            activity_kind=_normalise_required_text(detail.activity_kind, "activity_kind"),
            agent_path=_optional_text(detail.agent_path, "agent_path"),
            collaboration_call_id=_optional_text(
                detail.collaboration_call_id, "collaboration_call_id"
            ),
        )
    raise AssertionError("trace detail type table and validation branches diverged")


def _trace_detail_sha256(kind: str, detail: TraceDetail) -> str:
    expected_type = _TRACE_DETAIL_TYPES[kind]
    value: object = None if detail is None else asdict(detail)
    detail_type = "none" if expected_type is None else expected_type.__name__
    canonical = json.dumps(
        {"detail_type": detail_type, "value": value},
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _optional_item_id(value: str | None) -> str | None:
    if value is None:
        return None
    text = _normalise_required_text(value, "codex_item_id")
    try:
        parsed = uuid.UUID(text)
    except ValueError:
        return text
    if str(parsed) != text:
        raise ValueError("Codex item UUID must use canonical lowercase UUID text")
    return text


def _one_of(value: str, name: str, accepted: set[str] | frozenset[str]) -> str:
    normalized = _normalise_required_text(value, name)
    if normalized not in accepted:
        raise ValueError(f"unsupported {name}: {normalized}")
    return normalized


def _optional_text(value: str | None, name: str) -> str | None:
    return None if value is None else _normalise_required_text(value, name)


def _nonnegative_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_nonnegative_integer(value: int | None, name: str) -> int | None:
    return None if value is None else _nonnegative_integer(value, name)


def _optional_positive_integer(value: int | None, name: str) -> int | None:
    return None if value is None else _positive_integer(value, name)


def _optional_integer(value: int | None, name: str) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise ValueError(f"{name} must be an integer or null")
    return value


def _optional_percentage(value: float | None, name: str) -> float | None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number or null")
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return float(value)
