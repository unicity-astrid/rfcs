- Feature Name: `cli_proxy_protocol`
- Start Date: 2026-03-16
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The CLI proxy protocol defines how the CLI proxy capsule bridges the kernel's
IPC event bus to TUI frontend clients over a Unix domain socket. The proxy
enforces an ingress allowlist (clients can only publish to user-facing topics),
subscribes to TUI-relevant egress topics, and broadcasts events to all
connected clients. It supports up to 8 concurrent connections.

This RFC documents the protocol as currently implemented in
`astrid-capsule-cli`. It is not a proposal for new work.

# Motivation
[motivation]: #motivation

TUI frontends run as separate processes that cannot access the kernel's IPC
bus directly. Without a standardized proxy:

- Every frontend invents its own bridge, duplicating serialization and
  topic filtering.
- No security boundary between what clients can publish and what only
  kernel-side capsules should publish.
- Multi-client support requires coordination that belongs in a shared proxy.

The CLI proxy acts as the display server: it multiplexes access between TUI
frontends and the kernel event bus, enforcing topic-level access control.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Three layers participate in CLI communication:

1. **TUI client** - connects over Unix socket, sends user input, receives events.
2. **CLI proxy capsule** - bridges socket I/O to IPC bus with allowlist filtering.
3. **Kernel IPC bus** - internal event fabric.

```text
TUI client #1  <--UDS-->  CLI proxy  <--IPC-->  Kernel event bus
TUI client #2  <--UDS-->  capsule    <--IPC-->
```

The proxy runs a single-threaded event loop:
1. If no clients connected, block on `accept()`.
2. Poll for additional connections (non-blocking).
3. Read from all client streams (50ms timeout per stream).
4. Publish valid ingress messages to IPC bus.
5. Poll all IPC subscriptions.
6. Broadcast IPC messages to all connected clients.
7. Clean up dead streams.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## IPC topics (egress: IPC to clients)

| Direction | Topic | Payload | Description |
|---|---|---|---|
| Subscribe | `agent.v1.response` | `AgentResponse` | Final agent replies |
| Subscribe | `agent.v1.stream.delta` | `LlmStreamEvent` | Streaming token deltas |
| Subscribe | `astrid.v1.onboarding.required` | `OnboardingRequired` | Capsule needs env vars |
| Subscribe | `astrid.v1.elicit.*` | `ElicitRequest` | Runtime user input |
| Subscribe | `astrid.v1.approval` | `ApprovalRequired` | Approval gate events |
| Subscribe | `astrid.v1.response.*` | Various | General kernel responses |
| Subscribe | `astrid.v1.capsules_loaded` | (signal) | All capsules loaded |
| Subscribe | `registry.v1.response.*` | Various | Registry query responses |
| Subscribe | `registry.v1.active_model_changed` | `ProviderEntry` | Model switch notification |
| Subscribe | `registry.v1.selection.*` | `SelectionRequired` | Model picker UI |
| Subscribe | `session.v1.response.*` | Various | Session operation results |

## IPC topics (ingress: clients to IPC)

### Exact match allowlist

| Topic | Description |
|---|---|
| `user.v1.prompt` | User sends a prompt |
| `cli.v1.command.execute` | CLI command (e.g., `/clear`, `/models`) |

### Prefix match allowlist

| Prefix | Description |
|---|---|
| `astrid.v1.request.` | General kernel requests |
| `astrid.v1.elicit.response.` | Elicitation responses |
| `astrid.v1.approval.response.` | Approval decisions |
| `registry.v1.selection.` | Model picker selections |
| `session.v1.request.` | Session lifecycle requests |

Messages with topics not matching any allowlist entry are dropped. The proxy
logs a warning but keeps the connection open.

Note: `client.v1.disconnect` is intentionally *not* in the ingress allowlist.
The authoritative disconnect event is published by `close()` via the host
function to avoid double-counting in the idle monitor.

## Message schemas

The proxy passes `IpcMessage` objects between the socket and IPC bus. On
ingress, it parses the JSON from the socket, extracts `topic` and `payload`,
and publishes via `ipc::publish_json`. On egress, it receives poll envelopes
from the IPC bus, extracts individual messages, serializes each once, and
writes to all client streams.

### Ingress wire format (client to proxy)

```json
{
  "topic": "user.v1.prompt",
  "payload": { "type": "user_input", "text": "Hello", "session_id": "default" }
}
```

