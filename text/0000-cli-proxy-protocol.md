- Feature Name: `cli_proxy_protocol`
- Start Date: 2026-03-16
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

This RFC defines the IPC contract for the CLI proxy capsule, the bridge between
the kernel's IPC event bus and any number of TUI frontend clients over a Unix
domain socket. It specifies the wire format, ingress allowlist, egress topic
subscriptions, connection lifecycle, and multi-client semantics. The CLI proxy
is the display server in Astrid's OS model.

# Motivation
[motivation]: #motivation

The Astrid kernel communicates with capsules over an internal IPC event bus. TUI
frontends (the interactive CLI, future curses-based dashboards, accessibility
readers) run as separate processes that cannot access the bus directly. They need
a well-defined protocol to send user input to the kernel and receive agent
responses, approval requests, onboarding flows, and streaming tokens.

Without a standardized protocol:

- Every frontend invents its own ad-hoc bridge, duplicating serialization,
  topic filtering, and error handling.
- There is no security boundary between what a client can publish and what only
  kernel-side capsules should publish. A misbehaving TUI could impersonate an
  agent or inject capsule lifecycle events.
- Multi-client support (e.g., two terminal windows against one kernel) requires
  coordination that belongs in a shared proxy, not in each frontend.
- Alternative frontends (web, Discord, Telegram) have no reference contract to
  build against.

The CLI proxy capsule solves all of these by acting as the single point of
contact between the IPC bus and external clients. This RFC pins down that
contract so that any conforming client can connect, and any conforming proxy
implementation can serve them.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## The display server model

In a traditional OS, applications do not write pixels to the framebuffer
directly. They talk to a display server (X11, Wayland) over a Unix socket. The
display server multiplexes access, enforces permissions, and translates between
the application protocol and the hardware.

The CLI proxy plays the same role in Astrid. The kernel's IPC bus is the
internal communication fabric (the "hardware"). TUI frontends are the
applications. The proxy sits between them:

```
+------------------+       +------------------+       +-------------------+
|  TUI client #1   | <---> |                  | <---> |                   |
+------------------+  UDS  |   CLI proxy      |  IPC  |   Astrid kernel   |
+------------------+       |   capsule        |       |   event bus       |
|  TUI client #2   | <---> |                  | <---> |                   |
+------------------+       +------------------+       +-------------------+
```

UDS = Unix domain socket. IPC = kernel IPC event bus.

## Connecting

A client opens a connection to the Unix domain socket whose path the kernel
injected into the proxy capsule at boot. The path is deterministic per-session
and discoverable through the kernel's session metadata. The proxy accepts up to
8 concurrent connections.

## Sending input

A client writes a JSON message to the socket. The message is an `IpcMessage`
envelope containing a topic and payload. The proxy checks the topic against its
ingress allowlist. If the topic is allowed, the proxy publishes the message onto
the IPC bus. If not, the proxy drops the message and logs a warning.

Allowed topics:

- `user.v1.prompt` - user sends a prompt
- `cli.v1.command.execute` - CLI-specific command execution
- `astrid.v1.request.*` - general request topics
- `astrid.v1.elicit.response.*` - elicitation responses
- `astrid.v1.approval.response.*` - approval responses
- `registry.v1.selection.*` - registry selection topics
- `session.v1.request.*` - session management requests

A client that tries to publish to `capsule.v1.lifecycle.started` or
`llm.v1.stream.delta` will have its message silently dropped. The proxy writes
a warning to its own log but does not disconnect the client.

## Receiving events

The proxy subscribes to all TUI-relevant IPC topics and broadcasts each event to
every connected client. Events include agent responses, streaming tokens,
approval requests, onboarding fields, elicitation requests, capsule status
changes, registry events, and session responses.

The proxy pre-serializes each outbound message once, then writes the same bytes
to every connected client stream. This avoids redundant serialization when
multiple clients are connected.

## Disconnection

When a client disconnects (broken pipe, explicit close, timeout), the proxy
detects the dead stream during the next read or broadcast cycle, closes it, and
releases the connection slot. No reconnection protocol exists at this layer;
the client simply opens a new connection.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Socket binding

The kernel injects the Unix socket path into the CLI proxy capsule via its boot
configuration. The path follows the pattern:

```
/run/astrid/sessions/<session-id>/cli-proxy.sock
```

The proxy binds this path at startup. If the path already exists (stale socket
from a crashed prior instance), the proxy unlinks it before binding. The proxy
sets the socket permissions to `0600`, restricting access to the owning user.

## Wire format

All messages on the Unix socket are newline-delimited JSON. Each line is a
complete JSON object representing one `IpcMessage`:

