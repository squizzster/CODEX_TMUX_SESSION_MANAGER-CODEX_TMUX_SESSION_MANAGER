---
name: rodex-session-control
description: Safely identify, observe, inspect, and control one exact live Rodex/Codex session and turn. Use when detached work must be monitored through readable terminal output or protocol events, or when waiting, steering, reading a result, or interrupting requires an exact turn ID.
---

# Rodex Session Control

Use `rodex _help` for syntax. Work only within the caller's authorized session/task;
inspection does not authorize starting, steering, interrupting, or stopping work.

## Identify and inspect

1. Use `rodex _context --json` when pane identity matters and `rodex _running` for
   live workers. `_cat SESSION`, `_tail SESSION`, and `_events SESSION` are observation,
   not proof that a turn completed.
2. Before mutation, run `rodex _inspect SESSION --json`. Require `ok: true`, a
   non-null `runtime.runtime_id`, and `schema_version` matching
   [`MACHINE_ENVELOPE_SCHEMA_VERSION`](../../../src/rodex/machine_commands.py).
   Check the intended workspace in `data.thread.cwd`. Resuming an ended runtime uses
   the resumer's cwd; upgrading a retained live runtime preserves its existing cwd.
   [CLI workflows](../../../docs/CLI.md#open-detach-and-resume) define both paths.
   Inspection rejects missing durable identity or an App Server below
   [`CODEX_APP_SERVER.minimum_version`](../../../src/rodex/app_server_contract.py).
3. For start/steer, require `data.thread.can_accept_direct_input` not false.
   Follow [`_STARTABLE_THREAD_STATUSES`](../../../src/rodex/control.py): `idle` and
   `systemError` permit start; `active` permits steering only its exact `codex.turn_id`.
   Other states do not authorize these mutations. Retain a unique
   `rodex:dispatch:<UUID>` before each start/steer; never reuse it for different input.

## Exact mutation and observation

Send prompt text through stdin. Retain `data.dispatch.id`, `codex.turn_id`, and
`data.recommended_next` from the response.

```bash
printf '%s' "$PROMPT" | rodex _start SESSION --dispatch DISPATCH_ID --stdin --json
printf '%s' "$PROMPT" | rodex _steer SESSION --turn TURN_ID --dispatch NEW_DISPATCH_ID --stdin --json
rodex _wait SESSION --turn TURN_ID --timeout 30m --json
rodex _result SESSION --turn TURN_ID --json
rodex _interrupt SESSION --turn TURN_ID --json
```

Never use `tmux send-keys` or a name without Rodex verification. Execute recommended
commands as structured argv only within existing authority; they are not shell text
or automatic permission. A wait timeout does not interrupt: wait again with the same
turn ID or read `_result`.

## Model input and local display

`_start` and `_steer` submit real model input through the exact-control pipeline;
[prompt rules](../../../docs/PROMPT_SUBMISSION_FLOW.md) apply. An effective live
`_alias` change also attempts a `RODEX_AUTO_INFO` start/steer. Choose these routes
only when that model interaction is intended.

For an authorized display-only notice, use the existing runtime's
`SessionInteractionPipeline.send_message(target="main", text=..., start_model_turn=False)`.
An external caller uses `publish_session_interaction` or `publish_tui_notice` with
the verified proxy endpoint and `RuntimePeerIdentity`; there is no `_notice` CLI.
The existing route sends a downstream native `warning`, outside model context.
`delivered` means transport accepted, not confirmed rendering or durable history.
On the tested Codex 0.162.1 idle screen, both `warning` and `deprecationNotice`
required F2; neither provided inline chat text. Do not promise inline placement
or implement a notice by submitting a model prompt.

`/rodex light` and `/rodex dark` select presentation only; `dusk` is a placeholder.
Agent shell-command interception uses the separate native `PreToolUse(Bash)`
broker. Its capability-test policy denies exact integer `sleep N` commands below
10 seconds with model-visible feedback and allows other commands. An allow status is
an interception decision, not execution/completion evidence. Fresh managed startup
automatically bypasses native hook trust only when the exact Rodex hook is the sole
enabled hook; another enabled hook requires `_policy-setup`. Read the
[interaction pipeline report](../../../docs/INTERACTION_PIPELINE_REPORT.md) when
working with notices, interception, configuration, or recovery; it identifies the
existing owners, native UI evidence, and changes needed for inline notices.

## Uncertain outcomes

- On `dispatch_indeterminate`, reconcile through `_dispatch-status` for start/steer,
  or `_result` for interrupt, following the response's structured recommendation.
- `accepted` identifies the exact turn. Poll `not_observed` with the same dispatch ID
  and bounded backoff; leave it unresolved rather than resending. `ambiguous` needs a
  controller decision.
- `runtime_identity_missing` requires an authorized restart;
  `app_server_version_mismatch` requires an authorized runtime meeting the minimum.
  Neither grants permission to stop/relocate another worker.
- Approval and user-input requests go to the subscribed primary connection, normally
  the managed TUI, where an authorized user handles them.
- After a terminal `serverOverloaded` failure, Rodex can start a new `Continue...`
  turn automatically. Check exact turn/dispatch evidence before adding a manual
  retry. A wait or result for the failed turn does not follow that new recovery turn.

See [CLI workflows](../../../docs/CLI.md) for selector/alias pitfalls and
[runtime isolation](../../../docs/RUNTIME_ISOLATION.md) for identity fences.
