# Installation and updates

## Prerequisites

- Linux with `/proc` and `pidfd` support.
- Python 3.12 or newer, `uv`, and tmux on `PATH`.
- Installed, authenticated stable Codex CLI at or above the minimum enforced by
  [`CODEX_APP_SERVER`](src/rodex/app_server_contract.py) (currently 0.151.0).

[pyproject.toml](pyproject.toml) and [uv.lock](uv.lock) own dependency constraints.
The `uv_build` version range is a build-backend requirement, not a minimum `uv` CLI
version. Rodex does not enforce numeric SQLite or tmux version floors. Use the
[live gate](docs/DEVELOPMENT.md#validation-gates) to verify the installed combination.

From the checkout root:

```bash
uv sync --locked
./rodex _help
./rodex
```

The [checkout launcher](rodex) executes `.venv/bin/rodex` directly. Startup never
syncs dependencies or downloads code. Before CLI composition,
[`enter_fixed_installation`](src/rodex/installation.py) publishes/reuses a private
copy of code, installed dependencies, and shipped defaults, then re-executes there.

## Install the per-user command

The [supplied shim](usr/local/bin/rodex) defaults to the maintainer's absolute
checkout path. Set your actual checkout path before its first invocation; preserve
that export in shell startup configuration if using the installed command.

```bash
export RODEX_PROJECT_DIR="$(pwd -P)"
mkdir -p "$HOME/.local/bin"
install -m 0755 usr/local/bin/rodex "$HOME/.local/bin/rodex"
```

Ensure `$HOME/.local/bin` is on `PATH`. Only the shim is copied; the checkout and
its `.venv` remain the bootstrap for new invocations. The shim rejects untrusted
ownership, writable executable content, and unexpected symlinks, including in
ignored project directories. See [executable admission](docs/SECURITY.md#executable-admission).

```bash
command -v rodex
rodex _help
rodex _running
rodex
```

Confirm the TUI renders, note its Rodex name, detach with `Ctrl-D`, and run
`rodex NAME` to reopen it. Help and roster output alone do not prove startup.
For automated smoke and release gates, including which tests use model quota,
see [development validation](docs/DEVELOPMENT.md#validation-gates).

Rodex removes its own bootstrap virtualenv from the child environment, but preserves
a different caller-activated project virtualenv. The authority is
[`user_process_environment`](src/rodex/process_environment.py), shared with direct
Codex passthrough and transient App Server probes.

## Retained installations and compatibility

The fingerprint in [`implementation_digest`](src/rodex/implementation_identity.py)
covers first-party source, installed dependency contents, Python version, and shipped
defaults. Documentation edits do not change it. External/editable dependency paths
are rejected; only Rodex itself may use the checkout's editable path. Wheel installs
include the same prompt defaults and use the same retention boundary.

Publication is locked, verified, and atomic. Helpers use the retained interpreter
with `-I`. Python aliases normalize within the selected environment, not to a shared
base interpreter. Identical contents can reuse the same fingerprint store. Published copies
are never overwritten or automatically removed. Retain their directories and base
Python while associated sessions are running. Locations and overrides are in
[local state](docs/OPERATIONS.md#local-state).

New implementations can coexist with old daemons. A retained installation's
`.venv/bin/rodex` entrypoint accesses its exact implementation's sessions; a newer
entrypoint cannot acquire older live runtimes. Catalogs are schema-generation scoped:
builds within one generation share the catalog, but live admission also requires the
exact implementation. Earlier catalogs are not migrated; Codex owns its transcripts.
The narrowly permitted pre-retention resize bridge is documented under
[runtime compatibility](docs/RUNTIME_ISOLATION.md#compatibility-boundary).

## Update or relocate

For runtimes launched with 0.15 or later:

1. Update the bootstrap checkout to reviewed code and run `uv sync --locked`.
2. Reinstall the shim if it changed and run the applicable development gates.
3. Start a fresh runtime to use the new implementation. Detaching and reattaching
   keeps the live runtime's loaded code and retained helpers.
4. Keep old retained installations until their sessions exit.

Pre-0.15 runtimes did not pin helpers. Prefer a separate checkout/environment during
that upgrade and leave the old bootstrap untouched until its sessions end.

After moving a bootstrap checkout, update `RODEX_PROJECT_DIR`, recreate its `.venv`
with `uv venv --clear --python 3.12 .venv`, then run `uv sync --locked`. Virtualenvs
contain absolute paths; this operation replaces the bootstrap environment, not retained
installations. Retained installations contain absolute source/interpreter paths and
are not relocatable, even offline; do not copy them to a new store expecting reuse.

To relocate catalog/configuration state, first stop all Rodex processes using it.
Preserve the original retained-installation store and select its absolute path with
`RODEX_INSTALLATIONS_ROOT`, or let the bootstrap publish into a fresh empty store.
Move the remaining state with its private permissions and complete SQLite file set,
set an absolute `XDG_STATE_HOME` consistently, and restart. This does not relocate
external Codex rollouts or tmux endpoints. Never replace a live database or protected
parent: [SQL identity checks](docs/SQL_SCHEMA.md#storage-and-transactions) fail at the
next transaction boundary and require a fresh process.

## System installation and removal

A shared `/usr/local/bin/rodex` must point to a separately maintained root-owned,
non-group/world-writable checkout and environment, not a user's development tree.
Set that stable absolute path as the shim's default before installing it:

```bash
sudo install -m 0755 usr/local/bin/rodex /usr/local/bin/rodex
```

The supplied checkout-bound shim deliberately rejects root execution against a
user-owned project. Prefer the per-user installation.

Removing `$HOME/.local/bin/rodex` (or `/usr/local/bin/rodex` for the system route)
removes only that command. It does not remove the checkout, retained installations,
or Rodex data.
