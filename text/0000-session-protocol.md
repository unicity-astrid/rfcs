- Feature Name: `session_protocol`
- Start Date: 2026-03-16
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

This RFC defines the IPC contract for the session capsule: the append-only
conversation store that every frontend and agent capsule talks to. It specifies
three operations (append, get_messages, clear), the topic naming scheme, the
session-chaining model, the schema versioning strategy, and the concurrency
safety guarantees. The session capsule is the filesystem for conversations.

# Motivation
[motivation]: #motivation

Every frontend (CLI, Discord, Web) and every agent capsule needs to read and
write conversation history. Today that logic is scattered. Each frontend has its
own idea of what a "session" is, how messages are stored, and how history is
fetched. That duplication creates three problems:

1. **No single source of truth.** Two frontends talking to the same user can
   disagree on what the conversation looks like.
2. **No isolation.** Without a dedicated capsule behind capability-scoped IPC
   topics, any capsule that can publish to the bus can overwrite another
   capsule's history.
3. **No forward compatibility.** When the schema changes (and it will), every
   consumer needs to handle migration independently.

A standardized session protocol solves all three. One capsule owns the data. All
consumers speak the same IPC contract. Schema versioning is handled once, in the
capsule, not N times in N frontends.

The session capsule is deliberately simple. It is a dumb, trustworthy,
append-only store. It holds clean messages: what the user said, what the
assistant replied, what tools returned. It never transforms anything. Clean in,
clean out. Intelligence lives in the agent capsules that read from it, not in
the store itself.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## The session capsule

The session capsule is a WASM capsule that stores conversation messages in the
kernel's KV store. It exposes three operations over IPC:

- **Append** - add messages to the current session
- **Get messages** - retrieve the message history for the current session
- **Clear** - create a new empty session, linking back to the old one

Think of it as a filesystem for conversations. You write messages. You read them
back. You can start a new file (session) without destroying the old one.

## Talking to the session capsule

All communication happens via IPC topics on the kernel's event bus. A capsule
that wants to store a user message publishes to `session.append`. A capsule
that wants to read history publishes a request to
`session.v1.request.get_messages` and subscribes to a per-request reply topic.

### Appending messages

Fire-and-forget. Publish and move on.

```
Topic:   session.append
Payload: { "messages": [ { "role": "user", "content": "Hello" } ] }
```

The session capsule appends the messages to the current session in the KV store.
No response is sent. If the payload is malformed, the capsule logs an error and
drops the message. The publisher is not notified because append is
fire-and-forget by design: the common case is a frontend pushing a message
before an agent turn, where blocking on acknowledgment adds latency for no
practical benefit.

### Getting messages

Request/response with correlation IDs.

```
Request topic:   session.v1.request.get_messages
Response topic:  session.v1.response.get_messages.{correlation_id}

Request payload:
{
  "correlation_id": "abc123",
  "append_before_read": [          // optional
    { "role": "assistant", "content": "Sure, here is the answer." }
  ]
}

Response payload:
{
  "session_id": "ses_01HXYZ...",
  "messages": [
    { "role": "user", "content": "Hello" },
    { "role": "assistant", "content": "Sure, here is the answer." }
  ],
  "parent_session_id": null
}
```

The `append_before_read` field is the key ergonomic feature. It atomically
appends the given messages and then returns the full history. This eliminates
the race condition that would exist if a consumer had to publish a separate
append and then a separate get_messages: under concurrent load, another writer
could interleave between the two.

### Clearing a session

Creates a new, empty session. The old session stays intact in the KV store.

```
Topic:   session.v1.request.clear
Payload: {}
```

After this call, the current session ID changes. The new session's
`parent_session_id` points to the old session. Consumers that call get_messages
will see an empty history with the new session ID.

## Session chaining

Sessions form a linked list. Each session has an optional `parent_session_id`.
When a session is cleared, the new session points back to the old one. When a
session is compacted in the future (via a not-yet-specified compaction protocol),
the compacted session points back to the pre-compaction one.

To reconstruct the full history across boundaries, walk the chain:

```
ses_003 -> ses_002 -> ses_001 -> null
```

Each node in the chain is a complete, self-contained session. The chain provides
provenance, not a mandatory traversal. Most consumers only care about the
current session. Consumers that need deep history (audit, analytics) walk the
chain.

History is never silently truncated. If messages need to be dropped (context
window limits, for example), that is the agent's job, not the session capsule's.
The store preserves everything it receives.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Message schema

A message is a JSON object with these fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `role` | string | yes | One of `"user"`, `"assistant"`, `"tool"`, `"system"` |
| `content` | string | yes | The message text |
| `tool_call_id` | string | no | For `"tool"` role messages, the ID of the tool call this responds to |
| `tool_calls` | array | no | For `"assistant"` role messages, tool calls the assistant wants to make |
| `name` | string | no | An optional display name for the message author |

