# Codex tmux session manager

Development mode: **EXP**

`ctsm` keeps named Codex CLI sessions alive inside tmux so they can be detached,
listed, and resumed from a terminal. This first experiment deliberately wraps the
stable Codex interactive CLI rather than reading or rewriting Codex's own session
storage.

## Try it

Requires Linux, Python 3.12+, `tmux`, and the `codex` CLI.

```bash
uv sync
uv run ctsm doctor
uv run ctsm start genome --cwd /path/to/workspace --attach
uv run ctsm list
uv run ctsm attach genome
uv run ctsm stop genome
```

An optional first prompt can be passed with `--prompt`. Managed tmux sessions are
prefixed with `codex-`; unrelated tmux sessions are left alone. When `attach` is
called from inside tmux, `ctsm` switches clients instead of attempting a nested
attach.

## Current experiment boundary

- Session identity and liveness come from tmux.
- Conversation persistence and resumption remain Codex responsibilities.
- There is no dashboard, automatic restoration, metadata database, or orchestration
  policy yet; those should follow observed use rather than precede it.

Run the focused checks with `uv run pytest`.
