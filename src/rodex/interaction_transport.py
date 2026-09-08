"""Private session-operation transport; target policy stays in the interaction owner."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import unix_connect

from .interaction_pipeline import (
    DeliveryStatus,
    InteractionDeliveryIndeterminate,
    InteractionOperation,
    InteractionRequest,
    InteractionResult,
    SessionInteractionPipeline,
)

SESSION_INTERACTION_CONNECTION_PATH: Final = "/rodex-interaction"
SESSION_INTERACTION_METHOD: Final = "rodex/interaction"


def publish_tui_notice(
    proxy_socket_path: Path,
    message: str,
    *,
    connector: Callable[..., Any] = unix_connect,
) -> bool:
    """Ask Rodex's proxy to show one TUI-owned warning without an App Server turn."""
    return publish_session_interaction(
        proxy_socket_path,
        InteractionRequest("main", InteractionOperation.MESSAGE, "update-notice", text=message),
        connector=connector,
    ).accepted


def publish_session_interaction(
    proxy_socket_path: Path,
    operation: InteractionRequest,
    *,
    connector: Callable[..., Any] = unix_connect,
) -> InteractionResult:
    """Submit one explicit operation; a lost response is not permission to retry."""
    dispatch_id = operation.dispatch_id
    if operation.start_model_turn and dispatch_id is None:
        dispatch_id = f"rodex:dispatch:{uuid.uuid4()}"
    request_id = 0
    request = json.dumps(
        {
            "method": SESSION_INTERACTION_METHOD,
            "id": request_id,
            "params": {
                "target": operation.target,
                "operation": operation.operation.value,
                "text": operation.text,
                "start_model_turn": operation.start_model_turn,
                "open_if_missing": operation.open_if_missing,
                "size_percent": operation.size_percent,
                "dispatch_id": dispatch_id,
                "expected_binding": operation.expected_binding,
                "expected_thread_id": operation.expected_thread_id,
            },
        },
        separators=(",", ":"),
    )
    try:
        with connector(
            str(proxy_socket_path),
            uri=f"ws://localhost{SESSION_INTERACTION_CONNECTION_PATH}",
            compression=None,
            open_timeout=1,
            close_timeout=1,
            max_size=None,
        ) as connection:
            connection.send(request)
            is_main_notice = (
                operation.target == "main"
                and operation.operation == InteractionOperation.MESSAGE
                and not operation.start_model_turn
                and not operation.open_if_missing
            )
            timeout = 1 if is_main_notice else 10
            response = _interaction_json_object(connection.recv(timeout=timeout))
    except (ConnectionClosed, OSError, TimeoutError):
        return InteractionResult(
            DeliveryStatus.INDETERMINATE,
            "interaction response unavailable; do not blindly retry",
            {"dispatch_id": dispatch_id},
        )
    if response is None or response.get("id") != request_id:
        return InteractionResult(
            DeliveryStatus.INDETERMINATE, "interaction response did not match the request", {"dispatch_id": dispatch_id}
        )
    result = response.get("result")
    if not isinstance(result, dict):
        return InteractionResult(
            DeliveryStatus.INDETERMINATE, "interaction response is malformed", {"dispatch_id": dispatch_id}
        )
    try:
        return InteractionResult(DeliveryStatus(result["status"]), str(result.get("detail", "")), result.get("value"))
    except (ValueError, KeyError, TypeError):
        return InteractionResult(
            DeliveryStatus.INDETERMINATE, "interaction result status is malformed", {"dispatch_id": dispatch_id}
        )


def _interaction_json_object(message: str | bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(message)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def serve_session_interaction(connection: Any, pipeline: SessionInteractionPipeline) -> None:
    """Decode one public operation and return explicit rejection/uncertainty, not a silent drop."""
    request_id: object = None
    result = InteractionResult(DeliveryStatus.REJECTED, "invalid interaction request")
    try:
        try:
            request = _interaction_json_object(connection.recv(timeout=1))
            if request is not None:
                request_id = request.get("id")
                params = request.get("params")
                if request.get("method") == SESSION_INTERACTION_METHOD and isinstance(params, dict):
                    operation = InteractionOperation(params.get("operation"))
                    if operation in {
                        InteractionOperation.MESSAGE,
                        InteractionOperation.OPEN,
                        InteractionOperation.LOCATE,
                        InteractionOperation.FOCUS,
                        InteractionOperation.RESIZE,
                        InteractionOperation.CLOSE,
                    }:
                        result = pipeline.execute(
                            InteractionRequest(
                                target=params.get("target"),
                                operation=operation,
                                source="local-client",
                                text=params.get("text"),
                                start_model_turn=params.get("start_model_turn", False),
                                open_if_missing=params.get("open_if_missing", False),
                                size_percent=params.get("size_percent"),
                                dispatch_id=params.get("dispatch_id"),
                                expected_binding=params.get("expected_binding"),
                                expected_thread_id=params.get("expected_thread_id"),
                            )
                        )
        except InteractionDeliveryIndeterminate as error:
            result = InteractionResult(
                DeliveryStatus.INDETERMINATE,
                str(error),
                {name: getattr(error, name, None) for name in ("dispatch_id", "thread_id", "turn_id", "method")},
            )
        except (ValueError, TypeError) as error:
            result = InteractionResult(DeliveryStatus.REJECTED, str(error))
        except (RuntimeError, OSError) as error:
            result = InteractionResult(DeliveryStatus.FAILED, str(error))
        connection.send(
            json.dumps(
                {
                    "id": request_id,
                    "result": {"status": result.status.value, "detail": result.detail, "value": result.value},
                },
                separators=(",", ":"),
            )
        )
    except (ConnectionClosed, OSError, TimeoutError):
        return
    finally:
        connection.close()
