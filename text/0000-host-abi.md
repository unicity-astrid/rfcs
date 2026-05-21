- Feature Name: `host_abi`
- Start Date: 2026-03-22
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issues: [astrid#573](https://github.com/unicity-astrid/astrid/issues/573), [astrid#750](https://github.com/unicity-astrid/astrid/issues/750)

# Summary
[summary]: #summary

Define the host ABI — the syscall-like interface between the Astrid kernel and
WASM capsule guests. 51 host functions across 12 domain interfaces, plus 4
guest exports. All operations are capability-gated, audited, and per-principal
scoped.

Pre-1.0, the ABI also adopts an evolution discipline to make post-1.0 changes
non-fatal for third-party capsules: the monolithic `astrid:capsule@0.1.0`
package splits into per-domain packages (`astrid:ipc@1.0.0`, `astrid:net@1.0.0`,
…) plus a minimal `astrid:core@1.0.0`; the kernel registers every supported
`(package, version)` pair into the wasmtime Component Model linker explicitly;
and once a WIT file is published it is immutable forever — shape changes ship
as new files at new versions.

# Motivation
[motivation]: #motivation

The host ABI is the most critical contract surface in Astrid. Every capsule —
whether it handles LLM requests, manages sessions, or runs shell commands —
interacts with the kernel exclusively through these functions. A capsule cannot
make a system call, open a file, or send a network request without going
through the host ABI.

This is by design. The WASM sandbox provides memory isolation, but the host ABI
provides *semantic* isolation. A capsule can only do what its capabilities
allow, and every action is recorded in the audit chain.

Formalizing the host ABI as an RFC ensures:

1. **SDK authors** can implement language bindings (Rust, Python, Go) against
   a stable spec, not against Rust function signatures that change between
   releases.
2. **Capsule authors** can understand what operations are available without
   reading kernel source code.
3. **Security auditors** can review the complete privilege surface in one
   document.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## Architecture

```
┌──────────────────────────────────────────┐
│ Capsule (WASM guest)                     │
│                                          │
│  SDK: astrid_sdk::fs::read_file()        │
│         │                                │
│         ▼                                │
│  WIT-bindgen import: astrid:fs/host.read │  ← Component Model boundary
├──────────────────────────────────────────┤
│  Host ABI (this spec)                    │  ← Kernel enforces here
│         │                                │
│         ▼                                │
│  Capability check → VFS resolve →        │
│  Sandbox boundary → Audit log →          │
│  Actual I/O                              │
└──────────────────────────────────────────┘
```

The SDK wraps WIT-bindgen-generated imports in ergonomic Rust APIs. The
Component Model boundary is typed end-to-end — there is no separate FFI shim
layer, and there is no serialization at the call site. The host ABI defines
what happens once a call crosses the linker.

## Wire format

The transport is the WASM Component Model. Arguments and returns are typed
according to the WIT spec — records, lists, options, and `result<T, E>` are
passed natively across the host/guest boundary by wasmtime, with no
serialization at the call site. The WIT files are the actual import/export
declarations, not merely documentation.

## Capability gating

Every host function checks the calling capsule's declared capabilities before
executing. A capsule without `fs_read` capability cannot call `astrid_fs_read`.
Violations are logged to the audit chain and return an error to the guest.

## Per-principal scoping

Host functions that access stateful resources (KV, filesystem, logging) are
automatically scoped to the calling principal. A capsule handling a request
from user "alice" reads from alice's KV namespace and writes to alice's log
directory, transparently.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Canonical spec

The authoritative function-level specification lives in the
[`unicity-astrid/wit`](https://github.com/unicity-astrid/wit) repository as a
set of per-domain WIT files (`interfaces/<name>@<version>.wit`). The kernel
vendors a snapshot at `core/wit/` for its build, but the canonical source is
the WIT repo.

This RFC documents the design rationale, capability model, scoping semantics,
and evolution discipline. The WIT files are the reference for function
signatures and types. If this RFC and a WIT file disagree, the WIT file is
correct.

## Host interfaces

51 host functions organized into 12 domain interfaces:

| Interface | Functions | Capability | Purpose |
|---|---|---|---|
| `fs` | 7 | `fs_read`, `fs_write` | Virtual filesystem (workspace://, home://, tmp://) |
| `ipc` | 6 | `ipc_publish`, `ipc_subscribe` | IPC event bus: publish, subscribe, receive |
| `uplink` | 2 | `uplink` | Frontend connection registration and response sending |
| `kv` | 5 | (ungated) | Per-capsule key-value store, auto-scoped per principal |
| `net` | 6 | `net` | Unix socket I/O for capsule-to-daemon communication |
| `http` | 4 | `net` | HTTP client with streaming (SSE) support |
| `sys` | 7 | (ungated) | Logging, config, time, hooks, capability introspection |
| `cron` | 2 | `cron` | Scheduled recurring tasks |
| `process` | 4 | `host_process` | Sandboxed host process spawning (Seatbelt/bwrap) |
| `elicit` | 2 | `uplink` | Interactive user input (prompts, selections) |
| `approval` | 1 | (ungated) | Human-in-the-loop approval gates |
| `identity` | 5 | `identity` | User identity CRUD and platform linking |

A 13th block, `types`, defines shared types (`log-level`, `key-value-pair`,
`capsule-context`, `capsule-result`) used across interfaces. It has no functions.

### Capability model

Each host function checks the calling capsule's declared capabilities:

- **Gated:** `fs_read`, `fs_write`, `ipc_publish`, `ipc_subscribe`, `uplink`,
  `net`, `cron`, `host_process`, `identity`. Capsules must declare these in
  `[capabilities]` in Capsule.toml.
- **Ungated:** `kv`, `sys`, `approval`. Always available. KV is safe because
  it's namespace-scoped per capsule and principal. Sys functions are read-only
  or side-effect-free. Approval is a request, not an action.

Violations return an error to the guest and are logged to the audit chain.

### VFS scheme resolution

The `fs` interface resolves paths through VFS schemes:

| Scheme | Resolves to | Capability |
|---|---|---|
| `workspace://` | Project sandbox root (CWD) | `fs_read` / `fs_write` |
| `home://` | `~/.astrid/home/{principal}/` | `fs_read` / `fs_write` |
| `tmp://` | `~/.astrid/home/{principal}/.local/tmp/` | `fs_write` |

The security gate resolves schemes to physical paths at capsule load time.
Cross-scheme access is denied. A capsule with `fs_read = ["workspace://"]`
cannot read `home://`.

### Per-principal KV scoping

KV namespace: `{principal}:capsule:{capsule_name}`. The principal is resolved
from the invocation context (IPC message principal field), not the capsule's
static configuration. This means the same capsule serves different KV
namespaces depending on who is calling — transparent to the capsule author.

## Guest exports

Capsules export up to 4 entry points:

| Export | Description | Required |
|---|---|---|
| `astrid-hook-trigger` | Interceptor handler — receives action + payload, returns `InterceptResult` bytes (see [interceptor chain RFC](0000-interceptor-chain.md)) | No |
| `run` | Background task entry point — capsules with run loops (IPC subscribers) | No |
| `astrid-install` | Called once after first installation — setup KV state, validate config | No |
| `astrid-upgrade` | Called after version upgrade — receives previous version for migrations | No |

Capsules without `run` are "on-demand" — they only execute when an interceptor
or tool is invoked. Capsules with `run` start a background task that subscribes
to IPC topics and processes events in a loop.

## Error handling

Host functions that can fail return `result<T, string>` natively through the
Component Model. The error string describes the failure; the SDK converts it
to `Result<T, SysError>`. Capability violations include the missing capability
name in the error message. Unrecoverable errors (lock poisoning, memory
exhaustion) trap — those represent kernel invariant violations, not
guest-recoverable conditions.

## ABI evolution discipline

The Component Model linker enforces structural typing on imports. A
record-field add, a function add, or any other shape change in a published
WIT package makes every capsule built against the prior shape fail to
instantiate with errors of the form `component imports instance
'astrid:capsule/ipc@0.1.0', but a matching implementation was not found in
the linker`. This has already been observed concretely against the current
monolithic `astrid:capsule@0.1.0` package: additive-looking changes (adding
`principal: option<string>` to `ipc-message`, adding 14 net functions) detonate
every capsule built before the change, because the single mega-package means
any edit anywhere in the file is a shape change for everyone importing it.

Pre-1.0, "rebuild the whole ecosystem on every change" is the recovery path.
Post-1.0 it is not: third-party capsule authors who shipped six months ago
and have not touched their code must keep loading.

The ABI is governed by three coordinated rules. Each is load-bearing — landing
one without the others is wasted work.

### Rule 1: per-domain packages

The pre-1.0 monolithic `astrid:capsule@0.1.0` package containing 12 interfaces
is replaced by one package per domain:

```
astrid:ipc@1.0.0
astrid:net@1.0.0
astrid:kv@1.0.0
astrid:http@1.0.0
astrid:fs@1.0.0
astrid:process@1.0.0
astrid:sys@1.0.0
astrid:cron@1.0.0
astrid:elicit@1.0.0
astrid:approval@1.0.0
astrid:uplink@1.0.0
astrid:identity@1.0.0
```

Plus a minimal **`astrid:core@1.0.0`** for truly cross-cutting types only
(`principal`, `caller-context`, common error shapes). This package is kept as
small as humanly possible; every type that lives in it ripples across every
interface that imports it, so each entry is justified individually.

Each domain package owns its own types. `astrid:ipc/types` (`ipc-envelope`,
`ipc-message`) lives in `astrid:ipc`, not in a shared types module. Per-domain
type ownership isolates per-domain evolution: bumping `astrid:net` does not
recompile anything that only imports `astrid:kv`.

Capsules opt into exactly the subset they need:

```wit
world my-capsule {
    import astrid:ipc/host@1.0.0;
    import astrid:kv/host@1.0.0;
    // not net, not http — capsule does not use them
}
```

A kv-only capsule is unaffected when `astrid:net` adds a function.

### Rule 2: multi-version kernel registration

wasmtime's Component Model has no implicit version negotiation. A version is
either registered into the linker or it is not. The kernel therefore
explicitly registers every `(package, version)` pair it supports:

```rust
// pseudocode
bindings::ipc_v1_0::add_to_linker(&mut linker, host_v1_0_handler)?;
bindings::ipc_v1_1::add_to_linker(&mut linker, host_v1_1_handler)?;
bindings::ipc_v2_0::add_to_linker(&mut linker, host_v2_0_handler)?;
// per package, per supported version
```

This requires:

- **Build-time codegen:** one binding module per `(package, version)` pair.
- **Host-side shims:** handlers that translate between version shapes when
  shared types evolved. For example, if `ipc-message` gains a `principal`
  field in v1.1, the v1.0 handler ignores it when emitting to v1.0 capsules
  and synthesizes it from caller-context when receiving from v1.0 capsules.
  Shims are written once per evolution, not once per capsule.
- **Support window per package:** the kernel publishes which versions it
  loads (e.g. `astrid:ipc@{1.0.0, 1.1.0, 2.0.0}` simultaneously).
- **Deprecation policy:** when the kernel drops a version from the support
  window, capsules built against it stop loading. Drop dates are announced in
  advance, and capsules importing a soon-to-be-dropped version produce a
  load-time warning naming the removal date.

### Rule 3: frozen WIT files per version

Once `astrid:ipc@1.0.0` ships, the file at `interfaces/ipc@1.0.0.wit` is
**immutable forever**. New shapes get a new file:

```
interfaces/
  ipc@1.0.0.wit           # frozen
  ipc@1.1.0.wit           # frozen
  ipc@2.0.0.wit           # current
```

When a shape change is needed: copy the latest frozen file to a new version
path, modify, register the new version in the kernel, leave the old files
alone. The current pattern of editing a WIT file in place ends.

This is enforced by a CI lint (`scripts/lint-wit-immutability.sh`) in the
`unicity-astrid/wit` repository: any PR that touches a file matching
`*@X.Y.Z.wit` where that version is referenced in a kernel release manifest
fails the build. New shapes = new file, with no exceptions.

### Scope boundary: capsule-to-kernel vs capsule-to-capsule

This evolution discipline governs the **kernel ↔ capsule** boundary — the WIT
contract enforced by the wasmtime linker. It does **not** govern
**capsule ↔ capsule** communication, which travels over the IPC bus as
typed events on string topics (e.g. `tool.v1.execute.*`). The bus is not WIT-
typed; capsule-to-capsule shape changes manifest at runtime as deserialization
errors or unmatched subscriptions, not at load time as linker errors.

Bus-event evolution is governed separately by the topic-versioning convention
(`*.v1.*` → `*.v2.*`, with producers keeping the prior topic alive until
consumers migrate). Third-party capsule authors are free to add new event
handlers without affecting other capsules; only renames or non-additive
payload changes break consumers, and only at runtime.

# Drawbacks
[drawbacks]: #drawbacks

- **Large surface area.** 51 host functions is a lot to maintain stable. Each
  one is a compatibility commitment, and per-domain splitting multiplies the
  number of independently-versioned packages the kernel must track.
- **Host-side shim code grows with versions.** Every evolution of a shared
  type requires a translation shim in the kernel for each older version still
  in the support window. The maintenance load scales with how many versions
  the kernel pledges to support concurrently — bounded by the support window
  policy, but non-zero.
- **Codegen volume scales with the support matrix.** One binding module per
  `(package, version)` pair, registered explicitly into the linker, means
  build time and binary size grow with the number of supported versions
  rather than being constant. Mitigated by the support-window policy, but it
  is a real cost.
- **Per-domain ownership of cross-cutting types is awkward.** `principal` and
  `caller-context` either live in a shared `astrid:core` package (and ripple
  on every cross-cutting evolution) or are duplicated per interface (more
  surface to keep in sync). Neither is free; the RFC picks the smaller-`core`
  trade and admits the cost.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why WIT as the spec format?

WIT is the WASM ecosystem's canonical interface definition language. The
Component Model uses WIT directly as the import/export declarations — the
spec and the implementation are the same artefact, by construction.

## Why not fewer, coarser interfaces?

The original design had 7 functions in a single `host` interface. This was
split into 13 domain interfaces because:
- Capability gating maps to interfaces (grant `fs` without granting `net`)
- Documentation is clearer per domain
- SDK exposes per-domain modules
- Per-domain ABI versioning isolates evolution blast radius (see "ABI
  evolution discipline" above)

## Why per-domain packages instead of a single versioned `astrid:capsule`?

A single package (`astrid:capsule@X.Y.Z`) means every interface inside it
shares one version. Any change to any interface — even one a given capsule
does not import — bumps the package version, and the linker rejects the
capsule because the imported package version no longer matches. Per-domain
packages decouple this: bumping `astrid:net` has no effect on a capsule that
only imports `astrid:kv`. The cost is more package versions to track; the
benefit is that the kernel can evolve one domain at a time without recompiling
the ecosystem.

## Why are KV and sys functions ungated?

KV is scoped per-capsule and per-principal — a capsule can only access its own
data. System functions (logging, config, time) are read-only or side-effect-free.
Gating these would add friction without security benefit.

# Prior art
[prior-art]: #prior-art

- **POSIX**: The syscall interface between kernel and user-space. 400+ syscalls
  organized by domain (file, process, memory, network). Astrid's host ABI is
  the capsule equivalent.

- **WASI** (WebAssembly System Interface): Standardized host functions for WASM
  modules. `wasi_snapshot_preview1` provides filesystem, clock, random. Astrid
  extends beyond WASI with IPC, approval gates, identity, and capsule-specific
  operations.

- **wasmtime Component Model**: The actual host runtime. wasmtime's CM linker
  enforces structural typing per `(package, version)` pair, which is the
  mechanism this RFC's evolution discipline plays against. Astrid's per-domain
  package split is a direct response to CM's strict typing — coarser packages
  amplify the blast radius of any shape change, finer packages contain it.

- **Envoy WASM ABI**: Host functions for proxy filters (get/set headers,
  send HTTP, log). Similar pattern: domain-specific host APIs for sandboxed
  extensions.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Minimal `astrid:core` vs zero-shared types.** The RFC currently picks a
  minimal `astrid:core@1.0.0` carrying `principal`, `caller-context`, and
  common error shapes. The strictly purer alternative is zero shared types:
  every interface defines its own `principal`, accept the duplication, accept
  the loss of cross-domain ergonomics, gain full per-domain evolutionary
  independence. The trade-off pivots on how often cross-cutting types actually
  evolve in practice — if they prove churny, zero-shared wins; if they prove
  stable, the small-`core` design pays for itself in author ergonomics.

- **Push `principal` out of payload onto a resource handle.** A resource-typed
  caller-context (host-owned, opaque handle, accessor methods) would let the
  host evolve principal representation without changing any WIT payload at
  all. If this works, `astrid:core` shrinks further or vanishes entirely.
  Worth a design pass before 1.0; not blocking, because the record-based ABI
  works as long as the per-domain split + frozen-file rules hold.

- **World naming convention.** Per-domain packages can expose `astrid:ipc/host`
  vs `astrid:ipc/guest` worlds for the two directions, or a single
  `astrid:ipc` world that imports/exports as needed. The first matches
  wasmtime CM idiom and makes the split between host-provided imports and
  guest-provided exports explicit; the second is terser. Worth checking
  against current wasmtime examples before committing.

- **Pre-1.0 cleanup approach for `astrid:capsule@0.1.0`.** Hard cut (delete
  the monolithic package, force every first-party capsule to migrate before
  1.0 ships) versus deprecated alias (keep it loadable for a release or two,
  log deprecation warnings). The RFC leans hard-cut: pre-1.0 we have license
  to break, post-1.0 we do not, and an alias means the new model has to
  coexist with the dead one in codegen forever. Decision deferred to the
  implementation PR.

- **Support-window length.** How many versions of each package does the
  kernel pledge to load simultaneously? Two? Three? Indefinite? Each extra
  version adds shim maintenance. A "N+2 minor, N-1 major" convention is one
  option; "current and previous major" is another. Decision deferred until
  the first post-1.0 minor bump, by which point we'll have real maintenance
  data.

- **Capability introspection depth.** `check-capsule-capability` exists but is
  limited. Should capsules be able to query the full capability set of OTHER
  capsules? This enables a system capsule to display "what can each capsule do"
  but leaks capability information across the sandbox boundary.

- **Audit chain integration.** Which host functions should produce audit
  entries? Currently logging and approval are audited. Should every `fs_write`
  be audited? Every `ipc_publish`? The audit chain grows linearly with calls —
  full auditing could be expensive for high-throughput capsules.

- **Resource limits.** Should host functions enforce resource limits (max file
  size on `write-file`, max message size on `ipc-publish`, max KV value size)?
  Currently unbounded — a capsule can write arbitrarily large values.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Resource-typed caller-context.** Replace the payload-carried `principal`
  field with a host-owned resource handle exposed via accessor methods. This
  removes a cross-cutting record type from `astrid:core` entirely, letting
  the host evolve principal representation without any WIT payload change.
  See the corresponding open question.
- **`astrid_system_stats` host function.** Runtime observability for the system
  capsule — per-capsule WASM heap, invocation counts, event bus metrics.
- **Capability delegation.** A capsule grants a subset of its capabilities to
  a child capsule it spawns. Recursive restriction — children can only get
  *more restricted*, never *more permissive*.
- **Batch KV operations.** `kv_get_many`, `kv_set_many` for reducing
  round-trips in capsules that manage complex state.
- **File watching.** `fs_watch` for capsules that need to react to workspace
  file changes.
