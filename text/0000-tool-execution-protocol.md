- Feature Name: `tool_execution_protocol`
- Start Date: 2026-03-16
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

This RFC defines the IPC contract for tool execution routing between the
orchestrator's react loop, the tool router, and tool capsules. It specifies
four topic patterns (`tool.request.execute`, `tool.v1.execute.{tool_name}`,
`tool.v1.execute.result`, `tool.v1.cancel`), their payload schemas, the
router's forwarding logic, tool name validation rules, and the error handling
contract. The router acts as a stateless syscall dispatcher; tool capsules are
user-space utilities that subscribe to a single topic and publish results back
through a single return topic.

# Motivation
[motivation]: #motivation

The react loop (orchestrator) needs to invoke tools during an agent's
reasoning cycle. Tools live in isolated WASM capsules. The orchestrator cannot
call capsules directly - it must go through the IPC event bus. Without a
standardized protocol:

- Every tool capsule invents its own request/response format.
- The orchestrator needs per-tool knowledge of topic names and payload shapes.
- There is no consistent way to correlate a result with the request that
  triggered it.
- Cancellation requires per-tool cancellation logic instead of a single
  broadcast.
- Tool name validation happens nowhere, leaving the bus open to topic
  injection attacks.

A standard tool execution protocol gives the ecosystem a single, auditable
contract. Any capsule that conforms to this RFC can be invoked by any
orchestrator that conforms to it. The router in the middle is pure middleware:
it validates, forwards, and routes - nothing else.

This is the plumbing that makes the tool ecosystem composable. Without it,
every capsule is a snowflake. With it, capsules are interchangeable.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## The three actors

1. **React loop** (orchestrator) - the agent's reasoning engine. It decides
   which tool to call and with what arguments. It publishes a request and
   waits for a result.
2. **Router** - stateless middleware. It receives requests from the react loop,
   validates tool names, forwards valid requests to the right capsule, and
   routes results back.
3. **Tool capsule** - an isolated WASM process that implements one tool. It
   subscribes to its own topic, does the work, and publishes a result.

## Lifecycle of a tool call

```
React Loop              Router                      Tool Capsule (fs_read)
    |                      |                              |
    |-- tool.request.execute -->|                         |
    |   {call_id, tool_name,   |                         |
    |    arguments}             |                         |
    |                      |-- tool.v1.execute.fs_read -->|
    |                      |   {call_id, tool_name,       |
    |                      |    arguments}                |
    |                      |                              |
    |                      |<-- tool.v1.execute.result ---|
    |                      |   {call_id, result}          |
    |<-- tool.v1.execute.result ---|                      |
    |   {call_id, result}  |                              |
```

The react loop publishes to `tool.request.execute`. The router picks it up,
validates the tool name, and re-publishes to `tool.v1.execute.fs_read`. The
capsule does its work and publishes to `tool.v1.execute.result`. The react
loop receives the result, correlates it by `call_id`, and continues reasoning.

## Writing a conforming tool capsule

A tool capsule does three things:

1. **Subscribe** to `tool.v1.execute.{your_tool_name}` at startup.
2. **Process** incoming `ToolExecuteRequest` messages.
3. **Publish** a `ToolExecuteResult` to `tool.v1.execute.result` for every
   request received.

That is the entire contract. The capsule does not need to know about the
router, the react loop, or any other capsule.

### Example: a `hello_world` tool capsule

```rust
// Subscribe to your topic
ipc_subscribe("tool.v1.execute.hello_world")?;

// Process requests
loop {
    let envelope = ipc_recv()?;
    let request: ToolExecuteRequest = serde_json::from_value(envelope.payload)?;

    let greeting = format!("Hello, {}!", request.arguments["name"]);

    let result = ToolExecuteResult {
        call_id: request.call_id,
        result: ToolCallResult {
            call_id: request.call_id.clone(),
            content: greeting,
            is_error: false,
        },
    };

    ipc_publish("tool.v1.execute.result", &result)?;
}
```

### Reporting errors

