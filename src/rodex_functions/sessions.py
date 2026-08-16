"""Authoritative creation and lookup pipeline for Rodex session identities."""

from __future__ import annotations

import hashlib
import os
import pwd
import secrets
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from cool_name.functions import (
    allocate_unique_cool_name,
    create_and_verify_cool_names_schema,
    lookup_cool_name,
    normalise_rodex_display_name,
    reserve_specific_cool_name,
)
from rodex_sql import default_rodex_database_path as _default_rodex_database_path
from rodex_sql import (
    normalise_rodex_database_path,
    open_rodex_read_transaction,
    open_rodex_transaction,
    select_lookup_id,
    select_or_insert_lookup_id,
)

from .statistics_projection import (
    SessionStatisticsProjection,
    StatisticsDistribution,
    StatisticsNamedCount,
    TurnStatisticsProjection,
    validate_session_statistics_projection,
)

RODEX_SESSIONS_TABLE: Final = "rodex_sessions"
RODEX_UUID_UNIQUE_INDEX: Final = "rodex_sessions_uuid_ints_unique"
RODEX_SESSIONS_USERS_TABLE: Final = "rodex_sessions_users"
RODEX_SESSIONS_USERS_UNIQUE_INDEX: Final = "rodex_sessions_users_uid_gid_user_name_unique"
RODEX_SESSIONS_LOG_TABLE: Final = "rodex_sessions_log"
RODEX_SESSIONS_LOG_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_log_rodex_sessions_id_unique"
)
RODEX_CODEX_UUID_UNIQUE_INDEX: Final = "rodex_sessions_codex_session_uuid_ints_unique"
RODEX_TMUX_SESSIONS_TABLE: Final = "rodex_tmux_sessions"
RODEX_TMUX_SESSIONS_SESSION_UNIQUE_INDEX: Final = (
    "rodex_tmux_sessions_rodex_sessions_id_unique"
)
RODEX_TMUX_SESSIONS_ENDPOINT_UNIQUE_INDEX: Final = "rodex_tmux_sessions_endpoint_unique"
RODEX_SESSIONS_STATISTICS_TABLE: Final = "rodex_sessions_statistics"
RODEX_SESSIONS_STATISTICS_SESSION_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_rodex_sessions_id_unique"
)
RODEX_SESSIONS_STATISTICS_SESSION_REVISION_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_session_revision_unique"
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
    "rodex_sessions_statistics_sources_codex_uuid_unique"
)
RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_INDEX: Final = (
    "rodex_sessions_statistics_sources_session"
)
RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_ID_REVISION_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_sources_session_id_revision_unique"
)
RODEX_SESSIONS_STATISTICS_TURNS_TABLE: Final = "rodex_sessions_statistics_turns"
RODEX_SESSIONS_STATISTICS_TURNS_SOURCE_TURN_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_turns_source_turn_unique"
)
RODEX_SESSIONS_STATISTICS_TURNS_SESSION_TURN_INDEX: Final = (
    "rodex_sessions_statistics_turns_session_turn"
)
RODEX_SESSIONS_STATISTICS_TURNS_SESSION_ID_REVISION_UNIQUE_INDEX: Final = (
    "rodex_sessions_statistics_turns_session_id_revision_unique"
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
MAX_UUID_GENERATION_ATTEMPTS: Final = 8
STATISTICS_COVERAGE_STATES: Final = frozenset({"complete", "gapped"})
STATISTICS_TURN_OUTCOMES: Final = frozenset({"open", "completed", "aborted"})
STATISTICS_WORKER_STATES: Final = frozenset(
    {"starting", "catching_up", "up_to_date", "degraded", "stopped"}
)
_SESSION_PROJECTION_SCALAR_COLUMNS: Final = (
    "analyzer_event_count",
    "analyzer_source_count",
    "history_sessions_count",
    "history_records_count",
    "history_malformed_records_count",
    "turns_started_count",
    "turns_completed_count",
    "turns_aborted_count",
    "turns_open_count",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "context_observation_count",
    "context_latest_session_median_percent",
    "context_high_water_percent",
    "commands_executed_count",
    "model_tool_requests_count",
    "model_tool_outputs_paired_count",
    "file_change_operations_count",
    "file_change_distinct_paths_count",
    "file_change_occurrences_count",
    "web_operations_count",
    "web_queries_count",
    "web_result_records_count",
    "web_distinct_result_or_action_urls_count",
    "collaboration_operations_count",
    "collaboration_agents_started_count",
    "compactions_count",
    "distinct_workspaces_count",
    "typical_turns_count",
    "hands_on_turn_count",
    "hands_on_turn_rate_percent",
    "turns_with_nonzero_command_count",
    "turns_subsequently_completed_count",
    "completed_after_nonzero_command_percent",
    "command_zero_exit_rate_percent",
    "exact_command_repeat_rate_percent",
    "repeated_command_execution_count",
    "cached_input_share_percent",
    "reasoning_output_share_percent",
    "edited_turns_count",
    "verified_after_edit_count",
    "edit_then_verify_percent",
    "web_turns_count",
    "web_later_command_or_file_work_count",
    "web_follow_through_percent",
    "file_revisit_rate_percent",
    "revisited_distinct_path_count",
    "busiest_workspace_turn_share_percent",
    "workspace_tagged_turn_count",
    "turns_in_busiest_workspace_count",
    "turns_with_local_hour_count",
    "busiest_local_hour",
    "turns_in_busiest_local_hour_count",
    "goal_updates_count",
    "audit_privacy",
    "audit_percentile_method",
    "audit_token_method",
    "audit_token_snapshots_count",
    "audit_repeated_token_snapshots_count",
    "audit_token_epochs_count",
    "audit_duplicate_operations_ignored_count",
    "audit_duplicate_terminals_ignored_count",
    "audit_terminal_events_without_start_ignored_count",
    "audit_new_event_type_warnings_count",
)
_TURN_PROJECTION_SCALAR_COLUMNS: Final = (
    "duration_ms",
    "time_to_first_token_ms",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
    "context_observation_count",
    "context_high_water_percent",
    "commands_executed_count",
    "model_tool_requests_count",
    "model_tool_outputs_paired_count",
    "file_change_operations_count",
    "file_change_distinct_paths_count",
    "file_change_occurrences_count",
    "web_operations_count",
    "web_queries_count",
    "web_result_records_count",
    "web_distinct_result_or_action_urls_count",
    "collaboration_operations_count",
    "collaboration_agents_started_count",
    "compactions_count",
    "workspace_digest",
    "model",
    "local_start_hour",
    "hands_on",
    "completed_after_nonzero_command",
    "cached_input_share_percent",
    "reasoning_output_share_percent",
    "edited_then_verified",
    "web_research_followed_by_command_or_file_work",
    "goal_updates_count",
)
_TURN_DATABASE_SCALAR_COLUMNS: Final = (
    *_TURN_PROJECTION_SCALAR_COLUMNS[:11],
    "command_duration_observation_count",
    "command_duration_total_ms",
    "command_duration_median_ms",
    "command_duration_p75_ms",
    "command_duration_p90_ms",
    "command_duration_p95_ms",
    "command_duration_maximum_ms",
    *_TURN_PROJECTION_SCALAR_COLUMNS[11:],
)

_HALF_BITS: Final = 64
_HALF_MODULUS: Final = 1 << _HALF_BITS
_HALF_SIGN_BIT: Final = 1 << (_HALF_BITS - 1)
_SIGNED_BIGINT_MIN: Final = -_HALF_SIGN_BIT
_SIGNED_BIGINT_MAX: Final = _HALF_SIGN_BIT - 1

_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid_int_1 BIGINT NOT NULL,
    uuid_int_2 BIGINT NOT NULL,
    codex_session_uuid_int_1 BIGINT NOT NULL,
    codex_session_uuid_int_2 BIGINT NOT NULL,
    cool_names_id INTEGER NOT NULL,
    user_defined_cool_names_id INTEGER DEFAULT NULL,
    FOREIGN KEY (cool_names_id) REFERENCES cool_names (id),
    FOREIGN KEY (user_defined_cool_names_id) REFERENCES cool_names (id)
)
"""
_CREATE_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_UUID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE} (uuid_int_1, uuid_int_2)
"""
_CREATE_CODEX_UUID_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_CODEX_UUID_UNIQUE_INDEX}
ON {RODEX_SESSIONS_TABLE}
    (codex_session_uuid_int_1, codex_session_uuid_int_2)
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
_CREATE_STATISTICS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    statistics_revision INTEGER NOT NULL CHECK (statistics_revision >= 1),
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
    collaboration_operations_count INTEGER NOT NULL CHECK (
        collaboration_operations_count >= 0
    ),
    collaboration_agents_started_count INTEGER NOT NULL CHECK (
        collaboration_agents_started_count >= 0
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
    exact_command_repeat_rate_percent REAL DEFAULT NULL CHECK (
        exact_command_repeat_rate_percent IS NULL
        OR exact_command_repeat_rate_percent BETWEEN 0 AND 100
    ),
    repeated_command_execution_count INTEGER NOT NULL CHECK (
        repeated_command_execution_count >= 0
        AND repeated_command_execution_count <= commands_executed_count
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
    file_revisit_rate_percent REAL DEFAULT NULL CHECK (
        file_revisit_rate_percent IS NULL OR file_revisit_rate_percent BETWEEN 0 AND 100
    ),
    revisited_distinct_path_count INTEGER NOT NULL CHECK (
        revisited_distinct_path_count >= 0
        AND revisited_distinct_path_count <= file_change_distinct_paths_count
    ),
    busiest_workspace_turn_share_percent REAL DEFAULT NULL CHECK (
        busiest_workspace_turn_share_percent IS NULL
        OR busiest_workspace_turn_share_percent BETWEEN 0 AND 100
    ),
    workspace_tagged_turn_count INTEGER NOT NULL CHECK (workspace_tagged_turn_count >= 0),
    turns_in_busiest_workspace_count INTEGER NOT NULL CHECK (
        turns_in_busiest_workspace_count >= 0
        AND turns_in_busiest_workspace_count <= workspace_tagged_turn_count
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
_CREATE_STATISTICS_SESSION_REVISION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_SESSION_REVISION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TABLE} (rodex_sessions_id, statistics_revision)
"""
_CREATE_STATISTICS_DISTRIBUTIONS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    included_statistics_revision INTEGER NOT NULL CHECK (
        included_statistics_revision >= 1
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
    FOREIGN KEY (rodex_sessions_id, included_statistics_revision)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TABLE}
            (rodex_sessions_id, statistics_revision)
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
    included_statistics_revision INTEGER NOT NULL CHECK (
        included_statistics_revision >= 1
    ),
    count_kind TEXT NOT NULL CHECK (count_kind IN (
        'command_exit_status', 'command_family', 'model_tool', 'file_change_type',
        'web_action', 'collaboration_tool', 'model', 'goal_status'
    )),
    count_name TEXT NOT NULL CHECK (length(count_name) > 0),
    occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
    FOREIGN KEY (rodex_sessions_id, included_statistics_revision)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TABLE}
            (rodex_sessions_id, statistics_revision)
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
    included_statistics_revision INTEGER NOT NULL CHECK (
        included_statistics_revision >= 1
    ),
    limit_ordinal INTEGER NOT NULL CHECK (limit_ordinal >= 0),
    limitation TEXT NOT NULL CHECK (length(limitation) > 0),
    FOREIGN KEY (rodex_sessions_id, included_statistics_revision)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TABLE}
            (rodex_sessions_id, statistics_revision)
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
    codex_session_uuid_int_1 BIGINT NOT NULL,
    codex_session_uuid_int_2 BIGINT NOT NULL,
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
    included_statistics_revision INTEGER DEFAULT NULL CHECK (
        included_statistics_revision IS NULL OR included_statistics_revision >= 1
    ),
    CHECK (
        included_statistics_revision IS NULL OR rollout_file_path IS NOT NULL
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
    FOREIGN KEY (rodex_sessions_id) REFERENCES {RODEX_SESSIONS_TABLE} (id),
    FOREIGN KEY (rodex_sessions_id, included_statistics_revision)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TABLE}
            (rodex_sessions_id, statistics_revision)
        DEFERRABLE INITIALLY DEFERRED
)
"""
_CREATE_STATISTICS_SOURCES_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_SOURCES_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE}
    (codex_session_uuid_int_1, codex_session_uuid_int_2)
"""
_CREATE_STATISTICS_SOURCES_SESSION_INDEX = f"""
CREATE INDEX IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_INDEX}
ON {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} (rodex_sessions_id)
"""
_CREATE_STATISTICS_SOURCES_SESSION_ID_REVISION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_ID_REVISION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE}
    (rodex_sessions_id, id, included_statistics_revision)
"""
_CREATE_STATISTICS_TURNS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_statistics_sources_id INTEGER NOT NULL,
    codex_turn_id_sha256_int_1 BIGINT NOT NULL,
    codex_turn_id_sha256_int_2 BIGINT NOT NULL,
    codex_turn_id_sha256_int_3 BIGINT NOT NULL,
    codex_turn_id_sha256_int_4 BIGINT NOT NULL,
    codex_turn_id TEXT NOT NULL,
    included_statistics_revision INTEGER NOT NULL CHECK (
        included_statistics_revision >= 1
    ),
    started_at_utc TEXT DEFAULT NULL,
    terminal_at_utc TEXT DEFAULT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('open', 'completed', 'aborted')),
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
    collaboration_operations_count INTEGER NOT NULL CHECK (
        collaboration_operations_count >= 0
    ),
    collaboration_agents_started_count INTEGER NOT NULL CHECK (
        collaboration_agents_started_count >= 0
    ),
    compactions_count INTEGER NOT NULL CHECK (compactions_count >= 0),
    workspace_digest TEXT DEFAULT NULL,
    model TEXT DEFAULT NULL,
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
        included_statistics_revision)
        REFERENCES {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE}
            (rodex_sessions_id, id, included_statistics_revision)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (rodex_sessions_id, included_statistics_revision)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TABLE}
            (rodex_sessions_id, statistics_revision)
        DEFERRABLE INITIALLY DEFERRED
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
_CREATE_STATISTICS_TURNS_SESSION_ID_REVISION_UNIQUE_INDEX = f"""
CREATE UNIQUE INDEX IF NOT EXISTS
    {RODEX_SESSIONS_STATISTICS_TURNS_SESSION_ID_REVISION_UNIQUE_INDEX}
ON {RODEX_SESSIONS_STATISTICS_TURNS_TABLE}
    (rodex_sessions_id, id, included_statistics_revision)
"""
_CREATE_STATISTICS_TURN_NAMED_COUNTS_TABLE = f"""
CREATE TABLE IF NOT EXISTS {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rodex_sessions_id INTEGER NOT NULL,
    rodex_sessions_statistics_turns_id INTEGER NOT NULL,
    included_statistics_revision INTEGER NOT NULL CHECK (
        included_statistics_revision >= 1
    ),
    count_kind TEXT NOT NULL CHECK (count_kind IN (
        'command_exit_status', 'command_family', 'model_tool', 'file_change_type',
        'web_action', 'collaboration_tool', 'goal_status'
    )),
    count_name TEXT NOT NULL CHECK (length(count_name) > 0),
    occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
    FOREIGN KEY (rodex_sessions_id, rodex_sessions_statistics_turns_id,
        included_statistics_revision)
        REFERENCES {RODEX_SESSIONS_STATISTICS_TURNS_TABLE}
            (rodex_sessions_id, id, included_statistics_revision)
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


class RodexSessionError(RuntimeError):
    """The Rodex session registry could not satisfy its contract."""


class RodexSessionUUIDCollisionError(RodexSessionError):
    """Repeated secure UUID candidates collided with existing sessions."""


class RodexSessionStatisticsConflictError(RodexSessionError):
    """A statistics publication lost its identity or revision fence."""


class RodexSessionTurnStatisticsAmbiguousError(RodexSessionError):
    """One unqualified turn ID exists in multiple Codex lineage sources."""


@dataclass(frozen=True, slots=True)
class RodexSession:
    """The public identity allocated to one Rodex launch."""

    id: int
    rodex_uuid: uuid.UUID
    codex_session_uuid: uuid.UUID
    cool_names_id: int
    cool_name: str

    @property
    def uuid_int_1(self) -> int:
        """Return the unsigned high 64 bits of the public UUID."""
        return self.rodex_uuid.int >> _HALF_BITS

    @property
    def uuid_int_2(self) -> int:
        """Return the unsigned low 64 bits of the public UUID."""
        return self.rodex_uuid.int & (_HALF_MODULUS - 1)


@dataclass(frozen=True, slots=True)
class RodexSessionsUserIdentity:
    """A POSIX user's natural lookup key."""

    uid: int
    gid: int
    user_name: str


