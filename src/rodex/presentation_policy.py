"""Typed protocol-event presentation policies, independent of event processing.

The Codex TUI continues to receive and process its complete protocol stream. This
pipeline retains bounded semantic projections for Rodex-owned presentation surfaces,
where a configured policy may choose what the human sees without changing execution,
logging, lifecycle state, or protocol control flow.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock
from typing import Final

from .app_server_contract import CODEX_APP_SERVER
from .interaction_pipeline import (
    DeliveryStatus,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    InteractionTarget,
    SessionInteractionPipeline,
)

PRESENTATION_POLICY_TARGET: Final = "presentation-policy"
PRESENTATION_EVENT_HISTORY_LIMIT: Final = 512
PRESENTATION_ITEM_TEXT_HISTORY_LIMIT: Final = 128
PRESENTATION_ITEM_IDENTITY_LIMIT: Final = 512
PRESENTATION_ITEM_SELECTOR_HISTORY_LIMIT: Final = 8
PRESENTATION_TEXT_LIMIT_CHARS: Final = 64 * 1024
PRESENTATION_FIELD_LIMIT_CHARS: Final = 1024
PRESENTATION_HYDRATED_TURN_LIMIT: Final = 128
PRESENTATION_HYDRATED_ITEM_LIMIT: Final = 256
PRESENTATION_REQUEST_LIMIT: Final = 256

type ProtocolRequestIdentity = tuple[str, int | str]


class PresentationSurface(StrEnum):
    """Choose the native Codex screen or a Rodex-owned semantic projection."""

    NATIVE = "native"
    SEMANTIC = "semantic"


class PresentationEventKind(StrEnum):
    """Stable structural event families available to configured selectors."""

    ITEM = "item"
    ITEM_DELTA = "item_delta"
    LIFECYCLE = "lifecycle"
    CONTROL_REQUEST = "control_request"
    ERROR = "error"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ProtocolPresentationEvent:
    """Bounded semantic identity from one App Server event, never rendered prose."""

    sequence: int
    method: str
    kind: PresentationEventKind
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    item_type: str | None = None
    phase: str | None = None
    status: str | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class PresentationEventSelector:
    """Declaratively match typed fields; omitted fields are wildcards."""

    methods: frozenset[str] | None = None
    kinds: frozenset[PresentationEventKind] | None = None
    item_types: frozenset[str] | None = None
    phases: frozenset[str] | None = None
    statuses: frozenset[str] | None = None

    def matches(self, event: ProtocolPresentationEvent) -> bool:
        return (
            (self.methods is None or event.method in self.methods)
            and (self.kinds is None or event.kind in self.kinds)
            and (self.item_types is None or event.item_type in self.item_types)
            and (self.phases is None or event.phase in self.phases)
            and (self.statuses is None or event.status in self.statuses)
        )


@dataclass(frozen=True, slots=True)
class PresentationPolicyConfig:
    """One named display policy selected by a configured input action."""

    name: str
    surface: PresentationSurface
    visible_events: tuple[PresentationEventSelector, ...] = ()
    heading: str = ""

    def __post_init__(self) -> None:
        if not self.name or any(character.isspace() for character in self.name):
            raise ValueError("presentation policy names must be non-empty single tokens")
        if self.surface == PresentationSurface.SEMANTIC and not self.visible_events:
            raise ValueError("a semantic presentation policy needs at least one event selector")
        if self.surface == PresentationSurface.SEMANTIC and not self.heading:
            raise ValueError("a semantic presentation policy needs a heading")

    def admits(self, event: ProtocolPresentationEvent) -> bool:
        return any(selector.matches(event) for selector in self.visible_events)


PRESENTATION_POLICIES: Final = (
    PresentationPolicyConfig("dark", PresentationSurface.NATIVE),
    PresentationPolicyConfig(
        "light",
        PresentationSurface.SEMANTIC,
        visible_events=(
            PresentationEventSelector(
                item_types=frozenset({"agentMessage"}),
                phases=frozenset({"commentary"}),
            ),
        ),
        heading="RODEX LIGHT  commentary only",
    ),
)


@dataclass(frozen=True, slots=True)
class PresentationItemText:
    """Latest bounded display text accumulated for one structurally typed item."""

    thread_id: str
    turn_id: str | None
    item_id: str
    item_type: str
    phase: str | None
    status: str | None
    text: str
    complete: bool


@dataclass(frozen=True, slots=True)
class PresentationSnapshot:
    revision: int
    policy_name: str
    surface: PresentationSurface
    heading: str
    events: tuple[ProtocolPresentationEvent, ...]
    items: tuple[PresentationItemText, ...]


@dataclass(slots=True)
class _AccumulatedItemText:
    thread_id: str
    turn_id: str | None
    item_id: str
    item_type: str
    phase: str | None
    status: str | None
    text: str = ""
    complete: bool = False
    selector_events: deque[ProtocolPresentationEvent] = field(
        default_factory=lambda: deque(maxlen=PRESENTATION_ITEM_SELECTOR_HISTORY_LIMIT)
    )

    def snapshot(self) -> PresentationItemText:
        return PresentationItemText(
            self.thread_id,
            self.turn_id,
            self.item_id,
            self.item_type,
            self.phase,
            self.status,
            self.text,
            self.complete,
        )

    def admitted_by(self, policy: PresentationPolicyConfig) -> bool:
        return any(policy.admits(event) for event in self.selector_events)


class SessionPresentationPipeline:
    """Project every structured event once and derive one configured display view."""

    def __init__(
        self,
        policies: tuple[PresentationPolicyConfig, ...] = PRESENTATION_POLICIES,
        *,
        initial_policy: str = "dark",
    ) -> None:
        if not policies or len({policy.name for policy in policies}) != len(policies):
            raise ValueError("presentation policies must have unique names")
        self._policies = {policy.name: policy for policy in policies}
        if initial_policy not in self._policies:
            raise ValueError(f"unknown initial presentation policy: {initial_policy}")
        self._selected_policy = initial_policy
        self._root_thread_id: str | None = None
        self._sequence = 0
        self._revision = 0
        self._events: deque[ProtocolPresentationEvent] = deque(maxlen=PRESENTATION_EVENT_HISTORY_LIMIT)
        self._item_texts: OrderedDict[tuple[str, str], _AccumulatedItemText] = OrderedDict()
        self._item_identities: OrderedDict[tuple[str, str], tuple[str, str | None]] = OrderedDict()
        self._request_methods: OrderedDict[ProtocolRequestIdentity, str] = OrderedDict()
        self._lock = RLock()

    @property
    def policy_names(self) -> frozenset[str]:
        return frozenset(self._policies)

    def bind_root_thread(self, thread_id: str) -> None:
        """Scope semantic output to the exact main conversation, never a child thread."""
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("presentation requires a root thread identity")
        with self._lock:
            if self._root_thread_id == thread_id:
                return
            self._root_thread_id = thread_id
            self._revision += 1

    def select_policy(self, policy_name: str) -> None:
        if policy_name not in self._policies:
            raise ValueError(f"unknown presentation policy: {policy_name}")
        with self._lock:
            if self._selected_policy == policy_name:
                return
            self._selected_policy = policy_name
            self._revision += 1

    def observe_protocol_input(self, request: Mapping[str, object] | None) -> None:
        """Correlate bounded primary-TUI requests with their later responses."""
        if request is None:
            return
        method = request.get("method")
        request_key = _request_key(request.get("id"))
        if not isinstance(method, str) or request_key is None:
            return
        with self._lock:
            self._request_methods[request_key] = method
            self._request_methods.move_to_end(request_key)
            if len(self._request_methods) > PRESENTATION_REQUEST_LIMIT:
                self._request_methods.popitem(last=False)

    def observe_protocol_output(self, event: Mapping[str, object] | None) -> None:
        """Retain bounded structural meaning; never decide from rendered terminal words."""
        if event is None:
            return
        with self._lock:
            if not isinstance(event.get("method"), str):
                self._observe_correlated_response(event)
                return
            projected = self._project_event(event)
            if projected is None:
                return
            self._events.append(projected)
            self._remember_item_identity(projected)
            self._accumulate_item_text(projected)
            self._revision += 1

    def reset_after_disconnect(self) -> None:
        """Discard connection-scoped partial text while retaining completed item text."""
        with self._lock:
            incomplete_keys = [key for key, item_text in self._item_texts.items() if not item_text.complete]
            for key in incomplete_keys:
                del self._item_texts[key]
            self._item_identities.clear()
            self._request_methods.clear()
            if incomplete_keys:
                self._revision += 1

    def snapshot(self) -> PresentationSnapshot:
        with self._lock:
            policy = self._policies[self._selected_policy]
            root_thread_id = self._root_thread_id
            events = tuple(
                event
                for event in self._events
                if root_thread_id is not None and event.thread_id == root_thread_id and policy.admits(event)
            )
            items = tuple(
                item_text.snapshot()
                for item_text in self._item_texts.values()
                if item_text.text
                and root_thread_id is not None
                and item_text.thread_id == root_thread_id
                and item_text.admitted_by(policy)
            )
            return PresentationSnapshot(self._revision, policy.name, policy.surface, policy.heading, events, items)

    def _project_event(self, event: Mapping[str, object]) -> ProtocolPresentationEvent | None:
        raw_method = event.get("method")
        if not isinstance(raw_method, str) or not raw_method:
            return None
        params = event.get("params")
        params = params if isinstance(params, Mapping) else {}
        item = params.get("item")
        item = item if isinstance(item, Mapping) else {}
        self._sequence += 1
        item_id = _bounded_optional_text(item.get("id")) or _bounded_optional_text(params.get("itemId"))
        item_type = _bounded_optional_text(item.get("type"))
        phase = _bounded_optional_text(item.get("phase"))
        item_key = _item_key(params, item_id)
        if item_key in self._item_identities:
            prior_item_type, prior_phase = self._item_identities[item_key]
            item_type = item_type or prior_item_type
            phase = phase or prior_phase
        return ProtocolPresentationEvent(
            sequence=self._sequence,
            method=raw_method,
            kind=_event_kind(raw_method, event),
            thread_id=_bounded_optional_text(params.get("threadId")),
            turn_id=_bounded_optional_text(params.get("turnId")),
            item_id=item_id,
            item_type=item_type,
            phase=phase,
            status=_event_status(params, item),
            text=_event_text(raw_method, params, item),
        )

    def _remember_item_identity(self, event: ProtocolPresentationEvent) -> None:
        if event.thread_id is None or event.item_id is None or event.item_type is None:
            return
        key = (event.thread_id, event.item_id)
        self._item_identities[key] = (event.item_type, event.phase)
        self._item_identities.move_to_end(key)
        if len(self._item_identities) > PRESENTATION_ITEM_IDENTITY_LIMIT:
            self._item_identities.popitem(last=False)

    def _observe_correlated_response(self, response: Mapping[str, object]) -> None:
        request_key = _request_key(response.get("id"))
        method = None if request_key is None else self._request_methods.pop(request_key, None)
        if method != CODEX_APP_SERVER.thread_read_method:
            return
        result = response.get("result")
        thread = result.get("thread") if isinstance(result, Mapping) else None
        thread_id = _bounded_optional_text(thread.get("id")) if isinstance(thread, Mapping) else None
        turns = thread.get("turns") if isinstance(thread, Mapping) else None
        if thread_id is None or not isinstance(turns, list):
            return
        changed = False
        for turn in turns[-PRESENTATION_HYDRATED_TURN_LIMIT:]:
            if not isinstance(turn, Mapping):
                continue
            turn_id = _bounded_optional_text(turn.get("id"))
            items = turn.get("items")
            if not isinstance(items, list):
                continue
            for item in items[-PRESENTATION_HYDRATED_ITEM_LIMIT:]:
                if not isinstance(item, Mapping):
                    continue
                item_id = _bounded_optional_text(item.get("id"))
                item_type = _bounded_optional_text(item.get("type"))
                item_text = _event_text("item/completed", {}, item)
                if item_id is None or item_type is None or item_text is None:
                    continue
                self._sequence += 1
                projected = ProtocolPresentationEvent(
                    sequence=self._sequence,
                    method="item/completed",
                    kind=PresentationEventKind.ITEM,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    item_id=item_id,
                    item_type=item_type,
                    phase=_bounded_optional_text(item.get("phase")),
                    status=_event_status({}, item),
                    text=item_text,
                )
                if self._completed_item_text_already_matches(projected):
                    continue
                self._events.append(projected)
                self._remember_item_identity(projected)
                self._accumulate_item_text(projected)
                changed = True
        if changed:
            self._revision += 1

    def _completed_item_text_already_matches(self, event: ProtocolPresentationEvent) -> bool:
        if event.thread_id is None or event.item_id is None:
            return False
        existing = self._item_texts.get((event.thread_id, event.item_id))
        return (
            existing is not None
            and existing.complete
            and (event.turn_id is None or existing.turn_id == event.turn_id)
            and existing.item_type == event.item_type
            and (event.phase is None or existing.phase == event.phase)
            and (event.status is None or existing.status == event.status)
            and existing.text == (event.text or "")
        )

    def _accumulate_item_text(self, event: ProtocolPresentationEvent) -> None:
        if event.item_type is None or event.thread_id is None or event.item_id is None:
            return
        key = (event.thread_id, event.item_id)
        item_text = self._item_texts.get(key)
        if item_text is None:
            if not self._admitted_by_any_semantic_policy(event):
                return
            item_text = _AccumulatedItemText(
                event.thread_id,
                event.turn_id,
                event.item_id,
                event.item_type,
                event.phase,
                event.status,
            )
            self._item_texts[key] = item_text
            if len(self._item_texts) > PRESENTATION_ITEM_TEXT_HISTORY_LIMIT:
                self._item_texts.popitem(last=False)
        elif event.phase is not None:
            item_text.phase = event.phase
        item_text.item_type = event.item_type
        if event.status is not None:
            item_text.status = event.status
        if event.turn_id is not None:
            item_text.turn_id = event.turn_id
        if event.kind == PresentationEventKind.ITEM_DELTA and event.text:
            item_text.text = (item_text.text + event.text)[:PRESENTATION_TEXT_LIMIT_CHARS]
        elif event.method == "item/completed":
            if event.text is not None:
                item_text.text = event.text
            item_text.complete = True
        if self._admitted_by_any_semantic_policy(event):
            item_text.selector_events.append(event)

    def _admitted_by_any_semantic_policy(self, event: ProtocolPresentationEvent) -> bool:
        return any(
            policy.surface == PresentationSurface.SEMANTIC and policy.admits(event) for policy in self._policies.values()
        )


class PresentationPolicyInteractionAdapter:
    """Expose policy selection through the same typed interaction pipeline as menus."""

    def __init__(
        self,
        interactions: SessionInteractionPipeline,
        presentation: SessionPresentationPipeline,
        runtime_identity: str,
    ) -> None:
        self._interactions = interactions
        self._presentation = presentation
        self._target = InteractionTarget(
            PRESENTATION_POLICY_TARGET,
            runtime_identity,
            frozenset({InteractionOperation.SELECT_PRESENTATION_POLICY}),
            exists=lambda: True,
            deliver=self._deliver,
        )
        interactions.register(self._target)

    def _deliver(self, request: InteractionRequest) -> InteractionResult:
        if not isinstance(request.payload, str):
            return InteractionResult(DeliveryStatus.REJECTED, "presentation selection requires a policy name")
        try:
            self._presentation.select_policy(request.payload)
        except ValueError as error:
            return InteractionResult(DeliveryStatus.REJECTED, str(error))
        return InteractionResult(DeliveryStatus.COMPLETED, f"presentation policy selected: {request.payload}")

    def close(self) -> None:
        self._interactions.unregister(self._target)


def _item_key(params: Mapping[str, object], item_id: str | None) -> tuple[str, str] | None:
    thread_id = _bounded_optional_text(params.get("threadId"))
    return None if thread_id is None or item_id is None else (thread_id, item_id)


def _bounded_optional_text(value: object, limit: int = PRESENTATION_FIELD_LIMIT_CHARS) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _event_text(method: str, params: Mapping[str, object], item: Mapping[str, object]) -> str | None:
    value = params.get("delta") if _is_item_delta_method(method) else item.get("text")
    return value[:PRESENTATION_TEXT_LIMIT_CHARS] if isinstance(value, str) else None


def _event_status(params: Mapping[str, object], item: Mapping[str, object]) -> str | None:
    status = item.get("status")
    if isinstance(status, str):
        return _bounded_optional_text(status)
    nested_status = params.get("status")
    if isinstance(nested_status, Mapping):
        return _bounded_optional_text(nested_status.get("type"))
    turn = params.get("turn")
    return _bounded_optional_text(turn.get("status")) if isinstance(turn, Mapping) else None


def _event_kind(method: str, event: Mapping[str, object]) -> PresentationEventKind:
    if method == "error":
        return PresentationEventKind.ERROR
    if _is_item_delta_method(method):
        return PresentationEventKind.ITEM_DELTA
    if method in {"item/started", "item/completed"}:
        return PresentationEventKind.ITEM
    if method.startswith(("turn/", "thread/")):
        return PresentationEventKind.LIFECYCLE
    if "id" in event:
        return PresentationEventKind.CONTROL_REQUEST
    return PresentationEventKind.OTHER


def _is_item_delta_method(method: str) -> bool:
    return method.startswith("item/") and (method.endswith("/delta") or method.endswith("Delta"))


def _request_key(value: object) -> ProtocolRequestIdentity | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return "integer", value
    if isinstance(value, str):
        return "string", value
    return None
