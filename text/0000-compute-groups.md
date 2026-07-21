- Feature Name: `compute_groups`
- Start Date: 2026-07-21
- RFC PR: N/A (draft)
- Tracking Issue: N/A

# Summary
[summary]: #summary

This RFC defines an Astrid-owned parallel-compute boundary. A capsule may open
a principal-scoped compute group backed by one signed core-WebAssembly worker
artifact, a host-owned shared linear-memory region, and a resolved number of
worker instances. The capsule remains the control plane; worker code performs
the hot loop directly against shared memory. The kernel validates, schedules,
meters, cancels, and accounts for workers without learning Linux, tensor,
rendering, compiler, inference, or game-engine semantics.

The WIT in this RFC is a pre-1.0 draft. Astrid may implement and test against
the draft on feature branches, but the corresponding WIT PR remains unmerged
until the Astrid 1.0 contract freeze. Nothing in this document declares the
package shipped or immutable before that point.

# Motivation
[motivation]: #motivation

Astrid capsules are WebAssembly Components. A Component invocation is an
excellent capability boundary, but it is not itself a generic way to execute
capsule-authored code concurrently. A host function can start a native thread,
but a host function cannot receive a guest closure. Adding host functions such
as `spawn-vcpu`, `multiply-tensor`, or `render-tile` would therefore either be
fake parallelism or would move application logic into the kernel.

Several concrete consumers need the same lower-level mechanism:

- a Linux realm needs multiple virtual CPUs sharing guest RAM;
- tensor and inference runtimes need workers sharing tensors and queues;
- games and media pipelines need render, simulation, decode, and audio jobs;
- compilers need parallel front ends, code generation units, and link jobs;
- ordinary capsules need CPU parallelism without escaping to a host process.

The abstraction must also preserve Astrid's authority model. Alice's worker,
memory, accounting, cancellation, and results must never become visible to Bob.
An operator must be able to lower concurrency and shared-memory allowances for
a managed deployment. A local personal deployment should not acquire an
arbitrary Astrid-specific thread ceiling: `auto` should use the host's useful
parallelism, while explicit requests are limited only by declared operator and
principal policy plus actual host resource availability.

The expected outcome is one reusable primitive behind an audited
`astrid:compute@1.0.0` contract. Linux is the first demanding consumer, not a
special kernel path.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## The two artifacts

A compute-enabled capsule contains two kinds of WebAssembly artifact:

1. Its normal WebAssembly Component. This receives IPC, uses Astrid host
   interfaces, opens groups, submits work, and handles results.
2. One or more core-WebAssembly compute workers. A worker imports only a shared
   memory and exports a fixed Astrid compute ABI. It has no ambient WASI or
   Astrid host imports.

The worker is ordinary capsule-authored code. Astrid does not contain its
algorithm. The worker can implement a vCPU loop, a tensor kernel, a render
worker, or a compiler stage.

```toml
[[component]]
id = "controller"
file = "controller.wasm"
type = "executable"

[[component]]
id = "cpu"
file = "assets/cpu-worker.wasm"
type = "compute-worker"
hash = "blake3:<digest>"
```

The package signature covers both artifacts. `hash` additionally binds the
manifest entry to the exact worker bytes loaded by the runtime. The
`compute-worker` declaration is itself the scoped capability: it grants the
controller access to that id and digest, never to arbitrary worker bytes.

## Opening a group

The controller imports `astrid:compute/host@1.0.0` and requests a group:

```text
worker:              "cpu"
parallelism:         auto
initial-memory-pages: 256
maximum-memory-pages: 4096
mode:                parallel
```

`auto` means the host's useful parallelism, reduced by any operator ceiling and
by reservations already held by the same principal. It does not mean a fixed
Astrid constant. `exact(8)` requests eight workers and fails if the applicable
policy cannot admit all eight; it is never silently reduced. `at-most(8)`
accepts a lower effective value and reports it in `group-info`.

The returned group owns a shared memory and N persistent worker instances.
Every worker imports that same memory. Each worker instance has a distinct
`worker-index` in `[0, N)` and processes at most one submitted job at a time.

## Shared data and descriptors

The controller may copy setup data into or out of the shared region through
bounded `read` and `write` calls. Those calls are deliberately not the hot
path. A submitted `work-descriptor` is a small `(offset, length, tag)` reference
to a capsule-defined structure already in shared memory. Workers exchange bulk
data, queues, and synchronization state directly through WebAssembly shared
memory and atomic instructions.

