- Feature Name: `capsule_interface_system`
- Start Date: 2026-03-22
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#561](https://github.com/unicity-astrid/astrid/issues/561)

# Summary
[summary]: #summary

Formalize the capsule interface system: `[imports]`/`[exports]` declarations in
`Capsule.toml` with semver versioning, WIT (WASM Interface Types) as the
interface definition language, boot-time validation of the dependency graph,
and install-time export conflict detection.

# Motivation
[motivation]: #motivation

Astrid capsules communicate exclusively through an IPC event bus. Before this
RFC, there was no formal way to declare what interfaces a capsule provides or
requires. Capsules published to topics and subscribed to topics, but the kernel
had no visibility into whether the system was complete — if a session capsule
was missing, the react loop would silently fail when no one responded to
`session.v1.request.get_messages`.

This creates three problems:

1. **No boot-time validation.** The kernel loads capsules without checking
   whether required interfaces are present. Missing providers are discovered
   at runtime through timeouts and errors.

2. **No safe removal.** `astrid capsule remove` cannot know whether removing
   a capsule breaks other capsules. There is no dependency graph to check.

3. **No substitutability.** Two capsules that implement the same interface
   have no way to declare that they are interchangeable. Installing a second
   session implementation causes double-processing of events.

This RFC solves all three by introducing typed interface declarations, a
canonical interface definition format, and kernel/CLI enforcement mechanisms.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## Declaring interfaces

Every capsule declares what it provides and what it needs in `Capsule.toml`:

```toml
[imports.astrid]
llm = "^1.0"
session = { version = "^1.0", optional = true }

[exports.astrid]
session = "1.0.0"
```

### Exports

An export says "I provide this interface at this exact version." The version
is a `semver::Version` (exact: `1.0.0`). Multiple capsules may export the
same interface — the kernel warns about duplicates at boot.

### Imports

An import says "I need this interface to function." The version is a
`semver::VersionReq` (range: `^1.0`, `>=1.0 <2.0`, `*`). Imports can be
marked `optional` — the capsule will boot with reduced functionality if the
import is unsatisfied.

### Namespaces

The TOML table name IS the namespace: `[imports.astrid]` means namespace
`astrid`, interface `session`. Third-party namespaces use the same pattern:
`[imports.mycorp]`. This prevents flat-namespace collisions — two unrelated
`session` interfaces in different namespaces coexist without conflict.

## WIT interface definitions

