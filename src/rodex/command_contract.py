"""Authoritative Rodex command vocabulary, help, and machine grammar."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

RUNNING_COMMAND: Final = "_running"
CONTEXT_COMMAND: Final = "_context"
ALIAS_COMMAND: Final = "_alias"
SEND_COMMAND: Final = "_send"
WAIT_COMMAND: Final = "_wait"
CAT_COMMAND: Final = "_cat"
EVENTS_COMMAND: Final = "_events"
INSPECT_COMMAND: Final = "_inspect"
START_COMMAND: Final = "_start"
STEER_COMMAND: Final = "_steer"
DISPATCH_STATUS_COMMAND: Final = "_dispatch-status"
INTERRUPT_COMMAND: Final = "_interrupt"
RESULT_COMMAND: Final = "_result"
CREATE_COMMAND: Final = "_create"
DETACH_COMMAND: Final = "_detach"
HELP_COMMAND: Final = "_help"
STATS_COMMAND: Final = "_stats"
STATS_STATUS_COMMAND: Final = "_stats-status"
MOUSE_COMMAND: Final = "_mouse"
FORCE_FLAG: Final = "--force"


class CommandRoute(StrEnum):
    HELP = "help"
    LAUNCH = "launch"
    SESSION = "session"
    MACHINE = "machine"
    STATISTICS = "statistics"
    SELECTOR = "selector"
    CODEX = "codex"


@dataclass(frozen=True, slots=True)
class CommandSpec:
    token: str
    route: CommandRoute
    help_lines: tuple[str, ...]


COMMAND_SPECS: Final = (
    CommandSpec(HELP_COMMAND, CommandRoute.HELP, ("_help", "Show this help and exit.")),
    CommandSpec(
        CREATE_COMMAND,
        CommandRoute.LAUNCH,
        ("_create [NAME] [-- CODEX_ARGS...]", "Create and attach to a managed session."),
    ),
    CommandSpec(
        DETACH_COMMAND,
        CommandRoute.LAUNCH,
        (
            "_detach [SESSION|CODEX_ARGS...]",
            "Create, resume, or recover without attaching.",
        ),
    ),
    CommandSpec(
        RUNNING_COMMAND, CommandRoute.SESSION, ("_running", "List running sessions.")
    ),
    CommandSpec(
        CONTEXT_COMMAND,
        CommandRoute.SESSION,
        ("_context [--json]", "Show this pane's verified live Rodex context."),
    ),
    CommandSpec(
        ALIAS_COMMAND,
        CommandRoute.SESSION,
        ("_alias [--force] SESSION NAME", "Assign a preferred session name."),
    ),
    CommandSpec(
        SEND_COMMAND,
        CommandRoute.SESSION,
        ("_send SESSION PROMPT", "Send work to a running session."),
    ),
    CommandSpec(
        WAIT_COMMAND,
        CommandRoute.SESSION,
        (
            "_wait SESSION",
            "Wait until a running session is idle.",
            "_wait SESSION --turn ID [--timeout DURATION] --json",
            "Wait for one exact turn; never interrupt it.",
        ),
    ),
    CommandSpec(
        INSPECT_COMMAND,
        CommandRoute.MACHINE,
        ("_inspect SESSION --json", "Inspect one verified live thread."),
    ),
    CommandSpec(
        START_COMMAND,
        CommandRoute.MACHINE,
        (
            "_start SESSION [--dispatch ID] --stdin --json",
            "Start work only when the thread is idle.",
        ),
    ),
    CommandSpec(
        STEER_COMMAND,
        CommandRoute.MACHINE,
        (
            "_steer SESSION --turn ID [--dispatch ID] --stdin --json",
            "Steer one exact active turn.",
        ),
    ),
    CommandSpec(
        DISPATCH_STATUS_COMMAND,
        CommandRoute.MACHINE,
        (
            "_dispatch-status SESSION --dispatch ID --json",
            "Observe acceptance evidence for one dispatch.",
        ),
    ),
    CommandSpec(
        INTERRUPT_COMMAND,
        CommandRoute.MACHINE,
        ("_interrupt SESSION --turn ID --json", "Interrupt one exact active turn."),
    ),
    CommandSpec(
        RESULT_COMMAND,
        CommandRoute.MACHINE,
        ("_result SESSION --turn ID --json", "Read one exact turn result."),
    ),
    CommandSpec(
        CAT_COMMAND,
        CommandRoute.SESSION,
        ("_cat SESSION", "Print all retained terminal output."),
    ),
    CommandSpec(
        EVENTS_COMMAND,
        CommandRoute.SESSION,
        ("_events SESSION", "Stream filtered live protocol events as JSON lines."),
    ),
    CommandSpec(
        STATS_COMMAND,
        CommandRoute.STATISTICS,
        (
            "_stats SESSION [--turn ID] [--source CODEX_SESSION_ID] [--json]",
            "Show persistent session or exact-turn statistics.",
        ),
    ),
    CommandSpec(
        STATS_STATUS_COMMAND,
        CommandRoute.STATISTICS,
        ("_stats-status SESSION", "Show analytics freshness and health."),
    ),
    CommandSpec(
        MOUSE_COMMAND,
        CommandRoute.SESSION,
        ("_mouse SESSION [MODE]", "Show or set mouse: on, off, toggle, inherit."),
    ),
)

COMMANDS_BY_TOKEN: Final = {spec.token: spec for spec in COMMAND_SPECS}
RODEX_COMMANDS: Final = frozenset(COMMANDS_BY_TOKEN)


def _help_text() -> str:
    lines = [
        "usage: rodex [COMMAND [ARGUMENTS]]",
        "",
        "Rodex commands:",
        "  (no command)                       Create and attach to a managed session.",
    ]
    for spec in COMMAND_SPECS:
        for usage, description in zip(
            spec.help_lines[::2], spec.help_lines[1::2], strict=True
        ):
            lines.append(f"  {usage:<36} {description}")
    lines.extend(
        (
            "",
            "Use a Rodex session name or its linked Codex UUID as the sole argument "
            "to attach, resume, or recover it.",
            "Every other invocation is passed unchanged to Codex.",
            "",
        )
    )
    return "\n".join(lines)


HELP_TEXT: Final = _help_text()


class MachineUsageError(ValueError):
    """One exact machine command violated its declared grammar."""


@dataclass(frozen=True, slots=True)
class MachineCommandSpec:
    token: str
    operation: str
    usage: str
    needs_turn: bool = False
    needs_stdin: bool = False
    allows_dispatch: bool = False
    needs_dispatch: bool = False
    allows_timeout: bool = False


MACHINE_COMMAND_SPECS: Final = {
    spec.token: spec
    for spec in (
        MachineCommandSpec(
            INSPECT_COMMAND, "thread.inspect", "rodex _inspect SESSION --json"
        ),
        MachineCommandSpec(
            START_COMMAND,
            "turn.start",
            "rodex _start SESSION [--dispatch ID] --stdin --json",
            needs_stdin=True,
            allows_dispatch=True,
        ),
        MachineCommandSpec(
            STEER_COMMAND,
            "turn.steer",
            "rodex _steer SESSION --turn TURN_ID [--dispatch ID] --stdin --json",
            needs_turn=True,
            needs_stdin=True,
            allows_dispatch=True,
        ),
        MachineCommandSpec(
            DISPATCH_STATUS_COMMAND,
            "dispatch.status",
            "rodex _dispatch-status SESSION --dispatch DISPATCH_ID --json",
            allows_dispatch=True,
            needs_dispatch=True,
        ),
        MachineCommandSpec(
            WAIT_COMMAND,
            "turn.wait",
            "rodex _wait SESSION --turn TURN_ID [--timeout DURATION] --json",
            needs_turn=True,
            allows_timeout=True,
        ),
        MachineCommandSpec(
            INTERRUPT_COMMAND,
            "turn.interrupt",
            "rodex _interrupt SESSION --turn TURN_ID --json",
            needs_turn=True,
        ),
        MachineCommandSpec(
            RESULT_COMMAND,
            "turn.result",
            "rodex _result SESSION --turn TURN_ID --json",
            needs_turn=True,
        ),
    )
}


@dataclass(frozen=True, slots=True)
class MachineInvocation:
    spec: MachineCommandSpec
    session_name: str
    turn_id: str | None
    dispatch_id: str | None
    timeout_seconds: float | None


def machine_spec_for_arguments(arguments: list[str]) -> MachineCommandSpec | None:
    if not arguments:
        return None
    spec = MACHINE_COMMAND_SPECS.get(arguments[0])
    if spec is None:
        return None
    if (
        spec.token == WAIT_COMMAND
        and "--turn" not in arguments
        and "--json" not in arguments
    ):
        return None
    return spec


def parse_machine_invocation(arguments: list[str]) -> MachineInvocation:
    spec = machine_spec_for_arguments(arguments)
    if spec is None:
        raise MachineUsageError("arguments do not select an exact machine command")
    if len(arguments) < 2 or not arguments[1].strip() or arguments[1].startswith("-"):
        raise MachineUsageError(f"usage: {spec.usage}")
    seen_json = False
    seen_stdin = False
    turn_id: str | None = None
    dispatch_id: str | None = None
    timeout_seconds: float | None = None
    index = 2
    while index < len(arguments):
        option = arguments[index]
        if option == "--json" and not seen_json:
            seen_json = True
            index += 1
        elif option == "--stdin" and not seen_stdin:
            seen_stdin = True
            index += 1
        elif option == "--turn" and turn_id is None and index + 1 < len(arguments):
            turn_id = arguments[index + 1]
            index += 2
        elif option == "--dispatch" and dispatch_id is None and index + 1 < len(arguments):
            dispatch_id = arguments[index + 1]
            index += 2
        elif (
            option == "--timeout" and timeout_seconds is None and index + 1 < len(arguments)
        ):
            timeout_seconds = _parse_timeout_duration(arguments[index + 1])
            index += 2
        else:
            raise MachineUsageError(f"usage: {spec.usage}")
    if turn_id is not None and not turn_id.strip():
        raise MachineUsageError("turn ID must be non-empty")
    if dispatch_id is not None and not dispatch_id.strip():
        raise MachineUsageError("dispatch ID must be non-empty")
    if (
        not seen_json
        or seen_stdin != spec.needs_stdin
        or (turn_id is not None) != spec.needs_turn
        or (dispatch_id is not None and not spec.allows_dispatch)
        or (spec.needs_dispatch and dispatch_id is None)
        or (timeout_seconds is not None and not spec.allows_timeout)
    ):
        raise MachineUsageError(f"usage: {spec.usage}")
    return MachineInvocation(
        spec=spec,
        session_name=arguments[1],
        turn_id=turn_id,
        dispatch_id=dispatch_id,
        timeout_seconds=timeout_seconds,
    )


def _parse_timeout_duration(value: str) -> float:
    if not isinstance(value, str) or not value:
        raise MachineUsageError("timeout must be a positive duration such as 30s or 5m")
    multipliers = {"s": 1.0, "m": 60.0, "h": 3600.0}
    suffix = value[-1]
    multiplier = multipliers.get(suffix, 1.0)
    number = value[:-1] if suffix in multipliers else value
    try:
        seconds = float(number) * multiplier
    except ValueError as error:
        raise MachineUsageError(
            "timeout must be a positive duration such as 30s or 5m"
        ) from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise MachineUsageError("timeout must be a positive duration such as 30s or 5m")
    return seconds
