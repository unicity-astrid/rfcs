- Feature Name: `llm_provider_protocol`
- Start Date: 2026-03-16
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

This RFC defines the IPC contract between the orchestrator and LLM provider
capsules. Provider capsules act as device drivers: they subscribe to a
standardized request topic, translate requests into vendor-specific API calls,
and publish a standardized stream of response events back onto the bus. The
protocol is streaming-first, supports multiple simultaneous providers, and
correlates requests to responses via UUID.

# Motivation
[motivation]: #motivation

The orchestrator needs to call LLMs. Different providers (Anthropic, OpenAI,
local inference servers) expose wildly different APIs, authentication schemes,
streaming formats, and error semantics. Without a standard protocol, the
orchestrator must contain provider-specific code for every backend it supports.
That couples the kernel to vendor details it should never know about.

Provider capsules solve this by pushing vendor translation into user-space. Each
provider capsule is a device driver: it speaks the Astrid protocol on one side
and the vendor API on the other. The orchestrator publishes a request and
consumes a stream. It never touches an HTTP client, never parses SSE, never
handles rate limits. The capsule handles all of that.

This separation buys us:

- **Vendor isolation.** Adding a new provider means shipping a new capsule, not
  patching the kernel.
- **Parallel providers.** The topic namespace supports multiple providers running
  simultaneously. The orchestrator can route different models to different
  capsules.
- **Capability scoping.** Provider capsules can be granted only the network
  capabilities they need (e.g., `net:api.anthropic.com:443`) with no ambient
  authority.
- **Testability.** A mock provider capsule that replays canned streams makes
  integration testing deterministic.

Without this RFC, every capsule author who needs LLM access will invent their own
request/response format, their own streaming protocol, and their own error
handling. The ecosystem fragments before it starts.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## The device driver model

Think of LLM provider capsules as device drivers in an operating system. The OS
does not contain code for every printer, GPU, or network card. Instead, it
defines a standard interface. Driver authors implement that interface. User-space
programs call the interface without knowing which hardware sits behind it.

Astrid works the same way. The orchestrator does not contain code for Anthropic,
OpenAI, or any specific LLM vendor. It publishes a standardized `LlmRequest`
onto the IPC bus. A provider capsule picks it up, calls the vendor API, and
streams `LlmStreamEvent` messages back. The orchestrator consumes those events
without knowing which vendor produced them.

## Writing a provider capsule

A provider capsule must do three things:

1. **Subscribe** to `llm.v1.request.generate.{provider_name}` where
   `{provider_name}` is a unique identifier like `anthropic`, `openai`, or
   `local-llama`.
2. **Translate** each incoming `LlmRequest` into the vendor's native API format,
   call the vendor, and stream the response.
3. **Publish** `LlmStreamEvent` messages to `llm.v1.stream.{provider_name}` as
   the response arrives. The stream always ends with a `Done` event or an
   `Error` event.

Here is the lifecycle of a single request:

```
Orchestrator                          Provider Capsule
     |                                       |
     |-- LlmRequest (on request topic) ----->|
     |                                       |-- vendor HTTP call -->
     |                                       |<-- SSE stream -------
     |<-- TextDelta -------------------------|
     |<-- TextDelta -------------------------|
     |<-- ToolCallStart ---------------------|
     |<-- ToolCallDelta ---------------------|
     |<-- ToolCallEnd -----------------------|
     |<-- Usage -----------------------------|
     |<-- Done ------------------------------|
```

Every event carries the `request_id` from the original request. The orchestrator
uses this to correlate events to the request that triggered them.

## Multiple providers

Because the provider name is embedded in the topic, multiple providers can run
simultaneously:

- `llm.v1.request.generate.anthropic` - handled by the Anthropic capsule
- `llm.v1.request.generate.openai` - handled by the OpenAI capsule
- `llm.v1.request.generate.local-llama` - handled by a local inference capsule

The orchestrator chooses which provider to target by publishing to the
appropriate topic. Model routing logic lives in the orchestrator, not in the
providers.

## Error handling

If the vendor API returns an error (rate limit, auth failure, malformed request),
the provider capsule publishes an `Error` event with a human-readable message and
stops. The orchestrator never sees a half-finished stream without a terminal
event. Every stream ends with exactly one of `Done` or `Error`.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic naming

| Direction | Topic pattern | Payload |
|-----------|---------------|---------|
| Orchestrator to provider | `llm.v1.request.generate.{provider_name}` | `LlmRequest` |
| Provider to orchestrator | `llm.v1.stream.{provider_name}` | `LlmStreamEvent` |

`{provider_name}` is a lowercase alphanumeric identifier with hyphens allowed
(e.g., `anthropic`, `openai`, `local-llama`). It must match the regex
`[a-z0-9][a-z0-9-]*[a-z0-9]` or be a single character `[a-z0-9]`.

