- Feature Name: `distro_manifest`
- Start Date: 2026-03-17
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#550](https://github.com/unicity-astrid/astrid/issues/550)

# Summary
[summary]: #summary

Define a `Distro.toml` manifest schema and companion `Distro.lock` lockfile for
declaring curated bundles of capsules. A distro manifest is a declarative
document that lists capsules, their sources, version constraints, contract
requirements, and environment configuration hints. The lockfile pins exact
resolved versions and content hashes for reproducible installs.

The runtime uses these schemas to resolve, install, and validate a complete
working environment from a single manifest.

# Motivation
[motivation]: #motivation

Astrid's capsule ecosystem currently has no mechanism for declaring a set of
capsules as a coherent unit. Each capsule is independently sourced and installed.
There is no way for a third party to publish "install these 15 capsules
together" as a stable, versioned artifact with integrity guarantees.

A manifest schema for capsule bundles enables:

- Third-party distro authors to declare tested combinations of capsules.
- Tooling to validate, resolve, and install bundles as atomic units.
- Version constraints across capsules within a bundle.
- Contract enforcement: the distro declares what capabilities it requires
  (e.g. an LLM provider), and resolution validates that at least one capsule
  in the bundle provides each required contract.
- Reproducible installs via a lockfile with exact versions and BLAKE3 hashes.
- Environment configuration hints that are scoped to the bundle's context.

This RFC defines the contract surface: the schemas that distro authors write
and the runtime parses. It does not prescribe CLI commands, onboarding UX,
or installation mechanics — those are implementation concerns.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

A distro manifest is a TOML file named `Distro.toml` that declares metadata
about the distribution and lists the capsules it includes.

```toml
schema-version = 1

[distro]
name = "astralis"
version = "0.1.0"
description = "The complete Astrid AI assistant experience"
astrid-version = ">=0.5.0"
default-profile = "power_user"
requires = ["rfc:llm-provider.v1", "rfc:tool-execution.v1"]

[[capsule]]
name = "astrid-capsule-anthropic"
source = "@unicity-astrid/capsule-anthropic"
version = "0.1.0"
required = true
group = "ai"
provides = ["rfc:llm-provider.v1"]
priority = 10

[capsule.env]
ANTHROPIC_API_KEY = { required = true }

[[capsule]]
name = "astrid-capsule-openai-compat"
source = "@unicity-astrid/capsule-openai-compat"
version = "0.1.0"
group = "ai"
provides = ["rfc:llm-provider.v1"]
priority = 20

[capsule.env]
OPENAI_API_KEY = { required = true }

[[capsule]]
name = "astrid-capsule-fs"
source = "@unicity-astrid/capsule-fs"
version = "0.1.0"
group = "tools"
provides = ["rfc:tool-execution.v1"]
```

A distro is not a capsule. It has no WASM component, no IPC topics, no
capabilities. It is a packaging manifest that exists outside the runtime —
resolved at install-time, before the capsule runtime boots.

## Contracts: provides and requires

Capsules implement contracts — formalized interface specifications identified
by RFC number. The format is `rfc:<name>.v<version>`, e.g.
`rfc:llm-provider.v1`. Each identifier maps directly to a merged RFC in the
`unicity-astrid/rfcs` repository that specifies the exact tool schemas, IPC
topics, and host function requirements. The RFC IS the contract.

The `provides` field on a `[[capsule]]` entry declares which contracts that
capsule implements. The `requires` field on the `[distro]` table declares which
contracts the distro needs at least one capsule to provide.

When multiple capsules in the same group provide the same contract, the
`priority` field determines ordering. Lower values take priority. During
interactive init, the user is prompted to pick a provider when multiple
options exist.

## Source resolution

Sources use the `@org/repo` shorthand for GitHub repositories:

- `@unicity-astrid/capsule-anthropic` resolves to
  `https://github.com/unicity-astrid/capsule-anthropic`
- Version resolution uses git tags: `version = "0.1.0"` resolves to tag `v0.1.0`
- The runtime looks for a pre-built WASM binary as a GitHub release asset on
  that tag
- If no release asset exists, the runtime falls back to building from source
  (developer workflow)

## The lockfile

After resolving a `Distro.toml`, the runtime generates a `Distro.lock` file
that pins exact versions and content hashes. The lockfile lives in `etc/`
within the Astrid data directory (system-level for v0.5.0).

```toml
schema-version = 1

[distro]
name = "astralis"
version = "0.1.0"
resolved-at = "2026-03-20T14:30:00Z"

[[capsule]]
name = "astrid-capsule-anthropic"
version = "0.1.0"
source = "@unicity-astrid/capsule-anthropic"
hash = "blake3:af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"

[[capsule]]
name = "astrid-capsule-fs"
version = "0.1.0"
source = "@unicity-astrid/capsule-fs"
hash = "blake3:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
```

