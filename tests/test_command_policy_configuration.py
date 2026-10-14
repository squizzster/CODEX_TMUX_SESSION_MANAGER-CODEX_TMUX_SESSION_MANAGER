"""Narrow native hook admission and invocation-local trust adaptation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rodex import command_policy_configuration as configuration

HOOK_TRUST = configuration.NativePolicyHookTrust("/<session-flags>/config.toml:pre_tool_use:0:0", "sha256:" + "a" * 64)


class NativeTrustConnection:
    def __init__(self, hooks, inherited_bypass=False, *, errors=None, warnings=None):
        self.hooks = hooks
        self.inherited_bypass = inherited_bypass
        self.errors = [] if errors is None else errors
        self.warnings = [] if warnings is None else warnings
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv(self, **_kwargs):
        request = self.sent[-1]
        result = {}
        if request["method"] == "hooks/list":
            result = {
                "data": [
                    {
                        "cwd": "/workspace",
                        "errors": self.errors,
                        "hooks": self.hooks,
                        "warnings": self.warnings,
                    }
                ]
            }
        elif request["method"] == "config/read":
            result = {"config": {"bypass_hook_trust": self.inherited_bypass}}
        return json.dumps({"id": request["id"], "result": result})


def rodex_hook(*, trusted=False, enabled=True, **changes):
    hook = {
        "additionalContextLimit": None,
        "async": False,
        "command": configuration.policy_helper_command(),
        "currentHash": "sha256:" + "a" * 64,
        "displayOrder": 0,
        "enabled": enabled,
        "eventName": "preToolUse",
        "handlerType": "command",
        "isManaged": False,
        "key": "/<session-flags>/config.toml:pre_tool_use:0:0",
        "matcher": "^Bash$",
        "pluginId": None,
        "source": "sessionFlags",
        "sourcePath": "/<session-flags>/config.toml",
        "statusMessage": None,
        "timeoutSec": configuration.POLICY_HOOK_TIMEOUT_SECONDS,
        "trustStatus": "trusted" if trusted else "untrusted",
    }
    hook.update(changes)
    return hook


def user_hook(*, enabled=True):
    return {
        "command": "/home/user/check-command",
        "enabled": enabled,
        "eventName": "preToolUse",
        "matcher": "^Bash$",
        "source": "user",
    }


def admit(monkeypatch, hooks, arguments=(), inherited=False, *, expected_trust=None, **connection_options):
    connection = NativeTrustConnection(hooks, inherited, **connection_options)
    monkeypatch.setattr(configuration, "unix_connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(configuration, "require_unix_peer_process", lambda *_args: None)
    admission = configuration.policy_hook_admission(
        Path("/app.sock"), object(), Path("/workspace"), arguments, expected_trust=expected_trust
    )
    return admission, connection


@pytest.mark.parametrize(
    ("hooks", "arguments", "inherited", "expected"),
    [
        (rodex_hook(trusted=True), (), False, configuration.PolicyHookAdmission()),
        (
            rodex_hook(),
            (),
            False,
            configuration.PolicyHookAdmission(temporary_trust=HOOK_TRUST),
        ),
        (
            rodex_hook(trustStatus="modified"),
            (),
            False,
            configuration.PolicyHookAdmission(temporary_trust=HOOK_TRUST),
        ),
        (
            rodex_hook(),
            (configuration.POLICY_HOOK_TRUST_BYPASS_ARGUMENT,),
            False,
            configuration.PolicyHookAdmission(temporary_trust=HOOK_TRUST),
        ),
        (rodex_hook(), (), True, configuration.PolicyHookAdmission(temporary_trust=HOOK_TRUST)),
        (
            rodex_hook(),
            ("-c", "bypass_hook_trust=false"),
            True,
            configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_SETUP),
        ),
        (
            rodex_hook(enabled=False),
            (configuration.POLICY_HOOK_TRUST_BYPASS_ARGUMENT,),
            True,
            configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_CONFIG),
        ),
        (
            [rodex_hook(), user_hook()],
            (),
            False,
            configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_SETUP),
        ),
        (
            [rodex_hook(trustStatus="modified"), user_hook()],
            (),
            False,
            configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_SETUP),
        ),
        (
            rodex_hook(trustStatus="modified"),
            ("-c", "bypass_hook_trust=false"),
            False,
            configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_SETUP),
        ),
        (
            [rodex_hook(), user_hook(enabled=False)],
            (),
            False,
            configuration.PolicyHookAdmission(temporary_trust=HOOK_TRUST),
        ),
        (
            [rodex_hook(trusted=True), user_hook()],
            (),
            False,
            configuration.PolicyHookAdmission(),
        ),
        (
            [rodex_hook(), user_hook()],
            (configuration.POLICY_HOOK_TRUST_BYPASS_ARGUMENT,),
            False,
            configuration.PolicyHookAdmission(),
        ),
        (
            [rodex_hook(), user_hook()],
            (),
            True,
            configuration.PolicyHookAdmission(),
        ),
    ],
)
def test_native_verdicts_only_temporarily_trust_the_sole_enabled_rodex_hook(
    monkeypatch,
    hooks,
    arguments,
    inherited,
    expected,
):
    hook_list = hooks if isinstance(hooks, list) else [hooks]
    admission, connection = admit(monkeypatch, hook_list, arguments, inherited)

    assert admission == expected
    assert {request["method"] for request in connection.sent} <= {
        "initialize",
        "initialized",
        "hooks/list",
        "config/read",
    }


@pytest.mark.parametrize("trust_status", ["untrusted", "modified"])
@pytest.mark.parametrize(
    "changes",
    [
        {"command": "/tmp/unexpected-hook"},
        {"sourcePath": "/home/user/.codex/hooks.json"},
        {"handlerType": "prompt"},
        {"timeoutSec": 30},
        {"async": True},
        {"statusMessage": "Checking"},
        {"additionalContextLimit": 100},
        {"trustStatus": "future-native-status"},
        {"currentHash": "sha256:invalid"},
    ],
)
def test_changed_or_unknown_rodex_hook_is_never_automatically_trusted(monkeypatch, changes, trust_status):
    admission, _connection = admit(monkeypatch, [rodex_hook(**{"trustStatus": trust_status, **changes})])

    assert admission == configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_CONFIG)


@pytest.mark.parametrize(
    ("hooks", "expected"),
    [
        ([rodex_hook(trusted=True)], configuration.PolicyHookAdmission()),
        ([rodex_hook()], configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_CONFIG)),
        (
            [rodex_hook(trusted=True, key="changed-native-key")],
            configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_CONFIG),
        ),
        (
            [rodex_hook(trusted=True, currentHash="sha256:" + "b" * 64)],
            configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_CONFIG),
        ),
        (
            [rodex_hook(trusted=True, enabled=False)],
            configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_CONFIG),
        ),
        (
            [rodex_hook(trusted=True, command="/tmp/replaced-helper")],
            configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_CONFIG),
        ),
        (
            [rodex_hook(trusted=True), user_hook()],
            configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_SETUP),
        ),
    ],
)
def test_temporary_admission_requires_native_confirmation_of_the_same_hook(monkeypatch, hooks, expected):
    admission, _connection = admit(monkeypatch, hooks, expected_trust=HOOK_TRUST)

    assert admission == expected


def test_inventory_diagnostics_are_inactive_not_implicitly_trusted(monkeypatch):
    admission, _connection = admit(monkeypatch, [rodex_hook()], warnings=[{"message": "fixture warning"}])

    assert admission == configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_UNAVAILABLE)


def test_compatibility_inactive_reason_projects_the_admission(monkeypatch):
    connection = NativeTrustConnection([rodex_hook(), user_hook()])
    monkeypatch.setattr(configuration, "unix_connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(configuration, "require_unix_peer_process", lambda *_args: None)

    assert configuration.policy_hook_inactive_reason(Path("/app.sock"), object(), Path("/workspace"), ()) == (
        configuration.POLICY_INACTIVE_SETUP
    )


def test_unavailable_native_trust_is_inactive_not_implicitly_trusted(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(configuration, "unix_connect", unavailable)
    assert configuration.policy_hook_admission(Path("/app.sock"), object(), Path("/workspace"), ()) == (
        configuration.PolicyHookAdmission(configuration.POLICY_INACTIVE_UNAVAILABLE)
    )