The `v1` segment enables future protocol revisions without breaking existing
capsules.

## LlmRequest

```rust
struct LlmRequest {
    /// Unique identifier for this request. All stream events reference this ID.
    request_id: Uuid,

    /// The model identifier (e.g., "claude-sonnet-4-5", "gpt-4o").
    /// The provider capsule maps this to whatever the vendor expects.
    model: String,

    /// The system prompt. Providers that support a dedicated system parameter
    /// should use it. Providers that do not should prepend it as a system
    /// message.
    system: String,

    /// The conversation history, in order.
    messages: Vec<Message>,

    /// Tool definitions available to the model. May be empty.
    tools: Vec<LlmToolDefinition>,
}
```

All fields are required. `system` may be an empty string. `messages` must contain
at least one message. `tools` may be an empty vec.

## Message

```rust
struct Message {
    role: Role,
    content: MessageContent,
}

enum Role {
    System,
    User,
    Assistant,
    Tool,
}

enum MessageContent {
    /// Plain text content.
    Text(String),

    /// One or more tool calls made by the assistant.
    ToolCalls(Vec<ToolCall>),

    /// The result of a tool call, sent with Role::Tool.
    ToolResult(ToolCallResult),

    /// Mixed content (text, images, etc.) in a single message.
    MultiPart(Vec<ContentPart>),
}
```

### ContentPart

`ContentPart` supports multi-modal content within a single message:

```rust
enum ContentPart {
    Text(String),
    Image { media_type: String, data: Vec<u8> },
}
```

Provider capsules that do not support images must return an `Error` event if an
`Image` part is present.

### ToolCall

```rust
struct ToolCall {
    /// Unique identifier for this tool call, assigned by the model.
    id: String,

    /// The tool name. Supports namespaced format "server:tool" for MCP tools.
    name: String,

    /// The arguments as a JSON object.
    arguments: serde_json::Value,
}
```

The `name` field supports a `"server:tool"` format for tools exposed by MCP
servers. For example, `"github:create_issue"` refers to the `create_issue` tool
on the `github` MCP server. Provider capsules pass this name through as-is; they
do not interpret the namespace.

### ToolCallResult

```rust
struct ToolCallResult {
    /// The ID of the tool call this result corresponds to.
    call_id: String,

    /// The tool's output. Typically a JSON string, but may be plain text.
    content: String,

    /// Whether the tool execution failed.
    is_error: bool,
}
```

## LlmToolDefinition

```rust
struct LlmToolDefinition {
    /// The tool name. Same format as ToolCall::name.
    name: String,

    /// Human-readable description of what the tool does.
    description: Option<String>,

    /// JSON Schema describing the tool's input parameters.
    input_schema: serde_json::Value,
}
```

The `input_schema` must be a valid JSON Schema object. Provider capsules pass it
to the vendor API in whatever format the vendor expects (e.g., Anthropic uses
`input_schema`, OpenAI uses `parameters`).

## LlmStreamEvent

```rust
struct LlmStreamEvent {
    /// The request this event belongs to.
    request_id: Uuid,

    /// The event payload.
    event: StreamEvent,
}
```

### StreamEvent

```rust
enum StreamEvent {
    /// A chunk of generated text.
    TextDelta(String),

    /// The model has started a tool call.
    ToolCallStart {
        /// The tool call ID assigned by the model.
        id: String,
        /// The tool name (may use "server:tool" format).
        name: String,
    },

    /// An incremental chunk of tool call arguments (partial JSON).
    ToolCallDelta {
        /// The tool call ID this delta belongs to.
        id: String,
        /// A fragment of the JSON arguments string.
        args_delta: String,
    },

    /// The model has finished emitting arguments for this tool call.
    ToolCallEnd {
        /// The tool call ID.
        id: String,
    },

    /// A chunk of model reasoning (for reasoning/thinking models).
    ReasoningDelta(String),

    /// Token usage for this request.
    Usage {
        input_tokens: u64,
        output_tokens: u64,
    },

    /// The stream completed successfully.
    Done,

    /// The stream terminated with an error.
    Error(String),
}
```

### StopReason

When the provider capsule emits `Done`, it should include the stop reason in a
preceding `Usage` or as metadata. The stop reason vocabulary is:

```rust
enum StopReason {
    /// The model finished its response naturally.
    EndTurn,
    /// The response hit the maximum token limit.
    MaxTokens,
    /// The model wants to use a tool.
    ToolUse,
    /// The model hit a stop sequence.
    StopSequence,
}
```

Provider capsules must map vendor-specific stop reasons to these variants.

## Stream ordering guarantees

