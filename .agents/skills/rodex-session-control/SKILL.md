---
name: rodex-session-control
description: Safely identify, inspect, and control one exact live Rodex/Codex turn. Use when work must target a named Rodex session or when waiting, steering, reading a result, or interrupting requires an exact turn ID.
---

# Rodex Session Control

1. Run `rodex _context --json` when pane identity matters. Discover live sessions with `rodex _running`, then run `rodex _inspect SESSION --json` before mutation. Verify `data.thread.cwd` is the intended workspace.
2. Start only an inspected idle thread: `printf '%s' "$PROMPT" | rodex _start SESSION --stdin --json`.
3. Capture `codex.turn_id`. Use it for every follow-up:

- `printf '%s' "$PROMPT" | rodex _steer SESSION --turn TURN_ID --stdin --json`
- `rodex _wait SESSION --turn TURN_ID --timeout 30m --json`
- `rodex _result SESSION --turn TURN_ID --json`
- `rodex _interrupt SESSION --turn TURN_ID --json`

Prompts go through stdin. Never use `tmux send-keys` or infer identity from names alone. After a wait timeout, wait again with the same turn ID or call `_result`; the timeout does not interrupt it. On `dispatch_indeterminate`, run `_inspect`; never retry. If no attributable turn ID is available, report the uncertainty and stop. `runtime_upgrade_required` means exact control needs a runtime restarted by an authorized user. Never interrupt, stop, or rehome another worker without explicit authority. Live 0.147 evidence shows approval requests route to the subscribed attached TUI after a machine mutation client disconnects; an authorized user handles them there. User-input-request routing remains unverified.
