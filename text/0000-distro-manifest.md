- Feature Name: `distro_manifest`
- Start Date: 2026-03-17
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

Define a `Distro.toml` manifest schema for declaring curated bundles of
capsules. A distro manifest is a declarative document that lists capsules,
their sources, version constraints, and environment configuration hints.
The runtime uses this schema to resolve and install a complete working
environment from a single manifest.

# Motivation
[motivation]: #motivation

Astrid's capsule ecosystem currently has no mechanism for declaring a set
of capsules as a coherent unit. Each capsule is independently sourced and
installed. There is no way for a third party to publish "install these 15
capsules together" as a stable, versioned artifact.

A manifest schema for capsule bundles enables:

- Third-party distro authors to declare tested combinations of capsules.
- Tooling to validate, resolve, and install bundles as atomic units.
- Version constraints across capsules within a bundle.
- Environment configuration hints that are scoped to the bundle's context.

This RFC defines the contract surface: the schema that distro authors write
and the runtime parses. It does not prescribe CLI commands, onboarding UX,
or installation mechanics — those are implementation concerns.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

A distro manifest is a TOML file named `Distro.toml` that declares metadata
about the distribution and lists the capsules it includes.

```toml
[distro]
name = "astralis"
version = "0.1.0"
description = "The complete Astrid AI assistant experience"
astrid-version = ">=0.1.0"
default-profile = "power_user"

[[capsule]]
name = "astrid-capsule-anthropic"
source = "@unicity-astrid/capsule-anthropic"
required = true
group = "ai"

[capsule.env]
ANTHROPIC_API_KEY = { required = true }

[[capsule]]
name = "astrid-capsule-fs"
source = "@unicity-astrid/capsule-fs"
group = "tools"
```

A distro is not a capsule. It has no WASM component, no IPC topics, no
capabilities. It is a packaging manifest that exists outside the runtime —
resolved at install-time, before the capsule runtime boots.

The manifest uses the same source format as `Capsule.toml`'s `repository`
field and the same `semver::VersionReq` parsing as `astrid-version`.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

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

## `[[capsule]]` array

One or more entries. Each declares a capsule to include in the distro.

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `name` | `String` | yes | Non-empty, max 128 chars. Must match the capsule's `package.name`. No duplicates within the manifest. |
| `source` | `String` | yes | Non-empty. Same source formats as `astrid capsule install`: `@org/repo`, GitHub URL, `openclaw:name`, local path. |
| `version` | `String` | no | Valid `semver::VersionReq`. If present, the installed capsule's version must satisfy this constraint. |
| `required` | `bool` | no | Default `false`. When `true`, failure to resolve this capsule is a fatal error for the entire distro. |
| `description` | `String` | no | Overrides the capsule's own description for display. |
| `group` | `String` | no | Display-only grouping label (e.g. `"infrastructure"`, `"ai"`, `"tools"`). No runtime semantics. |

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

## Validation

A conforming parser must reject a `Distro.toml` that violates any of:

1. `distro.name` does not match `^[a-z][a-z0-9-]*$` or exceeds 64 chars.
2. `distro.version` is not valid semver.
3. `distro.astrid-version` is present and not a valid `semver::VersionReq`.
4. `distro.default-profile` is present and not a recognized profile name.
5. Zero `[[capsule]]` entries.
6. Any `capsule.name` is empty or exceeds 128 chars.
7. Any `capsule.source` is empty.
8. Any `capsule.version` is present and not a valid `semver::VersionReq`.
9. Duplicate `capsule.name` values.

## Relationship to `Capsule.toml`

`Distro.toml` references capsules by name and source but does not extend or
modify `Capsule.toml`. A capsule's identity, capabilities, components, and
dependencies remain defined entirely in its own `Capsule.toml`. The distro
manifest adds a layer above: selection, version gating, and env hints.

There is no runtime interaction between the two schemas. `Distro.toml` is
consumed at install-time; `Capsule.toml` is consumed at boot-time.

# Drawbacks
[drawbacks]: #drawbacks

- Introduces a second manifest schema alongside `Capsule.toml`. Ecosystem
  participants now have two TOML formats to understand. Mitigated by keeping
  `Distro.toml` deliberately minimal — it has no capability, component, IPC,
  or tool declarations.

- No lockfile. Two installs of the same distro version at different times may
  resolve different capsule versions (if `version` constraints are loose or
  absent). Lockfile semantics are deferred.

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
metadata lacks version constraints, required/optional semantics, and env hints.
The schema overhead is minimal and the expressiveness gains are significant.

# Prior art
[prior-art]: #prior-art

- **Homebrew Bundle (`Brewfile`)** — Flat list of packages. No version
  constraints or error tiers. `Distro.toml` adds both.
- **Docker Compose** — Multi-service declarations with image sources and
  environment config. Similar "manifest declares a set of components" pattern.
- **NixOS configuration** — Declarative system configuration that pins exact
  package sets. The closest conceptual parallel. `Distro.toml` is lighter
  (no full dependency solver).
- **VS Code Extension Packs** — Meta-extensions that bundle other extensions.
  Uses the extension dependency mechanism rather than a separate manifest.
  Rejected approach for Astrid (see Rationale).

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Lockfile schema.** Should a companion `Distro.lock` pin exact versions
  and content hashes? If so, what is its format? Deferred to a follow-up RFC.
- **Distro composition.** Should a distro be able to extend another via a
  `base` field? Adds complexity; deferred pending ecosystem demand.
- **Profile extensibility.** `default-profile` is currently constrained to
  the four built-in profiles. Should distros be able to define custom profiles
  inline?

# Future possibilities
[future-possibilities]: #future-possibilities

- **`Distro.lock`** for byte-reproducible installs with content hashes.
- **Distro inheritance** via a `base` field for additive specialization.
- **Distro-level capability constraints** that tighten or relax capsule
  defaults within the bundle.
- **Signed manifests** using ed25519 for supply-chain integrity.
- **Channel support** (`stable`, `beta`, `nightly`) as a version selector.
