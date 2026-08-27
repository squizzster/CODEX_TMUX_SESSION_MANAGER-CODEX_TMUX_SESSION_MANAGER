from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import rodex.application_pipeline as pipeline_module
from rodex.application_pipeline import (
    ROUTE_PREPARATIONS,
    PipelinePreparation,
    UnifiedRodexApplicationPipeline,
    select_rodex_invocation,
)
from rodex.command_contract import (
    COMMAND_SPECS,
    WAIT_COMMAND,
    ClassifiedRodexCommand,
    CommandRoute,
)
from rodex.errors import RodexExecutableNotFoundError
from rodex.managed_session_lifecycle import (
    OwnedSessionSelection,
    SelectorExecution,
    SessionSelection,
    UnregisteredCodexSessionSelection,
)
from rodex_registry import parse_codex_session_id


@pytest.mark.parametrize(
    ("arguments", "normalized", "route", "preparation"),
    [
        ([], ("_create",), CommandRoute.LAUNCH, PipelinePreparation.RUNTIME),
        (["_help"], ("_help",), CommandRoute.HELP, PipelinePreparation.DIRECT),
        (["_stats"], ("_stats",), CommandRoute.STATISTICS, PipelinePreparation.DIRECT),
        (["_cat"], ("_cat",), CommandRoute.SESSION, PipelinePreparation.RUNTIME),
        (["_tail"], ("_tail",), CommandRoute.SESSION, PipelinePreparation.RUNTIME),
        (
            ["_wait", "worker"],
            ("_wait", "worker"),
            CommandRoute.SESSION,
            PipelinePreparation.RUNTIME,
        ),
        (
            ["_wait", "worker", "--turn", "turn-1", "--json"],
            ("_wait", "worker", "--turn", "turn-1", "--json"),
            CommandRoute.MACHINE,
            PipelinePreparation.RUNTIME,
        ),
        (["_inspect"], ("_inspect",), CommandRoute.MACHINE, PipelinePreparation.RUNTIME),
        (
            ["worker"],
            ("worker",),
            CommandRoute.SELECTOR,
            PipelinePreparation.SELECTOR,
        ),
        (
            ["_head", "worker"],
            ("_head", "worker"),
            CommandRoute.CODEX,
            PipelinePreparation.DIRECT,
        ),
        (["--version"], ("--version",), CommandRoute.CODEX, PipelinePreparation.DIRECT),
        (
            ["--model", "example", "initial prompt"],
            ("--model", "example", "initial prompt"),
            CommandRoute.MANAGED_CODEX,
            PipelinePreparation.RUNTIME,
        ),
        (["exec"], ("exec",), CommandRoute.CODEX, PipelinePreparation.DIRECT),
        (
            ["--future-codex-option"],
            ("--future-codex-option",),
            CommandRoute.CODEX,
            PipelinePreparation.DIRECT,
        ),
    ],
)
def test_invocation_selection_is_single_typed_and_exhaustive(
    arguments: list[str],
    normalized: tuple[str, ...],
    route: CommandRoute,
    preparation: PipelinePreparation,
) -> None:
    invocation = select_rodex_invocation(arguments)

    assert invocation.arguments == normalized
    assert invocation.route is route
    assert invocation.preparation is preparation


def test_every_declared_route_has_one_preparation_and_every_command_uses_it() -> None:
    assert set(ROUTE_PREPARATIONS) == set(CommandRoute)

    for spec in COMMAND_SPECS:
        invocation = select_rodex_invocation([spec.token])
        assert invocation.route is spec.route
        assert invocation.preparation is ROUTE_PREPARATIONS[spec.route]

    exact_wait = select_rodex_invocation(
        [WAIT_COMMAND, "worker", "--turn", "turn-1", "--json"]
    )
    assert exact_wait.route is CommandRoute.MACHINE
    assert exact_wait.preparation is PipelinePreparation.RUNTIME


