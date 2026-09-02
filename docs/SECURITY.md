# Security model

Rodex is a local, single-Linux-user tool. Its trust boundary is the operating-system
user account: processes already running as the same uid can inspect that user's tmux,
Codex, and SQLite state and are not treated as hostile tenants. Rodex exposes no network
listener; control endpoints are Unix sockets below a private runtime root.

## Enforced boundaries

- A live attach or control action requires the durable SQL row and matching advertised
  Rodex session, registry, Codex thread, and `registered` state. Exact machine control
  additionally requires the current persisted/live 64-bit runtime ID and characterized
  App Server version. Missing, duplicated, or conflicting identity fails closed.
- New runtimes begin `pending`, become usable after the SQL identity commits, and exit
  if confirmation never arrives. An exact committed/pending pair is recoverable.
- Runtime roots are real, current-user-owned directories at mode `0700`, below either a
  private parent or root-owned sticky storage. Sockets and logs are mode `0600`.
- Codex/App Server, proxy, event, and control traffic uses Unix-domain WebSockets only;
  Rodex opens no TCP listener.
- The mode-`0600` proxy socket accepts a private Rodex update-notice endpoint. It sends
  the validated nonempty notice only to the current primary TUI as a native warning; it
  opens no upstream App Server connection and excludes the notice from protocol
  subscribers, SQLite, Codex thread content, and model turns.
- Managed Codex startup update prompts are disabled. Rodex's replacement check runs only
  read-only, bounded version commands, caches the npm result for 24 hours, fails open,
  and never installs or invokes an update. A nonblocking current-user regular-file lock
  elects one cross-process refresh owner; contenders retain the latest valid cache without
  waiting. Cache freshness requires a wall-clock age from zero through 24 hours; a
  future-dated timestamp is stale rather than extending the cache lifetime.
- Rodex uses stdlib `sqlite3` behind one canonical transaction owner. The database and
  sibling transition lock are current-user-owned regular files at mode `0600` below a
  real current-user-owned private directory. Linux `O_NOFOLLOW`/`O_CLOEXEC` opens retain
  the parent and lock descriptors through each transaction and retain one validated
  database descriptor for the process-local WAL lifetime. SQLite connects through
  `/proc/self/fd/<validated-database-fd>`.
- Only an explicit first-use bootstrap transaction may create the parent, transition
  lock, or database. Ordinary readers and writers require an existing database and lock
  and never create missing filesystem state. The first secure transaction records the
  parent, transition-lock, and database `(device, inode)` identities in process memory;
  bootstrap does not recreate a path that this process previously admitted and later finds
  missing.
- Rodex revalidates the retained and pathname identities, ownership, type, mode, symlink
  state, and SQLite-reported main path before connect, after connect, before `BEGIN`, and
  before `COMMIT`, and on connection/SQLite errors. Every ordinary transaction and
  integrity audit holds a shared `flock` on the retained sibling transition lock. The
  offline diagnostic maintenance entry uses its exclusive form. Lock and WAL-transition
  waits have ten-second monotonic deadlines with sleeping bounded backoff. Writers use WAL,
  `BEGIN IMMEDIATE`, foreign keys, a ten-second busy timeout, and `synchronous=NORMAL`;
  readers use a read-only/query-only deferred transaction and see a normal committed-WAL
  snapshot. One threadless, fork-safe process-local idle SQLite connection retains at most
  one validated WAL generation between sparse writes. It reuses the owner's validated
  main-file descriptor, owns no transaction or cooperative lock, and uses bounded
  checkpoint/growth settings. Identity switch and clean exit close SQLite before releasing
  that descriptor. Before a genuine fork, the parent closes the complete owner so the child
  inherits no live SQLite state.
- Database location enforcement is synchronous. Rodex has no filesystem watcher, worker,
  subscription, callback, polling loop, or recurring SQL. A missing or different identity
  is rejected at the next transaction boundary with restart guidance. A move-away-and-back
  completed entirely between transactions is not observable. The explicit integrity audit
  uses the same existing-only shared-lock boundary, includes committed WAL, performs no DDL,
  and rejects unexpected views as well as tables, indexes, and triggers. Live storage
  relocation and implicit repair are unsupported. Direct same-uid SQLite access that
  ignores the cooperative lock is outside the supported contract.
- Named runtime transitions use current-user-owned regular advisory-lock files at mode
  `0600` with no-follow opens, keyed by immutable Rodex session ID. Creation holds that
  lock from durable row publication through tmux identity/UI setup and registration
  confirmation. Existing-session transitions hold it through identity checking and
  endpoint replacement, then release it before terminal attachment. The verified runtime
  incarnation resolves to tmux's immutable `$session_id` for that final attach.
- Analytics reads only current-user-owned regular rollout files inside the configured
  sessions root, using no-follow and nonblocking opens before authenticating the Codex
  thread ID and stable complete-record prefix. Startup-only lineage discovery is bounded
  to the root UUIDv7 three-day window and reads only candidate metadata lines.
- The live context follower accepts only an absolute, exact-thread rollout filename
  beneath that configured sessions root. It reads a bounded tail and bounded appended
  lines for `token_count` records only, retaining no rollout bodies. Idle checks inspect
  metadata before bounded fingerprints, back off to a two-second ceiling, and wake early
  on existing exact-thread protocol activity.
- All Rodex sessions share the versioned `tmux-shared-v1.sock`; that socket is transport,
  not authority. Server-scope protocol and random incarnation markers identify a
  current-protocol server. Creation may claim only a completely unmarked server with no
  live session. A protocol mismatch or unmarked nonempty server is rejected without
  changing its sessions, global options, hooks, or keys.
