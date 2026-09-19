# Shared-runtime root causes and verification

Date: 2026-09-19. Project: CODEX_TMUX_SESSION_MANAGER / Rodex 0.14.0a2,
internal Linux ALPHA. Implementation baseline: `cf345bc`, not the supplied reviews'
`489c074` (0.14.0a1). The intervening overload-recovery/implementation-fingerprint
correction was preserved. Work is on `fix/shared-daemon-resource-lifetimes`.

## Scope and conclusion

The two supplied assessments reuse finding numbers for different defects. This report
uses boundary names instead. Their test counts are not added to this work's evidence.
Source tracing, failing intended-behavior tests, real descriptors/PTYs/SQLite/WebSockets,
and one independent read-only reviewer informed the corrections below. The reviewer did
not implement changes; its suggestions were checked against code and targeted tests.

The direct causes were mismatched resource lifetimes, missing per-runtime process input,
and cancellation/completion checks at an earlier boundary than the actual operation.
SQL attestation and eager analyzer imports were separate correctness boundaries. The
existing router, exact identity fences, one shared supervisor, one analytics worker and
transactional publication remain intact. A migration from short-lived to shared-process
ownership is a plausible explanation, not verified historical provenance.

## Traced causes and implemented corrections

| Boundary | Root cause and correction | Regression evidence |
| --- | --- | --- |
| Outer terminal descriptor | Manager timeout and service finalizer owned the same integer. Successful thread start now transfers exclusive ownership; only unstarted descriptors remain manager-owned. Failed construction/start reclaims them; timeout never revokes a worker's ownership. | `test_daemon_resource_lifetimes.py`: delayed wrapper, cancellation during admission, bind-before-stop, start failure, completion and replay. |
| Gateway wake descriptor | Callback copied a descriptor before concurrent cleanup could close/reuse it. Wake write and release now share a short reentrant lifetime lock. No join or blocking network wait occurs under it. | `test_terminal_gateway.py`: barrier holds a write while close attempts the same lock, proving the descriptor stays open until the write finishes. |
| Child working directory | tmux cwd and `PWD` never changed the shared daemon's OS cwd. A required absolute workspace now crosses the strict service contract and becomes `cwd` for App Server and every TUI attempt. Native argv is unchanged. | Captured App Server/TUI spawn options; real PTY relative-file reads from A/B; authenticated live A/B, native directory-option and resume checks. |
| Cancellation and completion | Bind/start ignored cancellation, while only started workers completed waiters/analytics. Context-lock admission now checks before and after peer validation; stopped reservations cannot start. Unstarted stop signals readiness/completion and retires analytics once. | Stop-before-bind, during admission, between bind/start, startup checkpoint, repeated stop and failed-thread tests. |
| Stop acknowledgement | Five-second client RPC raced a ten-second server join and a false success response. Stop now promptly acknowledges `stopping` or `terminal`; only actual worker completion establishes terminal state. Shutdown joins all workers under one ten-second total deadline. | State assertions with a deliberately delayed service; real isolated daemon teardown. |
| Completed-context retention | Completed contexts retained environments, arguments, events and threads indefinitely. They now compact to at most 1,024 identity/outcome/hash receipts. An ordered monotonic operation prefix and expired floor reject replay after eviction. | 1,200 completed reservations; bounded records, no config/environment retention, exact replay and expired-operation rejection. |
| Daemon request receiver | Locally accumulated SCM_RIGHTS descriptors escaped handler cleanup on decode errors; framing had no time bound. The receiver retains ownership until successful return, closes every failed acquisition, checks ancillary truncation and enforces a five-second absolute deadline across fragments. Shutdown wakes tracked pending sockets. | Real socketpairs: invalid UTF-8/JSON/object, partial/trailing/oversized frames, ancillary truncation, timeout and valid ownership transfer. |
| Multiple catalogs | Opening B closed a singleton descriptor still borrowed by A, causing false `database_moved` and rollback. Owners now retain each authenticated catalog across all borrowers, with at most one idle retained owner. Identity rejection and SQLite-before-descriptor close order remain. | A→B→A overlap, four concurrent catalogs, committed values and rollback; existing SQL, sparse-WAL, identity and maintenance suites. |
| Recovery cancellation | Timer firing cleared pending authority before hooks/session-lock/transport waits. A trusted runtime-local generation now remains cancellable until atomic final dispatch admission. A rejected token remains `rejected` through nested exact control; admission preserves accepted/indeterminate semantics. | Input/close/disconnect during a blocked hook; real coordinator/control barriers at session-lock and transport setup; post-admission cancellation and indeterminate receipt. |
| Idle event subscribers | Handler blocked on an empty queue after the peer closed. A 250 ms bounded queue wait checks the transport's public close state, without adding a watcher thread or blocking publishers. | Eight real Unix WebSocket connect/ready/disconnect cycles with no intervening publication; existing overflow/shutdown coverage. |
| Analytics retirement | Retiring entries were removed ahead of accepted events/retries. Retirement now retains events, requests final reconciliation on the existing worker, and records complete/inactive/incomplete outcomes. Only producer quiescence can establish completion, and quiescence during an old poll requires a new final generation. | Debounce-window completion, late append, retries/exhaustion, unavailable SQL, blocked poll, global close with live producers, quiescence during poll and cross-runtime serialization. |
| Catalog failure isolation | Failure-health publication could itself raise `database_moved` and terminate the one worker for all catalogs. That boundary now contains the failure, preserves retry/disable state, and logs a bounded runtime diagnostic. | Factory/poll moved-storage failures plus failed health publication do not stop a second catalog. |
| SQL attestation | Whole-string uppercasing/whitespace folding and global `IF NOT EXISTS` removal equated distinct literals. Token normalization now preserves quoted bytes, escapes, literal whitespace, numeric/operator boundaries and only removes the CREATE-prefix clause. No repair or schema migration occurs. | Public integrity audit rejects altered literal case/whitespace/clause text; quoted-token tests and existing catalog corpus pass. |
| Optional analyzer loading | Imports pulled private analyzer implementation into help and native delegation before the guarded worker boundary. Lightweight contracts and source configuration now remain independent; concrete loading occurs in the guarded worker factory. Package metadata pins the already locked commit. | Fresh-interpreter blocked-analyzer import permits help/native routing; worker-start failure remains fail-open; real full/incremental parity suites retained; wheel installation resolves the exact pin. |

