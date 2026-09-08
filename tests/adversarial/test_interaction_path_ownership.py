"""Reviewed source-wide effect inventory, not a hand-picked list of modules to scan.

New primitive callers must be classified here and in docs/INTERACTION_PATHS.md.
Behavioral tests establish delivery order; this audit catches newly introduced routes.
Python reflection cannot be proven safe by an AST inventory: imported aliases and the
current dependency-injected effect sinks are covered explicitly.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[2] / "src"

EFFECT_OWNERS = {
    "command results": {
        "rodex/agent_trace_commands.py:_show_agents:print",
        "rodex/agent_trace_commands.py:_show_or_follow_trace:print",
        "rodex/application_pipeline.py:UnifiedRodexApplicationPipeline._execute_help:print",
        "rodex/cli.py:main:print",
        "rodex/machine_commands.py:_print_machine_success:print",
        "rodex/machine_commands.py:print_machine_error:print",
        "rodex/managed_session_lifecycle.py:_attach_managed_session:print",
        "rodex/managed_session_lifecycle.py:_print_detached_runtime:print",
        "rodex/session_commands.py:_print_current_rodex_context:print",
        "rodex/session_commands.py:_print_running_sessions:print",
        "rodex/session_commands.py:_record_access_best_effort:print",
        "rodex/session_commands.py:_stream_protocol_events:print",
        "rodex/session_commands.py:execute_session_command:print",
        "rodex/statistics_commands.py:_print_human_statistics:print",
        "rodex/statistics_commands.py:execute_statistics_command:print",
        "rodex/session_commands.py:execute_session_command:.write",
        "rodex/session_tail.py:_write_lines:.write",
        "rodex/runtime.py:_erase_native_tmux_exit_message:.write",
    },
    "process ownership and native delegation": {
        "rodex/analytics.py:_lower_process_priority:subprocess.run",
        "rodex/cli.py:_exec_codex:os.execve",
        "rodex/environment_exec.py:main:os.execvpe",
        "rodex/runtime.py:run_session_host:subprocess.Popen",
        "rodex/tmux_executor.py:_run_async_command:asyncio.create_subprocess_exec",
        "rodex/analytics.py:AnalyticsSubprocessSupervisor._start_next_process:self._popen",
        "rodex/codex_update_notice.py:CodexUpdateNotice._run_version_command:self._run",
        "rodex/runtime.py:RodexRuntimeLauncher.codex_session_is_persisted:self._spawn_process",
        "rodex/tmux_executor.py:AsyncTmuxExecutor.run:self._runner",
        "rodex/tmux_executor.py:SyncTmuxExecutor.run:self._runner",
    },
    "interaction and observer transport adapters": {
        "rodex/agent_observer.py:_send_observer_event_frame:.sendall",
        "rodex/agent_observer.py:_send_observer_display_message:.sendall",
        "rodex/agent_observer.py:_observer_control_receiver:.sendall",
        "rodex/agent_observer.py:_try_send_observer_event_frame:.send",
        "rodex/control.py:_send_protocol_frame.send:.send",
        "rodex/protocol_proxy.py:CodexProtocolEventTap._handle_subscriber:.send",
        "rodex/protocol_proxy.py:CodexProtocolProxy._handle_connection.deliver_input:.send",
        "rodex/protocol_proxy.py:CodexProtocolProxy._handle_connection.deliver_output:.send",
        "rodex/protocol_proxy.py:CodexProtocolProxy._send_primary_tui_message:.send",
        "rodex/interaction_transport.py:publish_session_interaction:.send",
        "rodex/interaction_transport.py:serve_session_interaction:.send",
        "rodex/terminal_presentation.py:_write_observer_terminal:.write",
        "rodex/agent_observer.py:AgentObserverCoordinator._deliver_observer_snapshot:self._event_sender",
        "rodex/observer_pane.py:ObserverPaneController._deliver:self._state_sender",
        "rodex/observer_pane.py:ObserverPaneController._deliver:self._message_sender",
        "rodex/runtime.py:RodexRuntimeLauncher.attach:self._publish_tui_notice",
    },
    "read-only transient app server probes": {
        "rodex/runtime.py:RodexRuntimeLauncher._list_loaded_codex_threads:.send",
        "rodex/runtime.py:RodexRuntimeLauncher._read_persisted_codex_session:.send",
    },
    "storage and diagnostics": {
        "rodex_sql/transactions.py:_connect_validated_database:sqlite3.connect",
        "rodex/analytics_analyzer.py:_load_analyzer_bytes:os.write",
        "rodex/codex_update_notice.py:CodexUpdateNotice._write_cached_version:.write",
        "rodex/runtime.py:_record_runtime_path_keepalive_failure:.write",
    },
}

DIRECT_PRIMITIVES = {
    "print",
    "builtins.print",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.check_output",
    "subprocess.check_call",
    "subprocess.call",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "os.execve",
    "os.execvpe",
    "os.execvp",
    "os.execv",
    "os.system",
    "os.popen",
    "os.write",
    "sqlite3.connect",
}
INJECTED_SINKS = {
    "self._popen",
    "self._run",
    "self._spawn_process",
    "self._runner",
    "self._event_sender",
    "self._state_sender",
    "self._message_sender",
    "self._publish_tui_notice",
}
METHOD_PRIMITIVES = {"send", "sendall", "write", "writelines", "print"}


class EffectInventory(ast.NodeVisitor):
    def __init__(self, module: str, tree: ast.Module):
        self.module = module
        self.scope: list[str] = []
        self.effects: set[str] = set()
        self.aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for imported in node.names:
                    self.aliases[imported.asname or imported.name] = imported.name
            elif isinstance(node, ast.ImportFrom):
                for imported in node.names:
                    self.aliases[imported.asname or imported.name] = f"{node.module}.{imported.name}"

    def visit_ClassDef(self, node):
        self._visit_scope(node)

    def visit_FunctionDef(self, node):
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_scope(node)

    def _visit_scope(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node):
        name = ast.unparse(node.func)
        first, separator, rest = name.partition(".")
        resolved = self.aliases.get(first, first) + (separator + rest if separator else "")
        primitive = None
        if resolved in DIRECT_PRIMITIVES:
            primitive = "print" if resolved == "builtins.print" else resolved
        elif name in INJECTED_SINKS:
            primitive = name
        elif isinstance(node.func, ast.Attribute) and node.func.attr in METHOD_PRIMITIVES:
            primitive = f".{node.func.attr}"
        if primitive is not None:
            self.effects.add(f"{self.module}:{'.'.join(self.scope)}:{primitive}")
        self.generic_visit(node)


def test_every_production_effect_site_has_a_reviewed_owner():
    actual = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inventory = EffectInventory(path.relative_to(SOURCE_ROOT).as_posix(), tree)
        inventory.visit(tree)
        actual.update(inventory.effects)
    expected = set().union(*EFFECT_OWNERS.values())
    assert actual == expected, {"unclassified": sorted(actual - expected), "obsolete": sorted(expected - actual)}


def test_inventory_detects_aliases_nested_functions_and_new_modules():
    tree = ast.parse(
        "from subprocess import Popen as launch\nclass New:\n def run(self):\n  launch(['tmux'])\n  self.socket.send('x')"
    )
    inventory = EffectInventory("new/module.py", tree)
    inventory.visit(tree)
    assert inventory.effects == {"new/module.py:New.run:subprocess.Popen", "new/module.py:New.run:.send"}


def test_all_subprocess_entrypoints_are_classified():
    entrypoints = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.If) and any(
                isinstance(child, ast.Constant) and child.value == "__main__" for child in ast.walk(node.test)
            ):
                entrypoints.add(path.relative_to(SOURCE_ROOT).as_posix())
    assert entrypoints == {
        "rodex/session_host.py",
        "rodex/analytics_worker.py",
        "rodex/agent_observer.py",
        "rodex/environment_exec.py",
        "rodex/tmux_sharing_coordinator.py",
        "rodex/tmux_shared_ctrl_c.py",
        "rodex/status_animation_admission.py",
    }


def test_pane_mutations_and_model_rpc_calls_have_exact_owners():
    pane_owners = set()
    model_owners = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in {"send-keys", "pipe-pane", "new-window", "kill-window", "kill-server"}
                if node.value in {"split-window", "select-pane", "resize-pane", "kill-pane"}:
                    pane_owners.add(path.relative_to(SOURCE_ROOT).as_posix())
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"_start_turn", "_steer_turn", "_interrupt_turn"}
            ):
                model_owners.add(path.relative_to(SOURCE_ROOT).as_posix())
    assert pane_owners == {"rodex/pane_control.py"}
    assert model_owners == {"rodex/exact_turn_mutation.py"}


def test_bootstrap_app_server_requests_remain_read_only():
    tree = ast.parse((SOURCE_ROOT / "rodex/runtime.py").read_text(encoding="utf-8"))
    probes = {"_read_persisted_codex_session", "_list_loaded_codex_threads"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in probes:
            methods = {
                child.attr
                for child in ast.walk(node)
                if isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "CODEX_APP_SERVER"
                and child.attr.endswith("_method")
            }
            assert methods <= {
                "initialize_method",
                "initialized_method",
                "thread_read_method",
                "thread_loaded_list_method",
            }
