"""Exact SQLite schema and durable Rodex registry identity."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from cool_name.functions import create_and_verify_cool_names_schema
from rodex_sql import default_rodex_database_path as _default_rodex_database_path
from rodex_sql import (
    normalise_rodex_database_path,
    open_rodex_read_transaction,
    open_rodex_transaction,
    require_existing_rodex_database_path,
)

from .errors import RodexSessionError
from .identity import RodexRegistryId
from .statistics_fields import SESSION_STATISTICS_SCALARS, TURN_STATISTICS_SCALARS

RODEX_SESSIONS_TABLE: Final = "rodex_sessions"
RODEX_REGISTRIES_TABLE: Final = "rodex_registries"
RODEX_REGISTRIES_ID_UNIQUE_INDEX: Final = "rodex_registries_registry_id_unique"
RODEX_SESSION_ID_UNIQUE_INDEX: Final = "rodex_sessions_session_id_unique"
RODEX_SESSIONS_USERS_TABLE: Final = "rodex_sessions_users"
RODEX_SESSIONS_USERS_UNIQUE_INDEX: Final = "rodex_sessions_users_uid_gid_user_name_unique"
RODEX_SESSIONS_LOG_TABLE: Final = "rodex_sessions_log"
RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_log_rodex_sessions_id_unique"
)
RODEX_CODEX_SESSION_ID_UNIQUE_INDEX: Final = "rodex_sessions_codex_session_id_unique"
RODEX_TMUX_SESSIONS_TABLE: Final = "rodex_tmux_sessions"
RODEX_TMUX_SESSIONS_SESSION_UNIQUE_INDEX: Final = (
    "rodex_tmux_sessions_rodex_sessions_id_unique"
)
RODEX_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX: Final = "rodex_tmux_sessions_endpoint_unique"
RODEX_RUNTIME_INSTANCES_TABLE: Final = "rodex_runtime_instances"
RODEX_RUNTIME_INSTANCES_SESSION_UNIQUE_INDEX: Final = (
    "rodex_runtime_instances_rodex_sessions_id_unique"
)
RODEX_RUNTIME_INSTANCES_RUNTIME_ID_UNIQUE_INDEX: Final = (
    "rodex_runtime_instances_runtime_id_unique"
)
MODEL_NAMES_TABLE: Final = "model_names"
MODEL_NAMES_NAME_OF_THE_MODEL_UNIQUE_INDEX: Final = "model_names_name_of_the_model_unique"
REASONING_EFFORT_NAMES_TABLE: Final = "reasoning_effort_names"
REASONING_EFFORT_NAMES_NAME_OF_THE_REASONING_EFFORT_UNIQUE_INDEX: Final = (
    "reasoning_effort_names_name_of_the_reasoning_effort_unique"
)
RODEX_SESSIONS_STATISTICS_TABLE: Final = "rodex_sessions_statistics"
RODEX_SESSIONS_STATISTICS_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_rodex_sessions_id_unique"
)
RODEX_SESSIONS_STATISTICS_SESSION_PUBLICATION_SEQUENCE_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_session_publication_sequence_unique"
)
RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE: Final = (
    "rodex_sessions_statistics_distributions"
)
RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_distributions_kind_unique"
)
RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE: Final = (
    "rodex_sessions_statistics_named_counts"
)
RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_named_counts_key_unique"
)
RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE: Final = (
    "rodex_sessions_statistics_audit_limits"
)
RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_audit_limits_ordinal_unique"
)
RODEX_SESSIONS_STATISTICS_SOURCES_TABLE: Final = "rodex_sessions_statistics_sources"
RODEX_SESSIONS_STATISTICS_SOURCES_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_sources_codex_thread_id_unique"
)
RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_ID_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_sources_session_id_unique"
)
RODEX_SESSIONS_STATISTICS_SOURCES_PARENT_INDEX: Final = (
    "rodex_sessions_statistics_sources_parent"
)
RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_sources_session_id_publication_sequence_unique"
)
RODEX_SESSIONS_STATISTICS_SOURCES_HIERARCHY_PUBLICATION_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_sources_hierarchy_publication_unique"
)
RODEX_SESSIONS_STATISTICS_TURNS_TABLE: Final = "rodex_sessions_statistics_turns"
RODEX_SESSIONS_STATISTICS_TURNS_SOURCE_TURN_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_turns_source_turn_unique"
)
RODEX_SESSIONS_STATISTICS_TURNS_SESSION_TURN_INDEX: Final = (
    "rodex_sessions_statistics_turns_session_turn"
)
RODEX_SESSIONS_STATISTICS_TURNS_SESSION_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_turns_session_id_publication_sequence_unique"
)
RODEX_SESSIONS_STATISTICS_TURNS_SOURCE_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_turns_source_id_publication_sequence_unique"
)
RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE: Final = (
    "rodex_sessions_statistics_subagent_spawns"
)
RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_SOURCE_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_subagent_spawns_source_unique"
)
RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TURN_INDEX: Final = (
    "rodex_sessions_statistics_subagent_spawns_turn"
)
RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE: Final = (
    "rodex_sessions_statistics_turn_named_counts"
)
RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_turn_named_counts_key_unique"
)
RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_SESSION_KIND_INDEX: Final = (
    "rodex_sessions_statistics_turn_named_counts_session_kind"
)
RODEX_SESSIONS_STATISTICS_WORKERS_TABLE: Final = "rodex_sessions_statistics_workers"
RODEX_SESSIONS_STATISTICS_WORKERS_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_workers_rodex_sessions_id_unique"
)
RODEX_SESSIONS_COOL_NAMES_UNIQUE_INDEX: Final = "rodex_sessions_cool_names_id_unique"
RODEX_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX: Final = (
    "rodex_sessions_user_defined_cool_names_id_unique"
)
STATISTICS_COVERAGE_STATES: Final = frozenset({"complete", "gapped"})
STATISTICS_TURN_OUTCOMES: Final = frozenset({"open", "completed", "aborted"})
STATISTICS_WORKER_STATES: Final = frozenset(
    {"starting", "catching_up", "up_to_date", "degraded", "stopped"}
)
_CREATE_REGISTRIES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_REGISTRIES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_registry_id_signed_bigint BIGINT NOT NULL CHECK (
        typeof(rodex_registry_id_signed_bigint) = 'integer'
    ),
    CHECK (id = 1)
)
"""
_CREATE_REGISTRIES_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_REGISTRIES_ID_UNIQUE_INDEX}
ON {RODEX_REGISTRIES_TABLE} (rodex_registry_id_signed_bigint)
"""
_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_session_id_signed_bigint BIGINT NOT NULL CHECK (
        typeof(rodex_session_id_signed_bigint) = 'integer'
    ),
    codex_session_id_signed_bigint_1 BIGINT NOT NULL CHECK (
        typeof(codex_session_id_signed_bigint_1) = 'integer'
    ),
    codex_session_id_signed_bigint_2 BIGINT NOT NULL CHECK (
        typeof(codex_session_id_signed_bigint_2) = 'integer'
    ),
    cool_names_id INTEGER NOT NULL,
    user_defined_cool_names_id INTEGER DEFAULT NULL,
    FOREIGN KEY (cool_names_id) REFERENCES cool_names (id),
    FOREIGN KEY (user_defined_cool_names_id) REFERENCES cool_names (id)
)
"""
_CREATE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSION_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE} (rodex_session_id_signed_bigint)
"""
_CREATE_CODEX_SESSION_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_CODEX_SESSION_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE}
    (codex_session_id_signed_bigint_1, codex_session_id_signed_bigint_2)
"""
_CREATE_SESSIONS_COOL_NAMES_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_COOL_NAMES_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE} (cool_names_id)
"""
_CREATE_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE} (user_defined_cool_names_id)
"""
_CREATE_USERS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_USERS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid INTEGER NOT NULL,
    gid INTEGER NOT NULL,
    user_name TEXT NOT NULL
)
"""
_CREATE_USERS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_USERS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_USERS_TABLE} (uid, gid, user_name)
"""
_CREATE_LOG_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_LOG_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    rodex_sessions_users_id INTEGER NOT NULL,
    last_accessed_at_utc TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id),
    FOREIGN KEY (rodex_sessions_users_id) REFERENCES {RODEX_SESSIONS_USERS_TABLE} (id)
)
"""
_CREATE_LOG_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_LOG_TABLE} (rodex_sessions_id)
"""
_CREATE_TMUX_SESSIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_TMUX_SESSIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    tmux_server_socket_path TEXT NOT NULL,
    tmux_session_name TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_TMUX_SESSIONS_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_TMUX_SESSIONS_SESSION_UNIQUE_INDEX}
