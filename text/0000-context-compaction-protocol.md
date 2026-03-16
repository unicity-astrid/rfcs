- Feature Name: `context_compaction_protocol`
- Start Date: 2026-03-16
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The context compaction protocol defines how the context engine capsule manages
conversation history when it approaches the model's context window limit. It
provides two operations (compact, estimate_tokens) and two lifecycle hooks
(before_compaction, after_compaction). Plugin capsules can pin messages to
protect them from removal and can veto compaction entirely. The default
strategy is `summarize_and_truncate`.

This RFC documents the protocol as currently implemented in
`astrid-capsule-context-engine`. It is not a proposal for new work.

# Motivation
[motivation]: #motivation

LLMs have finite context windows. As conversations grow, the runtime must
decide what to keep and what to discard. This decision cannot live in a single
component because plugin capsules hold domain-specific knowledge about which
messages matter. A tool result from five turns ago might be irrelevant to a
chat capsule but critical to a debugging capsule.

The context engine is the memory manager. It controls page eviction (message
removal), but user-space processes (plugin capsules) can pin pages they need.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Three actors participate in compaction:

1. **React loop** (or orchestrator) - requests compaction when tokens are high.
2. **Context engine** - runs the compaction strategy, respects pins.
3. **Plugin capsules** - pin messages or veto via before_compaction hook.

```text
Requester          Context engine         Plugin A        Plugin B
    |                    |                    |               |
    |-- compact -------->|                    |               |
    |                    |-- before_compact ->|               |
    |                    |-- before_compact --|-------------->|
    |                    |<-- pin msg-42 -----|               |
    |                    |<-- skip: false ----|--- pin msg-7 -|
    |                    |                    |               |
    |                    |-- merge: union pins, any skip wins |
    |                    |-- summarize_and_truncate           |
    |                    |-- after_compact (fire-and-forget)  |
    |<-- response -------|                    |               |
```

The context engine subscribes to `context_engine.v1.*` and runs a blocking
event loop. It also provides standalone token estimation via
`context_engine.v1.estimate_tokens`.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## IPC topics

| Direction | Topic | Payload | Actor |
|---|---|---|---|
| Subscribe | `context_engine.v1.compact` | `CompactRequest` | Context engine |
| Publish | `context_engine.v1.response.compact` | `CompactResponse` | Context engine |
| Subscribe | `context_engine.v1.estimate_tokens` | `EstimateRequest` | Context engine |
| Publish | `context_engine.v1.response.estimate_tokens` | `EstimateResponse` | Context engine |
| Publish | `context_engine.v1.hook.before_compaction` | `BeforeCompactionPayload` | Context engine |
| Subscribe | `context_engine.v1.hook_response.{request_id}` | `BeforeCompactionHookResponse` | Context engine |
| Publish | `context_engine.v1.hook.after_compaction` | `AfterCompactionPayload` | Context engine |

## Message schemas

### CompactRequest

```json
{
  "session_id": "string",
  "messages": [{}],
  "max_tokens": 100000,
  "target_tokens": 75000
}
```

`target_tokens` is clamped to not exceed `max_tokens`.

### CompactResponse

```json
{
  "messages": [{}],
  "compacted": true,
  "messages_removed": 42,
  "strategy": "summarize_and_truncate"
}
```

### EstimateRequest / EstimateResponse

```json
{ "messages": [{}] }
```

```json
{ "estimated_tokens": 42000 }
```

### BeforeCompactionPayload

```json
{
  "session_id": "string",
  "messages": [{}],
  "message_count": 200,
  "estimated_tokens": 95000,
  "max_tokens": 100000,
  "response_topic": "context_engine.v1.hook_response.compact-1710576000000-0"
}
```

### BeforeCompactionHookResponse (camelCase)

```json
{
  "skip": false,
  "pinnedMessageIds": ["msg-42", "msg-87"],
  "customStrategy": null
}
```

Also accepts `protected_message_ids` as an alias for `pinnedMessageIds`.

### AfterCompactionPayload

```json
{
  "session_id": "string",
  "messages_before": 200,
  "messages_after": 158,
  "tokens_before": 95000,
  "tokens_after": 72000,
  "strategy_used": "summarize_and_truncate"
}
```

## Compaction behavior

1. Parse request. Clamp `target_tokens <= max_tokens`.
2. Estimate current tokens via `strategy::estimate_total_tokens`.
3. Fire `before_compaction` hook (subscribe-before-publish, block-wait up to
   `hook_timeout_ms`, max 50 responses).
4. Merge responses: any `skip: true` wins, union all `pinnedMessageIds`.
5. If skipped: return messages unchanged with `strategy: "skipped"`.
6. Run `strategy::summarize_and_truncate` with pinned IDs and `keep_recent`.
7. Fire `after_compaction` notification (fire-and-forget).
8. Publish compacted result.

The `keep_recent` config (default 10) controls how many recent turns are
always preserved regardless of token budget.

## Error handling

| Condition | Behavior |
|---|---|
| Invalid compact request | Error published to response topic |
| Invalid estimate_tokens request | Error published to response topic |
| Hook timeout with partial responses | Proceed with collected responses |
| All hook responses have `skip: true` | Return unchanged with `strategy: "skipped"` |
| Invalid pinned message ID | Silently ignored |

## Capsule.toml manifest

```toml
[package]
name = "astrid-capsule-context-engine"
version = "0.1.0"
description = "Pluggable context window compaction with interceptor hook support"

[capabilities]
ipc_publish = [
    "context_engine.v1.response.*",
    "context_engine.v1.hook.before_compaction",
    "context_engine.v1.hook.after_compaction",
]
ipc_subscribe = [
    "context_engine.v1.*",
    "context_engine.v1.hook.before_compaction",
    "context_engine.v1.hook.after_compaction",
    "context_engine.v1.hook_response.*",
]
```

# Drawbacks
[drawbacks]: #drawbacks

- The before_compaction hook adds up to 2000ms of latency before compaction
  proceeds. In the common case where no plugins respond, the engine waits
  the full timeout.
- Any single plugin can block compaction by returning `skip: true`. A
  misbehaving plugin could cause the context window to overflow.
- Token estimation uses a character-based heuristic, not a real tokenizer.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why fan-out hooks?** The engine lacks domain-specific knowledge. Only
plugins know which messages they depend on. Fan-out lets each plugin declare
its needs.

**Why "any skip wins"?** Compaction is destructive. Removing a message a
plugin depends on causes silent failures. The conservative choice is to let
any plugin veto.

**Why separate token estimation?** Token estimation is useful outside
compaction. A capsule might check whether adding a tool result will exceed
the budget before generating it.

# Prior art
[prior-art]: #prior-art

- **LangChain ConversationSummaryMemory**: Progressive summarization with no
  hook system. Works for single-chain, not multi-agent coordination.
- **Linux page eviction with mlock()**: Kernel evicts pages, processes pin
  via `mlock()`. Same pattern here.
- **Kubernetes admission webhooks**: Notify subscribers before mutation, let
  them modify or reject. The before_compaction hook follows this pattern.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should there be a per-capsule limit on how many messages can be pinned?
- Should cascading compaction (compacting an already-compacted session)
  be supported?
- Should messages carry priority weights that influence eviction order?

# Future possibilities
[future-possibilities]: #future-possibilities

- Incremental compaction (compact oldest 20% first, re-evaluate).
- Pluggable summarization strategies (extractive, abstractive, hybrid).
- Compaction analytics (frequency, summary quality, pin patterns).
