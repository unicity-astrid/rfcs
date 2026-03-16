- Feature Name: `tool_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#9](https://github.com/unicity-astrid/rfcs/pull/9)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `tool.v1` event contract defines the topic patterns and message schemas for
tool execution on the Astrid event bus. A stateless router validates tool names
and forwards requests to capsules. Any capsule can implement a tool by
subscribing to the appropriate topic and publishing a result.

# Motivation
[motivation]: #motivation

Agents call tools. The orchestrator decides which tool to invoke. A tool capsule
executes it. The contract between them must be standardized so that:

- Third-party tool capsules work without knowing the orchestrator's internals.
- The router can validate and forward requests without understanding tool logic.
- Tool results have a consistent shape that the LLM can reason about.
- Errors are values, not exceptions, so the LLM can recover.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Three actors participate in tool execution:

1. **Orchestrator** (react loop) - decides to call a tool, publishes a request.
2. **Router** - validates the tool name, forwards to the right topic.
3. **Tool capsule** - executes the tool, publishes a result.

```text
Orchestrator                  Router                     Tool capsule
    |                           |                            |
    |-- ToolExecuteRequest ---->|                            |
    |   (tool.v1.request.       |-- validate tool name       |
    |    execute)                |-- ToolExecuteRequest ----->|
    |                           |   (tool.v1.execute.        |
    |                           |    {tool_name})            |-- execute
    |                           |                            |
    |                           |<--- ToolExecuteResult -----|
    |                           |     (tool.v1.execute.      |
    |                           |      {tool_name}.result)   |
    |<--- ToolExecuteResult ----|                            |
    |     (tool.v1.execute.     |                            |
    |      result)              |                            |
```

A capsule that implements a tool subscribes to `tool.v1.execute.{tool_name}` via
an interceptor in its `Capsule.toml`. It receives a `ToolExecuteRequest`, does
its work, and publishes a `ToolExecuteResult` to
`tool.v1.execute.{tool_name}.result`.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| request | `tool.v1.request.execute` | `ToolExecuteRequest` | Orchestrator | Router |
| forward | `tool.v1.execute.{tool_name}` | `ToolExecuteRequest` | Router | Tool capsule |
| result | `tool.v1.execute.{tool_name}.result` | `ToolExecuteResult` | Tool capsule | Router |
| aggregated | `tool.v1.execute.result` | `ToolExecuteResult` | Router | Orchestrator |
| cancel | `tool.v1.request.cancel` | `ToolCancelRequest` | Orchestrator | Process tracker |

## Message schemas

### ToolExecuteRequest

```json
{
  "type": "tool_execute_request",
  "call_id": "string (unique per tool call)",
  "tool_name": "string (validated, no dots)",
  "arguments": { "JSON object matching tool's input schema" }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `call_id` | string | yes | Unique identifier for this invocation. Used to correlate the result. |
| `tool_name` | string | yes | Tool name. Must match `[a-zA-Z0-9_:-]`. No dots. |
| `arguments` | object | yes | JSON object matching the tool's declared input schema. |

### ToolExecuteResult

```json
{
  "type": "tool_execute_result",
  "call_id": "string (matches request)",
  "result": {
    "call_id": "string",
    "content": "string (tool output or error description)",
    "is_error": false
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `call_id` | string | yes | Must match the `call_id` from the originating request. |
| `result.call_id` | string | yes | Same as outer `call_id`. |
| `result.content` | string | yes | Tool output (success) or error description (failure). |
| `result.is_error` | boolean | yes | `true` if the tool failed. Defaults to `false`. |

### ToolCancelRequest

```json
{
  "type": "tool_cancel_request",
  "call_ids": ["string"]
}
```

Published by the orchestrator on turn cancellation (e.g. Ctrl+C). The kernel's
process tracker listens for this and sends SIGINT/SIGKILL to spawned children.

## Tool name validation

The router rejects tool names that are empty or contain characters outside
`[a-zA-Z0-9_:-]`. Dots are explicitly rejected because the router builds
forward topics via string interpolation (`tool.v1.execute.{tool_name}`). A dot
in the name would create unintended topic segments.

## Behavioral requirements

A conforming **router** implementation must:

1. Subscribe to `tool.v1.request.execute`.
2. Validate the tool name against `[a-zA-Z0-9_:-]`.
3. If invalid, publish a `ToolExecuteResult` with `is_error: true` to
   `tool.v1.execute.result`. Do not forward.
4. If valid, publish the request to `tool.v1.execute.{tool_name}`.
5. Subscribe to `tool.v1.execute.*.result` and forward all results to
   `tool.v1.execute.result`.
6. Never fabricate `call_id` values. Error results always use the `call_id`
   from the original request.

A conforming **tool capsule** must:

1. Subscribe to `tool.v1.execute.{tool_name}` via an interceptor.
2. Execute the tool using the provided `arguments`.
3. Publish a `ToolExecuteResult` to `tool.v1.execute.{tool_name}.result`.
4. Set `is_error: true` if execution fails, with a human-readable error in
   `content`.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Empty tool name | Router publishes error result, not forwarded |
| Invalid characters in tool name | Router publishes error result, not forwarded |
| Forward publish fails | Router publishes error result with publish error |
| Tool capsule returns error | `is_error: true` in result, forwarded normally |
| Tool capsule not subscribed | Request published to empty topic, no result (timeout at orchestrator) |

# Drawbacks
[drawbacks]: #drawbacks

- The router is an extra hop. Direct publish from orchestrator to tool capsule
  would be faster but would push validation into the orchestrator.
- No timeout at the router level. If a tool capsule never responds, the
  orchestrator must handle the timeout.
- Tool name validation is character-based, not registry-based. The router does
  not know which tools exist.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why a router instead of direct dispatch?** Centralizes validation. The
orchestrator does not need to know tool name rules or topic naming conventions.
Adding logging, rate limiting, or metrics to the router affects all tool calls
without touching any tool capsule.

**Why errors as values?** LLMs can reason about errors ("file not found, try a
different path"). Converting errors to out-of-band signals loses context. MCP
and OpenAI function calling both return errors in the content field.

**Why reject dots in tool names?** Topic injection.
`tool.v1.execute.foo.bar` creates a two-segment suffix. Capsules subscribing
to `tool.v1.execute.foo` would not receive it. The dot restriction is the
simplest prevention.

# Prior art
[prior-art]: #prior-art

- **MCP tool execution** (JSON-RPC `tools/call`): Request/response with
  structured content. No intermediate router.
- **OpenAI function calling**: Model emits function calls, runtime executes,
  result returned as tool message. Similar error-as-value contract.
- **Unix syscall dispatch**: Kernel validates syscall number, dispatches to
  handler. The router plays the same role for tool names.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should the router verify that a tool capsule is subscribed before forwarding,
  or is the current fire-and-forget approach sufficient?
- Should there be a standard timeout at the router level, or is this purely the
  orchestrator's responsibility?

# Future possibilities
[future-possibilities]: #future-possibilities

- Tool-level rate limiting at the router.
- Tool execution metrics (duration, error rate) collected at the router.
- Tool capability checking before forwarding (does the capsule have permission
  to access the requested resource?).
