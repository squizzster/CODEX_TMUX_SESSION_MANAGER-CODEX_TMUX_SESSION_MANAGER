# Rodex code concepts

Rodex should make durable session control feel as direct as the original Codex CLI.
Convenience belongs at the boundary; identity and state remain exact underneath it.

## Identity

- Rodex, Codex, tmux, users, and names are separate domains with explicit links.
- Never present, store, or pass one domain's identity as another domain's identity.
- A permanent generated name is the immutable storage anchor for a session.
- A user-defined name, when present, is its preferred outward identity everywhere.
- User-facing names resolve once to an integer session identity; later work uses that id.
- Names should reveal domain, purpose, and direction without requiring distant context.

## Behaviour

- Every invocation is classified once, enters one authoritative application pipeline,
  and executes exactly one domain mechanism regardless of CLI spelling.
- Preparation branches state real control flow; handler-specific dependencies remain
  explicit rather than being implied by inert pipeline metadata.
- Common actions may use bare words; their `--` forms must behave identically.
- Command vocabulary is centralized and excluded from every name-allocation route.
- The effective name remains consistent across tmux, status, and user communication.
- Persisted runtime information is evidence of a link, not proof that a task is live.
- Live-state answers verify the real runtime and remain scoped to the current POSIX user.
- Attach and resume enforce that same owner boundary before touching a live runtime.
- External runtime identities use exact matching rather than prefix or glob semantics.
- Transparent protocol mediation is preferred to terminal scraping for machine signals.

## Change

- A mutation is one transaction: either its complete meaning commits or nothing does.
- Lookup rows are selected by their complete natural key before an insert is attempted.
- Ownership, uniqueness, and availability are checked inside the mutating transaction.
- Optional replacement is refused by default and requires an explicit force signal.
- Failure cleanup targets only resources created by the failed operation.
- Cross-system transitions compensate external changes when durable commit fails.
- CLI code adapts arguments and output; domain libraries own rules and state transitions.
- Resolved identities and exact command specifications travel forward as typed context;
  later stages do not rediscover them from display text or argv.
- Errors name the failed contract and the next valid action; commands fail on stderr.

## Evolution

- Linux and compatible POSIX systems define the platform boundary.
- ALPHA work keeps changes coherent while broadening boundary and installation checks.
- Do not preserve accidental compatibility or speculative machinery without evidence.
- Keep public contracts small, descriptive, and close to the data they govern.
- Tests should protect identity, transaction, ownership, and lifecycle boundaries quickly.
- Documentation states future-facing standards; source and tests carry implementation facts.
