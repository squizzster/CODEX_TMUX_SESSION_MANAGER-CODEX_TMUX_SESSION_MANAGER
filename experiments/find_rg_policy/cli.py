#!/usr/bin/env python3
"""Inspect one literal Bash command without executing it."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from policy_engine import Outcome, inspect, inspect_search_workflows, load_policy

EXPERIMENT_ROOT = Path(__file__).resolve().parent
DEFAULT_POLICY = EXPERIMENT_ROOT / "policy.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bash", required=True, dest="command", help="literal find/rg shell workflow to inspect")
    parser.add_argument("--cwd", default=os.getcwd(), help="absolute working directory for resolving literal paths")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY, help="TOML policy file")
    parser.add_argument("--json", action="store_true", help="emit the structured decision")
    parser.add_argument(
        "--static",
        action="store_true",
        help="do not inspect filesystem metadata; scoped inputs report needs_filesystem",
    )
    parser.add_argument(
        "--extract",
        action="store_true",
        help="extract and inspect every direct top-level find/rg workflow in the supplied shell text",
    )
    arguments = parser.parse_args(argv)
    try:
        policy = load_policy(arguments.policy)
        if arguments.extract:
            decisions = inspect_search_workflows(
                arguments.command,
                cwd=arguments.cwd,
                policy=policy,
                verify_filesystem=not arguments.static,
            )
        else:
            decision = inspect(
                arguments.command,
                cwd=arguments.cwd,
                policy=policy,
                verify_filesystem=not arguments.static,
            )
    except (OSError, TypeError, ValueError) as exc:
        print(f"Configuration/input error: {exc}", file=sys.stderr)
        return 3
    if arguments.extract:
        if not decisions:
            print(policy.render("not_applicable"))
            return 3
        if arguments.json:
            print(
                json.dumps(
                    [item.to_dict() for item in decisions],
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
            )
        else:
            for item in decisions:
                result = "true" if item.decision.allowed else item.decision.message
                print(f"{item.workflow.ordinal}\t{item.workflow.start_line}\t{result}")
        return 0 if all(item.decision.allowed for item in decisions) else 2
    if arguments.json:
        print(json.dumps(decision.to_dict(), ensure_ascii=True, separators=(",", ":")))
    else:
        print("true" if decision.allowed else decision.message)
    if decision.outcome is Outcome.ALLOWED:
        return 0
    if decision.outcome in {Outcome.REJECTED, Outcome.NEEDS_FILESYSTEM}:
        return 2
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
