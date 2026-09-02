# Codex App Server 0.151.0 control evidence

Rodex exact control is characterized against Codex App Server 0.151.0 and requires that
version or a newer stable three-part release. Versions below 0.151.0, prereleases, and
unrecognized versions are rejected. The initialize response is checked before every
inspection, discovery, or mutation.

The checked-in protocol fixture
[`tests/fixtures/codex_app_server_0_151_contract.json`](../tests/fixtures/codex_app_server_0_151_contract.json)
was generated from the installed `codex-cli 0.151.0` schema on 2026-09-02. The contract
tests verify its required thread fields and the App Server minimum-version gate. The
protocol basis is the
[official App Server protocol](https://developers.openai.com/codex/app-server).

The current lifecycle boundary asserts:

- A subscribed primary connection owns exact turn, item, approval, and user-input
  delivery.
- A short-lived mutation connection may disconnect after acceptance while the subscribed
  primary continues receiving the lifecycle.
- `thread/read` does not make a second client a subscriber.
- Caller-owned `clientUserMessageId` values appear as `userMessage.clientId` in exact
  thread history and provide correlation evidence for `_dispatch-status`; they are not a
  server-side deduplication guarantee.
- A transient App Server may read a persisted, non-ephemeral standalone CLI thread with
  `thread/read(includeTurns=false)`. The exact missing-thread error remains the read-only
  gate before Rodex treats a standalone UUID as a normal prompt.

The deterministic Unix-socket tests exercise initialize, thread reads, exact mutations,
subscriber routing, and teardown without accepting alternate message or connection
shapes. Two authenticated model-backed replays are opt-in:

```bash
RODEX_RUN_LIVE_TURN_INTEGRATION=1 \
  uv run pytest tests/test_app_server_unix_integration.py \
  -k survives_initiator_disconnect -vv

RODEX_RUN_LIVE_USER_INPUT_INTEGRATION=1 \
  uv run pytest tests/test_app_server_unix_integration.py \
  -k user_input_routes -vv
```

Both replays clean up the exact resources they create. The ordinary lifecycle case uses
a harmless `sleep 5` with `approvalPolicy=never` and read-only sandboxing; every
unexpected server request fails the assertion. The user-input replay answers only its
expected, exactly scoped request.