The lockfile enables:

- Reproducible installs: `astrid init` with an existing lockfile installs
  exactly those versions, skipping resolution.
- Integrity verification: the BLAKE3 hash is checked against the content-
  addressed binary in `bin/` on every install.
- Update tracking: `astrid update` compares the lock against the manifest,
  resolves new versions, and writes a new lock.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Top-level fields

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `schema-version` | `u32` | yes | Currently `1`. A parser that encounters a value it does not support must reject the manifest with a clear error. |

## `[distro]` table

Required. Exactly one per manifest.

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `name` | `String` | yes | `^[a-z][a-z0-9-]*$`, max 64 chars. |
| `version` | `String` | yes | Valid semver (`MAJOR.MINOR.PATCH`). |
| `description` | `String` | no | |
| `authors` | `Vec<String>` | no | `"Name <email>"` convention. |
| `repository` | `String` | no | URL to the distro source. |
| `license` | `String` | no | SPDX identifier. |
| `astrid-version` | `String` | no | Valid `semver::VersionReq`. Runtime rejects the manifest if the constraint is not satisfied. |
| `default-profile` | `String` | no | One of: `safe`, `power_user`, `autonomous`, `ci`. Defaults to `safe`. |
| `requires` | `Vec<String>` | no | List of contract identifiers (`rfc:<name>.v<N>`). At least one capsule in the manifest must provide each listed contract. |

## `[[capsule]]` array

One or more entries. Each declares a capsule to include in the distro.

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `name` | `String` | yes | Non-empty, max 128 chars. Must match the capsule's `package.name`. No duplicates within the manifest. |
| `source` | `String` | yes | Non-empty. Source formats: `@org/repo` (GitHub shorthand), full GitHub URL, `openclaw:name`, local path. |
| `version` | `String` | no | Valid semver (`MAJOR.MINOR.PATCH`). Resolves to git tag `v{version}`. If absent, resolves to the latest tag. |
| `required` | `bool` | no | Default `false`. When `true`, failure to resolve this capsule is a fatal error for the entire distro. |
| `description` | `String` | no | Overrides the capsule's own description for display purposes. |
| `group` | `String` | no | Display-only grouping label (e.g. `"infrastructure"`, `"ai"`, `"tools"`). No runtime semantics beyond provider selection prompts. |
| `provides` | `Vec<String>` | no | List of contract identifiers (`rfc:<name>.v<N>`) that this capsule implements. |
| `priority` | `u32` | no | Default `100`. Lower values take priority when multiple capsules provide the same contract. Used for fallback ordering and interactive provider selection. |

### `[capsule.env]` table

Optional. Keys are environment variable names. Values are tables:

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `required` | `bool` | no | Default `false`. When `true`, the variable must be elicited before the distro is considered fully configured. |
| `default` | `String` | no | Default value to pre-fill. |
| `description` | `String` | no | Overrides the capsule's own `env.*.description`. |

Environment overrides are **hints**, not values. They do not store secrets.
They inform the runtime which environment variables matter for this distro's
context and how to prompt for them. Actual storage uses the same secure
mechanism as standalone capsule installation.

## `Distro.lock` schema

Generated by the runtime after resolving a `Distro.toml`. Not hand-authored.

### Top-level fields

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `schema-version` | `u32` | yes | Must match the `Distro.toml` schema-version. |

### `[distro]` table

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `name` | `String` | yes | Must match the manifest's `distro.name`. |
| `version` | `String` | yes | Must match the manifest's `distro.version`. |
| `resolved-at` | `String` | yes | ISO 8601 UTC timestamp of when the lock was generated. |

### `[[capsule]]` array

One entry per resolved capsule.

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `name` | `String` | yes | Must match a `capsule.name` in the manifest. |
| `version` | `String` | yes | Exact resolved semver. |
| `source` | `String` | yes | Fully resolved source (no shorthand). |
| `hash` | `String` | yes | `blake3:{hex}` — BLAKE3 hash of the installed WASM binary. |

### Lock resolution rules

1. If `Distro.lock` exists and its `distro.name` and `distro.version` match
   the manifest, install from the lock (skip resolution).
2. If `Distro.lock` is absent or stale (name/version mismatch), resolve from
   the manifest and write a new lock.
3. `astrid update` always re-resolves against the manifest, even if the lock
   is fresh.
4. The lock is authoritative: if a lock entry specifies `version = "0.1.0"`
   but the manifest now says `version = "0.2.0"`, the lock wins until the
   user runs `astrid update`.