The session capsule validates only that `role` and `content` are present and
that `role` is one of the four allowed values. It does not interpret the content.
It does not validate tool call schemas. It is a store, not a processor.

## Session record schema

A session record is persisted in the kernel KV store under the key
`session:{session_id}`. The record is a JSON object:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | Unique identifier (ULID format) |
| `schema_version` | integer | yes | Schema version number |
| `messages` | array | yes | Ordered list of message objects |
| `parent_session_id` | string | no | Points to the previous session in the chain |
| `created_at` | string | yes | ISO 8601 timestamp of session creation |
| `updated_at` | string | yes | ISO 8601 timestamp of last modification |

## Operations

### Append

| Property | Value |
|----------|-------|
| Topic | `session.append` |
| Direction | Fire-and-forget (no response) |
| Capability | `ipc:publish:session.append` |

**Request payload:**

```json
{
  "messages": [
    { "role": "user", "content": "Hello" }
  ]
}
```

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `messages` | array of Message | yes | Non-empty. Each element must be a valid Message. |

**Behavior:**

1. Deserialize the payload. If malformed, log a warning and drop.
2. Validate each message: `role` must be one of the allowed values, `content`
   must be a non-empty string.
3. Append all valid messages to the current session's `messages` array.
4. Update `updated_at` on the session record.
5. Persist to KV.

**Error handling:** No response is sent. Invalid payloads are logged and dropped.
This is intentional: append is fire-and-forget, and the publisher has already
moved on.

### Get Messages

| Property | Value |
|----------|-------|
| Request topic | `session.v1.request.get_messages` |
| Response topic | `session.v1.response.get_messages.{correlation_id}` |
| Direction | Request/response |
| Capability (publish) | `ipc:publish:session.v1.request.get_messages` |
| Capability (subscribe) | `ipc:subscribe:session.v1.response.get_messages.*` |

**Request payload:**

```json
{
  "correlation_id": "abc123",
  "append_before_read": []
}
```

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `correlation_id` | string | yes | Non-empty. Must not contain dots. |
| `append_before_read` | array of Message | no | If present, messages to append atomically before reading. |

**Correlation ID constraints:** The `correlation_id` must be non-empty and must
not contain dots (`.`). Dots in a correlation ID would create extra topic
segments (e.g., `session.v1.response.get_messages.abc.123` instead of
`session.v1.response.get_messages.abc123`), which breaks ACL pattern matching
on the wildcard `*` segment. The capsule rejects requests with invalid
correlation IDs by publishing an error response.

**Response payload (success):**

```json
{
  "session_id": "ses_01HXYZ...",
  "messages": [],
  "parent_session_id": null
}
```

| Field | Type | Description |
|-------|------|-------------|
| `session_id` | string | The current session's ID |
| `messages` | array of Message | The full ordered message history for this session |
| `parent_session_id` | string or null | The previous session in the chain, if any |

**Response payload (error):**

