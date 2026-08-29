"""Stateless, bounded projection of App Server events for the observer."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from contextlib import suppress
from typing import Final

from .observer_contract import (
    OBSERVER_PROJECTED_FIELD_MAX_CHARS,
    OBSERVER_PROJECTED_ID_MAX_CHARS,
    OBSERVER_PROJECTED_LIST_ITEM_LIMIT,
    OBSERVER_PROJECTED_TEXT_MAX_CHARS,
    OBSERVER_SCHEMA,
)

_ACTIVITY_METHODS: Final = frozenset({"item/started", "item/completed"})
_COLLABORATION_TOOL_NAMES: Final = {
    "spawnAgent": "collaboration.spawn_agent",
    "followupTask": "collaboration.followup_task",
    "sendMessage": "collaboration.send_message",
}


def project_subagent_activity_event(
    event: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Project one observed App Server sub-agent activity without content fields."""
    if event is None or event.get("method") not in _ACTIVITY_METHODS:
        return None
    method = event["method"]
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    item = params.get("item")
    if not isinstance(item, Mapping) or item.get("type") != "subAgentActivity":
        return None
    item_id = _projected_identifier(item.get("id"))
    thread_id = optional_uuid_text(params.get("threadId"))
    agent_thread_id = optional_uuid_text(item.get("agentThreadId"))
    raw_agent_path = item.get("agentPath")
    raw_activity_kind = item.get("kind")
    raw_turn_id = params.get("turnId")
    turn_id = None if raw_turn_id is None else _projected_identifier(raw_turn_id)
    if (
        item_id is None
        or thread_id is None
        or agent_thread_id is None
        or not isinstance(raw_agent_path, str)
        or not raw_agent_path
        or not isinstance(raw_activity_kind, str)
        or not raw_activity_kind
        or (raw_turn_id is not None and turn_id is None)
    ):
        return None
    truncated: list[str] = []
    projected = {
        "schema": OBSERVER_SCHEMA,
        "kind": "app_server_subagent_activity",
        "method": method,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "item": {
            "type": "subAgentActivity",
            "id": item_id,
            "activity_kind": _projected_text(
                raw_activity_kind,
                "item.activity_kind",
                truncated,
                OBSERVER_PROJECTED_FIELD_MAX_CHARS,
            ),
            "agent_thread_id": agent_thread_id,
            "agent_path": _projected_text(
                raw_agent_path,
                "item.agent_path",
                truncated,
                OBSERVER_PROJECTED_FIELD_MAX_CHARS,
            ),
        },
    }
    return _attach_overflow(projected, truncated, {})


