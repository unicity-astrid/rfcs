- Feature Name: `connection_lifecycle`
- Start Date: 2026-05-29
- RFC PR: [rfcs#29](https://github.com/unicity-astrid/rfcs/pull/29)
- Tracking Issue: [astrid#807](https://github.com/unicity-astrid/astrid/issues/807)

# Summary
[summary]: #summary

Introduce a dedicated **`connection`** bus contract (`astrid-bus:connection@1.0.0`)
that owns the whole client-connection lifecycle: the uplink-published
`connect` / `disconnect` envelopes (folded in from the former `client`
contract), a new kernel-emitted `principal-presence` event published when a
principal's live connection count crosses a boundary, and a **typed**
`disconnect-reason` (no stringly-typed reasons). Long-lived capsules consume
`principal-presence`; in particular the ReAct capsule aborts an in-flight agent
run when its principal's last client detaches — instead of churning LLM/tool
calls until a phase timeout. Both the presence event and the resulting abort
feed the metrics layer and the admin dashboard as first-class observability.

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
`principal-presence` event on `connection.v1.presence`:

- **first-connected** — the principal went from 0 to 1 live connections.
- **last-disconnected** — the principal went from N to 0 live connections.

A capsule that cares about a principal's presence subscribes to that topic and
matches on the transition. The canonical consumer is the ReAct capsule:

```text
1. Client (principal "alice") sends a prompt; ReAct starts a run (session S).
2. Alice quits mid-run. The uplink publishes connection.v1.disconnect{ graceful }.
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

## The `connection` contract (`wit/interfaces/connection.wit`)

A single, cohesive contract owns the entire connection lifecycle. It **replaces**
`astrid-bus:client@1.0.0`: the uplink-published `connect`/`disconnect` move here,
the typed reasons are co-located (they are connection-specific, not cross-cutting
`types.wit` material), and the kernel-emitted presence event lives here too.

```wit
package astrid-bus:connection@1.0.0;

/// The full client-connection lifecycle: uplink-published attach/detach
/// envelopes, the kernel-emitted per-principal presence event, and the typed
/// reasons shared between them.
interface connection {
    // ---- uplink -> kernel (folded in from the former `client` contract) ----

    /// A client attached to an uplink. Topic: `connection.v1.connect`.
    record connect {
        /// Optional client identifier (e.g. process name + PID).
        client-id: option<string>,
    }

    /// A client detached. Topic: `connection.v1.disconnect`.
    record disconnect {
        /// Typed reason (was `option<string>`); absent if not supplied.
        reason: option<disconnect-reason>,
    }

    // ---- kernel -> capsule ----

    /// Emitted when a principal's live connection count crosses a boundary, so
    /// capsules can react to a principal becoming present or absent.
    /// Topic: `connection.v1.presence`.
    record principal-presence {
        principal: string,
        transition: presence-transition,
        /// Live count *after* the transition (0 on `last-disconnected`). Carries
        /// the count, not just a boolean, so consumers and metrics get richer
        /// state without a new event shape.
        active-connections: u32,
        /// Reason carried from the triggering disconnect, when the kernel knows
        /// it. Absent for `first-connected` and for reasonless disconnects.
        reason: option<disconnect-reason>,
    }

    enum presence-transition {
        first-connected,    // 0 -> 1
        last-disconnected,  // N -> 0
    }

    // ---- shared reason types ----

    /// Why a client connection ended. Typed known cases plus an `other` escape
    /// hatch: unrecognized reasons degrade gracefully (extending a WIT variant
    /// later is itself a breaking change, so the hatch is the forward-safety).
    variant disconnect-reason {
        graceful,                  // client requested a clean quit
        dropped,                   // EOF / reset / broken pipe / crash / net loss
        timed-out,                 // inactivity / idle timeout
        shutdown,                  // daemon shutting down
        administrative,            // operator kick or daemon reset
        rejected(rejection-cause), // kernel/uplink closed it for cause
        other(string),             // unrecognized; diagnostics only, never matched
    }

    /// Why a connection was closed *for cause*.
    enum rejection-cause {
        unauthenticated,  // bad / expired token at handshake
        unauthorized,     // authenticated but lacking the required capability
        rate-limited,
        quota-exceeded,   // per-principal connection / resource quota
        protocol-error,   // malformed frame / protocol violation
    }
}
```

`system.wit` is **not** touched: it stays the *daemon/kernel* lifecycle contract
(boot, shutdown, watchdog, restart). Connection lifecycle is a distinct, growing
domain and earns its own independently-versioned package.

## Agent abort outcome (`wit/interfaces/agent.wit`)

The abort outcome is *agent* domain, not connection domain — it stays in
`agent.wit`. A capsule that aborts in-flight work in response to
`last-disconnected` reports the outcome so it is observable.
Topic: `agent.v1.aborted`.

```wit
record agent-aborted {
    session-id: string,
    principal: string,
    outcome: abort-outcome,
}

enum abort-outcome {
    aborted,           // a run was in flight and was torn down
    nothing-to-abort,  // no in-flight run for that principal/session
    failed,            // could not be cleanly aborted (details logged)
}
```

## Topics and the supersession of the just-landed `client.v1.*` wiring

The connect/disconnect topics become `connection.v1.connect` /
`connection.v1.disconnect`, and presence is `connection.v1.presence` — one
consistent namespace under the contract.

This **supersedes** the `client.v1.connect` / `client.v1.disconnect` wiring that
just merged in astrid#794 (kernel tracker) and capsule-cli#20/#21 (proxy). The
implementation of this RFC re-points those two call sites (the kernel
`connection_signal` topic match and the proxy's publish + `ipc_publish`
allowlist) to the new topics. The churn is real but small and entirely pre-1.0
(those changes are days old and unreleased); doing it in one coordinated change
under this RFC is cheaper than carrying a permanently inconsistent
`connection`-interface-with-`client.v1.`-topics naming. See
[Rationale](#rationale-and-alternatives) for the alternative of keeping the
topic strings frozen.

## Kernel behaviour

`Kernel::connection_opened` / `connection_closed` already maintain the
per-principal `active_connections` count. This RFC adds:

- On `connection_opened`, if the principal's count transitions `0 -> 1`, publish
  `principal-presence{ first-connected, active-connections: 1, reason: none }`.
- On `connection_closed`, if the principal's count transitions `1 -> 0`, publish
  `principal-presence{ last-disconnected, active-connections: 0, reason }`, where
  `reason` is threaded from the triggering `connection.v1.disconnect` (typed)
  when present, `some(dropped)` when the disconnect was detected by stream close
  (no graceful reason on the wire), or `none` if unknown.

The event is published **after** the counter mutation so a consumer that
re-reads the count via the management API sees a value consistent with the
event. Per-principal ordering is preserved. Publication is best-effort on the
broadcast bus (same delivery semantics as every other kernel event).

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
  - `astrid_agent_aborts_total{outcome}` — counter, from each
    `agent-aborted.outcome`.
  - `astrid_principal_disconnects_total{reason}` — counter, labelled by the typed
    reason discriminant (`graceful`, `dropped`, `rejected`, …); this is why the
    reason is typed rather than a free string.
- **Dashboard**: the admin dashboard subscribes to both topics (via the gateway's
  existing bus-backed SSE) to render a live "who is attached" view and an
  agent-abort feed.

## Error handling contract

- A capsule that fails to abort cleanly emits `agent-aborted{ failed }` and logs
  detail; it must not panic or wedge its run loop. `failed` is a terminal,
  observable outcome — not a retried error.
- The kernel never blocks on consumers: presence publication is fire-and-forget.
- Unknown `disconnect-reason` values arrive as `other(string)`, recorded under an
  `other` metric label; they never drive control flow.

## Migration and blast radius

The breaking surface looks large but is verified **narrow**. The `astrid-bus:*`
interface contracts (including this one) are advisory specifications — they are
**not** compiled into any capsule's WIT bindings. Capsules talk on the bus
through `astrid:ipc/host` with **string topics + serde-JSON payloads**; the SDK
exposes only `publish(topic, payload)` / `publish_json_as`, never generated
`connect`/`disconnect` types. The guest `capsule` world (`astrid-sys`) imports
only `astrid:*` *host* packages, so a change to `astrid-bus:client` /
`astrid-bus:connection` cannot make any capsule fail to compile.

Verified blast radius (capsule grep + binding-mechanism audit, astrid#808):

- **`astrid-capsule-cli`** — the only affected capsule. It publishes the topics
  as string literals (`ipc::publish_json_as("client.v1.connect", …)`) and lists
  them in `Capsule.toml`; the change is a string/JSON edit (new topic names +
  typed `reason` shape), not a type break.
- **All other capsules** — unaffected; no code change, no SDK bump required.
- **The kernel (`core/`, not a capsule)** — the real consumer: it hand-parses
  the topic + JSON in `connection_signal`, so the kernel router + its tests + the
  native-uplink emitters in `astrid-cli` change with the contract. In scope for
  the implementation, but a single repo.

So the implementation is two coordinated edits — `astrid-capsule-cli` and
`core/` — not a fleet-wide capsule migration.

# Drawbacks
[drawbacks]: #drawbacks

- New contract surface: a new `connection` package (connect/disconnect/presence/
  reasons) replacing `client`, plus an `agent-aborted` record in `agent.wit`.
- Breaking changes — the `client` → `connection` package rename, the typed
  `disconnect.reason`, and the connect/disconnect topic rename — require
  coordinated CLI-client + proxy updates and re-touch the just-merged
  `client.v1.*` wiring. We are pre-1.0 and these changes are unreleased, so the
  cost is one coordinated implementation rather than a migration burden, but it
  is real churn on recently-landed code.
- The abort is best-effort: a genuinely wedged run may report `failed` rather
  than stopping instantly.
- ReAct gains awareness of connection lifecycle — a mild layering concession,
  justified by it being the component that owns the wasteful in-flight work.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why a dedicated `connection` contract (vs adding to `system.wit`).** Without
it, the connection story is scattered: the uplink half (`connect`/`disconnect`)
in `client.wit`, the kernel half (`presence`) in `system.wit`, and the shared
reason in `types.wit` — three files for one domain. A dedicated contract puts the
whole lifecycle in one place, co-locates `disconnect-reason` with its only users,
and lets the domain version independently as it grows (admin kick/reset,
session-resume grace windows, per-connection metadata). `system.wit` stays what
it says it is — daemon/kernel lifecycle — instead of becoming a junk drawer.

**Why fold `client.wit` in entirely (vs leaving it and adding a separate
presence contract).** `client.wit` is *already* "connection lifecycle envelopes";
splitting connect/disconnect from presence across two contracts re-introduces the
scatter we are trying to remove. Since we are breaking `client` anyway (the typed
reason), folding it in is the cheap, coherent move.

**On the topic rename (and the alternative of freezing the strings).** Renaming
the topics to `connection.v1.*` re-touches astrid#794 / capsule-cli#20/#21. The
alternative is to keep the WIT package `connection` but leave the topic strings
as `client.v1.connect`/`disconnect` (topics are bus-string conventions, decoupled
from the package name) and use `astrid.v1.principal.presence` for the new event —
zero re-break of merged code, at the cost of a permanent naming mismatch
(`connection` interface, `client.v1.` topics). Recommended: rename, because the
churn is trivial and pre-1.0 and the consistent namespace is worth more than
avoiding a days-old re-touch. Flagged as a decision point.

**Why kernel-emitted last-disconnect (vs ReAct subscribing to disconnect
directly).** `disconnect` fires on *every* disconnect, not the *last* one. With
multiple clients for one principal a react-only approach would abort a live run
the moment any one client detaches — incorrect. "Last" requires the per-principal
count, which only the kernel authoritatively has.

**Why typed reasons (vs `option<string>`).** Free strings can't be metric labels
without cardinality risk, can't be exhaustively matched, and invite drift
("quit" vs "user-quit" vs "exit"). The `variant` + `other(string)` hatch gives
typed known cases while staying forward-safe.

**Why report abort outcomes (vs fire-and-forget).** Observability is a primary
goal: without `agent-aborted`, there is no way to see whether disconnect-driven
aborts happen, how often, or whether they fail.

# Prior art
[prior-art]: #prior-art

- HTTP/gRPC servers cancel in-flight request work when the client connection
  drops (axum/tower cancellation, gRPC cancellation propagation). This RFC is the
  bus-event analogue for agent runs.
- Kubernetes surfaces pod/endpoint lifecycle as typed events consumed by many
  controllers — "one authoritative lifecycle event, many consumers" mirrors
  `principal-presence`.
- Astrid's own `system.watchdog-tick` / `system.event-bus-lagged` establish the
  pattern of typed, kernel-emitted lifecycle events; this RFC gives the
  *connection* slice its own home rather than overloading `system`.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Topic rename vs frozen strings.** Decided-but-confirm: rename to
  `connection.v1.*` (re-touching the just-merged `client.v1.*` wiring) for a
  consistent namespace, vs keeping the strings frozen (see Rationale). Pre-1.0,
  breaking is acceptable; the question is whether the re-churn is worth the
  consistency.
- **Per-connection idle timeout.** The `timed-out` reason presumes the
  proxy/kernel can close an idle connection. That does not exist today (the idle
  monitor shuts down the whole daemon, not individual sockets). Is a
  per-connection idle timeout in scope, or is `timed-out` reserved for when it
  lands?
- **Intermediate count changes.** Presence fires only on `0<->N` boundaries. Do
  any consumers need every count change (e.g. `2 -> 1`)? If so, a `count-changed`
  transition must be added *now* (adding an enum variant later is breaking).

> Resolved during review: the `client` contract is broken end-to-end (package
> rename + typed reason) rather than string-mapped at the kernel boundary —
> acceptable because the project is pre-1.0 and the contract is unreleased.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Admin lifecycle / dashboard.** `principal-presence` and `agent-aborted` are
  the connection/agent half of the operability surface the explicit-daemon-
  lifecycle work (reset, staged-update flag, changelog, dashboard) builds on. The
  `administrative` disconnect reason is the seam where an operator-initiated
  kick/reset reports itself.
- **Session management.** A correct per-principal presence signal enables
  "resume my session on reconnect within N seconds" (suppress the abort during a
  grace window) without each capsule reinventing connection tracking.
- **Richer agent-lifecycle events.** `agent-aborted` is the first of a potential
  family (`agent-started`, `agent-completed`, …) giving the dashboard a full
  agent-run timeline.
- **Presence-driven scaling.** In a multi-tenant deployment, per-principal
  presence is a natural input to load-aware scheduling or capsule warm/cold
  decisions.