1. **A stream begins with content events.** The first event for a given
   `request_id` is either `TextDelta`, `ToolCallStart`, `ReasoningDelta`, or
   `Error`.
2. **Tool call events are ordered.** For a given tool call ID: `ToolCallStart`
   comes first, then zero or more `ToolCallDelta`, then `ToolCallEnd`. Tool
   calls may interleave with `TextDelta` events but individual tool call
   sequences must not interleave with each other.
3. **Usage is reported exactly once.** A single `Usage` event must appear before
   `Done`. If the vendor does not report usage, the provider capsule must emit
   `Usage { input_tokens: 0, output_tokens: 0 }` to satisfy the contract.
4. **Every stream has a terminal event.** The last event is either `Done` or
   `Error`. Never both. If the provider capsule crashes mid-stream, the
   orchestrator detects this via IPC channel closure and treats it as an error.
5. **No events after terminal.** After `Done` or `Error`, the provider capsule
   must not publish any more events for that `request_id`.

## Streaming requirement

Streaming is mandatory. All provider capsules must emit events incrementally as
the vendor produces output. Non-streaming vendors (batch APIs, local models that
produce complete responses) must still conform: the capsule should emit one or
more `TextDelta` events containing the response text, followed by `Usage` and
`Done`.

This design keeps the orchestrator simple. It always processes a stream. It
never needs to distinguish between streaming and non-streaming providers.

## Error handling contract

Provider capsules must handle vendor errors gracefully:

| Vendor condition | Capsule behavior |
|-----------------|------------------|
| Authentication failure | Emit `Error("Authentication failed: <detail>")` |
| Rate limit exceeded | Emit `Error("Rate limited: retry after <seconds>s")` |
| Model not found | Emit `Error("Model not found: <model>")` |
| Malformed request | Emit `Error("Invalid request: <detail>")` |
| Network error | Emit `Error("Network error: <detail>")` |
| Vendor 5xx | Emit `Error("Provider error: <detail>")` |
| Stream interrupted | Emit `Error("Stream interrupted: <detail>")` |

The error string must be human-readable. It should include enough detail for
an operator to diagnose the problem without reading provider capsule logs.

Retry logic is the provider capsule's responsibility. If the capsule retries
and ultimately fails, it emits a single `Error` event. The orchestrator never
sees intermediate failures.

## Serialization

All payloads are serialized as JSON over the IPC bus. Field names use
`snake_case`. Enum variants use the externally tagged representation:

```json
{ "TextDelta": "Hello, " }
{ "ToolCallStart": { "id": "tc_01", "name": "github:create_issue" } }
{ "Usage": { "input_tokens": 150, "output_tokens": 42 } }
{ "Done": null }
{ "Error": "Rate limited: retry after 30s" }
```

`LlmRequest` example:

```json
{
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "model": "claude-sonnet-4-5",
  "system": "You are a helpful assistant.",
  "messages": [
    {
      "role": "User",
      "content": { "Text": "What is the weather in Tokyo?" }
    }
  ],
  "tools": [
    {
      "name": "weather:get_forecast",
      "description": "Get the weather forecast for a location.",
      "input_schema": {
        "type": "object",
        "properties": {
          "location": { "type": "string" }
        },
        "required": ["location"]
      }
    }
  ]
}
```

`LlmStreamEvent` example:

```json
{
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "event": { "TextDelta": "The weather in Tokyo is" }
}
```

## Capability requirements

Provider capsules require the following capabilities:

- **Network**: `net:<vendor-host>:<port>` (e.g., `net:api.anthropic.com:443`)
- **IPC subscribe**: `ipc:sub:llm.v1.request.generate.{provider_name}`
- **IPC publish**: `ipc:pub:llm.v1.stream.{provider_name}`

Provider capsules must not require filesystem access. API keys and configuration
are injected via the capsule's environment at boot, not read from disk.

# Drawbacks
[drawbacks]: #drawbacks

- **Lowest common denominator.** A standardized protocol cannot expose every
  vendor-specific feature. Anthropic's cache control, OpenAI's logprobs, and
  other vendor extensions are not representable in this protocol. Provider
  capsules that want to expose vendor-specific features need an extension
  mechanism not defined here.
- **Streaming overhead.** For providers that return complete responses, wrapping
  them in streaming events adds serialization overhead. In practice this is
  negligible compared to LLM inference latency.
- **Additional capsule per provider.** Each vendor requires a separate capsule
  to be built, tested, and maintained. This is the explicit trade-off: vendor
  complexity lives in user-space, not the kernel.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why streaming-first instead of request/response with optional streaming?**
Every major LLM provider supports streaming. Users expect token-by-token output.
Making streaming the only mode keeps the orchestrator simple: one code path, one
event processing loop, no conditional branching on response type. Non-streaming
providers wrap their output in the same event protocol at negligible cost.