Errors are results, not exceptions. If your tool fails, publish a result with
`is_error: true` and a human-readable error description in `content`. The LLM
will see this error and can reason about it - retry, try a different tool, or
explain the failure to the user.

```rust
let result = ToolExecuteResult {
    call_id: request.call_id,
    result: ToolCallResult {
        call_id: request.call_id.clone(),
        content: "Permission denied: /etc/shadow is not readable".to_string(),
        is_error: true,
    },
};
```

## Cancellation

The react loop can cancel in-flight tool calls by publishing to
`tool.v1.cancel` with a list of `call_id` values. Tool capsules that support
cancellation subscribe to this topic and abort any matching in-progress work.
Cancellation is best-effort - the capsule may have already completed the work
and published a result.

## Tool name rules

Tool names must be non-empty and consist of alphanumeric characters, hyphens,
underscores, or colons. Dots are rejected. This is not arbitrary. The router
builds a topic by concatenating `tool.v1.execute.` with the tool name. If the
tool name contained dots, it would create sub-topics that collide with the
protocol's own topic hierarchy. A tool named `foo.bar` would route to
`tool.v1.execute.foo.bar`, which is indistinguishable from a protocol-level
topic. Rejecting dots prevents this injection.

Valid names: `fs_read`, `web-search`, `mcp:github:list_repos`, `my_tool_v2`

Invalid names: `fs.read` (dot), `` (empty), `foo bar` (space)

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Topic | Publisher | Subscriber | Purpose |
|-------|-----------|------------|---------|
| `tool.request.execute` | React loop | Router | Request tool execution |
| `tool.v1.execute.{tool_name}` | Router | Tool capsule | Forward request to specific tool |
| `tool.v1.execute.result` | Tool capsule | React loop, Router | Return execution result |
| `tool.v1.cancel` | React loop | Tool capsules | Cancel in-flight tool calls |

The `v1` segment is a protocol version marker. Future breaking changes to
payload schemas will use `v2`, `v3`, etc. The `tool.request.execute` topic
intentionally omits a version - it is the stable entry point that the router
consumes and translates into the versioned internal protocol.

## Payload schemas

### ToolExecuteRequest

Published on `tool.request.execute` and forwarded on
`tool.v1.execute.{tool_name}`.

```json
{
  "call_id": "string",
  "tool_name": "string",
  "arguments": {}
}
```

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `call_id` | String | Yes | Unique per tool call. Opaque to the router and capsule. The react loop generates it. |
| `tool_name` | String | Yes | Non-empty. Matches regex `^[a-zA-Z0-9_:-]+$`. No dots. |
| `arguments` | JSON Object | Yes | Arbitrary JSON object matching the tool's input schema. The router does not inspect or validate this field. |

### ToolExecuteResult

Published on `tool.v1.execute.result`.

