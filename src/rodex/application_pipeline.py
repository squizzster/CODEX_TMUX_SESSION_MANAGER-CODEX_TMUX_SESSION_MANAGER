"""The unified application control plane for every Rodex invocation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from .command_contract import (
    COMMANDS_BY_TOKEN,
    CREATE_COMMAND,
    HELP_COMMAND,
    HELP_TEXT,
    CommandRoute,
    MachineCommandSpec,
    machine_spec_for_arguments,
)
from .control import CodexControlClient
from .errors import RodexExecutableNotFoundError, RodexLaunchError
from .machine_commands import execute_machine_command, print_machine_error
from .runtime import RodexRuntimeLauncher
from .session_commands import execute_session_command
from .statistics_commands import execute_statistics_command

CodexDelegator = Callable[[str, Sequence[str]], int]
ExecutableResolver = Callable[[str], str | None]


class PipelineRequirement(StrEnum):
    """The external state required before one selected route can execute."""

    NONE = "none"
    DATABASE = "database"
    RUNTIME = "runtime"


ROUTE_REQUIREMENTS: Final = {
    CommandRoute.HELP: PipelineRequirement.NONE,
    CommandRoute.CODEX: PipelineRequirement.NONE,
    CommandRoute.STATISTICS: PipelineRequirement.DATABASE,
    CommandRoute.SELECTOR: PipelineRequirement.DATABASE,
    CommandRoute.MACHINE: PipelineRequirement.RUNTIME,
    CommandRoute.SESSION: PipelineRequirement.RUNTIME,
    CommandRoute.LAUNCH: PipelineRequirement.RUNTIME,
}


class CollisionGuard(Protocol):
    def __call__(self, arguments: list[str], configured_codex: str) -> None: ...


class SelectorResolver(Protocol):
    def __call__(self, selector: str, database_path: Path) -> bool: ...


class SelectorExecutor(Protocol):
    def __call__(
        self,
        selector: str,
        database_path: Path,
        launcher: RodexRuntimeLauncher,
        *,
        codex_available: bool,
    ) -> int: ...


class LaunchExecutor(Protocol):
    def __call__(
        self,
        arguments: list[str],
        database_path: Path,
        launcher: RodexRuntimeLauncher,
        *,
        codex_binary: str | None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class RodexInvocation:
    """One normalized invocation selected for exactly one application route."""

    arguments: tuple[str, ...]
    route: CommandRoute
    requirement: PipelineRequirement
    machine_spec: MachineCommandSpec | None = None


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Lazily acquired services shared by runtime-backed routes."""

    launcher: RodexRuntimeLauncher
    control_client: CodexControlClient
    codex_binary: str | None


@dataclass(frozen=True, slots=True)
class PreparedRodexInvocation:
    """One invocation after its declared requirements have been satisfied."""

    invocation: RodexInvocation
    runtime: RuntimeServices | None


def select_rodex_invocation(arguments: Sequence[str]) -> RodexInvocation:
    """Normalize argv and select exactly one exhaustive application route."""
    normalized = tuple(arguments) if arguments else (CREATE_COMMAND,)
    command = normalized[0]
    spec = COMMANDS_BY_TOKEN.get(command)
    if spec is not None:
        machine_spec = machine_spec_for_arguments(list(normalized))
        route = CommandRoute.MACHINE if machine_spec is not None else spec.route
        return RodexInvocation(
            normalized,
            route,
            ROUTE_REQUIREMENTS[route],
            machine_spec,
        )
    if len(normalized) == 1 and not command.startswith(("-", "_")):
        return RodexInvocation(
            normalized,
            CommandRoute.SELECTOR,
            PipelineRequirement.DATABASE,
        )
    return RodexInvocation(
        normalized,
        CommandRoute.CODEX,
        PipelineRequirement.NONE,
    )


class UnifiedRodexApplicationPipeline:
    """Select, satisfy requirements, and execute every Rodex invocation."""

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
        collision_guard: CollisionGuard,
        selector_resolver: SelectorResolver,
        selector_executor: SelectorExecutor,
        launch_executor: LaunchExecutor,
    ) -> None:
        self._database_path = database_path
        self._configured_codex = configured_codex
        self._configured_tmux = configured_tmux
        self._provided_launcher = launcher
        self._provided_control_client = control_client
        self._codex_delegator = codex_delegator
        self._resolve_executable = resolve_executable
        self._collision_guard = collision_guard
        self._selector_resolver = selector_resolver
        self._selector_executor = selector_executor
        self._launch_executor = launch_executor

    def execute(self, arguments: Sequence[str]) -> int:
        """Run the one route selected for this invocation."""
        invocation = select_rodex_invocation(arguments)
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
        if invocation.route is CommandRoute.SELECTOR:
            selector = argv[0]
            if not self._database_path.exists() or not self._selector_resolver(
                selector, self._database_path
            ):
                return self._execute_codex(argv)
            services = self._acquire_runtime()
            return self._selector_executor(
                selector,
                self._database_path,
                services.launcher,
                codex_available=services.codex_binary is not None,
            )

        services = prepared.runtime
        assert services is not None
        if invocation.route is CommandRoute.MACHINE:
            return execute_machine_command(
                argv,
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
            return self._launch_executor(
                argv,
                self._database_path,
                services.launcher,
                codex_binary=services.codex_binary,
            )
        raise AssertionError(  # pragma: no cover - route map and branches are exhaustive.
            f"unhandled Rodex application route: {invocation.route}"
        )

    def _prepare(self, invocation: RodexInvocation) -> PreparedRodexInvocation | int:
        if invocation.requirement is not PipelineRequirement.RUNTIME:
            return PreparedRodexInvocation(invocation, None)
        try:
            runtime = self._acquire_runtime()
        except RodexExecutableNotFoundError as error:
            if invocation.route is not CommandRoute.MACHINE:
                raise
            assert invocation.machine_spec is not None
            print_machine_error(
                invocation.machine_spec.operation,
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
        self._collision_guard(arguments, self._configured_codex)
        codex_binary = self._resolve_executable(self._configured_codex)
        if codex_binary is None:
            raise RodexExecutableNotFoundError(
                f"Codex executable was not found: {self._configured_codex}"
            )
        return self._codex_delegator(codex_binary, arguments)

    def _acquire_runtime(self) -> RuntimeServices:
        tmux_binary = self._resolve_executable(self._configured_tmux)
        if tmux_binary is None:
            raise RodexExecutableNotFoundError(
                f"tmux executable was not found: {self._configured_tmux}"
            )
        codex_binary = self._resolve_executable(self._configured_codex)
        launcher = self._provided_launcher or RodexRuntimeLauncher(
            codex_binary or self._configured_codex,
            tmux_binary,
        )
        return RuntimeServices(
            launcher,
            self._provided_control_client or CodexControlClient(),
            codex_binary,
        )