## Contract validation

At distro resolution time, the resolver must validate:

1. For each identifier in `distro.requires`, at least one `[[capsule]]` entry
   lists that identifier in its `provides` field.
2. If a required contract has no provider among `required = true` capsules,
   emit a warning (the provider is optional and may not be installed).

At kernel boot time (separate from distro resolution), the kernel performs its
own provides/requires validation against `Capsule.toml` declarations. The
distro-level validation is an early check that catches misconfigurations before
downloading anything.

## Version resolution

Version resolution uses git tags:

1. `version = "0.1.0"` resolves to git tag `v0.1.0`.
2. The runtime looks for a pre-built WASM binary as a GitHub release asset
   attached to that tag.
3. If no release asset is found, the runtime clones the repository at that
   tag and builds from source using `astrid-build`.
4. The resolved WASM binary is stored content-addressed in `bin/` using
   BLAKE3. The hash is recorded in `Distro.lock`.

### No-downgrade rule

If a capsule is already installed with a version that satisfies the manifest
constraint, the resolver keeps it. The resolver never downgrades — if the
installed version exceeds the constraint, a warning is emitted but the
installed version is preserved.

### Orphan detection

When re-resolving (e.g. after manifest edits), capsules present in the old
lock but absent from the new manifest are flagged as orphans. Orphans are
**warned**, not silently removed. Explicit removal requires `astrid capsule
remove`.

## Forward compatibility

- Unknown fields at any level are ignored with a warning logged at `info`
  level. This allows future schema versions to add fields without breaking
  older parsers.
- `schema-version` is the breaking-change mechanism. A parser must reject a
  manifest with a schema-version it does not support, with a clear error
  message indicating the minimum runtime version required.
- Unknown contract identifiers in `provides` and `requires` are preserved
  verbatim. The resolver does not validate that an identifier maps to a
  real RFC — that is the distro author's responsibility.

## Manifest validation

A conforming parser must reject a `Distro.toml` that violates any of:

1. `schema-version` is missing or not a supported value.
2. `distro.name` does not match `^[a-z][a-z0-9-]*$` or exceeds 64 chars.
3. `distro.version` is not valid semver.
4. `distro.astrid-version` is present and not a valid `semver::VersionReq`.
5. `distro.default-profile` is present and not a recognized profile name.
6. Any identifier in `distro.requires` does not match the contract format
   `rfc:<name>.v<N>`.
7. Zero `[[capsule]]` entries.
8. Any `capsule.name` is empty or exceeds 128 chars.
9. Any `capsule.source` is empty.
10. Any `capsule.version` is present and not valid semver.
11. Duplicate `capsule.name` values.
12. Any identifier in `capsule.provides` does not match the contract format
    `rfc:<name>.v<N>`.
13. Any required contract in `distro.requires` has no provider among the
    `[[capsule]]` entries.

## Relationship to `Capsule.toml`

`Distro.toml` references capsules by name and source but does not extend or
modify `Capsule.toml`. A capsule's identity, capabilities, components, and
dependencies remain defined entirely in its own `Capsule.toml`.

The `provides` field in `Distro.toml` is a **declaration** that mirrors what
the capsule declares in its own `Capsule.toml`. If there is a discrepancy,
the `Capsule.toml` is authoritative at runtime. The distro-level `provides`
exists for resolution-time validation (before the capsule is downloaded).

There is no runtime interaction between the two schemas. `Distro.toml` is
consumed at install-time; `Capsule.toml` is consumed at boot-time.

# Drawbacks
[drawbacks]: #drawbacks

- Introduces a second manifest schema alongside `Capsule.toml`. Ecosystem
  participants now have two TOML formats to understand. Mitigated by keeping
  `Distro.toml` deliberately minimal — it has no capability, component, IPC,
  or tool declarations.

- The `provides` field duplicates information from `Capsule.toml`. This is
  intentional: it enables resolution-time validation without downloading every
  capsule first. The trade-off is that distro authors must keep `provides`
  in sync with capsule declarations.

