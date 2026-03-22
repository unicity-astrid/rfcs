- Feature Name: `capsule_interface_system`
- Start Date: 2026-03-22
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#561](https://github.com/unicity-astrid/astrid/issues/561)

# Summary
[summary]: #summary

Formalize the capsule interface system: `[imports]`/`[exports]` declarations in
`Capsule.toml` with semver versioning, WIT (WASM Interface Types) as the
interface definition language, boot-time validation of the dependency graph,
install-time export conflict detection, and a middleware interceptor chain with
short-circuit semantics.

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

## What happens at boot

The kernel validates the interface graph before loading capsules:

1. Collect all exports from loaded capsules: `(namespace, name) → [versions]`
2. For each required import: verify at least one export satisfies the version
   requirement via semver matching
3. Log errors for unsatisfied required imports, info for unsatisfied optional
4. Warn when multiple capsules export the same interface (double-processing risk)
5. Proceed with boot — unsatisfied imports are warnings, not hard failures
   (pre-1.0 leniency)

## What happens at install

When installing a capsule that exports an interface already exported by an
installed capsule:

1. The CLI detects the conflict from `meta.json` exports
2. Prompts: "better-session exports astrid/session 1.0.0, already exported by
   astrid-capsule-session. Replace? [y/N]"
3. On confirmation: removes the existing provider, installs the new one
4. In non-interactive mode: error with guidance

No capsule needs to know another capsule's name. Conflicts are derived from
the interface data the system already has.

## Interceptor chain

Capsules register interceptors on IPC topics with a priority (default 100,
lower fires first). When an event matches multiple interceptors, the dispatcher
runs them sequentially as a middleware chain:

```toml
[[interceptor]]
event = "user.v1.prompt"
action = "guard_input"
priority = 10
```

Each interceptor returns one of:
- **Continue** — pass (possibly modified) payload to the next interceptor
- **Final** — short-circuit with a response, no further interceptors fire
- **Deny** — short-circuit with an audit-logged reason

A guard at priority 10 can reject malicious input before the react loop at
priority 100 ever processes it.

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
    for each installed_capsule:
        if installed_capsule.exports[ns][name] exists:
            conflicts.push((ns/name, installed_capsule.name))

if conflicts && interactive:
    prompt "Replace {conflicting_capsules}? [y/N]"
    if yes: remove conflicting capsules, proceed
    if no: abort

if conflicts && !interactive:
    error "Export conflict, remove existing capsule first"
```

## InterceptResult wire format

WASM guest output bytes are decoded as:

| Discriminant | Meaning | Payload |
|---|---|---|
| `0x00` | Continue | Remaining bytes = modified payload (or empty) |
| `0x01` | Final | Remaining bytes = response |
| `0x02` | Deny | Remaining bytes = UTF-8 reason string |
| Empty | Continue | Backward compatible with existing capsules |
| Unknown | Continue | Forward compatible (full bytes treated as payload) |

## Interceptor dispatch

Single-interceptor events use per-capsule ordered queues (preserving IPC `seq`
ordering). Multi-interceptor events run as a sequential chain in a spawned task:

```
let mut payload = event_payload;
for interceptor in sorted_by_priority_ascending:
    match interceptor.invoke(payload):
        Continue(modified) => payload = modified (if non-empty)
        Final(response) => return  // chain halted
        Deny { reason } => log + return  // chain halted
        Error(NotSupported) => continue  // skip, not a participant
        Error(e) => log + continue  // broken capsule doesn't block chain