```json
{
  "error": "invalid_correlation_id",
  "detail": "correlation_id must not contain dots"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `error` | string | Error code |
| `detail` | string | Human-readable description |

**Behavior:**

1. Validate the `correlation_id`. If empty or contains dots, publish an error
   response to the reply topic (using a sanitized version of the ID) and return.
2. If `append_before_read` is present and non-empty, append those messages to
   the current session (same logic as the Append operation).
3. Read the current session record from KV.
4. Publish the response to `session.v1.response.get_messages.{correlation_id}`.

The append-then-read is atomic within the capsule's single-threaded execution.
No other operation can interleave between the append and the read because WASM
capsules process one IPC message at a time.

### Clear

| Property | Value |
|----------|-------|
| Topic | `session.v1.request.clear` |
| Direction | Fire-and-forget |
| Capability | `ipc:publish:session.v1.request.clear` |

**Request payload:**

```json
{}
```

No fields are required. The payload must be a valid JSON object.

**Behavior:**

1. Record the current session's ID as `old_session_id`.
2. Generate a new session ID (ULID).
3. Create a new session record with `parent_session_id` set to `old_session_id`,
   an empty `messages` array, and `schema_version` set to the current version.
4. Set the new session as the current active session.
5. Persist both the new session record and the updated "current session" pointer
   to KV.

The old session record is not modified or deleted. It remains in the KV store
indefinitely, accessible by walking the session chain.

## Schema versioning

Every session record has a `schema_version` field. The capsule handles version
mismatches as follows:

| Stored version | Action |
|----------------|--------|
| v0 (missing field) | Stamp the record with `schema_version: 1`, re-save, and use as v1. |
| v1 (current) | Use as-is. |
| Unknown future version (> 1) | Start a fresh session. Log a warning. Link the new session to the old one via `parent_session_id`. |

This strategy is fail-secure. If the capsule encounters data it cannot
understand (a future version written by a newer capsule), it does not attempt
to interpret it. It starts clean and preserves the old data untouched in the
chain.

Forward-compatible deserialization works because the schema is additive. v1 is
a superset of v0. Future versions that only add optional fields can be read by
v1 code (unknown fields are ignored by the JSON deserializer). The version
bump to "unknown future" triggers only when the schema changes in a way that
v1 code cannot safely interpret.

## Concurrency safety

### Reply topic isolation

Every get_messages request gets its own reply topic:

```
session.v1.response.get_messages.{correlation_id}
```

The requesting capsule subscribes to this specific topic before publishing the
request. Because the correlation ID is unique per request, no two consumers
will receive each other's responses. This prevents cross-instance response
theft under concurrent load.

### Session isolation

The session capsule does not enforce session isolation between different users
or agents. That is the kernel's job. The kernel's topic ACL layer restricts
which capsules can publish to and subscribe from which topics. A capsule that
lacks the `ipc:publish:session.append` capability cannot write to the session
store. A capsule that lacks `ipc:subscribe:session.v1.response.get_messages.*`
cannot read responses.

Per-user or per-agent session isolation (ensuring agent A cannot read agent B's
session) is enforced by scoping topic ACLs at the kernel level, not within the
session capsule itself. The capsule trusts that if a message arrived on its
topic, the kernel already authorized it.

### Ordering guarantees

Within a single session, messages are ordered by insertion. The session capsule
processes IPC messages sequentially (WASM single-threaded execution). Two
appends from the same publisher arrive in the order they were published. Two
appends from different publishers arrive in the order the kernel's event bus
delivers them.

The session capsule does not provide causal ordering across publishers. If two
capsules race to append, the final order depends on the kernel's delivery order.
This is acceptable because in practice, conversation turns are sequential: the
user speaks, then the agent speaks, then the user speaks.

## Topic naming conventions

All topics follow the pattern established in this RFC:

| Purpose | Topic pattern |
|---------|---------------|
| Append (fire-and-forget) | `session.append` |
| Get messages (request) | `session.v1.request.get_messages` |
| Get messages (response) | `session.v1.response.get_messages.{correlation_id}` |
| Clear (fire-and-forget) | `session.v1.request.clear` |

The `v1` segment enables topic-level versioning. A future v2 of the get_messages
protocol can coexist with v1 by using `session.v2.request.get_messages`. The
append topic omits the version segment because it is fire-and-forget with no
response routing.

## Capability requirements

| Operation | Required capability |
|-----------|-------------------|
| Append | `ipc:publish:session.append` |
| Get messages (send request) | `ipc:publish:session.v1.request.get_messages` |
| Get messages (receive response) | `ipc:subscribe:session.v1.response.get_messages.*` |
| Clear | `ipc:publish:session.v1.request.clear` |
| Session capsule (receive all) | `ipc:subscribe:session.*` |

# Drawbacks
[drawbacks]: #drawbacks

- **Fire-and-forget append has no delivery guarantee.** If the session capsule
  is down or slow, appended messages are lost. The publisher has no way to know.
  This is a deliberate trade-off for latency, but it means the append path is
  not suitable for messages where loss is catastrophic (e.g., billing-relevant
  tool call results). A future RFC could add an acknowledged append variant.

- **Single-capsule bottleneck.** All session reads and writes funnel through one
  WASM instance. Under high concurrency (many agents, many sessions), this
  becomes a throughput bottleneck. Horizontal scaling (sharding by session ID)
  is a future concern, not addressed here.

- **No pagination.** Get messages returns the full history for the current
  session. For very long sessions, this could be a large payload. Pagination
  adds complexity to the protocol and is deferred to a future RFC.

- **Append topic lacks version segment.** The `session.append` topic does not
  include a `v1` segment, unlike the other operations. This is intentional
  (fire-and-forget needs no response routing, so there is no compatibility
  concern), but it creates a minor inconsistency in the naming scheme.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why a dedicated capsule instead of kernel-level storage?**

The kernel should be minimal. Session management is application logic. Putting
it in a capsule keeps the kernel's contract surface small and lets the session
logic evolve independently. It also means the session store runs in the same
sandbox as every other capsule, with the same capability restrictions, the same
audit trail, and the same upgrade path.

**Why fire-and-forget for append?**

The append path is the hottest path in the system. Every user message, every
assistant response, every tool result goes through it. Adding a
request/response round-trip doubles the latency of every conversation turn for
an acknowledgment that the caller almost never needs to act on. The trade-off
is worth it. If delivery guarantees matter for a specific use case, a future
RFC can define an acknowledged append without breaking this protocol.

**Why atomic append-before-read instead of separate calls?**

Without `append_before_read`, a consumer that needs to append a message and
then fetch the full history must make two IPC calls. Under concurrent load,
another writer can insert between the two, causing the consumer to see a
history that does not include its own append. The atomic variant eliminates
this race. It is the only way to get a consistent read-after-write without
external locking.

**Why session chaining instead of in-place mutation?**

In-place mutation (deleting old messages when clearing) destroys provenance.
Session chaining preserves every message ever written. The old session stays in
KV, untouched. The new session links back. This makes audit trivial: you can
always reconstruct exactly what happened, in what order, across how many
sessions. It also makes undo possible in the future.

**Why fail-secure on unknown schema versions?**

The alternative is "try to parse it anyway." That risks misinterpreting fields
that changed meaning between versions, leading to corrupted or misleading
conversation history. Starting fresh and linking back is safe: the old data is
preserved for a newer capsule that can read it, and the current capsule gets a
clean slate.

**Alternative: Redis-backed session store.** This would work for a centralized
deployment but violates the capsule model. The session store should be a
capsule like any other, subject to the same capability and isolation rules.
External dependencies bypass the security model.

**Alternative: Frontend-local storage.** Each frontend stores its own history
(e.g., in IndexedDB for web, SQLite for CLI). This is the status quo and it
is exactly what this RFC replaces. Frontend-local storage means no shared
history, no audit trail, and N implementations of the same logic.

# Prior art
[prior-art]: #prior-art

- **LangChain ChatMessageHistory.** Provides an abstract base class for chat
  message stores with `add_message`, `get_messages`, and `clear` methods. The
  session protocol's three operations map directly to these. LangChain supports
  multiple backends (Redis, PostgreSQL, in-memory) but does not define an IPC
  contract or session chaining. Each backend is a direct integration, not a
  protocol.

- **Claude conversation history API.** Anthropic's API uses a `messages` array
  in each request, with the client responsible for maintaining and sending the
  full history. There is no server-side session store. The session protocol
  provides what Anthropic's API deliberately omits: persistent, server-side
  history management.

- **Redis-backed session stores.** A common pattern in web applications: store
  session data in Redis with a TTL. Simple and fast, but no schema versioning,
  no session chaining, no capability-scoped access. The session protocol adds
  all three on top of the basic get/set pattern.

- **POSIX filesystem model.** The session capsule is the filesystem for
  conversations. Append is `write()`. Get messages is `read()`. Clear is
  creating a new file and keeping the old one. Session chaining is the inode
  chain. The analogy is intentional: the session capsule should be as boring
  and reliable as a filesystem.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Pagination for get_messages.** Long sessions will produce large payloads.
  Should pagination be cursor-based or offset-based? Should it be added to v1
  as optional fields or deferred to v2?

- **TTL and garbage collection.** Old sessions in the chain accumulate forever.
  Should the session capsule support TTL-based expiry? Or should a separate
  garbage collection capsule handle cleanup?

- **Acknowledged append.** For use cases where message delivery must be
  confirmed (e.g., billing events), should there be an `append_ack` operation
  with a response topic? What are the capability implications?

- **Cross-session search.** Walking the chain is O(n) in the number of sessions.
  Should there be an index capsule that enables search across the full history?

- **Message metadata.** Should messages carry metadata (timestamps, source
  capsule ID, capability token hash) in addition to the core fields? If so,
  should that be a required field or an optional `metadata` bag?

# Future possibilities
[future-possibilities]: #future-possibilities

- **Session compaction.** A compaction protocol that summarizes old messages
  (via an LLM capsule), replaces the full history with the summary, and chains
  the compacted session back to the original. The session chaining model is
  designed to support this.

- **Streaming get_messages.** For very long sessions, stream messages in chunks
  instead of returning the full array. This would use a sequence of IPC
  messages on the response topic.

- **Session forking.** Create a new session that starts with a copy of an
  existing session's messages, then diverges. Useful for "what if" scenarios
  where an agent explores multiple conversation branches.

- **Cross-session search index.** A separate capsule that subscribes to
  `session.append` and builds a searchable index across all sessions. Enables
  "find the conversation where we discussed X" without walking chains.

- **Message-level capabilities.** Restrict which capsules can read which
  messages within a session based on message metadata. For example, a
  "supervisor" message that only the orchestrator can read but the user-facing
  agent cannot see.

- **Acknowledged append (v2).** A request/response variant of append that
  confirms delivery and returns the updated message count. Uses the same
  correlation ID pattern as get_messages.

- **Bulk history export.** An operation that walks the full session chain and
  returns the concatenated history across all linked sessions, for audit or
  migration purposes.