@dataclass(frozen=True, slots=True)
class RodexSessionsUser:
    """A normalized POSIX user lookup row."""

    id: int
    uid: int
    gid: int
    user_name: str


@dataclass(frozen=True, slots=True)
class RodexSessionLog:
    """Creation provenance and most recent access for one Rodex session."""

    id: int
    rodex_sessions_id: int
    created_at_utc: str
    rodex_sessions_users_id: int
    last_accessed_at_utc: str


@dataclass(frozen=True, slots=True)
class RodexTmuxSession:
    """The tmux endpoint linked one-to-one with a Rodex session."""

    id: int
    rodex_sessions_id: int
    tmux_server_socket_path: str
    tmux_session_name: str


@dataclass(frozen=True, slots=True)
class RodexSessionStatistics:
    """Latest fully relational analyzer projection for one Rodex session."""

    id: int
    rodex_sessions_id: int
    statistics_revision: int
    statistics_projection_schema_version: str
    calculated_at_utc: str
    coverage_state: str
    projection: SessionStatisticsProjection


@dataclass(frozen=True, slots=True)
class RodexSessionStatisticsSource:
    """One Codex lineage source and its latest analyzed prefix provenance."""

    id: int
    rodex_sessions_id: int
    codex_session_uuid: uuid.UUID
    first_linked_at_utc: str
    rollout_file_path: str | None
    analyzed_size_bytes: int | None
    analyzed_mtime_ns: int | None
    analyzed_prefix_sha256: str | None
    verified_at_utc: str | None
    included_statistics_revision: int | None


@dataclass(frozen=True, slots=True)
class RodexSessionStatisticsSourceObservation:
    """Exact bytes and filesystem state used for one aggregate calculation."""

    codex_session_uuid: uuid.UUID
    rollout_file_path: Path
    analyzed_size_bytes: int
    analyzed_mtime_ns: int
    analyzed_prefix_sha256: str
    verified_at_utc: str


@dataclass(frozen=True, slots=True)
class RodexSessionTurnStatistics:
    """Latest persisted statistics projection for one exact Codex turn."""

    id: int
    rodex_sessions_id: int
    rodex_sessions_statistics_sources_id: int
    included_statistics_revision: int
    projection: TurnStatisticsProjection

    @property
    def codex_session_uuid(self) -> uuid.UUID:
        return self.projection.codex_session_uuid

    @property
    def codex_turn_id(self) -> str:
        return self.projection.codex_turn_id

    @property
    def started_at_utc(self) -> str | None:
        return self.projection.started_at_utc

    @property
    def terminal_at_utc(self) -> str | None:
        return self.projection.terminal_at_utc

    @property
    def outcome(self) -> str:
        return self.projection.outcome


@dataclass(frozen=True, slots=True)
class RodexSessionStatisticsWorker:
    """Independent health of one fail-open analytics worker."""

    id: int
    rodex_sessions_id: int
    worker_state: str
    diagnostic_code: str | None
    last_attempted_at_utc: str
    consecutive_failures: int
    next_retry_at_utc: str | None


@dataclass(frozen=True, slots=True)
class RodexSessionStatisticsView:
    """One transactionally consistent statistics, health, and provenance read."""

    statistics: RodexSessionStatistics | None
    worker: RodexSessionStatisticsWorker | None
    sources: tuple[RodexSessionStatisticsSource, ...]


@dataclass(frozen=True, slots=True)
class RodexSessionTurnStatisticsView:
    """One transactionally consistent exact-turn and parent statistics read."""

    statistics: RodexSessionStatistics | None
    worker: RodexSessionStatisticsWorker | None
    sources: tuple[RodexSessionStatisticsSource, ...]
    turn: RodexSessionTurnStatistics | None


@dataclass(frozen=True, slots=True)
class RodexSessionNames:
    """The permanent and optional user-defined names for one session."""

    rodex_sessions_id: int
    cool_name: str
    user_defined_cool_name: str | None

    @property
    def display_name(self) -> str:
        """Return the user-defined name when present, otherwise the generated name."""
        return self.user_defined_cool_name or self.cool_name


@dataclass(frozen=True, slots=True)
class RodexSessionRuntime:
    """The persisted identities needed to identify one user's live runtime."""

    rodex_sessions_id: int
    cool_name: str
    user_defined_cool_name: str | None
    codex_session_uuid: uuid.UUID
    tmux_server_socket_path: str
    tmux_session_name: str

    @property
    def display_name(self) -> str:
        """Return the effective user-facing name for this runtime."""
        return self.user_defined_cool_name or self.cool_name


@dataclass(slots=True)
class RodexUserDefinedCoolNameAssignment:
    """One serialized database/tmux name transition prepared for the CLI."""

    names: RodexSessionNames
    tmux_session: RodexTmuxSession | None
    renamed_tmux_session_name: str | None = None


def default_rodex_database_path() -> Path:
    """Resolve the current user's durable database path for compatibility."""
    return _default_rodex_database_path()


