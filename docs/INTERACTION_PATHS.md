# Interaction contract and production-path inventory

Rodex 0.14.0a2, ALPHA. SQL generation 20, isolated tmux protocol v4, runtime peer
contract v5, daemon protocol v2 and observer schema v3 form the current boundary.
Old contracts are rejected.

## Authoritative contract

`SessionInteractionPipeline` owns target resolution, validation, ordered content hooks,
post-hook checks, delivery, bounded content-free outcome records and exact outcome subscriptions. Each daemon runtime shares one
instance between terminal gateway, interceptors, proxy and observer. Exact-turn commands use the same contract inside
their existing transition lock. The observer renderer uses it in its own process.

```python
pipeline.send_message(target="main", text="Visible information", start_model_turn=False)
pipeline.send_message(target="agent-observer", text="Visible information", start_model_turn=False)
pipeline.send_message(target="main", text="Please investigate", start_model_turn=True)
```

`publish_session_interaction(socket_path, InteractionRequest(...), peer_identity=...)` submits the same
operations over the private `/rodex-interaction` endpoint. It cannot submit raw protocol
frames, raw terminal bytes, interceptor events, observer snapshots, launch commands or hooks. `publish_tui_notice` is an explicit
display-only convenience function using this transport, not a second delivery path.

| Target | Display | Model input | Pane operations |
|---|---|---|---|
| `main` | Native Codex warning/scrollback | Registered, currently displayed thread; idle-only start | Locate, focus, resize |
| `agent-observer` | Observer text | Rejected: multi-agent view has no single thread binding | Open/reuse, locate, focus, resize, close |
| Registered model target | No independent display | Exact coordinator start, steer or interrupt | None |
| Per-connection protocol target | Input/output frames | Native RPC intent preserved | None |
| `terminal` | Native output and configured inline completion | Native keyboard bytes; no implicit model intent | None |
| `input-interceptor-menu` | Shared command/argument view and release | Selection only; never starts a turn | None |
| `input-interceptor:<name>` | Configured action, placeholder or configuration error | Local handler; no implicit model turn | None |
| `presentation-policy` | Select configured native/semantic main viewport | Never changes model/protocol input | None |

Future agent-chat targets need an explicit thread and adapter; labels never imply one.
Primary open/close are session lifecycle operations, not pane-control operations.
Model input uses normal conversation echo, without printing a duplicate prompt.
Starting an active thread fails; steering requires its own operation and exact turn ID.
Alias announcements deliberately start when idle or steer the observed active turn.

Missing targets reject by default. Explicit `open_if_missing` can reopen a registered
presentation with known launch context, but cannot invent an agent. A target disappearing
mid-operation is rejected. Pane mutations carry the frozen pane ID into tmux. Model
dispatch rechecks selected connection, runtime and thread after lock/transport waits.
Deferred recovery also atomically admits its trusted cancellation generation at that
final boundary. Input, close, disconnect or supersession before admission rejects it;
after admission, accepted/indeterminate semantics remain unchanged.
Observer messages wait boundedly for socket readiness, send once and require exact-pane
admission. Reopening uses a blank view plus current reducer state, not the first spawn's
cached event. The snapshot budget reserves transport-address overhead.

## Processing and outcomes

Submitted user input first enters the runtime's typed `input_text_hook`. Its only
authority is ordered text edits; `protocol_input_text` alone applies those edits and
rebases/removes affected UTF-8 UI spans, preserving every other RPC field. Initial,
native, exact-control, and queued submissions converge here. The hook re-stats
the installation's `conf/hooks/user_prompt_substitutions.yaml` and the optional user
file under the caller's Rodex state root on submission. Both use the supplied
metadata-only SHA-512 function and independent caches; only changed files reload
before processing. User rules replace same-name global rules in place; new rules
append. One runtime lock owns the combined snapshot. Invalid versions are cached as
errors, never replaced by stale rules. A descriptive `main MESSAGE(false)` notice is
delivered once per failed version of each file; failures to deliver
remain eligible for the next attempt. A correlated RPC error refuses the submission
through that connection's existing protocol-output pipeline without closing the
connection. No timer or keystroke triggers configuration I/O.

Hooks can inspect, transform or reject content, not change source, destination, operation,
model-turn permission or dispatch/turn/thread identity. Protocol text changes preserve
RPC structure, IDs, methods and control/approval fields. Without hooks, native frames
retain their exact bytes. Structured observer state remains reducer-owned; rendered
text has a separate presentation stage. Hooks should select the operation/source they
intend to modify and remain short; they run synchronously.

