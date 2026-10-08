# Rodex

**Whenever you type `codex`, try `rodex` instead.**

Your favourite Codex harness, supercharged in Python. Keep the real Codex TUI,
editor, tools, and approvals. Add staying power, a window onto your agents,
and a few things you'll wonder how you worked without.

- **Let Codex cook.** Detach, leave it running on its host, and come back by a
  memorable name like `automatic-beluga`.
- **Watch the crew.** Your agents' progress and replies unfold in a live pane
  above Codex. Keep working below.
- **Less babysitting.** When a turn fails from server overload, Rodex waits and
  sends `Continue...` for you. Repeated overloads back off; typing cancels a pending retry.
- **Take the controls.** Start, steer, interrupt, and collect exact-turn results
  from another shell or agent.
- **Make it yours.** Change prompts, presentation, and session behaviour in Python,
  without rebuilding Codex. Simple prompt rules live in YAML.

## Get going

From a checkout with the [requirements installed](INSTALL.md#prerequisites):

```bash
uv sync --locked
./rodex
```

Detach with `Ctrl-D` or `Ctrl-b d`. Return with `./rodex NAME`.

[Install](INSTALL.md) · [Use Rodex](docs/CLI.md) · [Design](DESIGN.md) · [Develop](docs/DEVELOPMENT.md)

Development mode: **ALPHA** — in-house Linux pre-release; interfaces may change.
