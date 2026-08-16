# Installing Rodex

Rodex currently runs from its project checkout. The system command is a small shim
that sends every invocation to that checkout through its `uv` environment.

## Requirements

Install the requirements listed in [README.md](README.md#requirements), then prepare
the project environment from the project root:

```bash
uv sync
```

## Install the command

From the project root, copy the provided shim into `/usr/local/bin`:

```bash
sudo install -m 0755 usr/local/bin/rodex /usr/local/bin/rodex
```

This installs only the command shim. The project checkout and its `.venv` remain in
place and must not be removed while the shim is in use.

Verify the installation:

```bash
command -v rodex
rodex _running
```

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

## Update or remove

After pulling an updated shim, run the install command above again.

Remove only the system command with:

```bash
sudo rm /usr/local/bin/rodex
```

Removing the shim does not remove the project checkout, environment, or Rodex data.
