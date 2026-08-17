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


def test_registry_has_one_way_domain_dependencies_without_the_old_monolith() -> None:
    assert not (REGISTRY_ROOT / "sessions.py").exists()
    assert not (REGISTRY_ROOT.parent / "rodex_functions").exists()

    assert _relative_imports("identity") == set()
    assert _relative_imports("errors") == set()
    assert _relative_imports("validation") == set()
    assert _relative_imports("schema") == {"errors", "identity"}
    assert _relative_imports("statistics") == {
        "errors",
        "identity",
        "schema",
        "statistics_projection",
        "validation",
    }
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
