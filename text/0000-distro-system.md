- Feature Name: `distro_system`
- Start Date: 2026-03-22
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#567](https://github.com/unicity-astrid/astrid/issues/567)

# Summary
[summary]: #summary

Define the distro system: `Distro.toml` manifests for declaring curated bundles
of capsules, `Distro.lock` for reproducible installations, provider groups for
multi-select during init, shared variables with template resolution, and the
`astrid init` flow that ties it together.

# Motivation
[motivation]: #motivation

Astrid OS is a microkernel — the kernel provides primitives (sandbox, IPC, audit,
capabilities) but ships no application logic. Every user-facing feature (LLM
provider, session management, tool routing, CLI frontend) is a capsule.

A fresh Astrid installation has no capsules. The user needs:

1. A curated set of capsules that work together (a "distro")
2. A way to install them in one step
3. Provider selection (which LLM backend? which frontend?)
4. Shared configuration (API keys, model preferences)
5. Reproducible installs (same distro = same versions = same behavior)

Without a distro system, onboarding requires manually installing 12+ capsules,
configuring each one, and hoping the versions are compatible. The distro system
makes `astrid init` a one-command experience.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## Distro.toml

A distro is a TOML manifest declaring a curated bundle of capsules:

```toml
schema-version = 1

[distro]
id = "astralis"
name = "Astralis"
pretty-name = "Astralis 0.1.0 (Genesis)"
version = "0.1.0"
codename = "genesis"
release-date = "2026-03-21"
description = "The complete Astrid AI assistant experience"
maintainers = ["Joshua J. Bouw <josh@unicity-labs.com>"]
homepage = "https://github.com/unicity-astrid/astralis"
astrid-version = ">=0.5.0"

[distro.requires.astrid]
llm = "^1.0"
session = "^1.0"

[variables]
api_key = { secret = true, description = "API key for LLM provider" }

[[capsule]]
name = "astrid-capsule-cli"
source = "@unicity-astrid/capsule-cli"
version = "0.1.0"
role = "uplink"

[[capsule]]
name = "astrid-capsule-openai-compat"
source = "@unicity-astrid/capsule-openai-compat"
version = "0.1.0"
group = "llm"

[capsule.env]
api_key = "{{ api_key }}"
```

### Metadata

The `[distro]` section follows os-release conventions (Debian, Fedora). The
`pretty-name` field is what users see. The `codename` is tradition.

### Provider groups

Capsules with `group = "llm"` are presented as multi-select during init.
Multiple providers can coexist (different models for different tasks). Capsules
without a group are always installed.

### Roles

`role = "uplink"` marks frontend capsules. Validation: a distro must have at
least one uplink. Roles are deployment metadata, not interface contracts.

### Variables

`[variables]` declares shared configuration. Capsules reference variables via
`{{ var }}` templates in their `[capsule.env]` section. During init, the user
is prompted only for variables needed by their selected capsules. Secret
variables use `secret = true` for masked input.

## Distro.lock

A per-principal lockfile at `home/{principal}/.config/distro.lock`:

```toml
[meta]
schema-version = 1
distro-id = "astralis"
distro-version = "0.1.0"
locked-at = "2026-03-21T12:00:00Z"

[[capsule]]
name = "astrid-capsule-cli"
version = "0.1.0"
wasm-hash = "abc123..."
```

The lockfile records exact versions and BLAKE3 hashes. `astrid capsule update`
regenerates the lock after updates. This enables reproducible installs — same
lock = same binaries.

## Init flow

`astrid init [--distro NAME]`:

1. Fetch `Distro.toml` from `raw.githubusercontent.com/unicity-astrid/{name}/main/Distro.toml`
2. Check lockfile freshness — skip if already initialized and up to date
3. Display distro info (pretty-name, description)
4. Multi-select providers per group
5. Prompt for variables needed by selected capsules
6. Install each capsule with progress bar
7. Install standard WIT interfaces to `~/.astrid/wit/astrid/`
8. Write per-capsule `.env.json` with resolved templates
9. Write `Distro.lock`

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Distro.toml schema

### Top-level

| Field | Type | Required | Description |
|---|---|---|---|
| `schema-version` | integer | yes | Always `1` for this RFC |

### `[distro]`

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Machine-readable identifier (lowercase, hyphens) |
| `name` | string | yes | Human-readable name |
| `pretty-name` | string | no | Display name with version and codename |
| `version` | semver | yes | Distro version |
| `codename` | string | no | Release codename |
| `release-date` | date | no | ISO 8601 date |
| `description` | string | no | One-line description |
| `maintainers` | string[] | no | "Name \<email\>" format |
| `homepage` | URL | no | Project URL |
| `astrid-version` | version-req | no | Minimum kernel version |

### `[distro.requires.<namespace>]`

Interface requirements for the distro. Same semantics as capsule imports.
Validation: the selected capsules must collectively satisfy all requirements.

### `[variables]`

| Field | Type | Required | Description |
|---|---|---|---|
| key | string | — | Variable name |
| `secret` | bool | no | Mask input (default false) |
| `description` | string | no | Prompt text |

### `[[capsule]]`

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Capsule package name |
| `source` | string | yes | Source URI (see source resolution) |
| `version` | semver | yes | Expected version |
| `role` | string | no | `"uplink"` for frontends |
| `group` | string | no | Provider group name |

### `[capsule.env]`

Key-value pairs. Values may contain `{{ variable }}` templates resolved from
`[variables]` during init.

## Source resolution

