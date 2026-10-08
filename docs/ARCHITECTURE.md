# Architecture

Rodex preserves native Codex behavior while adding durable session identity and exact
local control. It is more than a protocol relay: terminal projection, lifecycle
admission, observer state, and rollout analytics have separate owners.

```text
caller → retained installation → CLI → implementation-scoped daemon
                                  │     ├─ runtime A: tmux A → TUI ↔ proxy ↔ App Server
                                  │     ├─ runtime B: tmux B → TUI ↔ proxy ↔ App Server
                                  │     └─ one analytics coordinator → SQLite
                                  └─ registry / verified reads / native passthrough
```

## Application and lifecycle

- [`UnifiedRodexApplicationPipeline`](../src/rodex/application_pipeline.py)
  classifies once using the [Rodex command contract](../src/rodex/command_contract.py)
  and [characterized Codex grammar](../src/codex_cli_contract/v0_151_0.py).
  [`cli.run`](../src/rodex/cli.py) composes dependencies; handlers do not duplicate
  domain policy. [CLI routing](CLI.md#invocation-routing) explains ambiguous input.
- [`ManagedSessionLifecycle`](../src/rodex/managed_session_lifecycle.py) owns create,
  open, resume, and recovery. [`RodexRuntimeLauncher`](../src/rodex/runtime.py) stages
  tmux and native children. [`DaemonRuntimeManager`](../src/rodex/daemon.py) owns
  reservations, TTY admission, runtime workers, and stop receipts.
  [`RetainedRuntimeUpgrade`](../src/rodex/runtime_upgrade.py) delegates a selected
  older idle runtime's shutdown to its owning interpreter, then returns to ordinary
  exact-thread resume and CAS registration on the current implementation.
- [`ExactTurnMutationCoordinator`](../src/rodex/exact_turn_mutation.py) owns start,
  steer, interrupt, mouse, and alias transitions. It resolves, locks, re-resolves,
  then revalidates immediately before mutation. Transport is not a public unfenced
  prompt API. [Runtime isolation](RUNTIME_ISOLATION.md) owns identity/lifetime rules.
- Cross-system transitions do external work outside SQLite writer transactions and
  compensate only resources changed by that operation. Post-success access telemetry
  is best-effort; its failure must not make successful model work retryable.

## Interaction and presentation

- [`SessionInteractionPipeline`](../src/rodex/interaction_pipeline.py) owns typed
  intent, target validation, content hooks, delivery, and outcome records. Adapters
  return to this contract; they must not partially reimplement its policy.
- [`TerminalSessionGateway`](../src/rodex/terminal_gateway.py) is the one native PTY
  input/output owner, regardless of attached-client count. The foreground
  [terminal bridge](../src/rodex/terminal_bridge.py) transfers its TTY and resize
  notifications without consuming native input.
- [Interaction paths](INTERACTION_PATHS.md) maps terminal, protocol, menu, observer,
  and background effects to owners. [Prompt submission](PROMPT_SUBMISSION_FLOW.md)
  owns the pre-Enter and structured-input contract; Codex still owns the editor.
- [`PrimaryConnectionLifecycleCoordinator`](../src/rodex/primary_connection_lifecycle.py)
  resets every participant even if one fails. Observer epoch ownership remains in
  the reducer, not in reset callers or transport.

## Persistence and resources

- [`SharedAnalyticsCoordinator`](../src/rodex/analytics.py) serializes runtime workers
  on one daemon thread, preserving separate cursors, retries, and health. See
  [analytics](ANALYTICS.md) for bounded work and incomplete-coverage semantics.
- [`rodex_registry`](../src/rodex_registry/__init__.py) owns durable session/lineage
  transitions and publication. The [trace contract](../src/rodex_registry/agent_trace_contract.py)
  prepares facts before SQL; its [writer](../src/rodex_registry/agent_trace_writer.py)
  appends them inside the caller's transaction. [SQL methodology](SQL_SCHEMA.md)
  owns schema standards, identity domains, and atomicity.
- [`rodex_sql.transactions`](../src/rodex_sql/transactions.py) alone creates SQLite
  connections and transaction boundaries. No background database watcher exists.
- [`SyncTmuxExecutor` and `AsyncTmuxExecutor`](../src/rodex/tmux_executor.py) alone
  launch tmux. Domain owners supply arguments/capabilities, never a second subprocess
  boundary. Captured calls are deadline-bounded; attachment has its natural lifetime.

The [effect-owner audit](../tests/adversarial/test_interaction_path_ownership.py)
enforces production effect boundaries. Keep new routes classified in that test and
[the interaction inventory](INTERACTION_PATHS.md#effect-audit), not in a parallel
informal call graph. Executable/privacy assumptions live in [security](SECURITY.md).
