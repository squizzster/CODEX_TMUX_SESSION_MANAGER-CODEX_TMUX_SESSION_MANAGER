# Installation and updates

## Prerequisites

- Linux with `/proc` and `pidfd` support.
- Python 3.12 or newer, `uv`, and tmux on `PATH`.
- Installed, authenticated stable Codex CLI at or above the minimum enforced by
  [`CODEX_APP_SERVER`](src/rodex/app_server_contract.py).

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
An unchanged bootstrap is recognized from lightweight file metadata; routine startup
does not hash source or dependency contents.

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
its `.venv` remain the bootstrap for new invocations. The shim checks only that the
configured project and executable entrypoint exist, then delegates normal executable
admission to the operating system. It does not recursively inspect the checkout.
See the [launcher boundary](docs/SECURITY.md#launcher-boundary).

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

Rodex records first-party source-file metadata, top-level environment entries, the
Python version, and shipped-default metadata in a bootstrap cache. If that ordinary
state is unchanged, startup reuses the retained interpreter without reading executable
contents. A change publishes one new retained copy with an opaque installation key.
External/editable dependency paths are rejected because they cannot be copied into a
self-contained retained environment; only Rodex itself may use the checkout's editable
path. Wheel installs include the same prompt defaults and use the same boundary.

Publication is locked and atomic. The lock coordinates the uncommon copy operation,
not every retained runtime. Helpers use the retained interpreter with `-I`. Python
aliases normalize within the selected environment, not to a shared base interpreter.
Published copies are never overwritten or automatically removed. Retain their
directories and base Python while associated sessions are running. Locations and
overrides are in [local state](docs/OPERATIONS.md#local-state). The installation key
is a routing namespace, not a security attestation or content digest.

Retained implementations can coexist. A retained installation's
`.venv/bin/rodex` entrypoint accesses its exact implementation's sessions. Opening
a supported older session through the current entrypoint upgrades that selected
runtime when its Codex thread is idle. Busy turns fail on stderr with commands to
retry when idle or reconnect using `rodex NAME --force-old`. The flag attaches
through the retained installation without upgrading or interrupting its work.
Other sessions remain on their owning daemon. Catalogs are schema-generation scoped:
builds within one generation share the catalog, while live handshakes require the
exact implementation. Earlier catalogs are not migrated; Codex owns its transcripts.
The narrowly permitted pre-retention resize bridge is documented under
[runtime compatibility](docs/RUNTIME_ISOLATION.md#compatibility-boundary).

## Update or relocate

For runtimes launched with 0.15 or later:

1. Update the bootstrap checkout to reviewed code and run `uv sync --locked`.
2. Reinstall the shim if it changed and run the applicable development gates.
3. Run `rodex NAME` while its turn is idle to upgrade a supported retained runtime.
   A session already using the current implementation simply reattaches.
   Use `rodex NAME --force-old` to reconnect and leave an older live runtime running.
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

A shared `/usr/local/bin/rodex` should point to a separately maintained checkout and
environment rather than a user's changing development tree. Set that stable absolute
path as the shim's default before installing it:

```bash
sudo install -m 0755 usr/local/bin/rodex /usr/local/bin/rodex
```

Prefer the per-user installation. The shim does not attempt to enforce a multi-user
trust policy; use ordinary filesystem ownership and OS accounts for that boundary.

Removing `$HOME/.local/bin/rodex` (or `/usr/local/bin/rodex` for the system route)
removes only that command. It does not remove the checkout, retained installations,
or Rodex data.