Each interface is formally specified as a WIT (WASM Interface Types) file.
These live in the canonical [wit repository](https://github.com/unicity-astrid/wit):

```wit
package astrid:session@1.0.0;

interface session {
    record message {
        role: string,
        content: string,
        timestamp: string,
    }

    get-messages: func(session-id: string) -> list<message>;
    append-message: func(session-id: string, msg: message);
    create-session: func() -> string;
}
```

The WIT files are installed to `~/.astrid/wit/astrid/` during `astrid init`.
The system capsule's `read_interface` tool lets the LLM read these contracts.

WIT is the spec — it defines the typed schemas. For v0.5.0, capsules do not
compile WIT directly. The IPC bus carries JSON-serialized payloads that
conform to the WIT-defined schemas. Future versions will use `wit-bindgen`
to generate typed SDK bindings.

### Relationship between WIT and Capsule.toml

WIT files define the message schemas. `Capsule.toml` declares which interfaces
a capsule implements or depends on. The kernel validates `Capsule.toml`
declarations at boot — it does NOT read or parse WIT files. WIT is for capsule
authors and SDKs. The kernel does simple string+semver matching on the TOML
declarations.

## What happens at boot

The kernel validates the interface graph before loading capsules:

1. Collect all exports from loaded capsules: `(namespace, name) → [(capsule, version)]`
2. For each required import: verify at least one export satisfies the version
   requirement via semver matching
3. Log errors for unsatisfied required imports, info for unsatisfied optional
4. Warn when multiple capsules export the same interface (double-processing risk)
5. Proceed with boot — unsatisfied imports are warnings, not hard failures
   (pre-1.0 leniency)

## What happens at install

When installing a capsule that exports an interface already exported by an
installed capsule:

1. The CLI scans installed capsules' `meta.json` for export overlap
2. Prompts: "better-session exports astrid/session 1.0.0, already exported by
   astrid-capsule-session. Replace? [y/N]"
3. On confirmation: removes the existing provider, installs the new one
4. In non-interactive mode: error with guidance

No capsule needs to know another capsule's name. Conflicts are derived from
the interface data the system already has.

## What happens at remove

`astrid capsule remove` checks whether the capsule is the sole exporter of any
interface that another capsule imports. If so, removal is blocked unless
`--force` is used. This prevents accidentally breaking the dependency graph.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Capsule.toml schema

### `[imports.<namespace>]`

Each key is an interface name. Values are either:

- **Short form:** `"^1.0"` — a `semver::VersionReq` string
- **Long form:** `{ version = "^1.0", optional = true }` — adds the `optional`
  flag (default `false`)

Interface names must match `^[a-z][a-z0-9-]*$` (lowercase, alphanumeric,
hyphens). Namespace names follow the same pattern.

### `[exports.<namespace>]`

Each key is an interface name. Values are `semver::Version` strings (`"1.0.0"`).

### Uplink restriction

Capsules with `uplink = true` capability MUST NOT declare `[imports]`. Uplinks
are entry points — they cannot depend on other capsules.

## meta.json persistence

On install, the CLI writes the resolved imports and exports into `meta.json`
alongside the capsule's `Capsule.toml`. The kernel reads `Capsule.toml` at
boot; the CLI reads `meta.json` for install-time conflict detection and
dependency tree visualization (`astrid capsule tree`).

```json
{
  "version": "1.0.0",
  "installed_at": "2026-03-21T12:00:00Z",
  "updated_at": "2026-03-21T12:00:00Z",
  "imports": { "astrid": { "llm": "^1.0" } },
  "exports": { "astrid": { "session": "1.0.0" } },
  "wasm_hash": "abc123..."
}
```

## Boot validation algorithm

```
exports_by_interface: HashMap<(namespace, name), Vec<(capsule_name, Version)>>

for each manifest:
    for each (ns, name, version) in manifest.export_triples():
        exports_by_interface[(ns, name)].push((manifest.name, version))

// Warn on duplicate exports
for ((ns, name), providers) in exports_by_interface:
    if providers.len() > 1:
        warn("Multiple capsules export {ns}/{name}: {providers}")

// Check imports
for each manifest:
    for each (ns, name, version_req, optional) in manifest.import_tuples():
        satisfied = exports_by_interface[(ns, name)]
            .any(|(_, v)| version_req.matches(v))
        if !satisfied && !optional:
            error("Required import {ns}/{name} {version_req} not satisfied")
        if !satisfied && optional:
            info("Optional import not satisfied")
```

## Install-time export conflict detection

```
for each (ns, name, _) in new_capsule.export_triples():
    for each installed_capsule (skip self):
        if installed_capsule.meta.exports[ns][name] exists:
            conflicts.push((ns/name, installed_capsule.name))

if conflicts && interactive:
    prompt "Replace {conflicting_capsules}? [y/N]"
    if yes: remove conflicting capsules, proceed
    if no: abort

if conflicts && !interactive:
    error "Export conflict, remove existing capsule first"
```

## WIT file storage

WIT files are content-addressed in `~/.astrid/wit/` using BLAKE3 hashes.
Standard interfaces are stored in `~/.astrid/wit/astrid/` with human-readable
names. Custom WIT files from capsule authors are stored by hash.

The `bin/` and `wit/` directories are append-only — artifacts are never deleted
on capsule remove. This preserves audit provability (the BLAKE3 hash in audit
entries must always resolve to a real file). Future `astrid gc` for explicit
cleanup.

# Drawbacks
[drawbacks]: #drawbacks

- **Pre-1.0 churn.** Interface versions will change frequently. Capsule
  authors must update version requirements as interfaces evolve.

- **Boot-time cost.** Validating the full import/export graph adds O(I+E) work
  at boot. Negligible for typical deployments (< 50 capsules).

- **No runtime enforcement.** An export declaration is a promise, not a proof.
  A capsule can declare `exports session = "1.0.0"` and not actually handle
  session requests. WIT compilation (future) will close this gap.

- **Append-only store growth.** `bin/` and `wit/` grow monotonically. For
  long-running systems with many capsule updates, this requires periodic
  garbage collection (not yet implemented).

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why not `provides`/`requires` with string prefixes?

The original design (implemented and reverted) used
`provides = ["rfc:session.v1", "topic:session.v1.*"]` and
`requires = ["rfc:llm-provider.v1"]`. This was rejected because:

- Flat namespace — no protection against collisions between unrelated interfaces.
  If two independent developers both create a `session` interface, they collide.
- No semver — version matching was exact string comparison. No way to express
  "any 1.x" or "at least 1.2".
- Mixed abstraction levels — `rfc:`, `topic:`, `tool:`, `llm:`, `uplink:`
  prefixes conflated interface contracts with implementation details.

## Why not Cargo-style `[dependencies]`?

Considered `[dependencies]` with `provides`/`requires` as a nod to Cargo
conventions. Rejected because Cargo dependencies are crate-level (code
dependencies), while our declarations are interface-level (capability
dependencies). `imports`/`exports` better communicates the semantics and
matches the WIT component model terminology.

## Why TOML table namespacing?

`[imports.astrid]` uses TOML's native nesting. The table name IS the namespace.
No additional syntax, no string parsing, no escape characters. Cargo made a
deliberate choice for a flat namespace (`serde` not `dtolnay/serde`). We chose
namespacing to prevent the collision problem Cargo accepted.

## Why WIT instead of JSON Schema or protobuf?

WIT is the WASM ecosystem's interface definition language. Since capsules are
WASM modules, WIT is the natural fit. Future `wit-bindgen` integration gives
capsule authors compile-time type safety. JSON Schema describes shapes but not
function signatures. Protobuf requires a compilation step and doesn't integrate
with the WASM component model.

## Why export conflict detection instead of `supersedes`?

A `supersedes = "old-capsule-name"` field was implemented and rejected.
Supersedes is name-based — capsule B must know capsule A's exact name.
Third-party capsules from a registry can't know what the user has installed.
Export conflict detection derives the conflict from interface data the system
already has, following the Nix model: immutable content-addressed storage +
declarative configuration = no need for name-based replacement metadata.

See also: Debian uses three orthogonal primitives (`Provides` + `Conflicts` +
`Replaces`) which are more expressive but still require packages to name each
other. RPM uses `Obsoletes` — single field, but name-based with the same
limitation. Nix avoids all of these because the package store is
content-addressed and the system configuration is declarative.

## Impact of not standardizing

Without typed interface declarations, Astrid capsules are just WASM binaries
that publish to topics. There is no dependency graph, no safe removal, no
substitutability, no boot validation. The system is a message bus, not a
runtime.

# Prior art
[prior-art]: #prior-art

- **Cargo** (`Cargo.toml`): `[dependencies]` with semver version requirements.
  Cargo's flat crate namespace is a known limitation. The `version = "^1.0"`
  semver requirement syntax is directly adopted.

- **Nix**: Content-addressed package store. No `Replaces`/`Conflicts`/`Obsoletes`
  concepts. Conflicts are detected from capabilities, not package names.
  Architectural property: immutable content-addressed storage + declarative
  configuration = no mutable global namespace to conflict over. Astrid's
  append-only `bin/` store and install-time conflict detection follow this model.

- **Debian** (`dpkg`): `Provides` + `Conflicts` + `Replaces` — three orthogonal
  primitives for package replacement. `Provides` is the closest analog to
  `[exports]`. More expressive than `supersedes` but still name-based.

- **RPM**: `Obsoletes` — single field for package succession. `Provides` for
  virtual capabilities. Name-based, same fundamental limitation as `supersedes`.

- **WIT** (WASM Interface Types): Component model interface definition language.
  Used by `wasmtime`, `jco`, `wit-bindgen`. Astrid uses WIT as the spec format
  for interface contracts without compiling components (for now).

- **OSGi** (Java): Bundle manifest with `Import-Package` and `Export-Package`
  headers with version ranges. The closest prior art to Astrid's import/export
  model — same semver matching, same boot-time resolution. OSGi proved the
  model works for large plugin ecosystems.

- **COM/DCOM** (Windows): Interface UUIDs with version numbers. Demonstrated
  that typed interface declarations + registry-based resolution enables
  component substitutability at scale. Too heavyweight for Astrid's model.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Strict vs lenient boot.** Should unsatisfied required imports hard-fail
  the boot (refuse to load the capsule) or warn and proceed? Currently we warn
  and proceed for pre-1.0 flexibility. Post-1.0, a missing required import is
  arguably a broken deployment that should fail fast. The counter-argument:
  partial functionality is better than no functionality — the user can install
  the missing provider while the system runs.

- **Version-aware conflict detection.** Currently any export overlap on
  `(namespace, name)` is a conflict regardless of version. Two capsules
  exporting `session 1.0.0` and `session 2.0.0` could theoretically coexist
  if consumers specify non-overlapping version requirements. Is this a real
  use case or premature generalization?

- **Interface exclusivity.** Should capsules be able to declare
  `[conflicts.astrid] session = "*"` to enforce "I'm the only provider of
  this interface"? This would replace the interactive prompt with a
  declarative constraint. Benefit: no user interaction needed. Risk: prevents
  legitimate multi-provider scenarios.

- **Export verification.** An export declaration is currently trust-based.
  Should the kernel verify at boot (or install) that a capsule actually handles
  the IPC topics corresponding to its exported interface? This requires either
  WIT compilation or a conformance test framework.

- **Circular imports.** The current toposort rejects circular dependencies.
  Should two capsules be allowed to import each other if they both export
  different interfaces? The IPC bus doesn't require ordering — both can boot
  and subscribe before either publishes.

# Future possibilities
[future-possibilities]: #future-possibilities

- **`wit-bindgen` integration.** SDK generates typed Rust/Python/Go structs
  from WIT files. Capsule authors get compile-time type safety against the
  interface contracts. The IPC bus carries typed payloads instead of JSON.

- **Interface-level mutual exclusion.** `[conflicts.astrid] session = "*"` —
  "I'm the only session provider, deactivate any other." Resolves the
  multi-provider problem declaratively without user interaction.

- **Runtime conformance testing.** Generate test harnesses from WIT specs
  that verify a capsule actually implements the interface it claims to export.
  Run as part of `astrid capsule install` or a dedicated `astrid capsule test`.

- **Garbage collection.** `astrid gc` to reclaim space in the append-only
  `bin/` and `wit/` stores. Only removes artifacts not referenced by any
  installed capsule's `meta.json`.

- **Cross-principal interface scoping.** Per-principal interface instances
  where each principal gets their own session provider — different session
  backends for different users on the same kernel.

- **Interface deprecation.** A mechanism for marking an interface version as
  deprecated (still functional, but emit warnings on import). Helps capsule
  authors migrate to newer versions.