ON {RODEX_TMUX_SESSIONS_TABLE} (rodex_sessions_id)
"""
_CREATE_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX}
ON {RODEX_TMUX_SESSIONS_TABLE} (tmux_server_socket_path, tmux_session_name)
"""
_CREATE_RUNTIME_INSTANCES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_RUNTIME_INSTANCES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    runtime_id_signed_bigint BIGINT NOT NULL CHECK (
        typeof(runtime_id_signed_bigint) = 'integer'
    ),
    started_at_utc TEXT NOT NULL,
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_RUNTIME_INSTANCES_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_RUNTIME_INSTANCES_SESSION_UNIQUE_INDEX}
ON {RODEX_RUNTIME_INSTANCES_TABLE} (rodex_sessions_id)
"""
_CREATE_RUNTIME_INSTANCES_RUNTIME_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_RUNTIME_INSTANCES_RUNTIME_ID_UNIQUE_INDEX}
ON {RODEX_RUNTIME_INSTANCES_TABLE} (runtime_id_signed_bigint)
"""
_CREATE_MODEL_NAMES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {MODEL_NAMES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name_of_the_model TEXT NOT NULL CHECK (length(trim(name_of_the_model)) > 0)
)
"""
_CREATE_MODEL_NAMES_NAME_OF_THE_MODEL_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {MODEL_NAMES_NAME_OF_THE_MODEL_UNIQUE_INDEX}
ON {MODEL_NAMES_TABLE} (name_of_the_model)
"""
_CREATE_REASONING_EFFORT_NAMES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {REASONING_EFFORT_NAMES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name_of_the_reasoning_effort TEXT NOT NULL CHECK (
        length(trim(name_of_the_reasoning_effort)) > 0
    )
)
"""
_CREATE_REASONING_EFFORT_NAMES_NAME_OF_THE_REASONING_EFFORT_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {REASONING_EFFORT_NAMES_NAME_OF_THE_REASONING_EFFORT_UNIQUE_INDEX}
ON {REASONING_EFFORT_NAMES_TABLE} (name_of_the_reasoning_effort)
"""
_CREATE_STATISTICS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    statistics_publication_sequence INTEGER NOT NULL CHECK (
        statistics_publication_sequence >= 1
    ),
    statistics_projection_schema_version TEXT NOT NULL,
    calculated_at_utc TEXT NOT NULL,
    coverage_state TEXT NOT NULL CHECK (coverage_state IN ('complete', 'gapped')),
    analyzer_event_count INTEGER NOT NULL CHECK (analyzer_event_count >= 0),
    analyzer_source_count INTEGER NOT NULL CHECK (analyzer_source_count >= 0),
    history_sessions_count INTEGER NOT NULL CHECK (history_sessions_count >= 0),
    history_records_count INTEGER NOT NULL CHECK (history_records_count >= 0),
    history_malformed_records_count INTEGER NOT NULL CHECK (
        history_malformed_records_count >= 0
    ),
    turns_started_count INTEGER NOT NULL CHECK (turns_started_count >= 0),
    turns_completed_count INTEGER NOT NULL CHECK (turns_completed_count >= 0),
    turns_aborted_count INTEGER NOT NULL CHECK (turns_aborted_count >= 0),
    turns_open_count INTEGER NOT NULL CHECK (turns_open_count >= 0),
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL CHECK (cached_input_tokens >= 0),
    cache_write_input_tokens INTEGER NOT NULL CHECK (cache_write_input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    reasoning_output_tokens INTEGER NOT NULL CHECK (reasoning_output_tokens >= 0),
    total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
    context_observation_count INTEGER NOT NULL CHECK (context_observation_count >= 0),
    context_latest_session_median_percent REAL DEFAULT NULL CHECK (
        context_latest_session_median_percent IS NULL
        OR context_latest_session_median_percent BETWEEN 0 AND 100
    ),
    context_high_water_percent REAL NOT NULL CHECK (
        context_high_water_percent BETWEEN 0 AND 100
    ),
    commands_executed_count INTEGER NOT NULL CHECK (commands_executed_count >= 0),
    model_tool_requests_count INTEGER NOT NULL CHECK (model_tool_requests_count >= 0),
    model_tool_outputs_paired_count INTEGER NOT NULL CHECK (
        model_tool_outputs_paired_count >= 0
        AND model_tool_outputs_paired_count <= model_tool_requests_count
    ),
    file_change_operations_count INTEGER NOT NULL CHECK (
        file_change_operations_count >= 0
    ),
    file_change_distinct_paths_count INTEGER NOT NULL CHECK (
        file_change_distinct_paths_count >= 0
    ),
    file_change_occurrences_count INTEGER NOT NULL CHECK (
        file_change_occurrences_count >= 0
    ),
    web_operations_count INTEGER NOT NULL CHECK (web_operations_count >= 0),
    web_queries_count INTEGER NOT NULL CHECK (web_queries_count >= 0),
    web_result_records_count INTEGER NOT NULL CHECK (web_result_records_count >= 0),
    web_distinct_result_or_action_urls_count INTEGER NOT NULL CHECK (
        web_distinct_result_or_action_urls_count >= 0
    ),
    compactions_count INTEGER NOT NULL CHECK (compactions_count >= 0),
    distinct_workspaces_count INTEGER NOT NULL CHECK (distinct_workspaces_count >= 0),
    typical_turns_count INTEGER NOT NULL CHECK (typical_turns_count >= 0),
    hands_on_turn_count INTEGER NOT NULL CHECK (
        hands_on_turn_count >= 0 AND hands_on_turn_count <= turns_started_count
    ),
    hands_on_turn_rate_percent REAL DEFAULT NULL CHECK (
        hands_on_turn_rate_percent IS NULL OR hands_on_turn_rate_percent BETWEEN 0 AND 100
    ),
    turns_with_nonzero_command_count INTEGER NOT NULL CHECK (
        turns_with_nonzero_command_count >= 0
    ),
    turns_subsequently_completed_count INTEGER NOT NULL CHECK (
        turns_subsequently_completed_count >= 0
        AND turns_subsequently_completed_count <= turns_with_nonzero_command_count
    ),
    completed_after_nonzero_command_percent REAL DEFAULT NULL CHECK (
        completed_after_nonzero_command_percent IS NULL
        OR completed_after_nonzero_command_percent BETWEEN 0 AND 100
    ),
    command_zero_exit_rate_percent REAL DEFAULT NULL CHECK (
        command_zero_exit_rate_percent IS NULL
        OR command_zero_exit_rate_percent BETWEEN 0 AND 100
    ),
    repeated_command_execution_count INTEGER NOT NULL CHECK (
        repeated_command_execution_count >= 0
        AND repeated_command_execution_count <= commands_executed_count
    ),
    exact_command_repeat_rate_percent REAL DEFAULT NULL CHECK (
        exact_command_repeat_rate_percent IS NULL
        OR exact_command_repeat_rate_percent BETWEEN 0 AND 100
    ),
    cached_input_share_percent REAL DEFAULT NULL CHECK (
        cached_input_share_percent IS NULL OR cached_input_share_percent BETWEEN 0 AND 100
    ),
    reasoning_output_share_percent REAL DEFAULT NULL CHECK (
        reasoning_output_share_percent IS NULL
        OR reasoning_output_share_percent BETWEEN 0 AND 100
    ),
    edited_turns_count INTEGER NOT NULL CHECK (edited_turns_count >= 0),
    verified_after_edit_count INTEGER NOT NULL CHECK (
        verified_after_edit_count >= 0 AND verified_after_edit_count <= edited_turns_count
    ),
    edit_then_verify_percent REAL DEFAULT NULL CHECK (
        edit_then_verify_percent IS NULL OR edit_then_verify_percent BETWEEN 0 AND 100
    ),
    web_turns_count INTEGER NOT NULL CHECK (web_turns_count >= 0),
    web_later_command_or_file_work_count INTEGER NOT NULL CHECK (
        web_later_command_or_file_work_count >= 0
        AND web_later_command_or_file_work_count <= web_turns_count
    ),
    web_follow_through_percent REAL DEFAULT NULL CHECK (
        web_follow_through_percent IS NULL OR web_follow_through_percent BETWEEN 0 AND 100
    ),
    revisited_distinct_path_count INTEGER NOT NULL CHECK (
        revisited_distinct_path_count >= 0
        AND revisited_distinct_path_count <= file_change_distinct_paths_count
    ),
    file_revisit_rate_percent REAL DEFAULT NULL CHECK (
        file_revisit_rate_percent IS NULL OR file_revisit_rate_percent BETWEEN 0 AND 100
    ),
    workspace_tagged_turn_count INTEGER NOT NULL CHECK (workspace_tagged_turn_count >= 0),
    turns_in_busiest_workspace_count INTEGER NOT NULL CHECK (
        turns_in_busiest_workspace_count >= 0
        AND turns_in_busiest_workspace_count <= workspace_tagged_turn_count
    ),
    busiest_workspace_turn_share_percent REAL DEFAULT NULL CHECK (
        busiest_workspace_turn_share_percent IS NULL
        OR busiest_workspace_turn_share_percent BETWEEN 0 AND 100
    ),
    turns_with_local_hour_count INTEGER NOT NULL CHECK (turns_with_local_hour_count >= 0),
    busiest_local_hour INTEGER DEFAULT NULL CHECK (
        busiest_local_hour IS NULL OR busiest_local_hour BETWEEN 0 AND 23
    ),
    turns_in_busiest_local_hour_count INTEGER NOT NULL CHECK (
        turns_in_busiest_local_hour_count >= 0
        AND turns_in_busiest_local_hour_count <= turns_with_local_hour_count
    ),
    goal_updates_count INTEGER NOT NULL CHECK (goal_updates_count >= 0),
    audit_privacy TEXT NOT NULL CHECK (length(audit_privacy) > 0),
    audit_percentile_method TEXT NOT NULL CHECK (length(audit_percentile_method) > 0),
    audit_token_method TEXT NOT NULL CHECK (length(audit_token_method) > 0),
    audit_token_snapshots_count INTEGER NOT NULL CHECK (audit_token_snapshots_count >= 0),
    audit_repeated_token_snapshots_count INTEGER NOT NULL CHECK (
        audit_repeated_token_snapshots_count >= 0
    ),
    audit_token_epochs_count INTEGER NOT NULL CHECK (audit_token_epochs_count >= 0),
    audit_duplicate_operations_ignored_count INTEGER NOT NULL CHECK (
        audit_duplicate_operations_ignored_count >= 0
    ),
    audit_duplicate_terminals_ignored_count INTEGER NOT NULL CHECK (
        audit_duplicate_terminals_ignored_count >= 0
    ),
    audit_terminal_events_without_start_ignored_count INTEGER NOT NULL CHECK (
        audit_terminal_events_without_start_ignored_count >= 0
    ),
    audit_new_event_type_warnings_count INTEGER NOT NULL CHECK (
        audit_new_event_type_warnings_count >= 0
    ),
    CHECK (
        turns_started_count = turns_completed_count + turns_aborted_count + turns_open_count
    ),
    CHECK (cached_input_tokens <= input_tokens),
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_STATISTICS_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_SESSION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TABLE} (rodex_sessions_id)
"""
_CREATE_STATISTICS_SESSION_PUBLICATION_SEQUENCE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_SESSION_PUBLICATION_SEQUENCE_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TABLE}
    (rodex_sessions_id, statistics_publication_sequence)
"""
_CREATE_STATISTICS_DISTRIBUTIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    included_statistics_publication_sequence INTEGER NOT NULL CHECK (
        included_statistics_publication_sequence >= 1
    ),
    distribution_kind TEXT NOT NULL CHECK (distribution_kind IN (
        'completed_turn_duration_ms', 'time_to_first_token_ms',
        'per_turn_total_tokens', 'command_duration_ms', 'commands_per_turn',
        'tool_requests_per_turn', 'files_per_turn'
    )),
    observation_count INTEGER NOT NULL CHECK (observation_count >= 0),
    total INTEGER NOT NULL CHECK (total >= 0),
    median REAL DEFAULT NULL CHECK (median IS NULL OR median >= 0),
    p75 INTEGER DEFAULT NULL CHECK (p75 IS NULL OR p75 >= 0),
    p90 INTEGER DEFAULT NULL CHECK (p90 IS NULL OR p90 >= 0),
    p95 INTEGER DEFAULT NULL CHECK (p95 IS NULL OR p95 >= 0),
    maximum INTEGER DEFAULT NULL CHECK (maximum IS NULL OR maximum >= 0),
    CHECK (
        (observation_count = 0 AND total = 0 AND median IS NULL AND p75 IS NULL
            AND p90 IS NULL AND p95 IS NULL AND maximum IS NULL)
        OR
        (observation_count > 0 AND median IS NOT NULL AND p75 IS NOT NULL
            AND p90 IS NOT NULL AND p95 IS NOT NULL AND maximum IS NOT NULL
            AND p75 <= p90 AND p90 <= p95 AND p95 <= maximum)
    ),
    FOREIGN KEY (rodex_sessions_id, included_statistics_publication_sequence)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TABLE}
            (rodex_sessions_id, statistics_publication_sequence)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_STATISTICS_DISTRIBUTIONS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE}
    (rodex_sessions_id, distribution_kind)