| Pattern | Resolution |
|---|---|
| `@org/repo` | `https://github.com/org/repo` |
| `https://...` | Direct URL (any Git host) |
| `./path` or `/path` | Local directory |
| `openclaw:name` | OpenClaw registry (future) |

Version tags: `v{semver}` → GitHub release asset `.wasm`. Fallback: clone +
`astrid-build` from source.

## Distro.lock schema

### `[meta]`

| Field | Type | Description |
|---|---|---|
| `schema-version` | integer | Always `1` |
| `distro-id` | string | From `Distro.toml` |
| `distro-version` | semver | From `Distro.toml` |
| `locked-at` | datetime | ISO 8601 UTC timestamp |

### `[[capsule]]`

| Field | Type | Description |
|---|---|---|
| `name` | string | Capsule package name |
| `version` | semver | Installed version |
| `wasm-hash` | string | BLAKE3 hash of the WASM binary (if applicable) |

### Freshness check

A lock is "fresh" if `distro-id` and `distro-version` match the manifest.
`astrid init` skips installation if the lock is fresh.

## Validation rules

1. `schema-version` must be `1`
2. `distro.id` must match `^[a-z][a-z0-9-]*$`
3. `distro.version` must be valid semver
4. No duplicate capsule names
5. At least one capsule with `role = "uplink"`
6. All `{{ var }}` references in `[capsule.env]` must have corresponding
   `[variables]` entries
7. Capsule versions must be valid semver
8. `astrid-version` (if present) must be valid semver requirement
9. Interface requirements in `[distro.requires]` must use valid semver

# Drawbacks
[drawbacks]: #drawbacks

- **Single distro per principal.** A principal can only have one active distro.
  Multiple distros would require a distro-switching mechanism.
- **Network dependency at init.** Fetching from GitHub requires connectivity.
  Offline installs need pre-downloaded `.capsule` archives or local sources.
- **No distro composition.** You cannot layer two distros or inherit from a
  base distro. This limits customization to provider selection and variables.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why not a package manager?

A package manager (like `apt` or `cargo install`) resolves dependencies from
a registry. Astrid's capsules have interface dependencies, not code
dependencies. The distro model provides a curated, tested bundle rather than
ad-hoc resolution.

## Why `Distro.toml` + `Distro.lock` instead of `Cargo.toml` + `Cargo.lock`?

The naming follows Rust conventions for the manifest/lockfile pair. The
semantics differ: `Distro.toml` is a deployment manifest (what to install),
not a build manifest (what to compile).

## Why per-principal lockfiles?

Different principals may have different provider selections and configurations.
A Discord bot principal might use GPT-4 while a CLI principal uses Claude.
Per-principal locks enable this without global state conflicts.

## Why `{{ var }}` template syntax?

Simple, unambiguous, doesn't conflict with TOML syntax. Mustache-style
templates are widely understood. The resolver does a single pass — no
recursive expansion.

# Prior art
[prior-art]: #prior-art

- **Linux distros** (Debian, Fedora, NixOS): Curated package sets with
  release metadata. `os-release` provides the metadata schema convention.
  NixOS's `configuration.nix` is the closest analog to `Distro.toml` —
  a declarative system description.

- **Cargo** (`Cargo.toml` + `Cargo.lock`): Manifest/lockfile pattern with
  semver resolution. Direct inspiration for the naming convention.

- **Docker Compose** (`docker-compose.yml`): Service bundle manifest with
  environment variable interpolation. Similar role to `Distro.toml` but
  for containers instead of capsules.

- **Helm Charts** (`Chart.yaml` + `values.yaml`): Kubernetes package manager
  with template variable system. The `[variables]` + `{{ var }}` pattern
  follows this model.

- **Spin** (Fermyon): `spin.toml` for WASM application composition. Provider
  selection at deploy time. Simpler variable model than Astrid's.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Offline installation.** Should `astrid init` support `--offline` mode with
  pre-downloaded `.capsule` archives? Enterprise deployments behind firewalls
  need this. The mechanism exists (local source resolution) but the UX for
  packaging a distro for offline use doesn't.

- **Distro inheritance.** Should `extends = "astralis"` allow layering? An
  enterprise distro could inherit the base Astralis capsules and add
  company-specific ones. Risk: inheritance creates implicit dependencies —
  the parent distro's capsules aren't visible in the child's manifest.

- **Lock scope.** The lockfile currently records capsule versions and hashes.
  Should it also lock WIT file versions? Capsule source URLs? If the SDK repo
  reorganizes WIT files, the lock is stale but passes freshness checks.

- **Update policy.** `astrid capsule update` currently fetches the latest
  version from the original source. Should it respect the distro's version
  pins? If the distro says `version = "0.1.0"`, should update refuse to go
  to `0.2.0`? Or should it update and warn about drift from the distro?

- **Multi-distro.** Can a principal install capsules from multiple distros?
  Currently the lock only records one `distro-id`. A user might want the base
  Astralis distro plus a domain-specific extension distro (e.g., `astralis` +
  `astralis-devops`).

# Future possibilities
[future-possibilities]: #future-possibilities

- **Distro registry.** A central index of published distros (like Docker Hub
  for distros).
- **Distro composition.** `extends` field for building on top of existing
  distros.
- **Distro diff.** Show what changed between two lockfiles.
- **Distro rollback.** `astrid rollback` restores a previous lockfile state.
  Content-addressed `bin/` store makes this cheap — old WASM binaries persist.
- **CI/CD distro testing.** `astrid init --dry-run` validates a distro
  manifest without installing.
