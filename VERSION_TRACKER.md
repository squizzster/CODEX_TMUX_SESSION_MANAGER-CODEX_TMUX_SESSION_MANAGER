# Current version and compatibility

Rodex is **0.17a**, an **ALPHA** Linux pre-release. Build metadata normalizes the
package version to `0.17a0`. Package versions, wire contracts and storage generations
are separate identities.

## Authorities

| Boundary | Authoritative source |
| --- | --- |
| Package version and Python minimum | [pyproject.toml](pyproject.toml), [RODEX_VERSION](src/rodex/version.py), [uv.lock](uv.lock) |
| Codex App Server minimum and stable-version admission | [CODEX_APP_SERVER](src/rodex/app_server_contract.py) |
| Characterized native CLI grammar | [Codex 0.151.0 grammar](src/codex_cli_contract/v0_151_0.py) |
| Current wire, storage and projection generations | [Current-contract assertions](tests/test_current_contracts.py) |
| Invoked installation's effective contracts | `rodex _version`, defined by [command_contract.py](src/rodex/command_contract.py) |

## Compatibility rules

- The installation key identifies retained code, not a Git commit, content digest,
  or security attestation. Updating the checkout does not replace live helpers.
- Catalog compatibility does not establish live-runtime ownership. Earlier catalogs
  are not implicitly migrated; unknown wire generations are refused.
- Within base catalog 22, trace v5 has one supported, verified normalization rebuild
  to trace v6/statistics v11. [Analytics recovery](docs/ANALYTICS.md#publication-and-recovery)
  defines its source verification, identity preservation and atomic repair audit.
- Supported retained runtimes use the [idle upgrade or explicit reconnect route](docs/RUNTIME_ISOLATION.md#retained-runtime-upgrade).
  The [resize-only legacy bridge](docs/RUNTIME_ISOLATION.md#compatibility-boundary)
  grants no launch, stop, adoption or model-input authority.
- The base App Server minimum does not guarantee optional hook support or native
  warning placement. See [hook admission](docs/SECURITY.md#hook-admission-and-resume)
  and [native notice limits](docs/INTERACTION_PATHS.md#native-notice-limits).

[Development validation](docs/DEVELOPMENT.md#validation-gates) owns verification.
Git history retains earlier package milestones and implementation changes.