Accepted upstream frames feed both the destination and projections. Fan-out does not
apply their protocol hooks again. Other rejected native traffic explicitly closes
the connection so an RPC cannot remain silently waiting forever.

| Outcome | Evidence |
|---|---|
| `completed` | Requested operation completed |
| `queued` | Newest-state dispatcher admitted an observer snapshot |
| `delivered` | Native transport accepted, observer admitted, or stdout flushed; pixels are not confirmed |
| `model_turn_started` | Exact control returned a turn/dispatch receipt |
| `rejected` | Invalid, absent, stale or unsupported request; no requested delivery |
| `failed` | Operation/delivery failed; the error remains visible |
| `indeterminate` | Acceptance unknown; retain dispatch identity and reconcile before retrying |

No automatic message/model resubmission occurs except the typed `serverOverloaded`
terminal-turn recovery described below. Snapshot retries synchronize idempotent
latest state, not a FIFO of every message. Outcome-observer failures cannot undo accepted
work. The record buffer contains metadata, not prompt bodies or another durable chat log.

## Entrypoints

| Entrypoint | Owner |
|---|---|
| Repository/installed shims and console script | Validate/exec → `rodex.main` → `cli.run` → application pipeline |
| `python -I -m rodex.daemon` | One private daemon endpoint → runtime manager → singular analytics coordinator |
| `python -m rodex.terminal_bridge` | Exact pane/TTY descriptor handoff → daemon runtime reservation |
| `python -m rodex.agent_observer` | Receiver/liveness → consumer/view → terminal presentation |
| `python -m rodex.environment_exec` | Prepared environment → process exec |
| `python -I -m rodex.terminal_exec` | Fresh session → controlling child PTY → unchanged native TUI argv/environment |
| `python -m rodex.tmux_sharing_coordinator` | Server identity → roster reconciliation |
| `python -m rodex.status_animation_admission` | Admitted animation, watchdog, watchdog gate |

## User and automation routes

| Intent | Authoritative route/effect |
|---|---|
| No arguments, `_create`, `_detach`, native interactive options/prompt | Application → managed lifecycle → daemon runtime/TUI → protocol-input pipeline |
| Existing name/alias/UUID, with or without `resume` | Same selector/open/resume/adoption pipeline |
| Unmatched explicit resume, delegated native syntax | `cli._exec_codex`; native replacement, outside managed interaction |
| Native typing and terminal replies | TERMINAL_INPUT → decoder/interceptor → native child PTY → TUI → protocol-input pipeline |
| Configured live matches | Every `live.reg_exp_intercept` match → verified native prefix → shared menu INTERACTIVE_INPUT → terminal DISPLAY_STATE |
| Command/option navigation | One `InputInterceptionMenu` owns filtering, wrapping selection, Enter/open and Escape/back; immutable view → same display pipeline |
| Option confirmation | Explicit selected command + configured option → SUBMITTED_COMMAND → its configured target/operation/payload; options without actions remain placeholders |
| Selected command without options | INPUT_CONFIGURATION_ERROR → main MESSAGE(false); report bad config, clear verified native prefix, release keyboard; no empty picker or command submission |
| Configured Enter match | Interception `on_enter.reg_exp_intercept` → SUBMITTED_COMMAND, including pasted input without live takeover; unmatched Enter stays native |
| Local release | INPUT_RELEASE → clear terminal DISPLAY_STATE; cancellation leaves native prefix; unsupported editing restores held suffix before key |
| Presentation selection | Configured `light`/`dark` action → SELECT_PRESENTATION_POLICY; never starts a turn or changes App Server delivery/logging |
| Local placeholder reply | Configured actionless command/option text → MESSAGE(false) → main display adapter |
| Initial prompts and native TUI protocol operations | Native TUI → protocol-input pipeline → App Server |
| Native terminal output | Readable child PTY → TERMINAL_OUTPUT → continuously updated native projection → selected native/semantic surface + inline compositor → bounded display queue → writable outer PTY |
| App Server primary output | Protocol-output pipeline → TUI unchanged, then typed presentation/context/observer/event projections; display filtering never rejects execution |
| Main `turn/completed` failure with `codexErrorInfo: serverOverloaded` | Typed recovery controller → rolling 30–960 second delay → main MESSAGE(true) `Continue...`; terminal-input outcome cancels pending dispatch |
| Primary TUI requests | Protocol-input pipeline → request correlation → App Server unchanged; correlated `thread/read` responses hydrate bounded typed presentation text |
| App Server control-client output | Protocol-output pipeline → its exact destination; never enters the primary presentation projection |
| `_start`, `_steer`, `_interrupt` | Exact selector lock → interaction operation → exact-control adapter → proxy |
| `_alias` | Serialized SQL/tmux rename → explicit start/steer announcement |
| Attach/update notice | Registered capability → bounded update producer → display-only interaction |
| `_inspect`, `_dispatch-status`, `_result`, exact `_wait` | Verified control reads/events, preserving JSON/dispatch identity |
| Human `_wait` | Verified live control → wait until idle |
| `_cat`, `_tail`, `_events` | Live read pipeline → scrollback/events → invoking terminal; event feedback refusal retained |
| `_mouse` | Exact mutation coordinator → session mouse option/readback |
| `_running`, `_context` | Registry/verified runtime discovery → invoking terminal |
| `_stats`, `_stats-status`, `_agents`, `_trace` | Statistics/trace read owners → invoking terminal |
| `_help`, lifecycle banners, command errors | Application/command formatting → invoking terminal |