"""
_CREATE_STATISTICS_NAMED_COUNTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    included_statistics_publication_sequence INTEGER NOT NULL CHECK (
        included_statistics_publication_sequence >= 1
    ),
    count_kind TEXT NOT NULL CHECK (count_kind IN (
        'command_exit_status', 'command_family', 'model_tool', 'file_change_type',
        'web_action', 'goal_status'
    )),
    count_name TEXT NOT NULL CHECK (length(count_name) > 0),
    occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
    FOREIGN KEY (rodex_sessions_id, included_statistics_publication_sequence)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TABLE}
            (rodex_sessions_id, statistics_publication_sequence)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_STATISTICS_NAMED_COUNTS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE}
    (rodex_sessions_id, count_kind, count_name)
"""
_CREATE_STATISTICS_AUDIT_LIMITS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    included_statistics_publication_sequence INTEGER NOT NULL CHECK (
        included_statistics_publication_sequence >= 1
    ),
    limit_ordinal INTEGER NOT NULL CHECK (limit_ordinal >= 0),
    limitation TEXT NOT NULL CHECK (length(limitation) > 0),
    FOREIGN KEY (rodex_sessions_id, included_statistics_publication_sequence)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TABLE}
            (rodex_sessions_id, statistics_publication_sequence)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_STATISTICS_AUDIT_LIMITS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE}
    (rodex_sessions_id, limit_ordinal)
"""
_CREATE_STATISTICS_SOURCES_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    codex_thread_id_signed_bigint_1 BIGINT NOT NULL CHECK (
        typeof(codex_thread_id_signed_bigint_1) = 'integer'
    ),
    codex_thread_id_signed_bigint_2 BIGINT NOT NULL CHECK (
        typeof(codex_thread_id_signed_bigint_2) = 'integer'
    ),
    parent_rodex_sessions_statistics_sources_id INTEGER DEFAULT NULL,
    agent_path TEXT DEFAULT NULL,
    agent_nickname TEXT DEFAULT NULL,
    subagent_history_start_ordinal INTEGER DEFAULT NULL CHECK (
        subagent_history_start_ordinal IS NULL
        OR subagent_history_start_ordinal >= 0
    ),
    first_linked_at_utc TEXT NOT NULL,
    rollout_file_path TEXT DEFAULT NULL,
    analyzed_size_bytes INTEGER DEFAULT NULL CHECK (
        analyzed_size_bytes IS NULL OR analyzed_size_bytes >= 0
    ),
    analyzed_mtime_ns INTEGER DEFAULT NULL CHECK (
        analyzed_mtime_ns IS NULL OR analyzed_mtime_ns >= 0
    ),
    analyzed_prefix_sha256 TEXT DEFAULT NULL CHECK (
        analyzed_prefix_sha256 IS NULL OR length(analyzed_prefix_sha256) = 64
    ),
    verified_at_utc TEXT DEFAULT NULL,
    included_statistics_publication_sequence INTEGER DEFAULT NULL CHECK (
        included_statistics_publication_sequence IS NULL
        OR included_statistics_publication_sequence >= 1
    ),
    CHECK (
        included_statistics_publication_sequence IS NULL
        OR rollout_file_path IS NOT NULL
    ),
    CHECK (
        (rollout_file_path IS NULL AND analyzed_size_bytes IS NULL
            AND analyzed_mtime_ns IS NULL AND analyzed_prefix_sha256 IS NULL
            AND verified_at_utc IS NULL)
        OR
        (rollout_file_path IS NOT NULL AND analyzed_size_bytes IS NOT NULL
            AND analyzed_mtime_ns IS NOT NULL AND analyzed_prefix_sha256 IS NOT NULL
            AND verified_at_utc IS NOT NULL)
    ),
    CHECK (
        (parent_rodex_sessions_statistics_sources_id IS NULL
            AND agent_path IS NULL
            AND agent_nickname IS NULL
            AND subagent_history_start_ordinal IS NULL)
        OR
        (parent_rodex_sessions_statistics_sources_id IS NOT NULL
            AND agent_path IS NOT NULL
            AND length(agent_path) > 0
            AND subagent_history_start_ordinal IS NOT NULL)
    ),
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id),
    FOREIGN KEY (rodex_sessions_id, parent_rodex_sessions_statistics_sources_id)
        REFERENCES {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} (rodex_sessions_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (rodex_sessions_id, included_statistics_publication_sequence)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TABLE}
            (rodex_sessions_id, statistics_publication_sequence)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_STATISTICS_SOURCES_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_SOURCES_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE}
    (codex_thread_id_signed_bigint_1, codex_thread_id_signed_bigint_2)
"""
_CREATE_STATISTICS_SOURCES_SESSION_ID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_ID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} (rodex_sessions_id, id)
"""
_CREATE_STATISTICS_SOURCES_PARENT_INDEX = f"""
CREATE INDEX IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_SOURCES_PARENT_INDEX}
ON {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE}
    (parent_rodex_sessions_statistics_sources_id)
"""
_CREATE_STATISTICS_SOURCES_SESSION_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE}
    (rodex_sessions_id, id, included_statistics_publication_sequence)
"""
_CREATE_STATISTICS_SOURCES_HIERARCHY_PUBLICATION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_SOURCES_HIERARCHY_PUBLICATION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE}
    (rodex_sessions_id, id, parent_rodex_sessions_statistics_sources_id,
        included_statistics_publication_sequence)