```json
{
  "topic": "user.v1.prompt",
  "payload": { ... },
  "signature": "base64-encoded-ed25519-sig",
  "source_id": "client-uuid",
  "timestamp": 1710576000000
}
```

### IpcMessage fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `topic` | `string` | Yes | Dot-separated topic path. |
| `payload` | `object` | Yes | Topic-specific payload (see below). |
| `signature` | `string` | No | Ed25519 signature over `topic + payload + timestamp`. Present when the message originates from a signed source. |
| `source_id` | `string` | Yes | UUID identifying the message source. For ingress messages, this is the client's self-assigned ID. For egress messages, this is the originating capsule or kernel component ID. |
| `timestamp` | `u64` | Yes | Unix timestamp in milliseconds. |

### Framing

Messages are delimited by `\n` (0x0A). A single message must not contain
literal newlines in its JSON encoding; the serializer must produce compact
single-line JSON. The maximum message size is 1 MiB. Messages exceeding this
limit are dropped and the proxy logs a warning.

## Egress: IPC to clients

The proxy subscribes to the following IPC topic patterns and broadcasts matching
messages to all connected clients:

| Topic pattern | Payload type | Description |
|---------------|-------------|-------------|
| `agent.v1.response.*` | `AgentResponse` | Agent text replies (partial and final). |
| `llm.v1.stream.*` | `LlmStreamEvent` | Streaming token deltas from the LLM provider. |
| `astrid.v1.onboarding.*` | `OnboardingRequired` | Schema-aware onboarding field collection. |
| `astrid.v1.elicit.request.*` | `ElicitRequest` | Install-time or runtime user input requests. |
| `astrid.v1.approval.request.*` | `ApprovalRequired` | Approval gate events requiring user confirmation. |
| `capsule.v1.*` | Various | Capsule lifecycle events (started, stopped, failed, health). |
| `registry.v1.*` | Various | Registry events (model picker, selection required, search results). |
| `session.v1.response.*` | Various | Session management responses (created, restored, listed). |
| `connection.v1.*` | `Connect` / `Disconnect` | Connection lifecycle events. |

### Broadcast semantics

For each inbound IPC message matching a subscribed topic:

1. Serialize the `IpcMessage` to compact JSON once.
2. Append `\n`.
3. Write the resulting bytes to every active client stream.
4. If a write fails (broken pipe, connection reset), mark that stream as dead.
5. After the broadcast pass, close and remove all dead streams.

The proxy does not buffer messages for disconnected clients. If a client is not
connected when an event fires, that event is lost for that client. Clients that
need history should request a session replay via `session.v1.request.replay`
after connecting.

## Ingress: clients to IPC

### Allowlist

The proxy maintains a static ingress allowlist. A client message is forwarded to
the IPC bus only if its topic matches the allowlist. Matching uses two rules:

**Exact match:**

| Topic | Payload type | Description |
|-------|-------------|-------------|
| `user.v1.prompt` | `UserInput` | User sends a prompt to the agent. |
| `cli.v1.command.execute` | `CliCommand` | CLI-specific command (e.g., `/clear`, `/status`). |

**Prefix match** (topic starts with the prefix):

| Prefix | Payload type | Description |
|--------|-------------|-------------|
| `astrid.v1.request.` | Various | General kernel requests from the user. |
| `astrid.v1.elicit.response.` | `ElicitResponse` | User replies to an elicitation prompt. |
| `astrid.v1.approval.response.` | `ApprovalResponse` | User approves or denies a gated action. |
| `registry.v1.selection.` | `SelectionResponse` | User selects from a registry picker. |
| `session.v1.request.` | Various | Session lifecycle requests (create, restore, list). |

**Rejection behavior:** Messages with topics not matching any allowlist entry are
dropped silently from the IPC bus perspective. The proxy logs a warning at
`WARN` level with the rejected topic and the client's `source_id`. The client
connection remains open. No error response is sent to the client.

### Ingress validation

Beyond topic filtering, the proxy performs the following validation on ingress
messages:

1. **JSON parse** - the message must be valid JSON conforming to `IpcMessage`.
   Malformed messages are dropped and a `WARN` log is emitted.
2. **Size check** - messages exceeding 1 MiB are dropped.
3. **Required fields** - `topic`, `payload`, `source_id`, and `timestamp` must
   all be present. Missing fields cause a drop with `WARN`.
4. **Timestamp skew** - the `timestamp` must be within 60 seconds of the
   proxy's wall clock. Stale or future-dated messages are dropped. This
   prevents replay of captured messages.