## Observer, background and lifecycle routes

| Origin | Owner/effect |
|---|---|
| Committed registration | Independently activate observer and attempt analytics startup |
| Agent work starts | Exact spawn, successful follow-up or known-child active/turn-start event → reducer running-agent ledger → OPEN → pane adapter |
| Agent work finishes | Matching child turn/request completion or inactive status → reducer removes one agent → last agent closes observer via CLOSE; main chat remains intact |
| Reopened agent work | Current target/path/turn fact → observer snapshot → view tracking; no original spawn or prompt replay |
| Later agent activity/prose | Projection → reducer → DISPLAY_STATE → dispatcher → pane-bound control frame |
| Observer message | MESSAGE → readiness-bounded send → receiver admission → terminal presentation |
| Observer bootstrap | Initial projected event via OPEN → view → terminal presentation |
| Reopen | Registered blank launch context + current reducer snapshot → new view |
| Analytics trace publication | Committed SQL receipt → nonblocking observer wake → indexed trace/evidence reads → presentation |
| Startup/overflow SQL catch-up | Durable projection → view → same terminal presentation |
| Analytics initial/event/retry wake | Scheduler → authenticated reader/analyzer → registry transaction: checkpoint, lineage, trace, statistics, health |
| Primary disconnect | Reset all lifecycle participants; reducer clears work/identity and advances epoch; close observer; retire connection targets |
| Capacity-recovery timer | Runtime-owned cancellable deferred call; primary disconnect/runtime close cancels it while retaining the runtime's rolling overload count |
| Session creation | Managed lifecycle/runtime → server claim, daemon reservation, exact bridge/TTY admission |
| Registration/rename/rollback | Registry transition and runtime markers → acknowledged daemon supervisor wake; one namespaced rename owner |
| Attach | Registered capability → interactive tmux attach |
| Startup rollback/stop | Managed lifecycle/runtime → exact session kill |
| Ctrl-D | Owned root binding → detach current client only |
| Ctrl-C | Native originating-client admission → private guarded termination or shared detach |
| Resize, external SIGINT | tmux resize/layout hook → daemon resize wake → gateway → child terminal dimensions; daemon runtime → foreground process group |
| Natural exit, signals, keepalive failure | Daemon runtime closes its children/gateway/proxy/observer/status/event tap and paths; the shared coordinator retires its analytics state |

## Deliberate domain boundaries

