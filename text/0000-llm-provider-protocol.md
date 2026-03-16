- Feature Name: `llm_provider_protocol`
- Start Date: 2026-03-16
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The LLM provider protocol defines how generation requests flow from the
orchestrator to vendor-specific provider capsules and back as a stream of
events. Each provider capsule is a device driver: it subscribes to a
per-provider request topic, translates to the vendor API, and publishes
standardized `LlmStreamEvent` messages. The protocol is streaming-only and
correlates requests to responses via UUID.

This RFC documents the protocol as currently implemented in
`astrid-capsule-anthropic`. It is not a proposal for new work.

# Motivation
[motivation]: #motivation

The orchestrator needs to call LLMs. Different providers expose different
APIs, auth schemes, and streaming formats. Without a standard protocol:

- The orchestrator must contain provider-specific code for every backend.
- Adding a new provider means patching the kernel.
- Testing requires real API keys.
- Capability scoping (network access per provider) is impossible.

Provider capsules push vendor translation into user-space. The orchestrator
publishes a request and consumes a stream. It never touches HTTP or SSE.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Three actors participate in LLM generation:

1. **React loop** - decides to call an LLM, publishes a request.
2. **Provider capsule** - translates to vendor API, streams back events.
3. **Registry** - tells the react loop which provider topic to target.

```text
React loop                    Anthropic capsule           Anthropic API
    |                               |                          |
    |-- LlmRequest (on ----------->|                          |
    |   llm.v1.request.generate.   |-- HTTP POST /v1/messages |
    |   anthropic)                 |   (streaming)            |
    |                               |<-- SSE stream -----------|
    |<-- LlmStreamEvent -----------|   (TextDelta)            |
    |<-- LlmStreamEvent -----------|   (ToolCallStart)        |
    |<-- LlmStreamEvent -----------|   (Usage)                |
    |<-- LlmStreamEvent -----------|   (Done)                 |
```

The Anthropic capsule reads the API key from its environment (`anthropic_api_key`
or `api_key`), builds the vendor request body, makes a streaming HTTP call via
the HTTP airlock, parses SSE events, and publishes standardized stream events.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## IPC topics

| Direction | Topic | Payload | Actor |
|---|---|---|---|
| Subscribe | `llm.v1.request.generate.anthropic` | `LlmRequest` | Anthropic capsule |
| Publish | `llm.v1.stream.anthropic` | `LlmStreamEvent` | Anthropic capsule |

The provider name suffix (`anthropic`) is derived from the capsule manifest.
Multiple providers coexist by using different suffixes.

## Message schemas

### LlmRequest

```json
{
  "LlmRequest": {
    "request_id": "UUID",
    "model": "string (e.g. claude-sonnet-4-20250514)",
    "messages": [{ "role": "User|Assistant|Tool|System", "content": "..." }],
    "tools": [{ "name": "string", "description": "string", "input_schema": {} }],
    "system": "string (system prompt)"
  }
}
```

### LlmStreamEvent

```json
{
  "LlmStreamEvent": {
    "request_id": "UUID (matches request)",
    "event": { "TextDelta": "string" }
  }
}
```

### StreamEvent variants

| Variant | Shape | Description |
|---|---|---|
| `TextDelta` | `string` | Partial text output |
| `ToolCallStart` | `{ id, name }` | Model started a tool call |
| `ToolCallDelta` | `{ id, args_delta }` | Incremental tool call arguments |
| `ToolCallEnd` | `{ id }` | Tool call arguments complete |
| `Usage` | `{ input_tokens, output_tokens }` | Token usage for the request |
| `Done` | (unit) | Stream completed successfully |
| `Error` | `string` | Stream terminated with error |

## Anthropic capsule behavior

1. Receives `LlmRequest` via interceptor on `llm.v1.request.generate.anthropic`.
2. Filters out `System` role messages (system prompt passed separately).
3. Converts messages to Anthropic API format (tool calls as `tool_use` blocks,
   tool results as `tool_result` blocks, multi-part content).
4. Sends POST to `https://api.anthropic.com/v1/messages` with `stream: true`.
5. Parses SSE response: `message_start`, `content_block_start`,
   `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`.
6. Maps each SSE event to the corresponding `StreamEvent` variant.
7. On error, publishes `StreamEvent::Error` and stops.

The capsule hardcodes `claude-sonnet-4-20250514` as the model and `max_tokens: 8192`.

## Error handling

| Condition | Behavior |
|---|---|
| Missing API key | Publish `Error("anthropic_api_key not configured")` |
| Non-200 HTTP status | Publish `Error("Anthropic API error ({status}): {body}")` |
| SSE parse failure | Skip unparseable event, continue stream |
| Network error | Publish `Error` with description |

## Capsule.toml manifest

```toml
[package]
name = "astrid-capsule-anthropic"
version = "0.1.0"
description = "Anthropic LLM Provider"

[[component]]
id = "anthropic"
file = "astrid_capsule_anthropic.wasm"
capabilities = { net = ["api.anthropic.com"] }

[capabilities]
ipc_publish = ["llm.v1.stream.anthropic"]

[env]
anthropic_api_key = { type = "secret", request = "Please enter your Anthropic API Key" }

[[llm_provider]]
id = "claude-3-5-sonnet-20241022"
description = "Claude 3.5 Sonnet"
capabilities = ["text", "vision", "tools"]

[[interceptor]]
event = "llm.v1.request.generate.anthropic"
action = "handle_llm_request"
```

# Drawbacks
[drawbacks]: #drawbacks

- Lowest common denominator. Vendor-specific features (cache control, logprobs)
  are not representable in the standard stream events.
- One capsule per provider. Each vendor requires building and maintaining a
  separate WASM capsule.
- The model name is currently hardcoded in the capsule, not derived from the
  `LlmRequest.model` field.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why streaming-only?** Every major LLM provider supports streaming. Users
expect token-by-token output. One code path in the orchestrator, no branching
on response type. Non-streaming vendors wrap output in the same events.

**Why a separate topic per provider?** Native IPC routing. A capsule subscribes
only to its own topic. Capability scoping is tight: a capsule can only publish
to its own stream topic.

**Why errors as stream events?** The orchestrator processes a uniform stream.
No separate error channel. The LLM can reason about errors in context.

# Prior art
[prior-art]: #prior-art

- **Anthropic Messages API**: SSE streaming with `content_block_delta` events.
  The capsule translates these to flat `StreamEvent` variants.
- **OpenAI Chat Completions API**: SSE streaming with `choices[0].delta`.
  Tool call deltas directly inspired `ToolCallDelta`.
- **Linux device drivers**: Kernel defines an interface, drivers implement it.
  Provider capsules follow the same pattern.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should the capsule respect the `model` field from `LlmRequest` instead of
  hardcoding the model?
- Should there be a vendor extension metadata field on `LlmRequest` for
  provider-specific features?
- Should `Done` carry the `StopReason` as a field?

# Future possibilities
[future-possibilities]: #future-possibilities

- Structured output mode via a `response_format` field on `LlmRequest`.
- Provider health metrics on a `llm.v1.status.{provider}` topic.
- Automatic failover: orchestrator re-publishes to provider B on Error from A.
