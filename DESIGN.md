# Change guide

Use the [README index](README.md) to select a topic; use this guide when a change
could belong to more than one boundary. [Architecture](docs/ARCHITECTURE.md) maps
the composed system to its owners.

## Choose the authority

- **Bootstrap, launcher, or update:** [installation](INSTALL.md) owns the distinction
  between command shims, selectable Python environments, and retained installations.
  The [system shim](usr/local/bin/rodex) performs bounded read-only discovery;
  [`enter_fixed_installation`](src/rodex/installation.py) owns publication into the
  private retained store after package startup. Keep discovery behavior covered by
  [shim tests](tests/test_install_shim.py), not by another environment search path.
- **Invocation or option meaning:** start at `COMMAND_SPECS` in
  [command_contract.py](src/rodex/command_contract.py) and
  `UnifiedRodexApplicationPipeline` in [application_pipeline.py](src/rodex/application_pipeline.py).
  [CLI routing](docs/CLI.md#invocation-routing) explains selector/prompt ambiguity
  and native delegation; a new Codex option is not automatically managed syntax.
- **Visible text or model input:** identify the intended effect in
  [interaction paths](docs/INTERACTION_PATHS.md). A local notice, composer rewrite,
  model submission, native tool decision, and tmux status have different authorities.
  Use the [prompt contract](docs/PROMPT_SUBMISSION_FLOW.md) for text transformations
  and [native policy contract](docs/SECURITY.md#native-command-policy) for shell hooks.
- **Live identity or lifetime:** use [runtime isolation](docs/RUNTIME_ISOLATION.md)
  before changing launch, reconnect, replacement, or cleanup. Package compatibility
  alone does not establish ownership of an existing runtime.
- **Durable identity or derived data:** [SQL](docs/SQL_SCHEMA.md) owns schema and
  transaction invariants; [analytics](docs/ANALYTICS.md) owns source admission,
  accounting, and incomplete coverage. A projection change must preserve canonical
  identities and authenticated source coordinates.

## Check compatibility and evidence

[pyproject.toml](pyproject.toml) and [uv.lock](uv.lock) own package/dependency facts;
`CODEX_APP_SERVER` in [app_server_contract.py](src/rodex/app_server_contract.py)
owns native version admission. [Current-contract tests](tests/test_current_contracts.py)
pin persisted and wire generations.

Choose [focused validation](docs/DEVELOPMENT.md#test-selection) for the changed
boundary. If adding an effect, update the existing
[effect-owner audit](docs/INTERACTION_PATHS.md#effect-audit). Dated native findings
are evidence for the stated version and scenario, not compatibility guarantees.
