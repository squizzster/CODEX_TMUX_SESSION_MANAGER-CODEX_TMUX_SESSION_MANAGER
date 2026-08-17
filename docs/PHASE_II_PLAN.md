# Phase II plan — not implemented

Phase II remains an evidence-gated plan. Phase I deliberately preserves the working
short-lived App Server control connection and does not claim approval routing or retry
deduplication that has not been observed.

## Order

1. Persist workspace identity before adding machine lifecycle: resolved workspace,
   Git worktree root/common directory, initial branch or detached state, initial HEAD,
   and Rodex-created base ref/commit. Resume from that workspace, never caller `cwd`.
2. Run an explicitly authorized live-turn experiment across TUI and second App Server
   clients. Characterize turn/item fanout, approval and user-input request ownership,
   initiator disconnect, and TUI reconnect. Change the proxy topology only if this
   produces a concrete failure or a simpler verified contract.
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
