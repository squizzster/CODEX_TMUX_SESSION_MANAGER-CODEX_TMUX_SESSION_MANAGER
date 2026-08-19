"""Persistent statistics command parsing and presentation."""

from __future__ import annotations

import json
from pathlib import Path

from rodex_registry import (
    CodexSessionId,
    RodexSessionTurnStatisticsAmbiguousError,
    lookup_owned_rodex_sessions_id_from_a_cool_name,
    parse_codex_session_id,
    read_rodex_session_statistics,
    read_rodex_session_turn_statistics,
    session_statistics_as_dict,
    turn_statistics_as_dict,
)

from .command_contract import STATS_COMMAND, STATS_STATUS_COMMAND
from .errors import RodexLaunchError


def execute_statistics_command(arguments: list[str], database_path: Path) -> None:
    """Execute the statistics route selected by the application pipeline."""
    if not arguments or arguments[0] not in {STATS_COMMAND, STATS_STATUS_COMMAND}:
        raise AssertionError("application pipeline selected an invalid statistics command")
    command = arguments[0]
    if command == STATS_STATUS_COMMAND:
        if len(arguments) != 2:
            raise RodexLaunchError("usage: rodex _stats-status SESSION_NAME")
        session_name = arguments[1]
        as_json = False
        turn_id = None
        source_codex_session_id = None
    else:
        if len(arguments) < 2:
            raise RodexLaunchError(
                "usage: rodex _stats SESSION_NAME "
                "[--turn TURN_ID] [--source CODEX_SESSION_ID] [--json]"
            )
        session_name = arguments[1]
        as_json = False
        turn_id: str | None = None
        source_codex_session_id: CodexSessionId | None = None
        index = 2
        while index < len(arguments):
            option = arguments[index]
            if option == "--json" and not as_json:
                as_json = True
                index += 1
            elif option == "--turn" and turn_id is None and index + 1 < len(arguments):
                turn_id = arguments[index + 1]
                index += 2
            elif (
                option == "--source"
                and source_codex_session_id is None
                and index + 1 < len(arguments)
            ):
                try:
                    source_codex_session_id = parse_codex_session_id(arguments[index + 1])
                except ValueError as error:
                    raise RodexLaunchError(
                        "--source requires a valid Codex session ID"
                    ) from error
                index += 2
            else:
                raise RodexLaunchError(
                    "usage: rodex _stats SESSION_NAME "
                    "[--turn TURN_ID] [--source CODEX_SESSION_ID] [--json]"
                )
        if source_codex_session_id is not None and turn_id is None:
            raise RodexLaunchError("--source requires --turn")
    session_id = lookup_owned_rodex_sessions_id_from_a_cool_name(
        session_name, database_path
    )
    if session_id is None:
        raise RodexLaunchError(f"unknown Rodex session: {session_name}")
    try:
        view = (
            read_rodex_session_statistics(session_id, database_path)
            if turn_id is None
            else read_rodex_session_turn_statistics(
                session_id,
                turn_id,
                database_path,
                codex_session_id=source_codex_session_id,
            )
        )
    except RodexSessionTurnStatisticsAmbiguousError as error:
        raise RodexLaunchError(str(error)) from error
    snapshot = view.statistics
    worker = view.worker
    payload = {
        "rodex_session_name": session_name,
        "statistics_publication_sequence": (
            None if snapshot is None else snapshot.statistics_publication_sequence
        ),
        "statistics_projection_schema_version": (
            None if snapshot is None else snapshot.statistics_projection_schema_version
        ),
        "calculated_at_utc": None if snapshot is None else snapshot.calculated_at_utc,
        "coverage_state": None if snapshot is None else snapshot.coverage_state,
        "worker_state": "not_started" if worker is None else worker.worker_state,
        "diagnostic_code": None if worker is None else worker.diagnostic_code,
        "last_attempted_at_utc": (None if worker is None else worker.last_attempted_at_utc),
        "consecutive_failures": (0 if worker is None else worker.consecutive_failures),
        "next_retry_at_utc": None if worker is None else worker.next_retry_at_utc,
        "registered_source_count": len(view.sources),
        "included_source_count": (
            0
            if snapshot is None
            else sum(
                source.included_statistics_publication_sequence
                == snapshot.statistics_publication_sequence
                for source in view.sources
            )
        ),
    }
    if command == STATS_STATUS_COMMAND:
        print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
        return
    if snapshot is None:
        raise RodexLaunchError(
            f"Rodex session has no analytics snapshot yet: {session_name}"
        )
    if turn_id is None:
        payload["statistics"] = session_statistics_as_dict(snapshot.projection)
    else:
        turn = view.turn
        if turn is None:
            raise RodexLaunchError(
                f"turn is not present in the latest statistics snapshot: {turn_id}"
            )
        payload["turn"] = {
            "codex_session_id": str(turn.codex_session_id),
            "turn_id": turn.codex_turn_id,
            "started_at_utc": turn.started_at_utc,
            "terminal_at_utc": turn.terminal_at_utc,
            "outcome": turn.outcome,
            "included_statistics_publication_sequence": (
                turn.included_statistics_publication_sequence
            ),
        }
        payload["statistics"] = turn_statistics_as_dict(turn.projection)
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
    else:
        _print_human_statistics(payload)
    return


def _print_human_statistics(payload: dict[str, object]) -> None:
    statistics = payload["statistics"]
    if not isinstance(statistics, dict):
        raise RodexLaunchError("stored analytics snapshot is invalid")
    turn = payload.get("turn")
    subject = ""
    if isinstance(turn, dict):
        subject = f" turn {turn.get('turn_id')}"
    print(
        f"Rodex {payload['rodex_session_name']}{subject} statistics "
        f"(publication sequence {payload['statistics_publication_sequence']}, "
        f"{payload['worker_state']})",
        flush=True,
    )
    for category in ("must_have_basic_stats", "recommended_insight_stats"):
        values = statistics.get(category)
        if isinstance(values, dict):
            title = category.replace("_", " ").title()
            print(f"{title}:", flush=True)
            for name, value in values.items():
                print(f"  {name}: {json.dumps(value, sort_keys=True)}", flush=True)
