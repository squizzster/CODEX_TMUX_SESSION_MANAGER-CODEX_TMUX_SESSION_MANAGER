# Codex, Rodex, and tmux

Rodex is a match-maker and launcher. Each identity keeps its own meaning:

- **Rodex session:** our secure UUID and internal `rodex_sessions.id`.
- **Codex session:** the real UUID allocated by Codex.
- **tmux session:** the exact tmux server socket path plus tmux session name.

Rodex and Codex UUIDs remain explicitly named on the owning session row. The tmux
endpoint is a separate operational row joined by `rodex_sessions_id`. Identities are
never presented or stored as another domain's identity.

## Basic launch pipeline

1. `./rodex` validates the `codex` and `tmux` executables.
2. tmux starts a small supervisor directly; Rodex never types with `send-keys`.
3. The supervisor starts one private Codex app-server and a transparent Rodex
   WebSocket proxy on short Unix sockets, then connects the normal TUI through it.
4. Rodex asks that private app-server for its one loaded Codex session UUID.
5. One SQLite transaction creates the Rodex identity, name, user/log, and tmux rows.
6. tmux is renamed to the cool name and displays its Rodex identity, tool count, and
   live private/shared attachment state in its status bar.
7. Rodex attaches to the ordinary Codex prompt; arguments and slash commands work.

The proxy forwards protocol frames unchanged in both directions, counts unique tool
starts, and fans structured TUI events to bounded live subscribers. tmux user options
advertise the live proxy/event sockets and observed Codex UUID; none are new SQLite
identities. Tool counts cover the current runtime and reset when it is resumed.

## Named reattachment

- `./rodex <cool-name>` resolves the name through its integer identity.
- If its stored tmux endpoint is live, Rodex attaches to it directly.
- If it has ended, Rodex starts a fresh tmux/app-server and asks Codex to resume the
  stored Codex UUID; the observed UUID must match before the endpoint is replaced.
- Both routes preserve all Rodex/Codex identity and update `last_accessed_at_utc`.
- `./rodex running` (`--running`, `sessions`, or `--sessions`) lists the current
  POSIX user's live runtimes.
- `./rodex alias SESSION NAME` sets its portable preferred name; `-f` replaces it.
- `send`, `wait`, and `tail` verify the stored and live Codex UUID before acting.

## Lifecycle

- `Ctrl-b d` detaches while Codex, its app-server, and tmux continue running.
- Exiting the Codex TUI ends its supervisor and private app-server; its cool name can
  transparently resume the saved Codex session later.
- A failure before SQL registration stops the exact new tmux session and leaves no
  partial database row.
- The runtime uses `$XDG_RUNTIME_DIR/rodex` when suitable—normally
  `/run/user/<uid>/rodex`—otherwise `/tmp/rodex-<uid>`. Unix sockets stay there because
  long project paths can exceed Linux socket limits.
- While a session host is alive, it refreshes the runtime root, shared tmux socket, and
  its private sockets and log hourly. A refresh failure ends that runtime rather than
  leaving a detached session that cannot be addressed. Normal cleanup eligibility
  resumes when the live hosts exit.

Exact tmux targets and compensated name transitions preserve the recorded endpoint.
