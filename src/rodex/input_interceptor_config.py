"""Each interception declares its live and submitted rules; transport knows no names."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class InterceptionRule:
    reg_exp_intercept: str
    flags: re.RegexFlag = re.NOFLAG

    def __post_init__(self) -> None:
        pattern = re.compile(self.reg_exp_intercept, self.flags)
        if not self.reg_exp_intercept.startswith("^") or not self.reg_exp_intercept.endswith("$"):
            raise ValueError("interception expressions must explicitly anchor the complete input")
        if pattern.fullmatch("") is not None:
            raise ValueError("interception expressions must not match empty input")

    def matches(self, text: str) -> bool:
        """Both phases match the entire input, never a matching line inside another message."""
        return re.fullmatch(self.reg_exp_intercept, text, self.flags) is not None


@dataclass(frozen=True)
class LiveInterceptionRule(InterceptionRule):
    helper_text: str = ""


@dataclass(frozen=True)
class InterceptionCommand:
    name: str
    helper_text: str


@dataclass(frozen=True)
class InputInterceptorRegistration:
    name: str
    completion_text: str
    live: LiveInterceptionRule
    on_enter: InterceptionRule
    command_list: tuple[InterceptionCommand, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not self.completion_text or not self.completion_text.isascii():
            raise ValueError("an interception requires a name and ASCII completion text")
        if any(character.isspace() for character in self.completion_text):
            raise ValueError("completion text must be one token")
        if not self.live.matches(self.completion_text):
            raise ValueError("the live expression must match its completion text")
        if len({command.name for command in self.command_list}) != len(self.command_list):
            raise ValueError("interception command names must be unique")

    @property
    def target(self) -> str:
        return f"input-interceptor:{self.name}"


INPUT_INTERCEPTORS = (
    InputInterceptorRegistration(
        name="rodex",
        completion_text="/rodex",
        live=LiveInterceptionRule(
            reg_exp_intercept=r"^/ro(?:d(?:ex?)?)?$",
            helper_text="issue a rodex command",
        ),
        on_enter=InterceptionRule(reg_exp_intercept=r"^/rodex (.*?)$", flags=re.MULTILINE),
    ),
)
