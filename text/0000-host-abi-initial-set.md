- Feature Name: `host_abi_initial_set`
- Start Date: 2026-03-17
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

Catalogue the initial set of 48 host functions that form the `astrid-sys`
syscall table — the ABI boundary between the Astrid kernel and WASM capsules.
Each function is specified with its WASM signature, input/output JSON schemas,
behavioral semantics, security constraints, and resource limits.

# Motivation
[motivation]: #motivation

The host ABI is the most critical contract surface in Astrid. Every capsule
depends on it. Today the ABI exists only as Rust source code spread across
`astrid-sys` (raw FFI), `astrid-sdk` (typed wrappers), and the kernel's host
callback implementations. There is no single document a capsule author can
read to understand what the kernel provides, what the calling conventions are,
or what security invariants the kernel enforces.

This RFC serves as:

- The authoritative specification for the initial syscall table.
- A reference for capsule authors implementing in any language (not just Rust).
- A baseline against which future host ABI changes are measured.
- Documentation of security boundaries, resource limits, and error contracts.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## Calling convention

All host functions use the Extism host function ABI. Parameters and return
values are `Vec<u8>` — opaque byte buffers. Functions that return data use
Extism's memory allocation (the return value is an `I64` pointer into guest
memory). Functions that return nothing are void.

Most payloads are JSON (UTF-8). Binary payloads (file contents, raw IPC) are
passed as-is. The SDK provides typed wrappers; the wire format is always bytes.

## Error reporting

Host functions signal errors by setting the Extism error string via
`extism_pdk::set_error_bytes()`. On success the error string is empty. On
failure it contains a human-readable error message. The SDK maps this to
`Result<T, SysError>`.

Capsules must check for errors after every host call. The kernel does not
abort the guest on error — it returns control and expects the guest to handle
the failure.

## Capability gating

Some host functions are gated by capabilities declared in `Capsule.toml`.
A call to a gated function without the required capability fails with an
error. Capability checks happen at call-time (not at load-time), with two
exceptions noted below.

## Resource limits

The kernel enforces per-call and per-capsule resource limits to prevent
denial-of-service from malicious or buggy capsules:

