"""Exact-control machine command execution and schema-v1 envelopes."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from cool_name import CoolNameError
from rodex_registry import (
    RodexSessionError,
    lookup_rodex_runtime_instance,
    lookup_rodex_session_names,
    record_a_rodex_session_access,
)
from rodex_sql import RodexSQLError

from .app_server_contract import RodexAppServerCompatibilityError
from .command_contract import (
    DISPATCH_STATUS_COMMAND,
    INSPECT_COMMAND,
    INTERRUPT_COMMAND,
    RESULT_COMMAND,
    START_COMMAND,
    STEER_COMMAND,
    WAIT_COMMAND,
    MachineCommandSpec,
    MachineUsageError,
    parse_machine_invocation,
)
from .control import (
    CodexControlClient,
    CodexDispatchStatus,
    CodexThreadState,
    CodexTurnResult,
    LiveRodexControl,
    RodexControlError,
    RodexDispatchIndeterminateError,
    RodexWaitTimeoutError,
)
from .errors import RodexLaunchError
from .live_runtime import (
    resolve_live_control,
    revalidate_live_control,
    session_transition_lock,
)
from .runtime import RodexRuntimeError, RodexRuntimeLauncher


class _RuntimeUpgradeRequiredError(RodexLaunchError):
    """A legacy live runtime lacks Phase I's exact incarnation identity."""


