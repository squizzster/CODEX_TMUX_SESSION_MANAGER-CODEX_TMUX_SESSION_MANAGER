from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import rodex.application_pipeline as pipeline_module
from rodex.application_pipeline import (
    ROUTE_REQUIREMENTS,
    PipelineRequirement,
    UnifiedRodexApplicationPipeline,
    select_rodex_invocation,
)
from rodex.command_contract import (
    COMMAND_SPECS,
    WAIT_COMMAND,
    CommandRoute,
)
from rodex.errors import RodexExecutableNotFoundError


@pytest.mark.parametrize(
    ("arguments", "normalized", "route", "requirement"),
    [
        ([], ("_create",), CommandRoute.LAUNCH, PipelineRequirement.RUNTIME),
        (["_help"], ("_help",), CommandRoute.HELP, PipelineRequirement.NONE),
        (["_stats"], ("_stats",), CommandRoute.STATISTICS, PipelineRequirement.DATABASE),
        (["_cat"], ("_cat",), CommandRoute.SESSION, PipelineRequirement.RUNTIME),
        (
            ["_wait", "worker"],
            ("_wait", "worker"),
            CommandRoute.SESSION,
            PipelineRequirement.RUNTIME,
        ),
        (
            ["_wait", "worker", "--turn", "turn-1", "--json"],
            ("_wait", "worker", "--turn", "turn-1", "--json"),
            CommandRoute.MACHINE,
            PipelineRequirement.RUNTIME,
        ),
        (["_inspect"], ("_inspect",), CommandRoute.MACHINE, PipelineRequirement.RUNTIME),
        (["worker"], ("worker",), CommandRoute.SELECTOR, PipelineRequirement.DATABASE),
        (
            ["_head", "worker"],
            ("_head", "worker"),
            CommandRoute.CODEX,
            PipelineRequirement.NONE,
        ),
        (["--version"], ("--version",), CommandRoute.CODEX, PipelineRequirement.NONE),
    ],
)
def test_invocation_selection_is_single_typed_and_exhaustive(
    arguments: list[str],
    normalized: tuple[str, ...],
    route: CommandRoute,
    requirement: PipelineRequirement,
) -> None:
    invocation = select_rodex_invocation(arguments)

    assert invocation.arguments == normalized
    assert invocation.route is route
    assert invocation.requirement is requirement


def test_every_declared_route_has_one_requirement_and_every_command_uses_it() -> None:
    assert set(ROUTE_REQUIREMENTS) == set(CommandRoute)

    for spec in COMMAND_SPECS:
        invocation = select_rodex_invocation([spec.token])
        assert invocation.route is spec.route
        assert invocation.requirement is ROUTE_REQUIREMENTS[spec.route]

    exact_wait = select_rodex_invocation(
        [WAIT_COMMAND, "worker", "--turn", "turn-1", "--json"]
    )
    assert exact_wait.route is CommandRoute.MACHINE
    assert exact_wait.requirement is PipelineRequirement.RUNTIME


def _pipeline(
    tmp_path: Path,
    trace: list[object],
    *,
    selector_exists: bool = False,
    available: dict[str, str | None] | None = None,
) -> UnifiedRodexApplicationPipeline:
    executables = (
        {"codex": "/bin/codex", "tmux": "/bin/tmux"} if available is None else available
    )

    def resolve_executable(name: str) -> str | None:
        trace.append(("resolve_executable", name))
        return executables.get(name)

    def delegate(binary: str, arguments: list[str] | tuple[str, ...]) -> int:
        trace.append(("codex", binary, tuple(arguments)))
        return 17

    def guard(arguments: list[str], configured_codex: str) -> None:
        trace.append(("collision_guard", tuple(arguments), configured_codex))

    def resolve_selector(selector: str, database_path: Path) -> bool:
        trace.append(("selector_resolver", selector, database_path))
        return selector_exists

    def execute_selector(
        selector: str,
        database_path: Path,
        launcher: Any,
        *,
        codex_available: bool,
    ) -> int:
        trace.append(
            (
                "selector",
                selector,
                database_path,
                launcher,
                codex_available,
            )
        )
        return 18

    def execute_launch(
        arguments: list[str],
        database_path: Path,
        launcher: Any,
        *,
        codex_binary: str | None,
    ) -> int:
        trace.append(("launch", tuple(arguments), database_path, launcher, codex_binary))
        return 19

    return UnifiedRodexApplicationPipeline(
        database_path=tmp_path / "rodex.sqlite3",
        configured_codex="codex",
        configured_tmux="tmux",
        launcher="launcher",  # type: ignore[arg-type]
        control_client="control",  # type: ignore[arg-type]
        codex_delegator=delegate,
        resolve_executable=resolve_executable,
        collision_guard=guard,
        selector_resolver=resolve_selector,
        selector_executor=execute_selector,
        launch_executor=execute_launch,
    )


