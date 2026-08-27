"""The complete Rodex process entry point and application composition root."""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from codex_cli_contract import CODEX_CLI_0_150_1
from cool_name import CoolNameError
from rodex_registry import (
    RodexSessionError,
    default_rodex_database_path,
)
from rodex_sql import RodexSQLError

from .application_pipeline import CodexDelegator, UnifiedRodexApplicationPipeline
from .codex_update_notice import CodexUpdateNotice
from .control import CodexControlClient, RodexControlError
from .errors import RodexExecutableNotFoundError, RodexLaunchError
from .managed_session_lifecycle import ManagedSessionLifecycle
from .runtime import RodexRuntimeError, RodexRuntimeLauncher


def _exec_codex(codex_binary: str, arguments: Sequence[str]) -> int:
    """Replace Rodex with a Codex command that does not belong in managed tmux."""
    os.execv(codex_binary, [codex_binary, *arguments])
    raise AssertionError("os.execv returned unexpectedly")


def run(
    argv: Sequence[str] | None = None,
    *,
    database_path: str | os.PathLike[str] | None = None,
    launcher: RodexRuntimeLauncher | None = None,
    control_client: CodexControlClient | None = None,
    codex_delegator: CodexDelegator = _exec_codex,
) -> int:
    """Construct and execute exactly one complete Rodex application invocation."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    configured_codex = os.environ.get("RODEX_CODEX_BINARY", "codex")
    configured_tmux = os.environ.get("RODEX_TMUX_BINARY", "tmux")
    resolved_database = (
        Path(os.path.abspath(Path(database_path).expanduser()))
        if database_path is not None
        else default_rodex_database_path()
    )
    application = UnifiedRodexApplicationPipeline(
        database_path=resolved_database,
        configured_codex=configured_codex,
        configured_tmux=configured_tmux,
        launcher=launcher,
        control_client=control_client,
        codex_delegator=codex_delegator,
        resolve_executable=shutil.which,
        runtime_launcher_factory=lambda codex_binary, tmux_binary: RodexRuntimeLauncher(
            codex_binary,
            tmux_binary,
            attach_notice=CodexUpdateNotice(codex_binary).message_if_available,
        ),
        session_lifecycle=ManagedSessionLifecycle(),
        codex_cli_contract=CODEX_CLI_0_150_1,
    )
    return application.execute(arguments)


def main() -> None:
    """Run Rodex and translate application failures into process exit semantics."""
    try:
        raise SystemExit(run())
    except RodexExecutableNotFoundError as error:
        print(f"rodex: {error}", file=sys.stderr)
        raise SystemExit(127) from error
    except KeyboardInterrupt:
        print(file=sys.stderr)
        raise SystemExit(130) from None
    except (
        CoolNameError,
        RodexControlError,
        RodexLaunchError,
        RodexRuntimeError,
        RodexSQLError,
        RodexSessionError,
        OSError,
        ValueError,
        sqlite3.Error,
    ) as error:
        print(f"rodex: {error}", file=sys.stderr)
        raise SystemExit(1) from error
