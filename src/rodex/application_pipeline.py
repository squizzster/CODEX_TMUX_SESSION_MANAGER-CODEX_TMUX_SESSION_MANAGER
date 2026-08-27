"""The unified application control plane for every Rodex invocation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from codex_cli_contract import (
    CODEX_CLI_0_150_1,
    CodexCliContract,
    CodexCliInvocation,
    CodexCliRoute,
)

from .agent_trace_commands import execute_agent_trace_command
from .command_contract import (
    CREATE_COMMAND,
    HELP_COMMAND,
    HELP_TEXT,
    ClassifiedRodexCommand,
    CommandRoute,
    classify_rodex_command,
)
from .control import CodexControlClient
from .errors import RodexExecutableNotFoundError, RodexLaunchError
from .machine_commands import execute_machine_command, print_machine_error
from .managed_session_lifecycle import (
    SelectorExecution,
    SessionSelection,
)
from .runtime import RodexRuntimeLauncher
from .session_commands import execute_session_command
from .statistics_commands import execute_statistics_command

CodexDelegator = Callable[[str, Sequence[str]], int]
ExecutableResolver = Callable[[str], str | None]


class PipelinePreparation(StrEnum):
    """The preparation branch taken before one selected route can execute."""

    DIRECT = "direct"
    SELECTOR = "selector"
    RUNTIME = "runtime"


ROUTE_PREPARATIONS: Final = {
    CommandRoute.HELP: PipelinePreparation.DIRECT,
    CommandRoute.CODEX: PipelinePreparation.DIRECT,
    CommandRoute.STATISTICS: PipelinePreparation.DIRECT,
    CommandRoute.AGENT_TRACE: PipelinePreparation.DIRECT,
    CommandRoute.SELECTOR: PipelinePreparation.SELECTOR,
    CommandRoute.MANAGED_CODEX: PipelinePreparation.RUNTIME,
    CommandRoute.MACHINE: PipelinePreparation.RUNTIME,
    CommandRoute.SESSION: PipelinePreparation.RUNTIME,
    CommandRoute.LAUNCH: PipelinePreparation.RUNTIME,
}


RuntimeLauncherFactory = Callable[[str, str], RodexRuntimeLauncher]


class SessionLifecycle(Protocol):
    def resolve_selector(
        self,
        selector: str,
        database_path: Path,
    ) -> SessionSelection | None: ...

    def execute_selector(
        self,
        selection: SessionSelection,
        database_path: Path,
        launcher: RodexRuntimeLauncher,
        *,
        codex_available: bool,
        configured_codex: str,
    ) -> SelectorExecution: ...

    def execute_launch(
        self,
        arguments: list[str],
        database_path: Path,
        launcher: RodexRuntimeLauncher,
        *,
        codex_binary: str | None,
        configured_codex: str,
    ) -> int: ...

    def execute_managed_interactive(
        self,
        codex_arguments: tuple[str, ...],
        database_path: Path,
        launcher: RodexRuntimeLauncher,
        *,
        codex_binary: str | None,
        configured_codex: str,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class RodexInvocation:
    """One normalized invocation selected for exactly one application route."""

    arguments: tuple[str, ...]
    route: CommandRoute
    preparation: PipelinePreparation
    classification: ClassifiedRodexCommand | None = None
    codex_invocation: CodexCliInvocation | None = None


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Lazily acquired services shared by runtime-backed routes."""

    launcher: RodexRuntimeLauncher
    control_client: CodexControlClient
    codex_binary: str | None


@dataclass(frozen=True, slots=True)
class PreparedRodexInvocation:
    """One invocation after its declared preparation branch has completed."""

    invocation: RodexInvocation
    runtime: RuntimeServices | None
    selected_session: SessionSelection | None = None


def select_rodex_invocation(
    arguments: Sequence[str],
    codex_cli_contract: CodexCliContract = CODEX_CLI_0_150_1,
) -> RodexInvocation:
    """Normalize argv and select exactly one exhaustive application route."""
    normalized = tuple(arguments) if arguments else (CREATE_COMMAND,)
    classification = classify_rodex_command(normalized)
    if classification is not None:
        route = classification.route
        return RodexInvocation(
            normalized,
            route,
            ROUTE_PREPARATIONS[route],
            classification,
        )
    codex_invocation = codex_cli_contract.classify(normalized)
    if codex_invocation.route is CodexCliRoute.PASSTHROUGH:
        return RodexInvocation(
            normalized,
            CommandRoute.CODEX,
            PipelinePreparation.DIRECT,
            codex_invocation=codex_invocation,
        )
    if codex_invocation.selector_candidate is not None:
        return RodexInvocation(
            normalized,
            CommandRoute.SELECTOR,
            PipelinePreparation.SELECTOR,
            codex_invocation=codex_invocation,
        )
    return RodexInvocation(
        normalized,
        CommandRoute.MANAGED_CODEX,
        PipelinePreparation.RUNTIME,
        codex_invocation=codex_invocation,
    )


