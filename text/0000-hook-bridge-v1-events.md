- Feature Name: `hook_bridge_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#15](https://github.com/unicity-astrid/rfcs/pull/15)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `hook.v1` event contract defines how lifecycle events are translated into
hook executions on the Astrid event bus. A hook bridge capsule subscribes to
lifecycle event types, executes user-defined hook scripts, and publishes
results.

# Motivation
[motivation]: #motivation

Users need to run custom logic (scripts, notifications, side effects) in
response to runtime events. The hook bridge contract must guarantee that:

- Hook execution is decoupled from the lifecycle event publisher.
- Hook results are observable.
- The lifecycle-to-hook mapping is configurable via interceptors.
- Hook failures do not block the runtime.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Three actors participate:

1. **Kernel/Orchestrator** - broadcasts lifecycle events.
2. **Hook bridge capsule** - subscribes to lifecycle events, runs hooks.
3. **Hook consumers** (optional) - observe hook results.

```text
Kernel                    Hook bridge               Hook consumer
    |                         |                          |
    |-- lifecycle event ----->|                          |
    |   (astrid.v1.lifecycle. |-- execute hook script    |
    |    tool_call_completed) |                          |
    |                         |-- hook result ---------->|
    |                         |   (hook.v1.result.*)     |
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| result | `hook.v1.result.{hook_name}` | `Custom` | Hook bridge | Hook consumers |

The hook bridge does not have its own request topics. It subscribes to
lifecycle event types via interceptors:

```toml
# Capsule.toml (hook bridge)
[[interceptor]]
event = "astrid.v1.lifecycle.session_created"
action = "on_session_created"

[[interceptor]]
event = "astrid.v1.lifecycle.tool_call_started"
action = "on_tool_call_started"

[[interceptor]]
event = "astrid.v1.lifecycle.tool_call_completed"
action = "on_tool_call_completed"

# ... etc for each lifecycle event that has registered hooks
```

## Message schemas

### Hook result

```json
{
  "type": "custom",
  "data": {
    "hook_name": "string",
    "event_type": "astrid.v1.lifecycle.tool_call_completed",
    "success": true,
    "output": "string | null",
    "error": "string | null",
    "duration_ms": 150
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `hook_name` | string | yes | The hook identifier. |
| `event_type` | string | yes | The lifecycle event that triggered this hook. |
| `success` | boolean | yes | Whether the hook script exited successfully. |
| `output` | string/null | no | Hook script stdout. |
| `error` | string/null | no | Hook script stderr or error message. |
| `duration_ms` | integer | yes | Execution time. |

## Lifecycle events mapped to hooks

The hook bridge subscribes to these lifecycle event_type strings:

| Lifecycle event | Semantic hook |
|----------------|---------------|
| `astrid.v1.lifecycle.session_created` | Session start |
| `astrid.v1.lifecycle.session_ended` | Session end |
| `astrid.v1.lifecycle.tool_call_started` | Before tool execution |
| `astrid.v1.lifecycle.tool_call_completed` | After tool execution |
| `astrid.v1.lifecycle.tool_result_persisting` | Tool result being saved |
| `astrid.v1.lifecycle.message_received` | User message received |
| `astrid.v1.lifecycle.message_sending` | Agent message about to send |
| `astrid.v1.lifecycle.message_sent` | Agent message delivered |
| `astrid.v1.lifecycle.sub_agent_spawned` | Sub-agent created |
| `astrid.v1.lifecycle.sub_agent_completed` | Sub-agent finished |
| `astrid.v1.lifecycle.sub_agent_failed` | Sub-agent errored |
| `astrid.v1.lifecycle.sub_agent_cancelled` | Sub-agent cancelled |
| `astrid.v1.lifecycle.context_compaction_started` | Compaction begins |
| `astrid.v1.lifecycle.context_compaction_completed` | Compaction finished |
| `astrid.v1.lifecycle.kernel_started` | Kernel boot |
| `astrid.v1.lifecycle.kernel_shutdown` | Kernel shutting down |

## Behavioral requirements

A conforming **hook bridge** must:

1. Subscribe to lifecycle events via interceptors in `Capsule.toml`.
2. Execute registered hook scripts when matching events arrive.
3. Publish results to `hook.v1.result.{hook_name}`.
4. Not block on hook execution. Hooks run asynchronously.
5. Enforce timeouts on hook scripts to prevent hanging.
6. Capture stdout/stderr for the result payload.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Hook script not found | Result with `success: false` and error description |
| Hook script times out | Result with `success: false` and timeout error |
| Hook script exits non-zero | Result with `success: false` and stderr |
| No hook consumers | Result published to empty topic, discarded |

# Drawbacks
[drawbacks]: #drawbacks

- The hook bridge must enumerate every lifecycle event it wants to
  intercept in `Capsule.toml`. There is no "subscribe to all lifecycle
  events" wildcard that works with the dispatcher's exact-segment matching.
- Hook scripts are fire-and-forget. There is no mechanism for a hook to
  veto an action (e.g. block a tool call).

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why a bridge capsule instead of direct hook execution in the kernel?**
Isolation. Hook scripts run in capsule context with capability restrictions.
A malicious hook cannot access kernel internals.

**Why fire-and-forget?** Blocking hooks would add latency to every lifecycle
transition. If a hook needs to veto an action, that should be modeled as an
approval gate, not a hook.

# Prior art
[prior-art]: #prior-art

- **Git hooks**: Scripts triggered by lifecycle events (pre-commit,
  post-receive). Fire-and-forget with exit code signaling.
- **GitHub Actions workflows**: Event-triggered automation with
  structured output.
- **Claude Code hooks**: Pre/post tool execution hooks with
  allow/deny semantics.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should hooks be able to veto actions (blocking hooks with allow/deny)?
- Should hook registration be dynamic (runtime add/remove) or only
  static (Capsule.toml)?

# Future possibilities
[future-possibilities]: #future-possibilities

- Blocking hooks with veto capability for security-critical events.
- Hook priority ordering when multiple hooks subscribe to the same event.
- Remote hook execution (webhook-style HTTP callbacks).
