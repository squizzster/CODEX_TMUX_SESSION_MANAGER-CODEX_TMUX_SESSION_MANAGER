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
- A persisted turn started and was then steered with two caller-owned
  `clientUserMessageId` values. Both emitted `userMessage` items with the corresponding
  `clientId`, and both survived a direct `thread/read(includeTurns=true)` history read.
  This is the observed basis for `_dispatch-status`; it is correlation evidence, not a
  deduplication claim. The replay deletes its exact test-created thread afterward.
- In a plan-mode read-only case, B started the turn and disconnected. A alone received
  the exact `item/tool/requestUserInput`, answered one option, received
  `serverRequest/resolved`, and then received the exact completed turn and final answer.
  Both clients explicitly opted into 0.147's experimental API capability for this case.
- Ephemeral 0.147 threads reject `thread/read(includeTurns=true)`, so the correlation
  replay deliberately uses a persisted, cleanup-scoped thread matching Rodex's
  production history-read mechanism. The user-input replay remains ephemeral.

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

Replay user-input routing separately (also one authenticated, read-only model turn):

```bash
RODEX_RUN_LIVE_USER_INPUT_INTEGRATION=1 \
  uv run pytest tests/test_app_server_unix_integration.py \
  -k user_input_routes -vv
```

The approval case remains recorded manual evidence because inducing elevation is
model-dependent. The ordinary lifecycle replay uses only a harmless `sleep 5` command
with `approvalPolicy=never` and read-only sandboxing to hold the turn open for steer;
any unexpected server request is rejected and fails the assertion. The user-input
replay answers only the expected, exactly scoped request and rejects any other server
request.