"""
_CREATE_STATISTICS_TURNS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_statistics_sources_id INTEGER NOT NULL,
    codex_turn_id_sha256_int_1 BIGINT NOT NULL CHECK (
        typeof(codex_turn_id_sha256_int_1) = 'integer'
    ),
    codex_turn_id_sha256_int_2 BIGINT NOT NULL CHECK (
        typeof(codex_turn_id_sha256_int_2) = 'integer'
    ),
    codex_turn_id_sha256_int_3 BIGINT NOT NULL CHECK (
        typeof(codex_turn_id_sha256_int_3) = 'integer'
    ),
    codex_turn_id_sha256_int_4 BIGINT NOT NULL CHECK (
        typeof(codex_turn_id_sha256_int_4) = 'integer'
    ),
    codex_turn_id TEXT NOT NULL,
    included_statistics_publication_sequence INTEGER NOT NULL CHECK (
        included_statistics_publication_sequence >= 1
    ),
    started_at_utc TEXT DEFAULT NULL,
    terminal_at_utc TEXT DEFAULT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('open', 'completed', 'aborted')),
    model_names_id INTEGER DEFAULT NULL,
    reasoning_effort_names_id INTEGER DEFAULT NULL,
    duration_ms INTEGER DEFAULT NULL CHECK (duration_ms IS NULL OR duration_ms >= 0),
    time_to_first_token_ms INTEGER DEFAULT NULL CHECK (
        time_to_first_token_ms IS NULL OR time_to_first_token_ms >= 0
    ),
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL CHECK (
        cached_input_tokens >= 0 AND cached_input_tokens <= input_tokens
    ),
    cache_write_input_tokens INTEGER NOT NULL CHECK (cache_write_input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    reasoning_output_tokens INTEGER NOT NULL CHECK (reasoning_output_tokens >= 0),
    total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
    context_observation_count INTEGER NOT NULL CHECK (context_observation_count >= 0),
    context_high_water_percent REAL NOT NULL CHECK (
        context_high_water_percent BETWEEN 0 AND 100
    ),
    commands_executed_count INTEGER NOT NULL CHECK (commands_executed_count >= 0),
    command_duration_observation_count INTEGER NOT NULL CHECK (
        command_duration_observation_count >= 0
    ),
    command_duration_total_ms INTEGER NOT NULL CHECK (command_duration_total_ms >= 0),
    command_duration_median_ms REAL DEFAULT NULL CHECK (
        command_duration_median_ms IS NULL OR command_duration_median_ms >= 0
    ),
    command_duration_p75_ms INTEGER DEFAULT NULL CHECK (
        command_duration_p75_ms IS NULL OR command_duration_p75_ms >= 0
    ),
    command_duration_p90_ms INTEGER DEFAULT NULL CHECK (
        command_duration_p90_ms IS NULL OR command_duration_p90_ms >= 0
    ),
    command_duration_p95_ms INTEGER DEFAULT NULL CHECK (
        command_duration_p95_ms IS NULL OR command_duration_p95_ms >= 0
    ),
    command_duration_maximum_ms INTEGER DEFAULT NULL CHECK (
        command_duration_maximum_ms IS NULL OR command_duration_maximum_ms >= 0
    ),
    model_tool_requests_count INTEGER NOT NULL CHECK (model_tool_requests_count >= 0),
    model_tool_outputs_paired_count INTEGER NOT NULL CHECK (
        model_tool_outputs_paired_count >= 0
        AND model_tool_outputs_paired_count <= model_tool_requests_count
    ),
    file_change_operations_count INTEGER NOT NULL CHECK (
        file_change_operations_count >= 0
    ),
    file_change_distinct_paths_count INTEGER NOT NULL CHECK (
        file_change_distinct_paths_count >= 0
    ),
    file_change_occurrences_count INTEGER NOT NULL CHECK (
        file_change_occurrences_count >= 0
    ),
    web_operations_count INTEGER NOT NULL CHECK (web_operations_count >= 0),
    web_queries_count INTEGER NOT NULL CHECK (web_queries_count >= 0),
    web_result_records_count INTEGER NOT NULL CHECK (web_result_records_count >= 0),
    web_distinct_result_or_action_urls_count INTEGER NOT NULL CHECK (
        web_distinct_result_or_action_urls_count >= 0
    ),
    compactions_count INTEGER NOT NULL CHECK (compactions_count >= 0),
    workspace_digest TEXT DEFAULT NULL,
    local_start_hour INTEGER DEFAULT NULL CHECK (
        local_start_hour IS NULL OR local_start_hour BETWEEN 0 AND 23
    ),
    hands_on INTEGER NOT NULL CHECK (hands_on IN (0, 1)),
    completed_after_nonzero_command INTEGER NOT NULL CHECK (
        completed_after_nonzero_command IN (0, 1)
    ),
    cached_input_share_percent REAL DEFAULT NULL CHECK (
        cached_input_share_percent IS NULL OR cached_input_share_percent BETWEEN 0 AND 100
    ),
    reasoning_output_share_percent REAL DEFAULT NULL CHECK (
        reasoning_output_share_percent IS NULL
        OR reasoning_output_share_percent BETWEEN 0 AND 100
    ),
    edited_then_verified INTEGER NOT NULL CHECK (edited_then_verified IN (0, 1)),
    web_research_followed_by_command_or_file_work INTEGER NOT NULL CHECK (
        web_research_followed_by_command_or_file_work IN (0, 1)
    ),
    goal_updates_count INTEGER NOT NULL CHECK (goal_updates_count >= 0),
    CHECK (outcome != 'open' OR terminal_at_utc IS NULL),
    CHECK (
        (command_duration_observation_count = 0 AND command_duration_total_ms = 0
            AND command_duration_median_ms IS NULL AND command_duration_p75_ms IS NULL
            AND command_duration_p90_ms IS NULL AND command_duration_p95_ms IS NULL
            AND command_duration_maximum_ms IS NULL)
        OR
        (command_duration_observation_count > 0
            AND command_duration_median_ms IS NOT NULL
            AND command_duration_p75_ms IS NOT NULL
            AND command_duration_p90_ms IS NOT NULL
            AND command_duration_p95_ms IS NOT NULL
            AND command_duration_maximum_ms IS NOT NULL
            AND command_duration_p75_ms <= command_duration_p90_ms
            AND command_duration_p90_ms <= command_duration_p95_ms
            AND command_duration_p95_ms <= command_duration_maximum_ms)
    ),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_statistics_sources_id,
        included_statistics_publication_sequence)
        REFERENCES {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE}
            (rodex_sessions_id, id, included_statistics_publication_sequence)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (rodex_sessions_id, included_statistics_publication_sequence)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TABLE}
            (rodex_sessions_id, statistics_publication_sequence)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (model_names_id) REFERENCES {MODEL_NAMES_TABLE} (id),
    FOREIGN KEY (reasoning_effort_names_id)
        REFERENCES {REASONING_EFFORT_NAMES_TABLE} (id)
)
"""
_CREATE_STATISTICS_TURNS_SOURCE_TURN_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_TURNS_SOURCE_TURN_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} (
    rodex_sessions_statistics_sources_id,
    codex_turn_id_sha256_int_1,
    codex_turn_id_sha256_int_2,
    codex_turn_id_sha256_int_3,
    codex_turn_id_sha256_int_4
)
"""
_CREATE_STATISTICS_TURNS_SESSION_TURN_INDEX = f"""
CREATE INDEX IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_TURNS_SESSION_TURN_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} (
    rodex_sessions_id,
    codex_turn_id_sha256_int_1,
    codex_turn_id_sha256_int_2,
    codex_turn_id_sha256_int_3,
    codex_turn_id_sha256_int_4
)
"""
_CREATE_STATISTICS_TURNS_SESSION_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_TURNS_SESSION_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TURNS_TABLE}
    (rodex_sessions_id, id, included_statistics_publication_sequence)
"""
_CREATE_STATISTICS_TURNS_SOURCE_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_TURNS_SOURCE_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TURNS_TABLE}
    (rodex_sessions_id, rodex_sessions_statistics_sources_id, id,
        included_statistics_publication_sequence)
"""
_CREATE_STATISTICS_SUBAGENT_SPAWNS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    subagent_rodex_sessions_statistics_sources_id INTEGER NOT NULL,
    parent_rodex_sessions_statistics_sources_id INTEGER NOT NULL,
    spawning_rodex_sessions_statistics_turns_id INTEGER NOT NULL,
    included_statistics_publication_sequence INTEGER NOT NULL CHECK (
        included_statistics_publication_sequence >= 1
    ),
    FOREIGN KEY (rodex_sessions_id,
        subagent_rodex_sessions_statistics_sources_id,
        parent_rodex_sessions_statistics_sources_id,
        included_statistics_publication_sequence)
        REFERENCES {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE}
            (rodex_sessions_id, id,
                parent_rodex_sessions_statistics_sources_id,
                included_statistics_publication_sequence)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (rodex_sessions_id,
        parent_rodex_sessions_statistics_sources_id,
        spawning_rodex_sessions_statistics_turns_id,
        included_statistics_publication_sequence)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TURNS_TABLE}
            (rodex_sessions_id, rodex_sessions_statistics_sources_id, id,
                included_statistics_publication_sequence)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        subagent_rodex_sessions_statistics_sources_id
        != parent_rodex_sessions_statistics_sources_id
    )
)
"""
_CREATE_STATISTICS_SUBAGENT_SPAWNS_SOURCE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_SOURCE_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE}
    (subagent_rodex_sessions_statistics_sources_id)
"""
_CREATE_STATISTICS_SUBAGENT_SPAWNS_TURN_INDEX = f"""
CREATE INDEX IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TURN_INDEX}
ON {RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE}
    (spawning_rodex_sessions_statistics_turns_id)