- The owning host's authority is `(absolute socket, server incarnation, immutable
  $session_id, primary %pane_id, runtime incarnation)`. External registered authority
  adds exact Rodex session, registry, SQL-row, Codex, and `registered` identities. The
  launcher mints it after a coherent snapshot and uniqueness-checked roster; async actors
  carry it. Every terminal action repeats its applicable fields at the exact target;
  primary actions also require the immutable pane ID. Name, socket, runtime, process
  context, or hook event is insufficient.
- Under one stable per-user XDG/runtime context, Rodex uses one private canonical
  database and one shared tmux server. Database-enforced display-name uniqueness covers
  every session recorded in that database, and the complete live tmux name is the
  user-facing display name. A different `XDG_STATE_HOME` is a separate database/name
  boundary; a different `RODEX_RUNTIME_DIR` is a separate shared-server boundary. No
  internet-wide uniqueness service exists. The database ID remains in the registered
  capability to reject stale or replaced storage; it is not part of the name.
- Server-global indexed client hooks are wake-only. They carry no source session or
  mutation target; the sharing coordinator re-inventories all registered sessions and
  submits a changed count only through that session's full capability. Rodex owns and
  verifies only its dedicated hook indices and options behind a server-incarnation fence,
  and never removes local session hooks. Root `C-c` and `C-d` are installed exactly or
  initialization fails closed if a non-Rodex binding owns either key. `C-d` uses tmux's
  synchronous current-client `detach-client` context without a reusable client-name
  target. Rodex sets `exit-unattached off` and exact-session `destroy-unattached off` so
  detach cannot implicitly destroy the runtime. Hook shell text is quoted and tmux
  format text is escaped. Capability comparisons are evaluated only as direct
  `if-shell -F` conditions, where literal operands keep `$session_id` and `%pane_id`
  sigils as comparison data. Capability-fenced reads select a payload-only
  `display-message` branch after that condition succeeds.
- These are synchronous operation-boundary checks. Rodex is not an IDS and adds no
  filesystem/tmux surveillance, inotify watcher, or real-time monitor. An unavoidable
  same-uid external race can install a key binding between Rodex's last absence check and
  bind because tmux has no conditional bind-if-absent primitive; Rodex can overwrite that
  racing change. Readback detects a later competing change, not absence at the bind
  instant, so same-uid tmux configuration must be coordinated during initialization.
- `_cat`, `_tail`, and `_events` resolve the same owned, registered live identity before
  reading; terminal reads target its immutable primary pane. `_agents`, `_trace`, and
  `_stats` resolve the owned durable identity and need
  no live runtime; explicit trace body reads re-authenticate the recorded rollout prefix.
  Terminal following emits only tmux's plain text and creates no persistent conversation
  copy.
- The input-disabled agent observer admits plaintext from a completed `agentMessage`
  authored by a tracked child, the current App Server's explicit collaboration `prompt`,
  and the latest completed root `userMessage` when the same exact turn performs that
  collaboration. Prompt text is tied to the exact `collabAgentToolCall` identity; root
  user text is separately labelled as provenance, never as the collaboration payload.
  Live text travels after process startup through length-framed messages on a
  runtime-specific mode-`0600` Unix stream socket and is not copied into process
  arguments or a second SQLite body. SQL records canonical tool/activity identity,
  encrypted-body metadata, and turn-request provenance through authenticated trace
  references. When Codex does not expose plaintext, Rodex reports it unavailable rather
  than inferring or recovering it. The view verifies the root and sender identities,
  strips terminal controls, and excludes user messages from other roots or turns,
  system/developer messages, hidden reasoning, commands, arbitrary tool arguments, and
  output payloads.
- The install shim executes only the project's preinstalled `.venv/bin/rodex` boundary.
  Before execution it scans project source, the complete virtual environment (including
  site packages), and generated bytecode once, rejecting untrusted ownership,
  group/world-writable content, and non-environment symlinks. Only `.git` and
  nonexecuting pytest/Ruff caches are pruned. The entrypoint's absolute interpreter is
  resolved and separately checked as a root- or current-user-owned, non-writable regular
  file. The shim never syncs or rewrites the environment. A system command must use an
  immutable root-owned installation.
- Shared tmux global environment state is not trusted as caller state. New-session startup
  gates a disposable pane by its runtime capability, transports byte-escaped environment
  values only over tmux stdin, installs global-name tombstones, and starts the real host
  only after that installation succeeds. Host and observer exec boundaries remove names
  outside the caller/tmux contract. General environment payload does not enter process
  arguments; pane working directories remain explicit tmux control arguments.
  The shared server remains a same-UID boundary: protection against malicious concurrent
  mutation of dynamic-loader state would require a separately trusted static launcher.

Rodex does not auto-adopt or auto-delete an unregistered or unverifiable tmux session.
`rodex _running` reports it for explicit diagnosis. This is deliberate: an orphan is
less harmful than attaching to or destroying the wrong runtime.

The 16-hex Rodex session ID is an integrity discriminator, not a credential.
Authorization comes from the current-uid filesystem boundary plus the exact durable and
live identity tuple. Do not expose the ID later as a bearer-authentication token.
The 16-hex runtime ID has the same role: incarnation fencing, not authentication. Its
compact form is chosen for reliable agent transcription; authorization still comes from
the current-user boundary and the complete durable/live identity tuple.
