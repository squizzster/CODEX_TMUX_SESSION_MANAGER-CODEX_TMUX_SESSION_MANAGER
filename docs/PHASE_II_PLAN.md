# Phase II evidence and remaining plan

Phase II remains evidence-gated. The 0.147 live experiment now proves that a short-lived
mutation client may disconnect after acceptance: lifecycle events and approval requests
continue on the subscribed primary connection. See
[the characterized evidence](APP_SERVER_0_147_LIVE_EVIDENCE.md). Retry deduplication and
durable workspace identity remain unproven and unimplemented.

## Order

1. Persist durable workspace identity and make resume use it: resolved workspace, Git
   worktree root/common directory, initial branch or detached state, initial HEAD, and
   Rodex-created base ref/commit. `_inspect` now exposes App Server's live `cwd`, but
   resume still uses caller `cwd` and therefore remains an explicit fidelity boundary.
2. Keep the live-turn characterization replayable through the opt-in integration test.
   Fanout, approval ownership, and initiator disconnect are observed. A real TUI
   reconnect replay remains useful; the proxy's primary handoff has a deterministic
   transport-level regression.
3. Decide retry semantics from observed behavior. Treat `clientUserMessageId` as
   correlation only unless deduplication is demonstrated. If callers need recovery,
   add a durable request ledger with payload digest, accepted turn, and an explicit
   indeterminate state; do not store prompts or responses.
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
