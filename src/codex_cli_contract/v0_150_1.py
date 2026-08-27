"""Codex 0.150.1 top-level commands and interactive option grammar."""

from __future__ import annotations

from typing import Final

from .contract import CodexCliContract, CodexCliOptionSpec, CodexOptionValueArity

CODEX_CLI_0_150_1_COMMAND_TOKENS: Final = frozenset(
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
        "execpolicy",
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
        "responses-api-proxy",
        "resume",
        "review",
        "sandbox",
        "sessions",
        "stdio-to-uds",
        "unarchive",
        "update",
    }
)

CODEX_CLI_0_150_1_OPTION_SPECS: Final = (
    CodexCliOptionSpec("config", ("-c", "--config"), CodexOptionValueArity.ONE),
    CodexCliOptionSpec("enable", ("--enable",), CodexOptionValueArity.ONE),
    CodexCliOptionSpec("disable", ("--disable",), CodexOptionValueArity.ONE),
    CodexCliOptionSpec(
        "remote",
        ("--remote",),
        CodexOptionValueArity.ONE,
        managed_compatible=False,
    ),
    CodexCliOptionSpec(
        "remote-auth-token-env",
        ("--remote-auth-token-env",),
        CodexOptionValueArity.ONE,
        managed_compatible=False,
    ),
    CodexCliOptionSpec("strict-config", ("--strict-config",)),
    CodexCliOptionSpec("image", ("-i", "--image"), CodexOptionValueArity.ONE_OR_MORE),
    CodexCliOptionSpec("model", ("-m", "--model"), CodexOptionValueArity.ONE),
    CodexCliOptionSpec("oss", ("--oss",)),
    CodexCliOptionSpec("local-provider", ("--local-provider",), CodexOptionValueArity.ONE),
    CodexCliOptionSpec("profile", ("-p", "--profile"), CodexOptionValueArity.ONE),
    CodexCliOptionSpec("sandbox", ("-s", "--sandbox"), CodexOptionValueArity.ONE),
    CodexCliOptionSpec("approve-for-me", ("--approve-for-me",)),
    CodexCliOptionSpec(
        "dangerously-bypass-approvals-and-sandbox",
        ("--dangerously-bypass-approvals-and-sandbox",),
    ),
    CodexCliOptionSpec(
        "dangerously-bypass-hook-trust", ("--dangerously-bypass-hook-trust",)
    ),
    CodexCliOptionSpec("cd", ("-C", "--cd"), CodexOptionValueArity.ONE),
    CodexCliOptionSpec("add-dir", ("--add-dir",), CodexOptionValueArity.ONE),
    CodexCliOptionSpec(
        "ask-for-approval",
        ("-a", "--ask-for-approval"),
        CodexOptionValueArity.ONE,
    ),
    CodexCliOptionSpec("search", ("--search",)),
    CodexCliOptionSpec("no-alt-screen", ("--no-alt-screen",)),
    CodexCliOptionSpec("help", ("-h", "--help"), managed_compatible=False),
    CodexCliOptionSpec("version", ("-V", "--version"), managed_compatible=False),
)

CODEX_CLI_0_150_1: Final = CodexCliContract(
    characterized_release="0.150.1",
    command_tokens=CODEX_CLI_0_150_1_COMMAND_TOKENS,
    option_specs=CODEX_CLI_0_150_1_OPTION_SPECS,
)
