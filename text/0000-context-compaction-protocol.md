- Feature Name: `context_compaction_protocol`
- Start Date: 2026-03-16
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

This RFC defines the IPC contract for context window compaction in Astrid. It
specifies four request/response message pairs and two lifecycle hooks that let
the context engine coordinate with plugin capsules to summarize, truncate, and
manage conversation history. Plugin capsules can pin critical messages to protect
them from removal and can veto compaction entirely. The protocol also provides a
standalone token estimation operation for pre-flight budget checks.

# Motivation
[motivation]: #motivation

Large language models have finite context windows. As a conversation grows, the
runtime must decide what to keep, what to summarize, and what to discard. This
decision cannot live inside a single component because plugin capsules hold
domain-specific knowledge about which messages matter. A tool-use result that a
coding assistant produced five turns ago might be irrelevant to a chat capsule
but critical to a debugging capsule that depends on that output.

Without a protocol:

- The context engine compacts blindly, discarding messages that plugins still
  need.
- Plugin capsules have no way to signal "do not remove this message" or "do not
  compact right now."
- Token estimation requires ad-hoc internal calls with no stable contract,
  making pre-flight budget checks unreliable across capsule boundaries.
- Session continuity breaks because there is no defined handoff between the old
  session and the compacted successor.

In the OS model, the context engine is the memory manager. It controls page
eviction (message removal), but user-space processes (plugin capsules) can pin
pages they still need. This RFC defines that pinning and eviction contract over
IPC.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## The context engine as memory manager

The context engine owns the conversation history for a session. When the token
count approaches the model's context window limit, the engine triggers
compaction: it summarizes older messages into a shorter representation and
truncates the history to fit within budget.

Plugin capsules participate in this process through two hooks:

1. **Before compaction** - The engine asks every plugin: "I am about to compact.
   Do you have any objections or messages you need preserved?" Plugins respond
   with pinned message IDs or a skip flag.
2. **After compaction** - The engine notifies every plugin: "Compaction is done.
   Here is the new state." Plugins update their internal bookkeeping.

## Walking through a compaction

A session has 200 messages totaling 95,000 tokens against a 100,000-token
budget. The context engine decides to compact.

1. The engine publishes a `before_compaction` hook to all plugin capsules. The
   payload includes the session ID and a list of all message IDs under
   consideration.
2. A coding assistant plugin responds: "Pin messages `msg-42` and `msg-87` -
   they contain tool results I still reference." A safety plugin responds:
   "Pin message `msg-12` - it contains the system prompt override."
3. The engine unions all pinned IDs: `{msg-12, msg-42, msg-87}`. No plugin
   set `skip: true`, so compaction proceeds.
4. The engine runs `summarize_and_truncate`. It preserves the 10 most recent
   turns (the `keep_recent` default), preserves the three pinned messages, and
   summarizes everything else into a compact representation.
5. The engine publishes an `after_compaction` notification with the new message
   list and token count.
6. The session capsule creates a new session with a parent pointer back to the
   original, establishing the session chain.

## Token estimation

Before triggering compaction, the engine (or any capsule with the right
capability) can request a token estimate for a set of messages. This is a
standalone operation, separate from compaction, useful for pre-flight checks
like "will this new tool result push us over budget?"

## For capsule developers

If your capsule stores references to specific messages (by ID), you should
subscribe to the `before_compaction` hook and pin those message IDs. If your
capsule depends on the full uncompacted history for correctness, set `skip: true`
in your hook response to block compaction until you are ready.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## IPC topic conventions

All topics follow the Astrid IPC topic naming convention from RFC-0001. The
`context_engine.v1` prefix identifies the context engine subsystem at version 1.

| Topic | Direction | Type |
|-------|-----------|------|
| `context_engine.v1.compact` | Requester to engine | Request |
| `context_engine.v1.response.compact` | Engine to requester | Response |
| `context_engine.v1.estimate_tokens` | Requester to engine | Request |
| `context_engine.v1.response.estimate_tokens` | Engine to requester | Response |
| `context_engine.v1.hook.before_compaction` | Engine to all plugins | Fan-out hook |
| `context_engine.v1.hook.after_compaction` | Engine to all plugins | Notification |

## Message schemas

All payloads are JSON. Field types use Rust naming conventions. Optional fields
may be omitted entirely (not sent as `null`).

### `context_engine.v1.compact` (request)