def initialise_rodex_database(database_path: str | os.PathLike[str] | None = None) -> Path:
    """Create and verify the current Rodex schema in one transaction."""
    path = normalise_rodex_database_path(database_path)
    with open_rodex_transaction(path) as connection:
        create_and_verify_cool_names_schema(connection)
        connection.execute(_CREATE_TABLE)
        _verify_sessions_table(connection)
        connection.execute(_CREATE_UNIQUE_INDEX)
        connection.execute(_CREATE_CODEX_UUID_UNIQUE_INDEX)
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
        connection.execute(_CREATE_STATISTICS_TABLE)
        _verify_statistics_table(connection)
        connection.execute(_CREATE_STATISTICS_SESSION_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TABLE,
            RODEX_SESSIONS_STATISTICS_SESSION_UNIQUE_INDEX,
            ["rodex_sessions_id"],
        )
        connection.execute(_CREATE_STATISTICS_SESSION_REVISION_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TABLE,
            RODEX_SESSIONS_STATISTICS_SESSION_REVISION_UNIQUE_INDEX,
            ["rodex_sessions_id", "statistics_revision"],
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
            ["codex_session_uuid_int_1", "codex_session_uuid_int_2"],
        )
        connection.execute(_CREATE_STATISTICS_SOURCES_SESSION_INDEX)
        _verify_index(
            connection,
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_INDEX,
            ["rodex_sessions_id"],
            unique=False,
        )
        connection.execute(_CREATE_STATISTICS_SOURCES_SESSION_ID_REVISION_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_SOURCES_TABLE,
            RODEX_SESSIONS_STATISTICS_SOURCES_SESSION_ID_REVISION_UNIQUE_INDEX,
            ["rodex_sessions_id", "id", "included_statistics_revision"],
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
        connection.execute(_CREATE_STATISTICS_TURNS_SESSION_ID_REVISION_UNIQUE_INDEX)
        _verify_unique_index(
            connection,
            RODEX_SESSIONS_STATISTICS_TURNS_TABLE,
            RODEX_SESSIONS_STATISTICS_TURNS_SESSION_ID_REVISION_UNIQUE_INDEX,
            ["rodex_sessions_id", "id", "included_statistics_revision"],
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
        _register_missing_statistics_sources(connection)
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


def create_a_rodex_session(
    database_path: str | os.PathLike[str] | None = None,
    *,
    codex_session_uuid: uuid.UUID | str,
    rodex_session_uuid: uuid.UUID | str | None = None,
    user_identity: RodexSessionsUserIdentity | None = None,
    tmux_server_socket_path: str | os.PathLike[str] | None = None,
    tmux_session_name: str | None = None,
) -> RodexSession:
    """Atomically persist a session and any live Codex/tmux linkage."""
    path = initialise_rodex_database(database_path)
    identity = (
        current_rodex_sessions_user_identity()
        if user_identity is None
        else _validate_user_identity(user_identity)
    )
    parsed_codex_session_uuid = _parse_uuid(codex_session_uuid, "codex_session_uuid")
    codex_uuid_int_1, codex_uuid_int_2 = split_a_codex_uuid_into_signed_bigints(
        parsed_codex_session_uuid
    )
    tmux_link = _normalise_tmux_link(tmux_server_socket_path, tmux_session_name)
    created_at_utc = _utc_now_timestamp()
    with open_rodex_transaction(path) as connection:
        rodex_sessions_users_id = _lookup_or_insert_rodex_sessions_user_id(
            connection, identity
        )
        existing_session_id = select_lookup_id(
            connection,
            RODEX_SESSIONS_TABLE,
            {
                "codex_session_uuid_int_1": codex_uuid_int_1,
                "codex_session_uuid_int_2": codex_uuid_int_2,
            },
        )
        if existing_session_id is not None:
            names_row = _select_rodex_session_names(connection, existing_session_id)
            if names_row is None:
                raise RodexSessionError(f"Rodex session disappeared: {existing_session_id}")
            display_name = _session_names_from_row(names_row).display_name
            raise RodexSessionError(
                f"Codex session already belongs to Rodex {display_name}.\n"
                f"Resume with: rodex {display_name}"
            )
        allocated_name = allocate_unique_cool_name(connection)
        preallocated = (
            None
            if rodex_session_uuid is None
            else _parse_uuid(rodex_session_uuid, "rodex_session_uuid")
        )
        candidates = (
            (preallocated,)
            if preallocated is not None
            else (
                uuid.UUID(int=secrets.randbits(128))
                for _ in range(MAX_UUID_GENERATION_ATTEMPTS)
            )
        )
        for rodex_uuid in candidates:
            uuid_int_1, uuid_int_2 = split_a_rodex_uuid_into_signed_bigints(rodex_uuid)
            try:
                cursor = connection.execute(
                    f"INSERT INTO {RODEX_SESSIONS_TABLE} "
                    "(uuid_int_1, uuid_int_2, codex_session_uuid_int_1, "
                    "codex_session_uuid_int_2, cool_names_id) VALUES (?, ?, ?, ?, ?)",
                    (
                        uuid_int_1,
                        uuid_int_2,
                        codex_uuid_int_1,
                        codex_uuid_int_2,
                        allocated_name.id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                if preallocated is not None:
                    raise RodexSessionUUIDCollisionError(
                        f"preallocated Rodex UUID is already occupied: {preallocated}"
                    ) from error
                continue
            if cursor.lastrowid is None:
                raise RodexSessionError("SQLite did not return a Rodex session id")
            session = RodexSession(
                id=cursor.lastrowid,
                rodex_uuid=rodex_uuid,
                codex_session_uuid=parsed_codex_session_uuid,
                cool_names_id=allocated_name.id,
                cool_name=allocated_name.cool_name,
            )
            connection.execute(
                f"INSERT INTO {RODEX_SESSIONS_LOG_TABLE} "
                "(rodex_sessions_id, created_at_utc, rodex_sessions_users_id, "
                "last_accessed_at_utc) VALUES (?, ?, ?, ?)",
                (
                    session.id,
                    created_at_utc,
                    rodex_sessions_users_id,
                    created_at_utc,
                ),
            )
            _register_statistics_source(
                connection,
                session.id,
                parsed_codex_session_uuid,
                created_at_utc,
            )
            if tmux_link is not None:
                socket_path, session_name = tmux_link
                connection.execute(
                    f"INSERT INTO {RODEX_TMUX_SESSIONS_TABLE} "
                    "(rodex_sessions_id, tmux_server_socket_path, tmux_session_name) "
                    "VALUES (?, ?, ?)",
                    (session.id, socket_path, session_name),
                )
            return session
        raise RodexSessionUUIDCollisionError(
            "could not allocate a unique Rodex UUID after "
            f"{MAX_UUID_GENERATION_ATTEMPTS} attempts"
        )


def generate_an_unregistered_rodex_uuid_candidate(
    database_path: str | os.PathLike[str] | None = None,
) -> uuid.UUID:
    """Generate an unused but deliberately unreserved UUID for a pending launch."""
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        for _ in range(MAX_UUID_GENERATION_ATTEMPTS):
            candidate = uuid.UUID(int=secrets.randbits(128))
            uuid_int_1, uuid_int_2 = split_a_rodex_uuid_into_signed_bigints(candidate)
            row = connection.execute(
                f"SELECT 1 FROM {RODEX_SESSIONS_TABLE} "
                "WHERE uuid_int_1 = ? AND uuid_int_2 = ?",
                (uuid_int_1, uuid_int_2),
            ).fetchone()
            if row is None:
                return candidate
    raise RodexSessionUUIDCollisionError(
        "could not generate an unused Rodex UUID candidate after "
        f"{MAX_UUID_GENERATION_ATTEMPTS} attempts"
    )


def current_rodex_sessions_user_identity() -> RodexSessionsUserIdentity:
    """Read the current effective POSIX UID, GID, and account name."""
    if os.name == "nt" or not hasattr(os, "getuid") or not hasattr(os, "getgid"):
        raise RodexSessionError("Rodex requires Linux or a compatible POSIX system")
    uid = os.getuid()
    gid = os.getgid()
    return RodexSessionsUserIdentity(
        uid=uid,
        gid=gid,
        user_name=pwd.getpwuid(uid).pw_name,
    )


def lookup_or_create_rodex_sessions_user(
    uid: int,
    gid: int,
    user_name: str,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionsUser:
    """Select a user lookup row first, inserting it only when absent."""
    identity = _validate_user_identity(
        RodexSessionsUserIdentity(uid=uid, gid=gid, user_name=user_name)
    )
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        user_id = _lookup_or_insert_rodex_sessions_user_id(connection, identity)
    return RodexSessionsUser(
        id=user_id,
        uid=identity.uid,
        gid=identity.gid,
        user_name=identity.user_name,
    )


def lookup_rodex_sessions_user(
    user_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionsUser | None:
    """Return one normalized user by internal id, or ``None`` when absent."""
    _validate_positive_id(user_id, "user_id")
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        row = connection.execute(
            f"SELECT id, uid, gid, user_name FROM {RODEX_SESSIONS_USERS_TABLE} "
            "WHERE id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return RodexSessionsUser(
        id=int(row[0]), uid=int(row[1]), gid=int(row[2]), user_name=str(row[3])
    )


def lookup_id_from_a_rodex_uuid(
    rodex_uuid: uuid.UUID | str,
    database_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Return the internal integer id for a Rodex UUID, or ``None`` when absent."""
    path = initialise_rodex_database(database_path)
    uuid_int_1, uuid_int_2 = split_a_rodex_uuid_into_signed_bigints(rodex_uuid)
    with open_rodex_transaction(path) as connection:
        row = connection.execute(
            f"SELECT id FROM {RODEX_SESSIONS_TABLE} "
            "WHERE uuid_int_1 = ? AND uuid_int_2 = ?",
            (uuid_int_1, uuid_int_2),
        ).fetchone()
    return None if row is None else int(row[0])


def lookup_rodex_uuid_from_an_id(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> uuid.UUID | None:
    """Return the public Rodex UUID for an internal id, or ``None`` when absent."""
    _validate_positive_id(session_id, "session_id")
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        row = connection.execute(
            f"SELECT uuid_int_1, uuid_int_2 FROM {RODEX_SESSIONS_TABLE} WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return join_signed_bigints_into_a_rodex_uuid(int(row[0]), int(row[1]))


def lookup_rodex_session_log(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionLog | None:
    """Return the one log row belonging to a session, or ``None`` when absent."""
    _validate_session_id(session_id)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        row = connection.execute(
            f"SELECT id, rodex_sessions_id, created_at_utc, rodex_sessions_users_id, "
            f"last_accessed_at_utc FROM {RODEX_SESSIONS_LOG_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
    return None if row is None else _session_log_from_row(row)


def record_a_rodex_session_access(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    accessed_at_utc: datetime | None = None,
) -> RodexSessionLog:
    """Update and return the most recent access timestamp for a session."""
    _validate_session_id(session_id)
    timestamp = _normalise_utc_datetime(accessed_at_utc)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        cursor = connection.execute(
            f"UPDATE {RODEX_SESSIONS_LOG_TABLE} SET last_accessed_at_utc = ? "
            "WHERE rodex_sessions_id = ?",
            (timestamp, session_id),
        )
        if cursor.rowcount != 1:
            raise RodexSessionError(f"Rodex session log does not exist: {session_id}")
        row = connection.execute(
            f"SELECT id, rodex_sessions_id, created_at_utc, rodex_sessions_users_id, "
            f"last_accessed_at_utc FROM {RODEX_SESSIONS_LOG_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise RodexSessionError(f"Rodex session log disappeared: {session_id}")
    return _session_log_from_row(row)


def record_a_rodex_session_runtime_resume(
    session_id: int,
    tmux_server_socket_path: str | os.PathLike[str],
    tmux_session_name: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    codex_session_uuid: uuid.UUID | str | None = None,
    accessed_at_utc: datetime | None = None,
) -> RodexTmuxSession:
    """Atomically activate a runtime, optionally replacing an unsaved Codex UUID."""
    _validate_session_id(session_id)
    tmux_link = _normalise_tmux_link(
        tmux_server_socket_path,
        tmux_session_name,
    )
    if tmux_link is None:  # Both arguments are required by this public contract.
        raise ValueError("a resumed session requires a tmux endpoint")
    socket_path, session_name = tmux_link
    codex_uuid_halves = (
        None
        if codex_session_uuid is None
        else split_a_codex_uuid_into_signed_bigints(codex_session_uuid)
    )
    timestamp = _normalise_utc_datetime(accessed_at_utc)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        if codex_uuid_halves is not None:
            parsed_codex_uuid = _parse_uuid(codex_session_uuid, "codex_session_uuid")
            codex_cursor = connection.execute(
                f"UPDATE {RODEX_SESSIONS_TABLE} "
                "SET codex_session_uuid_int_1 = ?, codex_session_uuid_int_2 = ? "
                "WHERE id = ?",
                (*codex_uuid_halves, session_id),
            )
            if codex_cursor.rowcount != 1:
                raise RodexSessionError(f"Rodex session does not exist: {session_id}")
            _register_statistics_source(
                connection,
                session_id,
                parsed_codex_uuid,
                timestamp,
            )
        tmux_cursor = connection.execute(
            f"UPDATE {RODEX_TMUX_SESSIONS_TABLE} "
            "SET tmux_server_socket_path = ?, tmux_session_name = ? "
            "WHERE rodex_sessions_id = ?",
            (socket_path, session_name, session_id),
        )
        if tmux_cursor.rowcount != 1:
            raise RodexSessionError(f"Rodex tmux session does not exist: {session_id}")
        log_cursor = connection.execute(
            f"UPDATE {RODEX_SESSIONS_LOG_TABLE} SET last_accessed_at_utc = ? "
            "WHERE rodex_sessions_id = ?",
            (timestamp, session_id),
        )
        if log_cursor.rowcount != 1:
            raise RodexSessionError(f"Rodex session log does not exist: {session_id}")
        row = connection.execute(
            f"SELECT id, rodex_sessions_id, tmux_server_socket_path, "
            f"tmux_session_name FROM {RODEX_TMUX_SESSIONS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise RodexSessionError(f"Rodex tmux session disappeared: {session_id}")
    return RodexTmuxSession(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        tmux_server_socket_path=str(row[2]),
        tmux_session_name=str(row[3]),
    )


def lookup_codex_uuid_from_a_rodex_session_id(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> uuid.UUID | None:
    """Return the Codex UUID stored on one Rodex session."""
    _validate_session_id(session_id)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        row = connection.execute(
            f"SELECT codex_session_uuid_int_1, codex_session_uuid_int_2 "
            f"FROM {RODEX_SESSIONS_TABLE} WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return join_signed_bigints_into_a_codex_uuid(int(row[0]), int(row[1]))


def publish_rodex_session_statistics(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    expected_current_codex_uuid: uuid.UUID | str,
    based_on_statistics_revision: int | None,
    statistics_projection_schema_version: str,
    calculated_at_utc: str,
    coverage_state: str,
    statistics_projection: SessionStatisticsProjection,
    analyzed_sources: Sequence[RodexSessionStatisticsSourceObservation],
) -> RodexSessionStatistics:
    """Atomically publish one fenced session projection, turns, and sources."""
    _validate_session_id(session_id)
    expected_halves = split_a_codex_uuid_into_signed_bigints(expected_current_codex_uuid)
    if based_on_statistics_revision is not None:
        _validate_positive_id(based_on_statistics_revision, "based_on_statistics_revision")
    schema_version = _normalise_required_text(
        statistics_projection_schema_version,
        "statistics_projection_schema_version",
    )
    calculated = _normalise_utc_timestamp_text(calculated_at_utc)
    coverage = _normalise_required_text(coverage_state, "coverage_state")
    if coverage not in STATISTICS_COVERAGE_STATES:
        raise ValueError(f"unsupported statistics coverage state: {coverage}")
    statistics_projection = validate_session_statistics_projection(statistics_projection)
    observations = tuple(_validate_source_observation(item) for item in analyzed_sources)
    if len({item.codex_session_uuid for item in observations}) != len(observations):
        raise ValueError("analyzed_sources contains a duplicate Codex UUID")
    if statistics_projection.analyzer_source_count != len(observations):
        raise ValueError(
            "analyzer source count must equal authenticated source observations"
        )
    turns = statistics_projection.turn_statistics
    turn_keys = {(item.codex_session_uuid, item.codex_turn_id) for item in turns}
    if len(turn_keys) != len(turns):
        raise ValueError("turn_statistics contains a duplicate source and turn ID")

    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        identity_row = connection.execute(
            f"SELECT codex_session_uuid_int_1, codex_session_uuid_int_2 "
            f"FROM {RODEX_SESSIONS_TABLE} WHERE id = ?",
            (session_id,),
        ).fetchone()
        if identity_row is None:
            raise RodexSessionError(f"Rodex session does not exist: {session_id}")
        if (int(identity_row[0]), int(identity_row[1])) != expected_halves:
            raise RodexSessionStatisticsConflictError(
                "current Codex UUID changed during statistics calculation"
            )
        previous_row = connection.execute(
            f"SELECT statistics_revision FROM {RODEX_SESSIONS_STATISTICS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
        previous_revision = None if previous_row is None else int(previous_row[0])
        if previous_revision != based_on_statistics_revision:
            raise RodexSessionStatisticsConflictError(
                "statistics revision changed during calculation"
            )
        new_revision = 1 if previous_revision is None else previous_revision + 1
        registered_rows = connection.execute(
            f"SELECT id, codex_session_uuid_int_1, codex_session_uuid_int_2 "
            f"FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchall()
        registered = {(int(row[1]), int(row[2])) for row in registered_rows}
        source_ids = {(int(row[1]), int(row[2])): int(row[0]) for row in registered_rows}
        observed = {
            split_a_codex_uuid_into_signed_bigints(item.codex_session_uuid)
            for item in observations
        }
        if not observed.issubset(registered):
            raise RodexSessionStatisticsConflictError(
                "statistics include an unregistered Codex source"
            )
        if observed != registered:
            raise RodexSessionStatisticsConflictError(
                "statistics omit a registered Codex lineage source"
            )
        turn_sources = {
            split_a_codex_uuid_into_signed_bigints(item.codex_session_uuid)
            for item in turns
        }
        if not turn_sources.issubset(observed):
            raise RodexSessionStatisticsConflictError(
                "turn statistics include a source outside the analyzed snapshot"
            )

        connection.execute(
            f"UPDATE {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
            "SET included_statistics_revision = NULL "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        )
        scalar_columns_sql = ", ".join(_SESSION_PROJECTION_SCALAR_COLUMNS)
        scalar_placeholders = ", ".join("?" for _ in _SESSION_PROJECTION_SCALAR_COLUMNS)
        scalar_updates = ", ".join(
            f"{column} = excluded.{column}" for column in _SESSION_PROJECTION_SCALAR_COLUMNS
        )
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TABLE} "
            "(rodex_sessions_id, statistics_revision, "
            "statistics_projection_schema_version, calculated_at_utc, "
            f"coverage_state, {scalar_columns_sql}) "
            f"VALUES (?, ?, ?, ?, ?, {scalar_placeholders}) "
            "ON CONFLICT(rodex_sessions_id) DO UPDATE SET "
            "statistics_revision = excluded.statistics_revision, "
            "statistics_projection_schema_version = "
            "excluded.statistics_projection_schema_version, "
            "calculated_at_utc = excluded.calculated_at_utc, "
            "coverage_state = excluded.coverage_state, "
            f"{scalar_updates}",
            (
                session_id,
                new_revision,
                schema_version,
                calculated,
                coverage,
                *(
                    getattr(statistics_projection, column)
                    for column in _SESSION_PROJECTION_SCALAR_COLUMNS
                ),
            ),
        )
        for table in (
            RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE,
            RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE,
            RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE,
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE rodex_sessions_id = ?", (session_id,)
            )
        connection.executemany(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE} "
            "(rodex_sessions_id, included_statistics_revision, distribution_kind, "
            "observation_count, total, median, p75, p90, p95, maximum) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    session_id,
                    new_revision,
                    item.distribution_kind,
                    item.observation_count,
                    item.total,
                    item.median,
                    item.p75,
                    item.p90,
                    item.p95,
                    item.maximum,
                )
                for item in statistics_projection.distributions
            ),
        )
        connection.executemany(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE} "
            "(rodex_sessions_id, included_statistics_revision, count_kind, "
            "count_name, occurrence_count) VALUES (?, ?, ?, ?, ?)",
            (
                (
                    session_id,
                    new_revision,
                    item.count_kind,
                    item.count_name,
                    item.occurrence_count,
                )
                for item in statistics_projection.named_counts
            ),
        )
        connection.executemany(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE} "
            "(rodex_sessions_id, included_statistics_revision, limit_ordinal, "
            "limitation) VALUES (?, ?, ?, ?)",
            (
                (session_id, new_revision, ordinal, limitation)
                for ordinal, limitation in enumerate(statistics_projection.audit_limits)
            ),
        )
        for item in observations:
            cursor = connection.execute(
                f"UPDATE {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} SET "
                "rollout_file_path = ?, analyzed_size_bytes = ?, "
                "analyzed_mtime_ns = ?, analyzed_prefix_sha256 = ?, "
                "verified_at_utc = ?, included_statistics_revision = ? "
                "WHERE rodex_sessions_id = ? AND codex_session_uuid_int_1 = ? "
                "AND codex_session_uuid_int_2 = ?",
                (
                    str(item.rollout_file_path),
                    item.analyzed_size_bytes,
                    item.analyzed_mtime_ns,
                    item.analyzed_prefix_sha256,
                    item.verified_at_utc,
                    new_revision,
                    session_id,
                    *split_a_codex_uuid_into_signed_bigints(item.codex_session_uuid),
                ),
            )
            if cursor.rowcount != 1:
                raise RodexSessionStatisticsConflictError(
                    "registered statistics source changed during publication"
                )
        connection.execute(
            f"DELETE FROM {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        )
        turn_scalar_columns_sql = ", ".join(_TURN_DATABASE_SCALAR_COLUMNS)
        turn_scalar_placeholders = ", ".join("?" for _ in _TURN_DATABASE_SCALAR_COLUMNS)
        turn_scalar_updates = ", ".join(
            f"{column} = excluded.{column}" for column in _TURN_DATABASE_SCALAR_COLUMNS
        )
        for item in turns:
            source_halves = split_a_codex_uuid_into_signed_bigints(item.codex_session_uuid)
            source_id = source_ids[source_halves]
            turn_hash = _turn_id_sha256_signed_bigints(item.codex_turn_id)
            existing = connection.execute(
                f"SELECT id, codex_turn_id FROM "
                f"{RODEX_SESSIONS_STATISTICS_TURNS_TABLE} "
                "WHERE rodex_sessions_statistics_sources_id = ? "
                "AND codex_turn_id_sha256_int_1 = ? "
                "AND codex_turn_id_sha256_int_2 = ? "
                "AND codex_turn_id_sha256_int_3 = ? "
                "AND codex_turn_id_sha256_int_4 = ?",
                (source_id, *turn_hash),
            ).fetchone()
            if existing is not None and str(existing[1]) != item.codex_turn_id:
                raise RodexSessionStatisticsConflictError(
                    "turn ID digest collision during statistics publication"
                )
            row = connection.execute(
                f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} "
                "(rodex_sessions_id, rodex_sessions_statistics_sources_id, "
                "codex_turn_id_sha256_int_1, codex_turn_id_sha256_int_2, "
                "codex_turn_id_sha256_int_3, codex_turn_id_sha256_int_4, "
                "codex_turn_id, included_statistics_revision, started_at_utc, "
                f"terminal_at_utc, outcome, {turn_scalar_columns_sql}) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                f"{turn_scalar_placeholders}) "
                "ON CONFLICT(rodex_sessions_statistics_sources_id, "
                "codex_turn_id_sha256_int_1, codex_turn_id_sha256_int_2, "
                "codex_turn_id_sha256_int_3, codex_turn_id_sha256_int_4) "
                "DO UPDATE SET included_statistics_revision = "
                "excluded.included_statistics_revision, "
                "started_at_utc = excluded.started_at_utc, "
                "terminal_at_utc = excluded.terminal_at_utc, "
                "outcome = excluded.outcome, "
                f"{turn_scalar_updates} "
                "RETURNING id",
                (
                    session_id,
                    source_id,
                    *turn_hash,
                    item.codex_turn_id,
                    new_revision,
                    item.started_at_utc,
                    item.terminal_at_utc,
                    item.outcome,
                    *_turn_database_scalar_values(item),
                ),
            ).fetchone()
            if row is None:
                raise RodexSessionError("turn statistics upsert returned no identity")
            turn_row_id = int(row[0])
            connection.executemany(
                f"INSERT INTO {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
                "(rodex_sessions_id, rodex_sessions_statistics_turns_id, "
                "included_statistics_revision, count_kind, count_name, "
                "occurrence_count) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        session_id,
                        turn_row_id,
                        new_revision,
                        count.count_kind,
                        count.count_name,
                        count.occurrence_count,
                    )
                    for count in item.named_counts
                ),
            )
        connection.execute(
            f"DELETE FROM {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} "
            "WHERE rodex_sessions_id = ? AND included_statistics_revision != ?",
            (session_id, new_revision),
        )
        _upsert_statistics_worker(
            connection,
            session_id,
            worker_state="up_to_date",
            diagnostic_code=None,
            last_attempted_at_utc=calculated,
            consecutive_failures=0,
            next_retry_at_utc=None,
        )
    published = lookup_rodex_session_statistics(session_id, path)
    if published is None:
        raise RodexSessionError(f"Rodex statistics disappeared: {session_id}")
    return published


def record_rodex_session_statistics_worker_health(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
    *,
    expected_current_codex_uuid: uuid.UUID | str,
    worker_state: str,
    diagnostic_code: str | None,
    last_attempted_at_utc: str,
    consecutive_failures: int,
    next_retry_at_utc: str | None = None,
) -> RodexSessionStatisticsWorker:
    """Update only fail-open worker health, preserving all last-good statistics."""
    _validate_session_id(session_id)
    expected_halves = split_a_codex_uuid_into_signed_bigints(expected_current_codex_uuid)
    state = _normalise_required_text(worker_state, "worker_state")
    if state not in STATISTICS_WORKER_STATES:
        raise ValueError(f"unsupported statistics worker state: {state}")
    diagnostic = (
        None
        if diagnostic_code is None
        else _normalise_required_text(diagnostic_code, "diagnostic_code")
    )
    if diagnostic is not None and (
        len(diagnostic) > 64
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
            for character in diagnostic
        )
    ):
        raise ValueError(
            "diagnostic_code must contain 1-64 lowercase ASCII letters, digits, "
            "or underscores"
        )
    attempted = _normalise_utc_timestamp_text(last_attempted_at_utc)
    if (
        not isinstance(consecutive_failures, int)
        or isinstance(consecutive_failures, bool)
        or consecutive_failures < 0
    ):
        raise ValueError("consecutive_failures must be a non-negative integer")
    next_retry = (
        None
        if next_retry_at_utc is None
        else _normalise_utc_timestamp_text(next_retry_at_utc)
    )
    if state == "up_to_date" and (
        diagnostic is not None or consecutive_failures != 0 or next_retry is not None
    ):
        raise ValueError(
            "up_to_date worker health cannot include diagnostics, failures, or retry"
        )
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        identity_row = connection.execute(
            f"SELECT codex_session_uuid_int_1, codex_session_uuid_int_2 "
            f"FROM {RODEX_SESSIONS_TABLE} WHERE id = ?",
            (session_id,),
        ).fetchone()
        if identity_row is None:
            raise RodexSessionError(f"Rodex session does not exist: {session_id}")
        if (int(identity_row[0]), int(identity_row[1])) != expected_halves:
            raise RodexSessionStatisticsConflictError(
                "current Codex UUID changed before worker health publication"
            )
        _upsert_statistics_worker(
            connection,
            session_id,
            worker_state=state,
            diagnostic_code=diagnostic,
            last_attempted_at_utc=attempted,
            consecutive_failures=consecutive_failures,
            next_retry_at_utc=next_retry,
        )
        row = _select_statistics_worker(connection, session_id)
    if row is None:
        raise RodexSessionError(f"Rodex statistics worker disappeared: {session_id}")
    return _statistics_worker_from_row(row)


def read_rodex_session_statistics(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionStatisticsView:
    """Read last-good statistics, worker health, and sources in one transaction."""
    _validate_session_id(session_id)
    path = initialise_rodex_database(database_path)
    with open_rodex_read_transaction(path) as connection:
        statistics_row = _select_statistics(connection, session_id)
        distribution_rows = _select_statistics_distributions(connection, session_id)
        named_count_rows = _select_statistics_named_counts(connection, session_id)
        audit_limit_rows = _select_statistics_audit_limits(connection, session_id)
        worker_row = _select_statistics_worker(connection, session_id)
        source_rows = _select_statistics_sources(connection, session_id)
    return RodexSessionStatisticsView(
        statistics=(
            None
            if statistics_row is None
            else _session_statistics_from_rows(
                statistics_row,
                distribution_rows,
                named_count_rows,
                audit_limit_rows,
            )
        ),
        worker=(None if worker_row is None else _statistics_worker_from_row(worker_row)),
        sources=tuple(_statistics_source_from_row(row) for row in source_rows),
    )


def read_rodex_session_turn_statistics(
    session_id: int,
    codex_turn_id: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    codex_session_uuid: uuid.UUID | str | None = None,
) -> RodexSessionTurnStatisticsView:
    """Read one exact turn and its parent freshness in one transaction."""
    _validate_session_id(session_id)
    turn_id = _normalise_required_text(codex_turn_id, "codex_turn_id")
    turn_hash = _turn_id_sha256_signed_bigints(turn_id)
    source_halves = (
        None
        if codex_session_uuid is None
        else split_a_codex_uuid_into_signed_bigints(codex_session_uuid)
    )
    path = initialise_rodex_database(database_path)
    with open_rodex_read_transaction(path) as connection:
        statistics_row = _select_statistics(connection, session_id)
        distribution_rows = _select_statistics_distributions(connection, session_id)
        named_count_rows = _select_statistics_named_counts(connection, session_id)
        audit_limit_rows = _select_statistics_audit_limits(connection, session_id)
        worker_row = _select_statistics_worker(connection, session_id)
        source_rows = _select_statistics_sources(connection, session_id)
        turn_scalar_columns = ", ".join(
            f"turns.{column}" for column in _TURN_DATABASE_SCALAR_COLUMNS
        )
        query = (
            f"SELECT turns.id, turns.rodex_sessions_id, "
            "turns.rodex_sessions_statistics_sources_id, "
            "sources.codex_session_uuid_int_1, "
            "sources.codex_session_uuid_int_2, turns.codex_turn_id, "
            "turns.included_statistics_revision, turns.started_at_utc, "
            f"turns.terminal_at_utc, turns.outcome, {turn_scalar_columns} "
            f"FROM {RODEX_SESSIONS_STATISTICS_TURNS_TABLE} AS turns "
            f"JOIN {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} AS sources "
            "ON sources.id = turns.rodex_sessions_statistics_sources_id "
            "WHERE turns.rodex_sessions_id = ? "
            "AND turns.codex_turn_id_sha256_int_1 = ? "
            "AND turns.codex_turn_id_sha256_int_2 = ? "
            "AND turns.codex_turn_id_sha256_int_3 = ? "
            "AND turns.codex_turn_id_sha256_int_4 = ? "
            "AND turns.codex_turn_id = ?"
        )
        parameters: tuple[object, ...] = (session_id, *turn_hash, turn_id)
        if source_halves is not None:
            query += (
                " AND sources.codex_session_uuid_int_1 = ? "
                "AND sources.codex_session_uuid_int_2 = ?"
            )
            parameters += source_halves
        turn_rows = connection.execute(query + " ORDER BY turns.id", parameters).fetchall()
        turn_named_count_rows = (
            []
            if len(turn_rows) != 1
            else _select_turn_statistics_named_counts(connection, int(turn_rows[0][0]))
        )
    if len(turn_rows) > 1:
        raise RodexSessionTurnStatisticsAmbiguousError(
            "turn ID exists in multiple Codex sources; qualify it with a session UUID"
        )
    return RodexSessionTurnStatisticsView(
        statistics=(
            None
            if statistics_row is None
            else _session_statistics_from_rows(
                statistics_row,
                distribution_rows,
                named_count_rows,
                audit_limit_rows,
            )
        ),
        worker=(None if worker_row is None else _statistics_worker_from_row(worker_row)),
        sources=tuple(_statistics_source_from_row(row) for row in source_rows),
        turn=(
            None
            if not turn_rows
            else _turn_statistics_from_rows(turn_rows[0], turn_named_count_rows)
        ),
    )


def lookup_rodex_session_statistics(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionStatistics | None:
    """Return the latest successful aggregate-only statistics projection."""
    return read_rodex_session_statistics(session_id, database_path).statistics


def list_rodex_session_statistics_sources(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> tuple[RodexSessionStatisticsSource, ...]:
    """List every Codex source registered to one Rodex statistics lineage."""
    return read_rodex_session_statistics(session_id, database_path).sources


def lookup_rodex_tmux_session(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexTmuxSession | None:
    """Return the tmux endpoint linked to one Rodex session."""
    _validate_session_id(session_id)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        return _select_rodex_tmux_session(connection, session_id)


def lookup_rodex_session_id_from_a_cool_name(
    cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Resolve a permanent or user-defined cool name through integer identities."""
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        allocated_name = lookup_cool_name(connection, cool_name)
        if allocated_name is None:
            return None
        rows = connection.execute(
            f"SELECT id FROM {RODEX_SESSIONS_TABLE} "
            "WHERE cool_names_id = ? OR user_defined_cool_names_id = ? "
            "ORDER BY id LIMIT 2",
            (allocated_name.id, allocated_name.id),
        ).fetchall()
    if len(rows) > 1:
        raise RodexSessionError(f"cool name resolves to multiple sessions: {cool_name}")
    return None if not rows else int(rows[0][0])


def lookup_owned_rodex_session_id_from_a_cool_name(
    cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    user_identity: RodexSessionsUserIdentity | None = None,
) -> int | None:
    """Resolve a name only when its session belongs to the selected POSIX user."""
    identity = _resolve_user_identity(user_identity)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        allocated_name = lookup_cool_name(connection, cool_name)
        if allocated_name is None:
            return None
        rows = _select_sessions_and_owners_by_cool_names_id(connection, allocated_name.id)
        if not rows:
            return None
        if len(rows) > 1:
            raise RodexSessionError(f"cool name resolves to multiple sessions: {cool_name}")
        user_id = _lookup_rodex_sessions_user_id(connection, identity)
        if user_id is None or int(rows[0][5]) != user_id:
            raise RodexSessionError(
                f"Rodex session is not owned by the current user: {cool_name}"
            )
        return int(rows[0][0])


def lookup_rodex_session_names(
    session_id: int,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexSessionNames | None:
    """Return the permanent and optional user-defined names for one session."""
    _validate_session_id(session_id)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        row = _select_rodex_session_names(connection, session_id)
    return None if row is None else _session_names_from_row(row)


def assign_a_user_defined_cool_name(
    session_cool_name: str,
    user_defined_cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
    user_identity: RodexSessionsUserIdentity | None = None,
    renamed_tmux_session_name: str | None = None,
) -> RodexSessionNames:
    """Atomically assign one name and an already-renamed live tmux endpoint."""
    if not isinstance(force, bool):
        raise TypeError("force must be a boolean")
    identity = _resolve_user_identity(user_identity)
    persisted_tmux_name = (
        None
        if renamed_tmux_session_name is None
        else _normalise_tmux_session_name(renamed_tmux_session_name)
    )
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        return _apply_user_defined_cool_name_assignment(
            connection,
            session_cool_name,
            user_defined_cool_name,
            identity,
            force=force,
            mutate=True,
            renamed_tmux_session_name=persisted_tmux_name,
        )


def validate_a_user_defined_cool_name_assignment(
    session_cool_name: str,
    user_defined_cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
    user_identity: RodexSessionsUserIdentity | None = None,
) -> RodexSessionNames:
    """Validate an assignment without inserting or updating any lookup row."""
    if not isinstance(force, bool):
        raise TypeError("force must be a boolean")
    identity = _resolve_user_identity(user_identity)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        return _apply_user_defined_cool_name_assignment(
            connection,
            session_cool_name,
            user_defined_cool_name,
            identity,
            force=force,
            mutate=False,
            renamed_tmux_session_name=None,
        )


@contextmanager
def open_a_user_defined_cool_name_assignment(
    session_cool_name: str,
    user_defined_cool_name: str,
    database_path: str | os.PathLike[str] | None = None,
    *,
    force: bool = False,
    user_identity: RodexSessionsUserIdentity | None = None,
) -> Iterator[RodexUserDefinedCoolNameAssignment]:
    """Serialize validation, a caller's live rename, and the durable assignment."""
    if not isinstance(force, bool):
        raise TypeError("force must be a boolean")
    identity = _resolve_user_identity(user_identity)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        planned_names = _apply_user_defined_cool_name_assignment(
            connection,
            session_cool_name,
            user_defined_cool_name,
            identity,
            force=force,
            mutate=False,
            renamed_tmux_session_name=None,
        )
        transition = RodexUserDefinedCoolNameAssignment(
            names=planned_names,
            tmux_session=_select_rodex_tmux_session(
                connection, planned_names.rodex_sessions_id
            ),
        )
        yield transition
        persisted_tmux_name = (
            None
            if transition.renamed_tmux_session_name is None
            else _normalise_tmux_session_name(transition.renamed_tmux_session_name)
        )
        transition.names = _apply_user_defined_cool_name_assignment(
            connection,
            session_cool_name,
            user_defined_cool_name,
            identity,
            force=force,
            mutate=True,
            renamed_tmux_session_name=persisted_tmux_name,
        )


def list_rodex_session_runtimes_for_a_user(
    database_path: str | os.PathLike[str] | None = None,
    *,
    user_identity: RodexSessionsUserIdentity | None = None,
) -> list[RodexSessionRuntime]:
    """List persisted runtime identities owned by one POSIX user."""
    identity = (
        current_rodex_sessions_user_identity()
        if user_identity is None
        else _validate_user_identity(user_identity)
    )
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        user_id = select_lookup_id(
            connection,
            RODEX_SESSIONS_USERS_TABLE,
            {"uid": identity.uid, "gid": identity.gid, "user_name": identity.user_name},
        )
        if user_id is None:
            return []
        rows = connection.execute(
            f"SELECT sessions.id, permanent.cool_name, user_defined.cool_name, "
            "sessions.codex_session_uuid_int_1, "
            "sessions.codex_session_uuid_int_2, tmux.tmux_server_socket_path, "
            "tmux.tmux_session_name "
            f"FROM {RODEX_SESSIONS_TABLE} AS sessions "
            f"JOIN {RODEX_SESSIONS_LOG_TABLE} AS log "
            "ON log.rodex_sessions_id = sessions.id "
            f"JOIN {RODEX_TMUX_SESSIONS_TABLE} AS tmux "
            "ON tmux.rodex_sessions_id = sessions.id "
            "JOIN cool_names AS permanent ON permanent.id = sessions.cool_names_id "
            "LEFT JOIN cool_names AS user_defined "
            "ON user_defined.id = sessions.user_defined_cool_names_id "
            "WHERE log.rodex_sessions_users_id = ? ORDER BY sessions.id",
            (user_id,),
        ).fetchall()
    return [
        RodexSessionRuntime(
            rodex_sessions_id=int(row[0]),
            cool_name=str(row[1]),
            user_defined_cool_name=None if row[2] is None else str(row[2]),
            codex_session_uuid=join_signed_bigints_into_a_codex_uuid(
                int(row[3]), int(row[4])
            ),
            tmux_server_socket_path=str(row[5]),
            tmux_session_name=str(row[6]),
        )
        for row in rows
    ]


def update_rodex_tmux_session_name(
    session_id: int,
    tmux_session_name: str,
    database_path: str | os.PathLike[str] | None = None,
) -> RodexTmuxSession:
    """Record a renamed tmux endpoint for one Rodex session."""
    _validate_session_id(session_id)
    session_name = _normalise_tmux_session_name(tmux_session_name)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        cursor = connection.execute(
            f"UPDATE {RODEX_TMUX_SESSIONS_TABLE} SET tmux_session_name = ? "
            "WHERE rodex_sessions_id = ?",
            (session_name, session_id),
        )
        if cursor.rowcount != 1:
            raise RodexSessionError(f"Rodex tmux session does not exist: {session_id}")
        row = connection.execute(
            f"SELECT id, rodex_sessions_id, tmux_server_socket_path, "
            f"tmux_session_name FROM {RODEX_TMUX_SESSIONS_TABLE} "
            "WHERE rodex_sessions_id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise RodexSessionError(f"Rodex tmux session disappeared: {session_id}")
    return RodexTmuxSession(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        tmux_server_socket_path=str(row[2]),
        tmux_session_name=str(row[3]),
    )


def lookup_rodex_session_id_from_a_codex_uuid(
    codex_session_uuid: uuid.UUID | str,
    database_path: str | os.PathLike[str] | None = None,
) -> int | None:
    """Return the Rodex id linked to a Codex thread UUID."""
    uuid_int_1, uuid_int_2 = split_a_codex_uuid_into_signed_bigints(codex_session_uuid)
    path = initialise_rodex_database(database_path)
    with open_rodex_transaction(path) as connection:
        row = connection.execute(
            f"SELECT id FROM {RODEX_SESSIONS_TABLE} "
            "WHERE codex_session_uuid_int_1 = ? "
            "AND codex_session_uuid_int_2 = ?",
            (uuid_int_1, uuid_int_2),
        ).fetchone()
    return None if row is None else int(row[0])


def split_a_rodex_uuid_into_signed_bigints(
    rodex_uuid: uuid.UUID | str,
) -> tuple[int, int]:
    """Map all 128 UUID bits into SQLite's two signed 64-bit integer values."""
    parsed = _parse_uuid(rodex_uuid, "rodex_uuid")
    return _split_uuid_into_signed_bigints(parsed)


def split_a_codex_uuid_into_signed_bigints(
    codex_session_uuid: uuid.UUID | str,
) -> tuple[int, int]:
    """Map all 128 Codex UUID bits into its two signed storage integers."""
    parsed = _parse_uuid(codex_session_uuid, "codex_session_uuid")
    return _split_uuid_into_signed_bigints(parsed)


def _split_uuid_into_signed_bigints(parsed: uuid.UUID) -> tuple[int, int]:
    high_unsigned = parsed.int >> _HALF_BITS
    low_unsigned = parsed.int & (_HALF_MODULUS - 1)
    return _unsigned_half_to_signed(high_unsigned), _unsigned_half_to_signed(low_unsigned)


def join_signed_bigints_into_a_rodex_uuid(uuid_int_1: int, uuid_int_2: int) -> uuid.UUID:
    """Reverse the SQLite signed representation into the original 128-bit UUID."""
    high_unsigned = _signed_half_to_unsigned(uuid_int_1)
    low_unsigned = _signed_half_to_unsigned(uuid_int_2)
    return uuid.UUID(int=(high_unsigned << _HALF_BITS) | low_unsigned)


def join_signed_bigints_into_a_codex_uuid(
    codex_session_uuid_int_1: int,
    codex_session_uuid_int_2: int,
) -> uuid.UUID:
    """Reverse the Codex storage integers into its original 128-bit UUID."""
    high_unsigned = _signed_half_to_unsigned(codex_session_uuid_int_1)
    low_unsigned = _signed_half_to_unsigned(codex_session_uuid_int_2)
    return uuid.UUID(int=(high_unsigned << _HALF_BITS) | low_unsigned)


def _normalise_tmux_link(
    tmux_server_socket_path: str | os.PathLike[str] | None,
    tmux_session_name: str | None,
) -> tuple[str, str] | None:
    values = (tmux_server_socket_path, tmux_session_name)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError(
            "tmux_server_socket_path and tmux_session_name must be provided together"
        )
    socket_path = os.fspath(tmux_server_socket_path)
    if not socket_path.strip():
        raise ValueError("tmux_server_socket_path must be non-empty")
    return socket_path, _normalise_tmux_session_name(tmux_session_name)


def _normalise_tmux_session_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("tmux_session_name must be a non-empty string")
    return value.strip()


def _verify_sessions_table(connection: sqlite3.Connection) -> None:
    columns = connection.execute(f"PRAGMA table_info({RODEX_SESSIONS_TABLE})").fetchall()
    observed = [(row[1], row[2].upper(), row[3], row[5]) for row in columns]
    expected = [
        ("id", "INTEGER", 0, 1),
        ("uuid_int_1", "BIGINT", 1, 0),
        ("uuid_int_2", "BIGINT", 1, 0),
        ("codex_session_uuid_int_1", "BIGINT", 1, 0),
        ("codex_session_uuid_int_2", "BIGINT", 1, 0),
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
        RODEX_UUID_UNIQUE_INDEX,
        ["uuid_int_1", "uuid_int_2"],
    )
    _verify_unique_index(
        connection,
        RODEX_SESSIONS_TABLE,
        RODEX_CODEX_UUID_UNIQUE_INDEX,
        ["codex_session_uuid_int_1", "codex_session_uuid_int_2"],
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


def _verify_statistics_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("statistics_revision", "INTEGER", 1, 0),
            ("statistics_projection_schema_version", "TEXT", 1, 0),
            ("calculated_at_utc", "TEXT", 1, 0),
            ("coverage_state", "TEXT", 1, 0),
            ("analyzer_event_count", "INTEGER", 1, 0),
            ("analyzer_source_count", "INTEGER", 1, 0),
            ("history_sessions_count", "INTEGER", 1, 0),
            ("history_records_count", "INTEGER", 1, 0),
            ("history_malformed_records_count", "INTEGER", 1, 0),
            ("turns_started_count", "INTEGER", 1, 0),
            ("turns_completed_count", "INTEGER", 1, 0),
            ("turns_aborted_count", "INTEGER", 1, 0),
            ("turns_open_count", "INTEGER", 1, 0),
            ("input_tokens", "INTEGER", 1, 0),
            ("cached_input_tokens", "INTEGER", 1, 0),
            ("cache_write_input_tokens", "INTEGER", 1, 0),
            ("output_tokens", "INTEGER", 1, 0),
            ("reasoning_output_tokens", "INTEGER", 1, 0),
            ("total_tokens", "INTEGER", 1, 0),
            ("context_observation_count", "INTEGER", 1, 0),
            ("context_latest_session_median_percent", "REAL", 0, 0),
            ("context_high_water_percent", "REAL", 1, 0),
            ("commands_executed_count", "INTEGER", 1, 0),
            ("model_tool_requests_count", "INTEGER", 1, 0),
            ("model_tool_outputs_paired_count", "INTEGER", 1, 0),
            ("file_change_operations_count", "INTEGER", 1, 0),
            ("file_change_distinct_paths_count", "INTEGER", 1, 0),
            ("file_change_occurrences_count", "INTEGER", 1, 0),
            ("web_operations_count", "INTEGER", 1, 0),
            ("web_queries_count", "INTEGER", 1, 0),
            ("web_result_records_count", "INTEGER", 1, 0),
            ("web_distinct_result_or_action_urls_count", "INTEGER", 1, 0),
            ("collaboration_operations_count", "INTEGER", 1, 0),
            ("collaboration_agents_started_count", "INTEGER", 1, 0),
            ("compactions_count", "INTEGER", 1, 0),
            ("distinct_workspaces_count", "INTEGER", 1, 0),
            ("typical_turns_count", "INTEGER", 1, 0),
            ("hands_on_turn_count", "INTEGER", 1, 0),
            ("hands_on_turn_rate_percent", "REAL", 0, 0),
            ("turns_with_nonzero_command_count", "INTEGER", 1, 0),
            ("turns_subsequently_completed_count", "INTEGER", 1, 0),
            ("completed_after_nonzero_command_percent", "REAL", 0, 0),
            ("command_zero_exit_rate_percent", "REAL", 0, 0),
            ("exact_command_repeat_rate_percent", "REAL", 0, 0),
            ("repeated_command_execution_count", "INTEGER", 1, 0),
            ("cached_input_share_percent", "REAL", 0, 0),
            ("reasoning_output_share_percent", "REAL", 0, 0),
            ("edited_turns_count", "INTEGER", 1, 0),
            ("verified_after_edit_count", "INTEGER", 1, 0),
            ("edit_then_verify_percent", "REAL", 0, 0),
            ("web_turns_count", "INTEGER", 1, 0),
            ("web_later_command_or_file_work_count", "INTEGER", 1, 0),
            ("web_follow_through_percent", "REAL", 0, 0),
            ("file_revisit_rate_percent", "REAL", 0, 0),
            ("revisited_distinct_path_count", "INTEGER", 1, 0),
            ("busiest_workspace_turn_share_percent", "REAL", 0, 0),
            ("workspace_tagged_turn_count", "INTEGER", 1, 0),
            ("turns_in_busiest_workspace_count", "INTEGER", 1, 0),
            ("turns_with_local_hour_count", "INTEGER", 1, 0),
            ("busiest_local_hour", "INTEGER", 0, 0),
            ("turns_in_busiest_local_hour_count", "INTEGER", 1, 0),
            ("goal_updates_count", "INTEGER", 1, 0),
            ("audit_privacy", "TEXT", 1, 0),
            ("audit_percentile_method", "TEXT", 1, 0),
            ("audit_token_method", "TEXT", 1, 0),
            ("audit_token_snapshots_count", "INTEGER", 1, 0),
            ("audit_repeated_token_snapshots_count", "INTEGER", 1, 0),
            ("audit_token_epochs_count", "INTEGER", 1, 0),
            ("audit_duplicate_operations_ignored_count", "INTEGER", 1, 0),
            ("audit_duplicate_terminals_ignored_count", "INTEGER", 1, 0),
            (
                "audit_terminal_events_without_start_ignored_count",
                "INTEGER",
                1,
                0,
            ),
            ("audit_new_event_type_warnings_count", "INTEGER", 1, 0),
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
            "CHECK (STATISTICS_REVISION >= 1)",
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
            ("included_statistics_revision", "INTEGER", 1, 0),
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
    _verify_statistics_revision_foreign_key(
        connection, RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE
    )


def _verify_statistics_named_counts_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("included_statistics_revision", "INTEGER", 1, 0),
            ("count_kind", "TEXT", 1, 0),
            ("count_name", "TEXT", 1, 0),
            ("occurrence_count", "INTEGER", 1, 0),
        ],
    )
    _verify_statistics_revision_foreign_key(
        connection, RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE
    )


def _verify_statistics_audit_limits_table(connection: sqlite3.Connection) -> None:
    _verify_table_columns(
        connection,
        RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE,
        [
            ("id", "INTEGER", 0, 1),
            ("rodex_sessions_id", "INTEGER", 1, 0),
            ("included_statistics_revision", "INTEGER", 1, 0),
            ("limit_ordinal", "INTEGER", 1, 0),
            ("limitation", "TEXT", 1, 0),
        ],
    )
    _verify_statistics_revision_foreign_key(
        connection, RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE
    )


def _verify_statistics_revision_foreign_key(
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
            "included_statistics_revision",
            "statistics_revision",
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
            ("codex_session_uuid_int_1", "BIGINT", 1, 0),
            ("codex_session_uuid_int_2", "BIGINT", 1, 0),
            ("first_linked_at_utc", "TEXT", 1, 0),
            ("rollout_file_path", "TEXT", 0, 0),
            ("analyzed_size_bytes", "INTEGER", 0, 0),
            ("analyzed_mtime_ns", "INTEGER", 0, 0),
            ("analyzed_prefix_sha256", "TEXT", 0, 0),
            ("verified_at_utc", "TEXT", 0, 0),
            ("included_statistics_revision", "INTEGER", 0, 0),
        ],
    )
    foreign_keys = connection.execute(
        f"PRAGMA foreign_key_list({RODEX_SESSIONS_STATISTICS_SOURCES_TABLE})"
    ).fetchall()
    observed_foreign_keys = {(row[2], row[3], row[4]) for row in foreign_keys}
    expected_foreign_keys = {
        (RODEX_SESSIONS_TABLE, "rodex_sessions_id", "id"),
        (
            RODEX_SESSIONS_STATISTICS_TABLE,
            "rodex_sessions_id",
            "rodex_sessions_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TABLE,
            "included_statistics_revision",
            "statistics_revision",
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
            "ANALYZED_SIZE_BYTES IS NULL OR ANALYZED_SIZE_BYTES >= 0",
            "ANALYZED_MTIME_NS IS NULL OR ANALYZED_MTIME_NS >= 0",
            "INCLUDED_STATISTICS_REVISION IS NULL OR INCLUDED_STATISTICS_REVISION >= 1",
            "INCLUDED_STATISTICS_REVISION IS NULL OR ROLLOUT_FILE_PATH IS NOT NULL",
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
            ("included_statistics_revision", "INTEGER", 1, 0),
            ("started_at_utc", "TEXT", 0, 0),
            ("terminal_at_utc", "TEXT", 0, 0),
            ("outcome", "TEXT", 1, 0),
            ("duration_ms", "INTEGER", 0, 0),
            ("time_to_first_token_ms", "INTEGER", 0, 0),
            ("input_tokens", "INTEGER", 1, 0),
            ("cached_input_tokens", "INTEGER", 1, 0),
            ("cache_write_input_tokens", "INTEGER", 1, 0),
            ("output_tokens", "INTEGER", 1, 0),
            ("reasoning_output_tokens", "INTEGER", 1, 0),
            ("total_tokens", "INTEGER", 1, 0),
            ("context_observation_count", "INTEGER", 1, 0),
            ("context_high_water_percent", "REAL", 1, 0),
            ("commands_executed_count", "INTEGER", 1, 0),
            ("command_duration_observation_count", "INTEGER", 1, 0),
            ("command_duration_total_ms", "INTEGER", 1, 0),
            ("command_duration_median_ms", "REAL", 0, 0),
            ("command_duration_p75_ms", "INTEGER", 0, 0),
            ("command_duration_p90_ms", "INTEGER", 0, 0),
            ("command_duration_p95_ms", "INTEGER", 0, 0),
            ("command_duration_maximum_ms", "INTEGER", 0, 0),
            ("model_tool_requests_count", "INTEGER", 1, 0),
            ("model_tool_outputs_paired_count", "INTEGER", 1, 0),
            ("file_change_operations_count", "INTEGER", 1, 0),
            ("file_change_distinct_paths_count", "INTEGER", 1, 0),
            ("file_change_occurrences_count", "INTEGER", 1, 0),
            ("web_operations_count", "INTEGER", 1, 0),
            ("web_queries_count", "INTEGER", 1, 0),
            ("web_result_records_count", "INTEGER", 1, 0),
            ("web_distinct_result_or_action_urls_count", "INTEGER", 1, 0),
            ("collaboration_operations_count", "INTEGER", 1, 0),
            ("collaboration_agents_started_count", "INTEGER", 1, 0),
            ("compactions_count", "INTEGER", 1, 0),
            ("workspace_digest", "TEXT", 0, 0),
            ("model", "TEXT", 0, 0),
            ("local_start_hour", "INTEGER", 0, 0),
            ("hands_on", "INTEGER", 1, 0),
            ("completed_after_nonzero_command", "INTEGER", 1, 0),
            ("cached_input_share_percent", "REAL", 0, 0),
            ("reasoning_output_share_percent", "REAL", 0, 0),
            ("edited_then_verified", "INTEGER", 1, 0),
            (
                "web_research_followed_by_command_or_file_work",
                "INTEGER",
                1,
                0,
            ),
            ("goal_updates_count", "INTEGER", 1, 0),
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
            "included_statistics_revision",
            "included_statistics_revision",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TABLE,
            "rodex_sessions_id",
            "rodex_sessions_id",
        ),
        (
            RODEX_SESSIONS_STATISTICS_TABLE,
            "included_statistics_revision",
            "statistics_revision",
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
            "CHECK ( INCLUDED_STATISTICS_REVISION >= 1 )",
            "OUTCOME IN ('OPEN', 'COMPLETED', 'ABORTED')",
            "OUTCOME != 'OPEN' OR TERMINAL_AT_UTC IS NULL",
            "HANDS_ON IN (0, 1)",
            "EDITED_THEN_VERIFIED IN (0, 1)",
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
            ("included_statistics_revision", "INTEGER", 1, 0),
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
            "included_statistics_revision",
            "included_statistics_revision",
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


def _register_missing_statistics_sources(connection: sqlite3.Connection) -> None:
    """Adopt root Codex identities created before the additive source table existed."""
    connection.execute(
        f"INSERT INTO {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
        "(rodex_sessions_id, codex_session_uuid_int_1, "
        "codex_session_uuid_int_2, first_linked_at_utc) "
        f"SELECT sessions.id, sessions.codex_session_uuid_int_1, "
        "sessions.codex_session_uuid_int_2, log.created_at_utc "
        f"FROM {RODEX_SESSIONS_TABLE} AS sessions "
        f"JOIN {RODEX_SESSIONS_LOG_TABLE} AS log "
        "ON log.rodex_sessions_id = sessions.id "
        "WHERE NOT EXISTS ("
        f"SELECT 1 FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} AS registered "
        "WHERE registered.codex_session_uuid_int_1 = "
        "sessions.codex_session_uuid_int_1 "
        "AND registered.codex_session_uuid_int_2 = "
        "sessions.codex_session_uuid_int_2)"
    )
    mismatch = connection.execute(
        f"SELECT sessions.id FROM {RODEX_SESSIONS_TABLE} AS sessions "
        f"LEFT JOIN {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} AS sources "
        "ON sources.codex_session_uuid_int_1 = sessions.codex_session_uuid_int_1 "
        "AND sources.codex_session_uuid_int_2 = sessions.codex_session_uuid_int_2 "
        "WHERE sources.id IS NULL OR sources.rodex_sessions_id != sessions.id "
        "LIMIT 1"
    ).fetchone()
    if mismatch is not None:
        raise RodexSessionError(
            "a current Codex identity conflicts with an existing statistics lineage: "
            f"Rodex session {int(mismatch[0])}"
        )


def _register_statistics_source(
    connection: sqlite3.Connection,
    session_id: int,
    codex_session_uuid: uuid.UUID,
    first_linked_at_utc: str,
) -> None:
    uuid_halves = split_a_codex_uuid_into_signed_bigints(codex_session_uuid)
    row = connection.execute(
        f"SELECT rodex_sessions_id FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
        "WHERE codex_session_uuid_int_1 = ? AND codex_session_uuid_int_2 = ?",
        uuid_halves,
    ).fetchone()
    if row is None:
        connection.execute(
            f"INSERT INTO {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
            "(rodex_sessions_id, codex_session_uuid_int_1, "
            "codex_session_uuid_int_2, first_linked_at_utc) VALUES (?, ?, ?, ?)",
            (session_id, *uuid_halves, first_linked_at_utc),
        )
        return
    if int(row[0]) != session_id:
        raise RodexSessionError(
            "Codex history already belongs to another Rodex statistics lineage: "
            f"{codex_session_uuid}"
        )


def _turn_database_scalar_values(
    projection: TurnStatisticsProjection,
) -> tuple[object, ...]:
    command_duration = projection.command_duration
    return (
        *(getattr(projection, name) for name in _TURN_PROJECTION_SCALAR_COLUMNS[:11]),
        command_duration.observation_count,
        command_duration.total,
        command_duration.median,
        command_duration.p75,
        command_duration.p90,
        command_duration.p95,
        command_duration.maximum,
        *(getattr(projection, name) for name in _TURN_PROJECTION_SCALAR_COLUMNS[11:]),
    )


def _turn_id_sha256_signed_bigints(turn_id: str) -> tuple[int, int, int, int]:
    normalized = _normalise_required_text(turn_id, "codex_turn_id")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    pieces = tuple(
        int.from_bytes(digest[offset : offset + 8], "big", signed=True)
        for offset in range(0, 32, 8)
    )
    return pieces[0], pieces[1], pieces[2], pieces[3]


def _validate_source_observation(
    observation: RodexSessionStatisticsSourceObservation,
) -> RodexSessionStatisticsSourceObservation:
    if not isinstance(observation, RodexSessionStatisticsSourceObservation):
        raise TypeError(
            "analyzed_sources must contain RodexSessionStatisticsSourceObservation values"
        )
    codex_uuid = _parse_uuid(observation.codex_session_uuid, "codex_session_uuid")
    source_path = observation.rollout_file_path.expanduser().resolve()
    if not source_path.is_absolute():
        raise ValueError("rollout_file_path must resolve to an absolute path")
    for value, field_name in (
        (observation.analyzed_size_bytes, "analyzed_size_bytes"),
        (observation.analyzed_mtime_ns, "analyzed_mtime_ns"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
    digest = _normalise_required_text(
        observation.analyzed_prefix_sha256, "analyzed_prefix_sha256"
    ).lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError("analyzed_prefix_sha256 must be 64 lowercase hexadecimal digits")
    return RodexSessionStatisticsSourceObservation(
        codex_session_uuid=codex_uuid,
        rollout_file_path=source_path,
        analyzed_size_bytes=observation.analyzed_size_bytes,
        analyzed_mtime_ns=observation.analyzed_mtime_ns,
        analyzed_prefix_sha256=digest,
        verified_at_utc=_normalise_utc_timestamp_text(observation.verified_at_utc),
    )


def _select_statistics(
    connection: sqlite3.Connection, session_id: int
) -> tuple[object, ...] | None:
    scalar_columns = ", ".join(_SESSION_PROJECTION_SCALAR_COLUMNS)
    return connection.execute(
        f"SELECT id, rodex_sessions_id, statistics_revision, "
        "statistics_projection_schema_version, calculated_at_utc, coverage_state, "
        f"{scalar_columns} FROM {RODEX_SESSIONS_STATISTICS_TABLE} "
        "WHERE rodex_sessions_id = ?",
        (session_id,),
    ).fetchone()


def _select_statistics_distributions(
    connection: sqlite3.Connection, session_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT distribution_kind, observation_count, total, median, p75, p90, "
        f"p95, maximum FROM {RODEX_SESSIONS_STATISTICS_DISTRIBUTIONS_TABLE} "
        "WHERE rodex_sessions_id = ? ORDER BY distribution_kind",
        (session_id,),
    ).fetchall()


def _select_statistics_named_counts(
    connection: sqlite3.Connection, session_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT count_kind, count_name, occurrence_count "
        f"FROM {RODEX_SESSIONS_STATISTICS_NAMED_COUNTS_TABLE} "
        "WHERE rodex_sessions_id = ? ORDER BY count_kind, count_name",
        (session_id,),
    ).fetchall()


def _select_statistics_audit_limits(
    connection: sqlite3.Connection, session_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT limit_ordinal, limitation "
        f"FROM {RODEX_SESSIONS_STATISTICS_AUDIT_LIMITS_TABLE} "
        "WHERE rodex_sessions_id = ? ORDER BY limit_ordinal",
        (session_id,),
    ).fetchall()


def _select_turn_statistics_named_counts(
    connection: sqlite3.Connection, turn_row_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT count_kind, count_name, occurrence_count "
        f"FROM {RODEX_SESSIONS_STATISTICS_TURN_NAMED_COUNTS_TABLE} "
        "WHERE rodex_sessions_statistics_turns_id = ? "
        "ORDER BY count_kind, count_name",
        (turn_row_id,),
    ).fetchall()


def _select_statistics_worker(
    connection: sqlite3.Connection, session_id: int
) -> tuple[object, ...] | None:
    return connection.execute(
        f"SELECT id, rodex_sessions_id, worker_state, diagnostic_code, "
        "last_attempted_at_utc, consecutive_failures, next_retry_at_utc "
        f"FROM {RODEX_SESSIONS_STATISTICS_WORKERS_TABLE} "
        "WHERE rodex_sessions_id = ?",
        (session_id,),
    ).fetchone()


def _select_statistics_sources(
    connection: sqlite3.Connection, session_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT id, rodex_sessions_id, codex_session_uuid_int_1, "
        "codex_session_uuid_int_2, first_linked_at_utc, rollout_file_path, "
        "analyzed_size_bytes, analyzed_mtime_ns, analyzed_prefix_sha256, "
        "verified_at_utc, included_statistics_revision "
        f"FROM {RODEX_SESSIONS_STATISTICS_SOURCES_TABLE} "
        "WHERE rodex_sessions_id = ? ORDER BY id",
        (session_id,),
    ).fetchall()


def _upsert_statistics_worker(
    connection: sqlite3.Connection,
    session_id: int,
    *,
    worker_state: str,
    diagnostic_code: str | None,
    last_attempted_at_utc: str,
    consecutive_failures: int,
    next_retry_at_utc: str | None,
) -> None:
    connection.execute(
        f"INSERT INTO {RODEX_SESSIONS_STATISTICS_WORKERS_TABLE} "
        "(rodex_sessions_id, worker_state, diagnostic_code, last_attempted_at_utc, "
        "consecutive_failures, next_retry_at_utc) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(rodex_sessions_id) DO UPDATE SET "
        "worker_state = excluded.worker_state, "
        "diagnostic_code = excluded.diagnostic_code, "
        "last_attempted_at_utc = excluded.last_attempted_at_utc, "
        "consecutive_failures = excluded.consecutive_failures, "
        "next_retry_at_utc = excluded.next_retry_at_utc",
        (
            session_id,
            worker_state,
            diagnostic_code,
            last_attempted_at_utc,
            consecutive_failures,
            next_retry_at_utc,
        ),
    )


def _lookup_or_insert_rodex_sessions_user_id(
    connection: sqlite3.Connection, identity: RodexSessionsUserIdentity
) -> int:
    return select_or_insert_lookup_id(
        connection,
        RODEX_SESSIONS_USERS_TABLE,
        {"uid": identity.uid, "gid": identity.gid, "user_name": identity.user_name},
    )


def _apply_user_defined_cool_name_assignment(
    connection: sqlite3.Connection,
    session_cool_name: str,
    user_defined_cool_name: str,
    identity: RodexSessionsUserIdentity,
    *,
    force: bool,
    mutate: bool,
    renamed_tmux_session_name: str | None,
) -> RodexSessionNames:
    normalised_alias = normalise_rodex_display_name(user_defined_cool_name)
    requested_session_name = lookup_cool_name(connection, session_cool_name)
    if requested_session_name is None:
        raise RodexSessionError(f"Rodex session does not exist: {session_cool_name}")
    session_rows = _select_sessions_and_owners_by_cool_names_id(
        connection, requested_session_name.id
    )
    if not session_rows:
        raise RodexSessionError(f"Rodex session does not exist: {session_cool_name}")
    if len(session_rows) > 1:
        raise RodexSessionError(
            f"cool name resolves to multiple sessions: {session_cool_name}"
        )
    session_row = session_rows[0]
    user_id = _lookup_rodex_sessions_user_id(connection, identity)
    if user_id is None or int(session_row[5]) != user_id:
        raise RodexSessionError(
            f"Rodex session is not owned by the current user: {session_cool_name}"
        )

    existing_alias_id = None if session_row[2] is None else int(session_row[2])
    candidate_alias = lookup_cool_name(connection, normalised_alias)
    if candidate_alias is None or existing_alias_id != candidate_alias.id:
        if existing_alias_id is not None and not force:
            raise RodexSessionError(
                f"Rodex session already has user-defined name {session_row[4]!r}; "
                "use --force to replace it"
            )
        if candidate_alias is not None:
            owners = _select_session_ids_by_cool_names_id(connection, candidate_alias.id)
            if any(int(owner[0]) != int(session_row[0]) for owner in owners):
                raise RodexSessionError(
                    f"Rodex name already belongs to another session: {normalised_alias}"
                )

    planned_names = RodexSessionNames(
        rodex_sessions_id=int(session_row[0]),
        cool_name=str(session_row[3]),
        user_defined_cool_name=normalised_alias,
    )
    if not mutate:
        return planned_names

    allocated_alias = reserve_specific_cool_name(connection, normalised_alias)
    owners = _select_session_ids_by_cool_names_id(connection, allocated_alias.id)
    if any(int(owner[0]) != int(session_row[0]) for owner in owners):
        raise RodexSessionError(
            f"Rodex name already belongs to another session: {normalised_alias}"
        )
    cursor = connection.execute(
        f"UPDATE {RODEX_SESSIONS_TABLE} SET user_defined_cool_names_id = ? WHERE id = ?",
        (allocated_alias.id, int(session_row[0])),
    )
    if cursor.rowcount != 1:
        raise RodexSessionError(f"Rodex session disappeared: {int(session_row[0])}")
    if renamed_tmux_session_name is not None:
        tmux_cursor = connection.execute(
            f"UPDATE {RODEX_TMUX_SESSIONS_TABLE} SET tmux_session_name = ? "
            "WHERE rodex_sessions_id = ?",
            (renamed_tmux_session_name, int(session_row[0])),
        )
        if tmux_cursor.rowcount != 1:
            raise RodexSessionError(
                f"Rodex tmux session does not exist: {int(session_row[0])}"
            )
    return planned_names


def _select_sessions_and_owners_by_cool_names_id(
    connection: sqlite3.Connection, cool_names_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT sessions.id, sessions.cool_names_id, "
        "sessions.user_defined_cool_names_id, permanent.cool_name, "
        "user_defined.cool_name, log.rodex_sessions_users_id "
        f"FROM {RODEX_SESSIONS_TABLE} AS sessions "
        "JOIN cool_names AS permanent ON permanent.id = sessions.cool_names_id "
        "LEFT JOIN cool_names AS user_defined "
        "ON user_defined.id = sessions.user_defined_cool_names_id "
        f"JOIN {RODEX_SESSIONS_LOG_TABLE} AS log "
        "ON log.rodex_sessions_id = sessions.id "
        "WHERE sessions.cool_names_id = ? "
        "OR sessions.user_defined_cool_names_id = ? ORDER BY sessions.id LIMIT 2",
        (cool_names_id, cool_names_id),
    ).fetchall()


def _select_session_ids_by_cool_names_id(
    connection: sqlite3.Connection, cool_names_id: int
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"SELECT id FROM {RODEX_SESSIONS_TABLE} "
        "WHERE cool_names_id = ? OR user_defined_cool_names_id = ? "
        "ORDER BY id LIMIT 2",
        (cool_names_id, cool_names_id),
    ).fetchall()


def _lookup_rodex_sessions_user_id(
    connection: sqlite3.Connection, identity: RodexSessionsUserIdentity
) -> int | None:
    return select_lookup_id(
        connection,
        RODEX_SESSIONS_USERS_TABLE,
        {"uid": identity.uid, "gid": identity.gid, "user_name": identity.user_name},
    )


def _resolve_user_identity(
    user_identity: RodexSessionsUserIdentity | None,
) -> RodexSessionsUserIdentity:
    return (
        current_rodex_sessions_user_identity()
        if user_identity is None
        else _validate_user_identity(user_identity)
    )


def _select_rodex_session_names(
    connection: sqlite3.Connection, session_id: int
) -> tuple[object, ...] | None:
    return connection.execute(
        f"SELECT sessions.id, permanent.cool_name, user_defined.cool_name "
        f"FROM {RODEX_SESSIONS_TABLE} AS sessions "
        "JOIN cool_names AS permanent ON permanent.id = sessions.cool_names_id "
        "LEFT JOIN cool_names AS user_defined "
        "ON user_defined.id = sessions.user_defined_cool_names_id "
        "WHERE sessions.id = ?",
        (session_id,),
    ).fetchone()


def _select_rodex_tmux_session(
    connection: sqlite3.Connection, session_id: int
) -> RodexTmuxSession | None:
    row = connection.execute(
        f"SELECT id, rodex_sessions_id, tmux_server_socket_path, tmux_session_name "
        f"FROM {RODEX_TMUX_SESSIONS_TABLE} WHERE rodex_sessions_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return RodexTmuxSession(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        tmux_server_socket_path=str(row[2]),
        tmux_session_name=str(row[3]),
    )


def _session_names_from_row(row: tuple[object, ...]) -> RodexSessionNames:
    return RodexSessionNames(
        rodex_sessions_id=int(row[0]),
        cool_name=str(row[1]),
        user_defined_cool_name=None if row[2] is None else str(row[2]),
    )


def _session_statistics_from_rows(
    row: tuple[object, ...],
    distribution_rows: Sequence[tuple[object, ...]],
    named_count_rows: Sequence[tuple[object, ...]],
    audit_limit_rows: Sequence[tuple[object, ...]],
) -> RodexSessionStatistics:
    scalar_values = dict(zip(_SESSION_PROJECTION_SCALAR_COLUMNS, row[6:], strict=True))
    distributions = tuple(
        StatisticsDistribution(
            distribution_kind=str(item[0]),
            observation_count=int(item[1]),
            total=int(item[2]),
            median=None if item[3] is None else float(item[3]),
            p75=None if item[4] is None else int(item[4]),
            p90=None if item[5] is None else int(item[5]),
            p95=None if item[6] is None else int(item[6]),
            maximum=None if item[7] is None else int(item[7]),
        )
        for item in distribution_rows
    )
    named_counts = _named_counts_from_rows(named_count_rows)
    expected_ordinals = tuple(range(len(audit_limit_rows)))
    actual_ordinals = tuple(int(item[0]) for item in audit_limit_rows)
    if actual_ordinals != expected_ordinals:
        raise RodexSessionError("stored statistics audit limits are not contiguous")
    projection = SessionStatisticsProjection(
        **scalar_values,
        distributions=distributions,
        named_counts=named_counts,
        audit_limits=tuple(str(item[1]) for item in audit_limit_rows),
        turn_statistics=(),
    )
    return RodexSessionStatistics(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        statistics_revision=int(row[2]),
        statistics_projection_schema_version=str(row[3]),
        calculated_at_utc=str(row[4]),
        coverage_state=str(row[5]),
        projection=projection,
    )


def _statistics_source_from_row(
    row: tuple[object, ...],
) -> RodexSessionStatisticsSource:
    return RodexSessionStatisticsSource(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        codex_session_uuid=join_signed_bigints_into_a_codex_uuid(int(row[2]), int(row[3])),
        first_linked_at_utc=str(row[4]),
        rollout_file_path=None if row[5] is None else str(row[5]),
        analyzed_size_bytes=None if row[6] is None else int(row[6]),
        analyzed_mtime_ns=None if row[7] is None else int(row[7]),
        analyzed_prefix_sha256=None if row[8] is None else str(row[8]),
        verified_at_utc=None if row[9] is None else str(row[9]),
        included_statistics_revision=None if row[10] is None else int(row[10]),
    )


def _turn_statistics_from_rows(
    row: tuple[object, ...],
    named_count_rows: Sequence[tuple[object, ...]],
) -> RodexSessionTurnStatistics:
    values = dict(zip(_TURN_DATABASE_SCALAR_COLUMNS, row[10:], strict=True))
    for key in (
        "hands_on",
        "completed_after_nonzero_command",
        "edited_then_verified",
        "web_research_followed_by_command_or_file_work",
    ):
        values[key] = bool(values[key])
    projection = TurnStatisticsProjection(
        codex_session_uuid=join_signed_bigints_into_a_codex_uuid(int(row[3]), int(row[4])),
        codex_turn_id=str(row[5]),
        started_at_utc=None if row[7] is None else str(row[7]),
        terminal_at_utc=None if row[8] is None else str(row[8]),
        outcome=str(row[9]),
        **values,
        named_counts=_named_counts_from_rows(named_count_rows),
    )
    return RodexSessionTurnStatistics(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        rodex_sessions_statistics_sources_id=int(row[2]),
        included_statistics_revision=int(row[6]),
        projection=projection,
    )


def _named_counts_from_rows(
    rows: Sequence[tuple[object, ...]],
) -> tuple[StatisticsNamedCount, ...]:
    return tuple(
        StatisticsNamedCount(
            count_kind=str(row[0]),
            count_name=str(row[1]),
            occurrence_count=int(row[2]),
        )
        for row in rows
    )


def _statistics_worker_from_row(
    row: tuple[object, ...],
) -> RodexSessionStatisticsWorker:
    return RodexSessionStatisticsWorker(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        worker_state=str(row[2]),
        diagnostic_code=None if row[3] is None else str(row[3]),
        last_attempted_at_utc=str(row[4]),
        consecutive_failures=int(row[5]),
        next_retry_at_utc=None if row[6] is None else str(row[6]),
    )


def _validate_user_identity(
    identity: RodexSessionsUserIdentity,
) -> RodexSessionsUserIdentity:
    if not isinstance(identity, RodexSessionsUserIdentity):
        raise TypeError("user_identity must be a RodexSessionsUserIdentity")
    if (
        not isinstance(identity.uid, int)
        or isinstance(identity.uid, bool)
        or identity.uid < 0
    ):
        raise ValueError("uid must be a non-negative integer")
    if (
        not isinstance(identity.gid, int)
        or isinstance(identity.gid, bool)
        or identity.gid < 0
    ):
        raise ValueError("gid must be a non-negative integer")
    if not isinstance(identity.user_name, str) or not identity.user_name.strip():
        raise ValueError("user_name must be a non-empty string")
    return RodexSessionsUserIdentity(
        uid=identity.uid,
        gid=identity.gid,
        user_name=identity.user_name.strip(),
    )


def _utc_now_timestamp() -> str:
    return _normalise_utc_datetime(datetime.now(UTC))


def _normalise_required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _normalise_utc_timestamp_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("calculated_at_utc must be a non-empty UTC timestamp")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("calculated_at_utc must be a valid UTC timestamp") from error
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("calculated_at_utc must be timezone-aware")
    return _normalise_utc_datetime(instant)


def _normalise_utc_datetime(value: datetime | None) -> str:
    instant = datetime.now(UTC) if value is None else value
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("UTC timestamps must be timezone-aware")
    return instant.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_session_id(session_id: int) -> None:
    _validate_positive_id(session_id, "session_id")


def _validate_positive_id(value: int, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _session_log_from_row(row: tuple[object, ...]) -> RodexSessionLog:
    return RodexSessionLog(
        id=int(row[0]),
        rodex_sessions_id=int(row[1]),
        created_at_utc=str(row[2]),
        rodex_sessions_users_id=int(row[3]),
        last_accessed_at_utc=str(row[4]),
    )


def _parse_uuid(value: uuid.UUID | str, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a uuid.UUID or string")
    return uuid.UUID(value)


def _unsigned_half_to_signed(value: int) -> int:
    return value - _HALF_MODULUS if value >= _HALF_SIGN_BIT else value


def _signed_half_to_unsigned(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("UUID halves must be integers")
    if not _SIGNED_BIGINT_MIN <= value <= _SIGNED_BIGINT_MAX:
        raise ValueError("UUID halves must fit a signed 64-bit SQLite integer")
    return value + _HALF_MODULUS if value < 0 else value
