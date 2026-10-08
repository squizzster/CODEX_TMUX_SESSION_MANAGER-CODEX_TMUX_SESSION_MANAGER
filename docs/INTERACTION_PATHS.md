# Interaction routes and ownership

## Typed intent

[`SessionInteractionPipeline`](../src/rodex/interaction_pipeline.py) owns target
resolution, validation, ordered content hooks, delivery, and bounded content-free
outcomes. A daemon runtime shares it across gateway, interceptors, proxy, and observer;
exact commands use the same contract under their transition lock. The observer process
uses its own instance for rendering.

Display intent does not imply model intent: `send_message(..., start_model_turn=False)`
is a notice, not a prompt. `main` may explicitly start a model turn; the observer is
a multi-agent view and has no single model-thread binding. Main open/close belongs to
session lifecycle, not pane presentation. Missing targets reject unless explicit
`open_if_missing` has a registered launch context; a label cannot invent an agent.

[`publish_session_interaction`](../src/rodex/interaction_transport.py) exposes these
operations on a private endpoint. It cannot accept arbitrary protocol frames, terminal
bytes, snapshots, launch commands, or hooks. `publish_tui_notice` uses this same path.
Hooks may transform/reject content, never change source, target, operation, turn/thread,
model permission, or dispatch identity. They run synchronously and should remain short.
Protocol edits retain non-text RPC fields; unmodified native frames preserve exact bytes.
Accepted fan-out does not apply the same hook twice.

## Production routes

| Intent/source | Authoritative path |
| --- | --- |
| Create, selector open, resume, detach | [application pipeline](../src/rodex/application_pipeline.py) → [managed lifecycle](../src/rodex/managed_session_lifecycle.py) → runtime |
| Native delegation | [`cli._exec_codex`](../src/rodex/cli.py); outside managed interaction |
| Native typing/output | decoder/interceptor → [gateway](../src/rodex/terminal_gateway.py) → child PTY / terminal surface |
| Managed initial prompt, verified Enter, queued or control text | [prompt admission](PROMPT_SUBMISSION_FLOW.md) → primary receipt or structured fallback |
| App Server frames | [proxy](../src/rodex/protocol_proxy.py) protocol input/output → original destination, then bounded projections |
| `_start`, `_steer`, `_interrupt`, alias, mouse | [exact mutation coordinator](../src/rodex/exact_turn_mutation.py) → fenced adapter |
| `_cat`, `_tail`, `_events` | [live read pipeline](../src/rodex/session_read_pipeline.py); terminal text and protocol events remain distinct |
| `_stats`, `_stats-status`, `_agents`, `_trace` | [statistics](../src/rodex/statistics_commands.py) / [trace commands](../src/rodex/agent_trace_commands.py) → owned durable reads |
| Local menu selection | [configuration](../src/rodex/input_interceptor_config.py) → submitted-command handler → typed target/operation |
| Attach/update notices | registered capability → display-only interaction |
| Analytics publication | committed SQL receipt → observer wake → bounded indexed reads |
| Resize/registration/stop/child exit | explicit supervisor or descriptor wake → runtime owner |

The proxy forwards accepted primary output to Codex before its presentation/context/
observer projections. Control-client output goes only to its destination, never into
the primary presentation. Ordinary clients use separate upstream connections; reading
or mutating does not make a control client the subscribed primary.

## Outcomes and recovery

`delivered` means transport accepted, observer admitted, or stdout flushed, not confirmed
pixels. `queued` admits a newest-state snapshot, not every intermediate message.
`model_turn_started` carries an exact turn/dispatch receipt. `rejected` performed no
requested delivery; `failed` exposes failure; `indeterminate` requires reconciliation
before retry. Observer failures cannot undo accepted work. Prompt rejection returns a
correlated RPC error without hanging the caller; other rejected native traffic closes
its connection when needed to avoid an unanswered RPC.

Model dispatch rechecks selector, connection, runtime, and thread after lock/transport
waits. Start accepts the statuses in [`_STARTABLE_THREAD_STATUSES`](../src/rodex/control.py)
(`idle` and `systemError`) unless direct input is explicitly refused. Active turns
require exact steer/interrupt intent. The current `_help` summary says idle-only;
the source predicate is authoritative for admission. See the
[automation skill](../.agents/skills/rodex-session-control/SKILL.md) for retry semantics.

[`ServerOverloadedRecoveryController`](../src/rodex/server_overloaded_recovery.py)
always watches the registered main thread, not child-thread starts on that connection.
A terminal `serverOverloaded` failure schedules `Continue...` after 30 seconds.
Repeats within five minutes double to a 960-second ceiling; a longer gap resets the
sequence. Terminal keyboard input, disconnect, close, or supersession cancels pending
work through hooks, lock waits, and transport setup until atomic dispatch admission.
Cancellation does not undo an admitted request. This is the explicit model-resubmission exception; generic
indeterminate mutations are not automatically resent.

## Terminal and presentation

