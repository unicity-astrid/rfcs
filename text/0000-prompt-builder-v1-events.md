- Feature Name: `prompt_builder_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#12](https://github.com/unicity-astrid/rfcs/pull/12)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `prompt_builder.v1` event contract defines the topic patterns and message
schemas for prompt assembly on the Astrid event bus. A prompt builder receives
raw messages, system prompt, and tool definitions, runs before/after hooks to
allow plugins to inject context, and returns the assembled prompt ready for
LLM submission.

# Motivation
[motivation]: #motivation

The prompt sent to the LLM is not just conversation history. It includes the
system prompt, tool definitions, memory context, and plugin-injected context.
Assembly must be standardized so that:

- Plugins (memory, RAG, etc.) can inject context without modifying the
  orchestrator.
- The orchestrator does not need to know which plugins contribute context.
- The hook pattern allows composable prompt enrichment.
- Any conforming implementation can serve as the prompt builder.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Three actors participate:

1. **Caller** (orchestrator) - requests prompt assembly.
2. **Prompt builder capsule** - assembles the prompt, runs hooks.
3. **Plugin capsules** (memory, RAG, etc.) - respond to hooks with
   additional context.

```text
Caller              Prompt builder              Plugin (e.g. memory)
    |                    |                            |
    |-- assemble ------>|                            |
    |                    |-- before_build ---------->|
    |                    |<-- hook response ---------|
    |                    |                            |
    |                    |-- assemble prompt          |
    |                    |                            |
    |                    |-- after_build ----------->|
    |<-- assembled ------|                            |
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| request | `prompt_builder.v1.assemble` | `Custom` | Caller | Prompt builder |
| response | `prompt_builder.v1.response.assemble` | `Custom` | Prompt builder | Caller |
| hook | `prompt_builder.v1.hook.before_build` | `Custom` | Prompt builder | Plugin capsules |
| hook | `prompt_builder.v1.hook.after_build` | `Custom` | Prompt builder | Plugin capsules |
| hook reply | `prompt_builder.v1.hook_response.{request_id}` | `Custom` | Plugin capsules | Prompt builder |

## Message schemas

### Assemble request

```json
{
  "type": "custom",
  "data": {
    "request_id": "string",
    "messages": [{ "role": "...", "content": "..." }],
    "system": "string",
    "tools": [{ "name": "...", "description": "...", "input_schema": {} }]
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | string | yes | Correlates the response. |
| `messages` | `Message[]` | yes | Conversation history. |
| `system` | string | yes | Base system prompt. |
| `tools` | `LlmToolDefinition[]` | yes | Available tools. |

### Assemble response

```json
{
  "type": "custom",
  "data": {
    "request_id": "string",
    "messages": [{ "role": "...", "content": "..." }],
    "system": "string (enriched)",
    "tools": [{ "name": "...", "description": "...", "input_schema": {} }],
    "error": null
  }
}
```

On failure, `error` contains a string description and other fields may be
absent.

### Before build hook

Published before assembly begins. Plugins respond with additional system
context.

```json
{
  "type": "custom",
  "data": {
    "request_id": "string",
    "response_topic": "prompt_builder.v1.hook_response.{request_id}",
    "context": {
      "messages": [{ "role": "...", "content": "..." }],
      "system": "string",
      "tools": [{ "name": "..." }]
    }
  }
}
```

### Before build hook response

```json
{
  "type": "custom",
  "data": {
    "appendSystemContext": "string (additional system prompt text)"
  }
}
```

Plugins may include additional fields as the hook response schema evolves.
The prompt builder appends `appendSystemContext` to the system prompt.

### After build hook (fire-and-forget)

Published after assembly completes. Informational only.

```json
{
  "type": "custom",
  "data": {
    "request_id": "string",
    "message_count": 10,
    "tool_count": 5
  }
}
```

## Behavioral requirements

A conforming **prompt builder** must:

1. Subscribe to `prompt_builder.v1.assemble`.
2. Publish `prompt_builder.v1.hook.before_build` with the request context.
3. Wait for responses on `prompt_builder.v1.hook_response.{request_id}`.
4. Append `appendSystemContext` from hook responses to the system prompt.
5. Assemble the final prompt (messages + enriched system + tools).
6. Publish the assembled result to `prompt_builder.v1.response.assemble`.
7. Publish `prompt_builder.v1.hook.after_build` (fire-and-forget).

A conforming **plugin** responding to hooks must:

1. Subscribe to `prompt_builder.v1.hook.before_build` via an interceptor.
2. Publish responses to the `response_topic` specified in the hook payload.
3. Respond within a reasonable timeout or not at all (the builder proceeds).

## Error handling

| Condition | Behavior |
|-----------|----------|
| No hook responders | Builder proceeds with unmodified system prompt |
| Hook responder timeout | Builder proceeds after timeout period |
| Prompt builder not subscribed | Request to empty topic, caller times out |
| Invalid assemble request | Builder publishes response with `error` field set |

# Drawbacks
[drawbacks]: #drawbacks

- The hook round-trip adds latency to every prompt assembly.
- The `appendSystemContext` interface is limited. Plugins cannot modify
  messages or tools, only append to the system prompt.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why not let plugins modify messages directly?** Risk. A plugin modifying
conversation history could corrupt context or inject prompt attacks. Limiting
plugins to `appendSystemContext` is a safe default.

**Why a separate prompt builder instead of inline assembly?** Separation of
concerns. The orchestrator focuses on the react loop. The prompt builder
focuses on assembling the right input for the LLM.

# Prior art
[prior-art]: #prior-art

- **LangChain prompt templates**: Composable prompt construction. Similar
  motivation, different mechanism (function composition vs IPC hooks).
- **MCP sampling**: The server can request LLM completion via the host.
  Different direction (server-to-host vs host-to-server).

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should plugins be able to modify messages, not just append system context?
- Should hook responses have a priority field for ordering multiple plugins?

# Future possibilities
[future-possibilities]: #future-possibilities

- Plugin-injected tool definitions (a memory plugin adding retrieval tools).
- Prompt caching hints for provider capsules.
- Token budget awareness during assembly.
