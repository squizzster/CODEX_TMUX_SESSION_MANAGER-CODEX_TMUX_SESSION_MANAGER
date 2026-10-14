from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from experiments.find_rg_policy import policy_engine
from experiments.find_rg_policy.policy_engine import Outcome, inspect, load_policy

EXPERIMENT_ROOT = Path(__file__).parents[1]
POLICY_PATH = EXPERIMENT_ROOT / "policy.toml"
CLI_PATH = EXPERIMENT_ROOT / "cli.py"


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.delenv("RIPGREP_CONFIG_PATH", raising=False)
    return load_policy(POLICY_PATH)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n")
    (tmp_path / "README.md").write_text("needle\n")
    (tmp_path / "extensionless").write_text("needle\n")
    return tmp_path


def check(command, workspace, policy):
    return inspect(command, cwd=workspace, policy=policy)


def test_find_root_rejects_statically_with_reference_message(monkeypatch, workspace, policy):
    def unexpected_probe(_path):
        raise AssertionError("the static rejection must not touch the filesystem")

    monkeypatch.setattr(policy_engine.os, "lstat", unexpected_probe)

    decision = check("find /", workspace, policy)

    assert decision.outcome is Outcome.REJECTED
    assert decision.code == "UNBOUNDED_DEPTH"
    assert not decision
    assert decision.message == (
        "Rejected: Stage 1: Directory input '/' has no maximum depth. Name a smaller subtree and an explicit "
        "depth bound; output limits and name filters do not bound traversal."
    )


def test_non_search_is_a_distinct_not_applicable_outcome(workspace, policy):
    decision = check("printf hello", workspace, policy)

    assert decision.outcome is Outcome.NOT_APPLICABLE
    assert decision.allowed is False
    assert not decision


@pytest.mark.parametrize(
    "command",
    [
        'printf "$HOME"',
        "printf 'find /'",
        "printf 'quoted; rg needle .'",
        'printf "$(find /)"',
        "bash -c 'find /'",
    ],
)
def test_indirect_or_mentioned_search_is_not_applicable(command, workspace, policy):
    assert check(command, workspace, policy).outcome is Outcome.NOT_APPLICABLE


def test_direct_search_with_dynamic_input_is_rejected(workspace, policy):
    assert check('rg needle "$HOME"', workspace, policy).code == "DYNAMIC_SHELL"


def test_environment_assignment_cannot_hide_direct_search(workspace, policy):
    assert check("ROOT=. rg needle README.md", workspace, policy).code == "UNSUPPORTED_COMMAND"


@pytest.mark.parametrize("path", ["README.md", "extensionless"])
def test_rg_accepts_verified_regular_file(path, workspace, policy):
    decision = check(f"rg needle {path}", workspace, policy)

    assert decision.outcome is Outcome.ALLOWED
    assert decision.allowed is True
    assert bool(decision) is True
    assert decision.scope["entries"] == 1
    assert decision.scope["content_files"] == 1


def test_filename_spelling_does_not_make_directory_a_file(workspace, policy):
    (workspace / "looks_like_file.json").mkdir()

    decision = check("rg needle looks_like_file.json", workspace, policy)

    assert decision.code == "UNBOUNDED_DIRECTORY"


@pytest.mark.parametrize(
    "command",
    [
        "find src -maxdepth 2 -type f",
        "rg --max-depth 2 needle src",
        "find README.md -printf '%s %p\\n'",
    ],
)
def test_bounded_inputs_are_allowed(command, workspace, policy):
    assert check(command, workspace, policy).allowed


@pytest.mark.parametrize(
    "command,code",
    [
        ("rg needle", "IMPLICIT_INPUT"),
        ("rg needle -", "UNBOUNDED_STDIN"),
        ("rg --files src", "UNBOUNDED_DIRECTORY"),
        ("find src -maxdepth 99", "DEPTH_BUDGET"),
        ("find src -maxdepth 1 -delete", "FIND_SIDE_EFFECT_OR_DYNAMIC_INPUT"),
        (r"find src -maxdepth 1 -exec rg needle '{}' \;", "FIND_SIDE_EFFECT_OR_DYNAMIC_INPUT"),
        ("rg -P needle README.md", "REGEX_ENGINE"),
        ("rg --pre cat needle README.md", "RG_EXTERNAL_OR_UNINSPECTED_INPUT"),
        ("rg -z needle README.md", "DECOMPRESSION"),
        ("rg -j99 needle README.md", "THREAD_BUDGET"),
        ("rg 'a{1,999999}' README.md", "PATTERN_EXPANSION"),
    ],
)
def test_consequential_rules_reject(command, code, workspace, policy):
    assert check(command, workspace, policy).code == code


