# Version tracker

This file owns the concise human-readable change history. [pyproject.toml](pyproject.toml)
owns the active package version, while
[current-contract tests](tests/test_current_contracts.py) pin wire and storage
generations. Package milestones below are verified from repository history; this
repository currently has no Git release tags, so they do not imply a published release.

## 0.17a — 2026-10-14 — current package identifier

Build metadata and wheel filenames normalize this PEP 440 spelling to `0.17a0`.
Daemon protocol v5, SQLite catalog generation 22, and other wire/storage generations
are unchanged. Retained running sessions keep their original implementation.

### Modified

- Changed primary prompts again show their actual expansion in the ordinary Rodex
  pane, through a separate bounded, transient read-only display above the composer.
  Original input is followed by `=> translated =>` and the forwarded text in normal
  terminal colours, with no bright heading.
  Transformation still runs once at structured submission; original native draft
  and recall remain unchanged. Post-send display never adds a prompt or retries
  delivery. Connection fences, safe terminal text, native/light restoration, expiry
  and complete-frame coalescing keep display separate from submission authority.
- Resumed primary connections now bind model input from correlated native lifecycle
  replies instead of requiring `thread/started`. This fixes 70% context reminders
  being rejected locally before `turn/steer`. Typed RPC IDs, child/control isolation,
  pending-selection suspension, and disconnect fences preserve exact authority.
  Missing-binding refusals have a content-free reason and three bounded pre-admission
  retries; admitted or uncertain sends are never resent. A real native/local-model
  regression verifies one reminder reaches the same running resumed turn without
  interruption. Existing retained runtimes require explicit upgrade to use this fix.
- Prompt rules now run only on structured submissions (A06). Terminal drafts and
  initial argv are not transformed early; text-matching receipts are removed.
  Native recall retains original input while model/server input is transformed.
  Repeated, no-RPC, prefix, reconnect and multipart cases cannot reuse preparation;
  strict rejection and local-command composer verification remain intact.
  This intentionally changes draft/history display semantics, not native retry
  deduplication or dispatch guarantees.
- Command-policy shutdown shares one two-second join deadline across the accept thread
  and all request workers (A17 shutdown-budget portion). Admission stops during close;
  unresolved workers remain owned and are reported with content-free counts. Tests
  cover saturated stalled workers, abandoned clients, incomplete requests, and eventual
  release. This does not cancel blocked filesystem calls or establish daemon-wide
  capacity limits across runtime replacement.
- Canonical token-accounting response fingerprints now include normalized cumulative
  thread usage (A15 conflict-rejection portion). Both replay and resident append paths
  reject cumulative-only conflicts while identical older duplicates retain the latest
  total. Fixed-expectation, append-split, source-local identity, and real-SQL worker
  tests preserve published statistics, trace facts, and checkpoints on rejection.
  Previously accepted conflicts degrade on replay without automatic catalog repair;
  projection generation, broader ordering/reset rules, and tied-statistic semantics
  are unchanged.
- Same-incarnation reopen repairs only a verified incumbent's tmux name (A12 name
  recovery portion), with complete-registration SQL compare-and-swap and exact primary
  authority retained through pending confirmation. Normal display-name restoration
  follows; failed repair or presentation leaves the discovered incumbent running.
  Adoption retry immutability remains unchanged. Alias announcements advance only
  their committed expected name, preserving all runtime authority. Real-tmux tests
  cover pending/registered reopen, collisions and alias revalidation; injected faults
  cover acknowledgement loss and competing identities. New-candidate setup-retention
  policy and real process-death cuts remain separate work.
- Crash-receipt reconciliation carries one verified pidfd through group-scoped
  TERM/CONT/KILL and exit probes (A02), replacing numeric termination and the STOP
  workaround. Unsupported capabilities, surviving unverified groups and unconfirmed
  exits retain receipts and refuse daemon startup. Graceful and escalation waits are
  two seconds each; signal delivery alone no longer retires a receipt. Isolated
  subreaper tests cover leader reap, descendants, stopped groups and untouched sentinels;
  forced PID/PGID reuse and a broad kernel matrix remain untested.
