from __future__ import annotations

import ast
from pathlib import Path

import rodex_registry
import rodex_sql

SOURCE_ROOT = Path(__file__).parents[1] / "src"
REGISTRY_ROOT = Path(__file__).parents[1] / "src" / "rodex_registry"
RODEX_ROOT = REGISTRY_ROOT.parent / "rodex"
SQL_ROOT = SOURCE_ROOT / "rodex_sql"


def _relative_imports(module_name: str) -> set[str]:
    tree = ast.parse((REGISTRY_ROOT / f"{module_name}.py").read_text(encoding="utf-8"))
    return {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 0
    }


def _function_calls(module_name: str, function_name: str) -> set[str]:
    tree = ast.parse((REGISTRY_ROOT / f"{module_name}.py").read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    return {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def _top_level_callers(call_name: str) -> set[tuple[str, str]]:
    callers: set[tuple[str, str]] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == call_name
                for call in ast.walk(node)
            ):
                callers.add((path.relative_to(SOURCE_ROOT).as_posix(), node.name))
    return callers


def test_registry_has_one_way_domain_dependencies_without_the_old_monolith() -> None:
    assert not (REGISTRY_ROOT / "sessions.py").exists()
    assert not (REGISTRY_ROOT / "agent_trace.py").exists()
    assert not (REGISTRY_ROOT.parent / "rodex_functions").exists()

    assert _relative_imports("identity") == set()
    assert _relative_imports("errors") == set()
    assert _relative_imports("validation") == set()
    assert _relative_imports("schema") == {"errors", "identity", "statistics_fields"}
    assert _relative_imports("execution") == {
        "errors",
        "identity",
        "schema",
        "validation",
    }
    assert _relative_imports("agent_trace_contract") == {
        "identity",
        "validation",
    }
    assert _relative_imports("agent_request_reconciliation") == {
        "errors",
        "identity",
        "schema",
    }
    assert _relative_imports("agent_trace_writer") == {
        "agent_request_reconciliation",
        "agent_trace_contract",
        "errors",
        "execution",
        "identity",
        "schema",
        "validation",
    }
    assert _relative_imports("agent_trace_reader") == {
        "agent_trace_contract",
        "identity",
        "schema",
        "validation",
    }
    assert _relative_imports("statistics") == {
        "agent_trace_contract",
        "agent_trace_writer",
        "errors",
        "execution",
        "identity",
        "schema",
        "statistics_fields",
        "statistics_projection",
        "validation",
    }
    assert _relative_imports("statistics_fields") == {"statistics_projection"}
    assert _relative_imports("analytics_registry") == {
        "agent_trace_contract",
        "errors",
        "identity",
        "statistics",
        "statistics_projection",
        "validation",
    }
    assert _relative_imports("lifecycle") == {
        "errors",
        "execution",
        "identity",
        "schema",
        "validation",
    }
    assert "statistics" not in _relative_imports("lifecycle")
    assert "statistics" not in _relative_imports("execution")


def test_registry_modules_do_not_import_their_public_facade() -> None:
    for module_name in (
        "agent_request_reconciliation",
        "agent_trace_contract",
        "agent_trace_reader",
        "agent_trace_writer",
        "analytics_registry",
        "errors",
        "execution",
        "identity",
        "lifecycle",
        "schema",
        "statistics",
        "statistics_fields",
        "statistics_projection",
        "validation",
    ):
        tree = ast.parse((REGISTRY_ROOT / f"{module_name}.py").read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "rodex_registry" not in imported


def test_runtime_analytics_uses_the_single_registry_boundary() -> None:
    tree = ast.parse((RODEX_ROOT / "analytics.py").read_text(encoding="utf-8"))
    imported_registry_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "rodex_registry"
        for alias in node.names
    }

    assert "RodexAnalyticsRegistry" in imported_registry_names
    assert "RodexAnalyticsPublication" in imported_registry_names
    assert not imported_registry_names.intersection(
        {
            "lookup_codex_session_id_from_a_rodex_sessions_id",
            "lookup_rodex_sessions_id_from_a_rodex_session_id",
            "publish_rodex_session_statistics",
            "read_rodex_session_statistics",
            "record_rodex_session_analytics_worker_health",
        }
    )


def test_registry_readers_use_only_the_canonical_read_transaction() -> None:
    lifecycle_readers = (
        "lookup_rodex_sessions_user",
        "lookup_rodex_sessions_id_from_a_rodex_session_id",
        "lookup_rodex_session_id_from_a_rodex_sessions_id",
        "lookup_rodex_session_log",
        "lookup_rodex_runtime_instance",
        "lookup_codex_session_id_from_a_rodex_sessions_id",
        "lookup_rodex_tmux_session",
        "lookup_rodex_sessions_id_from_a_cool_name",
        "lookup_owned_rodex_sessions_id_from_a_cool_name",
        "lookup_owned_rodex_sessions_id_from_a_codex_session_id",
        "lookup_rodex_session_names",
        "validate_a_user_defined_cool_name_assignment",
        "list_rodex_session_runtimes_for_a_user",
        "lookup_rodex_sessions_id_from_a_codex_session_id",
    )
    readers = (
        *(("lifecycle", name) for name in lifecycle_readers),
        ("schema", "lookup_rodex_registry_id"),
        ("statistics", "read_rodex_session_statistics"),
        ("statistics", "read_rodex_session_turn_statistics"),
        ("agent_trace_reader", "read_rodex_agent_trace"),
        ("execution", "list_rodex_session_codex_threads"),
    )

    for module_name, function_name in readers:
        calls = _function_calls(module_name, function_name)
        assert "initialise_rodex_database" not in calls
        assert "normalise_rodex_database_path" in calls
        assert "open_rodex_read_transaction" in calls


def test_sql_package_exposes_one_entry_for_each_storage_responsibility() -> None:
    assert {
        "open_rodex_bootstrap_transaction",
        "open_rodex_transaction",
        "open_rodex_read_transaction",
        "open_rodex_maintenance_lock",
        "require_active_rodex_transaction",
        "select_lookup_id",
        "select_or_insert_lookup_id",
    }.issubset(rodex_sql.__all__)
    assert not {
        "DatabaseLocationGuard",
        "database_location_guard",
        "prepare_rodex_database_path",
        "require_existing_rodex_database_path",
        "open_rodex_audit_transaction",
        "close_database_location_guards_for_testing",
        "database_terminal_signal",
        "subscribe_rodex_database_terminal",
    }.intersection(rodex_sql.__all__)
    assert "default_rodex_database_path" not in rodex_registry.__all__


def test_sqlite_connection_has_one_owner_and_no_background_admission() -> None:
    connect_owners: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sqlite3"
            and node.func.attr == "connect"
            for node in ast.walk(tree)
        ):
            connect_owners.add(relative)

    assert connect_owners == {"rodex_sql/transactions.py"}
    assert not (SQL_ROOT / "database_location_guard.py").exists()


def test_only_explicit_first_use_flows_can_bootstrap_storage() -> None:
    assert _top_level_callers("open_rodex_bootstrap_transaction") == {
        ("cool_name/functions.py", "get_unique_new_cool_name"),
        ("cool_name/functions.py", "initialise_cool_names_database"),
        ("rodex_registry/lifecycle.py", "create_a_rodex_session"),
        ("rodex_registry/lifecycle.py", "lookup_or_create_rodex_sessions_user"),
        ("rodex_registry/schema.py", "_bootstrap_or_audit_rodex_database"),
    }


def test_sql_storage_has_no_watcher_or_runtime_subscription_path() -> None:
    all_source = "\n".join(
        path.read_text(encoding="utf-8") for path in SOURCE_ROOT.rglob("*.py")
    )
    assert "close_database_location_guards_for_testing" not in all_source
    assert "require_existing_rodex_database_path" not in all_source
    assert "prepare_rodex_database_path" not in all_source
    assert "open_rodex_audit_transaction" not in all_source
    assert "DatabaseLocationGuard" not in all_source
    assert "database_location_guard" not in all_source
    assert "subscribe_rodex_database_terminal" not in all_source
    assert "inotify" not in all_source
    assert _top_level_callers("database_terminal_signal") == set()
