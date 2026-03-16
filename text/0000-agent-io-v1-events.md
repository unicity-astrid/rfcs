- Feature Name: `agent_io_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#9](https://github.com/unicity-astrid/rfcs/pull/9)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `user.v1` and `agent.v1` event contracts define the topic patterns and
message schemas for user input and agent output on the Astrid event bus.
Frontends publish user prompts; the orchestrator publishes agent responses
and streaming deltas. Any frontend (CLI, Discord, Web) can conform by
publishing and subscribing to these topics.

# Motivation
[motivation]: #motivation

Frontends are interchangeable. A CLI, a Discord bot, and a web UI must all be
able to send prompts and receive responses without special-casing. The contract
must be standardized so that:

- New frontends can be built without modifying the orchestrator.
- The orchestrator does not know or care which frontend is connected.
- Streaming deltas allow real-time output rendering.
- Session attribution supports multi-user, multi-session scenarios.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Two actors participate:

1. **Frontend capsule** - accepts user input, publishes it to the bus.
2. **Orchestrator** (react loop) - processes input, publishes responses.

```text
Frontend                          Orchestrator
    |                                  |
    |-- UserInput ------------------->|
    |   (user.v1.prompt)              |-- process
    |                                  |
    |<-- AgentResponse (delta) -------|
    |<-- AgentResponse (delta) -------|
    |<-- AgentResponse (final) -------|
    |   (agent.v1.response)           |
```

For real-time token streaming, the orchestrator also publishes deltas on a
separate topic:

```text
    |<-- Custom (text delta) ---------|
    |   (agent.v1.stream.delta)       |
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| input | `user.v1.prompt` | `UserInput` | Frontend capsule | Orchestrator |
| response | `agent.v1.response` | `AgentResponse` | Orchestrator | Frontend capsule |
| stream | `agent.v1.stream.delta` | `Custom` | Orchestrator | Frontend capsule |
| notify | `agent.v1.session_changed` | `Custom` | Orchestrator | (subscribers) |

## Message schemas

### UserInput

```json
{
  "type": "user_input",
  "text": "string",
  "session_id": "string (default: \"default\")",
  "context": null
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `text` | string | yes | - | The raw text input from the user. |
| `session_id` | string | no | `"default"` | Session identifier for conversation continuity. |
| `context` | object/null | no | `null` | Optional extra context (e.g. attached files, metadata). |

### AgentResponse

```json
{
  "type": "agent_response",
  "text": "string",
  "is_final": true,
  "session_id": "string (default: \"default\")"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `text` | string | yes | - | The agent's text output. |
| `is_final` | boolean | yes | - | `true` if this is the final response in the chain. |
| `session_id` | string | no | `"default"` | Session attribution. |

### Stream delta (Custom payload)

```json
{
  "type": "custom",
  "data": {
    "text": "partial token output"
  }
}
```

Published per-token or per-chunk for real-time rendering. Frontends accumulate
deltas until they receive an `AgentResponse` with `is_final: true`.

## Behavioral requirements

A conforming **frontend capsule** must:

1. Publish user input to `user.v1.prompt` as a `UserInput` payload.
2. Subscribe to `agent.v1.response` to receive agent output.
3. Optionally subscribe to `agent.v1.stream.delta` for real-time rendering.

A conforming **orchestrator** must:

1. Subscribe to `user.v1.prompt` via an interceptor.
2. Process the input (run the react loop).
3. Publish streaming deltas to `agent.v1.stream.delta` as they arrive.
4. Publish a final `AgentResponse` with `is_final: true` to
   `agent.v1.response` when the turn is complete.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Empty text input | Orchestrator may ignore or echo an error response |
| Unknown session_id | Orchestrator creates a new session |
| No frontend subscribed | Orchestrator publishes to empty topic, response lost |

# Drawbacks
[drawbacks]: #drawbacks

- The stream delta topic uses `Custom` payloads rather than a typed variant.
  This is less structured than `LlmStreamEvent`.
- No back-pressure mechanism. A slow frontend cannot signal the orchestrator
  to slow down.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why separate input and output topics?** Clean separation of concerns.
Frontends publish to one topic and subscribe to another. No request/response
correlation needed because session_id provides context.

**Why `Custom` for stream deltas?** The delta is a thin passthrough of LLM
token output. Adding a typed variant would duplicate `LlmStreamEvent` at a
different abstraction level.

# Prior art
[prior-art]: #prior-art

- **Claude Code**: Frontend receives streaming deltas, accumulates into final
  response. Similar two-phase output.
- **Discord bots**: Message in, message out. Session tracking via channel/thread.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should `agent.v1.stream.delta` use a typed payload instead of `Custom`?
- Should there be an explicit "agent is thinking" event for frontend UX?

# Future possibilities
[future-possibilities]: #future-possibilities

- Multi-modal input (images, audio) via `context` field.
- Typing indicators / progress events for frontend UX.
- Per-frontend topic namespacing for multi-tenant deployments.