## Explicit contracts and limitations

- Workspace is the invocation base, not an eagerly interpreted native `--cd` value.
  Relative options must not be applied twice. Native trust prompts remain native: the
  live fixture initially waited at that prompt, then was corrected to trust only its
  newly created empty directories in its isolated Codex home. No application trust bypass
  was introduced. An untrusted explicit `--cd` can therefore still wait before Rodex's
  registered attach; pre-registration onboarding/display is not redesigned here.
- Implementation fingerprints already fence loaded daemon/runtime code. The new required
  workspace field does not silently default for an old daemon. Stop old runtimes and their
  daemon before using changed code; no concurrent replacement root owner is introduced.
- `await_ready` describes live availability, not historical startup. Stop is cancellation
  admission, not confirmation that cleanup has finished. A started worker owns its
  resources until its finalizer, even after the caller's wait budget expires.
- Completion receipts have a count-based retry horizon. Exact retained operations return
  a terminal result; evicted identities expire. An older delayed reservation below the
  expiry floor must allocate a new operation identity; it is never silently restarted.
  Linux monotonic time avoids wall-clock rollback extending this rejection to new work.
- Orderly analytics retirement allows five seconds of cooperative effort and a 0.5-second
  settling interval. A shared-worker backlog consumes that budget. Daemon close separately
  waits up to five seconds; it cannot preempt arbitrary SQL/analyzer execution and never
  starts another worker. Until producer quiescence, shutdown retains arriving events;
  after quiescence it seals admission and requires a final generation. An unquiesced or
  unfinished runtime is incomplete, not complete. Health records the diagnostic when
  possible; daemon warnings and bounded in-memory receipts cover unavailable storage.
- This is best-effort projection reconciliation, not a durable repair queue or a promise
  to recover final statistics after daemon death. Codex remains the authoritative body
  owner; later activation requests reconciliation. `_stats` remains read-only.
- Fork closes idle SQL owners, not active parent borrowers. A fork while a transaction is
  active permits only immediate child exec or `_exit`; acquiring SQL in that child fails,
  and unwinding/using inherited SQLite contexts is unsupported. Existing sparse-write WAL
  retention and storage-replacement rejection are preserved, not bypassed.
- Large runtime/proxy extraction and a new upstream public analyzer API remain structural
  follow-ups, not demonstrated root-cause fixes. Only the dependency-light contracts needed
  for fail-open loading were extracted. The full-replay oracle remains independent.
- Detached unregistered-UUID adoption and concurrent native/control turn admission were
  unresolved product/native contracts in the supplied reports. This change does not alter
  them or claim that the reports established defects there.

## Verification record

Final gates:

- `uv run pytest --require-live-startup --cov --cov-report=term-missing -ra`:
  **1,923 passed, 3 skipped**, 181.56 seconds; **84.75% branch-inclusive coverage**
  against the required 70% gate. Both authenticated startup tests passed, including direct
  `/proc` cwd checks for App Server and TUI in A/B, relative/absolute `--cd`, reattachment,
  resume, adoption, real writer-conflict retry, and isolated daemon cleanup.
- Skips: two explicitly opt-in model-backed turn/user-input integrations; one exact native
  CLI fixture for 0.151.0, while installed Codex is 0.155.1. The run emitted one existing
  Python warning about the multithreaded test harness using `forkpty`.
- `uv run ruff format --check .`, `uv run ruff check .`, `git diff --check`, and
  `uv lock --check --offline`: passed.
- `uv build`: source archive and wheel passed. A clean Python 3.12.13 / SQLite 3.53.1
  wheel installation and installed `_help` passed. The analyzer resolved the pinned
  `13d23b296222e40c18cfc3b45d02c2adef45cce2` revision. Initial offline installation lacked
  a cached setproctitle wheel; ordinary installation fetched it successfully. Project
  dependency versions were not upgraded.

Intermediate failing runs were used to correct fixtures and expose missing interleavings;
they are not counted as successful release gates. Runtime data, SQLite fixtures, native histories and logs stayed
isolated from existing sessions. Live tests copy authentication into an isolated Codex
home, create harmless sessions, exercise local input/display and submit no model prompts.

The clean-install check used the built wheel in a disposable environment; it resolved the
exact analyzer commit and ran `_help`. Its temporary environment was removed before the
installed-launcher gate because that launcher's source-tree attestation correctly rejects
additional interpreter symlinks and writable package-manager lock files.

No production daemon turnover, production catalog migration or production session mutation
was performed. Synthetic race barriers and isolated live checks establish the exercised
contracts, not production incidence or exhaustive crash/power-loss guarantees.
