- Feature Name: `interceptor_chain`
- Start Date: 2026-03-22
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#585](https://github.com/unicity-astrid/astrid/issues/585)

# Summary
[summary]: #summary

Define the interceptor middleware chain: priority-ordered event dispatch with
short-circuit semantics. Interceptors return `Continue`, `Final`, or `Deny` to
control the chain. A guard at priority 10 can veto an event before the core
handler at priority 100 ever processes it.

# Motivation
[motivation]: #motivation

Astrid capsules register interceptors on IPC topics to handle events. Before
this RFC, all matching interceptors fired unconditionally in undefined order.
This creates two problems:

1. **No ordering guarantees.** An input validation capsule and the main ReAct
   loop both intercept `user.v1.prompt`. Which runs first? Without ordering,
   the ReAct loop might process malicious input before the validator sees it.

2. **No short-circuit capability.** Even if a validator runs first, it cannot
   stop the ReAct loop from also processing the event. There is no "deny" or
   "handled" signal. Every interceptor fires regardless of what earlier
   interceptors decided.

Together, these make it impossible to build a layered security/middleware
stack where guards protect core business logic. The pattern is foundational —
web servers (Express, Koa), network stacks (netfilter, Envoy), and game
engines (Bevy ECS) all solve it the same way: ordered execution with the
ability to halt the chain.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## Priority

Every interceptor has a priority (default 100, lower fires first):

```toml
[[interceptor]]
event = "user.v1.prompt"
action = "guard_input"
priority = 10

[[interceptor]]
event = "user.v1.prompt"
action = "handle_prompt"
priority = 100
```

Priority 10 fires before priority 100. Priority 0 fires before everything.
The default of 100 means existing capsules that don't specify priority continue
to work — they're just "normal" priority, and guards can be layered in front.

## Chain semantics

When an event matches interceptors across multiple capsules, the dispatcher
runs them sequentially in priority order. Each interceptor returns one of:

- **Continue** — "I'm done, pass the event to the next interceptor." The
  interceptor can optionally modify the payload — the modified version is
  what the next interceptor receives.

- **Final** — "I've handled this event. Stop the chain." No further
  interceptors fire. Use case: a cache hit at priority 30 returns a cached
  response, skipping the LLM call at priority 100.

- **Deny** — "This event is rejected. Stop the chain." No further
  interceptors fire. The reason is logged for audit. Use case: an input guard
  at priority 10 blocks prompt injection.

If an interceptor errors (crash, timeout, `NotSupported`), the chain
continues — a broken capsule should not block the entire pipeline.

## What this guarantees

The core capsule (ReAct loop, priority 100) only ever sees events that have
passed every higher-precedence guard. If a guard at priority 10 denies, the
core never runs. If a transform at priority 50 modifies the payload, the core
sees the modified version.

This is the kernel-level guarantee that "core always works" — the core
processes only clean, vetted, sanitized events.

## Backward compatibility

Existing capsules return empty bytes from `astrid_hook_trigger`. Empty bytes
are treated as `Continue` with no payload modification. No existing capsule
breaks.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Priority field

Added to `[[interceptor]]` in `Capsule.toml`:

```toml
[[interceptor]]
event = "user.v1.prompt"
action = "guard_input"
priority = 10   # optional, default 100
```

Type: `u32`. Lower values fire first. Default: `100`.

## InterceptResult wire format

The WASM guest's `astrid_hook_trigger` function returns raw bytes. The kernel
decodes them as `InterceptResult` using a discriminant byte prefix:

| First byte | Meaning | Remaining bytes |
|---|---|---|
| `0x00` | Continue | Modified payload (may be empty) |
| `0x01` | Final | Response payload |
| `0x02` | Deny | UTF-8 reason string |
| (empty) | Continue | Backward compatible — no modification |
| (other) | Continue | Forward compatible — full bytes as payload |

The discriminant byte is stripped before the payload is used. An empty return
(zero bytes) is `Continue` with no modification — this is the backward
compatibility path for all existing capsules.

Unknown discriminant values are treated as `Continue` with the full bytes
(including the unknown byte) as the payload. This provides forward
compatibility — future result types won't break old kernels.

## Dispatch algorithm