The fixed worker export receives only the worker index and descriptor fields:

```text
astrid_compute_abi_version() -> i32
astrid_compute_run(worker_index: i32,
                   descriptor_offset: i64,
                   descriptor_length: i64,
                   descriptor_tag: i64) -> i32
```

The return value is an application-defined status code. Astrid reports it but
does not interpret it.

## Submitting and joining

A submission chooses either one available worker or a specific worker index.
The latter supports stable vCPU-to-hart identity and other affinity-sensitive
workloads. The call returns a `job` resource immediately. A controller can
poll status, obtain an Astrid pollable, cancel the job, or join it.

Group cancellation first increments the shared cancellation generation and
wakes atomic waiters, allowing workers to stop cooperatively. Individual job
cancellation is represented by its host-owned token. In both cases the host's
25 ms epoch cadence forcibly interrupts code that does not cooperate. Dropping
a group cancels every job and joins every worker before releasing the
principal's reservation.

## Deterministic and parallel modes

`parallel` uses the resolved worker count. `deterministic` resolves to one
worker and preserves FIFO submission order. Deterministic mode is intended for
replay, debugging, differential tests, and workloads whose result order is
part of their contract. It is not a claim that an arbitrary floating-point
worker becomes bit-for-bit deterministic.

## What the agent sees

The agent normally sees an SDK API resembling a scoped executor, not native
threads and not Wasmtime:

```rust
let group = ComputeGroup::open(
    "cpu",
    GroupOptions::new()
        .parallelism(Parallelism::Auto)
        .shared_memory(256, 4096),
)?;

let job = group.submit(Work::at(offset, len).tag(BOOT_VCPU))?;
let result = job.join()?;
```

Linux may present its own shell and process model inside this mechanism. That
does not make Linux processes Astrid workers. The three counts remain distinct:

- Linux guest tasks are scheduled by Linux;
- Linux vCPUs are compute jobs pinned to Astrid workers;
- Astrid workers are host-scheduled WebAssembly executions.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Contract package

The proposed canonical contract is `host/compute@1.0.0.wit`, package
`astrid:compute@1.0.0`, interface `host`. Before Astrid 1.0 this file remains on
an unmerged contract branch and may change together with this RFC and its
experimental implementations.

### Types

`execution-mode` has two cases:

- `deterministic`: exactly one worker; FIFO dispatch.
- `parallel`: use the resolved parallelism.

`parallelism` has three cases:

- `auto`: choose useful host parallelism within policy.
- `exact(u32)`: admit exactly this many workers or return `quota`.
- `at-most(u32)`: admit at least one and no more than this many workers.

Zero in either numeric case is `invalid-input`.

`group-request` contains:

- `worker: string`: manifest component id of type `compute-worker`.
- `mode: execution-mode`.
- `parallelism: parallelism`.
- `initial-memory-pages: u32`.
- `maximum-memory-pages: u32`.

Memory pages are 65,536 bytes. Initial pages must be non-zero and no greater
than maximum pages. Both admission and growth are charged to the principal.

`group-info` contains the immutable worker id, mode, resolved worker count,
current pages, maximum pages, queued jobs, running jobs, and cumulative
accounting.

`work-descriptor` contains:

- `offset: u64` and `length: u64`, a range in current shared memory;
- `tag: u64`, opaque to the host;
- `worker-index: option<u32>`; `none` means next available worker;
- `fuel: option<u64>`; a caller may self-limit below operator policy. `none`
  uses the operator maximum when configured, or remains uncapped by Astrid
  policy. A value above the operator maximum is denied.

The range is validated at submit time with checked arithmetic. A later memory
grow cannot invalidate it. The host does not read or interpret descriptor
bytes.

`job-state` is `queued`, `running`, `completed`, `cancelled`, or `failed`.

`job-result` contains the worker's signed 32-bit status, fuel consumed,
wall-clock nanoseconds, and terminal state. A trapped or forcibly interrupted
worker returns `worker-failed` rather than an application status.

`accounting` contains workers reserved, current and peak shared-memory bytes,
jobs submitted/completed/cancelled/failed, and cumulative fuel. Values are
attributed to the authenticated principal by the host and cannot be selected
by the guest.

### Errors

`error-code` has these cases:

- `capability-denied`: capsule has no valid declared `compute-worker` objects.
- `invalid-input(string)`: malformed request, range, or zero count.
- `no-such-worker`: unknown id or a component not typed `compute-worker`.
- `worker-invalid(string)`: hash, validation, imports, exports, memory type, or
  ABI version mismatch.
