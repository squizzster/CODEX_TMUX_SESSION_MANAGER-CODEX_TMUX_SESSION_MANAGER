from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import get_type_hints

from rodex.control import CodexControlClient
from rodex_registry.agent_trace_contract import PreparedAgentTracePublication
from rodex_registry.agent_trace_writer import publish_agent_trace_in_transaction

SOURCE_ROOT = Path(__file__).parents[2] / "src"


def test_round3_generic_prompt_route_and_trace_monolith_are_removed() -> None:
    assert not hasattr(CodexControlClient, "send_prompt")
    assert not (SOURCE_ROOT / "rodex_registry" / "agent_trace.py").exists()


def test_round3_trace_writer_contract_is_prepared_only() -> None:
    hints = get_type_hints(publish_agent_trace_in_transaction)
    assert hints["publication"] is PreparedAgentTracePublication
    writer_source = inspect.getsource(publish_agent_trace_in_transaction)
    assert "prepare_agent_trace_publication" not in writer_source
    assert "RodexAgentTracePublication" not in writer_source


def test_round3_mutation_lock_has_one_command_domain_owner() -> None:
    machine_source = (SOURCE_ROOT / "rodex" / "machine_commands.py").read_text()
    session_source = (SOURCE_ROOT / "rodex" / "session_commands.py").read_text()
    control_source = (SOURCE_ROOT / "rodex" / "control.py").read_text()
    coordinator_source = (SOURCE_ROOT / "rodex" / "exact_turn_mutation.py").read_text()

    assert "session_transition_lock" in coordinator_source
    assert "session_transition_lock" not in machine_source
    assert "session_transition_lock" not in session_source
    assert "session_transition_lock" not in control_source
    assert ".start_turn(" not in machine_source
    assert ".steer_turn(" not in machine_source
    assert ".interrupt_turn(" not in machine_source


def test_round3_mutation_transport_is_reachable_only_from_the_coordinator() -> None:
    forbidden_public_methods = {"start_turn", "steer_turn", "interrupt_turn"}
    assert forbidden_public_methods.isdisjoint(vars(CodexControlClient))

    transport_calls = {"_start_turn", "_steer_turn", "_interrupt_turn"}
    callers: dict[str, set[str]] = {name: set() for name in transport_calls}
    for source_path in (SOURCE_ROOT / "rodex").glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in transport_calls:
                callers[node.func.attr].add(source_path.name)

    assert callers == {
        "_start_turn": {"exact_turn_mutation.py"},
        "_steer_turn": {"exact_turn_mutation.py"},
        "_interrupt_turn": {"exact_turn_mutation.py"},
    }


def test_round3_alias_has_one_coordinator_entry_without_public_lock_fragments() -> None:
    coordinator_source = (SOURCE_ROOT / "rodex" / "exact_turn_mutation.py").read_text(
        encoding="utf-8"
    )
    session_source = (SOURCE_ROOT / "rodex" / "session_commands.py").read_text(
        encoding="utf-8"
    )

    for obsolete_name in (
        "LockedSessionSelection",
        "ExactTurnTarget",
        "locked_selector",
        "resolve_locked_live_target",
        "locked_session_names",
        "revalidate_locked_runtime",
        "deliver_information_to_locked_runtime",
    ):
        assert obsolete_name not in session_source
    assert "def alias_transition(" in coordinator_source
    assert "session_transition_lock" not in session_source


def test_round3_request_reconciliation_has_no_independent_public_entry() -> None:
    source_path = SOURCE_ROOT / "rodex_registry" / "agent_request_reconciliation.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    public_functions = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }

    assert public_functions == set()
