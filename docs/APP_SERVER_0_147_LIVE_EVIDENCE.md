# Codex App Server 0.147 live control evidence

Observed on 2026-08-18 with `codex-cli 0.147.0`, private Unix WebSockets, two initialized
clients, and the [official App Server protocol](https://developers.openai.com/codex/app-server).

- Client A created an ephemeral thread and therefore held its subscription.
- Client B read that thread, started a turn, received `inProgress`, and disconnected
  before A received `turn/started`.
- A then received the exact turn start, item lifecycle, final answer, and completed turn.
  B's disconnect did not abort or strand the turn.
- In a separate read-only case, an `on-request` shell approval routed only to A. A
  declined it; the command did not run, its item completed as `declined`, and the turn
  completed normally.
- `thread/read` did not subscribe B. Reconnecting B likewise received no turn or item
  lifecycle and no approval request.
- Ephemeral 0.147 threads reject `thread/read(includeTurns=true)`; the opt-in replay uses
  `includeTurns=false`. Rodex production result reads target persisted TUI threads.

The causal conclusion is narrow: the mutation connection may be short-lived while a
subscribed primary remains continuous. Rodex must therefore preserve and reassign the
proxy's primary event ownership across a TUI retry. Keeping the mutation connection open
would not make it a subscriber.

Replay the deterministic lifecycle portion explicitly (it uses one authenticated,
read-only model turn):

```bash
RODEX_RUN_LIVE_TURN_INTEGRATION=1 \
  uv run pytest tests/test_app_server_unix_integration.py \
  -k survives_initiator_disconnect -vv
```

The approval case remains recorded manual evidence because inducing elevation is
model-dependent. The replay refuses tool use with `approvalPolicy=never` and read-only
sandboxing; any unexpected server request is rejected and fails the assertion.