def test_database_route_does_not_acquire_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace: list[object] = []
    monkeypatch.setattr(
        pipeline_module,
        "execute_statistics_command",
        lambda arguments, database: trace.append(
            ("statistics", tuple(arguments), database)
        ),
    )

    assert _pipeline(tmp_path, trace).execute(["_stats", "worker"]) == 0
    assert trace == [("statistics", ("_stats", "worker"), tmp_path / "rodex.sqlite3")]


@pytest.mark.parametrize(
    ("arguments", "executor_name", "exit_code"),
    [
        (["_cat", "worker"], "session", 0),
        (["_inspect", "worker", "--json"], "machine", 23),
        (["_create"], "launch", 19),
    ],
)
def test_runtime_routes_acquire_once_and_execute_exactly_one_domain(
    arguments: list[str],
    executor_name: str,
    exit_code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[object] = []

    def execute_session(*values: object) -> None:
        trace.append(("session", *values))

    def execute_machine(*values: object) -> int:
        trace.append(("machine", *values))
        return 23

    monkeypatch.setattr(pipeline_module, "execute_session_command", execute_session)
    monkeypatch.setattr(pipeline_module, "execute_machine_command", execute_machine)

    assert _pipeline(tmp_path, trace).execute(arguments) == exit_code
    assert trace[:2] == [
        ("resolve_executable", "tmux"),
        ("resolve_executable", "codex"),
    ]
    assert len([entry for entry in trace if entry[0] == executor_name]) == 1  # type: ignore[index]
    assert (
        not {
            entry[0]  # type: ignore[index]
            for entry in trace[2:]
            if entry[0] in {"session", "machine", "launch", "statistics", "codex"}  # type: ignore[index]
        }
        - {executor_name}
    )


def test_selector_resolves_before_runtime_and_never_probes_another_domain(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    database.touch()
    trace: list[object] = []

    assert _pipeline(tmp_path, trace, selector_exists=True).execute(["worker"]) == 18
    assert trace == [
        ("selector_resolver", "worker", database),
        ("resolve_executable", "tmux"),
        ("resolve_executable", "codex"),
        ("selector", "worker", database, "launcher", True),
    ]


def test_unmatched_selector_returns_to_the_codex_route_without_tmux(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    database.touch()
    trace: list[object] = []

    assert _pipeline(tmp_path, trace).execute(["worker"]) == 17
    assert trace == [
        ("selector_resolver", "worker", database),
        ("collision_guard", ("worker",), "codex"),
        ("resolve_executable", "codex"),
        ("codex", "/bin/codex", ("worker",)),
    ]


def test_codex_passthrough_never_touches_database_or_tmux(tmp_path: Path) -> None:
    trace: list[object] = []

    assert _pipeline(tmp_path, trace).execute(["--version"]) == 17
    assert trace == [
        ("collision_guard", ("--version",), "codex"),
        ("resolve_executable", "codex"),
        ("codex", "/bin/codex", ("--version",)),
    ]


def test_missing_tmux_is_rendered_as_a_machine_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace: list[object] = []

    def print_error(*arguments: object, **fields: object) -> None:
        trace.append(("machine_error", arguments, fields))

    monkeypatch.setattr(pipeline_module, "print_machine_error", print_error)

    status = _pipeline(
        tmp_path,
        trace,
        available={"codex": "/bin/codex"},
    ).execute(["_inspect", "worker", "--json"])

    assert status == 3
    assert trace[0] == ("resolve_executable", "tmux")
    assert trace[1][0] == "machine_error"  # type: ignore[index]
    assert trace[1][1][:3] == (  # type: ignore[index]
        "thread.inspect",
        "runtime_unavailable",
        "tmux executable was not found: tmux",
    )


def test_missing_codex_fails_passthrough_without_consulting_tmux(tmp_path: Path) -> None:
    trace: list[object] = []

    with pytest.raises(
        RodexExecutableNotFoundError,
        match="Codex executable was not found: codex",
    ):
        _pipeline(tmp_path, trace, available={"tmux": "/bin/tmux"}).execute(["--version"])

    assert trace == [
        ("collision_guard", ("--version",), "codex"),
        ("resolve_executable", "codex"),
    ]