def test_pipeline_checks_producer_and_allows_bounded_consumers(workspace, policy):
    decision = check("find src -maxdepth 2 -type f | rg py | sort | head -10", workspace, policy)

    assert decision.allowed
    assert decision.scope["entries"] == 2


def test_unrelated_stage_in_search_workflow_is_rejected_without_execution(workspace, policy):
    marker = workspace / "not-created"

    decision = check(f"rg needle README.md; touch {marker}", workspace, policy)

    assert decision.code == "UNSUPPORTED_COMMAND"
    assert not marker.exists()


def test_symlink_input_is_rejected(workspace, policy):
    (workspace / "link").symlink_to("README.md")

    assert check("rg needle link", workspace, policy).code == "SYMLINK_PATH"


def test_ordered_no_follow_options_restore_non_following_policy(workspace, policy):
    assert check("rg -L --no-follow needle README.md", workspace, policy).allowed
    assert check("find -L -P src -maxdepth 1", workspace, policy).allowed


def test_wide_shallow_tree_is_bounded(workspace, policy):
    for number in range(5):
        (workspace / "src" / str(number)).write_text("")
    narrow_policy = replace(policy, max_entries=4)

    assert check("find src -maxdepth 1", workspace, narrow_policy).code == "ENTRY_BUDGET"


def test_content_byte_limit_is_independent_of_find_inventory(workspace, policy):
    narrow_policy = replace(policy, max_file_bytes=1)

    assert check("rg needle README.md", workspace, narrow_policy).code == "FILE_BYTE_BUDGET"
    assert check("find README.md", workspace, narrow_policy).allowed


def test_hidden_ripgrep_config_requires_explicit_no_config(monkeypatch, workspace, policy):
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", "/not/read")

    assert check("rg needle README.md", workspace, policy).code == "HIDDEN_CONFIG"
    assert check("rg --no-config needle README.md", workspace, policy).allowed


def test_metadata_mode_needs_no_filesystem_census(workspace, policy):
    decision = check("rg --version", workspace, policy)

    assert decision.allowed
    assert decision.scope == {}


def test_cli_emits_message_and_machine_distinguishable_exit(workspace):
    completed = subprocess.run(
        [sys.executable, str(CLI_PATH), "--cwd", str(workspace), "--bash", "find /"],
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert completed.returncode == 2
    assert completed.stderr == ""
    assert completed.stdout.startswith("Rejected: Stage 1: Directory input '/'")


def test_cli_json_preserves_exact_truth_contract(workspace):
    completed = subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "--cwd",
            str(workspace),
            "--bash",
            "rg needle README.md",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
        env={key: value for key, value in os.environ.items() if key != "RIPGREP_CONFIG_PATH"},
    )
    payload = json.loads(completed.stdout)

    assert payload["outcome"] == "allowed"
    assert payload["allowed"] is True
    assert payload["code"] == "ALLOWED"


def test_cli_plain_allowed_result_is_literal_true(workspace):
    completed = subprocess.run(
        [sys.executable, str(CLI_PATH), "--cwd", str(workspace), "--bash", "rg needle README.md"],
        capture_output=True,
        text=True,
        timeout=3,
        env={key: value for key, value in os.environ.items() if key != "RIPGREP_CONFIG_PATH"},
    )

    assert completed.returncode == 0
    assert completed.stdout == "true\n"
    assert completed.stderr == ""


def test_cli_not_applicable_is_not_reported_as_success(workspace):
    completed = subprocess.run(
        [sys.executable, str(CLI_PATH), "--cwd", str(workspace), "--bash", "printf hello"],
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert completed.returncode == 3
    assert completed.stdout == "Not applicable: no direct find or rg command was found.\n"


def test_direct_search_parser_is_total_over_deterministic_noise(policy):
    randomizer = random.Random(4242)
    alphabet = "rg find -- ' \" $ ( ) \\ ; | {} [] abc012"

    for _ in range(500):
        command = randomizer.choice(("rg ", "find ")) + "".join(randomizer.choice(alphabet) for _ in range(80))
        decision = inspect(command, cwd="/synthetic/unavailable", policy=policy)
        assert isinstance(decision.allowed, bool)
