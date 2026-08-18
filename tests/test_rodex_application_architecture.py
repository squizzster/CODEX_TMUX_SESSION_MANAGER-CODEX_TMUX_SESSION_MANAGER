from __future__ import annotations

import ast
from pathlib import Path

import pytest

import rodex.cli as cli_module


def test_cli_is_the_complete_thin_process_entrypoint() -> None:
    """The entry point composes the application; domains retain their own mechanisms."""
    cli_path = Path(cli_module.__file__)
    module = ast.parse(cli_path.read_text())

    functions = {node.name for node in module.body if isinstance(node, ast.FunctionDef)}
    classes = {node.name for node in module.body if isinstance(node, ast.ClassDef)}

    assert functions == {"_exec_codex", "run", "main"}
    assert classes == set()


def test_cli_enters_exactly_one_application_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructed: list[dict[str, object]] = []
    executions: list[tuple[str, ...]] = []

    class RecordingApplicationPipeline:
        def __init__(self, **dependencies: object) -> None:
            constructed.append(dependencies)

        def execute(self, arguments: list[str]) -> int:
            executions.append(tuple(arguments))
            return 29

    monkeypatch.setattr(
        cli_module,
        "UnifiedRodexApplicationPipeline",
        RecordingApplicationPipeline,
    )
    database = tmp_path / "rodex.sqlite3"

    assert cli_module.run(["_help"], database_path=database) == 29
    assert len(constructed) == 1
    assert constructed[0]["database_path"] == database
    assert executions == [("_help",)]
