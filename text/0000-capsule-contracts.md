- Feature Name: `capsule_contracts`
- Start Date: 2026-03-20
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#550](https://github.com/unicity-astrid/astrid/issues/550)

# Summary
[summary]: #summary

Formalize the `[dependencies]` section in `Capsule.toml` and introduce
RFC-level contract identifiers (`rfc:<name>.v<N>`) as a new capability type.
This enables capsules to declare conformance to formalized interface
specifications, the kernel to validate dependency graphs at boot, and
`astrid capsule remove` to prevent breakage by refusing to remove sole
providers of required contracts.

# Motivation
[motivation]: #motivation

Astrid capsules already declare concrete capabilities via `provides` and
`requires` in `[dependencies]`, using prefixed identifiers:

- `topic:session.v1.response.get_messages` — IPC topic
- `tool:run_shell_command` — tool exposed to the LLM
- `llm:claude-3-5-sonnet` — LLM provider model
- `uplink:cli` — frontend connection

These work for wiring specific IPC channels and tools. But they operate at
the wrong level of abstraction for ecosystem-level questions:

- "Does this distro have an LLM provider?" — You would need to enumerate
  every possible `llm:*` identifier. No single capability says "I am an
  LLM provider."
- "Can I safely remove this capsule?" — You can check topic subscribers,
  but you cannot check whether this capsule is the only implementation of
  a contract that other capsules depend on.
- "Does this capsule conform to the session protocol?" — The capsule
  publishes `topic:session.v1.response.*`, but nothing says it implements
  the full session protocol as specified.

RFC-level contracts solve this. A capsule that declares
`provides = ["rfc:llm-provider.v1"]` is asserting conformance to RFC
`0000-llm-provider-protocol.md` — the full tool schema, IPC topic set,
error handling contract, and behavioral guarantees defined in that document.

This enables:

- **Distro-level validation.** `Distro.toml` can `requires = ["rfc:llm-provider.v1"]`
  and the resolver validates at install-time, before downloading anything.
- **Safe removal.** `astrid capsule remove` checks the dependency graph and
  blocks removal if the capsule is the sole provider of a required contract.
- **Substitutability.** Any capsule providing `rfc:llm-provider.v1` is a
  valid replacement for any other — the RFC defines the interface contract.
- **Dependency visualization.** `astrid capsule tree` renders the full
  provides/requires graph.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## Declaring contracts in Capsule.toml

The `[dependencies]` section in `Capsule.toml` has two fields:

```toml
[dependencies]
provides = [
    "rfc:llm-provider.v1",
    "topic:llm.v1.stream.anthropic",
    "tool:llm_generate",
]
requires = [
    "rfc:session-protocol.v1",
    "topic:session.v1.response.get_messages",
]
```

### `provides`

What this capsule offers to the system. Values are capability identifiers
with one of these prefixes:

| Prefix | Meaning | Example |
|--------|---------|---------|
| `rfc:` | Conformance to a formalized RFC specification | `rfc:llm-provider.v1` |
| `topic:` | IPC topic this capsule publishes on | `topic:llm.v1.stream.anthropic` |
| `tool:` | Tool exposed to the LLM agent | `tool:run_shell_command` |
| `llm:` | LLM provider model identifier | `llm:claude-3-5-sonnet` |
| `uplink:` | Frontend connection type | `uplink:cli` |

If `provides` is empty or absent, the kernel auto-derives it from the
capsule's `ipc_publish`, `[[tool]]`, `[[llm_provider]]`, and `[[uplink]]`
declarations. This auto-derivation covers `topic:`, `tool:`, `llm:`, and
`uplink:` prefixes only. `rfc:` contracts must always be declared explicitly
— they represent a deliberate assertion of conformance.

### `requires`

What this capsule needs from other capsules. At least one loaded capsule
must provide each listed capability before this capsule can boot.

Wildcards (`*`) are allowed in `requires` for pattern matching
(e.g. `topic:llm.v1.stream.*`), but not in `provides` (providers must be
exact).

### The `rfc:` prefix

The format is `rfc:<name>.v<version>`:

- `<name>` is the RFC's feature name in kebab-case (e.g. `llm-provider`,
  `session-protocol`, `tool-execution`)
- `<version>` is a positive integer, not semver — each version is an
  independent contract
- The identifier maps to a merged RFC document in `unicity-astrid/rfcs`
  (e.g. `text/NNNN-llm-provider-protocol.md`)

The RFC document is the contract specification. It defines tool schemas,
IPC topics, host function requirements, error handling, and behavioral
guarantees. A capsule that declares `provides = ["rfc:llm-provider.v1"]`
is asserting it implements everything in that RFC.

## Kernel boot validation

At boot, after loading all capsule manifests, the kernel builds a dependency
graph:

1. Collect all `provides` from every loaded capsule (including auto-derived).
2. For each capsule with `requires`, verify that every requirement is
   satisfied by at least one other capsule's `provides`.
3. If any requirement is unsatisfied, the kernel logs an error and refuses
   to boot that capsule.

Uplink capsules cannot declare `requires` — they load first, before any
other capsule type.

## Safe removal

