- Feature Name: `lifecycle_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#15](https://github.com/unicity-astrid/rfcs/pull/15)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `astrid.v1.lifecycle` event contract defines the event types, metadata
schema, and behavioral guarantees for lifecycle events on the Astrid event bus.
Lifecycle events are broadcast as `AstridEvent` variants (not `IpcPayload`)
and routed by `event_type()` string. Capsules subscribe via interceptor
patterns in `Capsule.toml`.

# Motivation
[motivation]: #motivation

Capsules need to observe runtime state changes (sessions created, tools
called, models resolved, agents spawned) for hooks, auditing, metrics, and
orchestration. The lifecycle contract must guarantee that:

- Events have a consistent metadata envelope (event ID, timestamp,
  correlation ID, session ID, source).
- Interceptor patterns use the same topic-matching semantics as IPC.
- The event vocabulary is enumerated so capsules know what to subscribe to.
- New event types can be added without breaking existing capsules.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Lifecycle events are broadcast by the kernel and orchestrator at state
transitions. Unlike IPC messages (which carry payloads between specific
actors), lifecycle events are observational. They inform subscribers that
something happened.

A capsule that wants to run a hook when a tool call completes:

```toml
# Capsule.toml
[[interceptor]]
event = "astrid.v1.lifecycle.tool_call_completed"
action = "on_tool_completed"
```

The capsule's `on_tool_completed` handler receives the event metadata and
fields (`call_id`, `tool_name`, `duration_ms`).

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Event routing

Lifecycle events are `AstridEvent` enum variants. Each variant has an
`event_type()` method returning a string like
`astrid.v1.lifecycle.tool_call_completed`. The `EventDispatcher` matches this
string against capsule interceptor patterns using segment-level matching:

- `*` matches exactly one segment.
- Pattern and event must have equal segment count.
- No trailing multi-segment wildcard.

Example: `astrid.v1.lifecycle.*` matches any single event type. To match all
lifecycle events, use `astrid.v1.lifecycle.*`.

## Event metadata

Every lifecycle event carries `EventMetadata`:

```json
{
  "event_id": "uuid",
  "timestamp": "ISO 8601 datetime",
  "correlation_id": "uuid | null",
  "session_id": "uuid | null",
  "user_id": "uuid | null",
  "source": "string (e.g. \"kernel\", \"react-loop\")"
}
```

## Event catalog

### Kernel lifecycle

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.kernel_started` | `version: String` | Kernel boot complete |
| `astrid.v1.lifecycle.kernel_shutdown` | `reason: Option<String>` | Kernel shutting down |
| `astrid.v1.lifecycle.runtime_started` | `version: String` | Runtime initialized |
| `astrid.v1.lifecycle.runtime_stopped` | `reason: Option<String>` | Runtime stopped |
| `astrid.v1.lifecycle.config_reloaded` | (none) | Configuration reloaded |
| `astrid.v1.lifecycle.config_changed` | `key: String` | Specific config key changed |
| `astrid.v1.lifecycle.health_check_completed` | `healthy: bool, checks_performed: u32, checks_failed: u32` | Health check result |

### Capsule lifecycle

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.capsule_loaded` | `capsule_id: String, capsule_name: String` | Capsule loaded successfully |
| `astrid.v1.lifecycle.capsule_failed` | `capsule_id: String, error: String` | Capsule failed to load |
| `astrid.v1.lifecycle.capsule_unloaded` | `capsule_id: String, capsule_name: String` | Capsule unloaded |

### Agent lifecycle

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.agent_started` | `agent_id: Uuid, agent_name: String` | Agent started |
| `astrid.v1.lifecycle.agent_stopped` | `agent_id: Uuid, reason: Option<String>` | Agent stopped |
| `astrid.v1.lifecycle.agent_loop_completed` | `agent_id: Uuid, turns: u32, duration_ms: u64` | React loop finished |

### Session lifecycle

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.session_created` | `session_id: Uuid` | New session created |
| `astrid.v1.lifecycle.session_ended` | `session_id: Uuid, reason: Option<String>` | Session ended |
| `astrid.v1.lifecycle.session_resumed` | `session_id: Uuid` | Existing session resumed |
| `astrid.v1.lifecycle.session_resetting` | `session_id: Uuid` | Session being cleared |

### Message lifecycle

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.message_received` | `message_id: Uuid, platform: String` | User message received |
| `astrid.v1.lifecycle.message_sending` | `message_id: Uuid, platform: String` | Agent message being sent |
| `astrid.v1.lifecycle.message_sent` | `message_id: Uuid, platform: String` | Agent message delivered |
| `astrid.v1.lifecycle.message_processed` | `message_id: Uuid, duration_ms: u64` | Full processing complete |

### LLM lifecycle

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.model_resolving` | `request_id: Uuid, provider: Option<String>, model: Option<String>` | Model being resolved |
| `astrid.v1.lifecycle.llm_request_started` | `request_id: Uuid, provider: String, model: String` | LLM API call started |
| `astrid.v1.lifecycle.llm_request_completed` | `request_id: Uuid, success: bool, input_tokens: Option<u32>, output_tokens: Option<u32>, duration_ms: u64` | LLM API call finished |
| `astrid.v1.lifecycle.llm_stream_started` | `request_id: Uuid, model: String` | Streaming begun |
| `astrid.v1.lifecycle.llm_stream_chunk` | `request_id: Uuid, chunk_index: u32, token_count: u32` | Chunk received |
| `astrid.v1.lifecycle.llm_stream_completed` | `request_id: Uuid, input_tokens: Option<u32>, output_tokens: Option<u32>, duration_ms: u64` | Streaming complete |
| `astrid.v1.lifecycle.prompt_building` | `request_id: Uuid` | Prompt assembly started |

### Tool lifecycle

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.tool_call_started` | `call_id: Uuid, tool_name: String, server_name: Option<String>` | Tool execution started |
| `astrid.v1.lifecycle.tool_call_completed` | `call_id: Uuid, tool_name: String, duration_ms: u64` | Tool execution succeeded |
| `astrid.v1.lifecycle.tool_call_failed` | `call_id: Uuid, tool_name: String, error: String, duration_ms: u64` | Tool execution failed |
| `astrid.v1.lifecycle.tool_result_persisting` | `call_id: Uuid, tool_name: String` | Tool result being saved to session |

