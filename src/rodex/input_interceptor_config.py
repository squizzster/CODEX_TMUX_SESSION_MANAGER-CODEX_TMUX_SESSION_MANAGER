"""Declarative terminal interception registrations; transport knows no command names."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class InputInterceptorRegistration:
    name: str
    match_pattern: str
    command: str
    description: str

    def __post_init__(self) -> None:
        if not self.name or not self.command or not self.command.isascii():
            raise ValueError("an input interceptor requires a name and an ASCII completion command")
        if any(character.isspace() for character in self.command):
            raise ValueError("an interceptor command must be one token")
        pattern = re.compile(self.match_pattern)
        if not self.match_pattern.startswith("^") or not self.match_pattern.endswith("$"):
            raise ValueError("match patterns must explicitly anchor the complete candidate")
        if pattern.fullmatch(self.command) is None or pattern.fullmatch("") is not None:
            raise ValueError("match pattern must match its command, not empty input")

    def matches(self, text: str) -> bool:
        """The sole admission predicate, shared by every terminal input route."""
        return re.fullmatch(self.match_pattern, text) is not None

    @property
    def target(self) -> str:
        return f"input-interceptor:{self.name}"


INPUT_INTERCEPTORS = (
    InputInterceptorRegistration(
        name="rodex",
        match_pattern=r"^/ro(?:d(?:ex?)?)?$",
        command="/rodex",
        description="issue a Rodex command",
    ),
)