`astrid capsule remove <name>` checks the dependency graph before removing:

1. Collect the capsule's `provides`.
2. For each provided capability, check if any other loaded capsule `requires`
   it.
3. If this capsule is the **sole provider** of a required capability, block
   removal with an error explaining which capsules depend on it.
4. `--force` bypasses this check.

Removal never silently breaks the dependency graph.

## Dependency tree

`astrid capsule tree` displays the provides/requires graph:

```
astrid-capsule-react
  requires: rfc:session-protocol.v1
    provided by: astrid-capsule-session
  requires: topic:session.v1.response.get_messages
    provided by: astrid-capsule-session

astrid-capsule-session
  provides: rfc:session-protocol.v1
  provides: topic:session.v1.response.get_messages
  provides: topic:session.v1.response.clear
  requires: (none)

astrid-capsule-anthropic
  provides: rfc:llm-provider.v1
  provides: tool:llm_generate
  requires: (none)
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Capability identifier format

All capability identifiers follow the format `<prefix>:<body>`:

| Prefix | Body format | Wildcards in provides | Wildcards in requires |
|--------|-------------|----------------------|----------------------|
| `rfc` | `<name>.v<N>` where `<name>` matches `^[a-z][a-z0-9-]*$` and `<N>` is a positive integer | No | No |
| `topic` | Dot-separated segments, each non-empty | No | Yes (`*` as final segment) |
| `tool` | Non-empty string | No | Yes (`*`) |
| `llm` | Non-empty string | No | Yes (`*`) |
| `uplink` | Non-empty string | No | No |

### Validation rules

A conforming parser must reject a `[dependencies]` section that violates:

1. Any identifier is empty or contains only whitespace.
2. Any identifier does not contain a `:` separator.
3. The prefix is not one of `rfc`, `topic`, `tool`, `llm`, `uplink`.
4. An `rfc:` identifier does not match `^rfc:[a-z][a-z0-9-]*\.v[1-9][0-9]*$`.
5. A `provides` entry contains a wildcard (`*`).
6. An uplink capsule declares `requires`.
7. A `topic:` body contains empty segments (e.g. `topic:foo..bar`).

## Auto-derivation of `provides`

When `dependencies.provides` is empty or absent, the kernel derives it:

| Source | Derived capability |
|--------|-------------------|
| `capabilities.ipc_publish` topics | `topic:{topic}` for each topic (wildcards stripped) |
| `[[tool]]` entries | `tool:{name}` for each tool |
| `[[llm_provider]]` entries | `llm:{id}` for each provider |
| `[[uplink]]` entries | `uplink:{name}` for each uplink |

Auto-derivation does **not** produce `rfc:` identifiers. RFC contract
conformance is always an explicit declaration — it cannot be inferred from
the capsule's structure because an RFC defines behavioral guarantees beyond
what the manifest can express.

If a capsule needs both auto-derived and explicit `provides`, it must list
all capabilities explicitly. Auto-derivation only runs when `provides` is
empty.

## Dependency resolution algorithm

At kernel boot:

```
let all_provides: HashMap<String, Vec<CapsuleName>> = collect_all_provides();

