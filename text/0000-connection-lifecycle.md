- Feature Name: `connection_lifecycle`
- Start Date: 2026-05-29
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#807](https://github.com/unicity-astrid/astrid/issues/807)

# Summary
[summary]: #summary

Define a kernel-emitted **connection-lifecycle** contract: the kernel publishes
a typed `principal-presence` event when a principal's live connection count
crosses a boundary (gains its first client, or loses its last). The event
carries a **typed** `disconnect-reason` — no stringly-typed reasons. Long-lived
capsules consume it to react to a principal becoming present or absent; in
particular the ReAct capsule aborts an in-flight agent run when its principal's
last client detaches, instead of churning until a phase timeout. Both the
presence event and the resulting abort feed the metrics layer and the admin
dashboard as first-class observability.

# Motivation
[motivation]: #motivation

Two problems, one enabling change.

**Orphaned agent runs.** The ReAct loop is event-driven: a user prompt kicks off
an LLM → tool → LLM ping-pong across bus events. If the client driving the run
disconnects mid-flight, nothing tells the agent to stop. The run keeps issuing
LLM requests (real API spend) and tool calls (real compute) with no consumer for
the output, until it either hits `max_iterations` (default 25) or ages out via a
per-phase watchdog timeout (up to ~2 minutes for the streaming phase). That is
wasted money, wasted CPU, and wasted wall-clock holding session state.

