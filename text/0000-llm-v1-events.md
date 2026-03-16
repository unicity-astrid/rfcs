- Feature Name: `llm_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#9](https://github.com/unicity-astrid/rfcs/pull/9)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `llm.v1` event contract defines the topic patterns and message schemas for
LLM provider communication on the Astrid event bus. Provider capsules receive
generation requests, stream tokens back, and publish final responses. The
contract is provider-agnostic: any LLM backend (Anthropic, OpenAI, local) can
conform by subscribing to its provider-specific topic suffix.

# Motivation
[motivation]: #motivation

The orchestrator must be able to call any LLM provider without knowing its
implementation. The contract between them must be standardized so that:

- New providers can be added by deploying a capsule, not modifying the kernel.
- The streaming event vocabulary is consistent across all providers.
- The orchestrator can switch providers at runtime without code changes.
- Token usage and stop reasons have a uniform shape for budgeting and audit.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Two actors participate in LLM communication:

1. **Orchestrator** (react loop) - publishes a generation request.
2. **Provider capsule** - streams tokens, then publishes a final response.

The topic suffix is the provider name (e.g. `anthropic`, `openai`, `local`).

```text
Orchestrator                         Provider capsule
    |                                       |
    |-- LlmRequest ----------------------->|
    |   (llm.v1.request.generate.          |
    |    {provider})                        |-- call API
    |                                       |
    |<-- LlmStreamEvent (TextDelta) -------|
    |<-- LlmStreamEvent (ToolCallStart) ---|
    |<-- LlmStreamEvent (ToolCallDelta) ---|
    |<-- LlmStreamEvent (ToolCallEnd) -----|
    |<-- LlmStreamEvent (Usage) -----------|
    |<-- LlmStreamEvent (Done) ------------|
    |   (llm.v1.stream.{provider})         |
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| request | `llm.v1.request.generate.{provider}` | `LlmRequest` | Orchestrator | Provider capsule |
| stream | `llm.v1.stream.{provider}` | `LlmStreamEvent` | Provider capsule | Orchestrator |

The orchestrator subscribes to `llm.v1.stream.*` via an interceptor to receive
streams from any active provider.

## Message schemas

### LlmRequest

```json
{
  "type": "llm_request",
  "request_id": "uuid",
  "model": "string (e.g. claude-sonnet-4-20250514)",
  "messages": [{ "role": "user", "content": "Hello" }],
  "tools": [{ "name": "read_file", "description": "...", "input_schema": {} }],
  "system": "string (system prompt)"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | UUID | yes | Correlates the response stream back to this request. |
| `model` | string | yes | The model identifier (e.g. `claude-sonnet-4-20250514`). |
| `messages` | `Message[]` | yes | Conversation history. |
| `tools` | `LlmToolDefinition[]` | yes | Available tools. May be empty. |
| `system` | string | yes | System prompt. May be empty string. |

### Message

```json
{
  "role": "user | assistant | system | tool",
  "content": "string | ToolCall[] | ToolCallResult | ContentPart[]"
}
```

Content is `#[serde(untagged)]` and discriminated by shape:

| Shape | Meaning |
|-------|---------|
| `"string"` | Plain text content. |
| `[{ "id", "name", "arguments" }]` | `ToolCall[]` - assistant requesting tool use. |
| `{ "call_id", "content", "is_error" }` | `ToolCallResult` - tool result. |
| `[{ "type": "text", "text" } \| { "type": "image", "data", "media_type" }]` | `ContentPart[]` - multipart. |

### LlmToolDefinition

```json
{
  "name": "string",
  "description": "string | null",
  "input_schema": { "JSON Schema object" }
}
```

### LlmStreamEvent

```json
{
  "type": "llm_stream_event",
  "request_id": "uuid",
  "event": { "StreamEvent variant" }
}
```

### StreamEvent

Serde-tagged enum. Variants:

