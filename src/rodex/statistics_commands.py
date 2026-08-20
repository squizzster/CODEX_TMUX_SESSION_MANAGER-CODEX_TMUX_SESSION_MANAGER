"""Persistent statistics command parsing and presentation."""

from __future__ import annotations

import json
from pathlib import Path

from rodex_registry import (
    CodexThreadId,
    RodexSessionStatisticsConflictError,
    RodexSessionStatisticsSourceSummary,
    RodexSessionTurnStatisticsAmbiguousError,
    lookup_owned_rodex_sessions_id_from_a_cool_name,
    parse_codex_thread_id,
    read_rodex_session_statistics,
    read_rodex_session_statistics_source_summaries,
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
        source_codex_thread_id = None
    else:
        if len(arguments) < 2:
            raise RodexLaunchError(
                "usage: rodex _stats SESSION_NAME "
                "[--turn TURN_ID] [--thread CODEX_THREAD_ID] [--json]"
            )
        session_name = arguments[1]
        as_json = False
        turn_id: str | None = None
        source_codex_thread_id: CodexThreadId | None = None
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
                option == "--thread"
                and source_codex_thread_id is None
                and index + 1 < len(arguments)
            ):
                try:
                    source_codex_thread_id = parse_codex_thread_id(arguments[index + 1])
                except ValueError as error:
                    raise RodexLaunchError(
                        "--thread requires a valid Codex thread ID"
                    ) from error
                index += 2
            else:
                raise RodexLaunchError(
                    "usage: rodex _stats SESSION_NAME "
                    "[--turn TURN_ID] [--thread CODEX_THREAD_ID] [--json]"
                )
        if source_codex_thread_id is not None and turn_id is None:
            raise RodexLaunchError("--thread requires --turn")
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
                codex_thread_id=source_codex_thread_id,
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
        "registered_thread_count": len(view.sources),
        "included_thread_count": (
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
        try:
            summaries = read_rodex_session_statistics_source_summaries(
                session_id,
                database_path,
                expected_statistics_publication_sequence=(
                    snapshot.statistics_publication_sequence
                ),
            )
        except RodexSessionStatisticsConflictError as error:
            raise RodexLaunchError(
                "statistics changed during the read; retry the command"
            ) from error
        payload["threads"] = [_source_summary_as_dict(item) for item in summaries]
    else:
        turn = view.turn
        if turn is None:
            raise RodexLaunchError(
                f"turn is not present in the latest statistics snapshot: {turn_id}"
            )
        payload["turn"] = {
            "rodex_sessions_statistics_sources_id": (
                turn.rodex_sessions_statistics_sources_id
            ),
            "codex_thread_id": str(turn.codex_thread_id),
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


def _source_summary_as_dict(
    summary: RodexSessionStatisticsSourceSummary,
) -> dict[str, object]:
    source = summary.source
    count_maps: dict[str, dict[str, int]] = {}
    for count in summary.named_counts:
        count_maps.setdefault(count.count_kind, {})[count.count_name] = (
            count.occurrence_count
        )
    return {
        "rodex_sessions_statistics_sources_id": source.id,
        "codex_thread_id": str(source.codex_thread_id),
        "source_kind": source.source_kind,
        "parent_rodex_sessions_statistics_sources_id": (
            source.parent_rodex_sessions_statistics_sources_id
        ),
        "thread_depth": source.thread_depth,
        "agent_path": source.agent_path,
        "agent_nickname": source.agent_nickname,
        "subagent_history_start_ordinal": source.subagent_history_start_ordinal,
        "spawning_codex_turn_id": source.spawning_codex_turn_id,
        "first_linked_at_utc": source.first_linked_at_utc,
        "lifecycle": {
            "turns_started": summary.turns_started_count,
            "turns_completed": summary.turns_completed_count,
            "turns_aborted": summary.turns_aborted_count,
            "turns_open": summary.turns_open_count,
            "first_turn_started_at_utc": summary.first_turn_started_at_utc,
            "last_turn_terminal_at_utc": summary.last_turn_terminal_at_utc,
        },
        "token_usage": {
            "input_tokens": summary.input_tokens,
            "cached_input_tokens": summary.cached_input_tokens,
            "cache_write_input_tokens": summary.cache_write_input_tokens,
            "output_tokens": summary.output_tokens,
            "reasoning_output_tokens": summary.reasoning_output_tokens,
            "total_tokens": summary.total_tokens,
        },
        "activity": {
            "commands_executed": summary.commands_executed_count,
            "model_tool_requests": summary.model_tool_requests_count,
            "model_tools_by_name": count_maps.get("model_tool", {}),
            "file_change_operations": summary.file_change_operations_count,
            "file_change_occurrences": summary.file_change_occurrences_count,
            "web_operations": summary.web_operations_count,
            "web_queries": summary.web_queries_count,
            "web_result_records": summary.web_result_records_count,
            "web_actions_by_name": count_maps.get("web_action", {}),
            "collaboration_operations": summary.collaboration_operations_count,
            "agents_started": summary.collaboration_agents_started_count,
            "compactions": summary.compactions_count,
        },
    }


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
    threads = payload.get("threads")
    if isinstance(threads, list):
        print("Threads:", flush=True)
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            lifecycle = thread.get("lifecycle", {})
            tokens = thread.get("token_usage", {})
            activity = thread.get("activity", {})
            label = thread.get("agent_nickname") or thread.get("source_kind")
            print(
                f"  {thread.get('rodex_sessions_statistics_sources_id')} {label}: "
                f"turns={lifecycle.get('turns_started')} "
                f"tokens={tokens.get('total_tokens')} "
                f"commands={activity.get('commands_executed')} "
                f"web_queries={activity.get('web_queries')}",
                flush=True,
            )