Until recently the kernel could not detect this: the per-principal connection
counter was structurally pinned at zero (see astrid#788/#789). Now that the
counter is populated, the kernel knows precisely when a principal's **last**
client disconnects — so it can signal capsules to abort orphaned work the moment
the client leaves, not minutes later.

**Connection observability.** Today `Kernel::connection_closed` only logs. There
is no event a dashboard or the metrics layer can consume to answer "who is
attached?", "why did this principal go away?", or "how often are agent runs
aborted on disconnect?". Connection lifecycle should be a first-class,
**typed**, observable signal — usable as metric labels and dashboard slices —
not a log line and a free-text string.

The expected outcome: orphaned runs abort within milliseconds; connection
presence and abort outcomes are graphable and visible in the dashboard; and the
reason a connection ended is a typed value the whole system can pattern-match on.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

The kernel maintains a per-principal count of live client connections. When that
count crosses a boundary for a principal, the kernel publishes a
`principal-presence` event on `astrid.v1.principal.presence`:

- **first-connected** — the principal went from 0 to 1 live connections.
- **last-disconnected** — the principal went from N to 0 live connections.

A capsule that cares about a principal's presence subscribes to that topic and
matches on the transition. The canonical consumer is the ReAct capsule:

```text
1. Client (principal "alice") sends a prompt; ReAct starts a run (session S).
2. Alice quits mid-run. The uplink publishes client.v1.disconnect{ graceful }.
3. The kernel decrements alice's count 1 -> 0 and publishes
   principal-presence{ principal: "alice", transition: last-disconnected,
                       active-connections: 0, reason: some(graceful) }.
4. ReAct sees last-disconnected for alice, finds her in-flight run (session S),
   tears it down (cleanup + reset to Idle), and publishes
   agent-aborted{ session-id: S, outcome: aborted }.
5. The metrics layer records astrid_active_connections{principal="alice"}=0 and
   astrid_agent_aborts_total{outcome="aborted"}+1; the dashboard reflects both.
```

A capsule that has nothing in flight for that principal simply does nothing (and,
if it participates in abort reporting, emits `agent-aborted{ nothing-to-abort }`).

The disconnect **reason** is typed. A consumer never parses a string for known
cases:

```text
match reason {
    some(graceful)                 => // user asked to quit
    some(dropped)                  => // socket died (crash / network)
    some(timed-out)                => // idle / inactivity timeout
    some(shutdown)                 => // daemon going down
    some(administrative)           => // operator kicked / reset
    some(rejected(unauthenticated))=> // bad/expired token at handshake
    some(rejected(unauthorized))   => // lacks capability
    some(rejected(rate-limited))   => // rate-limited
    some(rejected(quota-exceeded)) => // per-principal connection/resource quota
    some(rejected(protocol-error)) => // malformed frame / protocol violation
    some(other(s))                 => // unrecognized; diagnostics only
    none                           => // reason not known to the kernel
}
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Shared types (`wit/interfaces/types.wit`)

The reason is a shared type so both the `client` interface (the source of a
disconnect) and the `system` interface (the presence event) refer to the same
definition rather than duplicating it.

```wit
/// Why a client connection ended. Typed known cases plus an `other` escape
/// hatch: unrecognized reasons degrade gracefully without a breaking change to
/// this variant (adding a variant later IS a breaking change in WIT).
variant disconnect-reason {
    /// Client requested a clean disconnect (e.g. user quit).
    graceful,
    /// Transport closed ungracefully — EOF, reset, broken pipe, client crash,
    /// or network loss.
    dropped,
    /// Closed due to inactivity / idle timeout.
    timed-out,
    /// Daemon is shutting down.
    shutdown,
    /// Operator-initiated disconnect (admin kick or daemon reset).
    administrative,
    /// The kernel/uplink closed the connection for cause.
    rejected(rejection-cause),
    /// Unrecognized reason, preserved as free text for diagnostics only.
    /// Never pattern-matched for control flow.
    other(string),
}

/// Why a connection was closed *for cause* by the kernel or an uplink.
enum rejection-cause {
    /// Authentication failed (bad / expired token at handshake).
    unauthenticated,
    /// Authenticated but lacking the required capability.
    unauthorized,
    /// Rate-limited.
    rate-limited,
    /// Per-principal connection or resource quota exceeded.
    quota-exceeded,
    /// Malformed frame or protocol violation.
    protocol-error,
}
```

## Presence event (`wit/interfaces/system.wit`)

```wit
/// Emitted when a principal's live connection count crosses a boundary, so
/// capsules can react to a principal becoming present or absent.
/// Topic: `astrid.v1.principal.presence`.
record principal-presence {
    /// The principal whose presence changed.
    principal: string,
    /// The boundary that was crossed.
    transition: presence-transition,
    /// Live connection count for this principal *after* the transition
    /// (0 on `last-disconnected`). Carries the count, not just a boolean, so
    /// consumers and metrics get richer state without a new event shape.
    active-connections: u32,
    /// Reason carried from the triggering disconnect, when the kernel knows it.
    /// Absent for `first-connected`, and for disconnects with no reason on the
    /// wire.
    reason: option<disconnect-reason>,
}

enum presence-transition {
    /// 0 -> 1 live connections.
    first-connected,
    /// N -> 0 live connections.
    last-disconnected,
}
```

## Abort outcome (`wit/interfaces/agent.wit`)

A capsule that aborts in-flight work in response to `last-disconnected` reports
the outcome so it is observable. Topic: `astrid.v1.agent.aborted`.

```wit
/// Published by an agent capsule after it processes a `last-disconnected`
/// presence event for one of its sessions.
record agent-aborted {
    /// Session whose run was (or would have been) aborted.
    session-id: string,
    /// Principal the session belonged to.
    principal: string,
    /// What happened.
    outcome: abort-outcome,
}

enum abort-outcome {
    /// A run was in flight and was torn down.
    aborted,
    /// No in-flight run for that principal/session — nothing to do.
    nothing-to-abort,
    /// An in-flight run could not be cleanly aborted (details logged).
    failed,
}
```

## Breaking change: `client.wit` reason becomes typed

The reason originates at the uplink, where `client.wit` currently types it as a
free string:

```wit
// before
record disconnect { reason: option<string> }
// after (astrid-bus:client@2.0.0)
record disconnect { reason: option<disconnect-reason> }
```

This is a **breaking change** to the uplink contract and bumps the `client`
package major version. The CLI client and the CLI proxy capsule update in
lockstep (they are versioned together in this repo). See
[Unresolved questions](#unresolved-questions) for the migration sequencing and
the non-breaking alternative.

## Kernel behaviour

`Kernel::connection_opened` / `connection_closed` already maintain the
per-principal `active_connections` count. This RFC adds:

- On `connection_opened`, if the principal's count transitions `0 -> 1`, publish
  `principal-presence{ first-connected, active-connections: 1, reason: none }`.
- On `connection_closed`, if the principal's count transitions `1 -> 0`, publish
  `principal-presence{ last-disconnected, active-connections: 0, reason }`, where
  `reason` is threaded from the triggering `client.v1.disconnect` (typed) when
  present, or `some(dropped)` when the disconnect was detected by stream close
  (no graceful reason on the wire), or `none` if unknown.

The event is published **after** the counter mutation so a consumer that
re-reads the count via the management API sees a value consistent with the
event. Per-principal ordering is preserved (the kernel publishes presence events
for a given principal in count-transition order). Publication is best-effort on
the broadcast bus (same delivery semantics as every other kernel event).

## Consumer behaviour (ReAct)

ReAct keys runs by `session-id`. To act on a principal-scoped event it threads
the **principal** into its per-session `TurnState` when a run starts (today
`TurnState` is principal-agnostic). On `last-disconnected` for principal `P`,
ReAct iterates its active sessions, and for each whose principal is `P` it runs
its existing teardown path (`cleanup_inflight_mappings` + `reset_conversation_turn`
+ `set_phase(Idle)`), cancels any dispatched tools (`tool.v1.request.cancel`),
and publishes `agent-aborted`. If no session matches, it publishes
`agent-aborted{ nothing-to-abort }`.

## Observability

- **Metrics** (via the existing `metrics` facade):
  - `astrid_active_connections{principal}` — gauge, set from each
    `principal-presence.active-connections`.
  - `astrid_agent_aborts_total{outcome}` — counter, incremented from each
    `agent-aborted.outcome`.
  - `astrid_principal_disconnects_total{reason}` — counter, labelled by the
    typed reason discriminant (`graceful`, `dropped`, `rejected`, …); this is
    why the reason is typed rather than a free string.
- **Dashboard**: the admin dashboard subscribes to both topics (via the
  gateway's existing bus-backed SSE) to render a live "who is attached" view and
  an agent-abort feed.

## Error handling contract

- A capsule that fails to abort cleanly emits `agent-aborted{ failed }` and logs
  detail; it must not panic or wedge its run loop. `failed` is a terminal,
  observable outcome — not a retried error.
- The kernel never blocks on consumers: presence publication is fire-and-forget.
- Unknown `disconnect-reason` values arrive as `other(string)` and are recorded
  under an `other` metric label; they never drive control flow.

# Drawbacks
[drawbacks]: #drawbacks

- New contract surface: one shared variant + one enum (`types.wit`), one record
  + one enum (`system.wit`), one record + one enum (`agent.wit`), plus a
  breaking bump to `client.wit`.
- A breaking change to the uplink contract (`client@2.0.0`) requires coordinated
  CLI-client + proxy updates.
- The abort is best-effort: a genuinely wedged run (e.g. blocked in a host call)
  may report `failed` rather than stopping instantly.
- ReAct gains awareness of connection lifecycle — a mild layering concession,
  justified by it being the component that owns the wasteful in-flight work.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why kernel-emitted last-disconnect (vs ReAct subscribing to
`client.v1.disconnect` directly).** `client.v1.disconnect` fires on *every*
disconnect, not the *last* one. With multiple clients for one principal, a
react-only approach would abort a live run the moment any one client detaches —
incorrect. Computing "last" requires the per-principal count, which only the
kernel authoritatively has. Emitting the boundary-crossing event keeps the count
authority in one place.

**Why typed reasons (vs the existing `option<string>`).** Free strings can't be
metric labels without cardinality risk, can't be exhaustively matched, and
invite drift ("quit" vs "user-quit" vs "exit"). The `variant` with an
`other(string)` escape hatch gives typed known cases for metrics/dashboards while
staying forward-safe.

**Why report abort outcomes (vs fire-and-forget).** Observability is a primary
goal: without `agent-aborted`, there is no way to see whether disconnect-driven
aborts are happening, how often, or whether they fail. The cost is one extra
event type.

**Why carry the count in the event.** A boolean "present/absent" would force a
second round-trip (or a separate event) for any consumer that wants the count
(metrics, dashboard). `u32` is forward-compatible and free.

**Impact of not standardizing.** Orphaned runs continue to burn spend/CPU until
timeout; connection lifecycle stays invisible to operators; and each consumer
that wants disconnect info re-invents string parsing.

# Prior art
[prior-art]: #prior-art

- HTTP/gRPC servers cancel in-flight request work when the client connection
  drops (axum/tower `Connected`/cancellation, gRPC cancellation propagation).
  This RFC is the bus-event analogue for agent runs.
- Kubernetes surfaces pod/endpoint lifecycle as typed events consumed by many
  controllers — the model of "one authoritative lifecycle event, many
  consumers" mirrors `principal-presence`.
- Astrid's own `system.watchdog-tick` and `system.event-bus-lagged` already
  establish the pattern of typed, kernel-emitted lifecycle events in `system.wit`;
  `principal-presence` extends it.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **`client.wit` migration.** Two options:
  1. **End-to-end typed (recommended):** retype `client.disconnect.reason` to
     `disconnect-reason` and bump to `client@2.0.0`, updating the CLI client and
     proxy in lockstep. Clean, but a breaking uplink-contract change.
  2. **Non-breaking:** leave `client.wit` as `option<string>` and have the kernel
     map string → `disconnect-reason` at the boundary (unknown → `other`). No
     uplink change, but the untyped string lives one layer down and third-party
     uplinks keep emitting strings.
  Which do we take, and if (1), is it sequenced as its own PR ahead of the
  consumers?
- **Per-connection idle timeout.** The `timed-out` reason presumes the
  proxy/kernel can close an idle connection. That does not exist today (the idle
  monitor shuts down the whole daemon, not individual sockets). Is a
  per-connection idle timeout in scope, or is `timed-out` reserved for when it
  lands?
- **Intermediate count changes.** Presence fires only on `0<->N` boundaries.
  Do any consumers need every count change (e.g. `2 -> 1`)? If so, a
  `count-changed` transition would be added — but that is a breaking enum change,
  so it should be decided now.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Admin lifecycle / dashboard (the staged-update RFC).** `principal-presence`
  and `agent-aborted` are the connection/agent half of the operability surface
  the explicit-daemon-lifecycle work (reset, staged-update flag, changelog,
  dashboard) builds on. The `administrative` disconnect reason is the seam where
  an operator-initiated kick/reset reports itself.
- **Session management.** A correct per-principal presence signal enables
  features like "resume my session on reconnect within N seconds" (suppress the
  abort during a grace window) without each capsule reinventing connection
  tracking.
- **Richer agent-lifecycle events.** `agent-aborted` is the first of a potential
  family (`agent-started`, `agent-completed`, …) that would give the dashboard a
  full agent-run timeline.
- **Presence-driven scaling.** In a multi-tenant deployment, per-principal
  presence is the natural input to load-aware scheduling or capsule warm/cold
  decisions.
