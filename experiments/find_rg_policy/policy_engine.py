"""Fast, read-only admission policy for direct ``find`` and ``rg`` workflows.

This experiment parses and inspects commands. It never executes the supplied
command, consults history, or writes runtime state.
"""

from __future__ import annotations

import math
import os
import re
import shlex
import stat
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

MIB = 1024 * 1024


class Outcome(StrEnum):
    ALLOWED = "allowed"
    REJECTED = "rejected"
    NEEDS_FILESYSTEM = "needs_filesystem"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: Outcome
    code: str
    message: str
    stage: int | None = None
    scope: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOWED

    def __bool__(self) -> bool:
        return self.allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "allowed": self.allowed,
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class SearchWorkflow:
    """One direct top-level find/rg workflow extracted from larger shell text."""

    ordinal: int
    command: str
    start: int
    end: int
    start_line: int
    end_line: int

    def to_dict(self, *, include_command: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ordinal": self.ordinal,
            "start": self.start,
            "end": self.end,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }
        if include_command:
            result["command"] = self.command
        return result


@dataclass(frozen=True, slots=True)
class WorkflowDecision:
    """The admission decision for one extracted search workflow."""

    workflow: SearchWorkflow
    decision: Decision

    def to_dict(self, *, include_command: bool = False) -> dict[str, Any]:
        return {
            "workflow": self.workflow.to_dict(include_command=include_command),
            "decision": self.decision.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Policy:
    max_depth: int
    max_roots: int
    max_entries: int
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    max_pattern_bytes: int
    max_patterns: int
    max_context_lines: int
    max_threads: int
    max_stages: int
    max_command_chars: int
    census_seconds: float
    find_denied_options: frozenset[str]
    find_follow_options: frozenset[str]
    find_reference_options: frozenset[str]
    rg_external_options: frozenset[str]
    rg_decompression_options: frozenset[str]
    rg_follow_options: frozenset[str]
    rg_non_default_engines: frozenset[str]
    messages: dict[str, str]

    def render(self, code: str, **values: object) -> str:
        try:
            template = self.messages[code]
        except KeyError as exc:
            raise ValueError(f"policy has no message for {code}") from exc
        try:
            return template.format(**values)
        except KeyError as exc:
            raise ValueError(f"policy message {code} needs {exc.args[0]}") from exc


LIMIT_NAMES = {
    "max_depth",
    "max_roots",
    "max_entries",
    "max_files",
    "max_file_bytes",
    "max_total_bytes",
    "max_pattern_bytes",
    "max_patterns",
    "max_context_lines",
    "max_threads",
    "max_stages",
    "max_command_chars",
    "census_seconds",
}


def _string_set(section: dict[str, Any], name: str) -> frozenset[str]:
    value = section.get(name)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be a nonempty string list")
    return frozenset(value)


def load_policy(path: str | Path) -> Policy:
    with Path(path).open("rb") as stream:
        raw = tomllib.load(stream)
    if raw.get("schema_version") != 1:
        raise ValueError("policy schema_version must be 1")
    limits = raw.get("limits")
    find = raw.get("find")
    ripgrep = raw.get("ripgrep")
    messages = raw.get("messages")
    if not all(isinstance(section, dict) for section in (limits, find, ripgrep, messages)):
        raise ValueError("policy requires limits, find, ripgrep, and messages tables")
    assert isinstance(limits, dict)
    assert isinstance(find, dict)
    assert isinstance(ripgrep, dict)
    assert isinstance(messages, dict)
    if set(limits) != LIMIT_NAMES:
        raise ValueError("policy limits do not match the supported schema")
    integer_limits = LIMIT_NAMES - {"census_seconds"}
    for name in integer_limits:
        value = limits[name]
        minimum = 0 if name == "max_depth" else 1
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    census_seconds = limits["census_seconds"]
    if not isinstance(census_seconds, (int, float)) or isinstance(census_seconds, bool):
        raise ValueError("census_seconds must be numeric")
    if not math.isfinite(census_seconds) or census_seconds <= 0:
        raise ValueError("census_seconds must be finite and positive")
    if not messages or not all(
        isinstance(key, str) and isinstance(value, str) and value for key, value in messages.items()
    ):
        raise ValueError("messages must contain nonempty string templates")
    return Policy(
        **{name: limits[name] for name in integer_limits},
        census_seconds=float(census_seconds),
        find_denied_options=_string_set(find, "denied_options"),
        find_follow_options=_string_set(find, "follow_options"),
        find_reference_options=_string_set(find, "reference_options"),
        rg_external_options=_string_set(ripgrep, "external_options"),
        rg_decompression_options=_string_set(ripgrep, "decompression_options"),
        rg_follow_options=_string_set(ripgrep, "follow_options"),
        rg_non_default_engines=_string_set(ripgrep, "non_default_engines"),
        messages=dict(messages),
    )


class PolicyViolation(ValueError):
    def __init__(self, code: str, message: str, stage: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage


def _fail(policy: Policy, code: str, stage: int | None = None, **values: object) -> None:
    raise PolicyViolation(code, policy.render(code, **values), stage)


@dataclass(frozen=True, slots=True)
class Token:
    text: str
    kind: str
    start: int
    end: int
    quoted: bool = False


SHELL_OPERATORS = (
    "&>>",
    "<<<",
    "<<-",
    "&&",
    "||",
    "|&",
    ">>",
    "<<",
    ">&",
    "<&",
    "<>",
    ">|",
    "&>",
    "|",
    ";",
    "&",
    "(",
    ")",
    "<",
    ">",
    "\n",
)


def lex(command: str, policy: Policy) -> list[Token]:
    if "\0" in command:
        _fail(policy, "NUL")
    tokens: list[Token] = []
    index = 0
    while index < len(command):
        if command.startswith("\\\n", index):
            index += 2
            continue
        if command[index] in " \t\r":
            index += 1
            continue
        if command[index] == "#":
            newline = command.find("\n", index)
            index = len(command) if newline < 0 else newline
            continue
        operator = next((item for item in SHELL_OPERATORS if command.startswith(item, index)), None)
        if operator is not None:
            tokens.append(Token(operator, "operator", index, index + len(operator)))
            index += len(operator)
            continue

        start = index
        characters: list[str] = []
        quote = ""
        quoted = False
        while index < len(command):
            character = command[index]
            if quote == "'":
                if character == "'":
                    quote = ""
                    quoted = True
                else:
                    characters.append(character)
                index += 1
                continue
            if character == "\\":
                if index + 1 >= len(command):
                    _fail(policy, "QUOTING")
                following = command[index + 1]
                if following == "\n":
                    index += 2
                    continue
                if quote == '"' and following not in '$`"\\':
                    characters.append("\\")
                characters.append(following)
                index += 2
                continue
            if character in "$`":
                _fail(policy, "DYNAMIC_SHELL")
            if character == '"':
                quote = "" if quote == '"' else '"'
                quoted = True
                index += 1
                continue
            if not quote:
                if character == "'":
                    quote = "'"
                    quoted = True
                    index += 1
                    continue
                if character.isspace() or character in ";&|<>()":
                    break
                if character in "*?[{}" or (character == "~" and not characters):
                    _fail(policy, "SHELL_EXPANSION")
            characters.append(character)
            index += 1
        if quote:
            _fail(policy, "QUOTING")
        tokens.append(Token("".join(characters), "word", start, index, quoted))
        if len(tokens) > 2048:
            _fail(policy, "COMMAND_COMPLEXITY")
    return tokens


@dataclass(slots=True)
class Stage:
    argv: list[str]
    before: str = ""
    after: str = ""
    redirects: list[tuple[str, str, str]] = field(default_factory=list)
    wrappers: list[str] = field(default_factory=list)
    tool: str = ""
    options: list[tuple[str, Any]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    depth: int | None = None
    mode: str = "content"
    source: str = "paths"
    flags: dict[str, Any] = field(default_factory=dict)

    def has(self, option: str) -> bool:
        return any(name == option for name, _value in self.options)

    def values(self, option: str) -> list[Any]:
        return [value for name, value in self.options if name == option]


def _parse_shell(command: str, policy: Policy) -> list[Stage]:
    words: list[Token] = []
    redirects: list[tuple[str, str, str]] = []
    stages: list[Stage] = []
    before = ""
    tokens = lex(command, policy)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind == "operator" and token.text in {">", ">>", "<", ">&", "<&"}:
            descriptor = "0" if token.text.startswith("<") else "1"
            if words and not words[-1].quoted and words[-1].end == token.start and words[-1].text.isdigit():
                descriptor = words.pop().text
            if index + 1 >= len(tokens) or tokens[index + 1].kind != "word":
                _fail(policy, "REDIRECTION", len(stages) + 1)
            redirects.append((descriptor, token.text, tokens[index + 1].text))
            index += 2
            continue
        if token.kind == "operator":
            if token.text not in {"|", "&&", "||", ";", "\n"}:
                _fail(policy, "SHELL_CONTROL", len(stages) + 1, operator=repr(token.text))
            if words:
                stages.append(
                    Stage(
                        argv=[word.text for word in words],
                        before=before,
                        after=token.text,
                        redirects=redirects,
                    )
                )
                words = []
                redirects = []
                before = token.text
            elif token.text not in {";", "\n"}:
                _fail(policy, "SHELL_SYNTAX", len(stages) + 1)
            index += 1
            continue
        words.append(token)
        index += 1
    if words:
        stages.append(Stage(argv=[word.text for word in words], before=before, redirects=redirects))
    elif redirects or before in {"|", "&&", "||"}:
        _fail(policy, "SHELL_SYNTAX", len(stages) + 1)
    if not stages:
        _fail(policy, "EMPTY_COMMAND")
    return stages


def _executable_name(value: str, policy: Policy, stage: int) -> str:
    name = os.path.basename(value)
    if "/" in value and value not in {f"/bin/{name}", f"/usr/bin/{name}", f"/usr/local/bin/{name}"}:
        _fail(policy, "EXECUTABLE_IDENTITY", stage, executable=repr(value))
    return name


def _strip_wrappers(argv: list[str], policy: Policy, stage: int) -> tuple[list[str], list[str]]:
    remaining = list(argv)
    wrappers: list[str] = []
    for _depth in range(8):
        if not remaining:
            _fail(policy, "EMPTY_COMMAND", stage)
        wrapper = _executable_name(remaining[0], policy, stage)
        if wrapper == "command":
            offset = 1
            if offset < len(remaining) and remaining[offset] in {"--", "-p"}:
                offset += 1
        elif wrapper in {"nice", "ionice"}:
            offset = 1
            value_options = {"-n", "--adjustment"} if wrapper == "nice" else {"-n", "-c", "--class", "--classdata"}
            while offset < len(remaining) and remaining[offset].startswith("-"):
                value = remaining[offset]
                if value == "--":
                    offset += 1
                    break
                if value in value_options:
                    offset += 1
                    if offset >= len(remaining) or re.fullmatch(r"-?\d+", remaining[offset]) is None:
                        _fail(policy, "WRAPPER_ARGUMENT", stage, wrapper=wrapper)
                elif (
                    (wrapper == "ionice" and value in {"-t", "--ignore"})
                    or re.fullmatch(r"-(?:n|c)-?\d+", value)
                    or (wrapper == "nice" and re.fullmatch(r"-\d+", value))
                    or any(
                        value.startswith(option + "=") and re.fullmatch(r"-?\d+", value.split("=", 1)[1])
                        for option in value_options
                        if option.startswith("--")
                    )
                ):
                    pass
                else:
                    _fail(policy, "WRAPPER_ARGUMENT", stage, wrapper=wrapper)
                offset += 1
        elif wrapper == "timeout":
            offset = 1
            while offset < len(remaining) and remaining[offset].startswith("-"):
                value = remaining[offset]
                if value == "--":
                    offset += 1
                    break
                if value in {"--foreground", "--preserve-status", "-v", "--verbose"}:
                    offset += 1
                    continue
                if value in {"-k", "--kill-after", "-s", "--signal"}:
                    offset += 2
                    continue
                if value.startswith(("--kill-after=", "--signal=")) or re.fullmatch(r"-[ks].+", value):
                    offset += 1
                    continue
                _fail(policy, "WRAPPER_ARGUMENT", stage, wrapper=wrapper)
            if offset >= len(remaining) or re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)[smhd]?", remaining[offset]) is None:
                _fail(policy, "WRAPPER_ARGUMENT", stage, wrapper=wrapper)
            offset += 1
        else:
            return remaining, wrappers
        wrappers.append(wrapper)
        remaining = remaining[offset:]
    _fail(policy, "WRAPPER_DEPTH", stage)


_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_DIRECT_SEARCH_PREFIX = re.compile(r"\s*(?:/bin/|/usr/bin/|/usr/local/bin/)?(?:find|rg)(?=$|[\s<>;&|])")


def _search_after_assignments(argv: list[str]) -> bool:
    index = 0
    while index < len(argv) and _ASSIGNMENT.fullmatch(argv[index]):
        index += 1
    return index < len(argv) and os.path.basename(argv[index]) in {"find", "rg"}


_NON_BOUNDARY_SHELL_OPERATORS = ("&>>", "<<<", "<<-", ">>", "<<", ">&", "<&", "<>", ">|", "&>")


def _read_heredoc_delimiter(command: str, operator_start: int) -> tuple[str, bool, int] | None:
    """Return the quote-removed heredoc delimiter and the source position after it."""
    strip_tabs = command.startswith("<<-", operator_start)
    index = operator_start + (3 if strip_tabs else 2)
    while index < len(command) and command[index] in " \t":
        index += 1
    characters: list[str] = []
    quote = ""
    while index < len(command):
        character = command[index]
        if quote:
            if character == quote:
                quote = ""
            elif character == "\\" and quote == '"' and index + 1 < len(command):
                index += 1
                characters.append(command[index])
            else:
                characters.append(character)
            index += 1
            continue
        if character in " \t\r\n;|&<>()":
            break
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "\\" and index + 1 < len(command):
            index += 1
            characters.append(command[index])
            index += 1
            continue
        characters.append(character)
        index += 1
    if quote or not characters:
        return None
    return "".join(characters), strip_tabs, index


def _after_heredoc_bodies(
    command: str,
    body_start: int,
    heredocs: list[tuple[str, bool]],
) -> int:
    """Return the first position after queued heredoc bodies and delimiters."""
    position = body_start
    for delimiter, strip_tabs in heredocs:
        while position < len(command):
            newline = command.find("\n", position)
            line_end = len(command) if newline < 0 else newline
            line = command[position:line_end]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                position = line_end if newline < 0 else newline + 1
                break
            if newline < 0:
                return len(command)
            position = newline + 1
        else:
            return len(command)
    return position


def _top_level_spans(command: str, *, split_pipelines: bool) -> list[tuple[int, int]]:
    """Return conservative top-level shell spans without expanding source text.

    List controls and physical newlines delimit workflows. Pipelines can either be
    retained as one workflow or split into stages for direct-command detection.
    Escaped newlines, quotes, backticks, and command/process substitutions never
    create a boundary in their containing source.
    """
    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    quote = ""
    backtick = False
    heredocs: list[tuple[str, bool]] = []

    def pipeline_continues(end: int) -> bool:
        prefix = command[start:end].rstrip(" \t\r")
        return (prefix.endswith("|") and not prefix.endswith("||")) or prefix.endswith("|&")

    while index < len(command):
        character = command[index]
        if backtick:
            if character == "\\":
                index += 2
                continue
            if character == "`":
                backtick = False
            index += 1
            continue
        if quote:
            if quote == '"' and character == "\\":
                index += 2
                continue
            if character == quote:
                quote = ""
            index += 1
            continue
        if character == "\\":
            index += 2
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "`":
            backtick = True
            index += 1
            continue
        if command.startswith(("$(", "<(", ">("), index):
            depth = 1
            index += 2
            nested_quote = ""
            while index < len(command) and depth:
                nested = command[index]
                if nested_quote:
                    if nested_quote == '"' and nested == "\\":
                        index += 2
                        continue
                    if nested == nested_quote:
                        nested_quote = ""
                elif nested == "\\":
                    index += 2
                    continue
                elif nested in {"'", '"'}:
                    nested_quote = nested
                elif nested == "(":
                    depth += 1
                elif nested == ")":
                    depth -= 1
                index += 1
            continue
        at_word_boundary = index == start or command[index - 1].isspace() or command[index - 1] in ";|&"
        if character == "#" and at_word_boundary:
            newline = command.find("\n", index)
            if newline < 0:
                spans.append((start, index))
                return spans
            if heredocs:
                spans.append((start, index))
                index = _after_heredoc_bodies(command, newline + 1, heredocs)
                heredocs = []
                start = index
                continue
            if pipeline_continues(index):
                index = newline + 1
                continue
            spans.append((start, index))
            index = newline + 1
            start = index
            continue
        if command.startswith("<<", index) and not command.startswith("<<<", index):
            parsed_heredoc = _read_heredoc_delimiter(command, index)
            if parsed_heredoc is not None:
                delimiter, strip_tabs, delimiter_end = parsed_heredoc
                heredocs.append((delimiter, strip_tabs))
                index = delimiter_end
                continue
        non_boundary = next(
            (operator for operator in _NON_BOUNDARY_SHELL_OPERATORS if command.startswith(operator, index)),
            None,
        )
        if non_boundary is not None:
            index += len(non_boundary)
            continue
        if command.startswith(("&&", "||"), index):
            spans.append((start, index))
            index += 2
            start = index
            continue
        if command.startswith("|&", index):
            if split_pipelines:
                spans.append((start, index))
                start = index + 2
            index += 2
            continue
        if character == "|":
            if split_pipelines:
                spans.append((start, index))
                start = index + 1
            index += 1
            continue
        if character == "\n" and pipeline_continues(index):
            index += 1
            continue
        if character == "\n" and heredocs:
            spans.append((start, index))
            index = _after_heredoc_bodies(command, index + 1, heredocs)
            heredocs = []
            start = index
            continue
        if character in ";\n":
            spans.append((start, index))
            index += 1
            start = index
            continue
        if character == "&":
            # Retain the background operator with its left workflow so admission
            # reports SHELL_CONTROL rather than silently analyzing a foreground form.
            spans.append((start, index + 1))
            index += 1
            start = index
            continue
        index += 1
    spans.append((start, len(command)))
    return spans


def _top_level_segments(command: str) -> list[str]:
    """Split stages for applicability checks while retaining nested shell text."""
    return [command[start:end] for start, end in _top_level_spans(command, split_pipelines=True)]


def _looks_like_direct_search(command: str, policy: Policy) -> bool:
    if _DIRECT_SEARCH_PREFIX.match(command) is not None:
        return True
    for segment in _top_level_segments(command):
        try:
            # Python's shlex retains an escaped newline as a token, while Bash
            # removes the pair before word splitting.
            arguments = shlex.split(segment.replace("\\\n", ""), comments=True, posix=True)
        except ValueError:
            direct_prefix = re.match(r"\s*(?:/bin/|/usr/bin/|/usr/local/bin/)?(find|rg)(?=$|\s)", segment)
            if direct_prefix is not None:
                return True
            continue
        if not arguments:
            continue
        if os.path.basename(arguments[0]) in {"find", "rg"} or _search_after_assignments(arguments):
            return True
        if os.path.basename(arguments[0]) in {"command", "nice", "ionice", "timeout"}:
            try:
                remaining, _wrappers = _strip_wrappers(arguments, policy, 1)
            except PolicyViolation:
                if any(os.path.basename(argument) in {"find", "rg"} for argument in arguments[1:]):
                    return True
            else:
                if os.path.basename(remaining[0]) in {"find", "rg"} or _search_after_assignments(remaining):
                    return True
    return False


def extract_search_workflows(source: str, *, policy: Policy) -> tuple[SearchWorkflow, ...]:
    """Extract direct top-level find/rg workflows without executing shell text.

    Pipelines remain intact. Top-level lists and unescaped physical newlines form
    extraction boundaries; a backslash-newline remains part of its logical command.
    Search-looking text inside quotes, backticks, and command/process substitutions
    is not extracted.
    """
    if not isinstance(source, str):
        raise TypeError("source must be a string")
    workflows: list[SearchWorkflow] = []
    for raw_start, raw_end in _top_level_spans(source, split_pipelines=False):
        start = raw_start
        end = raw_end
        while start < end and source[start].isspace():
            start += 1
        while end > start and source[end - 1].isspace():
            end -= 1
        if start == end:
            continue
        command = source[start:end]
        if not _looks_like_direct_search(command, policy):
            continue
        workflows.append(
            SearchWorkflow(
                ordinal=len(workflows) + 1,
                command=command,
                start=start,
                end=end,
                start_line=source.count("\n", 0, start) + 1,
                end_line=source.count("\n", 0, end) + 1,
            )
        )
    return tuple(workflows)


def _words(value: str) -> frozenset[str]:
    return frozenset(value.split())


RG_VALUE_OPTIONS = _words(
    """regexp file after-context before-context context color colors context-separator
    dfa-size-limit encoding engine field-context-separator field-match-separator generate
    glob hostname-bin hyperlink-format iglob ignore-file max-columns max-count max-depth
    max-filesize path-separator pre pre-glob regex-size-limit replace sort sortr threads
    type type-add type-clear type-not"""
)
RG_SWITCH_OPTIONS = _words(
    """auto-hybrid-regex binary block-buffered byte-offset case-sensitive column count
    count-matches crlf debug files files-with-matches files-without-match fixed-strings
    follow glob-case-insensitive heading help hidden ignore-case ignore-file-case-insensitive
    include-zero invert-match json line-buffered line-number line-regexp mmap multiline
    multiline-dotall no-config no-ignore no-ignore-dot no-ignore-exclude no-ignore-files
    no-ignore-global no-ignore-messages no-ignore-parent no-ignore-vcs no-messages
    no-pcre2-unicode no-require-git no-unicode null null-data one-file-system only-matching
    passthru pcre2 pcre2-version pretty quiet search-zip smart-case sort-files stats
    stop-on-nonmatch text trace trim type-list unrestricted version vimgrep with-filename
    word-regexp no-filename no-line-number max-columns-preview no-auto-hybrid-regex
    no-binary no-block-buffered no-byte-offset no-column no-crlf no-fixed-strings no-follow
    no-glob-case-insensitive no-heading no-hidden no-ignore-file-case-insensitive
    no-include-zero no-invert-match no-line-buffered no-mmap no-multiline
    no-multiline-dotall no-one-file-system no-pcre2 no-search-zip no-stats no-text no-trim
    no-max-columns-preview no-json no-context-separator no-encoding no-pre ignore ignore-dot
    ignore-exclude ignore-files ignore-global ignore-messages ignore-parent ignore-vcs messages
    pcre2-unicode require-git unicode"""
)
RG_ALIASES = {"maxdepth": "max-depth", "passthrough": "passthru"}
RG_SHORT_OPTIONS = {
    "n": "line-number",
    "N": "no-line-number",
    "i": "ignore-case",
    "s": "case-sensitive",
    "S": "smart-case",
    "F": "fixed-strings",
    "P": "pcre2",
    "e": "regexp",
    "f": "file",
    "g": "glob",
    "t": "type",
    "T": "type-not",
    "l": "files-with-matches",
    "c": "count",
    "q": "quiet",
    "o": "only-matching",
    "v": "invert-match",
    "w": "word-regexp",
    "x": "line-regexp",
    "A": "after-context",
    "B": "before-context",
    "C": "context",
    "m": "max-count",
    "M": "max-columns",
    "j": "threads",
    "d": "max-depth",
    "u": "unrestricted",
    ".": "hidden",
    "L": "follow",
    "a": "text",
    "b": "byte-offset",
    "H": "with-filename",
    "I": "no-filename",
    "0": "null",
    "U": "multiline",
    "z": "search-zip",
    "r": "replace",
    "E": "encoding",
    "p": "pretty",
    "h": "help",
    "V": "version",
}


def _unsigned(value: str, option: str, policy: Policy, stage: int) -> int:
    if re.fullmatch(r"[0-9]{1,18}", value) is None:
        _fail(policy, "INVALID_NUMBER", stage, option=option, value=repr(value))
    return int(value)


def _size_bytes(value: str, option: str, policy: Policy, stage: int) -> int:
    match = re.fullmatch(r"([0-9]{1,18})([KMG]?)", value)
    if match is None:
        _fail(policy, "INVALID_SIZE", stage, option=option)
    return int(match[1]) * 1024 ** {"": 0, "K": 1, "M": 2, "G": 3}[match[2]]


def _parse_rg(stage_data: Stage, policy: Policy, stage_number: int) -> None:
    options: list[tuple[str, Any]] = []
    positional: list[str] = []
    ended = False
    index = 1
    while index < len(stage_data.argv):
        value = stage_data.argv[index]
        if ended or value == "-" or not value.startswith("-"):
            positional.append(value)
        elif value == "--":
            ended = True
        elif value.startswith("--"):
            name, separator, argument = value[2:].partition("=")
            name = RG_ALIASES.get(name, name)
            if name in RG_VALUE_OPTIONS:
                if not separator:
                    index += 1
                    if index >= len(stage_data.argv):
                        _fail(policy, "MISSING_ARGUMENT", stage_number, option=repr("--" + name))
                    argument = stage_data.argv[index]
                options.append((name, argument))
            elif name in RG_SWITCH_OPTIONS:
                if separator:
                    _fail(policy, "UNEXPECTED_ARGUMENT", stage_number, option=repr("--" + name))
                options.append((name, None))
            else:
                _fail(policy, "UNKNOWN_RG_OPTION", stage_number, option=repr("--" + name))
        else:
            offset = 1
            while offset < len(value):
                name = RG_SHORT_OPTIONS.get(value[offset])
                if name is None:
                    _fail(policy, "UNKNOWN_RG_OPTION", stage_number, option=repr("-" + value[offset]))
                argument = None
                if name in RG_VALUE_OPTIONS:
                    argument = value[offset + 1 :]
                    if not argument:
                        index += 1
                        if index >= len(stage_data.argv):
                            _fail(policy, "MISSING_ARGUMENT", stage_number, option=repr("-" + value[offset]))
                        argument = stage_data.argv[index]
                    options.append((name, argument))
                    break
                options.append((name, None))
                offset += 1
        index += 1

    stage_data.options = options
    stage_data.patterns = stage_data.values("regexp")
    special = {"files", "help", "version", "pcre2-version", "type-list", "generate"} & {name for name, _value in options}
    modes = {"count", "count-matches", "files-with-matches", "files-without-match", "json", "files"} & {
        name for name, _value in options
    }
    if len(modes) > 1:
        _fail(policy, "CONFLICTING_MODES", stage_number)
    stage_data.mode = next(iter(modes), "content")
    if stage_data.has("only-matching") and not modes:
        stage_data.mode = "fragments"
    if stage_data.has("quiet"):
        stage_data.mode = "existence"
    if stage_data.has("files"):
        stage_data.mode = "files"
    if special - {"files"}:
        stage_data.mode = "metadata"
    if not stage_data.patterns and not stage_data.has("file") and not special:
        if not positional:
            _fail(policy, "MISSING_PATTERN", stage_number)
        stage_data.patterns = [positional.pop(0)]
    stage_data.paths = positional

    stage_data.flags = {"fixed": False, "engine": "default", "follow": False, "unrestricted": 0}
    for name, argument in options:
        if name in {"max-depth", "max-count", "threads", "after-context", "before-context", "context", "max-columns"}:
            number = _unsigned(argument, "--" + name, policy, stage_number)
            if name == "max-depth":
                stage_data.depth = number
        elif name in {"max-filesize", "dfa-size-limit", "regex-size-limit"}:
            _size_bytes(argument, "--" + name, policy, stage_number)
        if name in {"fixed-strings", "no-fixed-strings"}:
            stage_data.flags["fixed"] = name == "fixed-strings"
        elif name in {"pcre2", "no-pcre2", "auto-hybrid-regex", "no-auto-hybrid-regex", "engine"}:
            engines = {
                "pcre2": "pcre2",
                "no-pcre2": "default",
                "auto-hybrid-regex": "auto",
                "no-auto-hybrid-regex": "default",
            }
            engine = argument if name == "engine" else engines[name]
            if engine not in {"default", "pcre2", "auto"}:
                _fail(policy, "ENGINE", stage_number, engine=repr(engine))
            stage_data.flags["engine"] = engine
        elif name in {"follow", "no-follow"}:
            stage_data.flags["follow"] = name == "follow"
        elif name == "unrestricted":
            stage_data.flags["unrestricted"] += 1


FIND_VALUE_OPTIONS = _words(
    """-amin -anewer -atime -cmin -cnewer -ctime -gid -group -ilname -iname -inum
    -ipath -iregex -links -lname -mmin -mtime -name -newer -newermt -path -perm
    -regex -samefile -size -type -uid -used -user -xtype -maxdepth -mindepth
    -regextype -printf -fprint -fprint0 -fls -files0-from -fstype"""
)
FIND_FLAG_OPTIONS = _words(
    """-daystart -depth -follow -noleaf -xdev -ignore_readdir_race
    -noignore_readdir_race -warn -nowarn -empty -executable -false -nogroup
    -nouser -readable -true -writable -delete -ls -print -print0 -prune -quit
    -help --help -version --version"""
)
FIND_EXEC_OPTIONS = {"-exec", "-execdir", "-ok", "-okdir"}
FIND_ALIASES = {
    "-or": "-o",
    "-and": "-a",
    "!": "-not",
    "-wholename": "-path",
    "-iwholename": "-ipath",
    "-mount": "-xdev",
}
FIND_OPERATORS = {"-a", "-o", "-not", ",", "(", ")"}


def _validate_find_expression(atoms: list[Any], policy: Policy, stage: int) -> None:
    cursor = 0

    def primary(level: int = 0) -> None:
        nonlocal cursor
        if level > 64 or cursor >= len(atoms):
            _fail(policy, "FIND_EXPRESSION", stage)
        token = atoms[cursor]
        cursor += 1
        if token == "-not":
            primary(level + 1)
            return
        if token == "(":
            expression(0, level + 1)
            if cursor >= len(atoms) or atoms[cursor] != ")":
                _fail(policy, "FIND_EXPRESSION", stage)
            cursor += 1
            return
        if not isinstance(token, tuple):
            _fail(policy, "FIND_EXPRESSION", stage)

    def expression(minimum: int = 0, level: int = 0) -> None:
        nonlocal cursor
        primary(level)
        while cursor < len(atoms) and atoms[cursor] != ")":
            token = atoms[cursor]
            explicit = isinstance(token, str) and token in {",", "-o", "-a"}
            operator = token if explicit else "-a"
            priority = {",": 0, "-o": 1, "-a": 2}[operator]
            if priority < minimum:
                return
            if explicit:
                cursor += 1
            expression(priority + 1, level + 1)

    if atoms:
        expression()
    if cursor != len(atoms):
        _fail(policy, "FIND_EXPRESSION", stage)


def _parse_find(stage_data: Stage, policy: Policy, stage_number: int) -> None:
    arguments = stage_data.argv
    index = 1
    atoms: list[Any] = []
    while index < len(arguments):
        value = arguments[index]
        if value in {"-H", "-L", "-P"}:
            stage_data.options.append((value, None))
        elif re.fullmatch(r"-O[0-3]", value):
            stage_data.options.append(("-O", value[2:]))
        elif value == "-D":
            index += 1
            if index >= len(arguments):
                _fail(policy, "MISSING_ARGUMENT", stage_number, option=repr("-D"))
            stage_data.options.append(("-D", arguments[index]))
        elif value == "--":
            index += 1
            break
        else:
            break
        index += 1
    while (
        index < len(arguments) and not arguments[index].startswith("-") and arguments[index] not in {"!", "(", ")", ","}
    ):
        stage_data.paths.append(arguments[index])
        index += 1
    while index < len(arguments):
        option = FIND_ALIASES.get(arguments[index], arguments[index])
        if option in FIND_OPERATORS:
            atoms.append(option)
            if option not in {"(", ")"}:
                stage_data.options.append((option, None))
            index += 1
            continue
        argument: Any = None
        if option in FIND_EXEC_OPTIONS:
            begin = index + 1
            index += 1
            while index < len(arguments) and arguments[index] not in {";", "+"}:
                index += 1
            if index >= len(arguments):
                _fail(policy, "FIND_EXPRESSION", stage_number)
            argument = [*arguments[begin:index], arguments[index]]
        elif option == "-fprintf":
            if index + 2 >= len(arguments):
                _fail(policy, "MISSING_ARGUMENT", stage_number, option=repr(option))
            argument = arguments[index + 1 : index + 3]
            index += 2
        elif option in FIND_VALUE_OPTIONS or re.fullmatch(r"-newer[aBcm][aBcmt]", option):
            index += 1
            if index >= len(arguments):
                _fail(policy, "MISSING_ARGUMENT", stage_number, option=repr(option))
            argument = arguments[index]
        elif option not in FIND_FLAG_OPTIONS:
            _fail(policy, "UNKNOWN_FIND_OPTION", stage_number, option=repr(option))
        stage_data.options.append((option, argument))
        atoms.append((option, argument))
        if option in {"-maxdepth", "-mindepth"}:
            number = _unsigned(argument, option, policy, stage_number)
            if option == "-maxdepth":
                stage_data.depth = number
        elif option in {"-type", "-xtype"} and re.fullmatch(r"[bcdflpsD](?:,[bcdflpsD])*", argument) is None:
            _fail(policy, "FIND_TYPE", stage_number, value=repr(argument))
        elif option == "-size" and re.fullmatch(r"[+-]?\d{1,18}[cbwkMG]?", argument) is None:
            _fail(policy, "FIND_SIZE", stage_number)
        index += 1
    _validate_find_expression(atoms, policy, stage_number)
    stage_data.flags = {"follow": False}
    for name, _argument in stage_data.options:
        if name in {"-H", "-L", "-follow", "-P"}:
            stage_data.flags["follow"] = name != "-P"
    if any(stage_data.has(option) for option in {"-help", "--help", "-version", "--version"}):
        stage_data.mode = "metadata"
    else:
        stage_data.mode = "inventory"
    if stage_data.has("-files0-from") and stage_data.paths:
        _fail(policy, "FIND_ROOTS", stage_number)


def _parse_consumer(stage_data: Stage, policy: Policy, stage_number: int) -> None:
    arguments = stage_data.argv[1:]
    if stage_data.tool == "true" and not arguments and stage_data.before == "||":
        stage_data.source = "none"
        return
    if stage_data.before != "|":
        _fail(policy, "UNSUPPORTED_COMMAND", stage_number, tool=repr(stage_data.tool))
    if stage_data.tool == "wc":
        if not all(value == "--" or re.fullmatch(r"-[lcwmL]+", value) for value in arguments):
            _fail(policy, "CONSUMER_ARGUMENT", stage_number, tool="wc")
    elif stage_data.tool in {"head", "tail"}:
        index = 0
        while index < len(arguments):
            value = arguments[index]
            if value in {"-n", "-c", "--lines", "--bytes"}:
                index += 1
                if index >= len(arguments):
                    _fail(policy, "CONSUMER_ARGUMENT", stage_number, tool=stage_data.tool)
                _unsigned(arguments[index].lstrip("+"), stage_data.tool, policy, stage_number)
            elif re.fullmatch(r"-(?:n|c)?\d+", value) or re.fullmatch(r"--(?:lines|bytes)=\d+", value):
                pass
            elif value not in {"--", "-q", "--quiet", "--silent", "-v", "--verbose"}:
                _fail(policy, "CONSUMER_ARGUMENT", stage_number, tool=stage_data.tool)
            index += 1
    elif stage_data.tool == "sort":
        index = 0
        while index < len(arguments):
            value = arguments[index]
            if value == "-k":
                index += 1
                if index >= len(arguments) or re.fullmatch(r"[0-9.,nrfbh]+", arguments[index]) is None:
                    _fail(policy, "CONSUMER_ARGUMENT", stage_number, tool="sort")
            elif re.fullmatch(r"-k[0-9.,nrfbh]+", value):
                pass
            elif value != "--" and re.fullmatch(r"-[rnhuzbfdsVM]+", value) is None:
                _fail(policy, "CONSUMER_ARGUMENT", stage_number, tool="sort")
            index += 1
    elif stage_data.tool == "sed":
        if len(arguments) != 2 or arguments[0] not in {"-n", "-nE", "-En"}:
            _fail(policy, "CONSUMER_ARGUMENT", stage_number, tool="sed")
        if re.fullmatch(r"(?:\d+(?:,\d+)?[pq];?)+", arguments[1]) is None:
            _fail(policy, "CONSUMER_ARGUMENT", stage_number, tool="sed")
    else:
        _fail(policy, "UNSUPPORTED_COMMAND", stage_number, tool=repr(stage_data.tool))
    stage_data.source = "stream"


def parse(command: str, policy: Policy) -> list[Stage] | None:
    if len(command) > policy.max_command_chars:
        _fail(policy, "COMMAND_COMPLEXITY")
    stages = _parse_shell(command, policy)
    if len(stages) > policy.max_stages:
        _fail(policy, "COMMAND_COMPLEXITY")
    for stage_number, stage_data in enumerate(stages, 1):
        stage_data.argv, stage_data.wrappers = _strip_wrappers(stage_data.argv, policy, stage_number)
        stage_data.tool = _executable_name(stage_data.argv[0], policy, stage_number)
    if not any(stage_data.tool in {"find", "rg"} or _search_after_assignments(stage_data.argv) for stage_data in stages):
        return None
    for stage_number, stage_data in enumerate(stages, 1):
        if stage_data.tool == "find":
            _parse_find(stage_data, policy, stage_number)
        elif stage_data.tool == "rg":
            _parse_rg(stage_data, policy, stage_number)
        else:
            _parse_consumer(stage_data, policy, stage_number)
        if stage_data.tool == "rg" and stage_data.before == "|" and stage_data.mode not in {"files", "metadata"}:
            if not stage_data.paths or stage_data.paths == ["-"]:
                stage_data.source = "stream"
        elif stage_data.tool == "rg" and stage_data.paths == ["-"]:
            stage_data.source = "external_stdin"
    return stages


@dataclass(frozen=True, slots=True)
class CensusRequest:
    stage: int
    path: str
    depth: int | None
    content: bool
    input_file_only: bool
    inspect_empty: bool


def _absolute(path: str, cwd: str) -> str:
    return os.path.abspath(os.path.join(cwd, path))


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _static_plan(
    stages: list[Stage],
    policy: Policy,
    cwd: str,
    environment: Mapping[str, str],
) -> list[CensusRequest]:
    requests: list[CensusRequest] = []
    root_count = 0
    for stage_number, stage_data in enumerate(stages, 1):
        input_redirect: str | None = None
        for descriptor, operator, target in stage_data.redirects:
            if descriptor == "0" and operator == "<" and stage_data.tool == "rg":
                if input_redirect is not None:
                    _fail(policy, "REDIRECTION", stage_number)
                input_redirect = target
            elif (operator in {">", ">>"} and descriptor in {"1", "2"} and target == "/dev/null") or (
                operator == ">&" and descriptor in {"1", "2"} and target in {"1", "2"}
            ):
                pass
            else:
                _fail(policy, "REDIRECTION", stage_number)
        if stage_data.tool not in {"find", "rg"}:
            continue
        option_names = {name for name, _value in stage_data.options}
        if stage_data.tool == "find":
            denied = sorted(option_names & policy.find_denied_options)
            if denied:
                _fail(
                    policy,
                    "FIND_SIDE_EFFECT_OR_DYNAMIC_INPUT",
                    stage_number,
                    options=", ".join(denied),
                )
            if stage_data.flags.get("follow") and option_names & policy.find_follow_options:
                _fail(policy, "SYMLINK_FOLLOW", stage_number)
            for option, value in stage_data.options:
                is_reference = option in policy.find_reference_options or re.fullmatch(r"-newer[aBcm][aBcm]", option)
                if is_reference:
                    reference = _absolute(value, cwd)
                    if any(_inside(reference, root) for root in ("/proc", "/sys", "/dev")):
                        _fail(
                            policy,
                            "REFERENCE_SCOPE",
                            stage_number,
                            path=repr(value),
                            option=option,
                        )
                    requests.append(CensusRequest(stage_number, reference, 0, False, False, False))
            for output_format in stage_data.values("-printf"):
                widths = [int(value) for value in re.findall(r"%[-+# 0]*\.?([0-9]+)", output_format)]
                if len(output_format.encode("utf-8", "surrogatepass")) > 4096 or any(width > 4096 for width in widths):
                    _fail(policy, "FORMAT_AMPLIFICATION", stage_number)
        else:
            external = sorted(option_names & policy.rg_external_options)
            if external:
                _fail(
                    policy,
                    "RG_EXTERNAL_OR_UNINSPECTED_INPUT",
                    stage_number,
                    options=", ".join("--" + name for name in external),
                )
            if option_names & policy.rg_decompression_options:
                _fail(policy, "DECOMPRESSION", stage_number)
            if stage_data.flags.get("follow") and option_names & policy.rg_follow_options:
                _fail(policy, "SYMLINK_FOLLOW", stage_number)
            if stage_data.flags.get("engine") in policy.rg_non_default_engines:
                _fail(policy, "REGEX_ENGINE", stage_number)
            if stage_data.flags.get("unrestricted", 0) > 3:
                _fail(policy, "UNRESTRICTED_COUNT", stage_number)
            pattern_bytes = sum(len(pattern.encode("utf-8", "surrogatepass")) for pattern in stage_data.patterns)
            if pattern_bytes > policy.max_pattern_bytes or len(stage_data.patterns) > policy.max_patterns:
                _fail(
                    policy,
                    "PATTERN_BUDGET",
                    stage_number,
                    max_pattern_bytes=policy.max_pattern_bytes,
                    max_patterns=policy.max_patterns,
                )
            if not stage_data.flags.get("fixed"):
                for pattern in stage_data.patterns:
                    repeats = re.findall(r"\{\d+(?:,\d*)?\}", pattern)
                    if any(int(number) > 10_000 for repeat in repeats for number in re.findall(r"\d+", repeat)):
                        _fail(policy, "PATTERN_EXPANSION", stage_number)
            for option in ("after-context", "before-context", "context"):
                if any(int(value) > policy.max_context_lines for value in stage_data.values(option)):
                    _fail(
                        policy,
                        "OUTPUT_CONTEXT",
                        stage_number,
                        option="--" + option,
                        maximum=policy.max_context_lines,
                    )
            if any(int(value) > policy.max_threads for value in stage_data.values("threads")):
                _fail(policy, "THREAD_BUDGET", stage_number, maximum=policy.max_threads)
            for option in ("regex-size-limit", "dfa-size-limit"):
                if any(
                    _size_bytes(value, "--" + option, policy, stage_number) > 64 * MIB
                    for value in stage_data.values(option)
                ):
                    _fail(policy, "REGEX_MEMORY", stage_number, option="--" + option)
            if environment.get("RIPGREP_CONFIG_PATH") and not stage_data.has("no-config"):
                _fail(policy, "HIDDEN_CONFIG", stage_number)
        if stage_data.mode == "metadata":
            continue

        paths = list(stage_data.paths)
        if input_redirect is not None:
            if stage_data.mode == "files":
                _fail(policy, "LISTING_STDIN", stage_number)
            if stage_data.before == "|" or paths not in ([], ["-"]):
                _fail(policy, "AMBIGUOUS_INPUT", stage_number)
            paths = [input_redirect]
        elif stage_data.source == "stream":
            continue
        elif stage_data.source == "external_stdin" or "-" in paths:
            _fail(policy, "UNBOUNDED_STDIN", stage_number)
        if not paths:
            if stage_data.tool == "rg" and stage_data.mode != "files":
                _fail(policy, "IMPLICIT_INPUT", stage_number)
            paths = ["."]
        root_count += len(paths)
        if root_count > policy.max_roots:
            _fail(policy, "ROOT_COUNT", None, count=root_count, maximum=policy.max_roots)
        if stage_data.depth is not None and stage_data.depth > policy.max_depth:
            _fail(
                policy,
                "DEPTH_BUDGET",
                stage_number,
                depth=stage_data.depth,
                maximum=policy.max_depth,
            )
        for path in paths:
            if path == "":
                _fail(policy, "EMPTY_PATH", stage_number)
            absolute_path = _absolute(path, cwd)
            if any(_inside(absolute_path, root) for root in ("/proc", "/sys", "/dev")):
                _fail(policy, "VIRTUAL_INPUT", stage_number, path=repr(path))
            directory_syntax = path in {".", "..", "/"} or path.endswith("/")
            if stage_data.depth is None and directory_syntax and input_redirect is None:
                _fail(policy, "UNBOUNDED_DEPTH", stage_number, path=repr(path))
            requests.append(
                CensusRequest(
                    stage_number,
                    absolute_path,
                    stage_data.depth,
                    stage_data.tool == "rg" and stage_data.mode != "files",
                    input_redirect is not None,
                    stage_data.tool == "find" and stage_data.has("-empty"),
                )
            )
    return requests


def _census(requests: list[CensusRequest], policy: Policy) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "entries": 0,
        "regular_files": 0,
        "content_files": 0,
        "content_bytes": 0,
        "symlinks_skipped": 0,
        "roots": [],
    }
    verified_components: set[str] = set()

    def check_deadline(stage: int) -> None:
        if time.monotonic() - started > policy.census_seconds:
            _fail(policy, "CENSUS_TIMEOUT", stage, seconds=policy.census_seconds)

    def checked_lstat(path: str, stage: int) -> os.stat_result:
        current = os.path.sep
        for component in Path(path).parts[1:]:
            current = os.path.join(current, component)
            check_deadline(stage)
            if current in verified_components:
                continue
            current_stat = os.lstat(current)
            if stat.S_ISLNK(current_stat.st_mode):
                _fail(policy, "SYMLINK_PATH", stage, path=repr(current))
            verified_components.add(current)
        return os.lstat(path)

    for request in requests:
        try:
            root_stat = checked_lstat(request.path, request.stage)
        except PolicyViolation:
            raise
        except OSError:
            _fail(policy, "FILESYSTEM_UNKNOWN", request.stage, path=repr(request.path))
        directory = stat.S_ISDIR(root_stat.st_mode)
        root_type = "directory" if directory else "regular_file" if stat.S_ISREG(root_stat.st_mode) else "special"
        result["roots"].append({"path": request.path, "type": root_type})
        if request.input_file_only and not stat.S_ISREG(root_stat.st_mode):
            _fail(policy, "NONREGULAR_INPUT", request.stage, path=repr(request.path))
        if directory and request.depth is None:
            _fail(policy, "UNBOUNDED_DIRECTORY", request.stage, path=repr(request.path))
        census_depth = (request.depth + 1 if request.inspect_empty else request.depth) if request.depth is not None else 0
        stack = [(request.path, root_stat, 0)]
        while stack:
            check_deadline(request.stage)
            path, path_stat, level = stack.pop()
            result["entries"] += 1
            if result["entries"] > policy.max_entries:
                _fail(policy, "ENTRY_BUDGET", request.stage, maximum=policy.max_entries)
            if stat.S_ISLNK(path_stat.st_mode):
                result["symlinks_skipped"] += 1
            elif stat.S_ISREG(path_stat.st_mode):
                result["regular_files"] += 1
                if request.content:
                    result["content_files"] += 1
                    result["content_bytes"] += path_stat.st_size
                    if path_stat.st_size > policy.max_file_bytes:
                        _fail(
                            policy,
                            "FILE_BYTE_BUDGET",
                            request.stage,
                            size=path_stat.st_size,
                            maximum=policy.max_file_bytes,
                        )
                    if result["content_files"] > policy.max_files:
                        _fail(policy, "FILE_COUNT_BUDGET", request.stage, maximum=policy.max_files)
                    if result["content_bytes"] > policy.max_total_bytes:
                        _fail(policy, "TOTAL_BYTE_BUDGET", request.stage, maximum=policy.max_total_bytes)
            elif stat.S_ISDIR(path_stat.st_mode):
                if level < census_depth:
                    try:
                        with os.scandir(path) as entries:
                            for entry in entries:
                                check_deadline(request.stage)
                                if result["entries"] + len(stack) >= policy.max_entries:
                                    _fail(policy, "ENTRY_BUDGET", request.stage, maximum=policy.max_entries)
                                stack.append((entry.path, entry.stat(follow_symlinks=False), level + 1))
                    except PolicyViolation:
                        raise
                    except OSError:
                        _fail(policy, "FILESYSTEM_UNKNOWN", request.stage, path=repr(path))
            else:
                _fail(policy, "NONREGULAR_INPUT", request.stage, path=repr(path))
    return result


def inspect(
    command: str,
    *,
    cwd: str | Path,
    policy: Policy,
    verify_filesystem: bool = True,
    environment: Mapping[str, str] | None = None,
) -> Decision:
    if not isinstance(command, str):
        raise TypeError("command must be a string")
    workdir = os.fspath(cwd)
    if not os.path.isabs(workdir):
        return Decision(Outcome.REJECTED, "CWD", f"Rejected: {policy.render('CWD')}")
    if not _looks_like_direct_search(command, policy):
        return Decision(Outcome.NOT_APPLICABLE, "NOT_APPLICABLE", policy.render("not_applicable"))
    try:
        stages = parse(command, policy)
        if stages is None:
            return Decision(Outcome.NOT_APPLICABLE, "NOT_APPLICABLE", policy.render("not_applicable"))
        requests = _static_plan(stages, policy, workdir, os.environ if environment is None else environment)
        if requests and not verify_filesystem:
            return Decision(
                Outcome.NEEDS_FILESYSTEM,
                "FILESYSTEM_REQUIRED",
                policy.render("FILESYSTEM_REQUIRED"),
                scope={"requests": len(requests)},
            )
        scope = _census(requests, policy) if requests else {}
    except PolicyViolation as violation:
        prefix = f"Stage {violation.stage}: " if violation.stage is not None else ""
        return Decision(
            Outcome.REJECTED,
            violation.code,
            f"Rejected: {prefix}{violation.message}",
            violation.stage,
        )
    return Decision(Outcome.ALLOWED, "ALLOWED", policy.render("allowed"), scope=scope)


def inspect_search_workflows(
    source: str,
    *,
    cwd: str | Path,
    policy: Policy,
    verify_filesystem: bool = True,
    environment: Mapping[str, str] | None = None,
) -> tuple[WorkflowDecision, ...]:
    """Extract and inspect every direct top-level search workflow in source order."""
    return tuple(
        WorkflowDecision(
            workflow=workflow,
            decision=inspect(
                workflow.command,
                cwd=cwd,
                policy=policy,
                verify_filesystem=verify_filesystem,
                environment=environment,
            ),
        )
        for workflow in extract_search_workflows(source, policy=policy)
    )