Sent by any capsule (typically the orchestrator or session capsule) to request
compaction of a session's context window.

```json
{
  "session_id": "string, required - the session to compact",
  "target_token_budget": "u64, optional - desired token count after compaction; defaults to model context window * 0.75",
  "keep_recent": "u32, optional - number of recent turns to always preserve; defaults to 10",
  "correlation_id": "string, required - unique ID to correlate request with response"
}
```

**Field constraints:**

| Field | Type | Required | Default | Constraints |
|-------|------|----------|---------|-------------|
| `session_id` | String | Yes | - | Must reference an existing session |
| `target_token_budget` | u64 | No | model window * 0.75 | Must be > 0 |
| `keep_recent` | u32 | No | 10 | Must be >= 1 |
| `correlation_id` | String | Yes | - | UUIDv7 recommended |

### `context_engine.v1.response.compact` (response)

Sent by the context engine after compaction completes (or fails).

```json
{
  "correlation_id": "string, required - matches the request",
  "status": "string, required - one of: 'compacted', 'skipped', 'error'",
  "session_id": "string, required - the original session ID",
  "new_session_id": "string, optional - the successor session ID if session chaining occurred",
  "messages_before": "u32, required - message count before compaction",
  "messages_after": "u32, required - message count after compaction",
  "tokens_before": "u64, required - estimated token count before compaction",
  "tokens_after": "u64, required - estimated token count after compaction",
  "pinned_message_ids": ["string, the IDs that were pinned by plugins"],
  "summary_text": "string, optional - the generated summary of compacted messages",
  "skip_reason": "string, optional - present when status is 'skipped'; identifies which capsule(s) vetoed",
  "error": "string, optional - present when status is 'error'; human-readable description"
}
```

**Status values:**

| Status | Meaning |
|--------|---------|
| `compacted` | Compaction succeeded. `new_session_id` is present if session chaining occurred. |
| `skipped` | A plugin set `skip: true` in its hook response. No messages were modified. |
| `error` | Compaction failed. The `error` field describes the failure. |

### `context_engine.v1.estimate_tokens` (request)

Requests a token count estimate for a set of messages or an entire session.

```json
{
  "session_id": "string, optional - estimate tokens for all messages in this session",
  "message_ids": ["string, optional - estimate tokens for these specific messages"],
  "raw_text": "string, optional - estimate tokens for this raw text",
  "model_id": "string, optional - the model whose tokenizer to use; defaults to the session's model",
  "correlation_id": "string, required - unique ID to correlate request with response"
}
```

Exactly one of `session_id`, `message_ids`, or `raw_text` must be provided. If
`session_id` is provided alongside `message_ids`, the engine estimates only the
specified messages within that session.

**Field constraints:**

| Field | Type | Required | Default | Constraints |
|-------|------|----------|---------|-------------|
| `session_id` | String | Conditional | - | Must reference an existing session |
| `message_ids` | Vec\<String\> | Conditional | - | Must be non-empty if provided |
| `raw_text` | String | Conditional | - | Must be non-empty if provided |
| `model_id` | String | No | Session's model | Must reference a known model |
| `correlation_id` | String | Yes | - | UUIDv7 recommended |

### `context_engine.v1.response.estimate_tokens` (response)

Returns the token estimate.

```json
{
  "correlation_id": "string, required - matches the request",
  "status": "string, required - one of: 'ok', 'error'",
  "total_tokens": "u64, required when status is 'ok' - the estimated token count",
  "breakdown": [
    {
      "message_id": "string - the message ID",
      "tokens": "u64 - estimated tokens for this message"
    }
  ],
  "model_id": "string, required when status is 'ok' - the tokenizer model used",
  "error": "string, optional - present when status is 'error'"
}
```

The `breakdown` field is present only when `message_ids` or `session_id` was
used in the request. It is omitted for `raw_text` estimates.

### `context_engine.v1.hook.before_compaction` (fan-out hook)

The context engine publishes this hook to all plugin capsules before performing
compaction. This is a fan-out: every subscribed capsule receives the message and
may respond.

**Hook request (engine to plugins):**

```json
{
  "hook_id": "string, required - unique ID for this hook invocation",
  "session_id": "string, required - the session about to be compacted",
  "message_ids": ["string, required - all message IDs under consideration for compaction"],
  "current_token_count": "u64, required - current estimated token count",
  "target_token_budget": "u64, required - the target after compaction",
  "keep_recent": "u32, required - number of recent turns that will be preserved regardless",
  "timeout_ms": "u64, required - how long the engine will wait for responses"
}
```

