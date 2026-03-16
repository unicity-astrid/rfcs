- Feature Name: `context_engine_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#10](https://github.com/unicity-astrid/rfcs/pull/10)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `context_engine.v1` event contract defines the topic patterns and message
schemas for context compaction and token estimation on the Astrid event bus.
A context engine receives message arrays and returns compacted versions that
fit within token budgets. Before/after hooks allow plugins to protect messages
or inject replacements.

# Motivation
[motivation]: #motivation

LLM context windows are finite. When conversation history exceeds the budget,
messages must be compacted without losing critical context. The contract must
guarantee that:

- Compaction is a separate concern from session storage.
- Plugins can protect specific messages from being removed.
- Plugins can inject replacement summaries.
- Token estimation is available as a standalone operation.
- Any conforming implementation (summarization, truncation, hybrid) can serve
  as the context engine.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Three actors participate:

1. **Caller** (orchestrator) - requests compaction or token estimation.
2. **Context engine capsule** - performs the compaction.
3. **Plugin capsules** - optionally respond to before/after hooks.

```text
Caller                Context engine              Plugin
    |                      |                         |
    |-- CompactRequest -->|                         |
    |                      |-- before_compaction -->|
    |                      |<-- hook response ------|
    |                      |-- compact              |
    |                      |-- after_compaction --->|
    |<-- CompactResponse --|                         |
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| request | `context_engine.v1.compact` | `Custom` | Caller | Context engine |
| response | `context_engine.v1.response.compact` | `Custom` | Context engine | Caller |
| request | `context_engine.v1.estimate_tokens` | `Custom` | Caller | Context engine |
| response | `context_engine.v1.response.estimate_tokens` | `Custom` | Context engine | Caller |
| hook | `context_engine.v1.hook.before_compaction` | `Custom` | Context engine | Plugin capsules |
| hook | `context_engine.v1.hook.after_compaction` | `Custom` | Context engine | Plugin capsules |
| hook reply | `context_engine.v1.hook_response.{request_id}` | `Custom` | Plugin capsules | Context engine |

## Message schemas

### CompactRequest

```json
{
  "type": "custom",
  "data": {
    "session_id": "string",
    "messages": [{ "role": "...", "content": "..." }],
    "max_tokens": 4096,
    "strategy": "string | null"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | The session being compacted. |
| `messages` | `Message[]` | yes | The full message array to compact. |
| `max_tokens` | integer | yes | Target token budget. |
| `strategy` | string/null | no | Compaction strategy hint (implementation-defined). |

### CompactResponse

```json
{
  "type": "custom",
  "data": {
    "messages": [{ "role": "...", "content": "..." }],
    "original_count": 50,
    "final_count": 20,
    "tokens_saved": 3000
  }
}
```

### EstimateTokensRequest

```json
{
  "type": "custom",
  "data": {
    "messages": [{ "role": "...", "content": "..." }]
  }
}
```

### EstimateTokensResponse

```json
{
  "type": "custom",
  "data": {
    "estimated_tokens": 1500
  }
}
```

### Before compaction hook

Published before compaction begins. Plugins respond with protected indices
or replacement messages.

```json
{
  "type": "custom",
  "data": {
    "request_id": "string",
    "response_topic": "context_engine.v1.hook_response.{request_id}",
    "messages": [{ "role": "...", "content": "..." }],
    "strategy": "string | null"
  }
}
```

### Before compaction hook response

```json
{
  "type": "custom",
  "data": {
    "protected_indices": [0, 1, 5],
    "replacement_messages": null
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `protected_indices` | `int[]` / null | Message indices that must not be removed. |
| `replacement_messages` | `Message[]` / null | If provided, replaces the entire message array before compaction. |

### After compaction hook (fire-and-forget)

```json
{
  "type": "custom",
  "data": {
    "original_count": 50,
    "final_count": 20,
    "tokens_saved": 3000
  }
}
```

## Behavioral requirements

A conforming **context engine** must:

1. Subscribe to `context_engine.v1.compact` and
   `context_engine.v1.estimate_tokens`.
2. Before compacting, publish `context_engine.v1.hook.before_compaction` and
   wait for responses on the scoped hook reply topic.
3. Respect `protected_indices` from hook responses.
4. Apply `replacement_messages` if provided by a hook response.
5. Publish the compacted result to `context_engine.v1.response.compact`.
6. After compacting, publish `context_engine.v1.hook.after_compaction`
   (fire-and-forget).
7. For token estimation, return an estimate without modifying messages.

A conforming **plugin** responding to hooks must:

1. Subscribe to `context_engine.v1.hook.before_compaction` via an interceptor.
2. Publish responses to the `response_topic` specified in the hook payload.

## Error handling

| Condition | Behavior |
|-----------|----------|
| No hook responders | Engine proceeds without protected indices |
| Hook responder timeout | Engine proceeds after timeout period |
| Empty messages array | Engine returns empty array, counts of 0 |
| Context engine not subscribed | Request published to empty topic, caller times out |

# Drawbacks
[drawbacks]: #drawbacks

- The hook pattern adds latency to every compaction. Two IPC round-trips
  (publish hook, wait for response) before the actual work starts.
- Plugins can deadlock compaction by never responding to hooks.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why hooks instead of direct plugin integration?** Decoupling. The context
engine does not need to know which plugins exist. Plugins opt in by
subscribing to the hook topics.

**Why separate compact and estimate_tokens?** Different use cases.
Estimation is cheap and used for proactive decisions ("should I compact
now?"). Compaction is expensive and destructive.

# Prior art
[prior-art]: #prior-art

- **LangChain ConversationSummaryMemory**: Summarizes old messages to fit
  context. Similar motivation, different mechanism.
- **Claude's context window management**: Server-side truncation with
  system prompt preservation.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should the hook timeout be configurable per-request or system-wide?
- Should compaction strategies be enumerated in this contract or left
  entirely to implementations?

# Future possibilities
[future-possibilities]: #future-possibilities

- Named compaction strategies registered at the engine level.
- Incremental compaction (compact only the delta since last compaction).
- Compaction audit trail (which messages were removed and why).
