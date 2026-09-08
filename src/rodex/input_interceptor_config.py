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
class InterceptionOption:
    name: str
    helper_text: str

    def __post_init__(self) -> None:
        if not self.name or any(character.isspace() for character in self.name):
            raise ValueError("an interception option requires a single-token name")


@dataclass(frozen=True)
class ArgumentMenuConfig:
    heading: str = "Select argument:"
    subheading: str = ""
    options: tuple[InterceptionOption, ...] = ()

    def __post_init__(self) -> None:
        if len({option.name for option in self.options}) != len(self.options):
            raise ValueError("interception option names must be unique")


@dataclass(frozen=True)
class InputInterceptorRegistration:
    name: str
    completion_text: str
    live: LiveInterceptionRule
    on_enter: InterceptionRule
    argument_menu: ArgumentMenuConfig = ArgumentMenuConfig()

    def __post_init__(self) -> None:
        if not self.name or not self.completion_text or not self.completion_text.isascii():
            raise ValueError("an interception requires a name and ASCII completion text")
        if any(character.isspace() for character in self.completion_text):
            raise ValueError("completion text must be one token")
        if not self.live.matches(self.completion_text):
            raise ValueError("the live expression must match its completion text")
        if any(not self.on_enter.matches(self.option_submission(option)) for option in self.argument_menu.options):
            raise ValueError("the Enter expression must accept every configured option")

    def option_submission(self, option: InterceptionOption) -> str:
        return f"{self.completion_text} {option.name}"

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
        argument_menu=ArgumentMenuConfig(
            heading="This line here should be in the config, select argument:",
            subheading="This 2nd line should also be configurable for the rodex...",
            options=(
                InterceptionOption("light", "Light test argument"),
                InterceptionOption("dark", "Dark one"),
                InterceptionOption("dusk", "Dusky one"),
            ),
        ),
    ),
    InputInterceptorRegistration(
        name="rodx",
        completion_text="/rodx",
        live=LiveInterceptionRule(
            reg_exp_intercept=r"^/ro(?:dx?)?$",
            helper_text="dummy test to see if we can move up and down in the rodex/rodx menu here",
        ),
        on_enter=InterceptionRule(reg_exp_intercept=r"^/rodx (.*?)$", flags=re.MULTILINE),
    ),
)
