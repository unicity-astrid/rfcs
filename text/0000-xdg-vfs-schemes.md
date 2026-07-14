- Feature Name: `xdg_vfs_schemes`
- Start Date: 2026-03-24
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#601](https://github.com/unicity-astrid/astrid/issues/601)

# Summary
[summary]: #summary

Introduce XDG-aligned VFS schemes — `config://`, `data://`, `cache://`, and
`state://` — so capsules can access their standard storage locations without
making assumptions about the physical layout of the principal home directory.
The kernel resolves each scheme to the appropriate path under
`~/.astrid/home/{principal}/`, mirroring the XDG Base Directory Specification
within Astrid's virtualised per-principal home.

# Motivation
[motivation]: #motivation

Astrid is a VM running as a program. Each principal has an isolated home
directory at `~/.astrid/home/{principal}/`, structured with `.config/`,
`.local/share/`, `.cache/`, and `.local/state/` subdirectories following XDG
conventions.

Currently capsules access these locations by hardcoding paths under `home://`:

```toml
# Capsule.toml today
capabilities = { fs_read = ["home://"], fs_write = ["home://"] }
```

```rust
// src/lib.rs today
fs::write("home://.config/spark.toml", &data)?;
```

This is wrong for two reasons:

1. **Capsules make assumptions about the OS layout.** `home://.config/` works
   today because the kernel puts `.config/` there, but capsules should not
   depend on this. If the layout changes, every capsule breaks.

2. **Capability declarations are too broad.** `fs_write = ["home://"]` grants
   write access to the entire principal home. A capsule that only needs to
   write one config file should declare exactly that, not blanket home access.

The fix is XDG-aligned schemes: `config://spark.toml` is a declaration of
intent ("I need to read/write my config") that the kernel resolves to the
correct physical path. Capsules stay layout-agnostic; the kernel owns the
mapping.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## New VFS schemes

Four new schemes are added, mapping directly to XDG Base Directory equivalents
within the principal home:

| Scheme | XDG equivalent | Physical path |
|--------|---------------|---------------|
| `config://` | `$XDG_CONFIG_HOME` | `~/.astrid/home/{principal}/.config/` |
| `data://` | `$XDG_DATA_HOME` | `~/.astrid/home/{principal}/.local/share/` |
| `cache://` | `$XDG_CACHE_HOME` | `~/.astrid/home/{principal}/.cache/` |
| `state://` | `$XDG_STATE_HOME` | `~/.astrid/home/{principal}/.local/state/` |

## Capsule usage

Declare the minimum capability needed:

```toml
# Capsule.toml
capabilities = { fs_read = ["config://"], fs_write = ["config://"] }
```

Use the scheme directly in code:

```rust
// Read config
let spark = fs::read_to_string("config://spark.toml")?;

// Write config
fs::write("config://spark.toml", &toml)?;

// Store persistent data
fs::write("data://sessions/current.json", &session)?;

// Cache (evictable)
fs::write("cache://schema-cache.json", &schemas)?;

// State (persists across reboots, not config)
fs::write("state://last-session-id", session_id.as_bytes())?;
```

## Choosing the right scheme

| Use case | Scheme |
|----------|--------|
| Capsule configuration (`spark.toml`, `.env`) | `config://` |
| Persistent user data (sessions, audit logs, KV) | `data://` |
| Derived/evictable data (schema caches, indexes) | `cache://` |
| Runtime state that survives reboots (last session, PID) | `state://` |
| Temp files (cleared on reboot) | `cwd://` or `tmp://` |

## Existing schemes are unchanged

`home://`, `cwd://`, and `wit://` remain. `home://` continues to mean "the
entire principal home root" — useful when a capsule genuinely needs broad
access. The new schemes are narrower, more expressive alternatives for common
access patterns.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Scheme resolution

The kernel's `ManifestSecurityGate::resolve_schemes()` gains four new prefix
handlers:

```
config:// → <principal_home>/.config/
data://    → <principal_home>/.local/share/
cache://   → <principal_home>/.cache/
state://   → <principal_home>/.local/state/
```

Where `<principal_home>` = `~/.astrid/home/{principal}/`.