The proxy does not validate `signature` on ingress. Signature verification is
the kernel's responsibility. The proxy's job is topic-level access control, not
cryptographic authentication.

## Key IPC payload types

These are the primary payload structures exchanged over the protocol. All fields
use `snake_case`. Optional fields may be omitted from the JSON.

### UserInput

Sent by the client when the user submits a prompt.

```json
{
  "text": "Explain the capability model",
  "session_id": "uuid",
  "context": {
    "working_directory": "/home/user/project",
    "selected_files": ["src/main.rs"]
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | `string` | Yes | The user's input text. |
| `session_id` | `string` | Yes | UUID of the active session. |
| `context` | `object` | No | Optional context (working directory, selected files, etc.). |

### AgentResponse

Sent by the kernel when an agent produces output.

```json
{
  "text": "The capability model uses ed25519...",
  "is_final": true,
  "session_id": "uuid"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | `string` | Yes | The agent's response text. |
| `is_final` | `bool` | Yes | `true` if this is the final chunk; `false` for partial responses. |
| `session_id` | `string` | Yes | UUID of the session. |

### LlmStreamEvent

Streaming token events from the LLM provider, forwarded through the kernel.

```json
{
  "delta": "cap",
  "session_id": "uuid",
  "sequence": 42,
  "done": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `delta` | `string` | Yes | The token fragment. |
| `session_id` | `string` | Yes | UUID of the session. |
| `sequence` | `u64` | Yes | Monotonically increasing sequence number within the stream. |
| `done` | `bool` | Yes | `true` on the final event in the stream. |

### ApprovalRequired

Sent by the kernel when a gated action needs user confirmation.

```json
{
  "approval_id": "uuid",
  "action": "fs_write",
  "description": "Write to /etc/config.toml",
  "capsule_id": "uuid",
  "session_id": "uuid",
  "risk_level": "high"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `approval_id` | `string` | Yes | Unique ID for this approval request. |
| `action` | `string` | Yes | The action being gated. |
| `description` | `string` | Yes | Human-readable description of the action. |
| `capsule_id` | `string` | Yes | The capsule requesting approval. |
| `session_id` | `string` | Yes | UUID of the session. |
| `risk_level` | `string` | No | Risk classification: `low`, `medium`, `high`, `critical`. |

### ApprovalResponse

Sent by the client in reply to an `ApprovalRequired` event.

```json
{
  "approval_id": "uuid",
  "approved": true,
  "session_id": "uuid"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `approval_id` | `string` | Yes | Must match the `approval_id` from the request. |
| `approved` | `bool` | Yes | `true` to approve, `false` to deny. |
| `session_id` | `string` | Yes | UUID of the session. |

### OnboardingRequired

Sent by the kernel when a capsule needs schema-aware field collection from the
user.

```json
{
  "capsule_id": "uuid",
  "fields": [
    {
      "name": "api_key",
      "field_type": "secret",
      "label": "API Key",
      "description": "Your provider API key",
      "required": true,
      "default": null
    },
    {
      "name": "model",
      "field_type": "enum",
      "label": "Model",
      "description": "Select a model",
      "required": true,
      "default": "gpt-4",
      "options": ["gpt-4", "gpt-3.5-turbo", "claude-3"]
    }
  ],
  "session_id": "uuid"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `capsule_id` | `string` | Yes | The capsule requesting onboarding. |
| `fields` | `array<OnboardingField>` | Yes | Ordered list of fields to collect. |
| `session_id` | `string` | Yes | UUID of the session. |

**OnboardingField:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | `string` | Yes | Machine-readable field name. |
| `field_type` | `string` | Yes | One of: `text`, `secret`, `enum`, `array`. |
| `label` | `string` | Yes | Human-readable label. |
| `description` | `string` | No | Help text for the field. |
| `required` | `bool` | Yes | Whether the field must be filled. |
| `default` | `any` | No | Default value (type depends on `field_type`). |
| `options` | `array<string>` | No | Valid options (required when `field_type` is `enum`). |

### ElicitRequest / ElicitResponse

Elicitation is used when a capsule needs runtime input from the user (distinct
from onboarding, which happens at install time).

**ElicitRequest:**

```json
{
  "elicit_id": "uuid",
  "prompt": "Enter the target directory",
  "field_type": "text",
  "capsule_id": "uuid",
  "session_id": "uuid"
}
```

**ElicitResponse:**

```json
{
  "elicit_id": "uuid",
  "value": "/home/user/target",
  "session_id": "uuid"
}
```

### SelectionRequired

Sent when the user needs to pick from a list (model picker, capsule selector).

```json
{
  "selection_id": "uuid",
  "prompt": "Select a model",
  "options": [
    { "id": "gpt-4", "label": "GPT-4", "description": "OpenAI GPT-4" },
    { "id": "claude-3", "label": "Claude 3", "description": "Anthropic Claude 3" }
  ],
  "session_id": "uuid"
}
```

### Connect / Disconnect

Connection lifecycle events. The proxy publishes these to the IPC bus when
clients connect or disconnect, and broadcasts received lifecycle events to
clients.

**Connect:**

```json
{
  "client_id": "uuid",
  "connected_at": 1710576000000
}
```

**Disconnect:**

```json
{
  "client_id": "uuid",
  "disconnected_at": 1710576000000,
  "reason": "broken_pipe"
}
```

## Connection lifecycle

### Accept loop

The proxy runs an async accept loop on the bound Unix socket. Each accepted
connection is assigned a slot in the active streams table (maximum 8 slots). If
all slots are full, the proxy closes the new connection immediately and logs
a `WARN`.

### Read cycle

The proxy polls each active stream with a 50ms read timeout per stream per
cycle. This prevents a slow or idle client from blocking reads on other streams.

For each stream:

1. Attempt to read a line (up to 1 MiB).
2. If data is available, parse as `IpcMessage`, validate, check allowlist,
   and publish to IPC bus if allowed.
3. If the read returns EOF or an error, mark the stream as dead.
4. If the timeout expires, move to the next stream.

### Dead stream cleanup

Dead streams detected during read or broadcast phases are cleaned up:

1. Call `close()` on the underlying socket.
2. Remove the stream from the active streams table.
3. Publish a `Disconnect` event to the IPC bus with the client's `source_id`.
4. Log the disconnection at `INFO` level.

This explicit cleanup prevents slot exhaustion. Without it, a client that
disconnects without a clean shutdown would permanently consume a slot.

### Concurrency model

The proxy uses a single-threaded async event loop (tokio). The read cycle and
broadcast cycle alternate. This avoids the complexity of concurrent writes to
the same stream and eliminates the need for per-stream locks.

The cycle is:

```
loop {
    1. Poll IPC bus for new messages (non-blocking drain)
    2. Broadcast any received messages to all active clients
    3. Poll each client stream for ingress messages (50ms timeout each)
    4. Publish any valid ingress messages to IPC bus
    5. Clean up dead streams
}
```

Worst-case latency for a single cycle with 8 connected clients (all idle):
8 * 50ms = 400ms. This is acceptable for interactive TUI use. If lower latency
becomes necessary, the timeout can be reduced or the design can migrate to a
select-based model that polls all streams concurrently.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Socket bind fails | Capsule exits with error. Kernel detects capsule death and reports it. |
| Malformed JSON from client | Drop message, log `WARN`, keep connection open. |
| Message exceeds 1 MiB | Drop message, log `WARN`, keep connection open. |
| Topic not in allowlist | Drop message, log `WARN`, keep connection open. |
| Timestamp skew > 60s | Drop message, log `WARN`, keep connection open. |
| Client stream write fails | Mark stream dead, clean up on next cycle. |
| Client stream read EOF | Mark stream dead, clean up on next cycle. |
| All 8 slots full | Reject new connection immediately, log `WARN`. |
| IPC bus disconnected | Capsule exits. Kernel restarts or reports failure. |

The proxy never panics on client misbehavior. All client-sourced errors are
handled gracefully with log warnings and (at worst) connection termination.

# Drawbacks
[drawbacks]: #drawbacks

- **Unix socket only.** This protocol is inherently local. Remote frontends
  (web, Discord) need a separate transport layer (likely WebSocket or gRPC)
  wrapping the same message format. The protocol does not address that
  transport mapping.
- **No message queuing.** Clients that disconnect and reconnect lose all events
  that fired while disconnected. For long-running agent tasks, this means a
  reconnecting TUI may show an incomplete conversation.
- **Sequential stream polling.** The 50ms-per-stream timeout means worst-case
  ingress latency scales linearly with connected clients. With 8 clients, a
  single cycle takes up to 400ms.
- **8-client limit is static.** The maximum concurrent connections is a
  compile-time constant rather than a configuration parameter.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why a proxy capsule instead of direct IPC access for frontends?**

Direct IPC access would require the kernel to expose the event bus to external
processes. This breaks the isolation model: every frontend becomes a trusted
component with full publish access to every topic. The proxy capsule interposes
an allowlist, enforcing that clients can only publish to topics appropriate for
user input. This is the same principle behind a display server - applications
do not get raw framebuffer access.

**Why Unix domain socket instead of TCP?**

The CLI proxy serves local clients. Unix sockets provide file-permission-based
access control (`0600`), zero network overhead, and no port management. TCP
adds unnecessary attack surface and complexity for a local-only protocol.

**Why newline-delimited JSON instead of a binary protocol?**

JSON is human-readable, debuggable with standard tools (`socat`, `jq`), and
matches the existing IPC payload format. The messages are small (sub-kilobyte
for most events). Binary framing (protobuf, msgpack) would add a build
dependency and tooling burden for negligible bandwidth savings.

**Why not length-prefixed framing?**

Newline-delimited JSON is simpler and widely supported. Length-prefixed framing
handles embedded newlines better, but we mandate compact single-line JSON
serialization, making literal newlines in the payload impossible.

**Why pre-serialize once for broadcast?**

With N connected clients receiving the same message, serializing once and writing
the same byte buffer N times avoids N redundant serialization passes. For
streaming token events (high frequency, small payloads), this matters.

**What is the impact of not standardizing this?**

Each frontend would invent its own bridge, leading to inconsistent allowlists,
duplicated serialization logic, and no guarantee that a client built for one
proxy works with another. The protocol becomes an implicit contract buried in
implementation code rather than an explicit specification.

# Prior art
[prior-art]: #prior-art

- **X11 / Wayland display server protocol.** The closest architectural analogy.
  A display server multiplexes access between applications and the graphics
  subsystem over a Unix socket. X11 uses a custom binary protocol; Wayland uses
  a message-based protocol with file descriptor passing. Astrid's CLI proxy
  serves the same role: multiplexing access between TUI frontends and the kernel
  event bus.
- **Docker daemon socket** (`/var/run/docker.sock`). The Docker daemon listens
  on a Unix socket and serves an HTTP API. Clients (docker CLI, portainer, etc.)
  connect over this socket. Access control is file-permission-based. The CLI
  proxy follows the same pattern: a daemon socket with permission-gated access.
- **tmux client/server.** tmux runs a server process that owns terminal state.
  Clients attach and detach over a Unix socket. Multiple clients can attach to
  the same session. The CLI proxy supports the same multi-client pattern.
- **Neovim remote API.** Neovim exposes a msgpack-RPC API over Unix socket or
  TCP. External UIs connect to it. The CLI proxy is similar in concept but uses
  JSON and a publish/subscribe model instead of RPC.
- **NATS / Redis Pub/Sub.** General-purpose pub/sub systems with topic-based
  routing. The CLI proxy is a minimal, embedded version of this pattern, scoped
  to the TUI use case with an ingress allowlist.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Session replay protocol.** Clients that reconnect need to catch up on missed
  events. The `session.v1.request.replay` topic is referenced but its payload
  and semantics are not defined here. A follow-up RFC should specify the replay
  protocol.
- **Authentication handshake.** The current design relies on Unix socket file
  permissions for access control. Should there be an explicit authentication
  handshake after connection (e.g., presenting a session token)? This matters
  if the socket permissions are relaxed for multi-user scenarios.
- **Backpressure.** If a client reads slowly and the proxy's write buffer fills,
  the current design marks the stream as dead. Should there be an explicit
  backpressure mechanism (flow control, message dropping with notification)
  before disconnecting?
- **Client-to-client messaging.** Should clients be able to address messages to
  other connected clients through the proxy, or is all communication strictly
  client-to-kernel?
- **Configurable connection limit.** The 8-client limit is static. Should it be
  configurable via the capsule manifest or boot configuration?

# Future possibilities
[future-possibilities]: #future-possibilities

- **WebSocket transport layer.** A thin adapter that terminates WebSocket
  connections and bridges them to the same `IpcMessage` protocol. This enables
  web-based frontends without changing the message format or allowlist.
- **Protocol versioning.** Adding a version field to the handshake (or to each
  message) enables backward-compatible evolution of the wire format.
- **Client capabilities.** Instead of a single global allowlist, each client
  could present a capability token at connection time that determines which
  topics it can publish to. This aligns with the kernel's capability model.
- **Multiplexed sessions.** A single client connection could subscribe to events
  from multiple sessions, enabling a dashboard TUI that monitors several agents.
- **Binary wire format option.** For high-throughput scenarios (large file
  transfers, binary tool outputs), an optional msgpack or protobuf framing mode
  could be negotiated at connection time.
- **gRPC gateway.** A proxy-of-proxies that exposes the CLI proxy protocol over
  gRPC for remote frontends (Discord bot, Telegram bot, Slack integration).