- Analytics distinguishes committed publication from resident acceptance (A14 recovery
  portion). Partial analyzer/trace/cursor acceptance retries unchanged bytes within
  the existing cost budget instead of parking them as invalid source data. Publication
  errors and acceptance failures rebuild cursors from SQL's authenticated checkpoint,
  including when an acknowledgement was lost. Phase diagnostics remain content-free;
  capture-instability retry handling is still open.
- Primary event subscriptions require a verified upstream and end on source loss
  (A04). Publication and invalidation carry the source epoch; stale callbacks cannot
  populate or close a replacement stream. Exact waits report continuity loss without
  retrying model work. Mandatory publication precedes auxiliary projections, and
  input EOF invalidates subscribers even while an output projection is blocked.
- Auxiliary protocol participants contain exceptions independently and expose
  content-free failure counts (A05 first stage). Intentional prompt rejection and
  mandatory event observation remain authoritative. Context rendering cannot suppress
  lower-usage steering evidence; failed control observations revoke pending work.
  Bounded asynchronous dispatch, overflow/reset ordering and blocked-input isolation
  remain open.
- A retained-runtime upgrade blocked by a never-saved Codex thread now gives the
  exact `rodex NAME --force-old` command for reconnecting without an upgrade.
- First correctness tranche from the 2026-10-14 integrated architecture assessment:
  acquired process descriptors close on read/parse failures (A01); failed exact
  STOP retained an unreconciled receipt without numeric group signals (initial A02
  gate, superseded by the group-scoped pidfd correction above).
- Invalid mutation acknowledgements preserve dispatch reconciliation evidence;
  alias errors distinguish committed names from refresh failure or uncertain
  announcements, with an exact status-query command (A03).
- Exact proxy route admission (A07), type-exact protected JSON scalars and RPC
  response IDs (A08), and immutable per-reservation policy environments (A09).
- tmux environment source queues carry command boundaries separately from string
  data, including semicolon names/values (A10). Gateway, daemon, policy broker and
  terminal bridge readiness use poll without `select`'s descriptor-number ceiling
  (A11); this does not establish aggregate capacity limits.

## 0.16a — 2026-10-14

Build metadata and wheel filenames normalize this PEP 440 spelling to `0.16a0`.

### Added

- Managed runtimes provide an active-prefix `m` fallback that toggles session mouse
  mode and briefly confirms `Mouse: ON/OFF` when the key is unbound or still uses
  tmux's stock pane-mark action, while preserving any non-stock user binding during
  startup and reconciliation.
- [Native interaction findings](docs/INTERACTION_PIPELINE_REPORT.md), dated
  2026-10-11, preserve Codex 0.162.1 notice-placement evidence and its verification limits.
- Native managed-session command-policy hook with volatile tmux-only command status,
  a typed allow/deny/rewrite-command extension point, and a first capability rule
  denying standalone integer sleeps below 10 seconds with model-visible feedback.
  The matcher accepts surrounding horizontal whitespace and one terminal semicolon.
  An exact Rodex-only hook inventory gets temporary native key/hash trust;
  another enabled hook retains `_policy-setup`. Decision and inactive-reminder rows
  last five seconds, expose tmux prefix mode, and restore the normal bar by exact
  claim token; successful admission clears stale inactive state.
- Production `PreToolUse` customer-policy library for direct `find`/`rg` workflows,
  with boundary-aware multiline extraction, declarative installed TOML rules,
  deterministic static outcomes, bounded live metadata census, exact rejection
  feedback, and no command execution, history, or persistence.
- Exact main-turn context stewardship at a 70% structured-usage crossing, with one
  asynchronous fixed steer per armed crossing, compaction suppression, final
  exact-turn admission, and no automatic retry after uncertain delivery.