"""
_CREATE_STATISTICS_TURN_NAMED_COUNTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_statistics_turns_id INTEGER NOT NULL,
    included_statistics_publication_sequence INTEGER NOT NULL CHECK (
        included_statistics_publication_sequence >= 1
    ),
    count_kind TEXT NOT NULL CHECK (count_kind IN (
        'command_exit_status', 'command_family', 'model_tool', 'file_change_type',
        'web_action', 'goal_status'
    )),
    count_name TEXT NOT NULL CHECK (length(count_name) > 0),
    occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_statistics_turns_id,
        included_statistics_publication_sequence)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TURNS_TABLE}
            (rodex_sessions_id, id, included_statistics_publication_sequence)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_STATISTICS_TURN_NAMED_COUNTS_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE}
    (rodex_sessions_statistics_turns_id, count_kind, count_name)
"""
_CREATE_STATISTICS_TURN_NAMED_COUNTS_SESSION_KIND_INDEX = f"""
CREATE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_SESSION_KIND_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE}
    (rodex_sessions_id, count_kind, count_name)
"""
_CREATE_STATISTICS_WORKERS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_WORKERS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    worker_state TEXT NOT NULL CHECK (
        worker_state IN ('starting', 'catching_up', 'up_to_date', 'degraded', 'stopped')
    ),
    diagnostic_code TEXT DEFAULT NULL,
    last_attempted_at_utc TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
    next_retry_at_utc TEXT DEFAULT NULL,
    CHECK (
        worker_state != 'up_to_date'
        OR (diagnostic_code IS NULL AND consecutive_failures = 0
            AND next_retry_at_utc IS NULL)
    ),
    CHECK (
        diagnostic_code IS NULL
        OR (length(diagnostic_code) BETWEEN 1 AND 64
            AND diagnostic_code NOT GLOB '*[^a-z0-9_]*')
    ),
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id)
)
"""
_CREATE_STATISTICS_WORKERS_SESSION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_WORKERS_SESSION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_WORKERS_TABLE} (rodex_sessions_id)
"""


def default_rodex_database_path() -> Path:
    """Resolve the current user's durable Rodex database path."""
    return _default_rodex_database_path()


