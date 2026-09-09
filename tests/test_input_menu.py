"""Pure menu-domain tests: each configuration drives matches, identity and options."""

from dataclasses import replace

import pytest

from rodex.input_interceptor_config import (
    INPUT_INTERCEPTORS,
    ArgumentMenuConfig,
    InputInterceptorRegistration,
    InterceptionOption,
    InterceptionRule,
    LiveInterceptionRule,
)
from rodex.input_menu import InputInterceptionMenu, InputMenuStage, InputMenuView


@pytest.mark.parametrize(
    "draft,names",
    [
        ("/", []),
        ("/r", []),
        ("/ro", ["rodex", "rodx"]),
        ("/rod", ["rodex", "rodx"]),
        ("/rode", ["rodex"]),
        ("/rodex", ["rodex"]),
        ("/rodx", ["rodx"]),
        ("/robot", []),
        ("/rodex light", []),
    ],
)
def test_each_live_expression_alone_drives_the_shared_completion_list(draft, names):
    menu = InputInterceptionMenu(INPUT_INTERCEPTORS, draft)
    assert [entry.name for entry in menu.matching_commands] == names
    assert [row.label for row in menu.view("/r").rows] == [entry.completion_text for entry in menu.matching_commands]


def test_cyclic_command_selection_preserves_identity_while_filtering():
    menu = InputInterceptionMenu(INPUT_INTERCEPTORS, "/ro")
    assert menu.selected_command.name == "rodex"
    menu.move_selection(-1)
    assert menu.selected_command.name == "rodx"
    menu.update_draft("/rod")
    assert menu.selected_command.name == "rodx"
    menu.move_selection(1)
    assert menu.selected_command.name == "rodex"
    menu.move_selection(1)
    assert menu.selected_command.name == "rodx"
    menu.update_draft("/rode")
    assert menu.selected_command.name == "rodex"
    for step in (1, -1, 1, -1):
        menu.move_selection(step)
        assert menu.view("/r").selected_index == 0
    menu.update_draft("/robot")
    menu.move_selection(1)
    assert menu.selected_command is None and not menu.open_arguments()


def test_argument_titles_rows_selection_and_escape_are_configured_menu_state():
    menu = InputInterceptionMenu(INPUT_INTERCEPTORS, "/rod")
    assert menu.open_arguments()
    view = menu.view("/r")
    config = INPUT_INTERCEPTORS[0].argument_menu
    assert view.stage == InputMenuStage.ARGUMENTS
    assert (view.heading, view.subheading) == (config.heading, config.subheading)
    assert [row.label for row in view.rows] == ["light", "dark", "dusk"]
    assert [row.helper_text for row in view.rows] == ["Light test argument", "Dark one", "Dusky one"]
    assert menu.selected_option.name == "light"
    menu.move_selection(-1)
    assert menu.selected_option.name == "dusk"
    menu.move_selection(1)
    assert menu.selected_option.name == "light"
    menu.move_selection(1)
    assert menu.selected_option.name == "dark"
    assert InputMenuView.deserialize(menu.view("/r").serialize()) == menu.view("/r")
    menu.back_to_commands()
    assert menu.draft == "/rod" and menu.selected_command.name == "rodex"
    assert menu.stage == InputMenuStage.COMMANDS and menu.selected_option is None


def test_empty_option_list_cannot_open_an_argument_picker():
    menu = InputInterceptionMenu(INPUT_INTERCEPTORS, "/rod")
    menu.move_selection(1)
    assert not menu.open_arguments()
    assert menu.stage == InputMenuStage.COMMANDS
    assert menu.selected_command.name == "rodx" and menu.view("/r").selected_index == 1


def test_empty_argument_picker_is_rejected_at_the_presentation_contract():
    menu = InputInterceptionMenu(INPUT_INTERCEPTORS, "/robot")
    with pytest.raises(ValueError, match="requires selectable options"):
        replace(menu.view("/r"), stage=InputMenuStage.ARGUMENTS)
    serialized = menu.view("/r").serialize().replace('"commands"', '"arguments"')
    with pytest.raises(ValueError, match="requires selectable options"):
        InputMenuView.deserialize(serialized)


def test_a_differently_named_configuration_supplies_every_menu_field():
    entry = InputInterceptorRegistration(
        "another",
        "!hello",
        LiveInterceptionRule(r"^!h(?:ello)?$", helper_text="Different helper"),
        InterceptionRule(r"^!hello (.*?)$"),
        ArgumentMenuConfig("Pick test", "Configured explanation", (InterceptionOption("only", "Only test option"),)),
    )
    menu = InputInterceptionMenu((entry,), "!h")
    assert menu.view("!").rows[0].label == "!hello"
    assert menu.view("!").rows[0].helper_text == "Different helper"
    assert menu.open_arguments()
    for step in (-1, 1, 1):
        menu.move_selection(step)
        assert menu.selected_option.name == "only"
    assert menu.view("!").heading == "Pick test"
    assert menu.view("!").subheading == "Configured explanation"
    assert entry.option_submission(menu.selected_option) == "!hello only"


def test_config_rejects_duplicate_or_unsubmittable_options():
    with pytest.raises(ValueError, match="unique"):
        ArgumentMenuConfig(options=(InterceptionOption("light", "One"), InterceptionOption("light", "Two")))
    with pytest.raises(ValueError, match="Enter expression"):
        replace(INPUT_INTERCEPTORS[0], on_enter=InterceptionRule(r"^/elsewhere (.*?)$"))


@pytest.mark.parametrize("steps", range(-12, 13))
def test_repeated_navigation_wraps_for_both_menu_lengths(steps):
    menu = InputInterceptionMenu(INPUT_INTERCEPTORS, "/rod")
    for _ in range(abs(steps)):
        menu.move_selection(1 if steps > 0 else -1)
    assert menu.view("/r").selected_index == steps % 2
    menu.update_draft("/rode")
    menu.open_arguments()
    for _ in range(abs(steps)):
        menu.move_selection(1 if steps > 0 else -1)
    assert menu.view("/r").selected_index == steps % 3