- A daemon now creates one random 256-bit incarnation capability at each start,
  publishes it privately, and requires it alongside the retained installation identity
  on every request and response.
- Agent traces now preserve Codex `developer` messages as a first-class role through
  normalization, contract validation, SQL storage, and trace readback.

### Modified

- `evaluate_command_policy` now checks `find`/`rg` before continuing to the existing
  standalone short-sleep rule. Search evaluation has a combined workflow/deadline
  budget, native denial reasons are bounded, and the helper/broker timeout is now
  five seconds while the native hook retains its three-second limit.
- Rodex now targets Python 3.11 or newer using equivalent pre-PEP-695 typing syntax.
  Launcher discovery includes PATH interpreters and leaves missing-package or module
  failures visible after selecting a compatible Python.
- The installed shim now resolves only existing, executable Rodex environments for the
  invoking user: explicit, checkout, active-virtualenv, uv-tool, and system-Python
  candidates. It performs no installation, synchronization, download, or recursive
  scan and reports an unsupported environment without modifying it.
- Saved-session resume no longer waits behind Codex 0.162.1's hidden hook-review
  prompt. Automatic admission verifies native trust after a provisional App Server
  restart instead of relying on the TUI bypass flag. Native start/resume/fork and
  same-name runtime replacement tests preserve UUIDs, permissions, and user hook state.
- Documentation refactor: complete README index, a change-boundary guide, consolidated
  interaction/hook contracts, dated native findings, and explicit separation of the
  launch shim from owner-provisioned checkout, uv-tool, virtualenv, or system
  environments. Missing-package, isolated-user-site, resume, notice, retry, and
  model-input gotchas are retained; application behavior is unchanged.
- The [session-control skill](.agents/skills/rodex-session-control/SKILL.md) now
  distinguishes model input from local notices, clarifies native command-policy
  status, and accounts for overload recovery starting a separate turn.
- Forward-contract checks characterize Codex 0.162.1's `partial_answer` phase
  without changing the 0.151.0 minimum: it is not a final result or light-view
  commentary, and trace metadata uses the existing `unknown` phase fallback.
- Ordinary startup recognizes an unchanged checkout and environment from lightweight
  metadata, returns a valid cached retained interpreter before taking the publication
  lock, and uses a new opaque key when it must publish a changed installation.
- Daemon protocol v5 carries the per-runtime policy endpoint; the terminal bridge
  and daemon bind clients to one exact daemon
  incarnation. Retained-runtime handoff continues to admit supported daemon-v3 and
  daemon-v4 installations, alongside daemon-v5.
- The current-only catalog is generation 22 (`rodex-v22.sqlite3`) and the agent-trace
  contract is v5. Statistics projection v10 counts each canonical response-usage
  record once while retaining cumulative snapshots as a legacy fallback. Generation
  21 remains untouched and is not implicitly migrated.

### Removed

- Routine startup no longer hashes Rodex source, installed dependency contents, or
  shipped configuration to identify itself.
- Recognized developer messages are no longer collapsed into the `unknown` role.
- The installed shim no longer recursively scans the checkout and virtual environment
  as an intrusion-detection policy.

## 0.14.0a2 — 2026-09-18 — historical package identifier

### Modified

- Server-overload recovery was bound to the correct live root session across stale
  runtime observations.

## 0.14.0a1 — 2026-09-17 — historical package identifier

### Added

- One implementation-scoped daemon replaced per-runtime Python hosts and coordinated
  runtime workers and analytics.

## 0.13.0a1 — 2026-09-17 — historical package identifier

### Added

- Runtime ownership was isolated with explicit tmux, process, peer, and persistence
  identity boundaries.

Earlier alpha milestones remain available in Git history; this tracker keeps only the
context needed to understand the current retained-runtime architecture.