def existing_rodex_database_path(
    database_path: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve an existing private registry without bootstrapping or repairing it."""
    return require_existing_rodex_database_path(
        normalise_rodex_database_path(database_path)
    )


def initialise_rodex_database(database_path: str | os.PathLike[str] | None = None) -> Path:
    """Create and verify the current Rodex schema in one transaction."""
    path = normalise_rodex_database_path(database_path)
    with open_rodex_transaction(path) as connection:
        connection.execute(_CREATE_REGISTRIES_TABLE)
        _verify_registries_table(connection)
        connection.execute(_CREATE_REGISTRIES_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_REGISTRIES_TABLE,
            RODEX_REGISTRIES_ID_UNIQUE_INDEX,
            ["rodex_registry_id_signed_bigint"],
        )
        registry_rows = connection.execute(
            f"SELECT id, rodex_registry_id_signed_bigint FROM {RODEX_REGISTRIES_TABLE}"
        ).fetchall()
        if not registry_rows:
            registry_id = RodexRegistryId.generate()
            connection.execute(
                f"INSERT INTO {RODEX_REGISTRIES_TABLE} "
                "(rodex_registry_id_signed_bigint) VALUES (?)",
                (registry_id.as_signed_bigint(),),
            )
        elif len(registry_rows) != 1 or registry_rows[0][0] != 1:
            raise RodexSessionError("Rodex registry must contain exactly its id=1 row")
        create_and_verify_cool_names_schema(connection)
        connection.execute(_CREATE_TABLE)
        _verify_sessions_table(connection)
        connection.execute(_CREATE_UNIQUE_INDEX)
        connection.execute(_CREATE_CODEX_SESSION_ID_UNIQUE_INDEX)
        connection.execute(_CREATE_SESSIONS_COOL_NAMES_UNIQUE_INDEX)
        connection.execute(_CREATE_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX)
        _verify_sessions_unique_indexes(connection)
        connection.execute(_CREATE_USERS_TABLE)
        _verify_sessions_users_table(connection)
        connection.execute(_CREATE_USERS_UNIQUE_INDEX)
        _verify_sessions_users_unique_index(connection)
        connection.execute(_CREATE_LOG_TABLE)
        _verify_sessions_log_table(connection)
        connection.execute(_CREATE_LOG_SESSION_UNIQUE_INDEX)
        _verify_sessions_log_unique_index(connection)
        connection.execute(_CREATE_TMUX_SESSIONS_TABLE)
        _verify_tmux_sessions_table(connection)
        connection.execute(_CREATE_TMUX_SESSIONS_SESSION_UNIQUE_INDEX)
        connection.execute(_CREATE_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX)
        _verify_tmux_sessions_unique_indexes(connection)
        connection.execute(_CREATE_RUNTIME_INSTANCES_TABLE)
        _verify_runtime_instances_table(connection)
        connection.execute(_CREATE_RUNTIME_INSTANCES_SESSION_UNIQUE_INDEX)
        connection.execute(_CREATE_RUNTIME_INSTANCES_RUNTIME_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_RUNTIME_INSTANCES_TABLE,
            RODEX_RUNTIME_INSTANCES_SESSION_UNIQUE_INDEX,
            ["rodex_sessions_id"],
        )
        _verify_unique_index(
            connection,
            RODEX_RUNTIME_INSTANCES_TABLE,
            RODEX_RUNTIME_INSTANCES_RUNTIME_ID_UNIQUE_INDEX,
            ["runtime_id_signed_bigint"],
        )
        connection.execute(_CREATE_MODEL_NAMES_TABLE)
        _verify_model_names_table(connection)
        connection.execute(_CREATE_MODEL_NAMES_NAME_OF_THE_MODEL_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            MODEL_NAMES_TABLE,
            MODEL_NAMES_NAME_OF_THE_MODEL_UNIQUE_INDEX,
            ["name_of_the_model"],
        )
        connection.execute(_CREATE_REASONING_EFFORT_NAMES_TABLE)
        _verify_reasoning_effort_names_table(connection)
        connection.execute(
            _CREATE_REASONING_EFFORT_NAMES_NAME_OF_THE_REASONING_EFFORT_UNIQUE_INDEX
        )
        _verify_unique_index(
            connection,
            REASONING_EFFORT_NAMES_TABLE,
            REASONING_EFFORT_NAMES_NAME_OF_THE_REASONING_EFFORT_UNIQUE_INDEX,
            ["name_of_the_reasoning_effort"],
        )
        connection.execute(_CREATE_STATISTICS_TABLE)
        _verify_statistics_table(connection)
        connection.execute(_CREATE_STATISTICS_SESSION_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TABLE,
            RODEX_SESSIONS_STATISTICS_SESSION_UNIQUE_INDEX,
            ["rodex_sessions_id"],
        )
        connection.execute(_CREATE_STATISTICS_SESSION_PUBLICATION_SEQUENCE_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TABLE,
            RODEX_SESSIONS_STATISTICS_SESSION_PUBLICATION_SEQUENCE_UNIQUE_INDEX,
            ["rodex_sessions_id", "statistics_publication_sequence"],
        )
        connection.execute(_CREATE_STATISTICS_DISTRIBUTIONS_TABLE)
        _verify_statistics_distributions_table(connection)
        connection.execute(_CREATE_STATISTICS_DISTRIBUTIONS_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE,
            RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_UNIQUE_INDEX,
            ["rodex_sessions_id", "distribution_kind"],
        )
        connection.execute(_CREATE_STATISTICS_NAMED_COUNTS_TABLE)
        _verify_statistics_named_counts_table(connection)
        connection.execute(_CREATE_STATISTICS_NAMED_COUNTS_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE,
            RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_UNIQUE_INDEX,
            ["rodex_sessions_id", "count_kind", "count_name"],
        )
        connection.execute(_CREATE_STATISTICS_AUDIT_LIMITS_TABLE)
        _verify_statistics_audit_limits_table(connection)
        connection.execute(_CREATE_STATISTICS_AUDIT_LIMITS_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE,
            RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_UNIQUE_INDEX,
            ["rodex_sessions_id", "limit_ordinal"],
        )
        connection.execute(_CREATE_STATISTICS_SOURCES_TABLE)
        _verify_statistics_sources_table(connection)
        connection.execute(_CREATE_STATISTICS_SOURCES_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            RODEX_SESSIONS_STATISTICS_SOURCES_UNIQUE_INDEX,
            ["codex_thread_id_signed_bigint_1", "codex_thread_id_signed_bigint_2"],
        )
        connection.execute(_CREATE_STATISTICS_SOURCES_SESSION_ID_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_ID_UNIQUE_INDEX,
            ["rodex_sessions_id", "id"],
        )
        connection.execute(_CREATE_STATISTICS_SOURCES_PARENT_INDEX)
        _verify_index(
            connection,
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            RODEX_SESSIONS_STATISTICS_SOURCES_PARENT_INDEX,
            ["parent_rodex_sessions_statistics_sources_id"],
            unique=False,
        )
        connection.execute(
            _CREATE_STATISTICS_SOURCES_SESSION_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX
        )
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX,
            [
                "rodex_sessions_id",
                "id",
                "included_statistics_publication_sequence",
            ],
        )
        connection.execute(
            _CREATE_STATISTICS_SOURCES_HIERARCHY_PUBLICATION_UNIQUE_INDEX
        )
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            RODEX_SESSIONS_STATISTICS_SOURCES_HIERARCHY_PUBLICATION_UNIQUE_INDEX,
            [
                "rodex_sessions_id",
                "id",
                "parent_rodex_sessions_statistics_sources_id",
                "included_statistics_publication_sequence",
            ],
        )
        connection.execute(_CREATE_STATISTICS_TURNS_TABLE)
        _verify_statistics_turns_table(connection)
        connection.execute(_CREATE_STATISTICS_TURNS_SOURCE_TURN_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            RODEX_SESSIONS_STATISTICS_TURNS_SOURCE_TURN_UNIQUE_INDEX,
            [
                "rodex_sessions_statistics_sources_id",
                "codex_turn_id_sha256_int_1",
                "codex_turn_id_sha256_int_2",
                "codex_turn_id_sha256_int_3",
                "codex_turn_id_sha256_int_4",
            ],
        )
        connection.execute(_CREATE_STATISTICS_TURNS_SESSION_TURN_INDEX)
        _verify_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            RODEX_SESSIONS_STATISTICS_TURNS_SESSION_TURN_INDEX,
            [
                "rodex_sessions_id",
                "codex_turn_id_sha256_int_1",
                "codex_turn_id_sha256_int_2",
                "codex_turn_id_sha256_int_3",
                "codex_turn_id_sha256_int_4",
            ],
            unique=False,
        )
        connection.execute(
            _CREATE_STATISTICS_TURNS_SESSION_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX
        )
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            RODEX_SESSIONS_STATISTICS_TURNS_SESSION_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX,
            [
                "rodex_sessions_id",
                "id",
                "included_statistics_publication_sequence",
            ],
        )
        connection.execute(
            _CREATE_STATISTICS_TURNS_SOURCE_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX
        )
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            RODEX_SESSIONS_STATISTICS_TURNS_SOURCE_ID_PUBLICATION_SEQUENCE_UNIQUE_INDEX,
            [
                "rodex_sessions_id",
                "rodex_sessions_statistics_sources_id",
                "id",
                "included_statistics_publication_sequence",
            ],
        )
        connection.execute(_CREATE_STATISTICS_SUBAGENT_SPAWNS_TABLE)
        _verify_statistics_subagent_spawns_table(connection)
        connection.execute(_CREATE_STATISTICS_SUBAGENT_SPAWNS_SOURCE_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE,
            RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_SOURCE_UNIQUE_INDEX,
            ["subagent_rodex_sessions_statistics_sources_id"],
        )
        connection.execute(_CREATE_STATISTICS_SUBAGENT_SPAWNS_TURN_INDEX)
        _verify_index(
            connection,
            RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE,
            RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TURN_INDEX,
            ["spawning_rodex_sessions_statistics_turns_id"],
            unique=False,
        )
        connection.execute(_CREATE_STATISTICS_TURN_NAMED_COUNTS_TABLE)
        _verify_statistics_turn_named_counts_table(connection)
        connection.execute(_CREATE_STATISTICS_TURN_NAMED_COUNTS_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE,
            RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_UNIQUE_INDEX,
            ["rodex_sessions_statistics_turns_id", "count_kind", "count_name"],
        )
        connection.execute(_CREATE_STATISTICS_TURN_NAMED_COUNTS_SESSION_KIND_INDEX)
        _verify_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE,
            RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_SESSION_KIND_INDEX,
            ["rodex_sessions_id", "count_kind", "count_name"],
            unique=False,
        )
        connection.execute(_CREATE_STATISTICS_WORKERS_TABLE)
        _verify_statistics_workers_table(connection)
        connection.execute(_CREATE_STATISTICS_WORKERS_SESSION_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_WORKERS_TABLE,
            RODEX_SESSIONS_STATISTICS_WORKERS_SESSION_UNIQUE_INDEX,
            ["rodex_sessions_id"],
        )
    return path


def lookup_rodex_registry_id(
    database_path: str | os.PathLike[str] | None = None,
) -> RodexRegistryId:
    """Return the durable identity of one exact Rodex registry database."""
    path = existing_rodex_database_path(database_path)
    with open_rodex_read_transaction(path) as connection:
        row = connection.execute(
            f"SELECT rodex_registry_id_signed_bigint "
            f"FROM {RODEX_REGISTRIES_TABLE} WHERE id = 1"
        ).fetchone()
    if row is None:
        raise RodexSessionError("Rodex registry identity disappeared")
    return RodexRegistryId.from_signed_bigint(row[0])


def _verify_registries_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(f"PRAGMA table_info({RODEX_REGISTRIES_TABLE})").fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    if observed != [
        ("id", "INTEGER", 0, 1),
        ("rodex_registry_id_signed_bigint", "BIGINT", 1, 0),
    ]:
        raise RodexSessionError(f"{RODEX_REGISTRIES_TABLE} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_REGISTRIES_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if (
        "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition
        or "CHECK (ID = 1)" not in definition
        or "TYPEOF(RODEX_REGISTRY_ID_SIGNED_BIGINT) = 'INTEGER'" not in definition
    ):
        raise RodexSessionError(f"{RODEX_REGISTRIES_TABLE} constraints mismatch")


def _verify_sessions_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(f"PRAGMA table_info({RODEX_SESSIONS_TABLE})").fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("rodex_session_id_signed_bigint", "BIGINT", 1, 0),
        ("codex_session_id_signed_bigint_1", "BIGINT", 1, 0),
        ("codex_session_id_signed_bigint_2", "BIGINT", 1, 0),
        ("cool_names_id", "INTEGER", 1, 0),
        ("user_defined_cool_names_id", "INTEGER", 0, 0),
    ]
    if observed != expected:
        raise RodexSessionError(f"{RODEX_SESSIONS_TABLE} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_SESSIONS_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{RODEX_SESSIONS_TABLE} id must use AUTOINCREMENT")
    required_identity_constraints = (
        "TYPEOF(RODEX_SESSION_ID_SIGNED_BIGINT) = 'INTEGER'",
        "TYPEOF(CODEX_SESSION_ID_SIGNED_BIGINT_1) = 'INTEGER'",
        "TYPEOF(CODEX_SESSION_ID_SIGNED_BIGINT_2) = 'INTEGER'",
    )
    if any(fragment not in definition for fragment in required_identity_constraints):
        raise RodexSessionError(f"{RODEX_SESSIONS_TABLE} identity constraints mismatch")
    if columns[-1][4] != "NULL":
        raise RodexSessionError(
            f"{RODEX_SESSIONS_TABLE}.user_defined_cool_names_id must default to NULL"
        )
    foreign_keys = connection.execute(
        f"PRAGMA foreign_key_list({RODEX_SESSIONS_TABLE})"
    ).fetchall()
    observed_foreign_keys = {(row[2], row[3], row[4]) for row in foreign_keys}
    expected_foreign_keys = {
        ("cool_names", "cool_names_id", "id"),
        ("cool_names", "user_defined_cool_names_id", "id"),
    }
    if observed_foreign_keys != expected_foreign_keys:
        raise RodexSessionError(
            f"{RODEX_SESSIONS_TABLE} foreign keys mismatch: {observed_foreign_keys!r}"
        )


def _verify_sessions_unique_indexes(connection: sqlite3.Connection) -> None:
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_SESSION_ID_UNIQUE_INDEX,
        ["rodex_session_id_signed_bigint"],
    )
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_CODEX_SESSION_ID_UNIQUE_INDEX,
        ["codex_session_id_signed_bigint_1", "codex_session_id_signed_bigint_2"],
    )
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_SESSIONS_COOL_NAMES_UNIQUE_INDEX,
        ["cool_names_id"],
    )
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_SESSIONS_USER_DEFINED_COOL_NAMES_UNIQUE_INDEX,
        ["user_defined_cool_names_id"],
    )


def _verify_sessions_users_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(
        f"PRAGMA table_info({RODEX_SESSIONS_USERS_TABLE})"
    ).fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("uid", "INTEGER", 1, 0),
        ("gid", "INTEGER", 1, 0),
        ("user_name", "TEXT", 1, 0),
    ]
    if observed != expected:
        raise RodexSessionError(
            f"{RODEX_SESSIONS_USERS_TABLE} schema mismatch: {observed!r}"
        )
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_SESSIONS_USERS_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{RODEX_SESSIONS_USERS_TABLE} id must use AUTOINCREMENT")


def _verify_sessions_users_unique_index(connection: sqlite3.Connection) -> None:
    indexes = connection.execute(
        f"PRAGMA index_list({RODEX_SESSIONS_USERS_TABLE})"
    ).fetchall()
    matching_indexes = [
        row for row in indexes if row[1] == RODEX_SESSIONS_USERS_UNIQUE_INDEX
    ]
    index_columns = connection.execute(
        f"PRAGMA index_info({RODEX_SESSIONS_USERS_UNIQUE_INDEX})"
    ).fetchall()
    if (
        len(matching_indexes) != 1
        or matching_indexes[0][2] != 1
        or [row[2] for row in index_columns] != ["uid", "gid", "user_name"]
    ):
        raise RodexSessionError(
            "Rodex sessions users unique index is missing: "
            f"{RODEX_SESSIONS_USERS_UNIQUE_INDEX}"
        )


def _verify_sessions_log_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(
        f"PRAGMA table_info({RODEX_SESSIONS_LOG_TABLE})"
    ).fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("rodex_sessions_id", "INTEGER", 1, 0),
        ("created_at_utc", "TEXT", 1, 0),
        ("rodex_sessions_users_id", "INTEGER", 1, 0),
        ("last_accessed_at_utc", "TEXT", 1, 0),
    ]
    if observed != expected:
        raise RodexSessionError(f"{RODEX_SESSIONS_LOG_TABLE} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RODEX_SESSIONS_LOG_TABLE,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{RODEX_SESSIONS_LOG_TABLE} id must use AUTOINCREMENT")
    foreign_keys = connection.execute(
        f"PRAGMA foreign_key_list({RODEX_SESSIONS_LOG_TABLE})"
    ).fetchall()
    observed_foreign_keys = {(row[2], row[3], row[4]) for row in foreign_keys}
    expected_foreign_keys = {
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
        (RODEX_SESSIONS_USERS_TABLE, "rodex_sessions_users_id", "id"),
    }
    if observed_foreign_keys != expected_foreign_keys:
        raise RodexSessionError(
            f"{RODEX_SESSIONS_LOG_TABLE} foreign keys mismatch: {observed_foreign_keys!r}"
        )


def _verify_sessions_log_unique_index(connection: sqlite3.Connection) -> None:
    indexes = connection.execute(
        f"PRAGMA index_list({RODEX_SESSIONS_LOG_TABLE})"
    ).fetchall()
    matching_indexes = [
        row for row in indexes if row[1] == RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX
    ]
    index_columns = connection.execute(
        f"PRAGMA index_info({RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX})"
    ).fetchall()
    if (
        len(matching_indexes) != 1
        or matching_indexes[0][2] != 1
        or [row[2] for row in index_columns] != ["rodex_sessions_id"]
    ):
        raise RodexSessionError(
            "Rodex sessions log unique index is missing: "
            f"{RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX}"
        )


def _verify_tmux_sessions_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("tmux_server_socket_path", "TEXT", 1, 0),
            ("tmux_session_name", "TEXT", 1, 0),
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )


def _verify_tmux_sessions_unique_indexes(connection: sqlite3.Connection) -> None:
    _verify_unique_index(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        RODEX_TMUX_SESSIONS_SESSION_UNIQUE_INDEX,
        ["rodex_sessions_id"],
    )
    _verify_unique_index(
        connection,
        RODEX_TMUX_SESSIONS_TABLE,
        RODEX_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX,
        ["tmux_server_socket_path", "tmux_session_name"],
    )


def _verify_runtime_instances_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_RUNTIME_INSTANCES_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("runtime_id_signed_bigint", "BIGINT", 1, 0),
            ("started_at_utc", "TEXT", 1, 0),
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_RUNTIME_INSTANCES_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )


def _verify_model_names_table(connection: sqlite3.Connection) -> None:
    _verify_lookup_name_table(
        connection,
        MODEL_NAMES_TABLE,
        "name_of_the_model",
    )


def _verify_reasoning_effort_names_table(connection: sqlite3.Connection) -> None:
    _verify_lookup_name_table(
        connection,
        REASONING_EFFORT_NAMES_TABLE,
        "name_of_the_reasoning_effort",
    )


def _verify_lookup_name_table(
    connection: sqlite3.Connection,
    table_name: str,
    name_column: str,
) -> None:
    _verify_table_columns(
        connection,
        table_name,
        [
            ("id", "INTEGER", 0, 1),
            (name_column, "TEXT", 1, 0),
        ],
    )
    _verify_table_definition_contains(
        connection,
        table_name,
        (
            "ID INTEGER PRIMARY KEY AUTOINCREMENT",
            f"LENGTH(TRIM({name_column.upper()})) > 0",
        ),
    )


def _verify_statistics_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("statistics_publication_sequence", "INTEGER", 1, 0),
            ("statistics_projection_schema_version", "TEXT", 1, 0),
            ("calculated_at_utc", "TEXT", 1, 0),
            ("coverage_state", "TEXT", 1, 0),
            *SESSION_STATISTICS_SCALARS.schema_columns,
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_SESSIONS_STATISTICS_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_STATISTICS_TABLE,
        (
            "CHECK ( STATISTICS_PUBLICATION_SEQUENCE >= 1 )",
            "CHECK (COVERAGE_STATE IN ('COMPLETE', 'GAPPED'))",
            "TURNS_STARTED_COUNT = TURNS_COMPLETED_COUNT + TURNS_ABORTED_COUNT "
            "+ TURNS_OPEN_COUNT",
            "CHECK (CACHED_INPUT_TOKENS <= INPUT_TOKENS)",
        ),
    )


def _verify_statistics_distributions_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("included_statistics_publication_sequence", "INTEGER", 1, 0),
            ("distribution_kind", "TEXT", 1, 0),
            ("observation_count", "INTEGER", 1, 0),
            ("total", "INTEGER", 1, 0),
            ("median", "REAL", 0, 0),
            ("p75", "INTEGER", 0, 0),
            ("p90", "INTEGER", 0, 0),
            ("p95", "INTEGER", 0, 0),
            ("maximum", "INTEGER", 0, 0),
        ],
    )
    _verify_statistics_publication_sequence_foreign_key(
        connection, RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE
    )


def _verify_statistics_named_counts_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("included_statistics_publication_sequence", "INTEGER", 1, 0),
            ("count_kind", "TEXT", 1, 0),
            ("count_name", "TEXT", 1, 0),
            ("occurrence_count", "INTEGER", 1, 0),
        ],
    )
    _verify_statistics_publication_sequence_foreign_key(
        connection, RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE
    )


def _verify_statistics_audit_limits_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("included_statistics_publication_sequence", "INTEGER", 1, 0),
            ("limit_ordinal", "INTEGER", 1, 0),
            ("limitation", "TEXT", 1, 0),
        ],
    )
    _verify_statistics_publication_sequence_foreign_key(
        connection, RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE
    )


def _verify_statistics_publication_sequence_foreign_key(
    connection: sqlite3.Connection, table_name: str
) -> None:
    foreign_keys = connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
    observed = {(row[2], row[3], row[4]) for row in foreign_keys}
    expected = {
        (
            RODEX_SESSIONS_STATISTICS_TABLE,
            "rodex_sessions_id",
            "rodex_sessions_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TABLE,
            "included_statistics_publication_sequence",
            "statistics_publication_sequence",
        ),
    }
    if observed != expected:
        raise RodexSessionError(f"{table_name} foreign keys mismatch: {observed!r}")
    _verify_table_definition_contains(
        connection, table_name, ("DEFERRABLE INITIALLY DEFERRED",)
    )


def _verify_statistics_sources_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("codex_thread_id_signed_bigint_1", "BIGINT", 1, 0),
            ("codex_thread_id_signed_bigint_2", "BIGINT", 1, 0),
            ("parent_rodex_sessions_statistics_sources_id", "INTEGER", 0, 0),
            ("agent_path", "TEXT", 0, 0),
            ("agent_nickname", "TEXT", 0, 0),
            ("subagent_history_start_ordinal", "INTEGER", 0, 0),
            ("first_linked_at_utc", "TEXT", 1, 0),
            ("rollout_file_path", "TEXT", 0, 0),
            ("analyzed_size_bytes", "INTEGER", 0, 0),
            ("analyzed_mtime_ns", "INTEGER", 0, 0),
            ("analyzed_prefix_sha256", "TEXT", 0, 0),
            ("verified_at_utc", "TEXT", 0, 0),
            ("included_statistics_publication_sequence", "INTEGER", 0, 0),
        ],
    )
    foreign_keys = connection.execute(
        f"PRAGMA foreign_key_list({RODEX_SESSIONS_STATISTICS_SOURCES_TABLE})"
    ).fetchall()
    observed_foreign_keys = {(row[2], row[3], row[4]) for row in foreign_keys}
    expected_foreign_keys = {
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
        (
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            "rodex_sessions_id",
            "rodex_sessions_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            "parent_rodex_sessions_statistics_sources_id",
            "id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TABLE,
            "rodex_sessions_id",
            "rodex_sessions_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TABLE,
            "included_statistics_publication_sequence",
            "statistics_publication_sequence",
        ),
    }
    if observed_foreign_keys != expected_foreign_keys:
        raise RodexSessionError(
            f"{RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} foreign keys mismatch: "
            f"{observed_foreign_keys!r}"
        )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
        (
            "TYPEOF(CODEX_THREAD_ID_SIGNED_BIGINT_1) = 'INTEGER'",
            "TYPEOF(CODEX_THREAD_ID_SIGNED_BIGINT_2) = 'INTEGER'",
            "ANALYZED_SIZE_BYTES IS NULL OR ANALYZED_SIZE_BYTES >= 0",
            "ANALYZED_MTIME_NS IS NULL OR ANALYZED_MTIME_NS >= 0",
            "INCLUDED_STATISTICS_PUBLICATION_SEQUENCE IS NULL "
            "OR INCLUDED_STATISTICS_PUBLICATION_SEQUENCE >= 1",
            "INCLUDED_STATISTICS_PUBLICATION_SEQUENCE IS NULL "
            "OR ROLLOUT_FILE_PATH IS NOT NULL",
            "DEFERRABLE INITIALLY DEFERRED",
        ),
    )


def _verify_statistics_turns_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_statistics_sources_id", "INTEGER", 1, 0),
            ("codex_turn_id_sha256_int_1", "BIGINT", 1, 0),
            ("codex_turn_id_sha256_int_2", "BIGINT", 1, 0),
            ("codex_turn_id_sha256_int_3", "BIGINT", 1, 0),
            ("codex_turn_id_sha256_int_4", "BIGINT", 1, 0),
            ("codex_turn_id", "TEXT", 1, 0),
            ("included_statistics_publication_sequence", "INTEGER", 1, 0),
            ("started_at_utc", "TEXT", 0, 0),
            ("terminal_at_utc", "TEXT", 0, 0),
            ("outcome", "TEXT", 1, 0),
            ("model_names_id", "INTEGER", 0, 0),
            ("reasoning_effort_names_id", "INTEGER", 0, 0),
            *TURN_STATISTICS_SCALARS.schema_columns,
        ],
    )
    foreign_keys = connection.execute(
        f"PRAGMA foreign_key_list({RODEX_SESSIONS_STATISTICS_TURNS_TABLE})"
    ).fetchall()
    observed_foreign_keys = {(row[2], row[3], row[4]) for row in foreign_keys}
    expected_foreign_keys = {
        (
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            "rodex_sessions_id",
            "rodex_sessions_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            "rodex_sessions_statistics_sources_id",
            "id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            "included_statistics_publication_sequence",
            "included_statistics_publication_sequence",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TABLE,
            "rodex_sessions_id",
            "rodex_sessions_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TABLE,
            "included_statistics_publication_sequence",
            "statistics_publication_sequence",
        ),
        (MODEL_NAMES_TABLE, "model_names_id", "id"),
        (
            REASONING_EFFORT_NAMES_TABLE,
            "reasoning_effort_names_id",
            "id",
        ),
    }
    if observed_foreign_keys != expected_foreign_keys:
        raise RodexSessionError(
            f"{RODEX_SESSIONS_STATISTICS_TURNS_TABLE} foreign keys mismatch: "
            f"{observed_foreign_keys!r}"
        )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
        (
            "TYPEOF(CODEX_TURN_ID_SHA256_INT_1) = 'INTEGER'",
            "TYPEOF(CODEX_TURN_ID_SHA256_INT_2) = 'INTEGER'",
            "TYPEOF(CODEX_TURN_ID_SHA256_INT_3) = 'INTEGER'",
            "TYPEOF(CODEX_TURN_ID_SHA256_INT_4) = 'INTEGER'",
            "CHECK ( INCLUDED_STATISTICS_PUBLICATION_SEQUENCE >= 1 )",
            "OUTCOME IN ('OPEN', 'COMPLETED', 'ABORTED')",
            "OUTCOME != 'OPEN' OR TERMINAL_AT_UTC IS NULL",
            "HANDS_ON IN (0, 1)",
            "EDITED_THEN_VERIFIED IN (0, 1)",
            "DEFERRABLE INITIALLY DEFERRED",
        ),
    )


def _verify_statistics_subagent_spawns_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            (
                "subagent_rodex_sessions_statistics_sources_id",
                "INTEGER",
                1,
                0,
            ),
            ("parent_rodex_sessions_statistics_sources_id", "INTEGER", 1, 0),
            ("spawning_rodex_sessions_statistics_turns_id", "INTEGER", 1, 0),
            ("included_statistics_publication_sequence", "INTEGER", 1, 0),
        ],
    )
    foreign_keys = connection.execute(
        f"PRAGMA foreign_key_list({RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE})"
    ).fetchall()
    observed_foreign_keys = {(row[2], row[3], row[4]) for row in foreign_keys}
    expected_foreign_keys = {
        (
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            "rodex_sessions_id",
            "rodex_sessions_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            "subagent_rodex_sessions_statistics_sources_id",
            "id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            "parent_rodex_sessions_statistics_sources_id",
            "parent_rodex_sessions_statistics_sources_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            "included_statistics_publication_sequence",
            "included_statistics_publication_sequence",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            "rodex_sessions_id",
            "rodex_sessions_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            "parent_rodex_sessions_statistics_sources_id",
            "rodex_sessions_statistics_sources_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            "spawning_rodex_sessions_statistics_turns_id",
            "id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            "included_statistics_publication_sequence",
            "included_statistics_publication_sequence",
        ),
    }
    if observed_foreign_keys != expected_foreign_keys:
        raise RodexSessionError(
            f"{RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE} foreign keys "
            f"mismatch: {observed_foreign_keys!r}"
        )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_STATISTICS_SUBAGENT_SPAWNS_TABLE,
        (
            "CHECK ( INCLUDED_STATISTICS_PUBLICATION_SEQUENCE >= 1 )",
            "SUBAGENT_RODEX_SESSIONS_STATISTICS_SOURCES_ID "
            "!= PARENT_RODEX_SESSIONS_STATISTICS_SOURCES_ID",
            "DEFERRABLE INITIALLY DEFERRED",
        ),
    )


def _verify_statistics_turn_named_counts_table(
    connection: sqlite3.Connection,
) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("rodex_sessions_statistics_turns_id", "INTEGER", 1, 0),
            ("included_statistics_publication_sequence", "INTEGER", 1, 0),
            ("count_kind", "TEXT", 1, 0),
            ("count_name", "TEXT", 1, 0),
            ("occurrence_count", "INTEGER", 1, 0),
        ],
    )
    foreign_keys = connection.execute(
        f"PRAGMA foreign_key_list({RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE})"
    ).fetchall()
    observed = {(row[2], row[3], row[4]) for row in foreign_keys}
    expected = {
        (
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            "rodex_sessions_id",
            "rodex_sessions_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            "rodex_sessions_statistics_turns_id",
            "id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            "included_statistics_publication_sequence",
            "included_statistics_publication_sequence",
        ),
    }
    if observed != expected:
        raise RodexSessionError(
            f"{RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} foreign keys "
            f"mismatch: {observed!r}"
        )


def _verify_statistics_workers_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_WORKERS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("worker_state", "TEXT", 1, 0),
            ("diagnostic_code", "TEXT", 0, 0),
            ("last_attempted_at_utc", "TEXT", 1, 0),
            ("consecutive_failures", "INTEGER", 1, 0),
            ("next_retry_at_utc", "TEXT", 0, 0),
        ],
    )
    _verify_single_foreign_key(
        connection,
        RODEX_SESSIONS_STATISTICS_WORKERS_TABLE,
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
    )
    _verify_table_definition_contains(
        connection,
        RODEX_SESSIONS_STATISTICS_WORKERS_TABLE,
        (
            "WORKER_STATE IN ('STARTING', 'CATCHING_UP', 'UP_TO_DATE', "
            "'DEGRADED', 'STOPPED')",
            "CHECK (CONSECUTIVE_FAILURES >= 0)",
            "WORKER_STATE != 'UP_TO_DATE' OR (DIAGNOSTIC_CODE IS NULL AND "
            "CONSECUTIVE_FAILURES = 0 AND NEXT_RETRY_AT_UTC IS NULL)",
            "LENGTH(DIAGNOSTIC_CODE) BETWEEN 1 AND 64",
            "DIAGNOSTIC_CODE NOT GLOB '*[^A-Z0-9_]*'",
        ),
    )


def _verify_table_columns(
    connection: sqlite3.Connection,
    table_name: str,
    expected: list[tuple[str, str, int, int]],
) -> None:
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    if observed != expected:
        raise RodexSessionError(f"{table_name} schema mismatch: {observed!r}")
    definition_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    definition = " ".join(str(definition_row[0]).upper().split())
    if "ID INTEGER PRIMARY KEY AUTOINCREMENT" not in definition:
        raise RodexSessionError(f"{table_name} id must use AUTOINCREMENT")


def _verify_single_foreign_key(
    connection: sqlite3.Connection,
    table_name: str,
    expected: tuple[str, str, str],
) -> None:
    foreign_keys = connection.execute(f"PRAGMA foreign_key_list({table_name})").fetchall()
    observed = {(row[2], row[3], row[4]) for row in foreign_keys}
    if observed != {expected}:
        raise RodexSessionError(f"{table_name} foreign keys mismatch: {observed!r}")


def _verify_unique_index(
    connection: sqlite3.Connection,
    table_name: str,
    index_name: str,
    expected_columns: list[str],
) -> None:
    indexes = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    matching = [row for row in indexes if row[1] == index_name]
    columns = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
    if (
        len(matching) != 1
        or matching[0][2] != 1
        or [row[2] for row in columns] != expected_columns
    ):
        raise RodexSessionError(f"unique index is missing: {index_name}")


def _verify_index(
    connection: sqlite3.Connection,
    table_name: str,
    index_name: str,
    expected_columns: list[str],
    *,
    unique: bool,
) -> None:
    indexes = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    matching = [row for row in indexes if row[1] == index_name]
    columns = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
    if (
        len(matching) != 1
        or bool(matching[0][2]) is not unique
        or [row[2] for row in columns] != expected_columns
    ):
        raise RodexSessionError(f"index is missing: {index_name}")


def _verify_table_definition_contains(
    connection: sqlite3.Connection,
    table_name: str,
    expected_fragments: Sequence[str],
) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    definition = " ".join(str(row[0]).upper().split())
    missing = [fragment for fragment in expected_fragments if fragment not in definition]
    if missing:
        raise RodexSessionError(f"{table_name} constraints are missing: {missing!r}")
