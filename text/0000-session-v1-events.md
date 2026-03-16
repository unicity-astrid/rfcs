- Feature Name: `session_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#10](https://github.com/unicity-astrid/rfcs/pull/10)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `session.v1` event contract defines the topic patterns and message schemas
for conversation history storage on the Astrid event bus. A session store
receives append, read, and clear operations. Scoped reply topics prevent
cross-instance response theft.

# Motivation
[motivation]: #motivation

Every agent turn needs conversation history. The session contract must
guarantee that:

- The store is dumb. No prompt assembly, no compaction, no transformation.
  Clean in, clean out.
- Operations use scoped reply topics so concurrent orchestrator instances
  cannot intercept each other's responses.
- Session linking via parent IDs preserves history chains across clears.
- Any conforming implementation (in-memory, database-backed, distributed)
  can serve as the session store.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Two actors participate:

1. **Caller** (orchestrator or frontend) - appends messages, reads history,
   clears sessions.
2. **Session store capsule** - persists and retrieves messages.

```text
Caller                            Session store
    |                                  |
    |-- append ---------------------->|
    |   (session.v1.append)           |-- persist
    |                                  |
    |-- get_messages ---------------->|
    |   (session.v1.request.          |-- query
    |    get_messages)                 |
    |<-- messages --------------------|
    |   (session.v1.response.         |
    |    get_messages.{correlation_id})|
```

The scoped reply topic `session.v1.response.get_messages.{correlation_id}`
ensures that only the requesting caller receives the response.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| append | `session.v1.append` | `Custom` | Caller | Session store |
| request | `session.v1.request.get_messages` | `Custom` | Caller | Session store |
| response | `session.v1.response.get_messages.{correlation_id}` | `Custom` | Session store | Caller |
| request | `session.v1.request.clear` | `Custom` | Caller | Session store |
| response | `session.v1.response.clear.{correlation_id}` | `Custom` | Session store | Caller |
| broadcast | `session.v1.clear` | `Custom` | Frontend | Orchestrator |

## Message schemas

All session messages use `Custom` payloads with JSON `data` objects.

### Append

```json
{
  "type": "custom",
  "data": {
    "session_id": "string",
    "message": {
      "role": "user | assistant | system | tool",
      "content": "string | ToolCall[] | ToolCallResult | ContentPart[]"
    }
  }
}
```

Fire-and-forget. No response topic. The store persists the message.

### Get Messages (request)

```json
{
  "type": "custom",
  "data": {
    "session_id": "string",
    "correlation_id": "string (unique per request)"
  }
}
```

### Get Messages (response)

Published to `session.v1.response.get_messages.{correlation_id}`:

```json
{
  "type": "custom",
  "data": {
    "messages": [
      { "role": "user", "content": "Hello" },
      { "role": "assistant", "content": "Hi there" }
    ]
  }
}
```

### Clear (request)

```json
{
  "type": "custom",
  "data": {
    "session_id": "string",
    "correlation_id": "string"
  }
}
```

### Clear (response)

Published to `session.v1.response.clear.{correlation_id}`:

```json
{
  "type": "custom",
  "data": {
    "success": true,
    "new_session_id": "string (if session was chained)"
  }
}
```

### Clear (broadcast)

`session.v1.clear` is a broadcast from frontends (e.g. user types `/clear`).
The orchestrator subscribes and initiates the scoped clear flow above.

## Behavioral requirements

A conforming **session store** must:

1. Subscribe to `session.v1.append`, `session.v1.request.get_messages`, and
   `session.v1.request.clear` via interceptors.
2. Persist messages in append order per `session_id`.
3. Return messages in chronological order on `get_messages`.
4. Publish responses to the scoped reply topic using the `correlation_id`
   from the request.
5. On clear, optionally create a new session linked to the old one via
   `parent_session_id`. Never silently truncate history.

A conforming **caller** must:

1. Generate a unique `correlation_id` per request.
2. Subscribe to the scoped reply topic before publishing the request.
3. Handle timeout if the session store does not respond.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Unknown session_id on get_messages | Return empty messages array |
| Unknown session_id on clear | Return success (idempotent) |
| Session store not subscribed | Request published to empty topic, caller times out |
| Malformed append payload | Store ignores silently |

# Drawbacks
[drawbacks]: #drawbacks

- Uses `Custom` payloads throughout instead of typed `IpcPayload` variants.
  This reduces compile-time type safety.
- The scoped reply pattern requires callers to dynamically subscribe before
  each request. More complex than a simple request/response pair.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why scoped reply topics?** Prevents response theft. If two orchestrator
instances both request messages for the same session, each gets its own
response on its own correlation topic. A shared response topic would
create a race condition.

**Why `Custom` instead of typed variants?** The session store is a generic
persistence layer. Typed variants would couple the IPC schema to the
session store's internal representation. `Custom` allows the schema to
evolve without coordinating `IpcPayload` changes.

**Why fire-and-forget for append?** Appending a message does not need
acknowledgment in the hot path. If the store is down, the orchestrator
will discover it on the next `get_messages` call.

# Prior art
[prior-art]: #prior-art

- **Redis Streams**: Append-only log with consumer groups. Similar append
  semantics.
- **MCP**: Does not define session storage; history management is left to
  the host.
- **OpenAI Assistants API**: Server-side thread management with append/read.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should session schema versioning be part of this contract or left to
  the implementation?
- Should there be a `session.v1.request.list` operation for enumerating
  sessions?

# Future possibilities
[future-possibilities]: #future-possibilities

- Session pagination for large histories.
- Session metadata (title, tags, timestamps) as a separate query.
- Cross-session search.
