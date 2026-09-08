"""One configuration-driven menu state: filtering, cyclic selection and argument picking."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum

from .input_interceptor_config import InputInterceptorRegistration, InterceptionOption

INPUT_MENU_TARGET = "input-interceptor-menu"
ARGUMENT_MENU_FOOTER = "Press enter to confirm or esc to go back"


class InputMenuStage(StrEnum):
    COMMANDS = "commands"
    ARGUMENTS = "arguments"


@dataclass(frozen=True)
class InputMenuRow:
    label: str
    helper_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not isinstance(self.helper_text, str):
            raise ValueError("menu rows require text labels and helpers")


@dataclass(frozen=True)
class InputMenuView:
    draft: str
    native_prefix: str
    rows: tuple[InputMenuRow, ...]
    selected_index: int | None
    stage: InputMenuStage = InputMenuStage.COMMANDS
    heading: str = ""
    subheading: str = ""

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) for value in (self.draft, self.native_prefix, self.heading, self.subheading)):
            raise ValueError("menu presentation fields must be text")
        if self.rows:
            if type(self.selected_index) is not int or not 0 <= self.selected_index < len(self.rows):
                raise ValueError("menu selection must identify a displayed row")
        elif self.selected_index is not None:
            raise ValueError("an empty menu cannot have a selection")

    def serialize(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def deserialize(cls, payload: str) -> InputMenuView:
        fields = json.loads(payload)
        fields["rows"] = tuple(InputMenuRow(**row) for row in fields["rows"])
        fields["stage"] = InputMenuStage(fields["stage"])
        return cls(**fields)


class InputInterceptionMenu:
    """Shared matches are choices, not ambiguous dispatches; selection is explicit identity."""

    def __init__(self, registrations: tuple[InputInterceptorRegistration, ...], draft: str) -> None:
        self._registrations = registrations
        self.draft = draft
        self.stage = InputMenuStage.COMMANDS
        self._selected_command_name: str | None = None
        self._selected_option_index = 0
        self.update_draft(draft)

    @property
    def matching_commands(self) -> tuple[InputInterceptorRegistration, ...]:
        return tuple(entry for entry in self._registrations if entry.live.matches(self.draft))

    @property
    def selected_command(self) -> InputInterceptorRegistration | None:
        return next((entry for entry in self._registrations if entry.name == self._selected_command_name), None)

    @property
    def selected_option(self) -> InterceptionOption | None:
        entry = self.selected_command
        if self.stage != InputMenuStage.ARGUMENTS or entry is None or not entry.argument_menu.options:
            return None
        return entry.argument_menu.options[self._selected_option_index]

    def update_draft(self, draft: str) -> None:
        self.draft = draft
        matches = self.matching_commands
        if self._selected_command_name not in {entry.name for entry in matches}:
            self._selected_command_name = matches[0].name if matches else None

    def move_selection(self, offset: int) -> None:
        if self.stage == InputMenuStage.ARGUMENTS:
            entry = self.selected_command
            if entry is not None and entry.argument_menu.options:
                self._selected_option_index = (self._selected_option_index + offset) % len(entry.argument_menu.options)
        else:
            matches = self.matching_commands
            if matches:
                index = next(index for index, entry in enumerate(matches) if entry.name == self._selected_command_name)
                self._selected_command_name = matches[(index + offset) % len(matches)].name

    def open_arguments(self) -> bool:
        if self.selected_command is None:
            return False
        self.stage = InputMenuStage.ARGUMENTS
        self._selected_option_index = 0
        return True

    def back_to_commands(self) -> None:
        self.stage = InputMenuStage.COMMANDS

    def view(self, native_prefix: str) -> InputMenuView:
        if self.stage == InputMenuStage.ARGUMENTS:
            entry = self.selected_command
            assert entry is not None
            return InputMenuView(
                self.draft,
                native_prefix,
                tuple(InputMenuRow(option.name, option.helper_text) for option in entry.argument_menu.options),
                self._selected_option_index if entry.argument_menu.options else None,
                stage=self.stage,
                heading=entry.argument_menu.heading,
                subheading=entry.argument_menu.subheading,
            )
        matches = self.matching_commands
        return InputMenuView(
            self.draft,
            native_prefix,
            tuple(InputMenuRow(entry.completion_text, entry.live.helper_text) for entry in matches),
            next((index for index, entry in enumerate(matches) if entry.name == self._selected_command_name), None),
        )
