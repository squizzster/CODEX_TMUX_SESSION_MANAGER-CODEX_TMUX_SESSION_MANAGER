---
name: rodex-session-control
description: Safely identify, observe, inspect, and control one exact live Rodex/Codex session and turn. Use when detached work must be monitored through readable terminal output or protocol events, or when waiting, steering, reading a result, or interrupting requires an exact turn ID.
---

# Rodex Session Control

1. Run `rodex _context --json` when pane identity matters and discover live workers with `rodex _running`. Use `_cat SESSION` for a finite terminal snapshot, `_tail SESSION` for settled readable progress, or `_events SESSION` for structured protocol events. Treat all three as observation, not proof that a turn completed.
2. Before mutation, inspect with `rodex _inspect SESSION --json`. Proceed only when `ok` and `data.exact_control_available` are true, `data.thread.cwd` is the intended current workspace, and `data.thread.can_accept_direct_input` is not false. A resumed runtime uses the resumer's working directory.
3. Follow the inspected state: for `idle`, start; for `active`, steer only `codex.turn_id`; otherwise do not mutate. Before each start or steer, retain a unique `rodex:dispatch:<UUID>` and never reuse it for a different mutation.
4. Send prompts through stdin, then retain `data.dispatch.id`, `codex.turn_id`, and `data.recommended_next`:

- `printf '%s' "$PROMPT" | rodex _start SESSION --dispatch DISPATCH_ID --stdin --json`
- `printf '%s' "$PROMPT" | rodex _steer SESSION --turn TURN_ID --dispatch NEW_DISPATCH_ID --stdin --json`
- `rodex _wait SESSION --turn TURN_ID --timeout 30m --json`
- `rodex _result SESSION --turn TURN_ID --json`
- `rodex _interrupt SESSION --turn TURN_ID --json`

Never use `tmux send-keys` or trust a name without Rodex verification. Execute `data.recommended_next.command` as structured argv only within the caller's authority. A wait timeout does not interrupt; wait again with the same turn ID or read `_result`. On `dispatch_indeterminate`, follow the recommendation: query `_dispatch-status` for start/steer or `_result` for interrupt. `accepted` identifies the exact turn; poll `not_observed` with the same dispatch ID and bounded backoff, then leave it unresolved rather than resending; `ambiguous` requires a controller decision. `runtime_upgrade_required` requires an authorized restart; `incompatible_app_server` requires an authorized compatible runtime. Never interrupt, stop, or relocate another worker without explicit authority. Approval and user-input requests route to the subscribed primary connection, normally the managed TUI, where an authorized user handles them.