**Hook response (plugin to engine):**

```json
{
  "hook_id": "string, required - must match the hook request",
  "capsule_id": "string, required - the responding capsule's identity",
  "skip": "bool, optional - if true, compaction is vetoed; defaults to false",
  "pinned_message_ids": ["string, optional - message IDs this capsule needs preserved"],
  "reason": "string, optional - human-readable explanation for skip or pin decisions"
}
```

**Hook response field constraints:**

| Field | Type | Required | Default | Constraints |
|-------|------|----------|---------|-------------|
| `hook_id` | String | Yes | - | Must match the request's `hook_id` |
| `capsule_id` | String | Yes | - | Must be the capsule's registered identity |
| `skip` | bool | No | false | Any `true` value across all responses vetoes compaction |
| `pinned_message_ids` | Vec\<String\> | No | [] | Each ID must exist in the hook request's `message_ids` |
| `reason` | String | No | - | For audit logging; not parsed by the engine |

### `context_engine.v1.hook.after_compaction` (notification)

Published after compaction completes. This is fire-and-forget: the engine does
not wait for or collect responses.

```json
{
  "session_id": "string, required - the original session ID",
  "new_session_id": "string, optional - the successor session ID if chaining occurred",
  "messages_removed": ["string, required - IDs of messages that were removed"],
  "messages_retained": ["string, required - IDs of messages that survived"],
  "summary_message_id": "string, optional - the ID of the generated summary message, if one was inserted",
  "tokens_after": "u64, required - token count after compaction"
}
```

## Compaction flow

The context engine executes compaction as the following ordered sequence:

1. **Publish `before_compaction` hook.** Fan out to all subscribed plugin
   capsules. Include the full list of message IDs, current token count, target
   budget, and timeout.

2. **Collect hook responses.** Wait up to `hook_timeout_ms` (default: 2000ms).
   Accept at most 50 responses. If the timeout expires, proceed with whatever
   responses have arrived. Late responses are discarded.

3. **Merge hook responses.**
   - If any response has `skip: true`, set compaction status to `skipped`.
     Record the capsule IDs and reasons in the response. Publish the compact
     response with status `skipped` and stop.
   - Union all `pinned_message_ids` across every response into a single set.
     Ignore any pinned ID that does not exist in the original message list.

4. **Run `summarize_and_truncate`.** The engine:
   - Partitions messages into three sets: pinned (never removed), recent (the
     last `keep_recent` turns, never removed), and candidates (everything else).
   - Generates a summary of the candidate messages using the session's LLM.
   - Inserts the summary as a new system message at the boundary between removed
     and retained messages.
   - Removes the candidate messages from the active history.

5. **Session chaining.** The session capsule creates a new session with:
   - A `parent_session_id` pointing to the original session.
   - The compacted message list as its initial history.
   - The summary message included at the appropriate position.

6. **Publish `after_compaction` notification.** Fire-and-forget to all
   subscribed capsules. Include the removed and retained message IDs, the
   summary message ID, and the post-compaction token count.

7. **Publish compact response.** Send the `context_engine.v1.response.compact`
   message to the original requester with status `compacted`, token counts, and
   the new session ID.

## Configuration

The context engine exposes the following configuration parameters. These can be
set in the kernel configuration or overridden per-session.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hook_timeout_ms` | u64 | 2000 | Maximum time to wait for `before_compaction` hook responses |
| `max_hook_responses` | u32 | 50 | Maximum number of hook responses to collect before proceeding |
| `keep_recent` | u32 | 10 | Default number of recent turns to preserve during compaction |
| `auto_compact_threshold` | f64 | 0.85 | Trigger automatic compaction when token usage exceeds this fraction of the model's context window |
| `target_token_ratio` | f64 | 0.75 | Default target token budget as a fraction of the model's context window |

## Ordering and concurrency guarantees

- **One compaction at a time.** The context engine holds a per-session lock
  during compaction. A second compact request for the same session while one is
  in progress receives an error response with `"error": "compaction already in
  progress"`.
- **Hook ordering.** The `before_compaction` hook is published before any
  messages are modified. The `after_compaction` notification is published after
  all mutations are committed to the session store.