- `quota`: worker or shared-memory admission exceeds effective policy.
- `busy`: a specifically addressed worker already has a queued or running job.
- `cancelled`: operation raced group, capsule, principal, or kernel shutdown.
- `worker-failed(string)`: compilation, instantiation, trap, or forced
  interruption. Detail is diagnostic and must not be parsed.
- `closed`: group or job has reached a terminal dropped state.
- `too-large`: a WIT read/write exceeds the per-call transfer ceiling.
- `unknown(string)`: non-classified host error.

The host must not reveal whether a worker belongs to another principal or
capsule. Worker lookup is restricted to the calling capsule's immutable signed
catalog and returns the same `no-such-worker` result as an unknown id.

### Functions and resources

`open(request) -> result<compute-group, error-code>` performs capability,
manifest, hash, ABI, memory, and quota validation before starting threads. It
is atomic from the caller's perspective: a failure leaves no reservation or
worker behind.

`compute-group.info()` is non-blocking.

`compute-group.read(offset, length)` and `write(offset, bytes)` copy setup or
result bytes. Each call is capped at 1 MiB. Concurrent access is allowed, but
the capsule is responsible for synchronization with running workers. These
calls have no implied memory fence beyond the WebAssembly/host atomic accesses
used by the implementation.

`compute-group.grow(delta-pages)` grows shared memory without relocating its
base address and returns the prior page count. It reserves the additional
bytes before growth and rolls the reservation back on failure.

`compute-group.submit(descriptor) -> job` validates and queues one call.
Deterministic groups are FIFO. In parallel groups, each worker queue is FIFO,
but untargeted jobs may be assigned to different workers and therefore have no
global completion-order guarantee. No fairness is promised between different
groups, but the runtime must prevent one principal from consuming another
principal's reserved worker slots.

`compute-group.cancel()` is idempotent and applies to queued and running jobs.
No new submissions are accepted afterward.

`job.status()` is non-blocking. `job.subscribe()` returns
`astrid:io/poll.pollable`. It becomes ready exactly when the job is terminal.
`job.join()` waits until terminal and returns `job-result` or a typed host
error. `job.cancel()` is idempotent.

Dropping a `job` releases only the observer handle; it does not silently cancel
the work. Dropping the owning group cancels all work. This avoids accidental
cancellation when SDK values move or a transient job handle is discarded.

## Worker artifact ABI

A `compute-worker` is a core Wasm module, not a Component. It must:

- import exactly one memory as `astrid_compute`, `memory`;
- declare that memory `shared` with a maximum;
- have no other imports;
- export `astrid_compute_abi_version: () -> i32` returning `1`;
- export `astrid_compute_run: (i32, i64, i64, i64) -> i32`;
- use memory32 for ABI 1; descriptor values wider than the addressable range
  are rejected before dispatch.

The imported memory's minimum must be no greater than the requested initial
pages. Its maximum must be no less than the requested maximum pages. The host
creates one `SharedMemory` per group and instantiates the module once per
resolved worker using separate Stores.

The first 64 bytes of shared memory are reserved for Astrid Compute ABI 1:

| Offset | Width | Meaning |
|-------:|------:|---------|
| 0 | 4 | magic `0x41534331` (`ASC1`) |
| 4 | 4 | ABI version (`1`) |
| 8 | 4 | cancellation generation, atomic u32 |
| 12 | 4 | group state, atomic u32 |
| 16 | 8 | resolved worker count |
| 24 | 8 | current memory bytes |
| 32 | 32 | reserved for a future ABI-1-compatible extension; zero |

User descriptors must start at offset 64 or later. Workers poll the
cancellation generation or use `memory.atomic.wait32` at offset 8. Astrid
increments and notifies that word on cancellation.

## Scheduling implementation requirements

The native host uses one bounded worker thread per admitted worker. Each thread
owns its Store and worker instance; Stores are never invoked concurrently.
Threads receive jobs over bounded queues. The runtime may replace OS threads
with equivalent isolated execution agents on another Astrid kernel, provided
the observable contract is unchanged.

Host calls occur at group/job boundaries only. The runtime must not cross a
host function for every worker instruction or shared-memory access.

The engine enables WebAssembly threads, shared memory, fuel consumption, and
epoch interruption. Each job receives a fresh fuel allowance. Fuel consumed is
charged to principal-attributed compute accounting. Cancellation and shutdown
use epoch interruption after the cooperative signal. A trapped worker closes
the group: its queued work and concurrently running sibling work are
cancelled, and new submissions return `closed`. ABI 1 does not silently
re-instantiate a failed stateful worker.

