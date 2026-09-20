"""Own submitted text edits and derived UI coordinates; hooks never own RPC fields."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any

from .app_server_contract import CODEX_APP_SERVER


class UserPromptErrorNotice:
    """One configuration version owns one successfully delivered user notice."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._delivered = False

    def notify(self, send: Callable[[], bool]) -> bool:
        with self._lock:
            if not self._delivered:
                self._delivered = send()
            return self._delivered


class UserPromptHookError(ValueError):
    """Refuse this submission, report the error, and keep its connection usable."""

    def __init__(
        self,
        message: str,
        *,
        notice: UserPromptErrorNotice | None = None,
        errors: tuple[UserPromptHookError, ...] = (),
    ) -> None:
        super().__init__(message)
        self.notice = notice if notice is not None else UserPromptErrorNotice()
        self.errors = errors


@dataclass(frozen=True, slots=True)
class PromptTextEdit:
    """One exact edit in character coordinates of the text preceding this edit."""

    start: int
    end: int
    replacement: str


# One call per submission, including multipart input. The hook receives text only
# and returns ordered edits; this domain owner alone applies them to the RPC.
InputTextHook = Callable[[tuple[str, ...]], tuple[tuple[PromptTextEdit, ...], ...]]


def is_user_input_submission(frame: object) -> bool:
    """Recognize submitted input, including image-only messages, but not tool output."""
    if not isinstance(frame, dict):
        return False
    method = frame.get("method")
    if not isinstance(method, str) or method not in {
        CODEX_APP_SERVER.turn_start_method,
        CODEX_APP_SERVER.turn_steer_method,
        CODEX_APP_SERVER.thread_queue_add_method,
        CODEX_APP_SERVER.thread_queue_update_method,
    }:
        return False
    params = frame.get("params")
    return isinstance(params, dict) and isinstance(params.get("input"), list) and bool(params["input"])


def user_input_text_items(frame: object) -> tuple[dict[str, Any], ...]:
    """Select submitted text, never settings, approvals, tools, or output."""
    if not is_user_input_submission(frame):
        return ()
    assert isinstance(frame, dict)
    return tuple(
        item
        for item in frame["params"]["input"]
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    )


def _rebase_elements(elements: list, start: int, end: int, replacement_bytes: int, text_bytes: int) -> list:
    """Keep original occurrences outside the exact edit; remove overlapping spans."""
    rebased = []
    shift = replacement_bytes - (end - start)
    for element in elements:
        if not isinstance(element, dict) or not isinstance(element.get("byteRange"), dict):
            continue
        byte_range = element["byteRange"]
        left, right = byte_range.get("start"), byte_range.get("end")
        if type(left) is not int or type(right) is not int or not 0 <= left <= right <= text_bytes:
            continue
        if end <= left:
            rebased.append(element | {"byteRange": byte_range | {"start": left + shift, "end": right + shift}})
        elif start >= right:
            rebased.append(element)
    return rebased


def apply_user_prompt_hook(payload: str | bytes | None, hook: InputTextHook) -> str | bytes | None:
    """Prepare one rule snapshot, apply exact edits, and retain every other RPC field."""
    if not isinstance(payload, (str, bytes)):
        return payload
    try:
        frame = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return payload
    if not is_user_input_submission(frame):
        return payload
    items = user_input_text_items(frame)
    edits_by_item = hook(tuple(item["text"] for item in items))
    if len(edits_by_item) != len(items):
        raise UserPromptHookError("user prompt hook returned the wrong number of text items")
    changed = False
    for item, edits in zip(items, edits_by_item, strict=True):
        original_text = text = item["text"]
        elements = item.get("text_elements")
        for edit in edits:
            if (
                not isinstance(edit, PromptTextEdit)
                or type(edit.start) is not int
                or type(edit.end) is not int
                or not 0 <= edit.start <= edit.end <= len(text)
                or not isinstance(edit.replacement, str)
            ):
                raise UserPromptHookError("user prompt hook returned an invalid text edit")
            if text[edit.start : edit.end] == edit.replacement:
                continue
            if isinstance(elements, list) and elements:
                try:
                    elements = _rebase_elements(
                        elements,
                        len(text[: edit.start].encode("utf-8")),
                        len(text[: edit.end].encode("utf-8")),
                        len(edit.replacement.encode("utf-8")),
                        len(text.encode("utf-8")),
                    )
                except UnicodeError as error:
                    raise UserPromptHookError("user prompt hook requires valid UTF-8 text") from error
            text = text[: edit.start] + edit.replacement + text[edit.end :]
        if text != original_text:
            item["text"] = text
            if isinstance(elements, list):
                item["text_elements"] = elements
            changed = True
    if not changed:
        return payload
    serialized = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    try:
        encoded = serialized.encode("utf-8")
        return encoded if isinstance(payload, bytes) else serialized
    except UnicodeError as error:
        raise UserPromptHookError("user prompt hook requires valid UTF-8 text") from error
