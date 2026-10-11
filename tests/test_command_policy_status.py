from __future__ import annotations

import time

from test_tmux_status import FakeTmux

from rodex.command_policy_status import POLICY_INACTIVE_OPTION, POLICY_STATUS_PUBLISHER, CommandPolicyStatus
from rodex.tmux_session_capability import TmuxRuntimeCapability
from rodex.tmux_status import (
    RODEX_STATUS_LEFT_FORMAT,
    STATUS_CLAIM_TOKEN_OPTION,
    StatusPriority,
    TmuxStatusPipeline,
    TmuxStatusPresentation,
)
from rodex_registry import RodexRuntimeId


def make_status(tmp_path, monkeypatch):
    tmux = FakeTmux()
    monkeypatch.setattr(CommandPolicyStatus, "_tmux", lambda _self, *args: tmux(*args))
    status = CommandPolicyStatus(
        "tmux",
        TmuxRuntimeCapability(tmp_path / "tmux.sock", "a" * 32, "$1", "%4", RodexRuntimeId(1)),
        "%4",
        duration=0.05,
    )
    return status, tmux


def test_active_policy_clears_stale_inactive_metadata_and_its_status_claim(tmp_path, monkeypatch):
    status, tmux = make_status(tmp_path, monkeypatch)
    status.inactive("stale inactive reason")
    assert tmux.options[POLICY_INACTIVE_OPTION] == "stale inactive reason"
    assert TmuxStatusPipeline(tmux, "%4").publish_transient(
        publisher=POLICY_STATUS_PUBLISHER,
        token="inactive-token",
        priority=StatusPriority.COMMAND_POLICY,
        presentation=TmuxStatusPresentation.message("stale inactive reason"),
    )

    status.active()

    assert POLICY_INACTIVE_OPTION not in tmux.options
    assert STATUS_CLAIM_TOKEN_OPTION not in tmux.options
    assert tmux.status_left == RODEX_STATUS_LEFT_FORMAT


def test_each_allowed_command_uses_the_shared_message_lifecycle(tmp_path, monkeypatch):
    status, tmux = make_status(tmp_path, monkeypatch)
    try:
        for command in ("printf first", "printf second"):
            status.allowed(command)
            deadline = time.monotonic() + 1
            while STATUS_CLAIM_TOKEN_OPTION not in tmux.options and time.monotonic() < deadline:
                time.sleep(0.005)
            assert tmux.options["status-format[0]"] == (
                TmuxStatusPresentation.message(f"Rodex policy allowed: {command}").status_format
            )

            deadline = time.monotonic() + 1
            while STATUS_CLAIM_TOKEN_OPTION in tmux.options and time.monotonic() < deadline:
                time.sleep(0.005)
            assert STATUS_CLAIM_TOKEN_OPTION not in tmux.options
            assert "status-format[0]" not in tmux.options
            assert tmux.status_left == RODEX_STATUS_LEFT_FORMAT
    finally:
        status.close()
