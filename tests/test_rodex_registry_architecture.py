from __future__ import annotations

import ast
from pathlib import Path

REGISTRY_ROOT = Path(__file__).parents[1] / "src" / "rodex_registry"


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


def test_registry_has_one_way_domain_dependencies_without_the_old_monolith() -> None:
    assert not (REGISTRY_ROOT / "sessions.py").exists()
    assert not (REGISTRY_ROOT.parent / "rodex_functions").exists()

    assert _relative_imports("identity") == set()
    assert _relative_imports("errors") == set()
    assert _relative_imports("validation") == set()
    assert _relative_imports("schema") == {"errors", "identity", "statistics_fields"}
    assert _relative_imports("statistics") == {
        "errors",
        "identity",
        "schema",
        "statistics_fields",
        "statistics_projection",
        "validation",
    }
    assert _relative_imports("statistics_fields") == {"statistics_projection"}
    assert _relative_imports("lifecycle") == {
        "errors",
        "identity",
        "schema",
        "statistics",
        "validation",
    }


def test_registry_modules_do_not_import_their_public_facade() -> None:
    for module_name in (
        "errors",
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


def test_registry_readers_use_only_the_existing_database_read_pipeline() -> None:
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
    )

    for module_name, function_name in readers:
        calls = _function_calls(module_name, function_name)
        assert "initialise_rodex_database" not in calls
        assert "existing_rodex_database_path" in calls
        assert "open_rodex_read_transaction" in calls