| Surface | Authoritative owner |
|---|---|
| Static/transient status and safety claims | `TmuxStatusPipeline` / `TmuxStatusClaimCommands` |
| Shared arrival/departure animation | Roster coordinator → animation admission → renderer/watchdog |
| Tool count and context/compaction animation | Protocol/rollout/timer → `TmuxStatusOption` |
| Every tmux subprocess | `SyncTmuxExecutor` / `AsyncTmuxExecutor` |
| Transient catalog/startup App Server probes | Runtime's read-only initialize/thread-read/loaded-list adapters |
| SQL connection/publication | `rodex_sql` transactions and registry publication pipeline |
| Runtime logs, update cache, analyzer memory files | Blocking child-diagnostic relay/file owners, not chat; new startup diagnostics wake the runtime supervisor after persistence |
| Keyboard framing and native PTY writes | `TerminalInputDecoder` / `TerminalInputInterceptor` → `TerminalSessionGateway`; tmux retains its owned lifecycle keys |
| Terminal readiness and lifecycle | `TerminalSessionGateway` blocks on outer/child PTYs, a wake-only pipe and the exact child `pidfd`; explicit registration/stop control events, diagnostic output, presentation revisions, tmux resize hooks and incomplete-input deadlines wake the relay without an idle supervisor timer |
| Native composer presentation at takeover/submission | Exact primary-pane fenced snapshot; prefix and end cursor must agree, no background screen polling |
| Native editor state | Codex; Rodex observes a bounded candidate and verifies the native composer at handoff |
| Main terminal surface | `TerminalSurfaceRenderer` → gateway output queue; one native projection plus configured semantic views, no editor mutation or second writer |
| Presentation classification | `SessionPresentationPipeline`; bounded App Server method/kind/thread/turn/item/type/phase/status fields and item-text accumulation |

### Escape timing

A lone Escape must be distinguished from the start of an arrow or Alt-key sequence.
The inspected tmux 3.2a server reported `escape-time 500`; Rodex leaves that server
setting unchanged. `TerminalInputDecoder` adds a 35 ms lone-Escape wait and exposes its
exact deadline to the gateway's blocking descriptor wait. No periodic relay poll is
needed when no further keyboard or child-output bytes arrive. The combined waits can
still include tmux's configured escape time; complete
Up/Down frames are decoded immediately. This is input framing, not a menu transition
delay. The valid argument picker goes back one level; the command list releases local
input. Commands without options never enter a picker and need no Escape recovery.

## Enforced audit and evidence

`test_interaction_path_ownership.py` recursively scans every production package and
classifies module + enclosing function + effect primitive: terminal writes, socket
sends, process execution, injected effect sinks and SQL connections. New or removed
effect-bearing functions require review. It also enumerates subprocess entrypoints,
restricts pane/model mutation owners and checks read-only bootstrap probes. This is
not a proof against arbitrary Python reflection, nor a runtime monitoring system.

Behavioral tests cover hook ordering, intent preservation, missing/stale targets,
explicit model input, indeterminate receipts, both proxy directions/control clients,
bounded observer probes, real tmux operations, real observer startup/reopen/rendering,
and runtime exit. The installed-command gate isolates tmux, SQLite and Codex history;
it verifies startup/resume, real native slash menus, local takeover/menu/submission/release,
ordinary editing afterwards, and no model turn from display-only delivery. Isolated PTYs
test controlling-terminal identity, event-driven idle waiting and presentation wakes,
input/output hooks, final-output drain, resize, signals, spawn failure and terminal
restoration. Tests never attach to or stop an existing user session.

Current checks cover both menu levels, regex filtering, cyclic selection, configured
actions/headings/options, fixed footer, rapid Escape, rejected delivery, missing-option
errors and immediate keyboard release. Terminal transcripts verify semantic filtering,
streaming typed text, hidden native activity, configured future item types, menu operation
inside semantic mode, native restoration, modal-control fallback and frame coalescing
without truncating in-flight terminal tokens. Protocol tests verify exact request/response
identity, root-thread scoping, history hydration, delta/item correlation and disconnect reset.
The isolated installed-command gate starts Rodex, selects light through the real menu,
restores dark through submitted input, resumes/reattaches, and stops only its own tmux
fixtures without inspecting process state through `/proc/PID/stat`.

The observer lifecycle uses a separate reducer-owned running-agent ledger, not
the presentation event/tombstone count. Starts are idempotent per request; turn/request
identity prevents an older completion from clearing newer work. Known agent identities
survive idle periods for reopening and are cleared with the connection epoch. Current
work facts restore target/turn tracking and the current trace cursor in a fresh pane
without replaying old prompts or historical trace requests.
Only relevant lifecycle/activity events reconcile pane visibility; unrelated streaming
deltas do not query tmux. The focused observer/interaction checks include a real isolated
tmux close/reopen test, with no live-user sessions or Codex model launches. The release
gate is the current Ruff, full coverage/live-startup, source-distribution and wheel
workflow in the README.
