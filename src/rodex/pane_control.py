"""Exact-target tmux adapter for primary and registered presentation panes."""

from __future__ import annotations

import os
import re
import shlex
import sys
import uuid
from typing import Final

from .native_composer import composer_gutter
from .process_environment import (
    exact_environment_exec_command,
    validated_user_environment_entries,
)
from .tmux_executor import SyncTmuxExecutor, SyncTmuxRunner, TmuxCommandResult
from .tmux_session_capability import (
    TmuxRuntimeCapability,
    capability_identity_if_shell_condition,
    capability_pane_read_arguments,
    combine_tmux_if_shell_conditions,
    primary_pane_capability_if_shell_condition,
    primary_pane_capability_read_arguments,
)

_PANE_ID_PATTERN: Final = re.compile(r"%[0-9]+")
_CREATION_RECEIPT_PATTERN: Final = re.compile(r"(pending|registered|retired):([0-9a-f]{32})(?::(%[0-9]+))?")


class TmuxPaneController:
    """Locate, open, focus, resize or close one registered pane through bounded tmux calls."""

    def __init__(
        self,
        tmux_binary: str,
        capability: TmuxRuntimeCapability,
        primary_pane_target: str,
        *,
        runner: SyncTmuxRunner,
        python_executable: str = sys.executable,
        primary: bool = False,
        primary_pane_option: str | None = None,
        owner_pane_option: str | None = None,
    ) -> None:
        self.validate_primary_pane_target(primary_pane_target)
        if not primary and (primary_pane_option is None or owner_pane_option is None):
            raise ValueError("a presentation pane requires explicit registration options")
        self._capability = capability
        self._primary_pane_target = primary_pane_target
        self._python_executable = python_executable
        self._primary = primary
        self._primary_pane_option = primary_pane_option
        self._creation_receipt_option = f"{primary_pane_option}_creation"
        self._owner_pane_option = owner_pane_option
        self._observer_pane_target: str | None = None
        self._creation_operation_id: str | None = None
        self._tmux_executor = SyncTmuxExecutor(
            tmux_binary,
            capability.tmux_server_socket_path,
            runner=runner,
        )

    @staticmethod
    def validate_primary_pane_target(primary_pane_target: str) -> None:
        if _PANE_ID_PATTERN.fullmatch(primary_pane_target) is None:
            raise ValueError("primary pane target must be an exact tmux pane ID")

    @property
    def known_pane_id(self) -> str | None:
        """Identity from the most recent fenced lookup; never resolve a new target here."""
        return self._primary_pane_target if self._primary else self._observer_pane_target

    @property
    def creation_pending(self) -> bool:
        """An unacknowledged split must be reconciled before another is admitted."""
        return self._creation_operation_id is not None and self._observer_pane_target is None

    def locate(self) -> str | None:
        if self._primary:
            identity = self._tmux_executor.run(
                primary_pane_capability_read_arguments(self._capability, "#{pane_id}|#{pane_dead}")
            )
            valid = identity.returncode == 0 and identity.stdout.strip() == f"{self._primary_pane_target}|0"
            return self._primary_pane_target if valid else None
        candidate = self._observer_pane_target
        if candidate is None:
            shown = self._tmux_executor.run(
                (
                    "show-options",
                    "-p",
                    "-v",
                    "-t",
                    self._primary_pane_target,
                    self._primary_pane_option,
                )
            )
            if shown.returncode != 0:
                return None
            candidate = shown.stdout.strip()
        if _PANE_ID_PATTERN.fullmatch(candidate) is None:
            return None
        read = capability_pane_read_arguments(
            self._capability,
            candidate,
            f"#{{pane_id}}|#{{{self._owner_pane_option}}}|#{{pane_dead}}",
        )
        identity = self._tmux_executor.run(
            (
                *read[:4],
                combine_tmux_if_shell_conditions(
                    read[4],
                    f"#{{==:#{{@rodex_pane_runtime_id}},{self._capability.runtime_id}}}",
                ),
                *read[5:],
            )
        )
        if identity.returncode != 0 or identity.stdout.strip() != f"{candidate}|{self._primary_pane_target}|0":
            self._observer_pane_target = None
            return None
        self._observer_pane_target = candidate
        return candidate

    def create(self, pane_command: tuple[str, ...]) -> str | None:
        return self._create(pane_command, retire_missing=True)

    def _create(self, pane_command: tuple[str, ...], *, retire_missing: bool) -> str | None:
        if self._primary:
            return self.locate()
        if self._observer_pane_target is not None:
            existing = self.locate()
            if existing is not None:
                return existing
        cwd = self._tmux_executor.run(
            primary_pane_capability_read_arguments(
                self._capability,
                "#{pane_id}|#{pane_current_path}",
            )
        )
        cwd_fields = cwd.stdout.rstrip("\n").split("|", maxsplit=1)
        if cwd.returncode != 0 or len(cwd_fields) != 2 or cwd_fields[0] != self._primary_pane_target or not cwd_fields[1]:
            return None
        previous_receipt = self._read_creation_value()
        if previous_receipt is None:
            return None
        receipt = _CREATION_RECEIPT_PATTERN.fullmatch(previous_receipt)
        if receipt is not None and receipt[1] != "retired":
            return self._adopt_creation_receipt(receipt.groups(), pane_command, retire_missing=retire_missing)
        try:
            operation_id = (
                self._creation_operation_id if not previous_receipt and self._creation_operation_id else uuid.uuid4().hex
            )
            environment_names = tuple(name for name, _value in validated_user_environment_entries(os.environ))
            command = exact_environment_exec_command(
                self._python_executable,
                environment_names,
                pane_command,
                operation_id=operation_id,
            )
        except ValueError:
            return None
        # The receipt is claimed in tmux before the split in the same native
        # queue. Competing/replacement coordinators all recover that operation.
        # Reissuing this compare-and-claim cannot duplicate a delayed split.
        self._creation_operation_id = operation_id
        split_command = (
            "split-window",
            "-v",
            "-b",
            "-d",
            "-p",
            "33",
            "-t",
            self._primary_pane_target,
            "-c",
            cwd_fields[1],
            "-P",
            "-F",
            "#{pane_id}",
            "-e",
            f"PWD={cwd_fields[1]}",
            *command,
        )
        split = self._mutate(
            (
                "if-shell",
                "-t",
                self._primary_pane_target,
                "-F",
                f"#{{==:#{{{self._creation_receipt_option}}},{previous_receipt}}}",
                " ; ".join(
                    shlex.join(command)
                    for command in (
                        (
                            "set-option",
                            "-p",
                            "-t",
                            self._primary_pane_target,
                            self._creation_receipt_option,
                            f"pending:{operation_id}",
                        ),
                        split_command,
                    )
                ),
                shlex.join(
                    ("display-message", "-p", "-t", self._primary_pane_target, f"#{{{self._creation_receipt_option}}}")
                ),
            )
        )
        pane_target = split.stdout.strip()
        if split.returncode != 0 or _PANE_ID_PATTERN.fullmatch(pane_target) is None:
            receipt = self._read_creation_receipt()
            if receipt is None:
                return None
            return self._adopt_creation_receipt(receipt, pane_command, retire_missing=retire_missing)
        return self._register_candidate(pane_target)

    def _adopt_creation_receipt(
        self, receipt: tuple[str, str, str | None], pane_command: tuple[str, ...], *, retire_missing: bool
    ) -> str | None:
        state, self._creation_operation_id, pane = receipt
        if state == "retired":
            self._creation_operation_id = None
            return None
        if state == "registered":
            assert pane is not None
            self._observer_pane_target = pane
            if self.locate() == pane:
                return pane
            if self.confirm_absent(pane) and retire_missing:
                return self._create(pane_command, retire_missing=False)
            return None
        pane = self._recover_creation()
        return None if pane is None else self._register_candidate(pane)

    def _read_creation_value(self) -> str | None:
        result = self._tmux_executor.run(
            primary_pane_capability_read_arguments(
                self._capability,
                f"#{{{self._creation_receipt_option}}}",
            )
        )
        value = result.stdout.strip()
        if result.returncode != 0:
            return None
        if value == "":
            return value
        match = _CREATION_RECEIPT_PATTERN.fullmatch(value)
        if match is None:
            return None
        state, _operation_id, pane = match.groups()
        if (state == "registered") != (pane is not None):
            return None
        return value

    def _read_creation_receipt(self) -> tuple[str, str, str | None] | None:
        value = self._read_creation_value()
        match = _CREATION_RECEIPT_PATTERN.fullmatch(value) if value else None
        if match is None:
            return None
        state, operation_id, pane = match.groups()
        return state, operation_id, pane

    def reconcile_retirement(self) -> tuple[bool, str | None]:
        """Fence unadmitted work, or recover the exact pane for a final close."""
        value = self._read_creation_value()
        if value is None:
            return False, None
        receipt = _CREATION_RECEIPT_PATTERN.fullmatch(value)
        if receipt is None or receipt[1] == "retired":
            operation_id = self._creation_operation_id
            if operation_id is None:
                return True, None
            retired = f"retired:{operation_id}"
            result = self._mutate(
                ("set-option", "-p", "-t", self._primary_pane_target, self._creation_receipt_option, retired),
                condition=f"#{{==:#{{{self._creation_receipt_option}}},{value}}}",
            )
            if result.returncode == 0 or self._read_creation_value() == retired:
                self._creation_operation_id = None
                return True, None
            return False, None
        state, self._creation_operation_id, pane = receipt.groups()
        if state == "pending":
            pane = self._recover_creation()
            if pane is not None:
                self._register_candidate(pane)
        return False, pane

    def _receipt_condition(self, pane: str | None = None) -> str:
        assert self._creation_operation_id is not None
        pending = f"#{{==:#{{{self._creation_receipt_option}}},pending:{self._creation_operation_id}}}"
        if pane is None:
            return pending
        registered = f"#{{==:#{{{self._creation_receipt_option}}},registered:{self._creation_operation_id}:{pane}}}"
        return f"#{{||:{pending},{registered}}}"

    def _registration_completed(self, pane: str) -> bool:
        expected = f"registered:{self._creation_operation_id}:{pane}"
        result = self._mutate(
            ("display-message", "-p", "-t", self._primary_pane_target, "1"),
            condition=f"#{{==:#{{{self._creation_receipt_option}}},{expected}}}",
        )
        if result.returncode != 0 or result.stdout.strip() != "1":
            return False
        self._observer_pane_target = pane
        return self.locate() == pane

    def _recover_creation(self) -> str | None:
        operation_id = self._creation_operation_id
        if operation_id is None:
            return None
        result = self._mutate(("list-panes", "-a", "-f", self._creation_condition(), "-F", "#{pane_id}"))
        candidates = result.stdout.splitlines()
        if result.returncode != 0 or len(candidates) != 1 or _PANE_ID_PATTERN.fullmatch(candidates[0]) is None:
            return None
        return candidates[0]

    def _creation_condition(self) -> str:
        assert self._creation_operation_id is not None
        return f"#{{m:*--operation-id={self._creation_operation_id}*,#{{pane_start_command}}}}"

    def _candidate_condition(self, pane_target: str) -> str:
        return combine_tmux_if_shell_conditions(
            capability_identity_if_shell_condition(self._capability),
            f"#{{==:#{{pane_id}},{pane_target}}}",
            "#{==:#{pane_dead},0}",
            self._creation_condition(),
            f"#{{||:#{{==:#{{@rodex_pane_runtime_id}},}},#{{==:#{{@rodex_pane_runtime_id}},{self._capability.runtime_id}}}}}",
            f"#{{||:#{{==:#{{{self._owner_pane_option}}},}},#{{==:#{{{self._owner_pane_option}}},{self._primary_pane_target}}}}}",
        )

    def _register_candidate(self, pane_target: str) -> str | None:
        # A primary-pane guard cannot authorize a mutation of a different pane.
        # Bind the candidate first, publish it last, then verify the exact binding.
        registration_steps = (
            ("set-option", "-p", "-t", pane_target, "@rodex_pane_runtime_id", str(self._capability.runtime_id)),
            (
                "set-option",
                "-p",
                "-t",
                pane_target,
                self._owner_pane_option,
                self._primary_pane_target,
            ),
            ("select-pane", "-d", "-t", pane_target),
        )
        for step in registration_steps:
            if self._mutate_candidate(pane_target, step).returncode != 0:
                if self._registration_completed(pane_target):
                    return pane_target
                self._discard_failed_candidate(pane_target)
                return None
        publication = (
            "if-shell",
            "-t",
            pane_target,
            "-F",
            self._candidate_condition(pane_target),
            " ; ".join(
                shlex.join(step)
                for step in (
                    ("set-option", "-p", "-t", self._primary_pane_target, self._primary_pane_option, pane_target),
                    (
                        "set-option",
                        "-p",
                        "-t",
                        self._primary_pane_target,
                        self._creation_receipt_option,
                        f"registered:{self._creation_operation_id}:{pane_target}",
                    ),
                    ("select-pane", "-t", self._primary_pane_target),
                )
            ),
            shlex.join(("run-shell", "false")),
        )
        result = self._mutate(publication, condition=self._receipt_condition(pane_target))
        if self._registration_completed(pane_target):
            return pane_target
        if result.returncode != 0:
            self._discard_failed_candidate(pane_target)
            return None
        # A registered receipt that lost its candidate is retained for exact
        # absence reconciliation; it is not authority to destroy a moved pane.
        return None

    def _mutate_candidate(self, pane_target: str, arguments: tuple[str, ...]) -> TmuxCommandResult:
        return self._tmux_executor.run(
            (
                "if-shell",
                "-t",
                pane_target,
                "-F",
                self._candidate_condition(pane_target),
                shlex.join(arguments),
                shlex.join(("run-shell", "false")),
            )
        )

    def focus(self, pane: str) -> bool:
        return self._mutate_target(pane, ("select-pane", "-t", pane)).returncode == 0

    def capture_cursor_line(self) -> tuple[str, int] | None:
        snapshot = self._capture_cursor_snapshot()
        if snapshot is None:
            return None
        lines, cursor_x, cursor_y, _width = snapshot
        return lines[cursor_y], cursor_x

    def capture_composer(self) -> tuple[tuple[str, ...], int, int] | None:
        snapshot = self._capture_cursor_snapshot()
        if snapshot is None:
            return None
        lines, cursor_x, cursor_y, width = snapshot
        for row in range(cursor_y, -1, -1):
            # The native gutter is at column zero. An arrow inside continuation
            # text is not another composer anchor.
            if (gutter := composer_gutter(lines[row])) is not None and len(gutter) == 2:
                return lines[row : cursor_y + 1], cursor_x, width
        return None

    def _capture_cursor_snapshot(self) -> tuple[tuple[str, ...], int, int, int] | None:
        """Read one primary presentation snapshot at an explicit input handoff.

        The cursor metadata brackets the capture in one fenced tmux command queue.
        Copy mode, a changed cursor or a retired runtime cannot admit a handoff.
        """
        if not self._primary:
            return None
        pane = self._primary_pane_target
        header = ("display-message", "-p", "-t", pane, "#{cursor_x}|#{cursor_y}|#{pane_in_mode}|#{pane_width}")
        capture = ("capture-pane", "-p", "-t", pane)
        result = self._tmux_executor.run(
            (
                "if-shell",
                "-t",
                pane,
                "-F",
                primary_pane_capability_if_shell_condition(self._capability),
                " ; ".join(shlex.join(command) for command in (header, capture, header)),
                shlex.join(("run-shell", "false")),
            )
        )
        lines = result.stdout.splitlines()
        if result.returncode != 0 or len(lines) < 3 or lines[0] != lines[-1]:
            return None
        try:
            cursor_x, cursor_y, pane_in_mode, width = map(int, lines[0].split("|"))
        except ValueError:
            return None
        if pane_in_mode or not 0 <= cursor_y < len(lines) - 2 or not 0 <= cursor_x < width:
            return None
        return tuple(lines[1:-1]), cursor_x, cursor_y, width

    def resize(self, pane: str, size_percent: int) -> bool:
        if type(size_percent) is not int or not 1 <= size_percent <= 99:
            raise ValueError("pane size must be an integer percentage from 1 to 99")
        return self._mutate_target(pane, ("resize-pane", "-t", pane, "-y", f"{size_percent}%")).returncode == 0

    def close(self, pane: str) -> bool:
        # Primary-pane closure is session termination, owned by session lifecycle.
        if self._primary:
            return False
        if self._mutate_target(pane, ("kill-pane", "-t", pane)).returncode != 0:
            return False
        if self._observer_pane_target == pane:
            self._observer_pane_target = None
        self._retire_creation(pane)
        return True

    def confirm_absent(self, pane: str) -> bool:
        """Only a successful owned-server read proves a timed-out close completed."""
        self.validate_primary_pane_target(pane)
        result = self._mutate(("list-panes", "-a", "-F", "#{pane_id}"))
        panes = result.stdout.splitlines()
        if result.returncode != 0 or not panes or any(_PANE_ID_PATTERN.fullmatch(value) is None for value in panes):
            return False
        if pane in panes:
            return False
        if self._observer_pane_target == pane:
            self._observer_pane_target = None
        self._retire_creation(pane)
        return True

    def _retire_creation(self, pane: str) -> None:
        receipt = self._read_creation_receipt()
        if receipt is None or receipt[0] != "registered" or receipt[2] != pane:
            return
        expected = f"registered:{receipt[1]}:{pane}"
        self._mutate(
            (
                "if-shell",
                "-t",
                self._primary_pane_target,
                "-F",
                f"#{{==:#{{{self._creation_receipt_option}}},{expected}}}",
                " ; ".join(
                    shlex.join(command)
                    for command in (
                        ("set-option", "-pu", "-t", self._primary_pane_target, self._primary_pane_option),
                        (
                            "set-option",
                            "-p",
                            "-t",
                            self._primary_pane_target,
                            self._creation_receipt_option,
                            f"retired:{receipt[1]}",
                        ),
                    )
                ),
            )
        )
        self._creation_operation_id = None

    def _mutate_target(self, pane: str, arguments: tuple[str, ...]) -> TmuxCommandResult:
        self.validate_primary_pane_target(pane)
        conditions = [
            capability_identity_if_shell_condition(self._capability),
            f"#{{==:#{{pane_id}},{pane}}}",
            "#{==:#{pane_dead},0}",
            f"#{{==:#{{@rodex_pane_runtime_id}},{self._capability.runtime_id}}}",
        ]
        if not self._primary:
            conditions.append(f"#{{==:#{{{self._owner_pane_option}}},{self._primary_pane_target}}}")
        return self._tmux_executor.run(
            (
                "if-shell",
                "-t",
                pane,
                "-F",
                combine_tmux_if_shell_conditions(*conditions),
                shlex.join(arguments),
                shlex.join(("run-shell", "false")),
            )
        )

    def _discard_failed_candidate(self, pane_target: str) -> None:
        """Only the still-pending receipt authorizes rollback, never an adopter's pane."""
        result = self._mutate(
            (
                "if-shell",
                "-t",
                pane_target,
                "-F",
                self._candidate_condition(pane_target),
                " ; ".join(
                    shlex.join(command)
                    for command in (
                        ("kill-pane", "-t", pane_target),
                        (
                            "set-option",
                            "-p",
                            "-t",
                            self._primary_pane_target,
                            self._creation_receipt_option,
                            f"retired:{self._creation_operation_id}",
                        ),
                    )
                ),
                shlex.join(("run-shell", "false")),
            ),
            condition=self._receipt_condition(),
        )
        if result.returncode == 0:
            self._creation_operation_id = None
            self._observer_pane_target = None

    def _mutate(self, arguments: tuple[str, ...], *, condition: str | None = None) -> TmuxCommandResult:
        guard = primary_pane_capability_if_shell_condition(self._capability)
        if condition is not None:
            guard = combine_tmux_if_shell_conditions(guard, condition)
        return self._tmux_executor.run(
            (
                "if-shell",
                "-t",
                self._primary_pane_target,
                "-F",
                guard,
                shlex.join(arguments),
                shlex.join(("run-shell", "false")),
            )
        )
