- Feature Name: `host_abi`
- Start Date: 2026-03-22
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#573](https://github.com/unicity-astrid/astrid/issues/573)

# Summary
[summary]: #summary

Define the host ABI — the syscall-like interface between the Astrid kernel and
WASM capsule guests. 55 host functions across 13 domain interfaces, plus 4
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

## Host interfaces

The canonical WIT spec lives at
[`core/wit/astrid-capsule.wit`](https://github.com/unicity-astrid/astrid/blob/main/wit/astrid-capsule.wit).
This section summarizes each interface.

### `types` — Shared types

Common types used across interfaces: `log-level` enum, `key-value-pair` record,
`capsule-context` and `capsule-result` records for hook execution.

### `fs` — Virtual filesystem (7 functions)

| Function | Description | Capability |
|---|---|---|
| `read` | Read file contents | `fs_read` |
| `write` | Write file contents | `fs_write` |
| `readdir` | List directory entries | `fs_read` |
| `stat` | File metadata (size, type, mtime) | `fs_read` |
| `mkdir` | Create directory | `fs_write` |
| `remove` | Delete file | `fs_write` |
| `exists` | Check if path exists | `fs_read` |

VFS paths use scheme prefixes: `workspace://` (project sandbox), `home://`
(principal home), `tmp://` (per-principal temp). The security gate resolves
schemes to physical paths and enforces capability boundaries.

### `ipc` — Inter-process communication (3 functions)

| Function | Description | Capability |
|---|---|---|
| `publish` | Publish message to IPC bus | `ipc_publish` |
| `subscribe` | Subscribe to topic pattern | `ipc_subscribe` |
| `recv` | Receive next message from subscription | `ipc_subscribe` |

Topic patterns use dot-separated segments with single-segment wildcards (`*`).
Messages carry `IpcPayload` variants (LLM request, tool result, custom JSON,
etc.) and are assigned monotonic sequence numbers at publish time.

### `uplink` — Frontend connection (3 functions)

| Function | Description | Capability |
|---|---|---|
| `send` | Send response to connected client | `uplink` |
| `recv` | Receive input from client | `uplink` |
| `ready` | Signal that the capsule is ready for input | `uplink` |

Only capsules with `uplink = true` capability can use these functions.

### `kv` — Key-value store (3 functions)

| Function | Description | Capability |
|---|---|---|
| `get` | Read value by key | (always allowed) |
| `set` | Write value by key | (always allowed) |
| `delete` | Remove key | (always allowed) |

KV is always available — no capability needed. Namespace is automatically
scoped: `{principal}:capsule:{capsule_name}`. Capsules cannot access each
other's KV data.

### `net` — Raw TCP networking (5 functions)

| Function | Description | Capability |
|---|---|---|
| `connect` | Open TCP connection | `net` |
| `read` | Read from stream | `net` |
| `write` | Write to stream | `net` |
| `close` | Close connection | `net` |
| `dns-resolve` | DNS lookup | `net` |

### `http` — HTTP client (4 functions)

| Function | Description | Capability |
|---|---|---|
| `fetch` | Blocking HTTP request | `net` |
| `stream-start` | Begin streaming HTTP request (SSE) | `net` |
| `stream-read` | Read next chunk from stream | `net` |
| `stream-close` | Close streaming connection | `net` |

### `sys` — System operations (8 functions)

| Function | Description | Capability |
|---|---|---|
| `log` | Structured logging with level | (always allowed) |
| `get-config` | Read capsule configuration | (always allowed) |
| `get-env` | Read environment variable | (always allowed) |
| `get-caller` | Get calling principal info | (always allowed) |
| `get-time` | Current UTC timestamp | (always allowed) |
| `random-bytes` | Cryptographic random bytes | (always allowed) |
| `trigger-hook` | Fan-out hook to matching interceptors | (always allowed) |
| `sleep` | Suspend execution for duration | (always allowed) |

### `cron` — Scheduled tasks (3 functions)

| Function | Description | Capability |
|---|---|---|
| `schedule` | Register a recurring task | `cron` |
| `cancel` | Cancel a scheduled task | `cron` |
| `list` | List active scheduled tasks | `cron` |

### `process` — Host process spawning (5 functions)

| Function | Description | Capability |
|---|---|---|
| `spawn` | Run a command and wait for exit | `host_process` |
| `spawn-background` | Start a background process | `host_process` |
| `read-logs` | Read buffered output from background process | `host_process` |
| `kill` | Terminate a background process | `host_process` |
| `list-processes` | List active background processes | `host_process` |

All processes run inside the platform sandbox (Seatbelt on macOS, bwrap on
Linux). The `host_process` capability lists allowed program names.

### `elicit` — Interactive user input (3 functions)

| Function | Description | Capability |
|---|---|---|
| `prompt` | Request text input from user | `uplink` |
| `select` | Present selection choices | `uplink` |
| `confirm` | Yes/no confirmation | `uplink` |

### `approval` — Human-in-the-loop gates (2 functions)

| Function | Description | Capability |
|---|---|---|
| `request` | Request human approval for an action | (always allowed) |
| `check` | Check if an action is pre-approved | (always allowed) |

### `identity` — User identity (4 functions)

| Function | Description | Capability |
|---|---|---|
| `create-user` | Create a new Astrid user | `identity` |
| `resolve` | Look up user by platform link | `identity` |
| `link` | Link a platform identity to an Astrid user | `identity` |
| `get-user` | Get user details by ID | `identity` |

## Guest exports

Capsules export up to 4 entry points:

| Export | Description | Required |
|---|---|---|
| `astrid-hook-trigger` | Interceptor handler — receives action + payload, returns `InterceptResult` bytes | No |
| `run` | Background task entry point — capsules with run loops (IPC subscribers) | No |
| `astrid-install` | Called once after first installation | No |
| `astrid-upgrade` | Called after version upgrade (receives previous version) | No |

## Error handling

Host functions return errors as JSON: `{ "error": "message" }`. The SDK
converts these to `Result<T, SysError>`. Capability violations include the
missing capability name in the error message.

# Drawbacks
[drawbacks]: #drawbacks

- **Large surface area.** 55 functions is a lot to maintain stable. Each one
  is a compatibility commitment.
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

- Should the ABI version be semver-independent from the kernel version?
  Currently they share the same version number.
- Should `astrid_system_stats` be added for runtime metrics (per-capsule
  memory, invocation counts, event bus throughput)? Planned for v0.6.0.
- Should there be a `capabilities` host function that lets a capsule query
  its own granted capabilities at runtime?

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
