# Interaction contract and production-path inventory

Rodex 0.8.0a1, ALPHA. SQL generation 19 and shared tmux protocol v2 are unchanged.
There is no old interaction endpoint or fallback adapter. Existing processes retain
the code they already loaded; this change does not restart existing sessions.

## Authoritative contract

`SessionInteractionPipeline` owns target resolution, validation, ordered content hooks,
post-hook checks, delivery and bounded content-free outcome records. The host shares one
instance between terminal gateway, interceptors, proxy and observer. Exact-turn commands use the same contract inside
their existing transition lock. The observer renderer uses it in its own process.

```python
pipeline.send_message(target="main", text="Visible information", start_model_turn=False)
pipeline.send_message(target="agent-observer", text="Visible information", start_model_turn=False)
pipeline.send_message(target="main", text="Please investigate", start_model_turn=True)
```

`publish_session_interaction(socket_path, InteractionRequest(...))` submits the same
operations over the private `/rodex-interaction` endpoint. It cannot submit raw protocol
frames, raw terminal bytes, interceptor events, observer snapshots, launch commands or hooks. `publish_tui_notice` is an explicit
display-only convenience function using this transport, not a second delivery path.

| Target | Display | Model input | Pane operations |
|---|---|---|---|
| `main` | Native Codex warning/scrollback | Registered, currently displayed thread; idle-only start | Locate, focus, resize |
| `agent-observer` | Observer text | Rejected: multi-agent view has no single thread binding | Open/reuse, locate, focus, resize, close |
| Registered model target | No independent display | Exact coordinator start, steer or interrupt | None |
| Per-connection protocol target | Input/output frames | Native RPC intent preserved | None |
| `terminal` | Native output bytes | Native keyboard bytes; no implicit model intent | None |
| `input-interceptor:<name>` | Interactive draft, submitted command, release | Local handler; placeholder is display-only | None |

Future agent-chat targets need an explicit thread and adapter; labels never imply one.
Primary open/close are session lifecycle operations, not pane-control operations.
Model input uses normal conversation echo, without printing a duplicate prompt.
Starting an active thread fails; steering requires its own operation and exact turn ID.
Alias announcements deliberately start when idle or steer the observed active turn.

Missing targets reject by default. Explicit `open_if_missing` can reopen a registered
presentation with known launch context, but cannot invent an agent. A target disappearing
mid-operation is rejected. Pane mutations carry the frozen pane ID into tmux. Model
dispatch rechecks selected connection, runtime and thread after lock/transport waits.
Observer messages wait boundedly for socket readiness, send once and require exact-pane
admission. Reopening uses a blank view plus current reducer state, not the first spawn's
cached event. The snapshot budget reserves transport-address overhead.

## Processing and outcomes

Hooks can inspect, transform or reject content, not change source, destination, operation,
model-turn permission or dispatch/turn/thread identity. Protocol text changes preserve
RPC structure, IDs, methods and control/approval fields. Without hooks, native frames
retain their exact bytes. Structured observer state remains reducer-owned; rendered
text has a separate presentation stage. Hooks should select the operation/source they
intend to modify and remain short; they run synchronously.

Accepted upstream frames feed both the destination and projections. Fan-out does not
apply their protocol hooks again. Rejected native traffic explicitly closes the
connection rather than leaving an RPC silently waiting forever.

| Outcome | Evidence |
|---|---|
| `completed` | Requested operation completed |
| `queued` | Newest-state dispatcher admitted an observer snapshot |
| `delivered` | Native transport accepted, observer admitted, or stdout flushed; pixels are not confirmed |
| `model_turn_started` | Exact control returned a turn/dispatch receipt |
| `rejected` | Invalid, absent, stale or unsupported request; no requested delivery |
| `failed` | Operation/delivery failed; the error remains visible |
| `indeterminate` | Acceptance unknown; retain dispatch identity and reconcile before retrying |

No automatic message/model resubmission occurs. Snapshot retries synchronize idempotent
latest state, not a FIFO of every message. Outcome-observer failures cannot undo accepted
work. The record buffer contains metadata, not prompt bodies or another durable chat log.

## Entrypoints

| Entrypoint | Owner |
|---|---|
| Repository/installed shims and console script | Validate/exec → `rodex.main` → `cli.run` → application pipeline |
| `python -m rodex.session_host` | Host contract → `run_session_host` |
| `python -m rodex.analytics_worker` | Worker contract → analytics scheduler |
| `python -m rodex.agent_observer` | Receiver/liveness → consumer/view → terminal presentation |
| `python -m rodex.environment_exec` | Prepared environment → process exec |
| `python -I -m rodex.terminal_exec` | Fresh session → controlling child PTY → unchanged native TUI argv/environment |
| `python -m rodex.tmux_sharing_coordinator` | Server identity → roster reconciliation |
| `python -m rodex.tmux_shared_ctrl_c` | Capability → private/shared exit policy |
| `python -m rodex.status_animation_admission` | Admitted animation, watchdog, watchdog gate |

