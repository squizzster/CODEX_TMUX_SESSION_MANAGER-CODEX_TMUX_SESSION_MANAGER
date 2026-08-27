# Installing Rodex

Rodex currently runs from its project checkout. The system command is a small shim
that sends every invocation to that checkout through its `uv` environment.

After installation, `rodex` is intended to replace `codex` in normal terminal use:
Rodex's exact underscore commands stay local, a managed name opens its durable session,
current interactive options and an optional prompt start a managed session, and Codex
subcommands or uncertain option forms are delegated unchanged.

## Requirements

Install the requirements listed in [README.md](README.md#requirements), then prepare
the project environment from the project root:

```bash
uv sync
```

## Install the command for one user (recommended)

From the project root, copy the provided shim into your user command directory:

```bash
mkdir -p "$HOME/.local/bin"
install -m 0755 usr/local/bin/rodex "$HOME/.local/bin/rodex"
```

Ensure `$HOME/.local/bin` is on `PATH`. This installs only the command shim. The project
checkout and its `.venv` remain in place and must not be removed while it is in use.

The shim refuses a checkout, executable, or project path with an untrusted owner or
group/world-write access. This prevents a command installed at a trusted location from
silently crossing into mutable code owned by another user.

Verify the installation:

```bash
command -v rodex
rodex _help
rodex _running
```

Rodex may report that a newer Codex release exists, but it never installs one. Run
`codex update` outside Rodex when you choose to update Codex.

Rodex keeps its durable registry under `$XDG_STATE_HOME/rodex`, using
`~/.local/state/rodex` when `XDG_STATE_HOME` is unset. See
[Local data](README.md#local-data) for the database and runtime paths and their
environment-variable overrides.

## A checkout at another path

The supplied shim defaults to this project's current checkout path. If the checkout
moves or another user installs it elsewhere, set `RODEX_PROJECT_DIR` to that absolute
path before running Rodex:

```bash
export RODEX_PROJECT_DIR=/absolute/path/to/rodex
rodex _running
```

Add that export to the shell's startup configuration when the override should persist.

## System-wide installation

A shared `/usr/local/bin/rodex` must not point at a user's mutable development checkout.
First install a root-owned, non-group/world-writable project and environment at a stable
system path, set that absolute path as the shim's default, then install the shim:

```bash
sudo install -m 0755 usr/local/bin/rodex /usr/local/bin/rodex
```

The supplied checkout-bound shim deliberately rejects root execution when its project
is owned by an ordinary user. Prefer the per-user route unless a separately maintained
system installation is genuinely required.

## Update or remove

After pulling an updated shim, run the install command above again.

Remove a per-user command with:

```bash
rm "$HOME/.local/bin/rodex"
```

Remove only the system command with:

```bash
sudo rm /usr/local/bin/rodex
```

Removing the shim does not remove the project checkout, environment, or Rodex data.