class UnifiedRodexApplicationPipeline:
    """Select, prepare, and execute every Rodex invocation."""

    def __init__(
        self,
        *,
        database_path: Path,
        configured_codex: str,
        configured_tmux: str,
        launcher: RodexRuntimeLauncher | None,
        control_client: CodexControlClient | None,
        codex_delegator: CodexDelegator,
        resolve_executable: ExecutableResolver,
        runtime_launcher_factory: RuntimeLauncherFactory,
        session_lifecycle: SessionLifecycle,
        codex_cli_contract: CodexCliContract = CODEX_CLI_0_150_1,
    ) -> None:
        self._database_path = database_path
        self._configured_codex = configured_codex
        self._configured_tmux = configured_tmux
        self._provided_launcher = launcher
        self._provided_control_client = control_client
        self._codex_delegator = codex_delegator
        self._resolve_executable = resolve_executable
        self._runtime_launcher_factory = runtime_launcher_factory
        self._session_lifecycle = session_lifecycle
        self._codex_cli_contract = codex_cli_contract

    def execute(self, arguments: Sequence[str]) -> int:
        """Run the one route selected for this invocation."""
        invocation = select_rodex_invocation(arguments, self._codex_cli_contract)
        prepared = self._prepare(invocation)
        if isinstance(prepared, int):
            return prepared
        invocation = prepared.invocation
        argv = list(invocation.arguments)

        if invocation.route is CommandRoute.HELP:
            return self._execute_help(argv)
        if invocation.route is CommandRoute.CODEX:
            return self._execute_codex(argv)
        if invocation.route is CommandRoute.STATISTICS:
            execute_statistics_command(argv, self._database_path)
            return 0
        if invocation.route is CommandRoute.AGENT_TRACE:
            execute_agent_trace_command(argv, self._database_path)
            return 0
        if invocation.route is CommandRoute.SELECTOR:
            services = prepared.runtime
            assert services is not None
            selection = prepared.selected_session
            if selection is None:
                return self._execute_managed_codex(invocation, services)
            outcome = self._session_lifecycle.execute_selector(
                selection,
                self._database_path,
                services.launcher,
                codex_available=services.codex_binary is not None,
                configured_codex=self._configured_codex,
            )
            if outcome is SelectorExecution.OPENED:
                return 0
            assert outcome is SelectorExecution.MANAGED_PROMPT
            return self._execute_managed_codex(invocation, services)

        services = prepared.runtime
        assert services is not None
        if invocation.route is CommandRoute.MACHINE:
            assert invocation.classification is not None
            machine_spec = invocation.classification.machine_spec
            assert machine_spec is not None
            return execute_machine_command(
                argv,
                machine_spec,
                self._database_path,
                services.launcher,
                services.control_client,
            )
        if invocation.route is CommandRoute.SESSION:
            execute_session_command(
                argv,
                self._database_path,
                services.launcher,
                services.control_client,
            )
            return 0
        if invocation.route is CommandRoute.LAUNCH:
            return self._session_lifecycle.execute_launch(
                argv,
                self._database_path,
                services.launcher,
                codex_binary=services.codex_binary,
                configured_codex=self._configured_codex,
            )
        if invocation.route is CommandRoute.MANAGED_CODEX:
            return self._execute_managed_codex(invocation, services)
        raise AssertionError(  # pragma: no cover - route map and branches are exhaustive.
            f"unhandled Rodex application route: {invocation.route}"
        )

    def _prepare(self, invocation: RodexInvocation) -> PreparedRodexInvocation | int:
        if invocation.preparation is PipelinePreparation.DIRECT:
            return PreparedRodexInvocation(invocation, None)
        if invocation.preparation is PipelinePreparation.SELECTOR:
            assert invocation.route is CommandRoute.SELECTOR
            selector = invocation.arguments[0]
            selection = self._session_lifecycle.resolve_selector(
                selector, self._database_path
            )
            return PreparedRodexInvocation(
                invocation,
                self._acquire_runtime(),
                selection,
            )
        assert invocation.preparation is PipelinePreparation.RUNTIME
        try:
            runtime = self._acquire_runtime()
        except RodexExecutableNotFoundError as error:
            if invocation.route is not CommandRoute.MACHINE:
                raise
            assert invocation.classification is not None
            machine_spec = invocation.classification.machine_spec
            assert machine_spec is not None
            print_machine_error(
                machine_spec.operation,
                "runtime_unavailable",
                str(error),
                retryable=True,
                session_name=(
                    invocation.arguments[1] if len(invocation.arguments) > 1 else None
                ),
                control=None,
            )
            return 3
        return PreparedRodexInvocation(invocation, runtime)

    def _execute_help(self, arguments: list[str]) -> int:
        if arguments != [HELP_COMMAND]:
            raise RodexLaunchError("usage: rodex _help")
        print(HELP_TEXT, end="")
        return 0

    def _execute_codex(self, arguments: list[str]) -> int:
        codex_binary = self._resolve_executable(self._configured_codex)
        if codex_binary is None:
            raise RodexExecutableNotFoundError(
                f"Codex executable was not found: {self._configured_codex}"
            )
        return self._codex_delegator(codex_binary, arguments)

    def _execute_managed_codex(
        self,
        invocation: RodexInvocation,
        services: RuntimeServices,
    ) -> int:
        codex_invocation = invocation.codex_invocation
        assert codex_invocation is not None
        assert codex_invocation.route is CodexCliRoute.MANAGED_INTERACTIVE
        return self._session_lifecycle.execute_managed_interactive(
            codex_invocation.arguments,
            self._database_path,
            services.launcher,
            codex_binary=services.codex_binary,
            configured_codex=self._configured_codex,
        )

    def _acquire_runtime(self) -> RuntimeServices:
        tmux_binary = self._resolve_executable(self._configured_tmux)
        if tmux_binary is None:
            raise RodexExecutableNotFoundError(
                f"tmux executable was not found: {self._configured_tmux}"
            )
        codex_binary = self._resolve_executable(self._configured_codex)
        launcher = self._provided_launcher or self._runtime_launcher_factory(
            codex_binary or self._configured_codex,
            tmux_binary,
        )
        return RuntimeServices(
            launcher,
            self._provided_control_client or CodexControlClient(),
            codex_binary,
        )