## User and automation routes

| Intent | Authoritative route/effect |
|---|---|
| No arguments, `_create`, `_detach`, native interactive options/prompt | Application → managed lifecycle → host/TUI → protocol-input pipeline |
| Existing name/alias/UUID, with or without `resume` | Same selector/open/resume/adoption pipeline |
| Unmatched explicit resume, delegated native syntax | `cli._exec_codex`; native replacement, outside managed interaction |
| Native typing and terminal replies | TERMINAL_INPUT → decoder/interceptor → native child PTY → TUI → protocol-input pipeline |
| Configured match | One `match_pattern` → verified native prefix → INTERACTIVE_INPUT target; subsequent Enter → SUBMITTED_COMMAND, no second matcher |
| Local release | INPUT_RELEASE → restore own status claim; cancellation leaves native prefix; unsupported editing restores held suffix before key |
| Local placeholder/menu | MESSAGE(false) → main display adapter; status draft → existing status-claim pipeline |
| Initial prompts and native TUI protocol operations | Native TUI → protocol-input pipeline → App Server |
| Native terminal output | TERMINAL_OUTPUT → bounded display queue → outer tmux PTY; unchanged without content hooks |
| App Server primary/control-client output | Protocol-output pipeline → destination; same accepted frame → projections |
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
| Exact agent spawn | Projection → reducer → OPEN → pane adapter → tmux split/registration/input-disable/focus |
| Later agent activity/prose | Projection → reducer → DISPLAY_STATE → dispatcher → pane-bound control frame |
| Observer message | MESSAGE → readiness-bounded send → receiver admission → terminal presentation |
| Observer bootstrap | Initial projected event via OPEN → view → terminal presentation |
| Reopen | Registered blank launch context + current reducer snapshot → new view |
| Analytics trace publication | Committed SQL receipt → nonblocking observer wake → indexed trace/evidence reads → presentation |
| Startup/overflow SQL catch-up | Durable projection → view → same terminal presentation |
| Analytics initial/event/retry wake | Scheduler → authenticated reader/analyzer → registry transaction: checkpoint, lineage, trace, statistics, health |
| Primary disconnect | Reset all lifecycle participants; reducer advances epoch; retire connection targets |
| Session creation | Managed lifecycle/runtime → server claim, inert session, prepared environment, exact host respawn |
| Registration/rename/rollback | Registry transition and runtime markers; one namespaced rename owner |
| Attach | Registered capability → interactive tmux attach |
| Startup rollback/stop | Managed lifecycle/runtime → exact session kill |
| Ctrl-D | Owned root binding → detach current client only |
| Ctrl-C | Private/shared confirmation policy → exact session termination |
| Resize, external SIGINT | Host → gateway → child terminal dimensions/foreground process group |
| Natural exit, signals, keepalive failure | Host closes owned children/gateway/proxy/observer/analytics/status/event tap and paths; gateway restores terminal attributes/FD flags |

## Deliberate domain boundaries

| Surface | Authoritative owner |
|---|---|
| Static/transient status and safety claims | `TmuxStatusPipeline` / `TmuxStatusClaimCommands` |
| Shared arrival/departure animation | Roster coordinator → animation admission → renderer/watchdog |
| Tool count and context/compaction animation | Protocol/rollout/timer → `TmuxStatusOption` |
| Every tmux subprocess | `SyncTmuxExecutor` / `AsyncTmuxExecutor` |
| Transient catalog/startup App Server probes | Runtime's read-only initialize/thread-read/loaded-list adapters |
| SQL connection/publication | `rodex_sql` transactions and registry publication pipeline |
| Runtime logs, update cache, analyzer memory files | File/diagnostic owners, not chat |
| Keyboard framing and native PTY writes | `TerminalInputDecoder` / `TerminalInputInterceptor` → `TerminalSessionGateway`; tmux retains its owned lifecycle keys |
| Native composer presentation at takeover/submission | Exact primary-pane fenced snapshot; prefix and end cursor must agree, no background screen polling |
| Escape rendering and general native editor state | Codex/tmux; Rodex forwards output and does not mirror the complete editor |

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
test controlling-terminal identity, input/output hooks, final-output drain, resize, signals,
spawn failure and terminal restoration. Tests never attach to or stop an existing user session.
