- Feature Name: `host_abi`
- Start Date: 2026-03-22
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issues: [astrid#573](https://github.com/unicity-astrid/astrid/issues/573), [astrid#750](https://github.com/unicity-astrid/astrid/issues/750)

# Summary
[summary]: #summary

Define the host ABI — the syscall-like interface between the Astrid kernel
and WASM capsule guests. 12 per-domain packages under `astrid:*`, a parallel
`astrid-bus:*` namespace for capsule-to-capsule IPC schemas. Surface uses
typed Component Model resources for long-lived handles (`file-handle`,
`tcp-stream`, `subscription`, `process-handle`, …) and typed per-package
`error-code` variants for failures. All operations are capability-gated,
audited, and per-principal scoped.

The 1.0 ABI also adopts an evolution discipline to make post-1.0 changes
non-fatal for third-party capsules: the monolithic `astrid:capsule@0.1.0`
package splits into per-domain packages (`astrid:ipc@1.0.0`,
`astrid:net@1.0.0`, …) plus `astrid:guest@1.0.0` for the lifecycle export
contract; the kernel registers every supported `(package, version)` pair
into the wasmtime Component Model linker explicitly; and once a WIT file
is published it is immutable forever — shape changes ship as new files at
new versions.

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
set of per-domain WIT files (`host/<name>@<version>.wit`). The kernel
vendors a snapshot at `core/wit/` for its build, but the canonical source is
the WIT repo. (Note: the `host/` directory holds the kernel-to-capsule ABI
covered by this RFC; the unrelated `interfaces/` directory holds
capsule-to-capsule IPC event contracts, which are governed by topic
versioning rather than the WIT discipline below.)

This RFC documents the design rationale, capability model, scoping semantics,
and evolution discipline. The WIT files are the reference for function
signatures and types. If this RFC and a WIT file disagree, the WIT file is
correct.

## Host interfaces

12 per-domain packages under `astrid:*` (the kernel ABI). Each package's
surface is a mix of methods on typed resources (long-lived handles) and
top-level functions on the `host` interface.

| Package | Surface | Capability | Purpose |
|---|---|---|---|
| `astrid:fs` | `file-handle` resource + path ops | `fs_read`, `fs_write` | Virtual filesystem (workspace://, home://, tmp://); file IO, metadata, canonicalize, hard-link |
| `astrid:ipc` | `subscription` resource + publish/publish-as | `ipc_publish`, `ipc_subscribe`, `uplink` (publish-as) | IPC event bus with `principal-attribution` verified/claimed distinction |
| `astrid:uplink` | uplink-register/send | `uplink` | Frontend connection registration |
| `astrid:kv` | get/set/delete/cas/list/list-page/clear-prefix | (ungated) | Per-capsule, per-principal key-value with atomic CAS |
| `astrid:net` | `unix-listener` / `tcp-listener` / `tcp-stream` / `udp-socket` resources + factories | `net`, `net_connect`, `net_udp`, `net_tcp_bind` | Unix sockets, outbound TCP, inbound TCP, UDP (unconnected + connected), DNS resolution |
| `astrid:http` | `http-stream` resource + http-request | `net` | HTTP client with SSRF airlock, streaming responses, typed method enum |
| `astrid:sys` | log, config, get-caller, clock-ms, clock-monotonic-ns, sleep-ns, random-bytes, check-capsule-capability, enumerate-capabilities | (ungated) | Logging, manifest config, time, entropy, capability introspection (self-enumeration + per-name check) |
| `astrid:process` | `process-handle` resource + spawn/spawn-background, **plus** a persistent tier (`spawn-persistent` → `process-id`, `attach`, id-keyed read/signal/wait/write/stop, `list-processes`/`status`, `watch`) | `host_process` (+ operator `allow_persistent` for the persistent tier) | OS-sandboxed (Seatbelt/bwrap) spawn with stdin/env/cwd, wait/signal/kill, exit-info; reattachable host-owned background processes addressed by principal-scoped id (see "Process: ephemeral and persistent tiers") |
| `astrid:elicit` | elicit/has-secret | `uplink` | Install/upgrade-time user prompts with typed elicit-type and elicit-response |
| `astrid:approval` | request-approval | (ungated) | Human-in-the-loop gate with typed approval-decision |
| `astrid:identity` | resolve/link/unlink/create-user/list-links | `identity` | Multi-platform identity (per-operation typed responses) |
| `astrid:guest` | Per-export worlds: `interceptor` / `background` / `installable` / `upgradable` | — | Lifecycle entry points (kernel → capsule); capsules `include` only what they implement |

### Capsule-to-capsule contracts live in a distinct `astrid-bus:*` namespace

The host ABI above (`astrid:*`) covers direct kernel-mediated calls. A
parallel namespace `astrid-bus:*` lives in `interfaces/*.wit` and carries
the schemas for events flowing between capsules over the IPC bus
(`astrid-bus:llm`, `astrid-bus:session`, `astrid-bus:tool`, etc.). The two
are different layers — host calls go through the wasmtime CM linker; bus
events are typed payloads on `astrid:ipc`. The namespace split makes the
layering visible in capsule worlds and `Capsule.toml`:

```toml
[imports.astrid]
fs = "1.0.0"
ipc = "1.0.0"

[imports.astrid-bus]
llm = "^1.0"
session = "^1.0"
```

### Foundation primitives — minimal WASI retention

The kernel exposes `wasi:io/poll@0.2.0` and `wasi:io/streams@0.2.0` to
capsules — these are the Component Model's standard `pollable` /
`input-stream` / `output-stream` resource types, and dropping them would
leave capsules unable to use the CM's async-readiness composition story.
Everything else from WASI (filesystem, sockets, clocks, random, cli) is
**not** in the linker: those operations would bypass Astrid's capability
gates, principal scoping, and audit chain. Random / monotonic clock /
sleep / wall-clock live under `astrid:sys` instead, so every host call
goes through the same audit layer.

The retention pays off through `subscribe-readiness` / `subscribe-readable`
/ `subscribe-exit` / `subscribe-logs` methods on the Astrid resources —
they return `wasi:io/poll.pollable`, so capsule code can call
`wasi:io/poll.poll([…])` to multiplex IPC + Unix sockets + TCP + UDP +
HTTP-stream chunks + child-process exits without embedding a runtime
purely for that.

### Capability model

Each host function checks the calling capsule's declared capabilities:

- **Gated:** `fs_read`, `fs_write`, `ipc_publish`, `ipc_subscribe`, `uplink`,
  `net`, `net_connect`, `net_udp`, `net_tcp_bind`, `host_process`,
  `identity`. Capsules must declare these in `[capabilities]` in
  `Capsule.toml`.
- **Ungated:** `kv`, `sys`, `approval`. Always available. KV is safe because
  it's namespace-scoped per capsule and principal. Sys functions are read-only
  or side-effect-free. Approval is a request, not an action.

Violations return `capability-denied` (a variant arm of the per-package
`error-code`) to the guest and are logged to the audit chain.

### Capability introspection

A capsule's capability set is **structural metadata, not a secret**. Knowing
that capsule X holds `host_process` grants nothing: capabilities are
*enforced*, not concealed, so a caller can only exercise what it was itself
granted regardless of what it knows. Introspection is therefore a plain
read-only view, ungated on the `sys` surface.

- `enumerate-capabilities() -> list<string>` returns the **calling capsule's
  own** *effective* capabilities — the set the kernel would enforce at
  host-call time, not the manifest text (a runtime-revoked grant does not
  appear). Argument-free: the kernel already knows the caller. This is the
  primitive a capsule uses to ground its behaviour in what it can actually do
  — an agent supervisor narrowing the tool surface it exposes, or refusing
  work that a missing capability would only fail deep — without hard-coding an
  assumption about its own manifest.
- `check-capsule-capability({source-uuid, capability}) -> {allowed}` answers
  the same question for a named capsule (self or other).

Gating cross-capsule introspection was considered and rejected as
security-by-obscurity: the capability name space is small and published here,
UUIDs are discoverable, and — decisively — posture is not sensitive, since
knowing a capability conveys no ability to use it. A gate would add a
capability and a migration to buy only marginal reconnaissance-hardening,
against Astrid's enforce-don't-conceal stance. Introspection stays a view.

`enumerate-capabilities` also underpins the **Capability delegation** future
possibility (see Future possibilities): a parent enumerates its own effective
set to compute the strictly-smaller subset it grants a spawned child.

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

### Process: ephemeral and persistent tiers

`astrid:process` is gated on `host_process` and spawns every child through the
OS sandbox (`bwrap` on Linux, `sandbox-exec`/Seatbelt on macOS) scoped to the
workspace. It exposes **two tiers**, because a capsule's WASM instance is
pooled and stateless — reset and returned to the pool between invocations.

- **Ephemeral** — `spawn` (synchronous) and `spawn-background` (returns a
  `process-handle` resource owned by the spawning instance's resource table;
  the host reaps the child when the handle drops, i.e. when the instance
  resets). Correct for fire-and-forget and within-one-invocation work.
- **Persistent** — `spawn-persistent` returns an opaque, principal-scoped
  `process-id` whose child lives in a **host-owned registry** (lifetime = the
  capsule, not the instance). It is reattachable — read / signal / wait /
  write / stop — from any *later* invocation of the same `(capsule, principal)`,
  including a different pooled instance.

The persistent tier exists because the canonical split-tool pattern — start a
process in one tool call, read its logs in a second, stop it in a third — is
structurally impossible with an instance-owned handle: the handle (and the
child) is reaped when the spawning instance returns to the pool, before the
second tool call runs. Today the only workaround is pinning such a capsule to a
single never-reset instance, forfeiting the dynamic pool. The persistent tier
generalizes beyond the shell capsule's background-process tools to MCP-stdio
subprocess hosts (a long-running child you talk to across invocations),
dev-server / build-watch supervisors, and log tailers.

The persistent surface, alongside `spawn` / `spawn-background` / `process-handle`:

```wit
/// Opaque, unforgeable, principal-scoped identity for a persistent process
/// that survives instance churn. A 256-bit host-minted CSPRNG token (host
/// entropy, never the guest); the host stores a keyed hash and re-checks the
/// calling principal == creator on EVERY id-keyed call, so a leaked token is
/// inert across the principal boundary. Treat as an opaque secret.
type process-id = string;

/// Opaque, host-encoded, resumable cursor for NON-DRAINING log reads.
record log-cursor { token: option<string> }
enum log-stream { stdout, stderr }

/// Lifecycle phase, reported WITHOUT draining logs.
enum process-phase { starting, running, exited, reaped }

/// Ring-overflow policy for a persistent stream.
enum overflow-policy { drop-oldest, backpressure }

/// Non-draining log slice addressed by cursor (the polling-safe complement to
/// the DRAINING `read-logs`). `data` is bytes (children emit non-UTF-8).
record log-chunk {
    data: list<u8>, next: log-cursor, bytes-dropped: u64, drained-eof: bool,
}

/// Per-child OS resource ceilings (RLIMIT / cgroup), applicable to every tier.
/// `none` per field => the principal's profile default. Declared in 1.0 so the
/// record shape is stable; ENFORCEMENT is NOT YET SUPPORTED — today the host
/// bounds only the process slot, log buffers, and lifetime, so a single child
/// can still burn unbounded CPU/RAM. Including the fields now avoids a breaking
/// record change once enforcement lands.
record resource-limits {
    /// Address-space / RSS ceiling (RLIMIT_AS or cgroup memory.max).
    max-memory-bytes: option<u64>,
    /// Cumulative CPU-time ceiling (RLIMIT_CPU → SIGXCPU then SIGKILL).
    max-cpu-ms: option<u64>,
    /// Max concurrent child PIDs (RLIMIT_NPROC / cgroup pids.max) — fork-bomb fence.
    max-pids: option<u32>,
    /// Max open file descriptors (RLIMIT_NOFILE).
    max-open-files: option<u32>,
}

/// Non-draining status snapshot; unit of `list-processes` / `status`.
record process-info {
    id: process-id, label: string, command: string, os-pid: option<u32>,
    phase: process-phase, exit: option<exit-info>, age-ms: u64, idle-ms: u64,
    buffered-bytes: u64, bytes-dropped: u64, stdin-open: bool,
    /// Cumulative CPU time + peak resident memory the child has consumed. The
    /// observability complement to `resource-limits`. Best-effort; `none` where
    /// the platform doesn't surface it or NOT YET POPULATED.
    cpu-ms: option<u64>, mem-bytes-peak: option<u64>,
}

// `spawn-request` carries persistent-only fields (label, keep-stdin-open,
// overflow, log-ring-bytes, max-lifetime-ms, idle-timeout-ms,
// exit-retention-ms), honored only by `spawn-persistent` and ignored by the
// ephemeral spawns, PLUS `limits: option<resource-limits>` (every tier;
// enforcement NOT YET SUPPORTED — see the record above). `process-signal`
// gains `stop` (SIGSTOP, pause) and `cont` (SIGCONT, resume) alongside
// term/hup/usr1/usr2/int, so a supervisor can throttle a runaway child without
// killing it. `error-code` additionally carries: no-such-process (unknown /
// released / wrong-principal / wrong-capsule — indistinguishable, no oracle),
// registry-full (retained-id cap), invalid-id (malformed, returned without a
// lookup), persist-unsupported (host cannot keep a detached child contained —
// e.g. macOS without the operator opt-in, or an owner-fallback principal).

/// Spawn a persistent background process. Gated on `host_process` AND the
/// operator `allow_persistent` sub-grant. Counts against BOTH the per-principal
/// concurrent cap (shared with `spawn-background`) AND a retained-id cap.
/// Fail-closed refusals (`persist-unsupported`): owner-fallback principal
/// (a persistent process needs an unambiguous owner); sandbox disabled; macOS
/// without the operator persistence opt-in.
spawn-persistent: func(request: spawn-request) -> result<process-id, error-code>;

/// Re-materialise a `process-handle` over a persistent process for THIS
/// invocation — the composition primitive (every ephemeral method works).
/// DETACH-on-drop: dropping it (incl. on instance reset) does NOT reap.
attach: func(id: process-id) -> result<process-handle, error-code>;

/// List the caller `(capsule, principal)`'s persistent processes (label-filter
/// optional). NEVER another principal's or capsule's. Empty is normal.
list-processes: func(label-filter: option<string>) -> result<list<process-info>, error-code>;
/// Non-draining status snapshot (the safe poll primitive). `status-many`
/// batches for a supervisor polling N children (one lock pass, one audit row).
status: func(id: process-id) -> result<process-info, error-code>;
status-many: func(ids: list<process-id>) -> result<list<process-info>, error-code>;

// id-keyed convenience fns — exact `attach(id)?.<method>()` sugar for
// single-shot tool calls (one principal check, one error mapping):
read-logs: func(id: process-id) -> result<read-logs-result, error-code>;   // DRAINS
read-since: func(id: process-id, stream: log-stream, cursor: log-cursor, max-bytes: u32) -> result<log-chunk, error-code>;
write-stdin: func(id: process-id, data: list<u8>) -> result<u32, error-code>;
close-stdin: func(id: process-id) -> result<_, error-code>;
signal: func(id: process-id, sig: process-signal) -> result<_, error-code>;
wait: func(id: process-id, timeout-ms: u64) -> result<exit-info, error-code>; // bounded REQUIRED
/// Graceful terminal: SIGTERM, grace, SIGKILL, drain, remove. (Immediate kill:
/// `attach(id)?.kill()`.)
stop: func(id: process-id, grace-ms: option<u64>) -> result<kill-result, error-code>;
/// Free an EXITED process's retained id + log tail without signalling.
release-process: func(id: process-id) -> result<_, error-code>;

/// Arm DURABLE lifecycle events for `id`, published by the host UNDER THE
/// CREATOR PRINCIPAL on `astrid.process.v1.{exited,log-ready,stdin-drained}.*`
/// (a pollable cannot survive instance reset, so the durable channel is the bus
/// the capsule already subscribes to). The `exited` payload carries a reason
/// (self-exit / killed / reaped-ttl / reaped-shutdown) so a supervisor
/// distinguishes a crash from a daemon-shutdown reap.
watch: func(id: process-id, suffix: option<string>) -> result<_, error-code>;
unwatch: func(id: process-id) -> result<_, error-code>;
```

**Identity is a capability, not a name.** A `process-id` is a 256-bit CSPRNG
secret (host entropy, the same `astrid:sys random-bytes` source); the host
stores a keyed hash and re-resolves the live `effective-principal` + `capsule`
on **every** id-keyed call, comparing against the recorded creator *before*
reading any field of the target entry. Wrong-owner / unknown / reaped all
collapse to `no-such-process` (no existence oracle); `invalid-id` is returned
without a registry lookup (timing-oracle defense). Unguessability is defense in
depth — the per-call principal check is the boundary, so even a fully-leaked
token (the shell returns the id in a tool message the LLM sees) is inert across
principals. The **spawn boundary** is the real isolation risk, not attach:
`effective-principal` falls back to the capsule owner when there is no
authenticated caller, so `spawn-persistent` **refuses** the owner-fallback
principal (else several tenants share a `default` namespace `list-processes`
would enumerate).

**Lifecycle.** A persistent process is principal-scoped authority that must not
leak. It is reaped on the first of: explicit `stop`/`release-process`;
`max-lifetime-ms` (omission means the host ceiling, never infinity — a guest
cannot request unbounded life); `idle-timeout-ms`; child self-exit +
`exit-retention-ms`; principal eviction; capsule full-unload (last instance,
never an instance reset); host-pressure GC; daemon shutdown. **Never** on
instance reset or `attach`-handle drop — that is the whole point. Two caps with
distinct errors: a per-principal **concurrent** cap shared with
`spawn-background` (`quota`) and a per-principal **retained-id** cap
(`registry-full`), plus an aggregate per-principal log-buffer ceiling.

**Sandbox lifetime.** Keeping a sandboxed child alive past the spawning instance
needs no new spawn topology: the child is already spawned by the *daemon's*
runtime, and only `kill_on_drop` + the handle's `Drop` reaps it on instance
drop. Relocating ownership into the host registry before the instance resets
moves that reap to registry teardown. On **Linux**, `bwrap`'s `--unshare-all` +
`--die-with-parent` make it kernel-enforced even on a daemon `SIGKILL`. On
**macOS**, Seatbelt has no PID namespace and no parent-death tie, so a child
that escapes its process group is unreapable and a daemon `SIGKILL` leaks it —
macOS persistence is therefore **materially weaker and fail-closed by default**
(`persist-unsupported` unless the operator opts in). The registry is in-memory,
so persistence does **not** survive a daemon restart (ids then resolve to
`no-such-process`, and the recovery signal is an empty `list-processes`); a
process the daemon can no longer see is one it cannot kill, audit, or attribute,
so re-adoption across restart is deliberately out of scope. Every persistent op
is audited (op + principal + capsule + a hash of the id; never the raw id, env,
stdin, or log contents).

**Environment and secrets.** A child inherits **no** ambient host environment:
the sandbox strips everything except a small kernel passthrough allowlist, and
`spawn-request.env` is the only way variables reach the child. The intended
model is that `env` carries operator-reviewable, non-secret configuration, and
a **secret** reaches a child over `write-stdin` (or `spawn-request.stdin`) —
**not** through `args`, which are recorded verbatim in the audit log. Whether
`env` values themselves may carry secrets (and are therefore excluded from
audit, as they already are) versus requiring an explicit secret channel is the
one open semantics question on this surface; the field shape is unaffected
either way.

## Guest exports

Guest exports live in `astrid:guest@1.0.0`, split into per-export worlds so
capsules `include` only the entry points they implement. Each world matches
a single export — the toolchain only emits the export when its world is
included, so the kernel sees only real implementations (no stub-detection
parsing needed).

| World | Export | Required if included |
|---|---|---|
| `interceptor` | `astrid-hook-trigger(action, payload)` returning `capsule-result` | Most capsules — handles routed events |
| `background` | `run()` | Capsules with a long-lived IPC subscriber loop |
| `installable` | `astrid-install()` | One-time install hook (may use `elicit` for secrets/config) |
| `upgradable` | `astrid-upgrade()` | Version-upgrade hook |

A typical capsule world:

```wit
world cli {
    include astrid:guest/interceptor@1.0.0;
    include astrid:guest/background@1.0.0;
    include astrid:guest/installable@1.0.0;
    import astrid:ipc/host@1.0.0;
    import astrid:uplink/host@1.0.0;
    import astrid:net/host@1.0.0;
}
```

## Error handling

Host functions return `result<T, error-code>` where `error-code` is a typed
variant per package — `astrid:fs/host.error-code`, `astrid:net/host.error-code`,
etc. Specific arms (`not-found`, `capability-denied`, `would-block`,
`is-directory`, `boundary-escape`, `airlock-rejected`, etc.) cover the
common cases; a catch-all `unknown(string)` arm carries best-effort host
detail for unforeseen errors without forcing an ABI bump.

The SDK translates per-package `error-code` to language-idiomatic errors
(`Result<T, SysError>` in Rust where `SysError` carries the source variant).
Capsule code pattern-matches on the specific arms.

Error strings (the `unknown` arm payload) are constrained: no host
real-paths, IP addresses, UUIDs, or capability names leak through. The
specific-arm enumeration is the contract; the string is for human
debugging only.

Unrecoverable errors (lock poisoning, memory exhaustion) trap — those
represent kernel invariant violations, not guest-recoverable conditions.

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

The pre-1.0 monolithic `astrid:capsule@0.1.0` package containing 11 functional
interfaces plus a shared `types` interface is replaced by one package per
domain:

```
astrid:fs@1.0.0
astrid:ipc@1.0.0
astrid:uplink@1.0.0
astrid:kv@1.0.0
astrid:net@1.0.0
astrid:http@1.0.0
astrid:sys@1.0.0
astrid:process@1.0.0
astrid:elicit@1.0.0
astrid:approval@1.0.0
astrid:identity@1.0.0
```

Plus **`astrid:guest@1.0.0`** for the guest export contract — split into
four per-export worlds (`interceptor`, `background`, `installable`,
`upgradable`) so capsules `include` only the entry points they implement.
The naming is symmetric with the per-domain `host` interfaces: `host` is
kernel-side (imported by capsules), `guest` is capsule-side (called by the
kernel).

Per-export worlds matter because the wasm32-wasip2 toolchain auto-stubs
every export declared in the world a component targets. A single world
covering all four entry points would force stubs for the ones a given
capsule does not implement, and the kernel would have to parse the wasm
binary to distinguish real implementations from toolchain stubs. Per-export
worlds eliminate the cause: an export appears in the binary only when the
capsule actually implements it, and the kernel uses plain export-presence
checks.

Each domain package owns its own types and resources in a single `host`
interface alongside its functions. `ipc-envelope` / `ipc-message` and the
`subscription` resource live in `astrid:ipc/host`; `file-handle` resource
and `file-stat` record live in `astrid:fs/host`; and so on. Per-domain
type ownership isolates per-domain evolution: bumping `astrid:net` does not
recompile anything that only imports `astrid:kv`. There is no shared
cross-cutting package — each package carries its own typed `error-code`
variant, and primitives (`option<string>` for principals, etc.) cover the
remaining shared concepts.

Capsules opt into exactly the subset they need on both axes — which guest
exports they implement and which host imports they use:

```wit
// Interceptor-only capsule:
world router {
    include astrid:guest/interceptor@1.0.0;
    import astrid:ipc/host@1.0.0;
    // not net, not http — capsule does not use them
}

// Run-loop capsule with an install hook:
world cli {
    include astrid:guest/interceptor@1.0.0;
    include astrid:guest/background@1.0.0;
    include astrid:guest/installable@1.0.0;
    import astrid:ipc/host@1.0.0;
    import astrid:uplink/host@1.0.0;
    import astrid:net/host@1.0.0;
}
```

A kv-only capsule is unaffected when `astrid:net` adds a function. An
interceptor-only capsule produces no `run` / install / upgrade exports in
its wasm binary, so the kernel sees only what is real.

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

Once `astrid:ipc@1.0.0` ships, the file at `host/ipc@1.0.0.wit` is
**immutable forever**. New shapes get a new file:

```
host/
  ipc@1.0.0.wit           # frozen
  ipc@1.1.0.wit           # frozen
  ipc@2.0.0.wit           # current
```

When a shape change is needed: copy the latest frozen file to a new version
path, modify, register the new version in the kernel, leave the old files
alone. The current pattern of editing a WIT file in place ends.

This is enforced by a CI lint (`scripts/lint-wit-immutability.sh`) in the
`unicity-astrid/wit` repository: any PR that modifies, deletes, or renames a
file matching `*@X.Y.Z.wit` that exists on the base ref fails the build. New
files matching the pattern are allowed — that is the legitimate evolution
path.

### Versioning rules

A WIT package version follows semver with one constraint imposed by the
Component Model linker: the linker is structurally typed per `(package,
version)` pair, so any change that produces a different shape is a contract
break from the linker's perspective regardless of intent. The version number
is therefore a signal to capsule authors about what they will have to do
when they bump.

| Change | Bump | Rationale |
|---|---|---|
| Add a new top-level function to an interface | **MINOR** | Existing function and type shapes are exactly preserved; old capsules' generated bindings are unaffected. The kernel registers the new minor as a fresh `(package, version)` pair and the old minor's implementation falls out as a subset. |
| Add a field to a record | **MAJOR** | Even `option<>` fields produce a different record layout in `wit-bindgen` output. Old capsules built against the prior shape fail to instantiate. |
| Add a variant to an enum | **MAJOR** | Different shape; exhaustive matches on return-position enums in old capsules break on regenerate. |
| Change a function signature | **MAJOR** | Obvious. |
| Remove a function, field, or variant | **MAJOR** | Obvious. |

Only purely orthogonal additions — new top-level functions on existing
interfaces — qualify as minor bumps, because they are the only change that
leaves the existing shape exactly preserved. Any touch of an existing
record, enum, or function signature is major. This is conservative relative
to the cultural "additive = minor" convention in other ecosystems and
matches the Component Model linker's actual semantics.

`Capsule.toml` accepts Cargo-style version constraints
(`ipc = "^1.0"`) in `[imports.astrid]`. The constraint expresses the
capsule author's intent — "I tolerate any 1.x" — while the wasm binary
still pins the exact version it was built against. The kernel's
multi-version registration policy (Rule 2) is what reconciles the two:
the kernel registers every minor in the supported window, so any 1.x
capsule loads against any 1.y kernel where y ≥ x and both are inside the
window.

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

- **Large surface area.** Twelve per-domain packages with typed resources
  and per-package error variants is a lot to maintain stable. Each public
  method on every resource is a compatibility commitment, and per-domain
  splitting multiplies the number of independently-versioned packages the
  kernel must track.
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
- **No central package for genuinely cross-cutting types.** Each domain owns
  its own types. If a future evolution introduces a type that genuinely
  belongs in multiple domain packages, the choice is duplicate-and-keep-in-
  sync (more surface) or introduce a shared package at that point. The bet
  is that genuinely cross-cutting types are rare.

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

- **Push `principal` out of payload onto a resource handle.** A resource-typed
  caller-context (host-owned, opaque handle, accessor methods) would let the
  host evolve principal representation without changing any WIT payload at
  all. Worth a design pass before 1.0; not blocking, because the record-based
  ABI works as long as the per-domain split + frozen-file rules hold. Today
  `caller-context` lives in `astrid:sys/host` and is returned by `get-caller`;
  if this lands, the record gets a resource counterpart and downstream
  capsules migrate to the handle-based accessor over a release.

- **Support-window length.** How many versions of each package does the
  kernel pledge to load simultaneously? Two? Three? Indefinite? Each extra
  version adds shim maintenance. A "N+2 minor, N-1 major" convention is one
  option; "current and previous major" is another. Decision deferred until
  the first post-1.0 minor bump, by which point we'll have real maintenance
  data.

- **Audit chain integration.** Which host functions should produce audit
  entries? Currently logging and approval are audited. Should every `fs_write`
  be audited? Every `ipc_publish`? The audit chain grows linearly with calls —
  full auditing could be expensive for high-throughput capsules.

- **Resource limits.** Should host functions enforce resource limits (max file
  size on `write-file`, max message size on `ipc-publish`, max KV value size)?
  Currently unbounded — a capsule can write arbitrarily large values.

- **Persistent-process `watch` publish authority.** The persistent tier's
  `watch` has the host publish `astrid.process.v1.*` lifecycle events under the
  creator principal. Does that need a manifest `[publish]` declaration, or is it
  a distinct *kernel-authored* topic class the capsule only `[subscribe]`s to
  (the host, not the capsule, is the authoritative publisher)? This ties to the
  topic-grammar work. If unresolved, the fallback is to drop `watch` and have
  supervisors poll `status`/`status-many` + a bounded `wait` — no new publish
  authority, slightly less efficient. Related: the principal-eviction reap
  chokepoint, whether `allow_persistent` is a distinct operator sub-grant vs
  `host_process` alone, and whether macOS persistence ships behind the operator
  opt-in with a best-effort reaper or is gated on a stronger macOS reaper first.
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
