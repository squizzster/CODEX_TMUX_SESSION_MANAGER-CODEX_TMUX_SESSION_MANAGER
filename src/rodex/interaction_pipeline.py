"""One target-addressed contract for messages, pane operations, and live protocol traffic.

Adapters own mechanisms and exact runtime fences. This owner resolves destinations,
orders hooks, preserves caller intent, and records what delivery actually established.
No hook runs under the registry lock; no failed delivery is automatically retried.
"""

from __future__ import annotations

import json
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import RLock


class InteractionOperation(StrEnum):
    MESSAGE = "message"
    DISPLAY_STATE = "display_state"
    PROTOCOL_INPUT = "protocol_input"
    PROTOCOL_OUTPUT = "protocol_output"
    TERMINAL_INPUT = "terminal_input"
    TERMINAL_OUTPUT = "terminal_output"
    INTERACTIVE_INPUT = "interactive_input"
    SUBMITTED_COMMAND = "submitted_command"
    INPUT_CONFIGURATION_ERROR = "input_configuration_error"
    INPUT_RELEASE = "input_release"
    OPEN = "open"
    LOCATE = "locate"
    FOCUS = "focus"
    RESIZE = "resize"
    CLOSE = "close"
    STEER = "steer"
    INTERRUPT = "interrupt"


class DeliveryStatus(StrEnum):
    COMPLETED = "completed"
    DELIVERED = "delivered"
    QUEUED = "queued"
    MODEL_TURN_STARTED = "model_turn_started"
    REJECTED = "rejected"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


class InteractionRejected(ValueError):
    """An operation cannot safely reach its requested destination."""


class InteractionDeliveryIndeterminate(RuntimeError):
    """Delivery may have happened; a caller must reconcile before retrying."""


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    target: str
    operation: InteractionOperation
    source: str
    text: str | None = None
    start_model_turn: bool = False
    # Immutable wire/state serialization prevents hooks mutating an earlier request.
    payload: str | bytes | None = None
    open_if_missing: bool = False
    size_percent: int | None = None
    dispatch_id: str | None = None
    expected_turn_id: str | None = None
    expected_thread_id: str | None = None
    expected_binding: str | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class InteractionResult:
    status: DeliveryStatus
    detail: str = ""
    value: object = None

    @property
    def accepted(self) -> bool:
        return self.status not in {DeliveryStatus.REJECTED, DeliveryStatus.FAILED, DeliveryStatus.INDETERMINATE}


@dataclass(frozen=True, slots=True)
class InteractionRecord:
    """Bounded, content-free outcome metadata, not a second conversation log."""

    request_id: str
    target: str
    source: str
    operation: InteractionOperation
    start_model_turn: bool
    status: DeliveryStatus


@dataclass(frozen=True, slots=True)
class InteractionTarget:
    name: str
    runtime_identity: str
    operations: frozenset[InteractionOperation]
    exists: Callable[[], bool]
    deliver: Callable[[InteractionRequest], InteractionResult]
    model_thread_id: Callable[[], str | None] = lambda: None
    open: Callable[[], InteractionResult] | None = None
    binding_identity: Callable[[], str | None] = lambda: None


MessageHook = Callable[[InteractionRequest], InteractionRequest]
OutcomeObserver = Callable[[InteractionRecord], None]