- **Atomicity.** The summarize-and-truncate step is atomic from the perspective
  of other readers: either the full compaction is visible (new session with
  compacted history) or the original session remains unchanged. There is no
  intermediate state where some messages are removed but the summary is not yet
  inserted.
- **Token estimation is stateless.** Token estimation requests do not acquire
  locks and can execute concurrently with compaction. The estimate reflects a
  point-in-time snapshot; it may become stale if compaction runs concurrently.

## Error handling

| Error condition | Behavior |
|----------------|----------|
| Session not found | Response with status `error`, error: `"session not found"` |
| Compaction already in progress | Response with status `error`, error: `"compaction already in progress"` |
| LLM summarization fails | Response with status `error`, error: `"summarization failed: {details}"`. No messages are modified. |
| Hook timeout expires | Proceed with responses collected so far. This is not an error. |
| All hook responses have `skip: true` | Response with status `skipped`. No messages are modified. |
| Invalid pinned message ID in hook response | Ignored silently. The engine logs a warning but does not fail. |
| Token estimation for nonexistent session | Response with status `error`, error: `"session not found"` |
| Token estimation with no input | Response with status `error`, error: `"exactly one of session_id, message_ids, or raw_text is required"` |

## Capability requirements

| Operation | Required capability |
|-----------|-------------------|
| `context_engine.v1.compact` | `context:compact` |
| `context_engine.v1.estimate_tokens` | `context:estimate` |
| `context_engine.v1.hook.before_compaction` (subscribe) | `context:hook:before_compaction` |
| `context_engine.v1.hook.after_compaction` (subscribe) | `context:hook:after_compaction` |

A capsule that lacks the required capability will not receive hook messages and
cannot publish to the request topics. The kernel enforces this at the IPC layer.

# Drawbacks
[drawbacks]: #drawbacks

- **Fan-out latency.** The `before_compaction` hook adds up to 2000ms of
  latency before compaction can proceed. In the common case where no plugins
  respond, the engine waits the full timeout. Mitigation: the timeout is
  configurable, and the engine proceeds with partial results.

- **Veto power is absolute.** Any single plugin can block compaction indefinitely
  by always returning `skip: true`. This is by design (fail-secure), but a
  misbehaving plugin could cause the context window to overflow. Mitigation: the
  kernel can revoke a capsule's `context:hook:before_compaction` capability if it
  abuses the veto.

- **Summarization quality.** The protocol defines when and how to trigger
  compaction but delegates the actual summarization to the LLM. Poor summaries
  lose critical context. This RFC intentionally does not specify the
  summarization prompt or strategy because that is an implementation detail that
  will evolve independently.

- **Complexity for simple capsules.** Capsules that do not care about message
  pinning still receive hook fan-outs if they subscribe. They must either ignore
  them or not subscribe. The subscription model handles this: capsules that do
  not need hooks simply do not request the hook capabilities.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why fan-out hooks instead of a central policy?

A central compaction policy would be simpler: the engine decides what to keep
and what to discard based on its own heuristics. But the engine lacks
domain-specific knowledge. A coding assistant knows that a compilation error
from three turns ago is still relevant. A retrieval capsule knows that a search
result it injected is still being referenced. Only the plugins themselves know
which messages they depend on.

Fan-out hooks let each plugin declare its needs. The engine merges these
declarations and executes compaction with full information.

## Why "any skip wins" instead of majority vote?

Compaction is a destructive operation. Removing a message that a plugin depends
on can cause silent failures. The conservative choice is to let any plugin veto.
If this proves too restrictive in practice, a future RFC can introduce priority
tiers or quorum-based voting. Starting permissive and tightening is harder to
reverse than starting strict and loosening.

## Why session chaining instead of in-place mutation?

In-place mutation of the message history creates problems:

- No audit trail of what was removed.
- No way to "go back" to the full history if the summary is insufficient.
- Race conditions with concurrent readers.

Session chaining creates a new session with a parent pointer. The original
session remains immutable. This gives a full audit trail, supports rollback by
re-reading the parent, and eliminates read-write races.

## Why separate token estimation from compaction?

Token estimation is useful outside of compaction. A capsule might want to check
whether adding a large tool result will exceed the budget before generating it.
Coupling estimation to the compaction flow would force capsules to trigger a
full compact cycle just to get a token count.

## Alternative: per-message TTLs