### Sub-agent lifecycle

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.sub_agent_spawned` | `subagent_id: Uuid, parent_id: Uuid, task: String, depth: u32` | Sub-agent created |
| `astrid.v1.lifecycle.sub_agent_progress` | `subagent_id: Uuid, message: String` | Sub-agent progress update |
| `astrid.v1.lifecycle.sub_agent_completed` | `subagent_id: Uuid, duration_ms: u64` | Sub-agent finished |
| `astrid.v1.lifecycle.sub_agent_failed` | `subagent_id: Uuid, error: String, duration_ms: u64` | Sub-agent failed |
| `astrid.v1.lifecycle.sub_agent_cancelled` | `subagent_id: Uuid, reason: Option<String>` | Sub-agent cancelled |

### MCP lifecycle

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.mcp_server_connected` | `server_name: String, protocol_version: String` | MCP server connected |
| `astrid.v1.lifecycle.mcp_server_disconnected` | `server_name: String, reason: Option<String>` | MCP server disconnected |
| `astrid.v1.lifecycle.mcp_tool_called` | `server_name: String, tool_name: String, arguments: Option<Value>` | MCP tool invoked |
| `astrid.v1.lifecycle.mcp_tool_completed` | `server_name: String, tool_name: String, success: bool, duration_ms: u64` | MCP tool finished |

### Security lifecycle

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.capability_granted` | `capability_id: Uuid, resource: String, action: String` | Capability token granted |
| `astrid.v1.lifecycle.capability_revoked` | `capability_id: Uuid, reason: Option<String>` | Capability revoked |
| `astrid.v1.lifecycle.capability_checked` | `resource: String, action: String, allowed: bool` | Capability check performed |
| `astrid.v1.lifecycle.authorization_denied` | `resource: String, action: String, reason: String` | Access denied |
| `astrid.v1.lifecycle.security_violation` | `violation_type: String, details: String` | Security violation detected |
| `astrid.v1.lifecycle.approval_requested` | `request_id: Uuid, resource: String, action: String, description: String` | Human approval requested |
| `astrid.v1.lifecycle.approval_granted` | `request_id: Uuid, duration: Option<String>` | Approval granted |
| `astrid.v1.lifecycle.approval_denied` | `request_id: Uuid, reason: Option<String>` | Approval denied |

### Budget lifecycle

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.budget_allocated` | `budget_id: Uuid, amount_cents: u64, currency: String` | Budget allocated |
| `astrid.v1.lifecycle.budget_warning` | `budget_id: Uuid, remaining_cents: u64, percent_used: f64` | Budget threshold warning |
| `astrid.v1.lifecycle.budget_exceeded` | `budget_id: Uuid, overage_cents: u64` | Budget exceeded |

### Audit

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.audit_entry_created` | `entry_id: Uuid, entry_type: String` | Audit log entry written |

### Error

| event_type | Fields | Description |
|------------|--------|-------------|
| `astrid.v1.lifecycle.error_occurred` | `code: String, message: String, stack_trace: Option<String>` | Unhandled error |

## Behavioral requirements

The **kernel** must:

1. Broadcast lifecycle events at every state transition listed above.
2. Include populated `EventMetadata` with every event.
3. Set `correlation_id` to link related events (e.g. all events in one
   react loop turn share a correlation ID).

A conforming **capsule** subscribing to lifecycle events must:

1. Declare interceptor patterns in `Capsule.toml`.
2. Handle events idempotently (the bus may deliver duplicates after lag).
3. Not block on lifecycle event handling (these are fire-and-forget
   notifications, not requests).

# Drawbacks
[drawbacks]: #drawbacks

- 42+ event types is a large surface. New capsule authors must understand
  which subset they need.
- No guaranteed delivery. Lifecycle events use broadcast channels; a slow
  subscriber may lag and miss events.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why `AstridEvent` variants instead of `IpcPayload`?** Lifecycle events are
observational, not transactional. They do not need the `IpcMessage` envelope
(source_id, signature, topic). The simpler `AstridEvent` type reduces
overhead.

**Why string-based event_type matching?** Reuses the existing topic-matching
infrastructure. Capsules declare interceptors the same way for both IPC
topics and lifecycle events.

# Prior art
[prior-art]: #prior-art

- **Kubernetes events**: Informational records of state changes. Similar
  observational model.
- **OpenTelemetry spans**: Structured lifecycle tracking with correlation.
  Astrid's `correlation_id` serves a similar purpose.
- **systemd journal**: Structured logging of service lifecycle events.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should lifecycle events carry severity levels for filtering?
- Should there be a guaranteed-delivery channel for critical events
  (security violations, budget exceeded)?

# Future possibilities
[future-possibilities]: #future-possibilities

- Event replay for debugging (persisted lifecycle events).
- Event filtering at the bus level (subscribers declare interest predicates).
- Custom lifecycle events from capsules (user-defined event_type strings).
