- Feature Name: `persistent_host_processes`
- Start Date: 2026-06-06
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

Add `astrid:process@1.1.0`, an additive evolution of the host process interface, introducing an **opt-in persistent tier**: a `spawn-persistent` call that returns an opaque, unforgeable, principal-scoped `process-id` whose backing process lives in a **host-owned registry** (lifetime = the capsule, not the spawning instance) and can be reattached — read, signalled, waited, written, killed — from any *later* invocation of the same capsule under the same principal, including a different pooled instance. The frozen `@1.0.0` ephemeral surface is re-exposed unchanged and composes with the new surface via an `attach(id) -> process-handle` primitive. The design generalizes well beyond its motivating consumer (the shell capsule's background-process tools) to MCP-stdio subprocess hosts, dev-server / build-watch supervisors, and log tailers, while keeping every operation capability-gated, sandbox-contained, per-principal isolated, quota-bounded, auditable, and fail-secure.

# Motivation
[motivation]: #motivation

`astrid:process@1.0.0`'s `spawn-background` returns a `process-handle`, a wasmtime **resource owned by the spawning instance's resource table**. The host reaps the child when that resource drops.

Since the dynamic instance pool + per-principal multiplexing landed, a capsule instance is reset and returned to the pool *between* tool invocations. So the `process-handle` resource — and therefore the child — is destroyed when the spawning tool call returns, **before** any later, separate tool call (likely dispatched onto a *different* pooled instance) could read its logs or kill it.

This makes the canonical split-tool pattern impossible:

```
spawn_background_process(cmd) -> id      # invocation A, instance 7
read_process_logs(id)         -> output  # invocation B, instance 3  ← id is dead
kill_process(id)                         # invocation C, instance 7  ← id is dead
```

The `astrid-capsule-shell` capsule offers exactly these three tools. Today they only work because of a `host_process` carve-out that pins such capsules to a **single, never-reset instance** (`pool size == 1`), forfeiting the dynamic pool and serializing all of the capsule's work onto one Store — the precise per-principal serialization Phase 2 removed everywhere else. As shell migrates to `astrid-sdk 0.7` it cannot keep the old pid-based `process::read_logs(id)` / `process::kill(id)` free functions, because `@1.0.0` is resource-handle-only with no way to reattach to a process by id.

The naive workarounds do not hold:

- **`bash &` / orphaning** — On Linux the child is spawned under a `bwrap` PID namespace with `--die-with-parent`; when the foreground process exits the namespace tears down and the backgrounded child is killed. On macOS (seatbelt, no PID namespace) it would *survive but leak* as an untracked, unattributable, unsandboxed-from-the-host's-view process. Both also lose the buffered ring logs, the principal attribution, and the cancellation net. Containment is a feature, and the sandbox is doing its job.
- **Pin the instance** — that *is* today's carve-out; it defeats the pool.

The real need is broader than shell. A long-running child you talk to across many invocations is the **canonical MCP-stdio subprocess** pattern; a **dev-server / build-watch supervisor** wants to start a process, tail its logs, and stop it across a conversation; a **log tailer / `astrid top`-style UI** wants a non-draining view of running children. None of these are expressible today. Per the design mandate, we build the general primitive — a host-owned, id-addressable, principal-scoped process registry — not just shell's three tools.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

`@1.1.0` offers **two explicit tiers**, side by side:

| Tier | Spawn | Lifetime | Reattach |
|---|---|---|---|
| **Ephemeral** (= `@1.0.0`) | `spawn` (sync) / `spawn-background` | Reaped when the spawning instance's resource handle drops (instance reset / drop). | No. |
| **Persistent** (new) | `spawn-persistent` | Host-owned; survives instance churn. Reaped only by explicit `stop`/`release-process`, a TTL/idle deadline, child-exit retention, principal eviction, capsule full-unload, host-pressure GC, or daemon shutdown. | Yes — by `process-id`, from any later invocation of the same `(capsule, principal)`. |

### The persistent flow

```rust
// Invocation A — start a dev server and hand the id back to the model.
let id = process::spawn_persistent(SpawnRequest {
    cmd: "npm".into(), args: vec!["run".into(), "dev".into()],
    label: Some("vite-dev".into()),
    ..Default::default()
})?;
// return `id` to the LLM (treat it as a SECRET — see security)

// Invocation B (possibly a different pooled instance) — tail it.
let logs = process::read_logs(&id)?;          // drains since last read
// ...or non-draining, multi-reader:
let chunk = process::read_since(&id, LogStream::Stdout, cursor, 64 * 1024)?;

// Invocation C — stop it.
process::stop(&id, /* grace */ Some(2_000))?; // SIGTERM, grace, SIGKILL, reap
```

The `process-id` is the only identity. It is an opaque, unforgeable, principal-bound capability token (a 256-bit host-minted secret), **not** a guessable integer. A capsule never parses, pattern-matches, or synthesizes it.

### `attach` — the composition primitive

For a *multi-step* invocation (write to stdin, then read, then wait) you reattach once and use the familiar `@1.0.0` resource:

```rust
let proc = process::attach(&id)?;             // re-materialise a process-handle
proc.write_stdin(b"SELECT 1;\n")?;            // REPL / psql / MCP-stdio child
let out = proc.read_logs()?;
// dropping `proc` (or instance reset) DETACHES — it does NOT reap the child.
```

`attach` re-materialises a normal `process-handle` over the persistent process for the duration of the current invocation. Every existing method works. The single behavioral difference from a `spawn-background` handle: dropping an `attach`ed handle **detaches** (severs this instance's view) rather than reaping. Reaping is always explicit (`stop` / `release-process`) or a deadline. The id-keyed free functions (`read-logs(id)`, `signal(id, sig)`, `write-stdin(id, …)`, …) are exact `attach(id)?.<method>()` sugar for single-shot tool calls.

### Discovery and events

- `list-processes(label-filter)` returns **only the caller `(capsule, principal)`'s** processes — the supervisor-rediscovery and post-restart recovery surface. An empty list is normal.
- `status(id)` / `status-many(ids)` return a non-draining `process-info` snapshot (phase, pid, age, idle, buffered bytes, dropped bytes) — the safe poll primitive for an `astrid top`-style UI on a timer.
- `watch(id)` arms **durable, event-driven** notifications on the IPC bus the capsule already subscribes to (a pollable cannot survive instance reset, so it is not the durable channel) — exit, log-ready, stdin-drained — the canonical pattern for a supervisor that reacts in a later invocation.

### Lifecycle you must understand

A persistent process is **principal-scoped authority that must not outlive its grantor or leak**. It is reaped on the first of: explicit `stop`/`release-process`; `max-lifetime-ms` wall-clock TTL; `idle-timeout-ms` (no operation touched it); `exit-retention-ms` after the child exits on its own; principal eviction; capsule full-unload; host-pressure GC; daemon shutdown. Omitting `max-lifetime-ms` means *the host ceiling*, never infinity — a guest cannot request an unbounded lifetime.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Versioning and additivity

`@1.0.0` stays **byte-frozen** per the ABI evolution discipline; `@1.1.0` is a **new file** (`wit/host/process@1.1.0.wit`). The Component Model linker resolves the exact `(package, version)` an importer requests, so `@1.0.0` importers are unaffected and the kernel world registers a *second* `add_to_linker`. WIT has no cross-version `use` for same-package shapes, so `@1.1.0` re-declares the `@1.0.0` records/resource it references. These re-declarations are **wire-compatible** (canonical-ABI lowering matches), but bindgen generates *distinct Rust types per versioned interface* (`process_1_0_0::ExitInfo` ≠ `process_1_1_0::ExitInfo`); the SDK provides `From` conversions. The re-exposed `process-handle` is likewise a *new resource type* in the `@1.1.0` interface — functionally identical, not binding-identical.

## The WIT

```wit
/// Host-side persistent process management — ADDITIVE evolution of
/// `astrid:process@1.0.0`. NEW FILE; @1.0.0 stays byte-frozen.
package astrid:process@1.1.0;

interface host {
    use astrid:io/poll@1.0.0.{pollable};

    /// Typed error returned from process operations. Superset of the
    /// @1.0.0 variant (WIT cannot extend a variant in place). Existing
    /// cases keep identical meaning; new cases are only ever returned
    /// from the new persistent surface.
    variant error-code {
        /// Capsule lacks `host_process` (or, for `spawn-persistent`, the
        /// operator `allow_persistent` sub-grant).
        capability-denied,
        /// cmd/args/cwd/env/label validation failure, or a lifecycle
        /// precondition violation (e.g. `release-process` on a running child).
        invalid-input,
        /// `cwd` resolved outside the workspace.
        boundary-escape,
        /// Per-principal CONCURRENT background-process cap exhausted
        /// (shared between ephemeral and persistent spawns).
        quota,
        /// Stdin payload exceeded the per-call 1 MB cap or the
        /// cumulative-per-process write quota.
        too-large,
        /// Handle closed (process exited and reaped). @1.0.0-shaped surface.
        closed,
        /// Spawn cancelled (capsule unloading).
        cancelled,
        /// Wait timed out before the process exited.
        wait-timeout,
        /// NEW. No persistent process with this id is VISIBLE to the calling
        /// principal: unknown, released/reaped, owned by a different principal,
        /// OR owned by a different capsule — the host MUST NOT distinguish
        /// these (leaking "exists but not yours" is a cross-principal oracle).
        /// The post-restart / post-reap recovery signal is an empty
        /// `list-processes`, not this error.
        no-such-process,
        /// NEW. Per-principal/per-capsule RETAINED-ID cap full (live +
        /// exited-but-unreleased). Distinct from `quota` (concurrent cap).
        registry-full,
        /// NEW. Structurally malformed id; returned WITHOUT a registry lookup
        /// (timing-oracle defense). Treat identically to `no-such-process`.
        invalid-id,
        /// NEW. `spawn-persistent` refused because the host cannot keep a
        /// detached child contained for this configuration (sandbox disabled;
        /// macOS without the operator persistence opt-in; unauthenticated
        /// owner-fallback principal). Fail-closed: never persist a process the
        /// host cannot reap or attribute.
        persist-unsupported,
        /// Unspecific host error; detail is best-effort.
        unknown(string),
    }

    /// Signal (Unix semantics). Re-declared from @1.0.0.
    enum process-signal { term, hup, usr1, usr2, int }

    record env-var { key: string, value: string }
    record exit-info { exit-code: option<s32>, signal: option<s32> }
    record process-result { stdout: string, stderr: string, exit: exit-info }

    /// DRAINING log read result. For non-draining cursor reads use `read-since`.
    record read-logs-result {
        stdout: string, stderr: string, running: bool, exit: option<exit-info>,
    }
    record kill-result {
        killed: bool, exit: option<exit-info>, stdout: string, stderr: string,
    }

    /// Request to spawn a host process. The five @1.0.0 fields keep identical
    /// position + meaning; the trailing NEW fields are honored ONLY by
    /// `spawn-persistent` and ignored by `spawn` / `spawn-background`.
    record spawn-request {
        cmd: string,
        args: list<string>,
        /// Optional stdin prelude. Capped 4 MiB cumulative.
        stdin: option<list<u8>>,
        env: list<env-var>,
        /// Workspace-relative cwd; abs/`..` rejected with `boundary-escape`.
        cwd: option<string>,
        /// NEW. Operator-readable label for `list-processes`/audit/`astrid ps`.
        /// Untrusted: host length-clamps (<= 128 B) + strips control chars.
        /// NOT identity — the `process-id` is the only identity. `none` =>
        /// derived from cmd.
        label: option<string>,
        /// NEW. Keep a writable stdin pipe open after the prelude so
        /// `write-stdin` works across invocations (REPL / psql / MCP-stdio).
        /// `none` => false (stdin closed after prelude, matching @1.0.0).
        keep-stdin-open: option<bool>,
        /// NEW. Ring overflow policy. `none` => drop-oldest (matches @1.0.0).
        overflow: option<overflow-policy>,
        /// NEW. Per-stream ring capacity (bytes). `none` => 1 MiB. Clamped to
        /// the profile ceiling.
        log-ring-bytes: option<u32>,
        /// NEW. Wall-clock lifetime ceiling from spawn. On expiry: SIGTERM ->
        /// grace -> SIGKILL -> reap. `none` => profile ceiling. Any value is
        /// clamped DOWN to the ceiling; a guest cannot request unbounded life.
        max-lifetime-ms: option<u64>,
        /// NEW. Reap an UNTOUCHED process (no read/wait/attach/signal/write)
        /// after this idle interval — the primary spawn-and-forget backstop.
        /// `none` => profile default. `some(0)` => `invalid-input`.
        idle-timeout-ms: option<u64>,
        /// NEW. After the child EXITS, retain its id + drained log tail this
        /// long before auto-reap (the "read its output later" window). `none`
        /// => profile default. Host-clamped. Bounds id accrual even if
        /// `release-process` is never called.
        exit-retention-ms: option<u64>,
    }

    /// NEW. Ring-overflow behaviour for a persistent stream.
    enum overflow-policy {
        /// Drop oldest bytes; cumulative loss surfaced as `bytes-dropped` so
        /// the capsule KNOWS its log is non-contiguous. Default; matches @1.0.0.
        drop-oldest,
        /// Backpressure: host stops draining the child's pipe when the ring is
        /// full, so the OS pipe buffer fills and the child blocks on write.
        /// Parks the host reader task only — never the WASM task or a tokio
        /// worker. Correct for REPL / MCP-stdio where dropping output corrupts
        /// framing. A child wedged this way is still bounded by `max-lifetime-ms`
        /// (idle-timeout will NOT fire — the child is "busy").
        backpressure,
    }

    /// NEW. Lifecycle phase, reported WITHOUT draining logs.
    enum process-phase {
        /// Spawn accepted; child not yet confirmed running.
        starting,
        running,
        /// Terminated; exit-info available; logs readable until released / TTL.
        exited,
        /// Exited AND retained id/buffers reaped. Terminal; id permanently dead.
        reaped,
    }

    /// NEW. Opaque, unforgeable, principal-scoped identity for a PERSISTENT
    /// process that survives instance churn. Wire form: a 256-bit host-minted
    /// CSPRNG token (host entropy, NEVER the guest), base32; the host stores
    /// only a keyed hash. Possession is necessary but NOT sufficient — the host
    /// re-checks the calling principal == creator on EVERY id-keyed call, so a
    /// leaked token is inert across the principal boundary. Treat as an opaque
    /// secret: never parse, pattern-match, or synthesize.
    type process-id = string;

    /// NEW. Opaque, host-encoded, resumable cursor into a process's log streams
    /// for NON-DRAINING reads (`read-since`). Encodes per-stream offset +
    /// generation, so a stale cursor after eviction/reap resolves to reported
    /// drops or `no-such-process`, never to another process's bytes.
    /// `token: none` on first read = from the oldest retained byte.
    record log-cursor { token: option<string> }

    enum log-stream { stdout, stderr }

    /// NEW. A non-draining slice of a stream, addressed by cursor. The
    /// polling-safe complement to `read-logs` (which DRAINS): multiple
    /// independent readers each keep a cursor and observe the full retained
    /// stream. `data` is `list<u8>` (children emit non-UTF-8).
    record log-chunk {
        data: list<u8>,
        /// Cursor for the NEXT `read-since` to resume exactly here. Monotonic.
        next: log-cursor,
        /// Cumulative bytes evicted before delivery through this cursor.
        bytes-dropped: u64,
        /// `true` once the child exited AND all retained output on this stream
        /// has been delivered through this cursor — the tailer's clean EOF.
        drained-eof: bool,
    }

    /// NEW. Non-draining status snapshot. Unit of `list-processes`/`status`.
    record process-info {
        id: process-id,
        label: string,
        /// cmd + args, pre-sandbox-wrap, as requested. Display/diagnostics.
        command: string,
        /// OS PID while running; `none` once reaped. Advisory ONLY (recycled).
        os-pid: option<u32>,
        phase: process-phase,
        exit: option<exit-info>,
        /// Host-monotonic ms since spawn (uptime).
        age-ms: u64,
        /// Ms since the last operation touched it (drives idle-timeout).
        idle-ms: u64,
        /// Bytes currently buffered/drainable (stdout + stderr).
        buffered-bytes: u64,
        /// Cumulative bytes evicted from the rings since spawn.
        bytes-dropped: u64,
        stdin-open: bool,
    }

    // ---- FROZEN @1.0.0 SURFACE (re-declared, identical semantics) ----

    /// A running or recently-terminated background process. Drop is automatic
    /// — the host reaps the child on resource drop. Per-principal concurrent
    /// cap applies.
    resource process-handle {
        /// Read buffered logs since last call. DRAINS.
        read-logs: func() -> result<read-logs-result, error-code>;
        write-stdin: func(data: list<u8>) -> result<u32, error-code>;
        close-stdin: func() -> result<_, error-code>;
        signal: func(sig: process-signal) -> result<_, error-code>;
        /// SIGKILL + drain. On a handle obtained via `attach`, this is IMMEDIATE
        /// (SIGKILL, no grace) and terminal — it reaps the underlying persistent
        /// process. For a graceful stop of a persistent process use the free
        /// `stop(id, grace-ms)`.
        kill: func() -> result<kill-result, error-code>;
        /// Wait for exit. On an `attach`ed handle a bounded timeout is REQUIRED
        /// (`none` => `invalid-input`): an unbounded wait would pin a pooled
        /// instance.
        wait: func(timeout-ms: option<u64>) -> result<exit-info, error-code>;
        wait-with-output: func(timeout-ms: option<u64>) -> result<process-result, error-code>;
        os-pid: func() -> result<u32, error-code>;
        /// Pollable; fires on exit. INSTANCE-LOCAL: dies on instance reset. For
        /// a persistent process, durable exit notice is the event model
        /// (`watch`), not this pollable.
        subscribe-exit: func() -> pollable;
        subscribe-logs: func() -> pollable;
    }

    /// Synchronous (blocking) process. Identical to @1.0.0.
    spawn: func(request: spawn-request) -> result<process-result, error-code>;
    /// EPHEMERAL background process (instance-owned; reaped on drop). Identical
    /// to @1.0.0. The new `spawn-request` fields are ignored here.
    spawn-background: func(request: spawn-request) -> result<process-handle, error-code>;

    // ---- NEW @1.1.0 PERSISTENT SURFACE ----

    /// Spawn a PERSISTENT background process whose lifetime is decoupled from
    /// the spawning instance. Returns a `process-id` reusable by any LATER
    /// invocation of the SAME capsule under the SAME principal.
    ///
    /// Fail-closed refusals (return `persist-unsupported`): the calling
    /// principal resolved via the capsule-OWNER fallback rather than an
    /// authenticated caller (a persistent process must have an unambiguous
    /// principal owner); the OS sandbox is disabled; or the host is macOS and
    /// the operator has not opted into macOS persistence (see Security —
    /// macOS lacks a PID-namespace reaper).
    ///
    /// Gated on `host_process` AND the operator `allow_persistent` sub-grant.
    /// Counts against BOTH the per-principal concurrent cap (shared with
    /// `spawn-background`) AND the retained-id cap. Honors the new
    /// `spawn-request` fields. Audit: cmd + args + cwd + label + id-hash.
    spawn-persistent: func(request: spawn-request) -> result<process-id, error-code>;

    /// Re-materialise a `process-handle` over a persistent process for THIS
    /// invocation — the composition primitive. DETACH-on-drop: dropping it
    /// (incl. on instance reset) does NOT reap the child; it detaches this
    /// instance's view. `no-such-process` if unknown to the caller.
    attach: func(id: process-id) -> result<process-handle, error-code>;

    /// List the calling `(capsule, principal)`'s persistent processes,
    /// optionally filtered by a label substring. NEVER returns another
    /// principal's or capsule's processes. Empty is normal (post-restart /
    /// post-reap recovery signal). Non-draining; bounded by the retained-id cap.
    list-processes: func(label-filter: option<string>) -> result<list<process-info>, error-code>;

    /// Non-draining status snapshot. The safe poll primitive. `no-such-process`
    /// if unknown to the caller.
    status: func(id: process-id) -> result<process-info, error-code>;
    /// Batch `status`. One registry pass + one audit record for a supervisor
    /// polling N children on a timer (avoids N lock-takes + N audit rows).
    /// Unknown ids are simply absent from the result.
    status-many: func(ids: list<process-id>) -> result<list<process-info>, error-code>;

    // id-keyed convenience fns — exact `attach(id)?.<method>()` sugar
    // (one code path, one principal check, one error mapping).

    /// `attach(id)?.read-logs()`. DRAINS.
    read-logs: func(id: process-id) -> result<read-logs-result, error-code>;
    /// NON-DRAINING, cursor-addressed, resumable read. Works on an exited
    /// process's retained tail until released/TTL. `max-bytes` host-capped.
    read-since: func(id: process-id, stream: log-stream, cursor: log-cursor, max-bytes: u32) -> result<log-chunk, error-code>;
    /// `attach(id)?.write-stdin(data)`. Requires `keep-stdin-open = true`.
    write-stdin: func(id: process-id, data: list<u8>) -> result<u32, error-code>;
    /// `attach(id)?.close-stdin()`.
    close-stdin: func(id: process-id) -> result<_, error-code>;
    /// `attach(id)?.signal(sig)`. Fire-and-forget.
    signal: func(id: process-id, sig: process-signal) -> result<_, error-code>;
    /// `attach(id)?.wait(timeout-ms)`. Bounded timeout REQUIRED. Does NOT
    /// release on exit. `wait-timeout` on elapse.
    wait: func(id: process-id, timeout-ms: u64) -> result<exit-info, error-code>;
    /// Graceful terminal stop: SIGTERM, wait up to `grace-ms`, SIGKILL; drain
    /// final buffers; REMOVE the id (frees concurrent + retained slot). One
    /// call instead of the signal+wait+kill dance. `grace-ms: none` => host
    /// default. (For an IMMEDIATE kill use `attach(id)?.kill()`.)
    stop: func(id: process-id, grace-ms: option<u64>) -> result<kill-result, error-code>;
    /// Drop the host's retention of an ALREADY-EXITED process: frees the
    /// retained-id slot + discards the buffered tail WITHOUT signalling.
    /// `invalid-input` if still running (use `stop`). Idempotent.
    release-process: func(id: process-id) -> result<_, error-code>;

    // ---- durable, event-driven notifications ----
    // A pollable dies on instance reset, so it cannot carry a cross-invocation
    // signal. The durable channel is the IPC bus the capsule already subscribes
    // to. (Topic-grammar + host-publish authority is the headline open question
    // — see Unresolved Questions.)

    /// Arm durable lifecycle events for `id`, published by the host UNDER THE
    /// CREATOR PRINCIPAL on `astrid.process.v1.{exited,log-ready,stdin-drained}.<suffix>`.
    /// The `exited` payload carries a REASON (self-exit / killed / reaped-ttl /
    /// reaped-shutdown) so a supervisor distinguishes a child crash from a
    /// daemon-shutdown reap. `log-ready` is coalesced (one in-flight per stream
    /// until drained). `suffix: none` => the id. The capsule reacts in a LATER
    /// invocation via its `[subscribe]` table — no pollable, no instance
    /// affinity. Idempotent. `no-such-process` if unknown to the caller.
    watch: func(id: process-id, suffix: option<string>) -> result<_, error-code>;
    /// Stop publishing events for `id` (child keeps running). Idempotent.
    unwatch: func(id: process-id) -> result<_, error-code>;
}
```

## Identity, ordering, and error contract (per symbol)

**`process-id`** — 256-bit token from the host CSPRNG (the same `OsRng`-backed source the host-random-source work landed; **never** the guest), base32. The host stores a hash keyed by a per-daemon-boot random key held only in the engine, plus `{creator: PrincipalId, capsule_id, label, spawned_at, last_touched, policy}`. Unguessability is *defense in depth*; the per-call principal/capsule check is the security boundary — a weaker RNG degrades gracefully, it does not collapse the model. IDs are never recycled within a daemon uptime and are **not** durable across a daemon restart (the in-memory registry is gone; all ids then resolve to `no-such-process`).

**`spawn-persistent`** — ordering: capability check (`host_process` + `allow_persistent`) → **authenticated-principal check** (refuse owner-fallback) → platform check (refuse macOS without opt-in) → cancel-token re-check → **atomic** `{concurrent-cap, retained-id-cap, registry insert, counter increment}` under one registry lock (closes the spawn TOCTOU where two instances for one principal both pass the cap) → sandboxed spawn. On any failure, no entry persists. Errors: `capability-denied` / `invalid-input` / `boundary-escape` / `quota` / `registry-full` / `persist-unsupported` / `cancelled` / `unknown`.

**`attach`** — principal check first; `no-such-process` on miss. Re-materialises a fresh detach-marked `process-handle` in the current instance's table. Valid only within the current invocation; storing it across a tool-call boundary is use-after-reset. `kill()` on it is terminal+reaping; all other methods are non-reaping; `wait(none)` => `invalid-input`.

**id-keyed fns** — each is exactly `attach(id)?.<method>()`: one principal check, one error mapping. `read-logs(id)` DRAINS. `wait(id, timeout-ms)` requires a bounded timeout (no `option`). `stop(id, grace-ms)` is the graceful terminal (SIGTERM→grace→SIGKILL→drain→remove); `release-process(id)` frees an *exited* entry without signalling (`invalid-input` on a running process; idempotent).

**`read-since`** — non-draining, cursor-addressed, resumable across invocations. Multiple readers each keep their own cursor over the same retained ring. Reports `bytes-dropped` (eviction before delivery) and `drained-eof`. The eviction count and the slice are read atomically under the entry lock. The cursor generation guards against ring-rotation/reap aliasing.

**`list-processes` / `status` / `status-many`** — resolve `(effective_principal, capsule_id)` *before* any filter; never widen scope. Non-draining. Wrong-owner / unknown / reaped all collapse to absence (or `no-such-process` for single-id), denying an existence oracle.

## Security model

- **Per-principal + per-capsule isolation (the hard mandate).** The registry is keyed `(capsule_id, PrincipalId, id-hash)`. Every id-keyed call re-resolves the live `effective_principal()` (per invocation — pooled instances multiplex principals) and `capsule_id` and compares against the recorded creator **before reading any field of the target**. `ManagedProcess.creator` already exists; `@1.1.0` makes it load-bearing for authz. A capsule never reaches another principal's or another capsule's process.
- **The spawn boundary is the real isolation risk, not attach.** `effective_principal()` falls back to the capsule owner (`default`) when there is no authenticated `caller_context` (load-time calls; run-loop capsules before the invocation context is set; the documented recv/poll fall-through). Minting a persistent id under that fallback would put several tenants' processes in a shared `default` namespace that `list-processes` would enumerate. Therefore **`spawn-persistent` refuses (`persist-unsupported`) unless the principal came from an authenticated caller** — a `has_authenticated_principal()` check distinct from `effective_principal()`'s lossy fallback.
- **ID as capability, non-enumerable.** A 256-bit CSPRNG secret defeats `read-logs(id+1)`-style probing; `invalid-id` is returned without a registry lookup (timing-oracle defense). A leaked token is still inert across principals.
- **Audit.** Every persistent op flows through `audit_process`, extended with op + principal + capsule + cmd/args/cwd + a **hash** of the id (never the raw id, env, stdin, or log contents). Reads are audited so the per-principal audit feed stays non-vacuous; `status-many` and audit-sampling keep a poll-on-a-timer supervisor from flooding the feed.
- **Sandbox lifetime (resolved, with caveats).** A wasmtime instance is a resource table inside the daemon address space; the child is spawned by the daemon's tokio runtime. The *only* thing reaping it on instance drop is `kill_on_drop(true)` + `Drop for ManagedProcess`. Persistence relocates `ManagedProcess` ownership into the engine-owned registry **before** the instance table resets, so `kill_on_drop` fires on registry teardown (explicit / TTL / capsule-unload / daemon-shutdown) instead. The child is spawned through the same `SandboxCommand::wrap` path (`managed.rs`): on **Linux** the inline arm emits `--unshare-all` + `--die-with-parent`, making `bwrap` PID-1 of a namespace so all descendants die with it regardless — the persistence guarantee is kernel-enforced even on a daemon `SIGKILL`. On **macOS** seatbelt has no PID namespace and no parent-death tie; `configure_piped` puts the child in its own process group (`killpg` covers in-group descendants) but a child that `setsid()`/`setpgid()`-escapes is unreapable by the registry, and a daemon `SIGKILL` leaves children running. macOS persistence is therefore **materially weaker** and is **fail-closed by default** (`persist-unsupported` unless the operator opts in). (Implementation note: the live sandbox path is the inline `SandboxCommand::wrap` arms, *not* `build_bwrap_prefix`/`build_seatbelt_prefix`; that arm currently hardcodes `--share-net`, so persistent children get network — the RFC's containment story owns that, and the two-codepath divergence is flagged for consolidation.)
- **Fail-secure defaults.** Persistence is opt-in (`spawn-persistent`, plus operator `allow_persistent`, default false — "may spawn ephemeral" and "may leave processes running" are different trust levels). Lifetimes are bounded by default. Errors are opaque. Children inherit the spawning profile and never gain capability; a principal's persistent processes are reaped on eviction (ephemeral + recursive).

## Lifecycle, limits, quotas

**Reap ladder** (first to fire wins): explicit `stop`/`release-process` (or `attach`ed `kill`) → child self-exit + `exit-retention-ms` → `max-lifetime-ms` → `idle-timeout-ms` → principal eviction (hard-reap all that principal's) → capsule full-unload (hard-reap via the existing `ProcessTracker::cancel_all` SIGINT→grace→SIGKILL ladder; **capsule-level**, only on the *last* instance, never on an instance reset — that would orphan-kill a sibling instance's children) → host-pressure GC (least-recently-touched victim) → daemon shutdown. **Explicitly never reaped on:** instance reset / pool return / dropping an `attach`ed handle. That is the whole fix.

**Quota accounting is a precondition, not a free bugfix.** Today `process_count_total` / `process_count_by_principal` live on `HostState` (per-instance). They are *correct today* only because the `host_process` carve-out pins the capsule to one never-reset instance (`reset_resources_on_return=false`; the `clear_on_return_preserves_resources_for_carveout` test asserts the count survives). The moment we **retire that carve-out** so process capsules rejoin the multi-instance pool, per-instance counters become wrong *and* resource-table-owned ephemeral handles would start getting reaped on return. So the registry relocation of both the counters and the persistent `ManagedProcess` ownership must land **atomically with** removing `reset_resources_on_return=false`. A capsule still using ephemeral `spawn-background` across invocations must either keep working (tested) or be explicitly required to migrate to `spawn-persistent`.

**Two caps, distinct errors.** (a) Per-principal **concurrent** cap `min(profile.max_background_processes, 8)`, shared ephemeral+persistent (a capsule can't double its allowance by mixing spawn kinds), error `quota`. (b) Per-principal **retained-id** cap (`max_persistent_processes` profile field; bounds live + exited-unreleased), error `registry-full` — the real backstop independent of any TTL.

**Buffers.** Per-stream ring 1 MiB default (`log-ring-bytes` overridable, profile-clamped). `drop-oldest` (lossy-but-live, surfaced as `bytes-dropped`) or `backpressure` (park reader, child blocks, `max-lifetime` backstop). A **new aggregate per-principal buffered-bytes ceiling** bounds total RAM across a principal's rings. A detached child's CPU/RAM is OS-sandbox-accounted, **not** charged to the WASM fuel/memory ledger (no WASM frame on the stack); what is bounded is the slot, retained id, buffered bytes, and lifetime.

## Host implementation sketch

A new `process/registry.rs` holds `PersistentProcessRegistry` (a sibling `Arc` of the proven `ProcessTracker`): `Mutex<HashMap<IdHash, RegistryEntry>>` (shard/`DashMap` if spawn-hot-path contention shows) + per-principal concurrent/retained/buffered counters. `RegistryEntry` wraps the existing `ManagedProcess` plus `{creator, capsule_id, label, command, spawned_at, last_touched, policy, ChildStdin, cursor offsets, watch_suffix}`. It is constructed once per `WasmEngine` (beside `ProcessTracker::new()`) and cloned into every pooled `HostState` (beside `process_tracker`). `ManagedProcess` gains a `ChildStdin` capture (the `@1.0.0` stub's missing piece for `write-stdin`/`close-stdin`), cursor offsets, and an ownership marker so `HostProcessHandle::drop` detaches (registry-owned) vs reaps (table-owned). A tokio-interval **reaper task** (at engine init, near the cancel-listener) enforces the TTL/idle/retention/host-pressure rungs; capsule-unload hooks `cancel_token.cancelled()`; principal eviction calls `registry.reap_principal(&PrincipalId)` (its chokepoint is an open question). Detached PIDs stay registered with `ProcessTracker` so cancel events still reach them. Profile gains `allow_persistent`, `max_persistent_processes`, `max_persistent_lifetime_secs`, `default_persistent_idle_secs` (untrusted-input: `#[serde(default)]` + upper-bound validation). Bindings register a second `add_to_linker` for `@1.1.0` alongside the untouched `@1.0.0`.

# Drawbacks
[drawbacks]: #drawbacks

- **New concurrency surface.** Retiring the carve-out means a process capsule's invocations now run on different pooled instances concurrently against the shared registry. The carve-out previously made per-capsule process access single-threaded *by construction*; the registry's per-entry + counter locking must be correct under genuine concurrency (concurrent `attach`+`stop` on one id; spawns racing the cap). A locking bug here is a use-after-reap or a quota bypass.
- **The detach-vs-reap-on-drop split is the sharpest footgun.** If `HostProcessHandle::drop` doesn't airtightly distinguish registry-owned (detach) from table-owned (reap), either persistent processes get SIGKILLed on every `attach`-then-reset (silently re-breaking the exact thing this fixes) or ephemeral handles leak. Needs an ownership marker + a tested ordering matrix.
- **macOS is materially weaker** (no PID namespace; setsid-escape; daemon-`SIGKILL` leaves children). Hence fail-closed by default there — but operators who opt in accept a real OS-process-leak vector under a hostile/buggy child.
- **No daemon-restart durability.** The registry is in-memory; a restart loses all ids. Deliberate (a process the daemon can't see can't be killed/audited/attributed), but MCP brokers / dev-server supervisors will hit it.
- **Capsule upgrade kills persistent processes** (capsule full-unload reaps). Every `astrid capsule upgrade` murders a long-running dev server. A defensible MVP cut, but operators hit it immediately; the `reparent-to-host` escape hatch is deferred.
- **Token-leak-via-transcript.** Shell returns the id in a tool message to the LLM; an id surfaced into a transcript a different principal reads is a leaked secret. The per-call principal check makes it inert across principals (defense in depth), but capsule authors must treat ids as secrets — an SDK doc burden.
- **Surface size + cognitive load.** One resource + ~16 free fns + several records/enums + 4 error cases — more than shell's minimum (deliberately). Operators now reason about a concurrent cap, a retained-id cap, an aggregate-buffer ceiling, and TTL/idle; conservative defaults must carry most users.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

- **Why a separate `spawn-persistent` (not a `persist: bool` on `spawn-background`)** — a flag overloads one function with two lifetime models and two incompatible drop-ownership meanings for the same `process-handle`, unauditable from the signature, and silently changes the frozen `@1.0.0` contract (breaking capsules that rely on drop-reaping as cleanup).
- **Why an opaque CSPRNG `string` id (not a `u64`/sequential/UUID)** — a sequential int lets `read-logs(id+1)` enumerate or hijack a sibling principal's process (the exact cross-principal reattach the mandate forbids, and the shape shell's stale source assumed). A bare UUID invites bearer-token treatment. The token is re-validated against the creator principal on every call (capability, not ambient name), and `string` lets the host evolve the encoding without a WIT change.
- **Why both `attach` AND id-keyed free fns** — free-fns-only forces duplicating every `@1.0.0` method and loses the ability to hold a handle across a multi-step invocation (write→read→wait) for MCP brokers; `attach`-only makes shell's single-shot tools needlessly verbose. So: `attach` as the load-bearing primitive (re-materialises the frozen resource), free fns as exact sugar.
- **Why event-driven `watch` (not durable pollables)** — a pollable is defined by `astrid:io/poll` as resource-table-bound and cancellation-raced; a "durable pollable" breaks that contract and reintroduces instance affinity under a new name. The IPC bus the capsule already consumes is the kernel-is-dumb-friendly durable channel (routing/scoping/audit already exist).
- **Why primitives, not host-side supervision/restart** — restart backoff / health-check / crash-loop detection is business logic (kernel-is-dumb), and a host auto-restart re-runs a command with no live principal frame to attribute fuel/quota to. `watch` + `status` + `stop` + `spawn-persistent` let a `capsule-supervisor` own the policy loop in capsule-space.
- **Why one shared concurrent budget** — a separate persistent budget would let a capsule spawn 8 ephemeral + 8 persistent = 16, splitting the security ceiling. One concurrent budget, plus a *separate* retained-id cap (different resource: live children vs retained ids/log-tails).
- **Deferred, not rejected:** `spill-to-vfs` overflow (turns a RAM leak into a disk leak; needs the `max_storage_bytes` interlock) and daemon-restart durability via re-adoption (re-adopting OS PIDs you can't prove you spawned is a much larger threat surface). Shipped scope is `drop-oldest`/`backpressure` + in-daemon-lifetime persistence.

# Prior art
[prior-art]: #prior-art

- **systemd user services + journald** — durable unit ids decoupled from the spawning shell; **journal cursors** are exactly the `read-since` non-draining, resumable, multi-reader model (the idea worth stealing).
- **tmux / screen** — session **reattach by name** across disconnects; the conceptual core of `attach(id)`.
- **Kubernetes Jobs/Pods + `kubectl logs --since`/`--follow`** — id-addressed logs, since-cursor, follow vs snapshot; informs `read-since` + `watch`.
- **supervisord / Erlang-OTP supervisors** — supervision trees and restart policy live *above* the spawn primitive — validates keeping supervision in capsule-space.
- **nohup / setsid / disown** — the classic detach; precisely what the sandbox must *prevent* a guest from doing uncontrolled (the macOS setsid-escape risk).
- **Cloudflare Durable Objects / browser Service Workers** — stateless invocation reattaching to durable host-side state by id; the architectural analogue of pooled-instance-reattaches-by-process-id.
- **WASI preview 2** — has **no** persistent/reattachable process model; this is a deliberate gap Astrid fills above the WASI substrate.
- **MCP stdio servers** — the canonical long-running child you talk to repeatedly; the flagship non-shell consumer driving `keep-stdin-open` + `backpressure` + `watch`.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **`watch` publish authority + topic grammar (headline).** The host publishes `astrid.process.v1.*` under the creator principal for a process that principal owns. Does this need a manifest `[publish]` declaration, or is it a distinct **kernel-authored** topic class (the host, not the capsule, is the authoritative publisher) that the capsule only `[subscribe]`s to? The post-#863 unification made `[publish]`/`[subscribe]` tables the only IPC-intent surface; an implicit host-publish-on-behalf must be blessed by the topic-grammar work ([astrid#809]). **Fallback if unresolved:** drop `watch` from the MVP and have supervisors poll `status`/`status-many` + bounded `wait` — slightly less efficient, no new authority question.
- **macOS reaper hardening.** Ship persistence on macOS only behind the operator opt-in with best-effort `killpg` + daemon-shutdown sweep, or mandate a stronger reaper (periodic process scan) first? Default here: opt-in, documented best-effort.
- **Principal-eviction chokepoint.** The reap ladder needs a single place to call `registry.reap_principal(&PrincipalId)`. Spawn/unload were traced; the eviction path was not confirmed to be a single chokepoint — if it isn't, eviction-reap degrades to TTL-only.
- **`allow_persistent` sub-grant.** Confirm persistence warrants an operator sub-grant layered on `host_process` (assumed yes — bigger blast radius) vs single-capability gating.
- **Profile defaults** (`default_persistent_idle_secs`, `max_persistent_lifetime_secs`, `max_persistent_processes`, aggregate-buffer ceiling) — exact values are operator policy, not ABI constants; confirm they live in `Quotas`.
- **`@1.0.0` concurrent-counter relocation.** Route the `@1.0.0` `spawn-background` per-principal count through the shared registry (a guest-visible-WIT-preserving behavioral fix to the `@1.0.0` host path) — recommended, flagged as a behavioral change.
- **Per-principal child OS limits** (`RLIMIT_NPROC`/`RLIMIT_AS`/cgroup on the sandbox) — MVP precondition or hardening follow-up? Leaning follow-up (slot+retained+buffer+TTL is a sufficient initial bound).
- **Unikernel.** `astrid:process` is desktop-kernel-only; `@1.1.0` inherits this. MCP-stdio brokers on the unikernel target get nothing — acceptable, but stated.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Daemon-restart durability** — a persisted id→pid map + re-adoption on startup, for brokers/supervisors that want cross-restart resilience (separate RFC; large threat surface).
- **`reparent-to-host` / capsule-survival** — a keep-alive policy so an in-place capsule upgrade doesn't kill a dev server; needs a host supervisor owning processes with no capsule and no live principal frame (an attribution model that doesn't exist yet).
- **`spill-to-vfs` overflow** — contiguous build/test logs past the ring, under the principal's tmp mount, counted against `max_storage_bytes`.
- **Cross-capsule attach under one principal** — an explicit, audited capability for a future scheduler/supervisor capsule to reach a process spawned by a *different* capsule under the same principal (capsule-scoped by default today).
- **`astrid ps` / `astrid top` ops tooling** — `list-processes`/`status` are the capsule-space half; a fleet-wide admin view across all principals is the operator half (belongs to the admin API, not a capsule capability; ties to the live-activity work).
- **`ProcessSupervisor` SDK convenience** — restart/stop/tail helpers + name→id persistence in capsule KV, built *on* these primitives, shipped as a later SDK minor once the primitives are proven.
