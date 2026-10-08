"""Selected-runtime upgrades through an authenticated retained installation.

The current launcher never speaks an older runtime's control/daemon protocol.
A helper runs with the old interpreter to attach unchanged, or stops only its
exact idle reservation for the ordinary current resume.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass, fields
from pathlib import Path

from .errors import RodexLaunchError
from .installation import INSTALLATION_MANIFEST, INSTALLATION_SCHEMA
from .tmux_executor import SyncTmuxRunner
from .tmux_session_capability import TmuxSessionCapability
from .tmux_sharing_coordinator import sharing_coordinator_hook_command

UPGRADE_PROTOCOL = "rodex-retained-runtime-upgrade-v1"
UPGRADE_HELPER_TIMEOUT_SECONDS = 40


def retained_coordinator_interpreter(
    command: str,
    *,
    tmux_binary: str,
    capability: TmuxSessionCapability,
) -> Path:
    """Parse a known hook without executing any of its shell text."""
    try:
        hook = shlex.split(command)
        if len(hook) != 3 or hook[:2] != ["run-shell", "-b"]:
            raise ValueError("unexpected coordinator hook")
        arguments = shlex.split(hook[2])
        if len(arguments) != 13 or arguments[0] != "exec":
            raise ValueError("unexpected coordinator invocation")
        interpreter = Path(arguments[1].replace("##", "#"))
        expected = sharing_coordinator_hook_command(
            str(interpreter), tmux_binary, capability.tmux_server_socket_path, capability.tmux_server_id
        )
        if shlex.split(expected) != hook:
            raise ValueError("coordinator does not match the selected server")
        if (
            not interpreter.is_absolute()
            or ".." in interpreter.parts
            or interpreter.parts[-3:] != (".venv", "bin", "python")
        ):
            raise ValueError("coordinator does not use a retained interpreter")
        root = interpreter.parents[2]
        if re.fullmatch(r"[0-9a-f]{64}", root.name) is None:
            raise ValueError("retained installation fingerprint is invalid")
        state = root.lstat()
        if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid() or state.st_mode & 0o022:
            raise ValueError("retained installation is not privately owned")
        manifest_path = root / INSTALLATION_MANIFEST
        descriptor = os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            state = os.fstat(stream.fileno())
            if not stat.S_ISREG(state.st_mode) or state.st_uid != os.getuid() or state.st_mode & 0o022:
                raise ValueError("retained installation manifest is not privately owned")
            manifest = json.load(stream)
        if manifest != {"schema": INSTALLATION_SCHEMA, "implementation": root.name} or not interpreter.is_file():
            raise ValueError("retained installation is incomplete")
    except (OSError, ValueError) as error:
        raise RodexLaunchError(f"cannot upgrade this session through its retained installation: {error}") from error
    return interpreter


@dataclass(frozen=True, slots=True)
class RetainedRuntimeUpgrade:
    """One authenticated retained incarnation for attachment or idle upgrade."""

    interpreter: Path
    capability: TmuxSessionCapability
    tmux_session_name: str
    coordinator_command: str
    codex_binary: str
    tmux_binary: str
    environment: dict[str, str]
    _runner: SyncTmuxRunner = subprocess.run

    def _request(self, database_path: Path) -> dict:
        capability = {
            field.name: (
                self.capability.internal_session_id
                if field.name == "internal_session_id"
                else str(getattr(self.capability, field.name))
            )
            for field in fields(self.capability)
        }
        return {
            "protocol": UPGRADE_PROTOCOL,
            "implementation": self.interpreter.parents[2].name,
            "capability": capability,
            "tmux_session_name": self.tmux_session_name,
            "coordinator_command": self.coordinator_command,
            "database_path": str(database_path.resolve()),
            "codex_binary": self.codex_binary,
            "tmux_binary": self.tmux_binary,
        }

    def attach(self, database_path: Path) -> str:
        """Use the owning implementation's attach path, with the caller's TTY."""
        environment = dict(self.environment)
        environment.pop("TMUX", None)
        try:
            result = self._runner(
                [
                    str(self.interpreter),
                    "-I",
                    str(Path(__file__).with_name("retained_runtime_handoff.py")),
                    "--attach",
                    json.dumps(self._request(database_path)),
                ],
                check=False,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RodexLaunchError(f"could not reconnect through the retained Rodex installation: {error}") from error
        if result.returncode not in (0, 2):
            raise RodexLaunchError(
                "could not reconnect through the retained Rodex installation; runtime was left running"
            )
        return "detach" if result.returncode == 0 else "exited"

    def stop(self, database_path: Path) -> Path:
        """Require idle and final old-daemon shutdown before allowing replacement."""
        request = self._request(database_path)
        try:
            result = self._runner(
                [str(self.interpreter), "-I", str(Path(__file__).with_name("retained_runtime_handoff.py"))],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                check=False,
                timeout=UPGRADE_HELPER_TIMEOUT_SECONDS,
                env=self.environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RodexLaunchError(f"retained runtime upgrade did not finish; retry resume: {error}") from error
        if result.returncode != 0:
            detail = result.stderr.strip() or "retained runtime upgrade failed"
            raise RodexLaunchError(detail)
        try:
            reply = json.loads(result.stdout)
            if (
                not isinstance(reply, dict)
                or set(reply) != {"protocol", "implementation", "runtime_id", "state", "cwd"}
                or reply["protocol"] != UPGRADE_PROTOCOL
                or reply["implementation"] != request["implementation"]
                or reply["runtime_id"] != str(self.capability.runtime_id)
                or reply["state"] != "terminal"
                or not isinstance(reply["cwd"], str)
            ):
                raise ValueError("upgrade helper did not confirm exact terminal ownership")
            workspace = Path(reply["cwd"])
            if not workspace.is_absolute() or not workspace.is_dir():
                raise ValueError("upgrade helper returned an unavailable working directory")
        except (ValueError, TypeError) as error:
            raise RodexLaunchError(f"retained runtime upgrade returned an invalid receipt: {error}") from error
        return workspace