### Egress wire format (proxy to client)

Individual `IpcMessage` objects from the poll envelope, serialized as JSON.
Each message is written to the socket as a complete JSON object.

## Broadcast semantics

For each poll envelope from an IPC subscription:
1. Parse envelope, check for `dropped` count (log warning if > 0).
2. Extract `messages` array.
3. Serialize each message to bytes once.
4. Write serialized bytes to every active stream.
5. If a write fails, mark stream as dead (skip remaining messages for it).
6. After all subscriptions processed, sort + dedup dead indices, close and
   remove dead streams in reverse order.

## Connection lifecycle

- **Accept**: Blocking `accept()` when no clients connected. Non-blocking
  `poll_accept()` for additional connections (max one per iteration).
- **Read**: 50ms timeout per stream. Linear scaling: N streams = N * 50ms
  worst case per iteration. Acceptable for 2-3 typical, 8 max.
- **Close**: `close()` is required to release the host-side `active_streams`
  entry. Without it, the connection counter grows monotonically and
  `poll_accept` refuses new connections after the limit.

## Error handling

| Condition | Behavior |
|---|---|
| Socket bind fails | Capsule exits with error |
| Malformed JSON from client | Drop message, log warning, keep connection |
| Topic not in allowlist | Drop message, log warning, keep connection |
| Client stream write fails | Mark dead, close on next cleanup |
| Client stream read error | Mark dead, close on next cleanup |
| IPC subscription error | Proxy shuts down (break main loop) |
| Accept error | Log warning, 100ms backoff, retry |

## Capsule.toml manifest

```toml
[package]
name = "astrid-capsule-cli"
version = "0.1.0"
description = "Native Unix Socket bridge for the Astrid CLI frontend."

[capabilities]
uplink = true
net_bind = ["unix:*"]
ipc_publish = [
    "user.v1.prompt",
    "client.v1.disconnect",
    "cli.v1.command.execute",
    "astrid.v1.request.*",
    "astrid.v1.elicit.response.*",
    "astrid.v1.approval.response.*",
    "registry.v1.get_providers",
    "registry.v1.set_active_model",
    "registry.v1.selection.*",
    "session.v1.request.*",
]
ipc_subscribe = [
    "agent.v1.response",
    "agent.v1.stream.delta",
    "astrid.v1.approval",
    "astrid.v1.capsules_loaded",
    "astrid.v1.elicit.*",
    "astrid.v1.onboarding.required",
    "astrid.v1.response.*",
    "registry.v1.response.*",
    "registry.v1.active_model_changed",
    "registry.v1.selection.*",
    "session.v1.response.*",
]
```

# Drawbacks
[drawbacks]: #drawbacks

- Unix socket only. Remote frontends (web, Discord) need a separate transport.
- No message queuing. Clients that disconnect lose events that fired while
  disconnected.
- Sequential stream polling: 50ms per stream means worst-case 400ms per
  cycle with 8 clients.
- The 8-client limit is a compile-time constant, not configurable.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why a proxy capsule instead of direct IPC access?** Direct access would
give every frontend full publish access to every topic. The proxy interposes
an allowlist, enforcing that clients can only publish to user-facing topics.

**Why Unix socket instead of TCP?** Local clients only. Unix sockets provide
file-permission-based access control, zero network overhead, and no port
management.

**Why pre-serialize once for broadcast?** With N clients receiving the same
message, serializing once avoids N redundant serialization passes. Matters
for high-frequency streaming events.

# Prior art
[prior-art]: #prior-art

- **X11/Wayland display server**: Multiplexes application access to graphics
  over Unix socket. The CLI proxy serves the same role for the IPC bus.
- **Docker daemon socket** (`/var/run/docker.sock`): Daemon listens on Unix
  socket, serves API. File-permission access control.
- **tmux client/server**: Server owns terminal state, clients attach/detach
  over Unix socket. Same multi-client pattern.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should there be a session replay protocol for clients that reconnect and
  need to catch up on missed events?
- Should the connection limit be configurable via the capsule manifest?
- Should there be backpressure handling (flow control) before marking slow
  streams as dead?

# Future possibilities
[future-possibilities]: #future-possibilities

- WebSocket transport adapter wrapping the same message protocol.
- Client capability tokens determining per-client ingress allowlists.
- Multiplexed sessions (one connection observing multiple agent sessions).