def execute_machine_command(
    arguments: list[str],
    spec: MachineCommandSpec,
    database_path: Path,
    launcher: RodexRuntimeLauncher,
    control_client: CodexControlClient,
) -> int:
    """Execute the exact-control command selected by the application pipeline."""
    if not arguments or arguments[0] != spec.token:
        raise AssertionError("application pipeline selected an invalid machine command")
    command = spec.token
    operation = spec.operation
    session_name = arguments[1] if len(arguments) > 1 else None
    session_id: int | None = None
    control: LiveRodexControl | None = None
    turn_id: str | None = None
    dispatch_id: str | None = None
    display_name: str | None = None
    try:
        invocation = parse_machine_invocation(arguments, spec)
        session_name = invocation.session_name
        turn_id = invocation.turn_id
        dispatch_id = invocation.dispatch_id
        session_id, runtime, control = resolve_live_control(
            session_name, database_path, launcher
        )
        names = lookup_rodex_session_names(session_id, database_path)
        if names is None:
            raise RodexLaunchError(f"Rodex session disappeared: {session_name}")
        display_name = names.display_name

        def revalidate() -> None:
            revalidate_live_control(launcher, runtime, control)

        if command == INSPECT_COMMAND:
            state = control_client.inspect_live(control)
            revalidate()
            persisted_runtime = lookup_rodex_runtime_instance(session_id, database_path)
            runtime_matches = (
                persisted_runtime is not None
                and control.runtime_identifier == persisted_runtime.runtime_identifier
            )
            compatibility_error: str | None = None
            compatible_version: str | None = None
            try:
                compatible_version = control_client.exact_control_version(control)
                revalidate()
            except RodexAppServerCompatibilityError as error:
                compatibility_error = str(error)
            _print_machine_success(
                operation,
                display_name,
                control,
                state=state,
                turn_id=state.active_turn_id,
                data={
                    "thread": _thread_state_payload(state),
                    "runtime_identity_persisted": runtime_matches,
                    "exact_control_available": (
                        runtime_matches and compatibility_error is None
                    ),
                    "app_server": {
                        "compatible_version": compatible_version,
                        "exact_control_compatible": compatibility_error is None,
                        "compatibility_error": compatibility_error,
                    },
                },
            )
            return 0

        _require_exact_runtime_instance(session_id, database_path, control)
        if command == START_COMMAND:
            prompt = _read_machine_prompt()
            with session_transition_lock(database_path, session_id):
                dispatch = control_client.start_turn(
                    control,
                    prompt,
                    dispatch_id=dispatch_id,
                    revalidate=revalidate,
                )
            success_state = None
            success_turn_id = dispatch.turn_id
            success_thread_id = dispatch.thread_id
            success_codex_session_id = dispatch.session_id
            success_data: dict[str, object] = {
                "accepted": True,
                "dispatch": _accepted_dispatch_payload(dispatch.dispatch_id),
                "recommended_next": _turn_recommendation(
                    display_name,
                    dispatch.turn_id,
                    "inProgress",
                ),
            }
        elif command == STEER_COMMAND:
            assert isinstance(turn_id, str)
            prompt = _read_machine_prompt()
            with session_transition_lock(database_path, session_id):
                dispatch = control_client.steer_turn(
                    control,
                    turn_id,
                    prompt,
                    dispatch_id=dispatch_id,
                    revalidate=revalidate,
                )
            success_state = None
            success_turn_id = dispatch.turn_id
            success_thread_id = dispatch.thread_id
            success_codex_session_id = dispatch.session_id
            success_data = {
                "accepted": True,
                "dispatch": _accepted_dispatch_payload(dispatch.dispatch_id),
                "recommended_next": _turn_recommendation(
                    display_name,
                    dispatch.turn_id,
                    "inProgress",
                ),
            }
        elif command == DISPATCH_STATUS_COMMAND:
            assert isinstance(dispatch_id, str)
            state, dispatch_status = control_client.dispatch_status(
                control,
                dispatch_id,
                revalidate=revalidate,
            )
            success_state = state
            success_turn_id = (
                dispatch_status.matches[0].turn_id
                if dispatch_status.observation == "accepted"
                else None
            )
            success_thread_id = None
            success_codex_session_id = None
            success_data = {
                "dispatch": _dispatch_status_payload(dispatch_status),
                "recommended_next": _dispatch_status_recommendation(
                    display_name,
                    dispatch_status,
                ),
            }
        elif command == INTERRUPT_COMMAND:
            assert isinstance(turn_id, str)
            with session_transition_lock(database_path, session_id):
                state = control_client.interrupt_turn(
                    control,
                    turn_id,
                    revalidate=revalidate,
                )
            success_state = state
            success_turn_id = turn_id
            success_thread_id = None
            success_codex_session_id = None
            success_data = {"interrupt_requested": True}
        elif command == RESULT_COMMAND:
            assert isinstance(turn_id, str)
            state, result = control_client.result(control, turn_id, revalidate=revalidate)
            if result.status in {"failed", "interrupted"}:
                code = "turn_failed" if result.status == "failed" else "turn_interrupted"
                exit_code = 6 if result.status == "failed" else 5
                print_machine_error(
                    operation,
                    code,
                    f"Codex turn ended with status {result.status}",
                    retryable=False,
                    session_name=display_name,
                    control=control,
                    state=state,
                    turn_id=turn_id,
                    data={"turn": _turn_result_payload(result)},
                )
                return exit_code
            success_state = state
            success_turn_id = turn_id
            success_thread_id = None
            success_codex_session_id = None
            success_data = {"turn": _turn_result_payload(result)}
        elif command == WAIT_COMMAND:
            assert isinstance(turn_id, str)
            state, result = control_client.wait_for_turn(
                control,
                turn_id,
                timeout_seconds=invocation.timeout_seconds,
                revalidate=revalidate,
            )
            if result.status in {"failed", "interrupted"}:
                code = "turn_failed" if result.status == "failed" else "turn_interrupted"
                exit_code = 6 if result.status == "failed" else 5
                print_machine_error(
                    operation,
                    code,
                    f"Codex turn ended with status {result.status}",
                    retryable=False,
                    session_name=display_name,
                    control=control,
                    state=state,
                    turn_id=turn_id,
                    data={"turn": _turn_result_payload(result)},
                )
                return exit_code
            success_state = state
            success_turn_id = turn_id
            success_thread_id = None
            success_codex_session_id = None
            success_data = {"turn": _turn_result_payload(result)}
        else:  # pragma: no cover - the command map and branches are one contract.
            raise AssertionError(f"unhandled machine command: {command}")
        try:
            record_a_rodex_session_access(session_id, database_path)
        except (OSError, RodexSQLError, RodexSessionError, sqlite3.Error) as error:
            success_data["warnings"] = [
                {
                    "code": "access_record_failed",
                    "message": str(error),
                }
            ]
        _print_machine_success(
            operation,
            display_name,
            control,
            state=success_state,
            turn_id=success_turn_id,
            thread_id=success_thread_id,
            codex_session_id=success_codex_session_id,
            data=success_data,
        )
        return 0
    except (
        CoolNameError,
        RodexAppServerCompatibilityError,
        RodexControlError,
        RodexLaunchError,
        RodexRuntimeError,
        RodexSQLError,
        RodexSessionError,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as error:
        code, retryable, exit_code = _machine_error_classification(error)
        error_data = (
            _indeterminate_dispatch_payload(
                error,
                display_name or session_name,
            )
            if isinstance(error, RodexDispatchIndeterminateError)
            else None
        )
        print_machine_error(
            operation,
            code,
            str(error),
            retryable=retryable,
            session_name=session_name,
            control=control,
            turn_id=(
                error.turn_id
                if isinstance(error, RodexDispatchIndeterminateError)
                and error.turn_id is not None
                else turn_id
            ),
            data=error_data,
        )
        return exit_code


def _read_machine_prompt() -> str:
    prompt = sys.stdin.read()
    if not prompt.strip():
        raise MachineUsageError("stdin prompt must be non-empty")
    return prompt


def _require_exact_runtime_instance(
    session_id: int,
    database_path: Path,
    control: LiveRodexControl,
) -> None:
    persisted = lookup_rodex_runtime_instance(session_id, database_path)
    if persisted is None or control.runtime_identifier is None:
        raise _RuntimeUpgradeRequiredError(
            "live runtime predates exact runtime identity; restart it with this "
            "Rodex version"
        )
    if persisted.runtime_identifier != control.runtime_identifier:
        raise RodexLaunchError(
            "live runtime identifier does not match its durable Rodex identity"
        )


def _thread_state_payload(state: CodexThreadState) -> dict[str, object]:
    return {
        "cwd": state.cwd,
        "status": state.status,
        "active_flags": list(state.active_flags),
        "active_turn_id": state.active_turn_id,
        "can_accept_direct_input": state.can_accept_direct_input,
    }


def _turn_result_payload(result: CodexTurnResult) -> dict[str, object]:
    return {
        "turn_id": result.turn_id,
        "status": result.status,
        "final_agent_message": result.final_agent_message,
        "final_agent_message_bytes": result.final_agent_message_bytes,
        "final_agent_message_truncated": result.final_agent_message_truncated,
        "structured_output": result.structured_output,
        "error": result.error,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "duration_ms": result.duration_ms,
        "changes": {
            "paths": list(result.changed_paths),
            "truncated": result.changed_paths_truncated,
        },
    }


def _accepted_dispatch_payload(dispatch_id: str) -> dict[str, object]:
    return {
        "id": dispatch_id,
        "observation": "accepted",
        "evidence": "mutation_response",
    }


def _dispatch_status_payload(status: CodexDispatchStatus) -> dict[str, object]:
    return {
        "id": status.dispatch_id,
        "observation": status.observation,
        "evidence": "thread_history",
        "matches": [
            {
                "turn_id": match.turn_id,
                "turn_status": match.turn_status,
                "user_message_item_id": match.user_message_item_id,
            }
            for match in status.matches
        ],
    }


def _turn_recommendation(
    session_name: str,
    turn_id: str,
    turn_status: str,
) -> dict[str, object]:
    if turn_status == "inProgress":
        return {
            "action": "wait_for_turn",
            "command": [
                "rodex",
                WAIT_COMMAND,
                session_name,
                "--turn",
                turn_id,
                "--json",
            ],
            "reason": "one exact accepted turn is still in progress",
        }
    return {
        "action": "read_turn_result",
        "command": [
            "rodex",
            RESULT_COMMAND,
            session_name,
            "--turn",
            turn_id,
            "--json",
        ],
        "reason": f"one exact accepted turn is {turn_status}",
    }


def _dispatch_status_recommendation(
    session_name: str,
    status: CodexDispatchStatus,
) -> dict[str, object]:
    if status.observation == "accepted":
        match = status.matches[0]
        return _turn_recommendation(session_name, match.turn_id, match.turn_status)
    status_command = [
        "rodex",
        DISPATCH_STATUS_COMMAND,
        session_name,
        "--dispatch",
        status.dispatch_id,
        "--json",
    ]
    if status.observation == "not_observed":
        return {
            "action": "poll_dispatch_status",
            "command": status_command,
            "reason": (
                "the dispatch is not yet present in thread history; this is not "
                "evidence that it was rejected"
            ),
        }
    return {
        "action": "controller_decision_required",
        "command": None,
        "candidate_commands": [
            _turn_recommendation(
                session_name,
                match.turn_id,
                match.turn_status,
            )["command"]
            for match in status.matches
        ],
        "reason": "more than one user message carries this dispatch ID",
    }


def _indeterminate_dispatch_payload(
    error: RodexDispatchIndeterminateError,
    session_name: str | None,
) -> dict[str, object]:
    dispatch = {
        "id": error.dispatch_id,
        "observation": "indeterminate",
        "evidence": "mutation_response_unavailable",
        "method": error.method,
        "thread_id": error.thread_id,
        "turn_id": error.turn_id,
    }
    if session_name is not None and error.dispatch_id is not None:
        recommended_next: dict[str, object] = {
            "action": "query_dispatch_status",
            "command": [
                "rodex",
                DISPATCH_STATUS_COMMAND,
                session_name,
                "--dispatch",
                error.dispatch_id,
                "--json",
            ],
            "reason": "query exact thread history before the controller decides",
        }
    elif session_name is not None and error.turn_id is not None:
        recommended_next = {
            "action": "read_turn_result",
            "command": [
                "rodex",
                RESULT_COMMAND,
                session_name,
                "--turn",
                error.turn_id,
                "--json",
            ],
            "reason": "the exact mutation target is known but its response was lost",
        }
    else:
        recommended_next = {
            "action": "controller_decision_required",
            "command": None,
            "reason": "no attributable dispatch or turn identifier is available",
        }
    return {"dispatch": dispatch, "recommended_next": recommended_next}


def _machine_envelope(
    operation: str,
    ok: bool,
    *,
    session_name: str | None,
    control: LiveRodexControl | None,
    state: CodexThreadState | None = None,
    turn_id: str | None = None,
    thread_id: str | None = None,
    codex_session_id: str | None = None,
    data: dict[str, object] | None = None,
    error: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operation": operation,
        "ok": ok,
        "rodex": {
            "session_identifier": (
                None
                if control is None or control.rodex_session_id is None
                else str(control.rodex_session_id)
            ),
            "display_name": session_name,
        },
        "runtime": {
            "identifier": (
                None
                if control is None or control.runtime_identifier is None
                else str(control.runtime_identifier)
            ),
            "state": None if control is None else "running",
        },
        "codex": {
            "thread_id": (
                state.thread_id
                if state is not None
                else thread_id
                or (None if control is None else str(control.codex_session_id))
            ),
            "session_id": (state.session_id if state is not None else codex_session_id),
            "turn_id": turn_id,
        },
        "data": {} if data is None else data,
        **({} if error is None else {"error": error}),
    }


def _print_machine_success(
    operation: str,
    session_name: str,
    control: LiveRodexControl,
    *,
    state: CodexThreadState | None = None,
    turn_id: str | None = None,
    thread_id: str | None = None,
    codex_session_id: str | None = None,
    data: dict[str, object] | None = None,
) -> None:
    print(
        json.dumps(
            _machine_envelope(
                operation,
                True,
                session_name=session_name,
                control=control,
                state=state,
                turn_id=turn_id,
                thread_id=thread_id,
                codex_session_id=codex_session_id,
                data=data,
            ),
            indent=2,
            allow_nan=False,
            sort_keys=True,
        ),
        flush=True,
    )


def print_machine_error(
    operation: str,
    code: str,
    message: str,
    *,
    retryable: bool,
    session_name: str | None,
    control: LiveRodexControl | None,
    state: CodexThreadState | None = None,
    turn_id: str | None = None,
    data: dict[str, object] | None = None,
) -> None:
    print(
        json.dumps(
            _machine_envelope(
                operation,
                False,
                session_name=session_name,
                control=control,
                state=state,
                turn_id=turn_id,
                data=data,
                error={"code": code, "message": message, "retryable": retryable},
            ),
            indent=2,
            allow_nan=False,
            sort_keys=True,
        ),
        flush=True,
    )


def _machine_error_classification(error: BaseException) -> tuple[str, bool, int]:
    if isinstance(error, MachineUsageError):
        return "invalid_argument", False, 2
    if isinstance(error, _RuntimeUpgradeRequiredError):
        return "runtime_upgrade_required", False, 3
    if isinstance(error, RodexAppServerCompatibilityError):
        return "incompatible_app_server", False, 3
    if isinstance(error, RodexDispatchIndeterminateError):
        return "dispatch_indeterminate", False, 7
    if isinstance(error, RodexWaitTimeoutError):
        return "wait_timeout", True, 4
    if isinstance(error, RodexLaunchError):
        if str(error).startswith("unknown Rodex session"):
            return "unknown_session", False, 2
        if "is not running" in str(error):
            return "runtime_not_running", True, 3
        return "identity_verification_failed", False, 3
    if isinstance(error, (RodexRuntimeError, OSError)):
        return "runtime_unavailable", True, 3
    if isinstance(error, RodexControlError):
        return "control_failed", False, 7
    return "operation_failed", False, 1