```json
{
  "call_id": "string",
  "result": {
    "call_id": "string",
    "content": "string",
    "is_error": false
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `call_id` | String | Yes | Matches the `call_id` from the originating `ToolExecuteRequest`. |
| `result` | ToolCallResult | Yes | The execution outcome. |
| `result.call_id` | String | Yes | Same as the outer `call_id`. Duplicated for compatibility with existing `ToolCallResult` consumers. |
| `result.content` | String | Yes | The tool's output on success, or a human-readable error description on failure. |
| `result.is_error` | bool | Yes | `false` for successful execution, `true` for errors. |

### ToolCancelRequest

Published on `tool.v1.cancel`.

```json
{
  "call_ids": ["string"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `call_ids` | Vec\<String\> | Yes | List of `call_id` values to cancel. Must contain at least one entry. |

## Tool name validation

The router validates tool names against the regex `^[a-zA-Z0-9_:-]+$`. This
allows:

- Alphanumeric characters (`a-z`, `A-Z`, `0-9`)
- Hyphens (`-`)
- Underscores (`_`)
- Colons (`:`) - used by MCP-bridged tools (e.g., `mcp:github:list_repos`)

Dots are explicitly rejected. The rejection is a security boundary: the topic
`tool.v1.execute.{tool_name}` must map to exactly one topic segment after the
`execute.` prefix. A dot in the tool name would split it into multiple
segments, creating a topic injection vector. For example, a tool named
`result` would route to `tool.v1.execute.result`, hijacking the result topic.
This regex prevents that.

Validation failures produce an immediate error result (see Router Behavior
below). The invalid request never reaches the bus.

## Router behavior

The router is a stateless, pure-middleware component. It holds no request
state, maintains no correlation tables, and performs no retries. Its behavior
is fully deterministic given an input message.

### On receiving `tool.request.execute`:

1. Deserialize the payload as `ToolExecuteRequest`. If deserialization fails,
   drop the message (malformed input is not routable - there is no `call_id`
   to correlate an error response).
2. Validate `tool_name` against `^[a-zA-Z0-9_:-]+$`.
   - On failure: publish a `ToolExecuteResult` to `tool.v1.execute.result`
     with `is_error: true` and content describing the validation failure.
     Return.
3. Construct the target topic: `format!("tool.v1.execute.{}", request.tool_name)`.
4. Publish the `ToolExecuteRequest` to the target topic.
   - On publish failure: publish a `ToolExecuteResult` to
     `tool.v1.execute.result` with `is_error: true` and content describing
     the publish failure. Return.

### On receiving `tool.v1.execute.result`:

The router does not intercept result messages. The react loop subscribes to
`tool.v1.execute.result` directly and consumes results. The router may
optionally subscribe for audit/logging purposes but must not modify or filter
result messages.

### On receiving `tool.v1.cancel`:

The router does not intercept cancel messages. Cancellation is a direct
broadcast from the react loop to all tool capsules. Capsules that support
cancellation subscribe to `tool.v1.cancel` and check whether any of their
in-flight `call_id` values appear in the `call_ids` list.

## Tool capsule contract

A conforming tool capsule:

1. Subscribes to exactly one topic: `tool.v1.execute.{its_tool_name}`.
2. Optionally subscribes to `tool.v1.cancel` if it supports cancellation.
3. For every `ToolExecuteRequest` received, publishes exactly one
   `ToolExecuteResult` to `tool.v1.execute.result`.
4. Never publishes to `tool.request.execute` or `tool.v1.execute.{other_tool}`.
5. Treats the `arguments` field as the sole input. Does not depend on envelope
   metadata for business logic.

### One request, one result

Every request produces exactly one result. This invariant is critical for the
react loop's correlation logic. If a capsule fails to publish a result, the
react loop will wait indefinitely (or until a timeout, which is outside this
RFC's scope). If a capsule publishes multiple results for the same `call_id`,
the react loop's behavior is undefined.

### Concurrency

A capsule may receive multiple requests concurrently (the IPC bus does not
serialize delivery). Capsules that cannot handle concurrent execution must
serialize internally. The protocol does not impose ordering guarantees on
delivery or result publication.

### Result timing

There is no protocol-level timeout. The react loop may implement its own
timeout and publish a cancellation, but the protocol itself does not enforce
deadlines. A capsule may take arbitrarily long to produce a result.

## Error handling contract

Errors are values, not out-of-band signals. Every error condition produces a
`ToolExecuteResult` with `is_error: true`. This design exists for one reason:
the LLM must be able to see and reason about errors. A silent failure or an
out-of-band exception is invisible to the model. An error result in `content`
is just another piece of text the model can incorporate into its reasoning.

Error result sources:

| Source | Condition | `content` value |
|--------|-----------|-----------------|
| Router | Invalid tool name | `"Invalid tool name '{name}': must match [a-zA-Z0-9_:-]+"` |
| Router | Publish failure | `"Failed to route tool call to '{name}': {reason}"` |
| Capsule | Tool-specific failure | Tool-defined error description |

The router never fabricates a `call_id`. If it cannot extract a `call_id` from
a malformed request, the message is dropped silently. This prevents the router
from publishing error results with incorrect correlation IDs.

## Rust type definitions

These types belong in `astrid-sdk` behind the `rfc-2` feature flag.

```rust
/// A request to execute a tool.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolExecuteRequest {
    /// Unique identifier for this tool call.
    pub call_id: String,
    /// The tool to invoke. Must match `^[a-zA-Z0-9_:-]+$`.
    pub tool_name: String,
    /// Arguments to pass to the tool. JSON object matching the tool's input schema.
    pub arguments: serde_json::Value,
}

/// The result of a tool execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolExecuteResult {
    /// Correlation ID matching the originating request.
    pub call_id: String,
    /// The execution outcome.
    pub result: ToolCallResult,
}

/// The outcome of a single tool call.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCallResult {
    /// Correlation ID (duplicated from ToolExecuteResult for standalone use).
    pub call_id: String,
    /// Tool output on success, or error description on failure.
    pub content: String,
    /// Whether this result represents an error.
    pub is_error: bool,
}

/// A request to cancel in-flight tool calls.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ToolCancelRequest {
    /// The call IDs to cancel.
    pub call_ids: Vec<String>,
}

/// Validates a tool name against the allowed character set.
pub fn validate_tool_name(name: &str) -> Result<(), String> {
    if name.is_empty() {
        return Err("Tool name must not be empty".to_string());
    }
    let valid = name.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == ':');
    if !valid {
        return Err(format!(
            "Invalid tool name '{}': must match [a-zA-Z0-9_:-]+",
            name
        ));
    }
    Ok(())
}
```

## Topic constants

```rust
/// The topic the react loop publishes tool execution requests to.
pub const TOPIC_TOOL_REQUEST_EXECUTE: &str = "tool.request.execute";

/// Constructs the topic for a specific tool capsule.
/// Panics if tool_name is invalid (caller must validate first).
pub fn topic_tool_execute(tool_name: &str) -> String {
    format!("tool.v1.execute.{}", tool_name)
}

/// The topic tool capsules publish results to.
pub const TOPIC_TOOL_EXECUTE_RESULT: &str = "tool.v1.execute.result";

/// The topic for cancellation broadcasts.
pub const TOPIC_TOOL_CANCEL: &str = "tool.v1.cancel";
```

# Drawbacks
[drawbacks]: #drawbacks

- **Extra hop through the router.** Every tool call traverses the bus twice
  (react loop to router, router to capsule) instead of once. This adds
  latency. In practice, the added latency is sub-millisecond for in-process
  pub/sub and negligible compared to the tool's execution time or the LLM's
  inference time.

- **Router is a single point of failure.** If the router crashes, no tool calls
  route. Mitigation: the router is stateless, so it can be restarted without
  losing state. A supervision tree can restart it automatically.

- **No streaming results.** This protocol models tools as request/response.
  Tools that produce incremental output (e.g., long-running searches) must
  buffer internally and return a single result. A future RFC could add a
  streaming variant.

- **Duplicate `call_id` in nested `ToolCallResult`.** The `call_id` appears in
  both `ToolExecuteResult` and the inner `ToolCallResult`. This redundancy
  exists for backward compatibility with consumers that work with
  `ToolCallResult` directly. It is a pragmatic trade-off.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why a router instead of direct publish?

The react loop could publish directly to `tool.v1.execute.{tool_name}`,
bypassing the router entirely. This was rejected for three reasons:

1. **Validation centralization.** Tool name validation happens in one place.
   Without the router, every react loop implementation must independently
   validate tool names, and a bug in any one of them is a topic injection
   vulnerability.

2. **Audit surface.** The router is a natural point for logging every tool
   invocation. A single subscriber on `tool.request.execute` sees all tool
   traffic.

3. **Future extension.** The router can be extended to enforce rate limits,
   capability checks, or routing policies without changing the react loop or
   capsule contracts.

## Why errors as results, not a separate error topic?

An alternative design uses `tool.v1.execute.error` for failures. This was
rejected because:

- The LLM needs to see errors in the same stream as successes. A separate
  topic means the react loop must merge two streams and present them uniformly.
- The `is_error` flag is simpler and keeps correlation trivial: one `call_id`,
  one result, always on the same topic.

## Why reject dots in tool names?

An alternative allows dots and uses a different encoding for the topic (e.g.,
replacing dots with double underscores). This was rejected because encoding
adds complexity, requires decoding on the capsule side, and is error-prone.
Rejecting dots is the simplest rule that prevents topic injection.

## Why `tool.request.execute` instead of `tool.v1.request.execute`?

The entry-point topic intentionally omits the version segment. The router is
the version boundary. A future `v2` router can subscribe to the same
`tool.request.execute` topic and translate requests into `tool.v2.execute.*`
topics. This keeps the react loop decoupled from the internal protocol
version.

## What is the impact of not standardizing this?

Without this RFC, each tool capsule defines its own invocation protocol. The
orchestrator accumulates per-tool routing logic. Adding a new tool requires
changes to the orchestrator. The ecosystem cannot grow independently.

# Prior art
[prior-art]: #prior-art

- **Model Context Protocol (MCP)** - JSON-RPC based tool execution with
  `tools/call` method. MCP uses a direct client-server model; this RFC uses
  pub/sub through a router. MCP's `CallToolResult` with `isError` field
  directly inspired the `is_error` pattern in `ToolCallResult`.

- **OpenAI function calling** - The model emits `tool_calls` with a unique
  `id`, and the client returns `tool` messages with a matching `tool_call_id`.
  The correlation-by-ID pattern is identical to this RFC's `call_id`.

- **LangChain tool interface** - Tools implement `_run(tool_input)` and return
  strings. Errors raise `ToolException`. LangChain's approach of surfacing
  errors as text to the LLM (via `handle_tool_error`) validates the "errors
  are values" design.

- **UNIX syscall dispatch** - The kernel's syscall table maps syscall numbers
  to handler functions. The router plays the same role: it maps tool names to
  capsule topics. Tool capsules are user-space utilities invoked through a
  stable ABI. This analogy is deliberate and carries through the OS model
  that Astrid follows.

- **NATS request/reply** - NATS uses inbox topics for request/reply
  correlation. This RFC uses a shared result topic with `call_id` correlation
  instead of per-request inbox topics. The shared topic is simpler for the
  broadcast bus model and avoids per-call topic creation overhead.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Timeouts.** Should the protocol define a timeout field in
  `ToolExecuteRequest`, or is timeout management purely the react loop's
  responsibility? The current design leaves timeouts to the caller.

- **Result delivery guarantees.** If the react loop is not subscribed to
  `tool.v1.execute.result` when a capsule publishes, the result is lost.
  Should the bus provide at-least-once delivery, or is fire-and-forget
  acceptable? This depends on the bus implementation (broadcast channel
  semantics) and is deferred.

- **Batched tool calls.** The current protocol sends one request per tool
  call. Should there be a batch variant that sends multiple requests in one
  message? This adds complexity for marginal latency savings.

- **call_id format.** The RFC treats `call_id` as an opaque string. Should it
  mandate a format (UUID v7, ULID, etc.) for sortability and uniqueness
  guarantees?

- **Schema validation.** Should the router validate `arguments` against the
  tool's declared schema, or is this purely the capsule's responsibility?
  The current design says the router does not inspect `arguments`.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Streaming tool results.** A `tool.v1.execute.stream` topic for tools that
  produce incremental output. Each stream message carries a `call_id` and a
  sequence number. A final message with `is_final: true` terminates the
  stream.

- **Tool discovery.** A `tool.v1.discover` topic where capsules advertise
  their name, description, and input schema. The react loop (or a registry
  capsule) aggregates these into a tool catalog that the LLM can query.

- **Capability-gated routing.** The router checks whether the requesting agent
  holds a capability token that authorizes the target tool. Unauthorized calls
  get an error result without reaching the capsule.

- **Priority and QoS.** A `priority` field in `ToolExecuteRequest` that the
  router uses to order delivery. Low-priority tool calls yield to
  high-priority ones when the bus is congested.

- **Router-level retries.** The router detects capsule liveness (via heartbeat
  topics) and retries failed deliveries to a backup capsule instance.

- **Multi-result tools.** Extend the protocol to allow tools that return
  multiple results (e.g., a search tool that streams results as they arrive).
  This overlaps with the streaming possibility but with discrete result
  messages rather than a continuous stream.