- Content-addressed storage means old binaries accumulate in `bin/`. Garbage
  collection (removing binaries not referenced by any capsule's `meta.json`)
  is deferred to a future CLI command.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why not a meta-capsule?** A distro could be a `Capsule.toml` that declares
`requires` dependencies on other capsules. Rejected because: (1) distros
operate before the WASM runtime exists, (2) `Capsule.toml` would need new
fields (`source`, `required`, `env` overrides) that pollute the schema for
all capsules, (3) capsule dependencies are runtime (capability-based), not
install-time (source-based).

**Why not extend `Capsule.toml`?** Adding a `[distro]` section to
`Capsule.toml` conflates runtime identity with packaging. A capsule's manifest
should describe what it *is*, not what other capsules it ships alongside.

**Why not a flat list (Brewfile-style)?** A plain list of sources with no
metadata lacks version constraints, required/optional semantics, contract
enforcement, and env hints. The schema overhead is minimal and the
expressiveness gains are significant.

**Why `rfc:<name>.v<N>` for contract identifiers?** Self-descriptive and
versioned. Each identifier maps to a real RFC document. Analogous to ERC-20
on Ethereum — the RFC IS the contract, the identifier IS the name. This
avoids a separate registry of contract IDs and ensures every contract has
a formal specification backing it.

**Why a lockfile instead of pinning in the manifest?** The manifest declares
intent (constraints), the lock records reality (exact versions + hashes).
This is the same split as `Cargo.toml`/`Cargo.lock`. Distro authors commit
the manifest; operators commit the lock for reproducibility.

**Why BLAKE3 for hashes?** Already used for content-addressed WASM binaries
in `bin/`. Consistency with the existing integrity model. Fast enough for
large binaries.

**Why `schema-version` instead of manifest version?** Schema-version is a
forward-compatibility mechanism for the format itself, independent of the
distro's semantic version. A parser can reject manifests it cannot understand
without parsing the rest of the file.

**Why one distro per system (v0.5.0)?** Simplifies the initial implementation.
The content-addressed binary store means per-principal distros (future work)
add no binary duplication — they are metadata-only overlays on the shared
`bin/` store.

# Prior art
[prior-art]: #prior-art

- **Cargo.toml / Cargo.lock** — The direct inspiration for the manifest/lock
  split. `Cargo.toml` declares version constraints; `Cargo.lock` pins exact
  versions with checksums. Distro.toml/Distro.lock follows the same pattern.

- **NixOS configuration** — Declarative system configuration with content-
  addressed packages. Astrid's `bin/` store with BLAKE3 hashes is structurally
  the Nix model. Per-principal home directories with shared binaries mirrors
  Nix's per-user profiles over a shared store.

- **Debian alternatives** — Multiple packages providing the same command
  (e.g. `editor`, `x-terminal-emulator`). Astrid's `provides`/`requires`
  with `priority` for fallback ordering follows the same pattern.

- **ERC-20 (Ethereum)** — Formalized contract identifiers where the standard
  IS the contract. Astrid's `rfc:llm-provider.v1` identifiers follow this
  philosophy: the RFC number is the contract, the specification is the
  interface.

- **systemd Requires/Wants** — Dependency semantics for service units.
  Astrid's `requires` on the distro table is the hard-dependency equivalent;
  optional capsules are the `Wants` equivalent.

- **Homebrew Bundle (`Brewfile`)** — Flat list of packages. No version
  constraints or error tiers. `Distro.toml` adds both.

- **Docker Compose** — Multi-service declarations with image sources and
  environment config. Similar "manifest declares a set of components" pattern.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Distro composition.** Should a distro be able to extend another via a
  `base` field? Adds complexity; deferred pending ecosystem demand.
- **Profile extensibility.** `default-profile` is currently constrained to
  the four built-in profiles. Should distros be able to define custom profiles
  inline?
- **Contract version compatibility.** Should `rfc:llm-provider.v2` be
  considered a superset of `rfc:llm-provider.v1`? Currently no — each version
  is an independent contract. Compatibility semantics are deferred.
- **Source authentication.** Should sources require signature verification
  beyond BLAKE3 content hashes? Deferred to the signed manifests future
  possibility.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Per-principal distros.** The content-addressed binary store means
  per-principal distro assignments add no binary duplication. Each principal's
  `Distro.lock` is metadata pointing into the shared `bin/` store. An admin
  principal could enforce which distro a user gets, or allow users to self-
  select.
- **Provisioning via capability scoping.** An admin principal creates user
  principals with scoped capabilities. The `capsule-install` capability
  determines the mode: full = free (user installs anything), restricted =
  hybrid (user can install from approved list), none = enforced (distro only).
  No external orchestrator needed — the runtime IS the multi-tenant platform.
- **Distro inheritance** via a `base` field for additive specialization.
- **Distro-level capability constraints** that tighten or relax capsule
  defaults within the bundle.
- **Signed manifests** using ed25519 for supply-chain integrity.
- **Channel support** (`stable`, `beta`, `nightly`) as a version selector.
- **Binary garbage collection** — a CLI command to remove content-addressed
  WASM binaries from `bin/` that are not referenced by any installed capsule.