for capsule in loaded_capsules {
    for req in capsule.dependencies.requires {
        if req.contains('*') {
            // Wildcard match: at least one provider must match the pattern
            if !all_provides.keys().any(|p| wildcard_match(req, p)) {
                error!("unsatisfied requirement: {capsule} requires {req}");
                refuse_boot(capsule);
            }
        } else {
            // Exact match
            if !all_provides.contains_key(req) {
                error!("unsatisfied requirement: {capsule} requires {req}");
                refuse_boot(capsule);
            }
        }
    }
}
```

Capsule boot order is not affected — requirements are checked against the
full set of loaded manifests, not against already-booted capsules. All
manifests are loaded before any WASM execution begins.

## Removal safety check

```
fn can_remove(target: &Capsule, all_capsules: &[Capsule]) -> Result<(), RemovalBlocked> {
    let target_provides: HashSet<&str> = target.dependencies.provides.iter().collect();

    for cap in all_capsules {
        if cap.name == target.name { continue; }
        for req in &cap.dependencies.requires {
            if !target_provides.contains(req.as_str()) { continue; }
            // target provides something `cap` requires
            // check if any OTHER capsule also provides it
            let other_providers: Vec<_> = all_capsules.iter()
                .filter(|c| c.name != target.name && c.dependencies.provides.contains(req))
                .collect();
            if other_providers.is_empty() {
                return Err(RemovalBlocked {
                    capability: req.clone(),
                    dependent: cap.name.clone(),
                });
            }
        }
    }
    Ok(())
}
```

Binary cleanup: content-addressed WASM binaries in `bin/` are only removed
if no other capsule references the same BLAKE3 hash.

## Relationship to Distro.toml

`Distro.toml` has its own `provides` and `requires` fields that use the
same `rfc:<name>.v<N>` identifier format. The relationship:

| | Distro.toml | Capsule.toml |
|---|---|---|
| **When validated** | Install-time (before download) | Boot-time (after loading manifests) |
| **Scope** | Distro-level: "does the bundle include an LLM provider?" | Capsule-level: "does this capsule's dependency exist?" |
| **`provides`** | Declaration mirroring `Capsule.toml` for early validation | Authoritative source of truth |
| **`requires`** | At least one capsule must provide | At least one other capsule must provide |

If `Distro.toml` and `Capsule.toml` disagree on `provides`, `Capsule.toml`
is authoritative at runtime. `Distro.toml` is an early hint for install-time
validation.

# Drawbacks
[drawbacks]: #drawbacks

- **Two levels of contracts.** Concrete capabilities (`topic:`, `tool:`)
  and abstract contracts (`rfc:`) coexist in the same `provides`/`requires`
  fields. This could confuse capsule authors about which level to use.
  Mitigated by documentation: use `rfc:` for interface conformance, use
  concrete prefixes for specific wiring.

- **No conformance verification.** Declaring `provides = ["rfc:llm-provider.v1"]`
  is a trust assertion — the kernel does not verify that the capsule actually
  implements the RFC's tool schemas and IPC topics. A conformance test suite
  is future work.

- **All-or-nothing auto-derivation.** If a capsule needs one explicit
  `provides` entry plus auto-derived ones, it must list everything manually.
  This is a minor ergonomic cost for the simplicity of the derivation logic.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why `rfc:` as a prefix, not a separate field?** Keeping all capability
types in the same `provides`/`requires` lists means the dependency resolver
handles them uniformly. A separate `contracts` field would require parallel
validation logic and split the dependency graph into two disconnected pieces.

**Why not semantic versioning on contracts?** `rfc:llm-provider.v2` is not
a superset of `v1`. Each version is an independent contract. This avoids
the complexity of compatibility ranges and makes requirements unambiguous.
If v2 is backward-compatible with v1, the capsule declares both:
`provides = ["rfc:llm-provider.v1", "rfc:llm-provider.v2"]`.

**Why not use the RFC number as the identifier?** `rfc:0042.v1` is opaque.
`rfc:llm-provider.v1` is self-descriptive. The name maps to the RFC's
feature name, which maps to the filename (`text/0042-llm-provider-protocol.md`).
Human readability wins over compactness.

**Why block removal instead of cascading?** Cascading deletes (removing
dependents automatically) are dangerous in a runtime with persistent state.
A capsule might own KV data, audit entries, or active sessions. Blocking
with a clear error is safer. `--force` exists for operators who know what
they are doing.

**Why not resolve boot order from dependencies?** Capsule manifests are
loaded (parsed) before any WASM execution. The dependency check runs against
the full manifest set. Boot order (which capsule's WASM runs first) is
orthogonal — capsules communicate via async IPC, not synchronous calls.

# Prior art
[prior-art]: #prior-art

- **ERC-20 / EIP standards (Ethereum)** — The direct inspiration for
  `rfc:` identifiers. Each ERC number defines a contract interface. Any
  token contract implementing ERC-20 is substitutable. Astrid's `rfc:`
  prefix works the same way: the RFC IS the contract.

- **Debian virtual packages** — Packages declare `Provides: mail-transport-agent`.
  Multiple packages (postfix, sendmail, exim4) provide the same virtual
  package. `Depends: mail-transport-agent` is satisfied by any provider.
  Astrid's `rfc:` contracts are virtual capabilities in the same sense.

- **OSGi bundles (Java)** — Bundles declare `Export-Package` and
  `Import-Package` with version ranges. The OSGi resolver validates at
  deploy-time. More complex than Astrid's model (no version ranges on
  contracts).

- **systemd unit dependencies** — `Requires=`, `Wants=`, `Provides=`
  (via aliases). Units fail to start if hard dependencies are missing.
  Astrid's model is equivalent for `requires`.

- **Cargo features** — Feature flags that gate compilation. Not a runtime
  dependency system, but the naming convention (`feature = ["dep/feature"]`)
  influenced the prefixed identifier format.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Conformance testing.** Should the RFC repo include a test harness that
  verifies a capsule actually implements the tools, topics, and behaviors
  specified by an `rfc:` contract? Deferred to future work.
- **Contract deprecation.** When `rfc:llm-provider.v2` supersedes `v1`,
  should the kernel warn capsules still requiring `v1`? Deferred pending
  ecosystem maturity.
- **Soft requires.** Should there be a `wants` field (like systemd `Wants=`)
  for optional dependencies that enhance but don't gate boot? Currently
  not needed — capsules handle missing optional deps via IPC timeouts.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Conformance test suite** generated from RFC specifications. A capsule
  declares `provides = ["rfc:llm-provider.v1"]` and CI runs the test suite
  against it.
- **Contract deprecation warnings** when a loaded capsule requires a
  contract version that has been superseded.
- **Capability negotiation** — capsules query available providers at runtime
  via a host function, enabling dynamic behavior based on what's installed.
- **Contract composition** — an RFC contract can `extends` another, creating
  inheritance chains (e.g. `rfc:streaming-llm-provider.v1` extends
  `rfc:llm-provider.v1`).
- **Signed contract assertions** — a capsule's `provides` declaration is
  signed by the author's ed25519 key, enabling trust chains for contract
  conformance.
