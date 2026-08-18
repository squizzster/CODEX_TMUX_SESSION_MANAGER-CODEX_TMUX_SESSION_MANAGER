# Phase II evidence and remaining plan

Phase II remains evidence-gated. The 0.147 live experiment now proves that a short-lived
mutation client may disconnect after acceptance: lifecycle events and approval requests
continue on the subscribed primary connection. See
[the characterized evidence](APP_SERVER_0_147_LIVE_EVIDENCE.md). User-input requests now
have the same live routing evidence. Dispatch recovery uses caller-owned correlation and
read-only observation; it does not assume or implement server-side deduplication.

## Order

1. Preserve caller-directed relocation. A resumed runtime intentionally adopts the
   caller's current working directory: moving a user's home/project/worktree and then
   resuming from its new location carries that location forward. Rodex identity is
   durable, but it is not a permanent workspace pin. `_inspect` reports the effective
   App Server `cwd` before mutation.
2. Keep the live-turn characterizations replayable through opt-in integration tests.
   Fanout, approval ownership, user-input ownership, dispatch correlation, and initiator
   disconnect are observed. A real TUI reconnect replay remains useful; the proxy's
   primary handoff has a deterministic transport-level regression.
3. Keep dispatch policy at the controller boundary. `_start` and `_steer` accept or
   generate a `dispatch ID`, pass it as `clientUserMessageId`, and return it even when
   response loss makes acceptance indeterminate. `_dispatch-status` reports zero, one,
   or multiple matching `userMessage.clientId` observations and recommends a next
   command. `not_observed` is not rejection; Rodex never silently retries.
4. Add machine lifecycle and discovery: resumable `_sessions`, create-only `_spawn`,
   resume-only `_resume`, controlled restart/rehome, and workspace freshness fields.
5. Broaden compatibility and recovery: `_doctor`, generated-schema checks across each
   supported Codex version, durable pending-attention metadata, and runtime-death
   reconciliation. Keep transparent TUI use available when machine mutation fails
   closed.
6. Add multi-agent task/message orchestration only after one-session exact control,
   workspace ownership, approval routing, and retry behavior have real evidence.

## Continuing boundaries

- Transport remains private Unix-domain WebSockets; no TCP listener is planned.
- SQLite stores Rodex identity, provenance, coordination, and bounded outcome metadata,
  never a second conversation history.
- Names remain display/discovery aids. Mutation requires verified durable and live IDs.
- No destructive lifecycle action gains implied authority from an automation command.
