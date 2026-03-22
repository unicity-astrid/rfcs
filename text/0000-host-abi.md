- Feature Name: `host_abi`
- Start Date: 2026-03-22
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#573](https://github.com/unicity-astrid/astrid/issues/573)

# Summary
[summary]: #summary

Define the host ABI — the syscall-like interface between the Astrid kernel and
WASM capsule guests. 51 host functions across 12 domain interfaces, plus 4
guest exports. All operations are capability-gated, audited, and per-principal
scoped.

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
┌─────────────────────────────────────┐
│ Capsule (WASM guest)                │
│                                     │
│  SDK: astrid_sdk::fs::read_file()   │
│         │                           │
│         ▼                           │
│  astrid-sys: astrid_fs_read()       │  ← FFI boundary
├─────────────────────────────────────┤
│  Host ABI (this spec)               │  ← Kernel enforces here
│         │                           │
│         ▼                           │
│  Capability check → VFS resolve →   │
│  Sandbox boundary → Audit log →     │
│  Actual I/O                         │
└─────────────────────────────────────┘
```

The SDK wraps host functions in ergonomic Rust APIs. `astrid-sys` is the raw
FFI layer. The host ABI defines what happens at the boundary.

## Wire format

The current transport is Extism (not Component Model). All structured arguments
are passed as JSON-encoded byte buffers, and all structured returns are
JSON-encoded byte buffers. The WIT types in the canonical spec describe the
*logical* contract. Once the kernel migrates to the WASM Component Model, these
become actual typed parameters.

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

The authoritative function-level specification is the WIT file:
[`core/wit/astrid-capsule.wit`](https://github.com/unicity-astrid/astrid/blob/main/wit/astrid-capsule.wit)

This RFC documents the design rationale, capability model, and scoping
semantics. The WIT file is the reference for function signatures and types.
If this RFC and the WIT file disagree, the WIT file is correct.

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

Host functions return errors as JSON: `{ "error": "message" }`. The SDK
converts these to `Result<T, SysError>`. Capability violations include the
missing capability name in the error message.

# Drawbacks
[drawbacks]: #drawbacks

- **Large surface area.** 51 host functions is a lot to maintain stable. Each
  one is a compatibility commitment.
- **JSON wire format overhead.** Serializing/deserializing JSON for every
  syscall is measurably slower than Component Model typed parameters. Acceptable
  pre-1.0; the migration path (WS-8) is planned.
- **No versioning on individual functions.** The package version (`0.1.0`)
  covers the entire ABI. Adding a function is a minor bump; changing a
  function signature is a major bump.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why WIT as the spec format?

WIT is the WASM ecosystem's canonical interface definition language. When
Astrid migrates to the Component Model, the WIT spec becomes the actual
import/export declarations — no rewrite needed.

## Why not fewer, coarser interfaces?

The original design had 7 functions in a single `host` interface. This was
split into 13 domain interfaces because:
- Capability gating maps to interfaces (grant `fs` without granting `net`)
- Documentation is clearer per domain
- Future SDK can expose per-domain modules

## Why JSON and not protobuf or msgpack?

JSON is human-readable (debugging), universally supported (every SDK language),
and matches the IPC bus payload format. The performance cost is acceptable for
the current scale. The Component Model migration eliminates serialization
entirely.

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

- **Extism**: Plugin framework providing the current host function transport.
  Astrid builds on Extism's plugin model but defines its own semantic layer
  (capabilities, audit, principal scoping) on top.

- **Envoy WASM ABI**: Host functions for proxy filters (get/set headers,
  send HTTP, log). Similar pattern: domain-specific host APIs for sandboxed
  extensions.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **ABI versioning independence.** Should the ABI version be semver-independent
  from the kernel version? Currently the WIT package version (`0.1.0`) and the
  kernel version (`0.5.0`) are decoupled but informally linked. A stable ABI
  version would let capsule authors target "ABI 1.0" regardless of which kernel
  version implements it. The counter-argument: two version numbers is confusing
  when there's only one implementation.

- **Capability introspection depth.** `check-capsule-capability` exists but is
  limited. Should capsules be able to query the full capability set of OTHER
  capsules? This enables a system capsule to display "what can each capsule do"
  but leaks capability information across the sandbox boundary.

- **Host function deprecation path.** When a host function needs to change
  signature (e.g., adding a parameter), how is backward compatibility handled?
  Options: versioned function names (`fs-read-v2`), optional parameters via
  JSON, or a clean break with the Component Model migration.

- **Audit chain integration.** Which host functions should produce audit
  entries? Currently logging and approval are audited. Should every `fs_write`
  be audited? Every `ipc_publish`? The audit chain grows linearly with calls —
  full auditing could be expensive for high-throughput capsules.

- **Resource limits.** Should host functions enforce resource limits (max file
  size on `write-file`, max message size on `ipc-publish`, max KV value size)?
  Currently unbounded — a capsule can write arbitrarily large values.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Component Model migration.** Replace Extism transport with native WASM
  Component Model imports/exports. WIT spec becomes the actual ABI, not just
  documentation.
- **`astrid_system_stats` host function.** Runtime observability for the system
  capsule — per-capsule WASM heap, invocation counts, event bus metrics.
- **Capability delegation.** A capsule grants a subset of its capabilities to
  a child capsule it spawns. Recursive restriction — children can only get
  *more restricted*, never *more permissive*.
- **Batch KV operations.** `kv_get_many`, `kv_set_many` for reducing
  round-trips in capsules that manage complex state.
- **File watching.** `fs_watch` for capsules that need to react to workspace
  file changes.
