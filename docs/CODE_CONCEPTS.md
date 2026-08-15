# Rodex code concepts

Rodex should make durable session control feel as direct as the original Codex CLI.
Convenience belongs at the boundary; identity and state remain exact underneath it.

## Identity

- Rodex, Codex, tmux, users, and names are separate domains with explicit links.
- Never present, store, or pass one domain's identity as another domain's identity.
- A permanent generated name is the canonical operational identity for a session.
- A user-defined name is an optional lookup alias, never a replacement for the anchor.
- User-facing names resolve once to an integer session identity; later work uses that id.
- Names should reveal domain, purpose, and direction without requiring distant context.

## Behaviour

- Equivalent intent enters one authoritative domain pipeline regardless of CLI spelling.
- Common actions may use bare words; their `--` forms must behave identically.
- Command vocabulary is centralized and excluded from every name-allocation route.
- Stable names remain canonical in tmux, status, logs, and operational communication.
- Persisted runtime information is evidence of a link, not proof that a task is live.
- Live-state answers verify the real runtime and remain scoped to the current POSIX user.
- Transparent protocol mediation is preferred to terminal scraping for machine signals.

## Change

- A mutation is one transaction: either its complete meaning commits or nothing does.
- Lookup rows are selected by their complete natural key before an insert is attempted.
- Ownership, uniqueness, and availability are checked inside the mutating transaction.
- Optional replacement is refused by default and requires an explicit force signal.
- Failure cleanup targets only resources created by the failed operation.
- CLI code adapts arguments and output; domain libraries own rules and state transitions.
- Errors name the failed contract and the next valid action; commands fail on stderr.

## Evolution

- Linux and compatible POSIX systems define the platform boundary.
- PROTO work favours small coherent changes, real use, and deliberate breaking redesign.
- Do not preserve accidental compatibility or speculative machinery without evidence.
- Keep public contracts small, descriptive, and close to the data they govern.
- Tests should protect identity, transaction, ownership, and lifecycle boundaries quickly.
- Documentation states future-facing standards; source and tests carry implementation facts.
