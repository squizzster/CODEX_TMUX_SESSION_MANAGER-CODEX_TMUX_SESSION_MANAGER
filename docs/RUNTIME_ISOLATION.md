# Runtime isolation

A selector, display name, socket, client count, pane ID, or Codex thread is an
address or observation, not sufficient authority to act on a runtime.

## Compatibility boundary

[Current-contract tests](../tests/test_current_contracts.py) pin release, catalog,
tmux, daemon, process-receipt, peer, observer, machine, trace, and statistics versions.
Do not replicate that manifest in callers. Live handshakes also require the exact
loaded installation key; [retained installations](../INSTALL.md#retained-installations-and-compatibility)
keep helpers on that implementation across checkout updates.

Earlier catalogs and unknown wire formats are not adopted or translated. A selected
registered runtime in a supported retained installation can undergo the explicit
[idle upgrade handoff](#retained-runtime-upgrade) below. Independently,
[`notify_legacy_runtime_resize`](../src/rodex/legacy_runtime_compat.py)
accepts the known pre-retention tmux-v4 coordinator, authenticates its v2 daemon
through a private process receipt and same-user socket, forwards resize, and pins
only its owned hook slots to a retained bridge. It cannot start, stop, adopt, access
an old catalog, or send model input. Unknown generations and foreign hooks remain fenced.

## Retained-runtime upgrade

`rodex NAME`, `rodex resume NAME`, and `_detach NAME` use the same selected-session
pipeline. When its coordinator belongs to another retained installation,
[`RetainedRuntimeUpgrade`](../src/rodex/runtime_upgrade.py) parses the known hook,
regenerates it for the exact server, and verifies its private installation manifest.
It never executes the stored shell text or overwrites old helpers.

The [handoff adapter](../src/rodex/retained_runtime_handoff.py) runs with the owning
interpreter and `-I`, importing that installation's existing control/daemon APIs.
Supported admission requires the shared catalog contract, tmux-v5,
process-receipt-v3, and daemon-v3, daemon-v4, or daemon-v5. The parent retains the session
transition lock throughout.
The adapter rechecks durable/full tmux identity and whole-runtime topology, inspects
the exact thread, verifies saved history and both live child receipts, then rechecks
idle immediately before stopping only that reservation. A busy thread reports on
stderr why upgrading would interrupt it, plus commands to retry when idle or
reconnect with `--force-old`; it is not interrupted or waited on.

For a recognized owned selector, `--force-old` skips the upgrade and attaches
through that same retained interpreter. The parent releases its transition lock
before interactive attachment; the adapter revalidates the durable registration,
full tmux capability, and coordinator, then calls the owning launcher's guarded
attach. It inherits terminal input/output and has no upgrade timeout. This path
does not stop, restart, or send model-control input, and does not replace helpers
or ownership markers. An ended runtime is refused rather than recreated. Unmatched
selectors and other Codex syntax keep the original arguments, including the flag.

The old daemon must report `terminal`, and the old tmux session must exit, before
the current launcher resumes the exact Codex UUID on a new daemon runtime/server.
The existing CAS registration path carries the predecessor incarnation, Rodex identity,
name and original thread cwd. No missing-history fallback can replace its Codex UUID.
Other old sessions and the daemon remain running; retained installations remain intact.
If replacement startup fails, the saved thread remains available for a later resume.

The old daemon has no atomic stop-if-idle operation. The transition lock serializes
managed mutations, and the final inspection catches intervening turn changes, but
native typing can still race that inspection and stop. This adapter does not claim
an atomic native-input exclusion. Tests cover
[handoff admission](../tests/test_runtime_upgrade.py) and
[two-session retained installation upgrades](../tests/test_installation.py).

## Capability and admission

[`TmuxRuntimeCapability` and `TmuxSessionCapability`](../src/rodex/tmux_session_capability.py)
are the authority types. Runtime authority binds the absolute socket, server
incarnation, immutable tmux `$session_id`, primary `%pane_id`, and runtime ID.
Registered authority adds Rodex session, registry, SQL-row, Codex, and registration
identities. Rodex's 16-hex IDs are integrity discriminators, not bearer credentials.

[`RodexRuntimeLauncher`](../src/rodex/runtime.py) mints authority from a coherent,
uniqueness-checked roster. Every effect rechecks its applicable tuple at the exact
target; primary actions also require the pane ID. tmux predicates belong in direct
`if-shell -F` conditions, with payload-only `display-message` in the selected branch.
Rendering a predicate as display output changes literal `$`/`%` semantics.

The shared daemon has two deliberately different identities. The opaque retained
installation key selects the compatible endpoint and code generation. At each daemon
start, [`RodexDaemonServer`](../src/rodex/daemon.py) generates a separate random
256-bit incarnation ID and atomically publishes it as a current-user-owned, mode-`0600`
regular file. [`RodexDaemonClient`](../src/rodex/daemon_client.py) binds to that value,
sends both identities on every operation, verifies both response fields, and rejects
a changed incarnation. This detects stale or replaced daemon state inside Rodex's
single-user trust boundary; it is not authentication against a hostile same-uid process.

The creation/admission sequence is:

1. Claim an entirely unmarked, empty tmux server dedicated to this runtime. Retain
   the creation nonce for cleanup; failure must not rediscover incumbent authority.
2. Reserve one runtime/operation in its implementation-scoped
   [`DaemonRuntimeManager`](../src/rodex/daemon.py), after verifying that daemon's
   current incarnation. Different implementations cannot reconcile each other's
   reservations or child receipts.
3. Transfer exactly one pane TTY descriptor from the bridge. Verify same-uid peer,
   PID, pane, TTY, nonce, and reservation before ownership transfers. The daemon
   runtime becomes the sole TTY owner; a manager timeout cannot revoke it.
4. Under the session transition lock, commit the expected-incarnation SQL transition,
   then confirm registration and UI identity before releasing the lock. New runtimes
   advertise `pending`; an exact durable/pending pair can finish interrupted confirmation.
   Unconfirmed runtimes expire rather than becoming implicitly registered.
5. Admit protocol traffic only after both ends verify the runtime/server/installation
   key and, for shared-daemon traffic, the daemon incarnation.
   [`RuntimePeerIdentity`](../src/rodex/runtime_peer.py) also pins native peers to the
   retained child process tree; a matching thread ID alone is insufficient.

### Primary conversation discovery

Before registration, startup verifies the native App Server version with a separate
read-only initialize exchange and reads the proxy's
[correlated, non-ephemeral primary acknowledgement](INTERACTION_PATHS.md#primary-conversation-identity)
through `rodex/primary-thread/read` on the authenticated `/rodex-interaction` route.
That query opens no upstream connection. It does not select from
`thread/loaded/list`: Codex can legitimately keep temporary helpers or child threads
loaded beside the main conversation. The query shares the steering identity owner,
does not submit native work, and returns no identity while main selection is pending
or its connection has been lost. Startup keeps its 30-second allowance; an exact
resume must still acknowledge the requested UUID. Regression coverage includes
[delayed startup](../tests/test_rodex_runtime.py) and
[native start/resume/fork with a loaded helper](../tests/test_primary_thread_discovery_native.py).

### Registration and name repair

Validated registration immediately retires the registration deadline. Analytics
activation remains an independent pending operation, retried at most once per
second on a monotonic deadline while the terminal gateway continues servicing I/O.
Successful activation retires that retry deadline. Only an actually unregistered
exact resume may enter active-writer startup recovery; unavailable analytics does
not make an accepted registration provisional again.

Locks span identity-sensitive transitions, not terminal attachment. Attachment uses
immutable `$session_id`, so a concurrent alias cannot redirect it. Discovery retains
the coherently observed name and exact primary capability, including while registration
is pending. Shared live verification can repair an externally renamed incumbent under
the transition lock: it rechecks live authority around a name-only SQL compare-and-swap
against the complete old registration. Socket, runtime, Codex root, and runtime-start
provenance do not change. Pending confirmation uses the retained exact pane rather than
the stale human name. Ambiguous or changed authority is refused, not adopted.

Reopening then restores the intended display name through the normal guarded rename.
Failures during repair, confirmation, rename, or UI refresh leave this discovered
incumbent running; another open can reconcile the reached phase. Name collisions must
be resolved before that rename can finish. Native changes between checks may require
another retry; this is not atomic exclusion of external tmux mutations. It does not
change cleanup policy for newly launched candidates or decide post-commit setup retention.
Real-tmux and injected-fault tests cover repair phases; real process-death cuts
remain unverified.

Alias finalization uses compare-and-swap and compensates its own tmux rename on failure.
Its subsequent announcement advances only the committed name in its expected control
snapshot, retaining exact authority. The alias command's initial name-only liveness
check is unchanged: reopen an externally renamed runtime before aliasing it.
See [SQL incarnation rules](SQL_SCHEMA.md#identity-and-lineage).

## Liveness and replacement

`RodexRuntimeLauncher.session_exists` retains the expected incarnation through the query.

| Observation | Meaning |
| --- | --- |
| Successful inventory lacks the runtime; canonical socket absent; owned socket refuses connection | Proven unreachable |
| Timeout, unavailable executor, malformed/ambiguous inventory, invalid file type, permissions | Error, not negative liveness |

Unreachable is not destruction authority or proof that native children exited.
Replacement still requires the expected durable incarnation and Codex writer admission.
Concurrent opens serialize and converge on one runtime. Only the unregistered exact
resume path retries a departing active writer within its bounded handoff window;
unrelated failures and completed registrations cannot enter that path.

## Resource lifetime

- [`ExclusiveUnixEndpoint`](../src/rodex/runtime_endpoint.py) retains endpoint locks
  and inodes: losers cannot unlink incumbents, and stale cleanup cannot remove replacements.
  A child-created App Server rendezvous alias is allowed only after alias, private
  directory, physical socket, and listener ownership verification. Cleanup removes
  the retained alias, not the child's target.
- Daemon request decoding has a five-second absolute deadline and closes every
  rejected/truncated `SCM_RIGHTS` acquisition. An admitted bridge has the runtime's
  lifetime, not the framing timeout. Shutdown wakes incomplete decoders.
- Stop acknowledges `stopping` or `terminal`; service-thread completion alone cannot
  prove terminal. Failed process cleanup retains the runtime context, receipt and
  live capability, and repeated stop requests retry finalization.
  Cancelled reservations cannot start. Completed contexts compact to at most 1,024
  small identity/outcome receipts without caller environments; evicted operations
  expire and cannot replay as new work.
- [`RuntimeProcessReceipts`](../src/rodex/process_receipts.py) bind native process
  groups to operation/runtime, PID, Linux start time, and uid. Parent-death guards
  protect daemon-owned children. `RuntimeProcessLifecycle` owns startup, replacement,
  termination and receipt retirement. Its `OwnedProcessGroup` authenticates and
  retains the pidfd before releasing the workload execution gate. This covers
  App Server, provisional policy replacement, TUI retries and transient catalog probes.
  Receipt publication cannot overwrite an unresolved predecessor. Retirement checks
  the complete identity; failed directory synchronization restores the exact receipt
  or keeps a pending restoration obligation if storage remains unavailable.
  Reconciliation validates `/proc` identity against
  the same retained, live pidfd used for all subsequent group signals. Nonblocking
  pidfd readiness works for non-children too; a second numeric PID lookup is not
  substituted for that descriptor.
  Reacquired capabilities remain retained across failed reconciliation attempts,
  even when the leader exits before retry. Gateway presentation closure does not
  close lifecycle authority, and a repeated old-gateway finalizer cannot stop a
  replacement group. The gateway owns a separate pidfd duplicate for exit readiness.
  TERM, CONT (for already-stopped members), KILL and exit probes use
  `PIDFD_SIGNAL_PROCESS_GROUP`. The [Linux implementation](https://github.com/torvalds/linux/commit/e1fb1dc08e73466830612bcf2f9f72180965c9ba.patch)
  addresses the retained kernel PID object, including after leader reap, without
  resolving a new group number. No preparatory STOP or numeric termination fallback
  remains. Unsupported or denied group-scoped signals leave the receipt unreconciled.
  Cleanup waits two seconds after TERM/CONT, then up to two more after KILL,
  polling the retained `Popen` child during confirmation to reap its exit. It never
  reaps unrelated children. Native acceptance fixtures use an isolated subreaper
  for orphaned grandchildren; the production host does not become a subreaper.
  Only confirmed group absence retires the receipt: successful signal delivery is
  not proof of exit, and unreaped zombies or uninterruptible members can prevent
  confirmation. If the leader cannot be verified/acquired, a numeric signal-zero
  probe can prove absence only; it cannot authorize termination of an existing group.
  An unresolved receipt refuses startup of that implementation-scoped daemon.
  The [isolated group tests](../tests/test_process_receipts.py) cover leader loss,
  reaping, surviving children, stopped groups, extinction and untouched sentinels.
  [Owned-lifecycle acceptance](../tests/test_process_lifecycle.py) additionally covers
  gated partial startup, signalling and receipt failures, retry refusal and repeated
  finalizers. Catalog failures retain their owner on the launcher and block its next
  probe until cleanup succeeds. Their separate `catalog-process` receipt namespace
  prevents daemon startup from terminating another live launcher's probe. Recovery
  after the catalog launcher itself exits is not automatic; its retained receipt
  remains available for explicit reconciliation, subject to the same leader/absence
  rules. This does not grant a new daemon authority over live catalog launchers.
  They do not force actual PID/PGID number reuse or establish support for every kernel
  backport. Descendants that leave the owned process group remain outside this contract.
- Runtime cwd is an explicit reservation field passed to App Server and TUI.
  Setting `PWD` is not `chdir`; native directory options must not be applied twice.

## tmux and observer lifecycle

Whole-runtime destruction additionally proves the sole server session and ownership
of every affected pane. Foreign panes block it. [CLI lifecycle keys](CLI.md#open-detach-and-resume)
execute in native originating-client context; `exit-unattached` and
`destroy-unattached` are disabled so the final detach does not destroy a runtime.
Private keyboard `Ctrl-C` first offers a four-second task-bar countdown. Its native
timers require the same warning sequence and client key table, so cancelling and
rearming cannot reuse an old deadline. `q` and expiry
recheck the full capability and require both the attachment count and roster to
identify only the originating client. A connected-but-exiting client cannot lend
its old timer to a replacement. External signals do not use this input path.

Global client/layout hooks are wake-only because a detached session may no longer
exist when the hook runs. The coordinator inventories the roster and uses each
session's capability. It touches only owned hook indices, never local session hooks.
Existing foreign `C-c`/`C-d` bindings fail initialization. tmux has no atomic
bind-if-absent primitive: coordinate same-uid configuration during initialization;
readback cannot rule out a racing overwrite at the bind instant.

Resize hooks and the retained foreground bridge's `SIGWINCH` wake the same gateway.
It reads guarded logical pane dimensions because tmux's kernel PTY size can lag its
hook. Generic layout changes and pane swaps must both reach this path.

Observer creation publishes an operation receipt before splitting; a lost split
reply cannot authorize a second pane. Splits request `-l 33%`. A pending creation
can retire after rejection only when its exact dispatch acknowledgement and a
complete native pre-creation diagnostic prove that split failed. Unknown errors,
timeouts and malformed replies retain candidate recovery. Retirement compares the
dispatched operation and primary capability, then verifies the receipt by readback;
an adopted or competing operation cannot inherit the original failure proof.
Registration publishes its binding last.
Uncertain retirement retains the original target and generation tombstone; new work
waits for that outcome. Frames carry runtime/server identity because separate tmux
servers can both contain `%0`. [Observer state](INTERACTION_PATHS.md#observer-state)
owns visibility, not pane mechanics.

Evidence: [runtime isolation](../tests/test_runtime_isolation.py),
[keyboard exit/detach](../tests/test_tmux_shared_ctrl_c.py),
[daemon lifetimes](../tests/test_daemon_resource_lifetimes.py),
[runtime peers](../tests/test_runtime_peer.py),
[registration/adoption](../tests/test_runtime_registration_adoption.py),
[transition locks](../tests/test_session_transition_lock.py), and
[observer safety](../tests/test_observer_runtime_safety.py).