```
matches = find_matching_interceptors(topic)  // sorted by priority ascending

if matches.len() == 1:
    // Fast path: per-capsule ordered queue (preserves IPC seq ordering)
    dispatch_single(matches[0])
    return

// Multi-interceptor chain: sequential in priority order
let mut payload = event_payload
for (capsule, action) in matches:
    match capsule.invoke_interceptor(action, payload):
        Continue(modified) =>
            if modified.is_not_empty():
                payload = modified
        Final(response) =>
            log(debug, "chain halted by Final")
            return
        Deny { reason } =>
            log(warn, "chain halted by Deny: {reason}")
            return
        Err(NotSupported) =>
            continue  // capsule doesn't participate
        Err(e) =>
            log(warn, "interceptor failed: {e}")
            continue  // don't let broken capsule block chain
```

### Single-interceptor fast path

When only one interceptor matches (the common case), the event goes through
the existing per-capsule mpsc queue. This preserves IPC `seq` ordering within
a capsule — events arrive in publish order. No chain overhead.

### Multi-interceptor chain

When multiple interceptors match, they run as a sequential chain in a spawned
async task. The dispatcher loop does not block — the chain executes
independently. Within the chain, interceptors run synchronously in priority
order (the whole point is deterministic ordering).

### Error handling

Interceptor errors (WASM trap, timeout, plugin lock poisoned) continue the
chain. Rationale: a buggy guard capsule should degrade to "no guard" rather
than blocking the entire event pipeline. The error is logged at warn level
for investigation.

The counter-argument is that a failed security guard should fail-closed (deny
the event). This is an unresolved question — the current choice prioritizes
availability over security. A future `fail_mode` field on `[[interceptor]]`
could make this configurable per interceptor.

## SDK surface

The SDK exposes `InterceptResult` for capsule authors who want chain control:

```rust
#[astrid::interceptor("guard_input")]
fn guard(payload: &[u8]) -> InterceptResult {
    if is_malicious(payload) {
        InterceptResult::deny("prompt injection detected")
    } else {
        InterceptResult::continue_with(payload)
    }
}
```

Capsules that don't need chain control continue returning `Vec<u8>` or `()`.
The SDK wraps these as `Continue` automatically.

# Drawbacks
[drawbacks]: #drawbacks

- **Sequential execution cost.** Multi-interceptor events run sequentially,
  not concurrently. A slow interceptor at priority 10 delays everything
  behind it. Mitigated by the single-interceptor fast path (most events).

- **Implicit ordering coupling.** Capsule authors must know what other
  capsules exist and their priorities to choose meaningful priority values.
  There is no formal "priority registry."

- **Error-continues-chain is controversial.** A failed security guard that
  continues the chain means the core processes unguarded input. This is the
  availability-over-security trade-off.

- **Payload modification semantics are loose.** `Continue` can return modified
  bytes, but "modified" is undefined at the IPC level. Does a modified prompt
  still carry the original session context? This is left to convention.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why not fire all interceptors concurrently?

The previous design dispatched all matching interceptors concurrently through
per-capsule queues. This is faster but prevents:
- Deterministic ordering (which guard runs first?)
- Short-circuit (how does a guard stop the core from running?)
- Payload modification (the core sees the original, not the modified version)

Sequential execution is the only way to provide middleware chain semantics.

## Why a discriminant byte instead of JSON?

JSON parsing adds overhead to every interceptor return. The discriminant byte
is a single branch — `match bytes[0]`. The remaining bytes are passed through
without parsing. This is the hot path (every IPC event).

For comparison, Linux netfilter uses integer verdicts (`NF_ACCEPT = 1`,
`NF_DROP = 0`). Envoy uses enum filter status. The pattern is always "small
fixed discriminant + payload."

## Why default priority 100 instead of 0?

If the default were 0, every existing capsule would be highest priority.
Adding a guard would require negative priorities or changing every existing
capsule. Default 100 leaves room for guards (0-99) and allows "lower than
default" interceptors (101+) for post-processing.

## Why not named priority levels (HIGH, MEDIUM, LOW)?

Named levels are easier to understand but harder to compose. If two capsules
both declare HIGH, which runs first? Numeric priorities allow arbitrary
interleaving. The convention is documented: 0-49 for guards, 50-99 for
transforms, 100+ for business logic.

# Prior art
[prior-art]: #prior-art

