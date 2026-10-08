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

See [CLI workflows](../../../docs/CLI.md) for selector/alias pitfalls and
[runtime isolation](../../../docs/RUNTIME_ISOLATION.md) for identity fences.
