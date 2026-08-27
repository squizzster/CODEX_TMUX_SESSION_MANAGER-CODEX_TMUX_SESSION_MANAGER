"""Typed parser for one declarative Codex top-level CLI snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class CodexCliRoute(StrEnum):
    """Whether one native Codex invocation belongs inside managed Rodex."""

    MANAGED_INTERACTIVE = "managed_interactive"
    PASSTHROUGH = "passthrough"


class CodexCliClassificationReason(StrEnum):
    """The exact contract decision behind one selected route."""

    INTERACTIVE = "interactive"
    SUBCOMMAND = "subcommand"
    DIRECT_OPTION = "direct_option"
    UNKNOWN_OPTION = "unknown_option"
    MALFORMED_OPTION = "malformed_option"
    CONFLICTING_OPTIONS = "conflicting_options"
    MULTIPLE_POSITIONALS = "multiple_positionals"


class CodexOptionValueArity(StrEnum):
    """How many values one occurrence consumes at the top-level parser."""

    NONE = "none"
    ONE = "one"
    ONE_OR_MORE = "one_or_more"


@dataclass(frozen=True, slots=True)
class CodexCliOptionSpec:
    """One current Codex top-level option and its aliases."""

    name: str
    tokens: tuple[str, ...]
    value_arity: CodexOptionValueArity = CodexOptionValueArity.NONE
    managed_compatible: bool = True


@dataclass(frozen=True, slots=True)
class CodexCliInvocation:
    """One native argv classified without interpreting its prompt text."""

    arguments: tuple[str, ...]
    route: CodexCliRoute
    reason: CodexCliClassificationReason
    selector_candidate: str | None = None


@dataclass(frozen=True, slots=True)
class CodexCliContract:
    """A maintainable snapshot of one Codex top-level CLI grammar."""

    characterized_release: str
    command_tokens: frozenset[str]
    option_specs: tuple[CodexCliOptionSpec, ...]
    _options_by_token: MappingProxyType[str, CodexCliOptionSpec] = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        options_by_token: dict[str, CodexCliOptionSpec] = {}
        for spec in self.option_specs:
            if not spec.tokens or any(not token.startswith("-") for token in spec.tokens):
                raise ValueError("Codex option tokens must begin with '-'")
            for token in spec.tokens:
                if token in options_by_token:
                    raise ValueError(f"duplicate Codex option token: {token}")
                options_by_token[token] = spec
        if any(not command or command.startswith("-") for command in self.command_tokens):
            raise ValueError("Codex command tokens must be non-option words")
        object.__setattr__(
            self,
            "_options_by_token",
            MappingProxyType(options_by_token),
        )

    def classify(self, arguments: tuple[str, ...]) -> CodexCliInvocation:
        """Classify current interactive syntax; let Codex own every uncertainty."""
        positionals: list[str] = []
        seen_options: set[str] = set()
        index = 0
        while index < len(arguments):
            token = arguments[index]
            if token == "--":
                positionals.extend(arguments[index + 1 :])
                break

            matched = self._match_option(token)
            if matched is not None:
                spec, inline_value = matched
                if not spec.managed_compatible:
                    return self._passthrough(
                        arguments,
                        CodexCliClassificationReason.DIRECT_OPTION,
                    )
                seen_options.add(spec.name)
                if spec.value_arity is CodexOptionValueArity.NONE:
                    if inline_value is not None:
                        return self._passthrough(
                            arguments,
                            CodexCliClassificationReason.MALFORMED_OPTION,
                        )
                    index += 1
                    continue
                if spec.value_arity is CodexOptionValueArity.ONE:
                    if inline_value is not None:
                        if not inline_value:
                            return self._passthrough(
                                arguments,
                                CodexCliClassificationReason.MALFORMED_OPTION,
                            )
                        index += 1
                        continue
                    if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                        return self._passthrough(
                            arguments,
                            CodexCliClassificationReason.MALFORMED_OPTION,
                        )
                    index += 2
                    continue

                if inline_value == "":
                    return self._passthrough(
                        arguments,
                        CodexCliClassificationReason.MALFORMED_OPTION,
                    )
                consumed = 1 if inline_value is not None else 0
                index += 1
                while index < len(arguments):
                    value = arguments[index]
                    if value == "--" or value.startswith("-"):
                        break
                    consumed += 1
                    index += 1
                if consumed == 0:
                    return self._passthrough(
                        arguments,
                        CodexCliClassificationReason.MALFORMED_OPTION,
                    )
                continue

            if token.startswith("-"):
                return self._passthrough(
                    arguments,
                    CodexCliClassificationReason.UNKNOWN_OPTION,
                )
            if not positionals and token in self.command_tokens:
                return self._passthrough(
                    arguments,
                    CodexCliClassificationReason.SUBCOMMAND,
                )
            positionals.append(token)
            index += 1

        if _managed_option_conflict(seen_options):
            return self._passthrough(
                arguments,
                CodexCliClassificationReason.CONFLICTING_OPTIONS,
            )
        if len(positionals) > 1:
            return self._passthrough(
                arguments,
                CodexCliClassificationReason.MULTIPLE_POSITIONALS,
            )
        selector_candidate = (
            positionals[0]
            if len(arguments) == 1 and positionals == [arguments[0]]
            else None
        )
        return CodexCliInvocation(
            arguments,
            CodexCliRoute.MANAGED_INTERACTIVE,
            CodexCliClassificationReason.INTERACTIVE,
            selector_candidate,
        )

    def _match_option(
        self,
        token: str,
    ) -> tuple[CodexCliOptionSpec, str | None] | None:
        exact = self._options_by_token.get(token)
        if exact is not None:
            return exact, None
        if token.startswith("--") and "=" in token:
            name, inline_value = token.split("=", 1)
            spec = self._options_by_token.get(name)
            return (spec, inline_value) if spec is not None else None
        if token.startswith("-") and not token.startswith("--") and len(token) > 2:
            spec = self._options_by_token.get(token[:2])
            if spec is not None and spec.value_arity is not CodexOptionValueArity.NONE:
                return spec, token[2:]
        return None

    @staticmethod
    def _passthrough(
        arguments: tuple[str, ...],
        reason: CodexCliClassificationReason,
    ) -> CodexCliInvocation:
        return CodexCliInvocation(arguments, CodexCliRoute.PASSTHROUGH, reason)


def _managed_option_conflict(seen_options: set[str]) -> bool:
    automatic = "approve-for-me"
    bypass = "dangerously-bypass-approvals-and-sandbox"
    manual_controls = {"ask-for-approval", "sandbox"}
    return (
        automatic in seen_options and bool((manual_controls | {bypass}) & seen_options)
    ) or (bypass in seen_options and bool(manual_controls & seen_options))