- **Bevy ECS** (Rust game engine): System ordering with explicit `before`/
  `after` constraints and run conditions. Systems can prevent later systems
  from running. The most direct inspiration — Astrid's interceptor chain is
  Bevy's system ordering applied to an IPC event bus. Bevy uses DAG-based
  ordering; Astrid uses numeric priorities (simpler, less expressive).

- **Express.js / Koa** (Node.js): `next()` middleware pattern. Each handler
  calls `next()` to pass control to the next handler, or doesn't call it to
  halt the chain. Astrid's `Continue` is `next()`, `Final`/`Deny` is "don't
  call next." Express popularized this pattern for web servers.

- **Envoy / Istio** (service mesh): HTTP filter chain with typed filter
  status: `Continue`, `StopIteration`, `StopAllIterationAndBuffer`. Runs
  filters in order, halts on stop. Applied to HTTP request/response
  processing. Astrid applies the same pattern to IPC events.

- **Linux netfilter** (kernel): `NF_ACCEPT`, `NF_DROP`, `NF_QUEUE` verdicts
  at each hook point (PRE_ROUTING, INPUT, FORWARD, OUTPUT, POST_ROUTING).
  The chain runs hooks in priority order; any hook can drop the packet.
  Astrid's Continue/Final/Deny maps to ACCEPT/STOLEN/DROP.

- **DOM events** (browser): `stopPropagation()` halts event bubbling.
  `preventDefault()` cancels the default action. Two different kinds of
  short-circuit for different purposes.

- **ASP.NET middleware pipeline**: Request delegates chained with `next()`.
  Each middleware can short-circuit by not calling `next()`. Terminal
  middleware always runs last (equivalent to default priority 100).

- **Servlet filters** (Java): `FilterChain.doFilter()` to continue,
  return without calling to halt. Same `next()` pattern as Express.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Should failed interceptors fail-closed or fail-open?** Currently a crashed
  guard continues the chain (fail-open). A `fail_mode` field on
  `[[interceptor]]` (`fail_mode = "deny"` vs `fail_mode = "continue"`) would
  let capsule authors choose. Security-critical guards would fail-closed;
  optional enrichment interceptors would fail-open. Is this over-engineering
  for pre-1.0?

- **Should the chain run in the dispatcher's task or a spawned task?**
  Currently multi-interceptor chains spawn a new task. This means the
  dispatcher can process other events while a chain runs, but it also means
  chain ordering is not guaranteed across events (event A's chain might
  complete after event B's chain if A's interceptors are slower). Is
  cross-event ordering important?

- **Should `Final` responses be published to the IPC bus?** When a cache
  interceptor returns `Final`, the response currently vanishes — no other
  capsule sees it. Should the kernel publish it as if the core handler
  produced it? This would make caching transparent to uplinks.

- **Should there be a maximum chain depth?** Currently unlimited. A malicious
  or misconfigured capsule set could create very long chains. Should the
  kernel cap chain length (e.g., 32 interceptors per event)?

- **Should priority be per-event or per-capsule?** Currently each
  `[[interceptor]]` has its own priority. A capsule with multiple
  interceptors could have them at different priorities. Is this useful or
  confusing?

# Future possibilities
[future-possibilities]: #future-possibilities

- **`fail_mode` field.** Per-interceptor fail-open vs fail-closed
  configuration. Guards declare `fail_mode = "deny"`, enrichment declares
  `fail_mode = "continue"`.

- **Payload type contracts.** Interceptors declare what payload types they
  accept and produce. The dispatcher validates type compatibility across the
  chain at boot — "interceptor A outputs type X, interceptor B expects type
  Y" would be a boot error.

- **Async interceptor chains.** Currently interceptors are synchronous
  (`invoke_interceptor` blocks). Async interceptors would allow I/O during
  chain processing (e.g., a guard that calls an external API for risk
  scoring).

- **Chain visualization.** `astrid capsule chain <topic>` shows the
  interceptor chain for a given topic: which capsules fire, in what order,
  with what priorities.

- **Conditional interceptors.** `[[interceptor]]` gains a `condition` field
  for runtime predicates: `condition = "principal != 'system'"` — only
  intercept events from non-system principals.

- **Interceptor metrics.** Per-interceptor invocation count, latency
  percentiles, deny rate. Exposed via `astrid_system_stats` host function
  for the system capsule to display.
