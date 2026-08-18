---
name: rodex-session-control
description: Safely identify, inspect, and control one exact live Rodex/Codex turn. Use when work must target a named Rodex session or when waiting, steering, reading a result, or interrupting requires an exact turn ID.
---

# Rodex Session Control

1. Run `rodex _context --json` when pane identity matters. Discover live sessions with `rodex _running`, then run `rodex _inspect SESSION --json` before mutation. Verify `data.thread.cwd` is the intended effective workspace. Resuming from a new caller working directory is an intentional relocation, not an identity failure.
2. Allocate and retain an opaque dispatch ID before mutation. Start only an inspected idle thread: `printf '%s' "$PROMPT" | rodex _start SESSION --dispatch DISPATCH_ID --stdin --json`.
3. Capture `data.dispatch.id`, `codex.turn_id`, and `data.recommended_next`. Use the exact turn ID for every follow-up:

- `printf '%s' "$PROMPT" | rodex _steer SESSION --turn TURN_ID --dispatch NEW_DISPATCH_ID --stdin --json`
- `rodex _wait SESSION --turn TURN_ID --timeout 30m --json`
- `rodex _result SESSION --turn TURN_ID --json`
- `rodex _interrupt SESSION --turn TURN_ID --json`

Prompts go through stdin. Never use `tmux send-keys` or infer identity from names alone. When Rodex returns `data.recommended_next.command`, execute that structured argv only when it remains within the caller's authority. After a wait timeout, wait again with the same turn ID or call `_result`; the timeout does not interrupt it. On `dispatch_indeterminate`, follow the returned recommendation: start/steer recommends `_dispatch-status`, while interrupt recommends `_result` for its already-known turn. An `accepted` dispatch observation yields an exact wait/result recommendation; `not_observed` is not rejection and recommends another status query; `ambiguous` requires a controller decision. Rodex never retries the mutation itself. `runtime_upgrade_required` means exact control needs a runtime restarted by an authorized user. Never interrupt, stop, or relocate another worker without explicit authority. Live 0.147 evidence shows approval and user-input requests route to the subscribed attached TUI after a machine mutation client disconnects; an authorized user handles them there.