## Capability and manifest rules

Each `[[component]] type = "compute-worker"` is an object-scoped capability
grant for that component id and hash. A capsule with no declared workers has no
compute host authority; capability introspection reports `compute` when at
least one valid declaration exists. This grants neither host processes,
arbitrary Wasm bytes, network, filesystem, IPC, device, nor driver access.

The executable controller is the first `[[component]]` whose `type` is empty or
`executable`. A `compute-worker` entry is never instantiated as a Component.
Every referenced path is resolved beneath the installed capsule root, rejects
symlinks and traversal, and is verified against its manifest hash before
compilation. Pack tooling must include every referenced compute worker and fail
if it is absent.

## Principal isolation and accounting

The host obtains the principal from kernel-stamped invocation state. It never
accepts a principal in WIT input.

Worker and current/peak shared-memory reservations are aggregated per
principal across all capsules. Effective limits are:

```text
worker allowance = operator max if configured, otherwise no Astrid policy cap
memory allowance = operator max if configured, otherwise no Astrid policy cap
```

For `auto`, useful host parallelism is the starting request. For `at-most`, it
is the numeric maximum. Both are reduced by remaining allowance. `exact` is
never reduced. Physical allocation failure remains possible even with no
policy ceiling and returns `worker-failed` or `quota` as appropriate.

A reservation is acquired before memory or threads are created and is held by
an RAII token. Every error and panic path must release it. Principal teardown,
capsule unload, upgrade, and daemon shutdown cancel and join groups before the
reservation is released.

Shared memory, module instances, queues, job ids, cancellation tokens, and
accounting objects are never reused across principals. Package bytes and
compiled immutable `Module` objects may be cached across principal views of
the same content hash.

## Operator configuration

The native daemon accepts these optional settings in `[capsule]`, with CLI
flags taking precedence:

```toml
[capsule]
compute_max_workers_per_principal = 8
compute_max_shared_memory_bytes_per_principal = 8589934592
compute_max_job_fuel = 5000000000
```

Absent worker and shared-memory settings mean no Astrid policy cap. Physical
host allocation can still fail. Explicit zero is invalid; disable compute by
shipping no compute-worker declarations. Absent job-fuel setting means no
arbitrary Astrid fuel cap; epoch interruption still makes cancellation and
shutdown enforceable.
Environment aliases follow the existing `ASTRID_CAPSULE_*` configuration
mapping.

## Lifecycle

ABI 1 groups are capsule-lifetime resources. They may survive multiple
messages only when owned by a long-lived run-loop instance; pooled interceptor
instances lose their resource table after each invocation and therefore close
their groups. Durable named groups reattachable across Store replacement are
deferred to a versioned successor because their identity, crash recovery, and
upgrade semantics require a host registry analogous to persistent processes.

Groups never survive capsule unload, capsule upgrade, principal removal, or
daemon restart. Workloads needing restart persistence checkpoint state through
an explicit capsule storage capability.

## Audit

Audit records are emitted for group open/close, memory grow, submit, terminal
job outcome, cancellation, quota denial, worker validation failure, and forced
interruption. Records include principal, capsule, worker id/hash, resolved
parallelism, memory pages, descriptor length/tag (not descriptor bytes), fuel,
duration, and outcome.

High-frequency shared-memory loads/stores and atomics are not individually
audited. Their group and job envelope is the auditable authority boundary.

## Required tests

An implementation is not conforming without tests that prove:

- two worker instances observe the same shared memory;
- actual overlap occurs on at least two native threads;
- deterministic mode preserves FIFO order and uses one worker;
- exact, at-most, and auto resolution obey policy;
- cross-capsule reservations aggregate for one principal;
- Alice cannot read, join, cancel, or infer Bob's group/job;
- malformed imports, exports, hashes, paths, and ABI versions fail closed;
- descriptor overflow and reserved-header overlap are rejected;
- fuel exhaustion, trap, cancellation, capsule unload, and daemon shutdown
  terminate work and release reservations;
- a panic or partial thread-start failure rolls back every reservation;
- no worker import other than the shared memory can instantiate;
- per-call read/write bounds and shared-memory growth bounds are enforced;
- a compute-enabled capsule gains no fs, net, process, IPC, or driver effect;
- measured parallel speedup and open/submit/join overhead are reported against
  deterministic mode without promising a fixed ratio.

