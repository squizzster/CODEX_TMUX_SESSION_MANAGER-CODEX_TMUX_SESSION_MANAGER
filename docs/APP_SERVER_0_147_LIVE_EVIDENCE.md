# Codex App Server live control evidence

Rodex's characterized minimum App Server compatibility floor is 0.147.0, not a maximum
or exact runtime pin. Later recognized versions are accepted; earlier or unrecognized
versions are rejected before exact control. The original characterization was recorded
on 2026-08-18 with `codex-cli 0.147.0`. The two authenticated regression replays below
were run again successfully on 2026-08-29 with installed `codex-cli 0.150.1`.

The evidence uses private Unix WebSockets, two initialized clients, and the
[official App Server protocol](https://developers.openai.com/codex/app-server).

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
- In a read-only case, B started the turn and disconnected. A alone received
  the exact `item/tool/requestUserInput`, answered one option, received
  `serverRequest/resolved`, and then received the exact completed turn and final answer.
  Both clients explicitly opted into the experimental API capability for this case.
- The original 0.147 evidence showed that ephemeral threads reject
  `thread/read(includeTurns=true)`. The correlation replay therefore deliberately uses
  a persisted, cleanup-scoped thread matching Rodex's production history-read
  mechanism. The user-input replay remains ephemeral.
- A separate transient App Server successfully read an exact persisted, non-ephemeral
  standalone CLI thread with `thread/read(includeTurns=false)` while reporting it
  `notLoaded`. A guaranteed-absent canonical UUID returned JSON-RPC code `-32600` with
  `thread not loaded: <UUID>`. No turn or TUI was started. Rodex uses these two observed
  outcomes as the read-only gate before adopting a standalone Codex UUID.

The causal conclusion is narrow and remains current on 0.150.1: the mutation connection
may be short-lived while a subscribed primary remains continuous. Rodex therefore
preserves and reassigns the proxy's primary event ownership across a TUI retry. Keeping
the mutation connection open would not make it a subscriber.

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
request. Both automated replays clean up the exact resources they create.
