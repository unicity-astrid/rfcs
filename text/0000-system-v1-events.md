- Feature Name: `system_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#15](https://github.com/unicity-astrid/rfcs/pull/15)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The system event contract defines the topic patterns and message schemas for
system-level signals on the Astrid event bus: client connection lifecycle,
capsule load notifications, health monitoring, watchdog ticks, event bus lag
warnings, and restart signals.

# Motivation
[motivation]: #motivation

System-level signals coordinate the kernel's operational state. The contract
must be standardized so that:

- Frontends know when capsules are ready.
- The orchestrator can detect event bus backpressure.
- Health monitoring is observable by any capsule.
- Client connection tracking works across all frontends.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

System events are published by the kernel and its infrastructure (connection
tracker, watchdog, health monitor). Unlike lifecycle events (which track
application-level state transitions), system events track infrastructure
health.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| notify | `astrid.v1.capsules_loaded` | `Custom` | Kernel | Frontend, Registry |
| notify | `astrid.v1.health.failed` | `Custom` | Health monitor | (subscribers) |
| tick | `astrid.v1.watchdog.tick` | `Custom` | Watchdog (every 5s) | Orchestrator |
| warn | `astrid.v1.event_bus.lagged` | `Custom` | EventDispatcher (rate-limited 10s) | Orchestrator |
| signal | `system.v1.lifecycle.restart` | `Custom` | Kernel | Orchestrator |
| connect | `client.v1.connected` | `Connect` | Host `net_accept` | Connection tracker |
| disconnect | `client.v1.disconnect` | `Disconnect` | Frontend | Connection tracker |

## Message schemas

### Capsules loaded

```json
{
  "type": "custom",
  "data": {
    "capsule_count": 15,
    "failed_count": 0
  }
}
```

Published once after all capsules finish loading. Frontends wait for this
before showing the ready state.

### Health failed

```json
{
  "type": "custom",
  "data": {
    "check_name": "string",
    "error": "string"
  }
}
```

### Watchdog tick

```json
{
  "type": "custom",
  "data": {
    "uptime_seconds": 3600
  }
}
```

Published every 5 seconds. The orchestrator uses this for timeout
enforcement on in-flight operations.

### Event bus lagged

```json
{
  "type": "custom",
  "data": {
    "lagged_count": 42
  }
}
```

Published by the `EventDispatcher` when a receiver falls behind the
broadcast channel. Rate-limited to at most once per 10 seconds to avoid
amplifying the backpressure.

### Restart signal

Published when the kernel initiates a restart. The orchestrator uses this
to clean up ephemeral keys and reset state.

### Connect

```json
{
  "type": "connect"
}
```

Unit payload. Published when a new socket connection is accepted.

### Disconnect

```json
{
  "type": "disconnect",
  "reason": "quit"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `reason` | string/null | no | Reason for disconnection (e.g. "quit", "timeout"). Omitted from JSON when null. |

## Behavioral requirements

The **kernel** must:

1. Publish `astrid.v1.capsules_loaded` after all capsule loading completes.
2. Run a watchdog that publishes `astrid.v1.watchdog.tick` at a regular
   interval (currently 5 seconds).
3. Publish `astrid.v1.event_bus.lagged` when broadcast lag is detected,
   rate-limited.

A conforming **frontend** must:

1. Publish `Connect` to `client.v1.connected` on connection.
2. Publish `Disconnect` to `client.v1.disconnect` before closing.
3. Wait for `astrid.v1.capsules_loaded` before starting interactive use.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Frontend disconnects without publishing Disconnect | Connection tracker may count stale connections until timeout |
| Health check fails | `astrid.v1.health.failed` published, kernel continues running |
| Event bus lag detected | Warning published, events may be dropped for slow receivers |

# Drawbacks
[drawbacks]: #drawbacks

- The watchdog tick is a fixed 5-second interval, not configurable.
- Event bus lag detection is reactive, not preventive. Slow subscribers
  have already missed events by the time the warning fires.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why separate system events from lifecycle events?** System events track
infrastructure health (bus lag, connectivity, watchdog). Lifecycle events
track application state (sessions, tools, agents). Different audiences.

**Why rate-limit lag warnings?** A lagging subscriber would generate a
flood of lag warnings, worsening the backpressure it is trying to report.

# Prior art
[prior-art]: #prior-art

- **Kubernetes liveness/readiness probes**: Watchdog-style health checks.
- **NATS slow consumer notifications**: Similar to event bus lag warnings.
- **TCP keepalive**: Connection liveness detection.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should the watchdog interval be configurable?
- Should there be a `system.v1.lifecycle.shutdown` signal distinct from
  `astrid.v1.lifecycle.kernel_shutdown`?

# Future possibilities
[future-possibilities]: #future-possibilities

- Per-subscriber lag tracking with targeted backpressure.
- Configurable watchdog intervals per deployment.
- System event persistence for post-mortem debugging.
