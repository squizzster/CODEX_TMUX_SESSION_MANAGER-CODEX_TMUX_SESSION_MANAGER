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
bytes, snapshots, launch commands, hooks, or exact-turn steer/interrupt operations.
Those mutations remain internal or use the separately fenced exact-control route.
`publish_tui_notice` uses this same path.
Hooks may transform/reject content, never change source, target, operation, turn/thread,
model permission, or dispatch identity. They run synchronously and should remain short.
Protocol edits retain non-text RPC fields; unmodified native frames preserve exact bytes.
Accepted fan-out does not apply the same hook twice.

Generic content hooks require Python composition through
`SessionInteractionPipeline(hooks=...)`. Production composes `input_text_hook` from
the [prompt-rule loader](PROMPT_SUBMISSION_FLOW.md#rule-files); there is no
user-configured generic content-hook loader.

## Production routes

| Intent/source | Authoritative path |
| --- | --- |
| Create, selector open, resume, detach | [application pipeline](../src/rodex/application_pipeline.py) → [managed lifecycle](../src/rodex/managed_session_lifecycle.py) → runtime |
| Native delegation | [`cli._exec_codex`](../src/rodex/cli.py); outside managed interaction |
| Native typing/output | decoder/interceptor → [gateway](../src/rodex/terminal_gateway.py) → child PTY / terminal surface |
| Managed initial prompt, native Enter, queued or control text | native submission → [structured prompt transformation](PROMPT_SUBMISSION_FLOW.md) → App Server |
| Changed primary prompt display | successful upstream send → bounded [expansion handoff](../src/rodex/prompt_expansion_display.py) → gateway-owned read-only surface; no extra submission |
| App Server frames | [proxy](../src/rodex/protocol_proxy.py) protocol input/output → original destination; primary output → mandatory event tap → auxiliary projections |
| Main context reaches 70% | exact numeric usage projection → [context threshold controller](../src/rodex/context_threshold_steering.py) → shared interaction pipeline → exact active-turn steer |
| Native shell command policy | App Server `PreToolUse(Bash)` → [retained helper](../src/rodex/command_policy_hook.py) → [private broker](../src/rodex/command_policy.py) → ordered [find/rg and later policies](FIND_RG_COMMAND_POLICY.md) → native Codex enforcement; then asynchronous [tmux-only status](../src/rodex/command_policy_status.py) |
| `_start`, `_steer`, `_interrupt`, alias, mouse | [exact mutation coordinator](../src/rodex/exact_turn_mutation.py) → fenced adapter |
| Active-prefix `m` | user binding when non-stock; otherwise runtime-installed fallback → current tmux session's `mouse` option |
| Keyboard `Ctrl-C` / `Ctrl-D` | native originating-client binding → [lifecycle key contract](RUNTIME_ISOLATION.md#tmux-and-observer-lifecycle) |
| `_cat`, `_tail`, `_events` | [live read pipeline](../src/rodex/session_read_pipeline.py); terminal text and protocol events remain distinct |
| Bootstrap preparation | [`prepare_installation`](../src/rodex/installation.py) → startup stderr; [preparation notice contract](../INSTALL.md#retained-installations-and-compatibility) |
| `_stats`, `_stats-status`, `_agents`, `_trace` | [statistics](../src/rodex/statistics_commands.py) / [trace commands](../src/rodex/agent_trace_commands.py) → owned durable reads |
| Local menu selection | [configuration](../src/rodex/input_interceptor_config.py) → submitted-command handler → typed target/operation |
| Attach/update notices | registered capability → display-only interaction |
| Analytics publication | committed SQL receipt → observer wake → bounded indexed reads |
| Resize/registration/stop/child exit | explicit supervisor or descriptor wake → runtime owner |

The proxy forwards accepted primary output to Codex before its presentation/context/
observer projections. Control-client output goes only to its destination, never into
the primary presentation. Ordinary clients use separate upstream connections; reading
or mutating does not make a control client the subscribed primary.

The gateway owns the post-send, connection-fenced expansion view; no terminal work
runs on the forwarding thread. [Prompt submission](PROMPT_SUBMISSION_FLOW.md#one-structured-submission-boundary)
owns its appearance, limits, expiry, and distinction from native warning notices.

### Primary conversation identity

Main model identity comes from a type-exact, connection-local correlation of the
primary's `thread/start`, `thread/resume`, or `thread/fork` request with its successful
`result.thread.id` and required boolean `result.thread.ephemeral`. It is established
before forwarding that reply. Notifications
cannot select main: child starts share the stream, and native resume need not emit
`thread/started`. Explicit temporary (`ephemeral: true`) starts/forks share typed-ID
correlation but do not select or suspend main. Their replies must confirm that kind;
ordinary requests must acknowledge a non-ephemeral thread. Pending main selection
suspends model authority; conflicting or malformed
acknowledgements invalidate it until reconnect. Input EOF revokes it immediately,
even if an output projection delays full connection release. Control and old-connection
responses cannot bind a replacement primary.

[Startup discovery](RUNTIME_ISOLATION.md#primary-conversation-discovery) reads this
same identity independently of model-steering availability. It does not introduce
another binding authority.

### Event continuity and projection failures

The event tap admits subscriptions only after primary upstream peer verification.
Its source epoch fences publication and loss under the same registration lock.
Primary loss clears snapshots, drains queued events, and interrupts subscribers;
no connection remains subscribed across a source gap. Input-transport exit also
invalidates the source before waiting for upstream close or output projections.
The proxy owns mandatory publication and invalidation, outside auxiliary resets.
This is explicit loss, not transparent reconnect or recovery of missing history.
Auxiliary callbacks run synchronously and can delay forwarding; an input callback
blocked before the next read can also delay observing EOF.

Auxiliary input/output callbacks, tool counts, presentation, context/working status,
steering/recovery observation, observer panes and analytics wakes have independent
exception boundaries. A failing participant does not skip later participants or
abort forwarded traffic. `CodexProtocolProxy.projection_failures` exposes immutable
participant/count/exception-type records, without payloads, exception messages or
tracebacks. These counters indicate degradation, not proven projection completeness.
Context status rendering is guarded separately so a display error cannot suppress
the percentage update that revokes stale steering. Failed control-producing
observations reset pending steering/recovery authority; later fresh lifecycle
evidence is required to admit new automatic work. Reset failures are counted too;
these controllers clear admission evidence before calling external cancellation.
Prompt-hook rejection, native transport failures and mandatory identity/event
observation stay outside that guard. The guard is synchronous, not a scheduling or
execution-time guarantee.

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
require exact steer/interrupt intent. See the
[automation skill](../.agents/skills/rodex-session-control/SKILL.md) for retry semantics.

### Overload recovery

[`ServerOverloadedRecoveryController`](../src/rodex/server_overloaded_recovery.py)
watches the registered main thread. It requires `turn/completed`, status `failed`,
and `error.codexErrorInfo == "serverOverloaded"`; child threads, error prose, and
nonterminal retry notifications do not qualify.

Recovery submits `Continue...` as real model input through the registered `main`
adapter, exact control, and prompt rules. Its dispatch identity is
`rodex:server-overloaded:<thread>:<failed-turn>`. The first delay is 30 seconds;
failures observed within five minutes double it up to 960 seconds. A longer gap
resets the sequence. **The gap includes the wait itself:** after a 480-second wait,
a serial failure resets if no intervening failure was observed. The ceiling does
not imply monotonically increasing serial retries.

Terminal keyboard input, disconnect, close, root rebinding, or supersession cancels
pending work through hooks, lock waits, and transport setup until atomic dispatch
admission. Cancellation cannot undo an admitted request. An external control request
is not itself a terminal-input cancellation signal; thread state is still checked
at dispatch. Recovery logs metadata but has no countdown notice or exposed YAML/CLI
toggle. This is the explicit model-resubmission exception; generic indeterminate
mutations are not automatically resent.

Evidence: `test_backoff_doubles_within_five_minutes_caps_at_sixteen_minutes_and_then_resets`
and cancellation/admission cases in
[test_server_overloaded_recovery.py](../tests/test_server_overloaded_recovery.py).

### Context threshold steering

[`ContextThresholdSteeringController`](../src/rodex/context_threshold_steering.py)
observes the registered main thread's numeric context percentage. The value comes
directly from structured App Server token usage or the authenticated exact-thread
rollout follower: `100 * totalTokens / modelContextWindow`. It does not scrape or
parse the rendered `Context: N%` status, and display rounding cannot trigger it.
Exactly `70.0` qualifies; every lower value does not.

A qualifying below-to-`>=70%` crossing schedules one asynchronous `STEER` for the
exact active main turn. [`CONTEXT_STEER_MESSAGE`](../src/rodex/context_threshold_steering.py)
owns the fixed text: request a tagged progress update at a natural break, then
continue only with clear objectives and a path forward. It is real model input.

High usage observed before registration or before a turn becomes active waits for
both authorities. Child-thread usage is ignored. Scheduling reserves the crossing;
final dispatch admission consumes it. Only the typed, local `model_thread_unbound`
refusal before admission retries, after 0.25, 1, then 2 seconds. This is finite recovery
for binding readiness, not an eventual-delivery guarantee. Exhaustion or another
failure disarms the crossing until an exact observation falls below 70%.
Compaction cancels pending work, ignores its usage snapshots, and requires a fresh
post-compaction below-to-threshold crossing. Turn completion, disconnect, root
rebinding, and close also cancel pending work.
Reconnect clears observations but does not rearm a consumed crossing; a fresh
below-threshold observation is required within the surviving controller lifetime.

The deferred request enters `SessionInteractionPipeline`, where hooks and exact target
checks still apply. After session-lock and transport waits, the exact mutation adapter
revalidates connection, runtime, thread, turn, context percentage, compaction state,
dispatch identity, and fixed text immediately before `turn/steer`. Only an admitted
`completed` result records success. Admitted attempts, other rejections, failures,
and indeterminate delivery are never resent automatically. Logs retain dispatch identity,
admission state, a content-free rejection code, and whether recovery was scheduled;
arbitrary exception bodies are not logged. Protocol forwarding never waits for this work, and the
external `/rodex-interaction` decoder does not expose `STEER`.

Evidence: [controller and race cases](../tests/test_context_threshold_steering.py),
[raw usage projection](../tests/test_rodex_protocol_proxy.py), and
[pipeline/exact-control integration](../tests/test_interaction_integration.py).
[Primary binding regressions](../tests/test_primary_thread_binding.py) reproduce
resume without a start notification. The [isolated native test](../tests/test_context_threshold_native.py)
uses a persisted/resumed native thread and synthetic local model counters to prove
one reminder reaches the same running turn, without interruption or a new turn.
Native 0.162.1 passed; SQL/tmux discovery is fixture-provided in that test.

## Model input and local notices

Within an existing composed runtime, use explicit display intent:

```python
result = interaction_pipeline.send_message(
    target="main",
    text="Rodex: local operation finished.",
    start_model_turn=False,
    source="runtime-operation",
)
```

For real model input, `start_model_turn=True` and a retained unique `dispatch_id`
use the registered model adapter and exact mutation coordinator. The
[automation skill](../.agents/skills/rodex-session-control/SKILL.md) owns external
start/steer and dispatch reconciliation. An observer pane has no single model-thread
binding. A live alias change is another
[explicit model-input side effect](CLI.md#open-detach-and-resume).

For an external display-only caller, `publish_session_interaction` or
`publish_tui_notice` in [interaction_transport.py](../src/rodex/interaction_transport.py)
requires the verified proxy endpoint and `RuntimePeerIdentity`. The private
`/rodex-interaction` endpoint admits typed operations, not arbitrary RPC or PTY bytes.
These are Python APIs; no `_notice` CLI exists.

The [proxy](../src/rodex/protocol_proxy.py) sends a downstream `warning` with the
currently attested main-thread ID, which is null while lifecycle acknowledgement
is pending or binding is unavailable.
Display delivery requires the primary connection, not a loaded thread. It opens
no upstream App Server connection and bypasses
presentation, observer, and analytics event callbacks. There is no model submission,
Rodex conversation record, or durable replay queue. `delivered` means transport
acceptance; a missing primary connection fails delivery. Terminal capture can still see
native notice output. The [proxy isolation tests](../tests/test_rodex_protocol_proxy.py)
verify this separation.

Update, prompt-error, and menu-placeholder notices use that route.

### Native notice limits

Native Codex owns placement. In the characterized **0.162.1** idle-screen probe,
`warning` and `deprecationNotice` text appeared in F2's warning center, not inline;
neither generated upstream model input. In `light`, warnings are not semantic
commentary items. Changing notification type or using hook `systemMessage` does
not establish an inline-rendering contract.

The probe bypassed the full managed gateway and did not cover active turns,
persisted history or restart; native `thread/read(includeTurns=true)` was unsupported
in that fixture. Its ad-hoc captures are not a committed regression test.
`await_native_warning` in [managed startup tests](../tests/test_managed_startup.py)
accepts either inline or F2 placement, so passing it does not prove inline display.
Use the [proxy isolation tests](../tests/test_rodex_protocol_proxy.py) for transport
separation and [native hook tests](../tests/test_command_policy_native.py) for
start/resume/fork trust—not as evidence of notice persistence.

An inline display extension needs explicit retention and native/light, editing,
resize, reconnect and restart behavior. Fabricated assistant items or model prompts
would change the display-only contract.

## Terminal and presentation

[`TerminalSessionGateway`](../src/rodex/terminal_gateway.py) blocks on outer/child PTY
readiness, a wake pipe, child `pidfd`, and explicit input/confirmation deadlines. Registration,
stop, persisted diagnostics, selected-view revisions, and resize wake it without a
fixed idle relay timer. [Local-command composer confirmation](#local-commands)
uses bounded event-specific rechecks, not idle screen polling. Prompt rules do not
rewrite the composer.

Main-thread working transitions enable the gateway's native elapsed-counter and
title deadlines. A counter refresh sends the existing native resize signal without
changing geometry or composer input; terminal turn/disconnect transitions remove
the deadlines. The gateway coalesces title frames and restores the idle field.
Title frames wait for a complete native escape, UTF-8, and synchronized-update
boundary, even after the output queue drains. Only the newest deferred title is
retained; completing a consumed terminal query also releases it.
[Terminal settings](OPERATIONS.md#terminal-settings) owns the visible cadence;
idle relaying remains event-driven.

[`NativeTerminalProjection`](../src/rodex/native_terminal_projection.py) continuously
advances one [`NativeTerminalScreen`](../src/rodex/native_terminal_screen.py), even
while semantic presentation is visible. [`TerminalSurfaceRenderer`](../src/rodex/terminal_surface.py)
composites without feeding its own frames back into that native screen. Preserve:

- Exact query/UTF-8/encoding-switch boundaries when batching bytes. Cursor replies
  use child coordinates; capability and colour queries reach the real terminal.
- Complete escape tokens and synchronized updates before switching surfaces.
  Coalescing semantic frames must not discard required native bytes or replies.
  Surface changes also wait for a paintable cursor/mode state: positioning a cursor
  at delayed autowrap would change the next character, and origin/insert modes
  change how a frame is interpreted. The latest requested surface resumes after
  native output restores a paintable state. Native reconstruction restores scroll
  margins before positioning the cursor, including its visibility.
- Native clear/resize/reflow ownership. A clear ends eligibility for old reflow history.
  Whole-row erasure disconnects both adjacent soft-wrap links; line moves disconnect
  their source/destination boundaries and move sparse blank rows over old text.
  Character shifts retain the vacated cell positions used by reflow and discard
  cells beyond the viewport. These properties matter on the next resize even when
  the current screen looks unchanged.
- Native wide-character cells. Partial overwrites clear the surviving half of an
  overlapping glyph, and combining marks stay with its leading cell before redraw
  or semantic presentation reads the screen.
- [`SessionPresentationPipeline`](../src/rodex/presentation_policy.py) identity:
  deltas inherit the exact started item and `thread/read` uses request correlation.
  Keep hidden semantic history without rebuilding/waking an unused native-policy view;
  invalidate on selected item/event/selector eviction as well as new text.

Approval/unrecognized modal input stays native.
[Operations](OPERATIONS.md#terminal-settings) owns available presentation choices.

[`TerminalInputDecoder`](../src/rodex/terminal_input.py) distinguishes lone Escape
with a 35 ms deadline; complete arrow frames decode immediately. Rodex leaves tmux's
own `escape-time` unchanged, so it can add latency. This is framing, not a menu delay.
Production uses neither `tmux send-keys` nor a tmux Enter binding or pane-piping adapter.

An admitted paste or terminal reply owns its bytes through its closing marker,
including after bounded buffering starts streaming. Reply-looking text within a
paste cannot become a reply or escape an argument picker's ignored-paste policy.
A reply's retained final `ESC` waits for its next byte without an expiry deadline;
it is not a lone keyboard Escape and must not cause repeated zero-timeout polls.

Evidence: [input framing](../tests/test_terminal_input.py),
[gateway blocking](../tests/test_terminal_gateway.py),
[native continuation across presentation changes](../tests/test_native_terminal_protocol.py),
and [real-tmux edit/resize comparisons](../tests/test_native_terminal_resize.py).
These compare subsequent updates as well as the immediate display. They do not
establish equivalence for every VT extension or identify an uncaptured live glitch.

tmux handles the active-prefix `m` shortcut before terminal input reaches the gateway.
Runtime UI initialization replaces only an unbound entry or tmux's stock
`select-pane -m` command with a session mouse toggle followed by a transient
`Mouse: ON/OFF` confirmation. Any other effective `prefix/m` binding is a user
override and remains authoritative; reconciliation does not reclaim it.

The [exit countdown](CLI.md#open-detach-and-resume) also uses native client-local
task-bar messages, not session-wide status claims or gateway input. Its timers and
destructive checks belong to [tmux lifecycle ownership](RUNTIME_ISOLATION.md#tmux-and-observer-lifecycle).

[`PlainTailCursor`](../src/rodex/session_tail.py) separates committed history from
settled visible output. It recognizes status only beside the native composer,
accepts left-justified activity labels, and retains one compaction opening per
observed compaction. [_tail behavior and sampling limits](CLI.md#observe-without-guessing-completion)
apply independently of the native redraw interval; no live session input is injected.

## Local commands

[`INPUT_INTERCEPTORS`](../src/rodex/input_interceptor_config.py) is Python
configuration for whole-input live/submitted matching, headings, options, and actions.
It is separate from prompt YAML. Decoder and renderer do not branch on command names.

The interceptor verifies the already-forwarded native prefix and end cursor before
opening completion, for example after `/ro`. `InputInterceptionMenu` in
[input_menu.py](../src/rodex/input_menu.py) owns cyclic selection, Tab completion,
Enter, and back/cancel. Typed commands and menu selections converge on
`InputInterceptorPresentation._deliver` in
[input_interceptor_presentation.py](../src/rodex/input_interceptor_presentation.py).

Accepted local handling removes only the verified prefix with DEL; it does not
submit Enter to the model. Unsupported editing during command completion restores
held text as bracketed paste before forwarding. The argument picker ignores unrelated
edit/paste keys. Empty option lists, including the current `/rodx` test registration,
produce configuration-error notices. Actionless or unmatched configured arguments
produce placeholder notices, never arbitrary command execution.

`ConfiguredInterceptionAction` currently carries `target`, `operation`, and `payload`.
Adding `MESSAGE` to its option table alone cannot supply `text` or permission to
start a model turn; that requires an explicit adapter/contract change.
Evidence: [interceptor presentation](../tests/test_input_interceptor_presentation.py),
[terminal input](../tests/test_terminal_input.py), and
[managed startup](../tests/test_managed_startup.py).

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
Primary disconnect resets all participants even if one fails; only the observer reducer
advances its observer epoch. The tap's separate source epoch fences subscriptions.
Pane closure removes its tmux history, not the durable trace.

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

[`OwnedProcessGroup`](../src/rodex/process_receipts.py) owns gated native process
execution for the runtime, gateway and catalog probe. The same lifecycle retains
authenticated pidfd authority through termination and exact receipt retirement;
the terminal gateway owns presentation and a separate exit-readiness descriptor.

Session-wide status-bar and static terminal-title configuration remain under
[`TmuxStatusPipeline`](../src/rodex/tmux_status.py). Client-local native messages for
keyboard exit, ownership rejection and mouse-toggle confirmation do not claim or
restore session-wide status options.
Its timed-notice API owns delayed restoration; `TimedStatusPublisher` owns the
coalescing runtime worker. Use `TmuxStatusPresentation.message(text)` for literal
text with control stripping and prefix indication. A launcher notice uses
`pipeline.publish_timed(..., duration=5, capability=capability)` to survive its
publisher's exit. Both expiry paths restore through the same exact claim commands;
callers supply content and duration rather than assembling delayed tmux commands or
escaping the restored bar.
Command-policy status uses its lower-priority, exact-token transient claim, never
the protocol, observer, terminal-input, or analytics publication paths. Hook trust
admission reads native inventory and requests temporary private-server configuration.
Command allow/deny/rewrite responses separately use Codex's native hook stdout contract.
Sharing animations have their own [admission owner](../src/rodex/status_animation_admission.py).
[`CodexWorkingStatusObserver`](../src/rodex/protocol_proxy.py) projects main-thread
activity into transitions; [`TmuxWorkingStatus`](../src/rodex/tmux_status.py) atomically
publishes the activity option and redraw cadence through the existing status publisher.
tmux advances the bar dots without Rodex frames. Active terminal-title timing belongs
to the existing gateway relay; [`terminal_title.py`](../src/rodex/terminal_title.py)
owns its fixed frames, elapsed rendering, and restricted control token.
tmux effects use the [executor boundary](../src/rodex/tmux_executor.py); SQL effects
use [transactions](../src/rodex_sql/transactions.py). [Runtime isolation](RUNTIME_ISOLATION.md)
owns cleanup/hook authority, including the resize-only compatibility exception.
[`RetainedRuntimeUpgrade`](../src/rodex/runtime_upgrade.py) and its
[handoff adapter](../src/rodex/retained_runtime_handoff.py) own selected-session
upgrade and `--force-old` attachment effects. [Runtime isolation](RUNTIME_ISOLATION.md#retained-runtime-upgrade)
defines their admission and identity checks; unmatched invocations retain their
Codex arguments.
See [development](DEVELOPMENT.md#test-selection) for focused tests and live gates.