```

# Drawbacks
[drawbacks]: #drawbacks

- **Pre-1.0 churn.** Interface versions will change frequently. Capsule
  authors must update version requirements as interfaces evolve.
- **Boot-time cost.** Validating the full import/export graph adds O(I+E) work
  at boot. Negligible for typical deployments (< 50 capsules).
- **No runtime enforcement.** An export declaration is a promise, not a proof.
  A capsule can declare `exports session = "1.0.0"` and not actually handle
  session requests. WIT compilation (future) will close this gap.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why not `provides`/`requires` with string prefixes?

The original design used `provides = ["rfc:session.v1", "topic:session.v1.*"]`
and `requires = ["rfc:llm-provider.v1"]`. This was rejected because:

- Flat namespace — no protection against collisions between unrelated interfaces
- No semver — version matching was exact string comparison
- Mixed abstraction levels — `rfc:`, `topic:`, `tool:`, `llm:`, `uplink:`
  prefixes conflated interface contracts with implementation details

## Why not Cargo-style `[dependencies]`?

Considered `[dependencies]` with `provides`/`requires` as a nod to Cargo
conventions. Rejected because Cargo dependencies are crate-level (code
dependencies), while our declarations are interface-level (capability
dependencies). `imports`/`exports` better communicates the semantics.

## Why TOML table namespacing?

`[imports.astrid]` uses TOML's native nesting. The table name IS the namespace.
No additional syntax, no string parsing, no escape characters.

## Why WIT instead of JSON Schema or protobuf?

WIT is the WASM ecosystem's interface definition language. Since capsules are
WASM modules, WIT is the natural fit. Future `wit-bindgen` integration gives
capsule authors compile-time type safety. JSON Schema describes shapes but not
function signatures. Protobuf requires a compilation step and doesn't integrate
with the WASM component model.

## Why export conflict detection instead of `supersedes`?

A `supersedes = "old-capsule-name"` field was implemented and rejected.
Supersedes is name-based — capsule B must know capsule A's exact name.
Third-party capsules can't know what the user has installed. Export conflict
detection derives the conflict from interface data the system already has,
following the Nix model where the system figures it out from declarations.

## Impact of not standardizing

Without typed interface declarations, Astrid capsules are just WASM binaries
that publish to topics. There is no dependency graph, no safe removal, no
substitutability, no boot validation. The system is a message bus, not a
runtime.

# Prior art
[prior-art]: #prior-art

- **Cargo** (`Cargo.toml`): `[dependencies]` with semver version requirements.
  Cargo's flat crate namespace is a known limitation that Astrid avoids via
  TOML table namespacing.

- **Nix**: Content-addressed store, no `Replaces`/`Conflicts`/`Obsoletes`.
  Conflicts are detected from capabilities, not names. The architectural
  property: immutable content-addressed storage + declarative configuration =
  no mutable global namespace. Astrid's `bin/` store follows this model.

- **Debian** (`dpkg`): `Provides` + `Conflicts` + `Replaces` — three
  orthogonal primitives for package replacement. More expressive than
  `supersedes` but requires capsules to name each other.

- **RPM**: `Obsoletes` — single field for package succession. Name-based,
  same limitation as `supersedes`.

- **WIT** (WASM Interface Types): Component model interface definition.
  Used by `wasmtime`, `jco`, `wit-bindgen`. Astrid uses WIT as the spec
  format without compiling components (for now).

- **Bevy ECS**: System ordering with run conditions. Systems can prevent
  later systems from running. Direct inspiration for the interceptor chain
  with short-circuit semantics.

- **Express.js**: `next()` middleware pattern. Not calling `next()` halts
  the chain. Interceptor `Deny` and `Final` are the equivalent.

- **Linux netfilter**: `NF_ACCEPT`/`NF_DROP`/`NF_QUEUE` verdicts at each
  hook point. The interceptor chain's Continue/Final/Deny maps directly.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should boot validation hard-fail (refuse to boot) on unsatisfied required
  imports? Currently it logs errors but proceeds. Pre-1.0 leniency vs
  fail-secure.
- Should the interceptor chain support payload modification across capsules?
  Currently `Continue` can return modified bytes, but the semantics of what
  "modified" means for IPC payloads are undefined.
- Should export conflict detection consider semver compatibility? Currently
  any overlap on `(namespace, name)` is a conflict. Two capsules exporting
  `session 1.0.0` and `session 2.0.0` could coexist if consumers specify
  version requirements.

# Future possibilities
[future-possibilities]: #future-possibilities

- **`wit-bindgen` integration.** SDK generates typed Rust structs from WIT
  files. Capsule authors get compile-time type safety. The bus carries typed
  payloads instead of JSON.
- **Interface-level mutual exclusion.** `[conflicts.astrid] session = "*"` —
  "I'm the only session provider, deactivate any other." Replaces the
  user-facing prompt with a declarative constraint.
- **Runtime conformance testing.** Generate test harnesses from WIT specs
  that verify a capsule actually implements the interface it claims to export.
- **Versioned interceptor chains.** Different chain semantics per interface
  version — v1 events use fire-all, v2 events use middleware chain.
- **Cross-principal interface scoping.** Per-principal interface instances
  where each principal gets their own session provider.
