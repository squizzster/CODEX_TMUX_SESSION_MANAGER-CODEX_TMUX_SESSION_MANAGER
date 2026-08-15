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
2. tmux starts a small supervisor directly; Rodex does not type commands with
   `send-keys`.
3. The supervisor starts one private Codex app-server on a short Unix socket and then
   starts the normal TUI with `codex --remote unix://...`.
4. Rodex asks that private app-server for its one loaded Codex session UUID.
5. One SQLite transaction creates the Rodex identity, permanent cool name, user/log
   rows, and tmux match.
6. tmux is renamed to the cool name and displays `Rodex: <cool-name>` in its status.
7. Rodex attaches to the ordinary Codex prompt; arguments and slash commands work.

## Named reattachment

- `./rodex <cool-name>` resolves the name through its integer identity.
- The stored socket and tmux name identify the exact existing session.
- An older token-named session is renamed to its cool name before attachment.
- Reattachment updates `last_accessed_at_utc`; it does not start another Codex.

## Lifecycle

- `Ctrl-b d` detaches while Codex, its app-server, and tmux continue running.
- Exiting the Codex TUI ends its supervisor and private app-server.
- A failure before SQL registration stops the exact new tmux session and leaves no
  partial database row.
- The runtime uses `$XDG_RUNTIME_DIR` when suitable, otherwise `/tmp/rodex-<uid>`.
  Unix sockets stay there because long project paths can exceed Linux socket limits.

Recovery policy, richer navigation, and enterprise logging will evolve from real use.