Resolution is performed at capsule load time (same as `home://` and `cwd://`),
canonicalised once, and stored as physical path prefixes for runtime checks.

## Capability declarations

Each scheme is a valid value in `fs_read` and `fs_write` capability arrays:

```toml
capabilities = { fs_read = ["config://", "data://"], fs_write = ["config://"] }
```

Sub-path scoping is supported:

```toml
# Read-only access to a specific config file
capabilities = { fs_read = ["config://spark.toml"] }

# Write access scoped to one data subdirectory
capabilities = { fs_write = ["data://sessions/"] }
```

## Directory creation

The kernel creates the physical directories for each scheme at principal home
provisioning time (alongside the existing `.config/` creation). No capsule
needs to call `fs::create_dir` for these roots.

## SDK

No new SDK functions are needed. Capsules use the existing `fs::read`,
`fs::write`, `fs::read_to_string`, `fs::exists`, etc. with the new scheme
prefixes.

# Drawbacks
[drawbacks]: #drawbacks

- Adds four new scheme identifiers to the kernel's VFS resolver — small
  maintenance surface.
- Capsules currently using `home://.config/` must migrate. This is a
  mechanical find-and-replace but requires a new release for each capsule.
- `config://` conflicts with the word "config" used elsewhere (e.g.
  `[capabilities]` in Capsule.toml). Developers must distinguish the VFS
  scheme from the manifest key.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why XDG naming?

XDG Base Directory is the established standard for this exact problem. Every
Linux/macOS developer knows `$XDG_CONFIG_HOME`. Using the same names makes the
mapping to the OS model immediately obvious: `config://` IS `$XDG_CONFIG_HOME`,
virtualised.

## Why not just `home://.config/`?

Capsules that hardcode `home://.config/` are making assumptions about the
physical layout. If the kernel ever renames `.config/` to `config/` (or moves
to a flatter structure), all capsules break. The scheme is the stable API; the
physical path is an implementation detail the kernel owns.

## Why not a single `xdg://config/` namespace?

`config://` is more ergonomic and consistent with `home://` and `cwd://`.
A nested namespace (`xdg://config/spark.toml`) adds a level of indirection
without benefit.

## Alternative: keep `home://` with stricter sub-path scoping

Rather than new schemes, we could require `fs_read = ["home://.config/"]` as
the declaration (already supported). This avoids new scheme identifiers but
still hardcodes the physical layout in the manifest. Rejected for the same
layout-coupling reason.

# Prior art
[prior-art]: #prior-art

- **XDG Base Directory Specification** — the direct inspiration. Used by
  virtually all modern Linux applications.
- **macOS container sandbox** — apps declare entitlements for specific
  containers (`~/Library/Application Support/`, `~/Library/Caches/`) rather
  than broad home directory access.
- **Android scoped storage** — apps declare specific storage categories
  (`MUSIC`, `DOCUMENTS`) rather than blanket filesystem access.
- **WebAssembly Component Model** (`wasi:filesystem`) — WASI separates
  pre-opened directories from arbitrary path access, same principle.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should `cache://` contents be evictable by the kernel (e.g. `astrid gc`)?
  The XDG spec says cache is evictable but we have not defined an eviction
  policy.
- Should `tmp://` also be introduced as the fifth scheme, mapping to
  `~/.astrid/home/{principal}/.local/tmp/` (already exists in the FHS layout)?
- Migration path for existing capsules using `home://.config/` — should the
  kernel emit a deprecation warning when `home://` is used with a `.config/`
  sub-path?

# Future possibilities
[future-possibilities]: #future-possibilities

- `tmp://` scheme for per-principal temp files (already has a physical
  directory in the FHS layout).
- `log://` scheme for per-capsule log directories, replacing the current
  kernel-managed log rotation.
- XDG scheme scoping by capsule name by default — e.g. `config://` resolves
  to `~/.astrid/home/{principal}/.config/{capsule-name}/` — so capsules are
  isolated from each other's config without explicit sub-path declarations.
  This would be a separate RFC as it changes the isolation model.
- The `cwd.v1.context` event (tracked in astrid#601) could carry per-directory
  overrides for scheme resolution, enabling project-local config that shadows
  principal-level config.