# Drawbacks
[drawbacks]: #drawbacks

This adds a second WebAssembly execution shape beside Components. Core workers
need a small ABI and a separate build path. Shared memory necessarily exposes
data races inside a group; Astrid confines the race to one principal and
capsule but cannot make incorrect worker synchronization safe.

Wasmtime's shared-memory support has different resource-limiter and pooling
constraints from ordinary Component memories. Astrid must meter it explicitly
and cannot assume the existing Store limiter accounts for it.

One native thread per admitted worker has predictable isolation but a real
stack and scheduler cost. A future implementation may use a shared executor,
but it must preserve Store exclusivity and the contract's admission semantics.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why signed worker modules?** A host cannot invoke a guest closure received
through WIT. A declared module is independently validatable, cacheable, and
instantiable in one Store per worker while keeping the algorithm in user space.

**Why core Wasm instead of another Component?** Shared linear memory and atomic
instructions are a core-Wasm facility. Component resources are ideal for the
control plane but do not currently provide shared-memory composition between
independently executing component instances.

**Why no host imports in workers?** It makes the worker a pure compute unit.
Effects remain on the audited controller boundary, prevents capability
amplification, and permits more aggressive scheduling and replay.

**Why not WASI threads?** Astrid intentionally owns its host ABI and targets
`wasm32-unknown-unknown`. Importing a WASI runtime would bypass Astrid's
principal, capability, audit, and portability boundaries.

**Why not host processes, QEMU, or containers?** They remain useful backends and
development tools, but they are larger authority domains, have different
resume and accounting behavior, and do not create a portable Astrid primitive.

**Why not make Linux the primitive?** Tensor, render, compiler, and inference
workers do not need a guest kernel. Making them boot Linux would add overhead
and make Linux the accidental policy layer.

**Why not expose native threads directly?** A native closure pointer is neither
portable nor safe across the Wasm boundary. Hiding native threads behind
compute groups lets a future bare-metal Astrid kernel use different machinery
without changing capsules.

**Why not treat the normal instance pool as compute?** The pool serves
independent Component invocations with separate memories. It neither supplies
shared memory nor stable worker identity and is tuned for request concurrency,
not a parallel hot loop.

# Prior art
[prior-art]: #prior-art

- WebAssembly threads define shared linear memory, atomics, wait, and notify.
- Wasmtime provides `SharedMemory` that can be cloned into modules instantiated
  in separate Stores; Astrid uses that mechanism but owns scheduling, policy,
  metering, and lifecycle.
- Web Workers separate a message-oriented control plane from shared-memory
  workers, but browser origin policy is not Astrid principal policy.
- CUDA command queues and GPU kernels separate dispatch descriptors from a hot
  device data path. Compute groups follow the same control/data-plane split
  without making the initial ABI GPU-specific.
- Plan 9 keeps resources explicit and nameable rather than smuggling ambient
  authority into processes. Compute groups similarly make the execution and
  shared-memory relationship an explicit resource.
- Linux cgroups and job objects motivate aggregate accounting and teardown, but
  the Astrid unit is a cryptographic principal and signed capsule artifact.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should the first accepted ABI standardize a helper crate for the reserved
  memory header, or only its byte layout?
- Should a future opt-in request allow clean re-instantiation after a trapped
  stateful worker, rather than ABI 1's fail-closed group teardown?
- Should shared-memory accounting contribute to the existing memory telemetry
  high-water field or a distinct compute-memory field (the enforcement must be
  aggregate either way)?
- The Component Model's future native task/thread facilities may eventually
  express part of this contract. Adoption must preserve the Astrid package and
  authority semantics rather than expose an engine-specific API.

# Future possibilities
[future-possibilities]: #future-possibilities

- Durable named groups with checkpoint/restore and reattachment.
- A package build target and Rust `no_std` worker crate that emits and hashes
  `compute-worker` artifacts automatically.
- GPU queues and device memory behind a sibling `astrid:device` contract, with
  compute groups submitting portable kernels where the device supports them.
- Driver capsules whose data plane is a compute worker and whose effects cross
  a narrow audited device capability.
- Structured collectives (`broadcast`, barriers, reductions) implemented as an
  SDK library over the shared ABI header, not kernel algorithms.
- Tensor-logic planning that chooses group topology and descriptors while this
  RFC remains the execution substrate. No tensor semantics are activated by
  ABI 1.
- Bare-metal Astrid scheduling compute workers as native kernel tasks while
  preserving the same WIT and worker ABI.