**Why a separate topic per provider instead of a single topic with a provider
field?** Separate topics give the IPC bus native routing. A capsule subscribes
only to its own topic and never sees traffic meant for other providers. This
avoids fan-out waste and keeps capability scoping tight: a capsule can only
publish to its own stream topic.

**Why externally tagged enum serialization?** It matches serde's default for Rust
enums and is unambiguous to parse. Internally tagged or adjacently tagged
representations add complexity with no benefit for this use case.

**Why not use MCP sampling directly?** MCP's `sampling/createMessage` is a
request/response protocol designed for a different interaction pattern (server
requesting the client to sample). It does not support streaming, does not define
tool call streaming semantics, and does not support multiple simultaneous
providers. Astrid's protocol borrows concepts from MCP but defines a
streaming-first IPC contract tailored to the capsule runtime.

**Alternative: embed provider logic in the kernel.** This is what most agent
frameworks do. It works for prototypes but couples the kernel to every vendor SDK,
bloats the trusted computing base, and means adding a new provider requires a
kernel release. The device driver model pushes this complexity to user-space
where it belongs.

**Impact of not standardizing:** Every capsule that needs LLM access invents its
own protocol. The orchestrator grows provider-specific code paths. Testing
requires real API keys. The ecosystem fragments.

# Prior art
[prior-art]: #prior-art

- **Anthropic Messages API.** SSE-based streaming with `message_start`,
  `content_block_delta`, `message_delta`, and `message_stop` events. Astrid's
  `StreamEvent` enum simplifies this into flat variants (`TextDelta`,
  `ToolCallStart`, `ToolCallDelta`, etc.) that are easier to consume without
  nested state tracking.
- **OpenAI Chat Completions API.** SSE-based streaming with `choices[0].delta`
  chunks. Uses `finish_reason` for stop reasons. Tool calls arrive as
  incremental JSON fragments, which directly inspired `ToolCallDelta`.
- **MCP sampling (`sampling/createMessage`).** Defines a request/response
  protocol for LLM sampling with `messages`, `modelPreferences`, and
  `systemPrompt`. Does not define streaming semantics. Astrid's `LlmRequest`
  borrows the message structure but adds streaming, tool definitions, and
  provider routing.
- **LiteLLM.** A Python library that wraps multiple LLM providers behind a
  unified interface. Validates the need for provider abstraction but operates
  as a library, not an IPC protocol. Astrid's approach moves the abstraction
  boundary to a process isolation boundary.
- **Linux device drivers.** The kernel defines `struct file_operations` and
  drivers implement it. User-space programs call `read()` and `write()` without
  knowing the hardware. Provider capsules follow the same pattern: the
  orchestrator calls the protocol, the capsule translates to hardware (vendor
  API).

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Vendor extensions.** How should provider capsules expose vendor-specific
  features (cache control, logprobs, penalties, structured output constraints)?
  An extension metadata field on `LlmRequest` is one option but the schema
  needs design work.
- **Model capabilities discovery.** Should there be a mechanism for the
  orchestrator to query what models a provider capsule supports and their
  capabilities (context window, vision support, tool use support)?
- **Token counting.** Should provider capsules expose a token counting endpoint
  so the orchestrator can estimate costs before sending a request?
- **Request cancellation.** If the orchestrator wants to cancel an in-flight
  request, what is the mechanism? A cancel topic? A special IPC message?
- **StopReason delivery.** The current design mentions StopReason but does not
  assign it a dedicated event variant. Should `Done` carry the stop reason as
  a field, or should it remain separate metadata?

# Future possibilities
[future-possibilities]: #future-possibilities

- **Batch request support.** A `llm.v1.request.batch.{provider_name}` topic for
  bulk inference where latency is less important than throughput.
- **Structured output mode.** A field on `LlmRequest` that constrains the model
  to produce output matching a JSON Schema, using vendor-native structured output
  features where available.
- **Image and audio generation.** The provider protocol could expand to cover
  non-text modalities via new topic families (`image.v1.request.generate.*`,
  `audio.v1.request.generate.*`).
- **Provider health and metrics.** A `llm.v1.status.{provider_name}` topic where
  provider capsules publish health checks, latency percentiles, and rate limit
  headroom.
- **Cost tracking.** Extending `Usage` with a `cost` field so the orchestrator
  can enforce budget limits per request or per session.
- **Automatic failover.** The orchestrator routes a request to provider A; if it
  receives an `Error`, it re-publishes to provider B. The standardized protocol
  makes this trivial since both providers speak the same language.
- **Caching layer.** A capsule that sits in front of providers, caches responses
  keyed by message hash, and replays cached streams. The protocol's streaming
  nature means the cache capsule can replay events with the same ordering
  guarantees.
