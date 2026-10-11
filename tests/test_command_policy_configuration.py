"""Native trust admission is read-only and never invents a bypass."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rodex import command_policy_configuration as configuration


class NativeTrustConnection:
    def __init__(self, hooks, inherited_bypass=False):
        self.hooks = hooks
        self.inherited_bypass = inherited_bypass
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
            result = {"data": [{"cwd": "/workspace", "hooks": self.hooks}]}
        elif request["method"] == "config/read":
            result = {"config": {"bypass_hook_trust": self.inherited_bypass}}
        return json.dumps({"id": request["id"], "result": result})


@pytest.mark.parametrize(
    "trusted,enabled,arguments,inherited,expected",
    [
        (True, True, (), False, None),
        (False, True, (), False, configuration.POLICY_INACTIVE_SETUP),
        (False, True, ("--dangerously-bypass-hook-trust",), False, None),
        (False, True, (), True, None),
        (False, True, ("-c", "bypass_hook_trust=false"), True, configuration.POLICY_INACTIVE_SETUP),
        (True, False, ("--dangerously-bypass-hook-trust",), True, configuration.POLICY_INACTIVE_CONFIG),
    ],
)
def test_native_verdicts_and_explicit_false_override_inheritance(
    monkeypatch,
    trusted,
    enabled,
    arguments,
    inherited,
    expected,
):
    hook = {
        "command": configuration.policy_helper_command(),
        "source": "sessionFlags",
        "eventName": "preToolUse",
        "matcher": "^Bash$",
        "enabled": enabled,
        "trustStatus": "trusted" if trusted else "untrusted",
    }
    connection = NativeTrustConnection([hook], inherited)
    monkeypatch.setattr(configuration, "unix_connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(configuration, "require_unix_peer_process", lambda *_args: None)
    assert (
        configuration.policy_hook_inactive_reason(Path("/app.sock"), object(), Path("/workspace"), arguments) == expected
    )
    assert {request["method"] for request in connection.sent} <= {
        "initialize",
        "initialized",
        "hooks/list",
        "config/read",
    }


def test_unavailable_native_trust_is_inactive_not_implicitly_trusted(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(configuration, "unix_connect", unavailable)
    assert configuration.policy_hook_inactive_reason(Path("/app.sock"), object(), Path("/workspace"), ()) == (
        configuration.POLICY_INACTIVE_UNAVAILABLE
    )