[`TerminalSessionGateway`](../src/rodex/terminal_gateway.py) blocks on outer/child PTY
readiness, a wake pipe, child `pidfd`, and explicit input/handoff deadlines. Registration,
stop, persisted diagnostics, selected-view revisions, and resize wake it without a
fixed idle relay timer. [Prompt handoff](PROMPT_SUBMISSION_FLOW.md#enter-handoff)
uses bounded event-specific rechecks, not idle screen polling.

Main-thread working transitions enable a three-second native elapsed-counter refresh
deadline in the gateway. A due refresh sends the existing native resize signal without
changing terminal geometry or composer input; terminal turn/disconnect transitions
remove the deadline. Idle relaying retains its event-driven wait.

[`NativeTerminalProjection`](../src/rodex/native_terminal_projection.py) continuously
advances one [`NativeTerminalScreen`](../src/rodex/native_terminal_screen.py), even
while semantic presentation is visible. [`TerminalSurfaceRenderer`](../src/rodex/terminal_surface.py)
composites without feeding its own frames back into that native screen. Preserve:

- Exact query/UTF-8/encoding-switch boundaries when batching bytes. Cursor replies
  use child coordinates; capability and colour queries reach the real terminal.
- Complete escape tokens and synchronized updates before switching surfaces.
  Coalescing semantic frames must not discard required native bytes or replies.
- Native clear/resize/reflow ownership. A clear ends eligibility for old reflow history.
- [`SessionPresentationPipeline`](../src/rodex/presentation_policy.py) identity:
  deltas inherit the exact started item and `thread/read` uses request correlation.
  Keep hidden semantic history without rebuilding/waking an unused native-policy view;
  invalidate on selected item/event/selector eviction as well as new text.

Menus are configuration-driven, not command-name branches in decoder or renderer.
[`InputInterceptionMenu`](../src/rodex/input_menu.py) owns cyclic selection,
Enter, and back/cancel. Empty option lists are configuration errors, never empty
pickers; actionless options are placeholders. Configured typed and selected commands
converge on one handler. Unsupported editing restores held input before forwarding.
Approval/unrecognized modal input stays native. [Operations](OPERATIONS.md#terminal-settings)
owns available presentation choices.

[`TerminalInputDecoder`](../src/rodex/terminal_input.py) distinguishes lone Escape
with a 35 ms deadline; complete arrow frames decode immediately. Rodex leaves tmux's
own `escape-time` unchanged, so it can add latency. This is framing, not a menu delay.
Production uses neither `tmux send-keys` nor a tmux Enter binding or pane-piping adapter.

## Observer state

```text
App Server event → projection → producer reducer → newest complete snapshot
                                                        ↓ private framed socket
input-disabled pane ← view ← consumer reducer ← validated runtime/server frame
```

[`observer_projection`](../src/rodex/observer_projection.py) validates/bounds events.
[`ObserverStateReducer`](../src/rodex/observer_state.py) owns identity, tombstones,
epochs, revisions, and a running-agent ledger separate from presentation history.
The dispatcher in [agent_observer.py](../src/rodex/agent_observer.py) is transport,
not state authority. Consumers apply revisions once and replace state on epoch/overflow.
Root identity and the [frame contract](../src/rodex/observer_contract.py) fence admission.

The top-third, input-disabled pane stays open while any tracked agent is working,
closes at zero, and reopens with current reducer state, not the first spawn's cached
prompt. Exact request/turn identity prevents old completion from clearing newer work.
Only relevant lifecycle events reconcile panes; unrelated deltas do not query tmux.
Primary disconnect resets all participants even if one fails; only the reducer
advances epoch. Pane closure removes its tmux history, not the durable trace.

Correlation requires the exact collaboration call ID: `spawn_agent` creates a thread
and turn, `followup_task` another turn on that thread, `send_message` no new turn.
Generic `interacted` cannot identify the operation. Unbound turn-producing requests
wait FIFO for distinct target turns; `send_message` cannot acquire a later turn or
terminal recap. [SQL provenance](SQL_SCHEMA.md#agent-requests-and-trace) is authoritative.

Live collaboration prompts and exact same-turn root user text have different labels
and provenance. Missing plaintext stays unavailable, never inferred from replies.
The allowed text/privacy boundary is in [security](SECURITY.md#content-and-privacy).
Committed analytics wakes bounded indexed observer reads; no trace-polling timer is
needed. Presentation includes only active/terminal-pending exact turns.

## Effect audit

[`EFFECT_OWNERS`](../tests/adversarial/test_interaction_path_ownership.py) is the
executable per-function inventory of terminal writes, socket sends, process execution,
injected effect sinks, and SQL connections across production packages. Add/remove
classifications there when a route changes, and update the route-level map above.
It also checks subprocess entrypoints, mutation owners, and read-only bootstrap probes;
it is not a proof against arbitrary Python reflection or a runtime monitor.

Status effects remain under [`TmuxStatusPipeline`](../src/rodex/tmux_status.py);
sharing animations have their own [admission owner](../src/rodex/status_animation_admission.py).
[`CodexWorkingStatusObserver`](../src/rodex/protocol_proxy.py) projects main-thread
activity into transitions; [`TmuxWorkingStatus`](../src/rodex/tmux_status.py) atomically
publishes the activity option and redraw cadence through the existing status publisher.
tmux's status format advances the dots locally without a Rodex animation timer.
tmux effects use the [executor boundary](../src/rodex/tmux_executor.py); SQL effects
use [transactions](../src/rodex_sql/transactions.py). [Runtime isolation](RUNTIME_ISOLATION.md)
owns cleanup/hook authority, including the resize-only compatibility exception.
Selected-session upgrades use
[`RetainedRuntimeUpgrade`](../src/rodex/runtime_upgrade.py) to run a bounded
[handoff adapter](../src/rodex/retained_runtime_handoff.py) through the old retained
interpreter. Its result/error output is private to the current launcher; control
inspection, exact reservation stop and tmux ownership checks use the existing old
domain owners. Successful terminal shutdown returns to managed exact-thread resume.
See [development](DEVELOPMENT.md#test-selection) for focused tests and live gates.
