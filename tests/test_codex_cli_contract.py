from __future__ import annotations

import pytest

from codex_cli_contract import (
    CODEX_CLI_0_151_0,
    CODEX_CLI_0_151_0_COMMAND_TOKENS,
    CodexCliClassificationReason,
    CodexCliRoute,
)


def classify(*arguments: str):  # type: ignore[no-untyped-def]
    return CODEX_CLI_0_151_0.classify(arguments)


def test_contract_attests_the_current_codex_command_and_alias_vocabulary() -> None:
    assert CODEX_CLI_0_151_0.characterized_release == "0.151.0"
    assert (
        frozenset(
            {
                "a",
                "agents",
                "app-server",
                "apply",
                "archive",
                "cloud",
                "completion",
                "debug",
                "delete",
                "doctor",
                "e",
                "exec",
                "exec-server",
                "features",
                "fork",
                "help",
                "login",
                "logout",
                "mcp",
                "mcp-server",
                "migrate-rollouts",
                "plugin",
                "queue",
                "remote-control",
                "resume",
                "review",
                "sandbox",
                "unarchive",
                "update",
            }
        )
        == CODEX_CLI_0_151_0_COMMAND_TOKENS
    )


@pytest.mark.parametrize("command", sorted(CODEX_CLI_0_151_0_COMMAND_TOKENS))
def test_every_current_codex_command_and_alias_is_direct(command: str) -> None:
    invocation = classify(command)

    assert invocation.route is CodexCliRoute.PASSTHROUGH
    assert invocation.reason is CodexCliClassificationReason.SUBCOMMAND


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("Project: CODEX_TMUX_SESSION_MANAGER",),
        ("--model", "gpt-5.6-sol"),
        ("--model", "gpt-5.6-sol", "review this project"),
        ("-mgpt-5.6-sol", "review this project"),
        ("--model=gpt-5.6-sol", "review this project"),
        ("--search", "research this project"),
        ("-C", "/tmp/project", "inspect this project"),
        ("-C/tmp/project", "inspect this project"),
        ("--strict-config",),
        ("prompt first", "--search"),
        ("--",),
        ("--", "exec"),
        ("--", "--future-codex-option"),
        ("--image", "first.png", "second.png"),
        ("--image=first.png", "second.png", "--", "inspect images"),
    ],
)
def test_current_interactive_shapes_are_managed(arguments: tuple[str, ...]) -> None:
    invocation = CODEX_CLI_0_151_0.classify(arguments)

    assert invocation.arguments == arguments
    assert invocation.route is CodexCliRoute.MANAGED_INTERACTIVE
    assert invocation.reason is CodexCliClassificationReason.INTERACTIVE


def test_only_one_raw_bare_prompt_is_a_possible_session_selector() -> None:
    assert classify("project").selector_candidate == "project"
    assert classify("--", "project").selector_candidate is None
    assert classify("--model", "project").selector_candidate is None


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        (("--help",), CodexCliClassificationReason.DIRECT_OPTION),
        (("-h",), CodexCliClassificationReason.DIRECT_OPTION),
        (("--version",), CodexCliClassificationReason.DIRECT_OPTION),
        (("-V",), CodexCliClassificationReason.DIRECT_OPTION),
        (
            ("--remote", "unix:///tmp/codex.sock"),
            CodexCliClassificationReason.DIRECT_OPTION,
        ),
        (
            ("--remote=unix:///tmp/codex.sock",),
            CodexCliClassificationReason.DIRECT_OPTION,
        ),
        (("--future-codex-option",), CodexCliClassificationReason.UNKNOWN_OPTION),
        (("-Z",), CodexCliClassificationReason.UNKNOWN_OPTION),
        (("--model",), CodexCliClassificationReason.MALFORMED_OPTION),
        (("--model=",), CodexCliClassificationReason.MALFORMED_OPTION),
        (("--search=true",), CodexCliClassificationReason.MALFORMED_OPTION),
        (("--image",), CodexCliClassificationReason.MALFORMED_OPTION),
        (
            ("--image=", "first.png"),
            CodexCliClassificationReason.MALFORMED_OPTION,
        ),
        (
            ("first positional", "second positional"),
            CodexCliClassificationReason.MULTIPLE_POSITIONALS,
        ),
        (
            ("--", "first positional", "second positional"),
            CodexCliClassificationReason.MULTIPLE_POSITIONALS,
        ),
        (
            ("--approve-for-me", "--sandbox", "workspace-write"),
            CodexCliClassificationReason.CONFLICTING_OPTIONS,
        ),
        (
            (
                "--dangerously-bypass-approvals-and-sandbox",
                "--ask-for-approval",
                "never",
            ),
            CodexCliClassificationReason.CONFLICTING_OPTIONS,
        ),
    ],
)
def test_uncertain_or_noninteractive_shapes_remain_exact_codex_passthrough(
    arguments: tuple[str, ...],
    reason: CodexCliClassificationReason,
) -> None:
    invocation = CODEX_CLI_0_151_0.classify(arguments)

    assert invocation.arguments == arguments
    assert invocation.route is CodexCliRoute.PASSTHROUGH
    assert invocation.reason is reason


def test_options_before_a_current_subcommand_still_select_passthrough() -> None:
    invocation = classify("--model", "gpt-5.6-sol", "exec", "run tests")

    assert invocation.route is CodexCliRoute.PASSTHROUGH
    assert invocation.reason is CodexCliClassificationReason.SUBCOMMAND


def test_variadic_images_consume_command_looking_values_until_a_boundary() -> None:
    invocation = classify("--image", "first.png", "exec", "--", "inspect images")

    assert invocation.route is CodexCliRoute.MANAGED_INTERACTIVE
    assert invocation.selector_candidate is None
