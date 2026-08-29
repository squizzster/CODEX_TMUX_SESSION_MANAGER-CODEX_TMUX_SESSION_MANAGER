# Rodex code concepts

Rodex makes durable session use feel like the Codex CLI while keeping every identity,
runtime, turn, and durable state transition exact. Convenience belongs at the process
boundary; domain policy has one canonical owner underneath it.

## Application boundary

- One declarative CLI contract classifies every invocation into one application route.
- Native Codex syntax passes through unchanged. Rodex underscore commands remain local.
- CLI handlers parse arguments and render human text or versioned machine envelopes;
  they do not reimplement identity, mutation, transport, or persistence policy.
- A responsibility has one entry point and a single downstream path. A helper must not
  leave an owner, partially perform its policy, and later rejoin it.
- Command vocabulary is centralized and excluded from every name-allocation route.

## Identity and observation

- Rodex registries, Rodex sessions, runtime incarnations, Codex threads, Codex turns,
  tmux endpoints, operating-system users, and display names are separate identity
  domains.
- A permanent generated name is an immutable storage anchor. A user-defined alias, when
  present, is the preferred outward name.
- Persisted runtime information is evidence of a link, not proof that the runtime is
  live. Live operations verify the tmux endpoint, control endpoint, durable runtime
  incarnation, Codex thread, and current user as required by their contract.
- Human-readable terminal observation comes from verified tmux plain text. Structured
  lifecycle observation comes from the App Server protocol. Neither is used as a
  substitute for authoritative exact-turn state.
- A terminal follower emits committed rows promptly, settles mutable visible rows, and
  never treats observation as proof that a turn completed.

## Exact-turn mutation

- `ExactTurnMutationCoordinator` is the sole owner of start, steer, interrupt, and alias
  mutation policy.
- The coordinator resolves a selector, acquires that session's transition lock, resolves
  the selector again, and keeps the lock through live discovery and mutation. A selector
  that changed while waiting fails closed.
- Start, steer, and interrupt resolve one durable runtime incarnation and revalidate the
  selector, tmux endpoint, control metadata, and runtime ID immediately before transport
  can send its first mutation frame. Transport independently verifies the expected Codex
  thread in the same bounded chain.
- `CodexControlClient` owns bounded App Server transport only. Its mutation methods are
  package-private and require the coordinator's revalidation fence; there is no
  unfenced prompt-sending API.
- The machine-command handler invokes only the coordinator's start, steer, and interrupt
  operations. The human session-command handler invokes only its `alias_transition`;
  neither handler calls mutation transport directly.
- Start accepts only an idle thread. Steer and interrupt require the caller's exact
  active turn ID and carry it as the App Server's expected-turn guard. A stale selector
  or runtime produces no mutation frame; a mismatched turn is rejected by the App Server.
- Alias assignment has one public operation, `alias_transition`. It plans from a
  consistent read, performs tmux work without holding a SQLite writer transaction, and
  finalizes with compare-and-swap checks. A failed durable finalize compensates a
  completed tmux rename.
- A successful live alias change sends one `RODEX_AUTO_INFO` through the same fenced
  start-or-steer policy. Notification failure does not undo an already committed name.

## Durable trace

- Authenticated rollout records are first normalized into immutable typed trace facts.
  The registry trace contract is the sole owner of complete validation, canonical
  UTC/text/identity normalization, typed-detail matching, duplicate detection, and
  detail hashing.
- Contract preparation happens before `BEGIN` and issues a sealed
  `PreparedAgentTracePublication`. The writer accepts only that exact contract-issued
  object; copied, replaced, or manually constructed prepared values are rejected before
  SQL.
- The trace writer accepts prepared values only inside an active Rodex transaction. It
  appends the deduplicated ledger, advances the publication head, and rolls all trace and
  request-provenance changes back together on failure.
- Thread membership resolution considers only distinct thread IDs present in the batch,
  uses bounded `VALUES` chunks, and occurs after membership writes in the same
  transaction. Unrelated historical memberships are not materialized.
- Agent-request provenance and FIFO target-turn reconciliation are writer-internal
  steps, not independently callable sequencing APIs.
- The trace reader owns snapshot, pagination, and follow reads. Body expansion is an
  explicit one-shot re-authentication path; follow mode remains bounded metadata-only.
- Trace SQL stores identity, coordinates, hashes, sizes, capture state, and typed facts.
  It does not duplicate plaintext message, command, tool, or output bodies.

## Transactions and failure

- Every registry mutation verifies the current schema generation inside its intended
  short transaction. Ordinary operations do not rerun bootstrap or integrity attestation.
- Lookup rows are selected by their complete natural key before insertion.
- Ownership, uniqueness, expected durable state, and incarnation checks occur inside
  the transaction that relies on them.
- Cross-system transitions avoid external work while holding SQLite writer locks and
  compensate only resources changed by the failed operation.
- Post-success access telemetry is best-effort. A telemetry warning cannot turn an
  already successful user or exact-turn mutation into a retryable failure.
- Errors identify the failed contract and the next valid action; human errors use
  stderr, and machine control uses versioned structured envelopes.

## Platform and security boundary

- Linux is the platform boundary. SQLite path security depends on inotify,
  `/proc/self/fd`, `O_NOFOLLOW`, and `flock`.
- Database access goes through the Rodex transaction boundary, which validates the
  securely opened database identity and enforces owner, mode, schema-generation, and
  transition-lock invariants.
- Public contracts stay small, descriptive, and close to the data they govern. Tests
  enforce the single-owner call graph, exact identity fences, transaction boundaries,
  privacy, and bounded resource behavior.