def _pipeline(
    tmp_path: Path,
    trace: list[object],
    *,
    selected_session: SessionSelection | None = None,
    selector_outcome: SelectorExecution = SelectorExecution.OPENED,
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

    class FakeSessionLifecycle:
        def resolve_selector(
            self, selector: str, database_path: Path
        ) -> SessionSelection | None:
            trace.append(("selector_resolver", selector, database_path))
            return selected_session

        def execute_selector(
            self,
            selection: SessionSelection,
            database_path: Path,
            launcher: Any,
            *,
            codex_available: bool,
            configured_codex: str,
        ) -> SelectorExecution:
            trace.append(
                (
                    "selector",
                    selection,
                    database_path,
                    launcher,
                    codex_available,
                    configured_codex,
                )
            )
            return selector_outcome

        def execute_launch(
            self,
            arguments: list[str],
            database_path: Path,
            launcher: Any,
            *,
            codex_binary: str | None,
            configured_codex: str,
        ) -> int:
            trace.append(
                (
                    "launch",
                    tuple(arguments),
                    database_path,
                    launcher,
                    codex_binary,
                    configured_codex,
                )
            )
            return 19

        def execute_managed_interactive(
            self,
            codex_arguments: tuple[str, ...],
            database_path: Path,
            launcher: Any,
            *,
            codex_binary: str | None,
            configured_codex: str,
        ) -> int:
            trace.append(
                (
                    "managed_codex",
                    codex_arguments,
                    database_path,
                    launcher,
                    codex_binary,
                    configured_codex,
                )
            )
            return 29

    return UnifiedRodexApplicationPipeline(
        database_path=tmp_path / "rodex.sqlite3",
        configured_codex="codex",
        configured_tmux="tmux",
        launcher="launcher",  # type: ignore[arg-type]
        control_client="control",  # type: ignore[arg-type]
        codex_delegator=delegate,
        resolve_executable=resolve_executable,
        runtime_launcher_factory=lambda _codex, _tmux: "new launcher",  # type: ignore[return-value]
        session_lifecycle=FakeSessionLifecycle(),
    )


def test_statistics_route_uses_database_context_without_acquiring_runtime(
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
        (["--model", "example", "initial prompt"], "managed_codex", 29),
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
            if entry[0]  # type: ignore[index]
            in {
                "session",
                "machine",
                "launch",
                "managed_codex",
                "statistics",
                "codex",
            }
        }
        - {executor_name}
    )


def test_selector_resolves_before_runtime_and_never_probes_another_domain(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    database.touch()
    trace: list[object] = []

    selection = OwnedSessionSelection("worker", 41)

    assert _pipeline(tmp_path, trace, selected_session=selection).execute(["worker"]) == 0
    assert trace == [
        ("selector_resolver", "worker", database),
        ("resolve_executable", "tmux"),
        ("resolve_executable", "codex"),
        (
            "selector",
            selection,
            database,
            "launcher",
            True,
            "codex",
        ),
    ]


def test_unmatched_selector_becomes_one_managed_initial_prompt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    database.touch()
    trace: list[object] = []

    assert _pipeline(tmp_path, trace).execute(["worker"]) == 29
    assert trace == [
        ("selector_resolver", "worker", database),
        ("resolve_executable", "tmux"),
        ("resolve_executable", "codex"),
        (
            "managed_codex",
            ("worker",),
            database,
            "launcher",
            "/bin/codex",
            "codex",
        ),
    ]


def test_unregistered_codex_uuid_can_become_a_managed_prompt_after_runtime_probe(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rodex.sqlite3"
    trace: list[object] = []
    selector = "01a015f4-f27c-7592-8060-d12313e8d0ce"
    selection = UnregisteredCodexSessionSelection(
        selector, parse_codex_session_id(selector)
    )

    assert (
        _pipeline(
            tmp_path,
            trace,
            selected_session=selection,
            selector_outcome=SelectorExecution.MANAGED_PROMPT,
        ).execute([selector])
        == 29
    )

    assert trace == [
        ("selector_resolver", selector, database),
        ("resolve_executable", "tmux"),
        ("resolve_executable", "codex"),
        (
            "selector",
            selection,
            database,
            "launcher",
            True,
            "codex",
        ),
        (
            "managed_codex",
            (selector,),
            database,
            "launcher",
            "/bin/codex",
            "codex",
        ),
    ]


def test_codex_passthrough_never_touches_database_or_tmux(tmp_path: Path) -> None:
    trace: list[object] = []

    assert _pipeline(tmp_path, trace).execute(["--version"]) == 17
    assert trace == [
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
        ("resolve_executable", "codex"),
    ]


def test_machine_execution_receives_the_single_classified_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace: list[object] = []
    classifications: list[ClassifiedRodexCommand | None] = []
    executed_specs: list[object] = []
    real_classifier = pipeline_module.classify_rodex_command

    def classify(arguments: tuple[str, ...]) -> ClassifiedRodexCommand | None:
        result = real_classifier(arguments)
        classifications.append(result)
        return result

    def execute_machine(
        arguments: list[str],
        spec: object,
        database_path: Path,
        launcher: object,
        control_client: object,
    ) -> int:
        executed_specs.append(spec)
        return 0

    monkeypatch.setattr(pipeline_module, "classify_rodex_command", classify)
    monkeypatch.setattr(pipeline_module, "execute_machine_command", execute_machine)

    assert _pipeline(tmp_path, trace).execute(["_inspect", "worker", "--json"]) == 0
    assert len(classifications) == 1
    classification = classifications[0]
    assert classification is not None
    assert executed_specs == [classification.machine_spec]
