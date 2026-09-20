"""Re-stat YAML on submission, reload changed rules, and return exact prompt edits."""

from __future__ import annotations

import os
import re
import stat
import struct
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock

import yaml

from rodex_sql import default_rodex_state_root

from .file_stat_sha512 import file_stat_sha512
from .protocol_input_text import PromptTextEdit, UserPromptErrorNotice, UserPromptHookError

USER_PROMPT_SUBSTITUTIONS_PATH = Path(__file__).resolve().parents[2] / "conf" / "hooks" / "user_prompt_substitutions.yaml"


def default_user_prompt_substitutions_path(environment: Mapping[str, str] | None = None) -> Path:
    return default_rodex_state_root(environment) / "conf" / "hooks" / USER_PROMPT_SUBSTITUTIONS_PATH.name


class UserPromptHookConfigurationError(UserPromptHookError):
    """Refuse this submission when its current configuration cannot be loaded."""


@dataclass(frozen=True, slots=True)
class PromptSubstitution:
    pattern: re.Pattern[str]
    replacement: str
    count: int
    name: str | None = None

    def edits(self, text: str) -> tuple[PromptTextEdit, ...]:
        matches = self.pattern.finditer(text)
        selected = [next(matches, None)] if self.count == 1 else list(matches)
        # Apply right to left so every match retains its original coordinates.
        return tuple(
            PromptTextEdit(match.start(), match.end(), match.expand(self.replacement))
            for match in reversed(selected)
            if match is not None
        )


def _slash_component(expression: str, start: int) -> tuple[str, int]:
    component: list[str] = []
    index = start
    while index < len(expression):
        character = expression[index]
        if character == "/":
            return "".join(component), index + 1
        if character == "\\" and index + 1 < len(expression):
            following = expression[index + 1]
            component.append("/" if following == "/" else character + following)
            index += 2
        else:
            component.append(character)
            index += 1
    raise ValueError("expected /pattern/replacement/flags or s/pattern/replacement/flags")


def _compile_substitution(expression: str) -> PromptSubstitution:
    start = 2 if expression.startswith("s/") else 1 if expression.startswith("/") else 0
    if not start:
        raise ValueError("expected /pattern/replacement/flags or s/pattern/replacement/flags")
    pattern, cursor = _slash_component(expression, start)
    replacement, cursor = _slash_component(expression, cursor)
    return _compile_rule(pattern, replacement, expression[cursor:])


def _compile_rule(pattern: str, replacement: str, flags: str) -> PromptSubstitution:
    if not all(isinstance(value, str) for value in (pattern, replacement, flags)):
        raise ValueError("match, replace, and flags must be strings")
    pattern.encode("utf-8")
    replacement.encode("utf-8")
    if not pattern:
        raise ValueError("the regular expression must not be empty")
    if set(flags) - set("gims") or len(flags) != len(set(flags)):
        raise ValueError("flags may contain i, m, s, and g once each")
    regex_flags = re.NOFLAG
    for flag, value in (("i", re.IGNORECASE), ("m", re.MULTILINE), ("s", re.DOTALL)):
        if flag in flags:
            regex_flags |= value
    compiled = re.compile(pattern, regex_flags)
    # Compile the replacement even if the pattern never matches the empty string.
    compiled.sub(replacement, "")
    return PromptSubstitution(compiled, replacement, 0 if "g" in flags else 1)


def _compile_named_rule(entry: dict) -> PromptSubstitution:
    unknown = set(entry) - {"name", "match", "replace", "flags"}
    if unknown:
        raise ValueError(f"unknown rule fields: {', '.join(sorted(map(str, unknown)))}")
    missing = {"name", "match", "replace"} - set(entry)
    if missing:
        raise ValueError(f"missing rule fields: {', '.join(sorted(missing))}")
    if not isinstance(entry["name"], str) or not entry["name"].strip():
        raise ValueError("name must be a non-empty descriptive string")
    return replace(_compile_rule(entry["match"], entry["replace"], entry.get("flags", "")), name=entry["name"])


class UserPromptHook:
    """One runtime owns the ordered global/user snapshot for each submission."""

    def __init__(self, path: Path = USER_PROMPT_SUBSTITUTIONS_PATH, *, user_path: Path | None = None) -> None:
        self._lock = Lock()
        self._files = (_PromptRulesFile(path),) + (
            (_PromptRulesFile(user_path, optional=True),) if user_path is not None else ()
        )

    def __call__(self, texts: tuple[str, ...]) -> tuple[tuple[PromptTextEdit, ...], ...]:
        substitutions = self._rules_for_submission()
        transformed = []
        for text in texts:
            edits = []
            for substitution in substitutions:
                for edit in substitution.edits(text):
                    edits.append(edit)
                    text = text[: edit.start] + edit.replacement + text[edit.end :]
            transformed.append(tuple(edits))
        return tuple(transformed)

    def _rules_for_submission(self) -> tuple[PromptSubstitution, ...]:
        with self._lock:
            merged: list[PromptSubstitution] = []
            positions: dict[str, int] = {}
            errors = []
            for source in self._files:
                try:
                    substitutions = source.rules_for_submission()
                except UserPromptHookConfigurationError as error:
                    errors.append(error)
                    continue
                for substitution in substitutions:
                    if substitution.name is not None and substitution.name in positions:
                        merged[positions[substitution.name]] = substitution
                    else:
                        if substitution.name is not None:
                            positions[substitution.name] = len(merged)
                        merged.append(substitution)
            if len(errors) == 1:
                raise errors[0]
            if errors:
                raise UserPromptHookConfigurationError("\n".join(map(str, errors)), errors=tuple(errors))
            return tuple(merged)


