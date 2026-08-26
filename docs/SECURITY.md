# Security model

Rodex is a local, single-POSIX-user tool. Its trust boundary is the operating-system
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
- SQLite databases are current-user-owned regular files at mode `0600` below a private
  directory. Final symlinks and nonregular paths are rejected. WAL plus a busy timeout
  separates normal readers from analytics writes.
- Named runtime transitions use current-user-owned regular advisory-lock files at mode
  `0600` with no-follow opens. The per-session lock is held through identity checking
  and endpoint replacement, then released before terminal attachment.
- Analytics reads only current-user-owned regular rollout files inside the configured
  sessions root, using no-follow and nonblocking opens before authenticating the Codex
  thread ID and stable complete-record prefix. Startup-only lineage discovery is bounded
  to the root UUIDv7 three-day window and reads only candidate metadata lines.
- tmux operations use argv execution and exact `=name` or `=name:` targets. Dynamic hook
  commands are shell-quoted; cleanup and failure handling target one named runtime.
- `_cat`, `_tail`, and `_events` resolve the same owned, registered live identity before
  reading. `_agents`, `_trace`, and `_stats` resolve the owned durable identity and need
  no live runtime; explicit trace body reads re-authenticate the recorded rollout prefix.
  Terminal following emits only tmux's plain text and creates no persistent conversation
  copy.
- The install shim accepts only root- or current-user-owned, non-group/world-writable
  project code and `uv`. A system command must use an immutable root-owned installation.

Rodex does not auto-adopt or auto-delete an unregistered or unverifiable tmux session.
`rodex _running` reports it for explicit diagnosis. This is deliberate: an orphan is
less harmful than attaching to or destroying the wrong runtime.

The 16-hex Rodex session ID is an integrity discriminator, not a credential.
Authorization comes from the current-uid filesystem boundary plus the exact durable and
live identity tuple. Do not expose the ID later as a bearer-authentication token.
The 16-hex runtime ID has the same role: incarnation fencing, not authentication. Its
compact form is chosen for reliable agent transcription; authorization still comes from
the current-user boundary and the complete durable/live identity tuple.