| Variant | Fields | Description |
|---------|--------|-------------|
| `TextDelta` | `string` | Partial text output. |
| `ToolCallStart` | `{ id, name }` | Tool call initiated. |
| `ToolCallDelta` | `{ id, args_delta }` | Partial tool call arguments JSON. |
| `ToolCallEnd` | `{ id }` | Tool call arguments complete. |
| `ReasoningDelta` | `string` | Chain-of-thought delta (o-series, DeepSeek, etc.). |
| `Usage` | `{ input_tokens, output_tokens }` | Token counts (both `usize`). |
| `Done` | (none) | Stream complete. Final event. |
| `Error` | `string` | Error message. Terminal. |

### LlmResponse (non-streaming)

```json
{
  "type": "llm_response",
  "request_id": "uuid",
  "response": {
    "message": { "role": "assistant", "content": "..." },
    "has_tool_calls": false,
    "stop_reason": "EndTurn",
    "usage": { "input_tokens": 100, "output_tokens": 50 }
  }
}
```

### StopReason

| Value | Meaning |
|-------|---------|
| `EndTurn` | Natural end of response. |
| `MaxTokens` | Hit max token limit. |
| `ToolUse` | Model requested tool use. |
| `StopSequence` | Stop sequence encountered. |

## Behavioral requirements

A conforming **provider capsule** must:

1. Subscribe to `llm.v1.request.generate.{provider_name}` via an interceptor.
2. Call the upstream LLM API using the provided `model`, `messages`, `tools`,
   and `system`.
3. Stream events to `llm.v1.stream.{provider_name}` as they arrive. Each event
   must include the `request_id` from the originating request.
4. Always emit `Done` or `Error` as the final stream event.
5. Include `Usage` before `Done` when the upstream API provides token counts.

A conforming **orchestrator** must:

1. Subscribe to `llm.v1.stream.*` to receive events from any active provider.
2. Correlate events by `request_id`.
3. Accumulate `ToolCallStart` / `ToolCallDelta` / `ToolCallEnd` sequences
   into complete `ToolCall` objects for tool execution.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Upstream API error | Provider publishes `StreamEvent::Error(message)`, terminates stream |
| Invalid model name | Provider publishes `StreamEvent::Error`, does not call API |
| Network timeout | Provider publishes `StreamEvent::Error` |
| Provider capsule not subscribed | Request published to empty topic, no response (timeout at orchestrator) |

# Drawbacks
[drawbacks]: #drawbacks

- Provider-specific topic suffixes mean the orchestrator must know which
  provider to target. The registry capsule resolves this indirection.
- Streaming adds complexity. A request/response-only mode is not currently
  defined as a separate topic.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why provider-specific topic suffixes?** Allows multiple providers to coexist
without routing logic. The orchestrator publishes to the correct provider
directly.

**Why streaming as the primary mode?** Agents benefit from progressive output.
Non-streaming responses can be emitted as a single `TextDelta` followed by
`Done`.

**Why `MessageContent` as untagged?** Matches how LLM APIs represent content.
The shape discrimination is unambiguous because each variant has a distinct
JSON structure.

# Prior art
[prior-art]: #prior-art

- **Anthropic Messages API**: Streaming via SSE with `content_block_delta`,
  `message_delta` events. Similar event vocabulary.
- **OpenAI Chat Completions**: Streaming via SSE with `choices[0].delta`.
  `StreamEvent` normalizes across both.
- **MCP**: Does not define LLM communication; it is a tool protocol.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should there be a non-streaming request/response topic pair for simple
  use cases?
- Should provider capability advertisement (vision support, tool support)
  be part of this contract or the registry contract?

# Future possibilities
[future-possibilities]: #future-possibilities

- Multi-modal content (images, audio) in `Message` via `ContentPart`.
- Provider fallback chains (try provider A, fall back to B on error).
- Cost estimation events for budget enforcement.