class _PromptRulesFile:
    """Per-file cache; accessed only under its owning UserPromptHook's lock."""

    def __init__(self, path: Path, *, optional: bool = False) -> None:
        self.path = path
        self._optional = optional
        self._sha512: str | None = None
        self._substitutions: tuple[PromptSubstitution, ...] = ()
        self._cached_error: str | None = None
        self._notice_fingerprint: str | None = None
        self._version_notice = UserPromptErrorNotice()
        self._stat_error: str | None = None
        self._stat_notice = UserPromptErrorNotice()

    def _fingerprint(self) -> str | None:
        try:
            fingerprint = file_stat_sha512(os.fspath(self.path))
        except (OSError, ValueError, OverflowError, struct.error) as error:
            if self._optional and isinstance(error, FileNotFoundError):
                self._sha512 = None
                self._substitutions = ()
                self._cached_error = None
                self._notice_fingerprint = None
                self._stat_error = None
                return None
            message = f"cannot stat user prompt hooks at {self.path}: {error}"
            if message != self._stat_error:
                self._stat_error = message
                self._stat_notice = UserPromptErrorNotice()
            raise UserPromptHookConfigurationError(message, notice=self._stat_notice) from error
        self._stat_error = None
        return fingerprint

    def _version_error(self, fingerprint: str, message: str, *, cache: bool = True) -> UserPromptHookConfigurationError:
        if fingerprint != self._notice_fingerprint:
            self._notice_fingerprint = fingerprint
            self._version_notice = UserPromptErrorNotice()
        if cache:
            self._sha512 = fingerprint
            self._cached_error = message
        return UserPromptHookConfigurationError(message, notice=self._version_notice)

    def rules_for_submission(self) -> tuple[PromptSubstitution, ...]:
        fingerprint = self._fingerprint()
        if fingerprint is None:
            return ()
        if fingerprint == self._sha512:
            if self._cached_error is not None:
                raise self._version_error(fingerprint, self._cached_error)
            return self._substitutions
        # A save may replace or rewrite the file while it is being read. Never
        # cache those contents under another version's metadata fingerprint.
        for _attempt in range(3):
            try:
                with os.fdopen(os.open(self.path, os.O_RDONLY | os.O_NONBLOCK), encoding="utf-8") as source:
                    # This is a file-type guard, not a second change detector.
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise OSError("expected a regular YAML file")
                    content = source.read()
            except (OSError, UnicodeError) as error:
                if self._optional and isinstance(error, FileNotFoundError):
                    fingerprint = self._fingerprint()
                    if fingerprint is None:
                        return ()
                    continue
                raise self._version_error(
                    fingerprint, f"cannot read user prompt hooks at {self.path}: {error}"
                ) from error
            after_read = self._fingerprint()
            if after_read is None:
                return ()
            if after_read != fingerprint:
                fingerprint = after_read
                continue
            try:
                substitutions = _parse_substitutions(self.path, content)
            except UserPromptHookConfigurationError as error:
                raise self._version_error(fingerprint, str(error)) from error
            self._substitutions = substitutions
            self._sha512 = fingerprint
            self._cached_error = None
            return substitutions
        raise self._version_error(
            fingerprint, f"{self.path}: hook file kept changing while reading; submit again", cache=False
        )


def load_user_prompt_hook(
    path: Path | None = None,
    *,
    user_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> UserPromptHook:
    """Install both defaults lazily, or an explicit config plus optional overrides."""
    if path is None:
        path = USER_PROMPT_SUBSTITUTIONS_PATH
        if user_path is None:
            user_path = default_user_prompt_substitutions_path(environment)
    return UserPromptHook(path, user_path=user_path)


def _parse_substitutions(path: Path, source: str) -> tuple[PromptSubstitution, ...]:
    try:
        entries = yaml.safe_load(source)
    except (yaml.YAMLError, RecursionError) as error:
        raise UserPromptHookConfigurationError(f"invalid user prompt hook YAML at {path}: {error}") from error
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise UserPromptHookConfigurationError(f"{path}: expected a YAML list of substitution rules")
    substitutions = []
    names = set()
    for number, expression in enumerate(entries, start=1):
        label = expression.get("name") if isinstance(expression, dict) else None
        location = f"{path}: rule {number}" + (f" ({label})" if isinstance(label, str) else "")
        try:
            if isinstance(expression, dict):
                rule = _compile_named_rule(expression)
                if rule.name in names:
                    raise ValueError(f"duplicate rule name: {rule.name}")
                names.add(rule.name)
                substitutions.append(rule)
            elif isinstance(expression, str):
                substitutions.append(_compile_substitution(expression))
            else:
                raise ValueError("expected a named rule or substitution string")
        except (ValueError, re.error, IndexError, OverflowError, RecursionError) as error:
            raise UserPromptHookConfigurationError(f"{location}: {error}") from error
    return tuple(substitutions)