class SessionInteractionPipeline:
    """Resolve → validate → transform → revalidate → deliver → record, on every route."""

    def __init__(
        self,
        *,
        hooks: tuple[MessageHook, ...] = (),
        outcome_observers: tuple[OutcomeObserver, ...] = (),
    ) -> None:
        self._hooks = tuple(hooks)
        self._outcome_observers = tuple(outcome_observers)
        self._targets: dict[str, InteractionTarget] = {}
        self._registry_lock = RLock()
        self._records: deque[InteractionRecord] = deque(maxlen=256)

    def register(self, target: InteractionTarget) -> None:
        if not target.name or not target.runtime_identity:
            raise ValueError("interaction target needs a name and exact runtime identity")
        with self._registry_lock:
            if target.name in self._targets:
                raise ValueError(f"interaction target already registered: {target.name}")
            self._targets[target.name] = target

    def unregister(self, target: InteractionTarget) -> None:
        """Remove only this binding, never a replacement with the same display name."""
        with self._registry_lock:
            if self._targets.get(target.name) is target:
                del self._targets[target.name]

    @property
    def records(self) -> tuple[InteractionRecord, ...]:
        with self._registry_lock:
            return tuple(self._records)

    def send_message(
        self,
        *,
        target: str,
        text: str,
        start_model_turn: bool = False,
        source: str = "rodex",
        open_if_missing: bool = False,
        dispatch_id: str | None = None,
    ) -> InteractionResult:
        return self.execute(
            InteractionRequest(
                target,
                InteractionOperation.MESSAGE,
                source,
                text=text,
                start_model_turn=start_model_turn,
                open_if_missing=open_if_missing,
                dispatch_id=dispatch_id,
            )
        )

    def execute(self, request: InteractionRequest) -> InteractionResult:
        try:
            result = self._execute(request)
        except InteractionRejected as error:
            result = InteractionResult(DeliveryStatus.REJECTED, str(error))
        except InteractionDeliveryIndeterminate:
            self._record(request, DeliveryStatus.INDETERMINATE)
            raise
        except Exception:
            # Preserve indeterminate model-dispatch exceptions and their exact IDs.
            # Recording a failed attempt must never encourage automatic resubmission.
            self._record(request, DeliveryStatus.FAILED)
            raise
        self._record(request, result.status)
        return result

    def _execute(self, request: InteractionRequest) -> InteractionResult:
        self._validate_request(request)
        with self._registry_lock:
            target = self._targets.get(request.target)
        if target is None:
            raise InteractionRejected(f"unknown interaction target: {request.target}")
        if request.operation not in target.operations:
            raise InteractionRejected(f"{request.target} does not support {request.operation}")
        # OPEN/LOCATE own their lookup. Repeating expensive pane probes before a
        # read creates subprocess amplification on every streamed agent delta.
        lookup_operation = request.operation in {InteractionOperation.OPEN, InteractionOperation.LOCATE}
        exists = True if lookup_operation else target.exists()
        binding = target.binding_identity()
        if request.expected_binding is not None and request.expected_binding != binding:
            raise InteractionRejected("requested interaction binding is stale")
        request = replace(request, expected_binding=binding)
        model_operation = request.start_model_turn or request.operation in {
            InteractionOperation.STEER,
            InteractionOperation.INTERRUPT,
        }
        bound_thread_id = target.model_thread_id() if model_operation else None
        if model_operation and bound_thread_id is None:
            raise InteractionRejected(f"{request.target} has no unambiguous model thread binding")
        if request.expected_thread_id is not None and request.expected_thread_id != bound_thread_id:
            raise InteractionRejected("requested model thread binding is stale")
        request = replace(request, expected_thread_id=bound_thread_id)
        if not exists and not (request.open_if_missing or request.operation == InteractionOperation.OPEN):
            raise InteractionRejected(f"interaction target is not available: {request.target}")
        transformed = request
        for hook in self._hooks:
            transformed = hook(transformed)
            self._validate_transform(request, transformed)
            self._validate_request(transformed)
        with self._registry_lock:
            if self._targets.get(request.target) is not target:
                raise InteractionRejected("interaction target changed while processing the operation")
        if model_operation and target.model_thread_id() != bound_thread_id:
            raise InteractionRejected("model thread binding changed while processing the message")
        still_exists = exists if not self._hooks or lookup_operation else target.exists()
        if not still_exists and not lookup_operation:
            if exists:
                raise InteractionRejected("existing interaction target disappeared while processing the operation")
            if not request.open_if_missing or target.open is None:
                raise InteractionRejected(f"interaction target is not available: {request.target}")
            opened = target.open()
            if not opened.accepted:
                return opened
            if not target.exists():
                raise InteractionRejected("opened interaction target is no longer available")
            if not exists and binding is None:
                binding = target.binding_identity()
                transformed = replace(transformed, expected_binding=binding)
        with self._registry_lock:
            if self._targets.get(request.target) is not target:
                raise InteractionRejected("interaction target changed before delivery")
        if target.binding_identity() != binding:
            raise InteractionRejected("interaction destination changed before delivery")
        if model_operation and target.model_thread_id() != bound_thread_id:
            raise InteractionRejected("model thread binding changed before delivery")
        return target.deliver(transformed)

    @staticmethod
    def _validate_request(request: InteractionRequest) -> None:
        if not isinstance(request, InteractionRequest) or not isinstance(request.operation, InteractionOperation):
            raise InteractionRejected("interaction requires a typed request and operation")
        if (
            not isinstance(request.target, str)
            or not request.target
            or not isinstance(request.source, str)
            or not request.source
        ):
            raise InteractionRejected("interaction requires an explicit target and source")
        if type(request.start_model_turn) is not bool or type(request.open_if_missing) is not bool:
            raise InteractionRejected("message intent and missing-target policy must be booleans")
        if request.operation == InteractionOperation.MESSAGE:
            if (
                not isinstance(request.text, str)
                or not request.text
                or (request.start_model_turn and not request.text.strip())
            ):
                raise InteractionRejected("message text must be non-empty")
        elif request.start_model_turn:
            raise InteractionRejected("only an explicit message can request a model turn")
        if request.operation in {InteractionOperation.STEER, InteractionOperation.INTERRUPT} and (
            not isinstance(request.expected_turn_id, str) or not request.expected_turn_id.strip()
        ):
            raise InteractionRejected("exact turn operation requires a turn ID")
        if request.operation == InteractionOperation.STEER and (
            not isinstance(request.text, str) or not request.text.strip()
        ):
            raise InteractionRejected("steering text must be non-empty")
        if request.operation == InteractionOperation.RESIZE and (
            type(request.size_percent) is not int or not 1 <= request.size_percent <= 99
        ):
            raise InteractionRejected("pane size must be an integer percentage from 1 to 99")
        if request.operation in {
            InteractionOperation.TERMINAL_INPUT,
            InteractionOperation.TERMINAL_OUTPUT,
        } and not isinstance(request.payload, bytes):
            raise InteractionRejected("terminal operations require a byte-stream payload")
        if request.operation in {
            InteractionOperation.INTERACTIVE_INPUT,
            InteractionOperation.SUBMITTED_COMMAND,
            InteractionOperation.INPUT_CONFIGURATION_ERROR,
            InteractionOperation.INPUT_RELEASE,
        } and not isinstance(request.text, str):
            raise InteractionRejected("interceptor operations require a text draft")

    @staticmethod
    def _validate_transform(original: InteractionRequest, transformed: InteractionRequest) -> None:
        if not isinstance(transformed, InteractionRequest):
            raise InteractionRejected("interaction hook must return an InteractionRequest")
        if replace(transformed, text=original.text, payload=original.payload) != original:
            raise InteractionRejected("interaction hooks may transform content, not target, authority, or intent")
        if original.payload != transformed.payload:
            if original.operation in {InteractionOperation.TERMINAL_INPUT, InteractionOperation.TERMINAL_OUTPUT}:
                if not isinstance(transformed.payload, bytes):
                    raise InteractionRejected("terminal hooks must preserve a byte-stream payload")
            elif original.operation in {InteractionOperation.PROTOCOL_INPUT, InteractionOperation.PROTOCOL_OUTPUT}:
                # Only text leaves may change: RPC IDs, methods, thread/turn identity,
                # approval decisions, control fields and message structure stay exact.
                if _protocol_structure(original.payload) != _protocol_structure(transformed.payload):
                    raise InteractionRejected("protocol hooks may change text, not RPC identity or control fields")
            else:
                raise InteractionRejected("structured state and pane launch payloads are owned by their domain reducer")

    def _record(self, request: InteractionRequest, status: DeliveryStatus) -> None:
        record = InteractionRecord(
            request.request_id,
            request.target,
            request.source,
            request.operation,
            request.start_model_turn,
            status,
        )
        with self._registry_lock:
            self._records.append(record)
        for observer in self._outcome_observers:
            try:
                observer(record)
            except Exception:
                # Post-delivery diagnostics cannot turn accepted work into a retry.
                continue


def _protocol_structure(payload: str | bytes | None) -> object:
    try:
        value = json.loads(payload) if payload is not None else None
    except (ValueError, UnicodeDecodeError):
        return payload

    def without_text(item: object) -> object:
        if isinstance(item, dict):
            return {
                key: "<text>" if key in {"text", "delta", "message"} and isinstance(child, str) else without_text(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [without_text(child) for child in item]
        return item

    return without_text(value)
