# Installation and updates

## Prerequisites

- Linux with `/proc` and `pidfd` support.
- Python 3.11 or newer and tmux on `PATH`.
- `uv` to prepare or update a checkout. An installed shim does not require `uv` for a
  user who already has another usable Rodex environment.
- Installed, authenticated stable Codex CLI at or above the minimum enforced by
  [`CODEX_APP_SERVER`](src/rodex/app_server_contract.py).

[pyproject.toml](pyproject.toml) and [uv.lock](uv.lock) own dependency constraints.
The `uv_build` version range is a build-backend requirement, not a minimum `uv` CLI
version. Rodex does not enforce numeric SQLite or tmux version floors. Use the
[live gate](docs/DEVELOPMENT.md#validation-gates) to verify the installed combination.

Crash reconciliation additionally needs group-scoped pidfd signalling:
[`PIDFD_SIGNAL_PROCESS_GROUP`](https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html)
is upstream since Linux 6.9 and is also present in some enterprise backports.
Rodex uses the capability, not a kernel-version string. An unsupported/denied
operation, unverifiable surviving group, or unconfirmed exit retains its receipt and
refuses that implementation's daemon startup; there is no numeric termination fallback.
Ordinary help/native delegation and startup without outstanding receipts do not probe
this capability. See [receipt lifetime and verification limits](docs/RUNTIME_ISOLATION.md#resource-lifetime).

From the checkout root:

```bash
uv sync --locked
./rodex _help
./rodex
```

The [checkout launcher](rodex) executes `.venv/bin/rodex` directly. The
[installed shim](usr/local/bin/rodex) resolves the first structurally usable Rodex
environment available to the invoking user; a missing or inaccessible configured
checkout does not prevent a per-user environment from being selected. Launcher
discovery never installs Python, creates an environment, syncs dependencies, or
downloads code. Once an environment has been selected, package import and dependency
errors remain visible and normal Rodex startup begins.
Before CLI composition,
[`enter_fixed_installation`](src/rodex/installation.py) publishes/reuses a private
copy of code, installed dependencies, and shipped defaults, then re-executes there.
An unchanged bootstrap is recognized from lightweight file metadata; routine startup
does not hash source or dependency contents.

## Command and environment are separate

`/usr/local/bin/rodex` and `$HOME/.local/bin/rodex` are launch shims, not bundled
Python applications. Copying either shim makes the command discoverable but does not
install the `rodex` package or its dependencies. Each invoking user still needs one
existing environment selected by the [bounded resolver](#install-the-per-user-command).

Seeing `ModuleNotFoundError: No module named 'rodex'` means the shim found a supported
standalone Python but that interpreter could not import the package. This is an
intentional diagnostic, not permission for the shim to install anything. A plain
`python3.11 --version` therefore proves only the interpreter requirement.

Rodex invokes standalone interpreters with `-I` and clears `PYTHONHOME`, `PYTHONPATH`,
and `PYTHONSTARTUP`. Consequently, `python -m pip install --user ...` and an ad-hoc
`PYTHONPATH` do not satisfy launcher discovery. Use a real tool/virtual environment,
an administrator-maintained system environment, or the checkout `.venv`.

### Prepare a per-user uv tool environment

This is the simplest independent environment when the user already has `uv`. A
maintainer first builds the reviewed checkout:

```bash
uv build
```

Make the resulting wheel and every parent directory readable/traversable by the
target user. That user then deliberately installs the tool with an existing Python:

```bash
uv tool install --python python3.11 --no-python-downloads \
  /readable/path/to/codex_tmux_session_manager-0.17a0-py3-none-any.whl
"$(uv tool dir --bin)/rodex" _help
"$(uv tool dir --bin)/rodex" _version
```

This explicit `uv tool install` command creates and modifies the user's tool
environment; the launcher never runs it. The wheel is not a dependency bundle: `uv`
also resolves the runtime dependencies declared in [pyproject.toml](pyproject.toml),
including the pinned protocol-analyzer source, so the user needs repository/index
access or a suitably populated cache. `--no-python-downloads` ensures this command
uses an already installed interpreter rather than fetching one.

`uv` also links the package's `rodex` entry point into its user tool-bin directory.
If that directory precedes `/usr/local/bin` on `PATH`, the user entry point runs the
same tool environment directly. Otherwise, the supplied shim recognizes the exact
`codex-tmux-session-manager` environment in the normal uv tool directory. Once the
environment exists, `uv` itself is not needed for each Rodex invocation.

### Prepare an explicit virtual environment

Without a uv tool environment, an owner can use an existing Python installation that
provides `venv` and `pip`:

```bash
python3.11 -m venv "$HOME/.local/share/rodex/bootstrap"
"$HOME/.local/share/rodex/bootstrap/bin/python" -m pip install \
  /readable/path/to/codex_tmux_session_manager-0.17a0-py3-none-any.whl
export RODEX_ENVIRONMENT="$HOME/.local/share/rodex/bootstrap"
"$RODEX_ENVIRONMENT/bin/rodex" _help
```

This install has the same dependency/source-access requirements as the uv-tool route.
Persist `RODEX_ENVIRONMENT` in that user's shell configuration so the supplied shim
can select it. If `venv`, `pip`, the wheel, or a required dependency is unavailable,
preparation stops there; the owner or administrator decides how to supply it. Rodex
does not install an OS package manager, Python, `uv`, `pip`, or its own package as a
fallback.

## Install the per-user command

Skip this section when an administrator already supplied `/usr/local/bin/rodex` or
`uv tool install` placed a satisfactory `rodex` entry point on `PATH`. To copy the
repository shim instead, note that it defaults to the maintainer's absolute checkout
path. Set your actual checkout path before its first invocation; preserve that export
in shell startup configuration if using the installed command.

```bash
export RODEX_PROJECT_DIR="$(pwd -P)"
mkdir -p "$HOME/.local/bin"
install -m 0755 usr/local/bin/rodex "$HOME/.local/bin/rodex"
```

Ensure `$HOME/.local/bin` is on `PATH`. Only the shim is copied. It checks a bounded
sequence of already-existing capabilities:

1. An explicit `RODEX_ENVIRONMENT` containing an executable `bin/rodex` and a
   Python 3.11-or-newer interpreter.
2. The configured checkout `.venv`.
3. The caller's active `VIRTUAL_ENV`.
4. An explicit existing `RODEX_PYTHON`.
5. Exact uv-tool environment names (`codex-tmux-session-manager` and `rodex`) beneath
   `UV_TOOL_DIR`, the user's normal uv tool directory, or the directory reported by an
   existing `uv tool dir` command. `RODEX_UV` selects an exact uv executable.
6. Python 3.11-or-newer candidates on `PATH` and at fixed locations under
   `/usr/local/bin` and `/usr/bin`.

Environment candidates are verified for an executable entry point and Python version;
their imports are not preflighted. Automatic standalone-Python discovery first
prefers a candidate that can import Rodex. If none can, the first compatible
interpreter runs the command so its missing-package or module error remains visible.
If no compatible interpreter or environment exists, the shim reports that the user's
environment is unsupported and exits without changing anything. It does not invoke
`uv sync`, `uv run`, installation commands, `find`, `rg`, or recursive inspection.
See the [launcher boundary](docs/SECURITY.md#launcher-boundary).

A successful Rodex startup still creates or updates its documented private runtime,
state, and retained-installation data. The no-modification guarantee above applies to
launcher discovery and unsupported-environment failure, not normal application use.

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

The optional native command hook has a separate
[setup workflow](docs/CLI.md#command-policy-check). Retained-installation updates
can change its helper path and native hash; the
[hook admission contract](docs/SECURITY.md#hook-admission-and-resume) covers trust
reuse and automatic admission across start, resume, and fork.

## Retained installations and compatibility

Rodex records first-party source-file metadata, top-level environment entries, the
Python version, and shipped-default metadata in a bootstrap cache. If that ordinary
state is unchanged, startup reuses the retained interpreter without reading executable
contents. A change publishes one new retained copy with an opaque installation key.
External/editable dependency paths are rejected because they cannot be copied into a
self-contained retained environment; only Rodex itself may use the checkout's editable
path. Wheel installs include the same prompt defaults and use the same boundary.

Preparing a new copy reports the source release version, full installation ID,
source-code and installed-dependency paths, and the completed installation's path
on stderr. The release comes from the source being copied; the installation ID
distinguishes copies even when their release versions match. These start/completion
messages are flushed; cached launches stay silent and stdout is unchanged. Completion
means the installation is ready, not that a session has attached. Waiting for the
preparation lock is silent.

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
runtime when its Codex thread is idle. Refusals that leave an older live runtime
running include a command to reconnect using `rodex NAME --force-old`; busy turns
also explain how to retry the upgrade when idle. The flag attaches through the
retained installation without upgrading or interrupting its work.
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
with `uv venv --clear --python 3.11 .venv`, then run `uv sync --locked`. Virtualenvs
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

A shared `/usr/local/bin/rodex` should default to one separately maintained checkout
rather than a changing development tree. Users who cannot read or execute its
environment can still use one of their own existing candidates. Set the stable
checkout's absolute path as the shim's default. From that checkout, install or update
the command with:

```bash
sudo install -m 0755 "$(pwd -P)/usr/local/bin/rodex" /usr/local/bin/rodex
```

Each invoking OS user must already have one usable candidate: access to the maintained
checkout environment, an active/explicit Rodex environment, a Rodex uv-tool
environment, or a Python 3.11-or-newer installation that can import Rodex. Installing
the shared shim alone is therefore insufficient; use
[one explicit preparation route](#command-and-environment-are-separate) for every user
who cannot execute the maintained checkout environment. A shared checkout `.venv`
that points into a maintainer's private home is rejected by execution, not repaired.
The shim never provisions missing capability. The maintained checkout supplies
executable source only when its environment is selected; otherwise the selected user
or system environment supplies it. Use ordinary filesystem ownership for those trust
boundaries.

After replacing the command, verify both the selected shim and a real target user:

```bash
command -v rodex
sudo -iu TARGET_USER /usr/local/bin/rodex _help
```

Removing `$HOME/.local/bin/rodex` (or `/usr/local/bin/rodex` for the system route)
removes only that command. It does not remove the checkout, retained installations,
or Rodex data.