Instead of hook-based pinning, each message could carry a TTL. The engine
evicts expired messages automatically. This was rejected because TTLs are too
coarse. A message's relevance depends on the conversation state, not wall-clock
time. A tool result from 30 minutes ago might be critical; a greeting from 5
seconds ago is not.

## Alternative: plugin-driven compaction

Instead of a central engine, each plugin could compact its own slice of the
context. This was rejected because it fragments the token budget. The engine
needs a global view to make optimal tradeoffs across the entire message history.

## Impact of not standardizing

Without this RFC, each deployment implements its own compaction strategy.
Capsules cannot portably declare pinning requirements. Token estimation has no
stable interface. Session chaining semantics vary across implementations.

# Prior art
[prior-art]: #prior-art

- **Claude context window management.** Anthropic's Claude models handle long
  conversations by truncating older messages from the context. The system has no
  plugin hook mechanism; the truncation policy is internal. This RFC adds the
  plugin coordination layer that Claude's approach lacks.

- **GPT conversation summarization.** OpenAI's ChatGPT summarizes older messages
  when the context window fills. The summarization is opaque to the user and to
  any tools in the conversation. This RFC makes summarization observable and
  influenceable by plugins.

- **LangChain ConversationSummaryMemory.** LangChain provides a
  `ConversationSummaryMemory` class that progressively summarizes conversation
  history. It runs inline during message processing and has no hook system for
  external components to influence what gets summarized. The approach works for
  single-chain applications but does not support multi-agent coordination.

- **Operating system page eviction.** Linux's memory manager uses a clock
  algorithm to select pages for eviction, but processes can pin pages via
  `mlock()`. This RFC's pinning mechanism draws from the same pattern: the
  memory manager (context engine) decides the eviction policy, but user-space
  (plugins) can pin specific pages (messages).

- **Event-driven hook systems.** WordPress's action/filter hooks, Git's pre/post
  hooks, and Kubernetes admission webhooks all follow the pattern of "notify
  subscribers before a mutation, let them modify or reject it." The
  `before_compaction` hook follows this established pattern.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Summarization strategy.** This RFC defines when compaction triggers and how
  plugins influence it, but does not specify the summarization prompt or
  algorithm. Should a follow-up RFC standardize the summarization interface, or
  is it purely an implementation detail?

- **Pin limits.** Should there be a maximum number of messages a single capsule
  can pin? Without limits, a capsule could pin every message and effectively
  disable compaction without using `skip: true`. A per-capsule pin budget might
  be needed.

- **Cascading compaction.** When a compacted session itself grows too large,
  should the engine compact the compacted session (creating a chain of depth 2+)?
  What are the implications for summary quality as summaries of summaries
  accumulate?

- **Priority-based eviction.** The current design treats all non-pinned,
  non-recent messages equally. Should messages carry priority weights that
  influence which messages are summarized first?

- **Cross-session pinning.** If a capsule pins a message in session A, and
  session A gets chained to session B, should the pin carry over to session B's
  copy of that message?

# Future possibilities
[future-possibilities]: #future-possibilities

- **Incremental compaction.** Instead of summarizing all candidates at once, the
  engine could compact in waves - summarizing the oldest 20% first, then
  re-evaluating. This would reduce the latency and LLM cost of each compaction
  step.

- **Semantic deduplication.** Before summarization, the engine could detect and
  merge semantically duplicate messages (e.g., repeated tool invocations with the
  same result).

- **Compaction analytics.** Expose metrics on compaction frequency, summary
  quality scores, pin patterns, and veto frequency. This data could inform
  automatic tuning of `auto_compact_threshold` and `keep_recent`.

- **User-visible compaction.** Surface compaction events in the frontend so users
  know when and what was summarized. Allow users to "expand" a summary to see the
  original messages (via the session chain).

- **Tiered pinning.** Instead of binary pin/no-pin, allow plugins to assign
  priority levels to messages. The engine evicts lowest-priority messages first,
  only touching higher-priority messages when necessary.

- **Pluggable summarizers.** Define a standard interface for summarization
  strategies (extractive, abstractive, hybrid) so that capsule developers can
  swap in custom summarizers without modifying the context engine.

- **Quota-based veto budgets.** Assign each capsule a limited number of veto
  tokens per time window. Once exhausted, the capsule's `skip: true` responses
  are downgraded to advisory warnings rather than hard vetoes.