def project_collaboration_invocation_event(
    event: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Project one exact current-Codex collaboration invocation for live display."""
    if event is None or event.get("method") not in _ACTIVITY_METHODS:
        return None
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    item = params.get("item")
    if not isinstance(item, Mapping) or item.get("type") != "collabAgentToolCall":
        return None
    item_id = _projected_identifier(item.get("id"))
    raw_tool_name = item.get("tool")
    status = item.get("status")
    prompt = item.get("prompt")
    thread_id = optional_uuid_text(params.get("threadId"))
    sender_thread_id = optional_uuid_text(item.get("senderThreadId"))
    raw_receiver_ids = item.get("receiverThreadIds")
    raw_turn_id = params.get("turnId")
    turn_id = None if raw_turn_id is None else _projected_identifier(raw_turn_id)
    if (
        item_id is None
        or not isinstance(raw_tool_name, str)
        or not raw_tool_name
        or not isinstance(status, str)
        or not status
        or (prompt is not None and not isinstance(prompt, str))
        or thread_id is None
        or sender_thread_id is None
        or not isinstance(raw_receiver_ids, list)
        or (raw_turn_id is not None and turn_id is None)
    ):
        return None
    bounded_receivers = raw_receiver_ids[:OBSERVER_PROJECTED_LIST_ITEM_LIMIT]
    receivers = [optional_uuid_text(value) for value in bounded_receivers]
    if any(value is None for value in receivers):
        return None
    truncated: list[str] = []
    omitted: dict[str, int] = {}
    tool_name = _projected_text(
        raw_tool_name,
        "item.raw_tool_name",
        truncated,
        OBSERVER_PROJECTED_FIELD_MAX_CHARS,
    )
    if len(raw_receiver_ids) > len(bounded_receivers):
        omitted["item.receiver_thread_ids"] = len(raw_receiver_ids) - len(bounded_receivers)
    projected = {
        "schema": OBSERVER_SCHEMA,
        "kind": "app_server_collaboration_invocation",
        "method": event["method"],
        "thread_id": thread_id,
        "turn_id": turn_id,
        "item": {
            "type": "collabAgentToolCall",
            "id": item_id,
            "raw_tool_name": tool_name,
            "tool_name": _COLLABORATION_TOOL_NAMES.get(tool_name),
            "status": _projected_text(
                status,
                "item.status",
                truncated,
                OBSERVER_PROJECTED_FIELD_MAX_CHARS,
            ),
            "sender_thread_id": sender_thread_id,
            "receiver_thread_ids": receivers,
            "prompt": (
                None
                if prompt is None
                else _projected_text(
                    prompt,
                    "item.prompt",
                    truncated,
                    OBSERVER_PROJECTED_TEXT_MAX_CHARS,
                )
            ),
        },
    }
    return _attach_overflow(projected, truncated, omitted)


def project_agent_message_event(
    event: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Project completed agent-authored text without prompts or reasoning content."""
    if event is None or event.get("method") != "item/completed":
        return None
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    item = params.get("item")
    if not isinstance(item, Mapping) or item.get("type") != "agentMessage":
        return None
    item_id = _projected_identifier(item.get("id"))
    text = item.get("text")
    thread_id = optional_uuid_text(params.get("threadId"))
    raw_turn_id = params.get("turnId")
    turn_id = None if raw_turn_id is None else _projected_identifier(raw_turn_id)
    phase = item.get("phase")
    if (
        item_id is None
        or not isinstance(text, str)
        or not text
        or thread_id is None
        or (raw_turn_id is not None and turn_id is None)
        or (phase is not None and not isinstance(phase, str))
    ):
        return None
    truncated: list[str] = []
    projected = {
        "schema": OBSERVER_SCHEMA,
        "kind": "app_server_agent_message",
        "thread_id": thread_id,
        "turn_id": turn_id,
        "item": {
            "type": "agentMessage",
            "id": item_id,
            "phase": (
                None
                if phase is None
                else _projected_text(
                    phase,
                    "item.phase",
                    truncated,
                    OBSERVER_PROJECTED_FIELD_MAX_CHARS,
                )
            ),
            "text": _projected_text(
                text,
                "item.text",
                truncated,
                OBSERVER_PROJECTED_TEXT_MAX_CHARS,
            ),
        },
    }
    return _attach_overflow(projected, truncated, {})


def project_user_message_event(
    event: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Project exact completed parent-user text for same-turn request correlation."""
    if event is None or event.get("method") != "item/completed":
        return None
    params = event.get("params")
    if not isinstance(params, Mapping):
        return None
    item = params.get("item")
    if not isinstance(item, Mapping) or item.get("type") != "userMessage":
        return None
    item_id = _projected_identifier(item.get("id"))
    content = item.get("content")
    if item_id is None or not isinstance(content, list):
        return None
    truncated: list[str] = []
    omitted: dict[str, int] = {}
    text_blocks: list[str] = []
    remaining = OBSERVER_PROJECTED_TEXT_MAX_CHARS
    bounded_content = content[:OBSERVER_PROJECTED_LIST_ITEM_LIMIT]
    for index, block in enumerate(bounded_content):
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        if remaining <= 0:
            omitted["item.content"] = len(content) - index
            break
        projected = _projected_text(
            text,
            f"item.content[{index}].text",
            truncated,
            remaining,
        )
        text_blocks.append(projected)
        remaining -= len(projected)
    if len(content) > len(bounded_content):
        omitted["item.content"] = max(
            omitted.get("item.content", 0),
            len(content) - len(bounded_content),
        )
    thread_id = optional_uuid_text(params.get("threadId"))
    turn_id = _projected_identifier(params.get("turnId"))
    if not text_blocks or not any(text_blocks) or thread_id is None or turn_id is None:
        return None
    projected_event = {
        "schema": OBSERVER_SCHEMA,
        "kind": "app_server_user_message",
        "thread_id": thread_id,
        "turn_id": turn_id,
        "item": {
            "type": "userMessage",
            "id": item_id,
            "text_blocks": text_blocks,
        },
    }
    return _attach_overflow(projected_event, truncated, omitted)


def optional_uuid_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    with suppress(ValueError):
        parsed = uuid.UUID(value)
        if str(parsed) == value:
            return value
    return None


def _projected_identifier(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > OBSERVER_PROJECTED_ID_MAX_CHARS
    ):
        return None
    return value


def _projected_text(
    value: str,
    field_name: str,
    truncated_fields: list[str],
    limit: int,
) -> str:
    if len(value) <= limit:
        return value
    truncated_fields.append(field_name)
    return value[:limit]


def _attach_overflow(
    projected: dict[str, object],
    truncated_fields: list[str],
    omitted_list_items: dict[str, int],
) -> dict[str, object]:
    if truncated_fields or omitted_list_items:
        projected["projection_overflow"] = {
            "truncated_fields": truncated_fields,
            "omitted_list_items": omitted_list_items,
        }
    return projected