| Resource | Limit |
|----------|-------|
| Guest payload (any single parameter) | 10 MB |
| File path length | 4 KB |
| Log message | 64 KB |
| KV key | 4 KB |
| IPC topic | 256 bytes |
| Approval action string | 256 bytes |
| Approval resource string | 1,024 bytes |
| Inbound uplink message | 1 MB |
| HTTP response body | Unbounded (caller's risk) |
| Active network streams per capsule | 8 |
| IPC recv timeout | 60,000 ms |
| Approval timeout | 60,000 ms |
| Elicit timeout | 120,000 ms |

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Notation

Each function is specified as:

```
astrid_function_name(param1: bytes, param2: bytes) -> bytes
astrid_void_function(param1: bytes, param2: bytes)
```

`-> bytes` means the function returns data via Extism memory allocation
(WASM `I64` return). No arrow means void return. All `bytes` parameters are
`Vec<u8>` in the Extism ABI.

---

## 1. File System (VFS)

Paths use the Astrid VFS scheme: `workspace://` for the workspace root,
`global://` for the global Astrid home. Physical paths are resolved by the
kernel with symlink escape detection — any path that escapes the capsule's
allowed boundary is rejected.

Gated by `capabilities.fs_read` and `capabilities.fs_write` in `Capsule.toml`.

### `astrid_read_file(path: bytes) -> bytes`

Read entire file contents.

- **Input:** UTF-8 path string.
- **Output:** Raw file bytes (binary-safe).
- **Errors:** Path does not exist, path escapes boundary, read permission
  denied.

### `astrid_write_file(path: bytes, content: bytes)`

Write bytes to a file. Overwrites if file exists, creates if it does not.

- **Input:** `path` — UTF-8 path string. `content` — raw bytes.
- **Errors:** Path escapes boundary, write permission denied, parent directory
  does not exist.

### `astrid_fs_exists(path: bytes) -> bytes`

Check if a path exists.

- **Input:** UTF-8 path string.
- **Output:** JSON boolean: `true` or `false`.

### `astrid_fs_stat(path: bytes) -> bytes`

Get file or directory metadata.

- **Input:** UTF-8 path string.
- **Output:** JSON object with stat fields (size, permissions, timestamps).

### `astrid_fs_readdir(path: bytes) -> bytes`

List directory entries.

- **Input:** UTF-8 path string.
- **Output:** JSON array of directory entry metadata objects.

### `astrid_fs_mkdir(path: bytes)`

Create a directory.

- **Input:** UTF-8 path string.
- **Errors:** Path escapes boundary, parent does not exist.

### `astrid_fs_unlink(path: bytes)`

Delete a file or directory.

- **Input:** UTF-8 path string.
- **Errors:** Path escapes boundary, does not exist.

---

## 2. Inter-Process Communication (IPC)

The IPC subsystem is a publish-subscribe event bus. Capsules publish messages
to topics and subscribe to topic patterns. The kernel enforces ACLs declared
in `capabilities.ipc_publish` and `capabilities.ipc_subscribe`.

### `astrid_ipc_publish(topic: bytes, payload: bytes)`

Publish a message to the event bus.

- **Input:** `topic` — UTF-8 topic string (max 256 bytes). `payload` — raw
  bytes (typically JSON).
- **Errors:** Topic exceeds 256 bytes, topic does not match any declared
  `ipc_publish` pattern.
- **Security:** Rate-limited. Topic length capped at 256 bytes.

### `astrid_ipc_subscribe(topic: bytes) -> bytes`

Subscribe to a topic pattern on the event bus.

- **Input:** UTF-8 topic pattern. Supports `*` wildcard matching one or more
  trailing segments.
- **Output:** Subscription handle ID (binary, used in poll/recv).
- **Errors:** Topic pattern does not match any declared `ipc_subscribe`
  ACL pattern.
- **Security:** ACL checked at subscription time. A pattern must match at
  least one declared `ipc_subscribe` pattern in the capsule's manifest.

### `astrid_ipc_unsubscribe(handle: bytes)`

Unsubscribe from a topic.

- **Input:** Subscription handle ID (from `astrid_ipc_subscribe`).
- **Errors:** Handle not found, handle is runtime-owned (interceptor
  auto-subscription — cannot be unsubscribed by the guest).

### `astrid_ipc_poll(handle: bytes) -> bytes`

Non-blocking check for pending messages.

- **Input:** Subscription handle ID.
- **Output:** JSON envelope:

```json
{
  "messages": [
    { "topic": "...", "payload": "...", "source_id": "..." }
  ],
  "dropped": 0,
  "lagged": 0
}
```

`messages` is empty if nothing is pending. `dropped` and `lagged` report
message loss from slow consumers.

### `astrid_ipc_recv(handle: bytes, timeout_ms: bytes) -> bytes`

Block until a message arrives or timeout.

- **Input:** `handle` — subscription handle ID. `timeout_ms` — UTF-8 decimal
  string, max 60,000.
- **Output:** Same JSON envelope as `astrid_ipc_poll`.
- **Behavior:** Returns empty `messages` on timeout. Respects capsule
  cancellation tokens. Timeout capped at 60,000 ms; values above are clamped.

### `astrid_get_interceptor_handles() -> bytes`

Query auto-subscribed interceptor handle mappings.

- **Output:** JSON array:

```json
[
  { "handle_id": 42, "action": "handle_prompt", "topic": "user.v1.prompt" }
]
```

Used by run-loop capsules to discover which handles correspond to which
interceptor actions declared in `Capsule.toml`.

---

## 3. Uplinks

Uplinks are direct connections to frontends (CLI, Discord, Telegram). They
allow capsules to receive messages from and send messages to platform users.

### `astrid_uplink_register(name: bytes, platform: bytes, profile: bytes) -> bytes`

Register an uplink connection.

- **Input:** `name` — uplink identifier. `platform` — platform string
  (e.g. `"cli"`, `"discord"`). `profile` — interaction profile: `"chat"`,
  `"interactive"`, `"notify"`, `"bridge"`, or `"receive_only"`.
- **Output:** Uplink UUID as UTF-8 string.

### `astrid_uplink_send(uplink_id: bytes, platform_user_id: bytes, content: bytes) -> bytes`

Send a message via a registered uplink.

- **Input:** `uplink_id` — UUID from registration. `platform_user_id` —
  target user on the platform. `content` — message bytes (max 1 MB).
- **Output:** JSON: `{"ok": true}` or `{"ok": false, "dropped": true}`.

---

## 4. Key-Value Store

Per-capsule persistent storage. Keys are scoped to the capsule — no
cross-capsule access.

### `astrid_kv_get(key: bytes) -> bytes`

Retrieve a value.

- **Input:** UTF-8 key string (max 4 KB).
- **Output:** Stored value bytes, or empty if key does not exist.

### `astrid_kv_set(key: bytes, value: bytes)`

Store a key-value pair.

- **Input:** `key` — UTF-8 string (max 4 KB). `value` — raw bytes.

### `astrid_kv_delete(key: bytes)`

Delete a key. Idempotent — deleting a nonexistent key is not an error.

- **Input:** UTF-8 key string.

### `astrid_kv_list_keys(prefix: bytes) -> bytes`

List keys matching a prefix.

- **Input:** UTF-8 prefix string.
- **Output:** JSON array of key strings: `["key1", "key2"]`.

### `astrid_kv_clear_prefix(prefix: bytes) -> bytes`

Delete all keys matching a prefix.

- **Input:** UTF-8 prefix string.
- **Output:** JSON number of deleted keys.

---

## 5. Configuration & Environment

### `astrid_get_config(key: bytes) -> bytes`

Retrieve a capsule configuration value.

- **Input:** UTF-8 key string.
- **Output:** JSON-serialized value, or empty string if not found.
- **Well-known keys:** `ASTRID_SOCKET_PATH` — kernel Unix domain socket path.
- **Note:** These are values from the capsule's `[env]` manifest section,
  elicited during install.

---

## 6. Network (Unix Domain Sockets)

Capsules that declare `capabilities.net_bind` can bind Unix domain sockets
for direct frontend communication. The kernel pre-binds the socket; the
capsule accepts connections.

Gated by `capabilities.net_bind` in `Capsule.toml`. Checked once at
bind-time.

### `astrid_net_bind_unix(path: bytes) -> bytes`

Bind a Unix domain socket.

- **Input:** UTF-8 socket path.
- **Output:** Listener handle (opaque string).
- **Security:** `net_bind` capability required. Socket is pre-bound by kernel.

### `astrid_net_accept(listener_handle: bytes) -> bytes`

Accept an incoming connection. Blocks until a connection arrives.

- **Input:** Listener handle.
- **Output:** Stream handle (opaque string).
- **Limit:** Max 8 concurrent streams per capsule.
- **Security:** Session token authentication handshake on accept.

### `astrid_net_poll_accept(listener_handle: bytes) -> bytes`

Non-blocking accept. Returns immediately.

- **Input:** Listener handle.
- **Output:** Stream handle if a connection is pending, empty bytes otherwise.

### `astrid_net_read(stream_handle: bytes) -> bytes`

Read bytes from a stream. Blocks until data is available.

- **Input:** Stream handle.
- **Output:** Raw bytes.

### `astrid_net_write(stream_handle: bytes, data: bytes)`

Write bytes to a stream.

- **Input:** `stream_handle` — stream handle. `data` — raw bytes.

### `astrid_net_close_stream(stream_handle: bytes)`

Close a stream. Idempotent.

- **Input:** Stream handle.

---

## 7. HTTP

Outbound HTTP requests. The kernel enforces SSRF prevention by blocking
requests to local, private, and multicast IP ranges via a safe DNS resolver.

Gated by `capabilities.net` in `Capsule.toml` (list of allowed domains).

### `astrid_http_request(request: bytes) -> bytes`

Issue an HTTP request.

- **Input:** JSON:

```json
{
  "url": "https://api.example.com/v1/data",
  "method": "GET",
  "headers": { "Authorization": "Bearer ..." },
  "body": "..."
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | yes | Full URL. |
| `method` | string | yes | HTTP method (`GET`, `POST`, etc.). |
| `headers` | object | no | Header key-value pairs. |
| `body` | string | no | Request body. |

- **Output:** JSON:

```json
{
  "status": 200,
  "headers": { "content-type": "application/json" },
  "body": "..."
}
```

- **Security:** DNS resolver blocks loopback (127.0.0.0/8), private
  (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16), link-local
  (169.254.0.0/16), and multicast (224.0.0.0/4) ranges. Default timeout
  30 seconds.

---

## 8. Process Execution

Capsules can spawn native host processes. The kernel sandboxes them
(`sandbox-exec` on macOS, `bwrap` on Linux).

Gated by `capabilities.host_process` in `Capsule.toml`.

### `astrid_spawn_host(request: bytes) -> bytes`

Spawn a process and block until completion.

- **Input:** JSON:

```json
{ "cmd": "git", "args": ["status"] }
```

- **Output:** JSON:

```json
{ "stdout": "...", "stderr": "...", "exit_code": 0 }
```

### `astrid_spawn_background_host(request: bytes) -> bytes`

Spawn a non-blocking background process.

- **Input:** JSON: `{ "cmd": "...", "args": ["..."] }`
- **Output:** JSON: `{ "id": 42 }` (process handle).

### `astrid_read_process_logs_host(request: bytes) -> bytes`

Read buffered output from a background process. Each call drains the buffer —
only new output since the last read is returned.

- **Input:** JSON: `{ "id": 42 }`
- **Output:** JSON:

```json
{
  "stdout": "...",
  "stderr": "...",
  "running": true,
  "exit_code": null
}
```

### `astrid_kill_process_host(request: bytes) -> bytes`

Terminate a background process. Sends SIGINT, then SIGKILL after a 2-second
grace period.

- **Input:** JSON: `{ "id": 42 }`
- **Output:** JSON:

```json
{
  "killed": true,
  "exit_code": 137,
  "stdout": "...",
  "stderr": "..."
}
```

---

## 9. Identity

User identity resolution and platform linking. Gated by
`capabilities.identity` with three privilege levels: `resolve` < `link` <
`admin`. Higher levels imply all lower levels.

### `astrid_identity_resolve(request: bytes) -> bytes`

Resolve a platform user to an Astrid user.

- **Capability:** `identity = ["resolve"]`
- **Input:** JSON: `{ "platform": "discord", "platform_user_id": "123456" }`
- **Output:** JSON:

```json
{ "found": true, "user_id": "...", "display_name": "..." }
```

Or `{ "found": false }` if no link exists.

### `astrid_identity_link(request: bytes) -> bytes`

Link a platform identity to an Astrid user.

- **Capability:** `identity = ["link"]`
- **Input:** JSON:

```json
{
  "platform": "discord",
  "platform_user_id": "123456",
  "astrid_user_id": "uuid-...",
  "method": "manual"
}
```

- **Output:** JSON: `{ "ok": true, "link": { "linked_at": "..." } }`

### `astrid_identity_unlink(request: bytes) -> bytes`

Unlink a platform identity.

- **Capability:** `identity = ["link"]`
- **Input:** JSON: `{ "platform": "discord", "platform_user_id": "123456" }`
- **Output:** JSON: `{ "ok": true, "removed": true }`

### `astrid_identity_list_links(request: bytes) -> bytes`

List all platform links for an Astrid user.

- **Capability:** `identity = ["link"]`
- **Input:** JSON: `{ "astrid_user_id": "uuid-..." }`
- **Output:** JSON: `{ "ok": true, "links": [{ "platform": "...", "platform_user_id": "...", "linked_at": "..." }] }`

### `astrid_identity_create_user(request: bytes) -> bytes`

Create a new Astrid user.

- **Capability:** `identity = ["admin"]`
- **Input:** JSON: `{ "display_name": "Alice" }` (`display_name` optional).
- **Output:** JSON: `{ "ok": true, "user_id": "uuid-..." }`

---

## 10. Approval

Human-in-the-loop approval for sensitive actions. The kernel checks an
allowance store for instant approval before publishing to the event bus.

### `astrid_request_approval(request: bytes) -> bytes`

Request human approval for an action.

- **Input:** JSON:

```json
{
  "action": "git push",
  "resource": "git push origin main",
  "risk_level": "high"
}
```

| Field | Type | Required | Max Length | Description |
|-------|------|----------|-----------|-------------|
| `action` | string | yes | 256 bytes | Short action description. |
| `resource` | string | yes | 1,024 bytes | Full resource description. |
| `risk_level` | string | yes | 64 bytes | `"low"`, `"medium"`, `"high"`. |

Control characters are stripped from all fields.

- **Output:** JSON:

```json
{ "approved": true, "decision": "approve" }
```

| Decision | Meaning |
|----------|---------|
| `"approve"` | One-time approval for this request. |
| `"approve_session"` | Approved for the rest of this session. |
| `"approve_always"` | Creates a permanent allowance. |
| `"deny"` | User denied the request. |
| `"allowance"` | Auto-approved by an existing allowance (instant, no user prompt). |

- **Timeout:** 60 seconds. Returns `{ "approved": false }` on timeout.

---

## 11. Elicit (Install/Upgrade User Input)

Collect user input during capsule installation or upgrade lifecycle hooks.
These functions are **only callable during `#[install]` or `#[upgrade]`
handlers** — calling them at any other time returns an error.

### `astrid_elicit(request: bytes) -> bytes`

Prompt the user for a configuration value.

- **Input:** JSON:

```json
{
  "type": "secret",
  "key": "ANTHROPIC_API_KEY",
  "description": "Enter your Anthropic API key"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | `"secret"`, `"text"`, `"select"`, `"array"`. |
| `key` | string | yes | Configuration key to store. |
| `description` | string | no | Prompt text. |
| `default` | string | no | Default value (text/select). |
| `options` | array | no | Valid choices (select). |
| `placeholder` | string | no | Placeholder hint text. |

- **Output:** Depends on type:
  - `secret`: `{ "ok": true }` (value stored in `SecretStore`, never returned)
  - `text` / `select`: `{ "value": "..." }`
  - `array`: `{ "values": ["...", "..."] }`
- **Timeout:** 120 seconds.
- **Security:** Secrets are stored in the kernel's `SecretStore` and injected
  into the capsule's config at boot. The capsule never receives the raw secret
  value through this call.

### `astrid_has_secret(request: bytes) -> bytes`

Check if a secret has been configured.

- **Input:** JSON: `{ "key": "ANTHROPIC_API_KEY" }`
- **Output:** JSON: `{ "exists": true }`

---

## 12. Scheduling (Cron)

Dynamic background scheduling. Allows capsules to register recurring jobs
that wake the capsule at specified intervals.

**Status:** Declared but not yet implemented. Calls are accepted and logged
but have no effect.

### `astrid_cron_schedule(name: bytes, schedule: bytes, payload: bytes)`

Schedule a dynamic cron job.

- **Input:** `name` — job identifier. `schedule` — cron expression
  (e.g. `"0 0 * * *"`). `payload` — JSON payload delivered when triggered.

### `astrid_cron_cancel(name: bytes)`

Cancel a scheduled cron job.

- **Input:** `name` — job identifier (from `astrid_cron_schedule`).

---

## 13. System

Logging, clocks, lifecycle signals, hooks, and capability checks.

### `astrid_log(level: bytes, message: bytes)`

Log a message to the OS journal.

- **Input:** `level` — one of `"trace"`, `"debug"`, `"info"`, `"warn"`,
  `"error"`. Case-insensitive. Aliases: `"warning"` → `"warn"`,
  `"err"` → `"error"`. Default: `"info"`. `message` — UTF-8 string
  (max 64 KB).

### `astrid_clock_ms() -> bytes`

Get wall-clock time.

- **Output:** UTF-8 decimal string of milliseconds since UNIX epoch.
- **Fallback:** Returns `"0"` if system clock is unavailable.

### `astrid_signal_ready()`

Signal that the capsule's run loop is initialized and ready.

- **Behavior:** Sends `true` on the kernel's readiness watch channel. Must be
  called by run-loop capsules after setting up IPC subscriptions. The kernel
  waits for this signal before considering the capsule fully booted.

### `astrid_trigger_hook(event: bytes) -> bytes`

Trigger a synchronous hook and collect responses from matching interceptors.

- **Input:** JSON:

```json
{ "hook": "topic.name", "payload": { "..." } }
```

- **Output:** JSON array of responses from each matching interceptor:
  `[{ ... }, { ... }]`.
- **Max payload:** 1 MB.
- **Security:** The calling capsule is excluded from the fan-out to prevent
  infinite recursion.

### `astrid_check_capsule_capability(request: bytes) -> bytes`

Check whether a capsule has a specific capability.

- **Input:** JSON:

```json
{ "source_uuid": "uuid-...", "capability": "allow_prompt_injection" }
```

- **Output:** JSON: `{ "allowed": true }` or `{ "allowed": false }`.
- **Fail-closed:** Unknown UUIDs or unknown capability names return
  `{ "allowed": false }`.
- **Known capabilities:** `"allow_prompt_injection"`.

### `astrid_get_caller() -> bytes`

Get the caller context (user and session) for the current execution.

- **Output:** JSON: `{ "session_id": "...", "user_id": "..." }`.
- **Note:** Returns empty object when caller context is not yet threaded
  through (current implementation limitation).

---

## Security invariants

The following invariants hold across all host functions:

1. **Fail-closed.** Any ambiguity in capability checks, ACL matching, or
   path resolution results in denial, not approval.
2. **No cross-capsule KV access.** KV keys are scoped per-capsule at the
   storage layer. A capsule cannot read another capsule's keys regardless of
   the key string.
3. **Symlink escape prevention.** All VFS paths are canonicalized. If
   canonicalization reveals the path escapes the capsule's allowed boundary,
   the call fails.
4. **SSRF prevention.** HTTP requests are resolved through a safe DNS
   resolver that blocks local, private, link-local, and multicast IP ranges.
5. **IPC scope isolation.** A capsule can only subscribe to topics matching
   its declared `ipc_subscribe` patterns. It can only publish to topics
   matching its declared `ipc_publish` patterns.
6. **Process sandboxing.** Spawned host processes run inside an OS-level
   sandbox (`sandbox-exec` on macOS, `bwrap` on Linux).
7. **Interceptor recursion prevention.** `astrid_trigger_hook` excludes the
   calling capsule from the fan-out.
8. **Runtime-owned subscriptions are immutable.** Interceptor
   auto-subscriptions created by the kernel cannot be unsubscribed by the
   guest.
9. **Identity privilege escalation prevention.** Identity operations enforce
   a strict hierarchy: `resolve` < `link` < `admin`. A capsule with
   `resolve` cannot call `link` or `admin` operations.

# Drawbacks
[drawbacks]: #drawbacks

- **Retroactive documentation.** This RFC documents an existing implementation
  rather than proposing a new design. The implementation may have accumulated
  inconsistencies that a clean-sheet design would avoid.
- **Large surface area.** 48 functions is a substantial ABI. Some may be
  candidates for consolidation (e.g. `spawn_host` vs
  `spawn_background_host`). However, stabilizing the current ABI as a
  baseline is more valuable than redesigning it now.
- **JSON wire format.** JSON adds serialization overhead and ambiguity
  (number precision, encoding). A binary format (MessagePack, FlatBuffers)
  would be more efficient. However, JSON is debuggable, universally supported,
  and consistent with the IPC event bus payloads.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why document as-is instead of redesigning?** The ABI is already in use by
15+ capsules. A redesign would require rewriting every capsule. Documenting
the current state provides immediate value (capsule authors can build against
a spec) and establishes a baseline from which incremental improvements can
be proposed via follow-up RFCs.

**Why `Vec<u8>` everywhere?** The Extism host function ABI uses raw byte
buffers. Type safety is provided by the SDK layer (`astrid-sdk`), not the
ABI layer. This keeps the ABI language-agnostic — a capsule can be written
in any language that compiles to WASM and can serialize JSON.

**Why JSON instead of a binary format?** JSON aligns with the IPC event bus
(also JSON payloads) and is human-readable for debugging. Performance-critical
paths (file I/O, IPC) pass raw bytes when possible. Structured requests/responses
use JSON for consistency.

# Prior art
[prior-art]: #prior-art

- **WASI (WebAssembly System Interface).** The closest parallel — a standard
  syscall interface for WASM modules. WASI uses a component model with
  typed interfaces (WIT). Astrid uses the simpler Extism model with JSON
  payloads. WASI's scope is broader (filesystem, sockets, clocks); Astrid's
  ABI includes domain-specific functions (IPC, identity, approval, hooks).
- **Extism PDK.** Astrid's host function ABI is built on Extism. The
  calling convention, memory model, and error reporting follow Extism patterns.
- **POSIX syscalls.** The conceptual model: a stable ABI between kernel and
  user-space, with numbered/named functions, capability checks, and
  resource limits.
- **Deno permissions.** Deno gates filesystem, network, and process access
  behind runtime permissions. Astrid's capability model is similar but
  declared in the manifest rather than at the command line.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **`astrid_get_caller` completeness.** Currently returns an empty object.
  Should caller context (session ID, user ID, call ID) be threaded through
  all execution paths?
- **Cron implementation.** The cron functions are declared but not
  implemented. Should they be removed from this RFC until implemented, or
  kept as a committed contract?
- **HTTP request schema strictness.** Should the `url` field require HTTPS,
  or allow HTTP for development? Should there be an explicit domain allowlist
  check against `capabilities.net`?
- **VFS stat/readdir output schemas.** The exact JSON shapes for
  `astrid_fs_stat` and `astrid_fs_readdir` are not fully specified here.
  Should they be pinned in this RFC or a follow-up?
- **Error code standardization.** Currently errors are free-form strings.
  Should there be numeric error codes for programmatic handling?

# Future possibilities
[future-possibilities]: #future-possibilities

- **Typed ABI via WASM component model (WIT).** Replace JSON serialization
  with WIT-defined interfaces for compile-time type safety and zero-copy
  performance.
- **Streaming host functions.** Currently all I/O is request-response. A
  streaming ABI (e.g. `astrid_fs_read_stream`) would enable efficient large
  file handling.
- **Cross-capsule KV.** Scoped shared storage between capsules that declare
  a mutual capability.
- **GPU / accelerator access.** Host functions for offloading compute to
  hardware accelerators.
- **Numeric error codes.** A standard error enum mapped to the free-form
  error strings.
