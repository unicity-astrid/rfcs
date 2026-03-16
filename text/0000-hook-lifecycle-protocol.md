- Feature Name: `hook_lifecycle_protocol`
- Start Date: 2026-03-16
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

This RFC defines the mapping between kernel lifecycle events and user-space hook
invocations, the merge strategies that reconcile multiple hook responses into a
single action, the four handler types that execute hook logic, and the chaining
semantics that let each hook observe prior results. The hook bridge acts as the
interrupt dispatcher in Astrid's OS model: it receives structured events from the
kernel, fans them out to registered subscribers, merges their responses according
to per-event rules, and returns a single decision to the caller.

# Motivation
[motivation]: #motivation

Astrid's kernel emits lifecycle events at well-defined points: session boundaries,
tool calls, message flow, sub-agent spawn/completion, context compaction, and
kernel start/stop. Today these events exist but have no standardized hook surface.
Capsules and frontends cannot intercept, modify, or block operations at these
points without reaching into kernel internals.

A formal hook protocol solves three problems:

1. **Observability without coupling.** Audit capsules, logging frontends, and
   analytics pipelines can subscribe to lifecycle events without modifying the
   kernel or the capsule that triggered the event.

2. **Policy enforcement at the boundary.** Security capsules can block tool calls,
   redact message content, or inject approval gates by subscribing to the
   appropriate hook and returning a `Block` or `Ask` result. The kernel does not
   need to know about the policy; it only knows that a hook said "stop."

3. **Composable middleware.** Multiple hooks can chain on the same event. Each hook
   sees the previous hooks' results. The merge strategy defines how their responses
   combine. This gives capsule authors the same middleware composition pattern that
   Express.js and Webpack provide, but with explicit merge semantics instead of
   implicit ordering.

Without this RFC, every integration that needs to intercept a lifecycle event must
either fork the kernel or use ad-hoc IPC hacks. The hook bridge standardizes the
interception surface and makes it a first-class part of the contract.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## The hook bridge in the OS model

Think of the hook bridge as the interrupt dispatcher. When the kernel reaches a
lifecycle boundary (a tool is about to execute, a message is about to send, a
session starts), it fires a structured event. The hook bridge receives that event,
looks up all registered subscribers, calls each one in registration order, merges
their responses, and returns a single decision to the kernel.

The kernel never calls hook handlers directly. It calls one host function,
`astrid_trigger_hook`, and the hook bridge handles fan-out, error isolation, and
response merging.

## Registering a hook

A capsule or configuration file declares hook subscriptions. Each subscription
binds a lifecycle event to a handler, with optional matchers that filter which
invocations reach the handler.

```toml
[[hooks]]
event = "tool_call_started"
handler = { type = "command", command = "security-scan", args = ["--tool", "{{tool_name}}"] }
matcher = { type = "tool_names", names = ["shell_exec", "file_write"] }
on_fail = "block"
```

This subscription fires the `security-scan` command before every call to
`shell_exec` or `file_write`. If the command fails, the tool call is blocked.

## What happens when a hook fires

1. The kernel emits an event (e.g., `tool_call_started`).
2. The hook bridge maps the event to a hook name (`before_tool_call`).
3. The bridge filters subscribers through their matchers.
4. The bridge calls each matching handler in registration order.
5. Each handler receives a `HookContext` containing the event data and all
   previous hook results in the chain.
6. Each handler returns a `HookResult`.
7. The bridge merges all results using the event's merge strategy.
8. The bridge returns the merged result to the kernel.

## Merge strategies in plain terms

- **None**: The kernel does not care about responses. Fire-and-forget. Used for
  observation-only hooks like `session_start` and `session_end`.
- **ToolCallBefore**: Any subscriber that says "skip this tool call" wins. The
  last subscriber to modify parameters gets the final say on parameter values.
  This lets a security hook block while a parameter-rewriting hook adjusts args.
- **LastNonNull**: The last subscriber to provide a non-null value for a named
  field wins. Used when multiple hooks might transform a result or message, and
  the final transformation should prevail.

## Handler types

Four handler types execute hook logic:

- **Command**: Runs a shell command. The hook bridge passes event data as JSON on
  stdin and reads the `HookResult` from stdout.
- **Http**: Sends an HTTP request to an external endpoint. The response body is
  parsed as a `HookResult`.
- **Wasm**: Calls a function in a WASM module. The function receives serialized
  `HookContext` and returns serialized `HookResult`.
- **Agent**: Sends a prompt to an LLM and interprets the response as a
  `HookResult`. This handler type is stubbed for future implementation.

## Fail actions

If a handler crashes, times out, or returns invalid output, the fail action
determines what happens:

- **Warn** (default): Log a warning and treat the result as `Continue`.
- **Block**: Treat the failure as a `Block` result. The operation is stopped.
- **Ignore**: Silently treat the result as `Continue`.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Event-to-hook mapping table

The kernel emits lifecycle events. The hook bridge maps each event to a hook name
and a merge strategy. The table below is the authoritative mapping.

### Session events

| Lifecycle Event | Hook Name | Merge Strategy |
|----------------|-----------|----------------|
| `session_created` | `session_start` | None |
| `session_ended` | `session_end` | None |

### Tool events

| Lifecycle Event | Hook Name | Merge Strategy |
|----------------|-----------|----------------|
| `tool_call_started` | `before_tool_call` | ToolCallBefore |
| `tool_call_completed` | `after_tool_call` | LastNonNull on `modified_result` |
| `tool_result_persisting` | `tool_result_persist` | LastNonNull on `transformed_result` |

### Message events

| Lifecycle Event | Hook Name | Merge Strategy |
|----------------|-----------|----------------|
| `message_received` | `message_received` | None |
| `message_sending` | `message_sending` | LastNonNull on `modified_content` |
| `message_sent` | `message_sent` | None |

### Sub-agent events

| Lifecycle Event | Hook Name | Merge Strategy |
|----------------|-----------|----------------|
| `sub_agent_spawned` | `subagent_start` | None |
| `sub_agent_completed` | `subagent_stop` | None |
| `sub_agent_failed` | `subagent_stop` | None |
| `sub_agent_cancelled` | `subagent_stop` | None |

### Context and kernel events

| Lifecycle Event | Hook Name | Merge Strategy |
|----------------|-----------|----------------|
| `context_compaction_started` | `on_compaction_started` | None |
| `context_compaction_completed` | `on_compaction_completed` | None |
| `kernel_started` | `kernel_start` | None |
| `kernel_shutdown` | `kernel_stop` | None |

## Merge strategies

### None

Subscriber responses are discarded. The bridge returns `Continue` unconditionally.
Used for observation-only hooks where the kernel does not need feedback.

Pseudocode:

```
fn merge_none(results: Vec<HookResult>) -> HookResult {
    // results are ignored
    HookResult::Continue
}
```

### ToolCallBefore

Designed for the `before_tool_call` hook. Two fields are merged independently:

1. **`skip`**: If any subscriber returns `Block`, the merged result is `Block`.
   This is a logical OR: any single subscriber can veto a tool call.
2. **`modified_params`**: The last subscriber to return a non-null
   `modified_params` value wins. Earlier modifications are overwritten.

This separation lets a security hook block a dangerous tool call while a separate
parameter-rewriting hook adjusts arguments, without the two interfering.

Pseudocode:

```
fn merge_tool_call_before(results: Vec<HookResult>) -> HookResult {
    let mut blocked = false;
    let mut block_reason = None;
    let mut params = None;

    for result in results {
        match result {
            HookResult::Block { reason } => {
                blocked = true;
                block_reason = Some(reason);
            }
            HookResult::ContinueWith { modifications } => {
                if let Some(p) = modifications.get("modified_params") {
                    params = Some(p.clone());
                }
            }
            _ => {}
        }
    }

    if blocked {
        return HookResult::Block { reason: block_reason.unwrap_or_default() };
    }
    match params {
        Some(p) => HookResult::ContinueWith {
            modifications: [("modified_params".into(), p)].into(),
        },
        None => HookResult::Continue,
    }
}
```

### LastNonNull

Used for `after_tool_call` (field: `modified_result`), `tool_result_persist`
(field: `transformed_result`), and `message_sending` (field: `modified_content`).

The bridge iterates through results in order. For the designated field, the last
subscriber to return a non-null value for that field wins. All other fields in
`modifications` are ignored for merge purposes.

Pseudocode:

```
fn merge_last_non_null(results: Vec<HookResult>, field: &str) -> HookResult {
    let mut value = None;

    for result in results {
        match result {
            HookResult::ContinueWith { modifications } => {
                if let Some(v) = modifications.get(field) {
                    value = Some(v.clone());
                }
            }
            HookResult::Block { reason } => {
                return HookResult::Block { reason };
            }
            _ => {}
        }
    }

    match value {
        Some(v) => HookResult::ContinueWith {
            modifications: [(field.into(), v)].into(),
        },
        None => HookResult::Continue,
    }
}
```

Note: A `Block` result in a LastNonNull merge short-circuits and returns `Block`.
This preserves the invariant that any subscriber can halt an operation, even in
merge strategies primarily designed for value transformation.

## HookResult

Every hook handler returns one of four variants:

```rust
enum HookResult {
    /// No changes. Proceed as normal.
    Continue,

    /// Proceed with modifications to named fields.
    ContinueWith {
        modifications: HashMap<String, Value>,
    },

    /// Block the operation.
    Block {
        reason: String,
    },

    /// Pause and ask the user a question before proceeding.
    Ask {
        question: String,
        default: Option<String>,
    },
}
```

Serialized as JSON:

```json
{ "type": "continue" }
{ "type": "continue_with", "modifications": { "modified_result": "..." } }
{ "type": "block", "reason": "Tool shell_exec is not permitted in this context" }
{ "type": "ask", "question": "Allow file write to /etc/passwd?", "default": "deny" }
```

## HookContext

The hook bridge constructs a `HookContext` for each handler invocation:

```rust
struct HookContext {
    /// Unique ID for this hook invocation (all handlers in one fan-out share it).
    invocation_id: Uuid,

    /// The lifecycle event that triggered this hook.
    event: String,

    /// Active session ID, if any.
    session_id: Option<String>,

    /// Authenticated user ID, if any.
    user_id: Option<String>,

    /// Timestamp of the event (UTC, RFC 3339).
    timestamp: String,

    /// Event-specific payload. Contents vary by event type.
    data: HashMap<String, Value>,

    /// Results from previously executed handlers in this chain.
    previous_results: Vec<HookResult>,
}
```

The `data` field contains event-specific information. Examples:

- For `tool_call_started`: `{ "tool_name": "shell_exec", "params": { "command": "ls" }, "server_name": "local" }`
- For `message_sending`: `{ "role": "assistant", "content": "Here is the result..." }`
- For `sub_agent_spawned`: `{ "agent_id": "abc-123", "parent_id": "root", "capabilities": [...] }`
- For `kernel_started`: `{ "version": "0.3.0", "capsules_loaded": 5 }`

The `previous_results` field lets each handler see what earlier handlers returned.
This enables conditional logic: a hook can check whether a prior hook already
blocked the operation, or inspect modifications made by earlier hooks in the chain.

## Hook handlers

### Command

```rust
struct CommandHandler {
    /// The executable to run.
    command: String,

    /// Command-line arguments. Supports `{{variable}}` template substitution
    /// from HookContext.data fields.
    args: Vec<String>,

    /// Additional environment variables to set.
    env: HashMap<String, String>,

    /// Working directory for the command. Defaults to the kernel's working dir.
    working_dir: Option<PathBuf>,
}
```

Execution protocol:

1. The bridge serializes `HookContext` as JSON and writes it to the command's
   stdin.
2. The bridge reads stdout until EOF.
3. The bridge parses stdout as a JSON `HookResult`.
4. If the process exits with a non-zero code, the fail action applies.
5. If the process exceeds a configurable timeout (default: 30 seconds), the bridge
   kills it and the fail action applies.

Template substitution in `args` replaces `{{key}}` with the corresponding value
from `HookContext.data`. Missing keys are replaced with empty strings.

### Http

```rust
struct HttpHandler {
    /// The URL to send the request to.
    url: String,

    /// HTTP method. Defaults to POST.
    method: Option<String>,

    /// Additional headers.
    headers: HashMap<String, String>,

    /// Request body template. Supports `{{variable}}` substitution.
    /// If omitted, the full HookContext is sent as the JSON body.
    body_template: Option<String>,
}
```

Execution protocol:

1. The bridge sends the HTTP request with the `HookContext` as the JSON body
   (or the rendered `body_template`).
2. The `Content-Type` header is set to `application/json` unless overridden.
3. The bridge reads the response body and parses it as a JSON `HookResult`.
4. Non-2xx status codes trigger the fail action.
5. Timeout is configurable (default: 30 seconds).

### Wasm

```rust
struct WasmHandler {
    /// Path to the WASM module.
    module_path: PathBuf,

    /// The exported function to call.
    function: String,
}
```

Execution protocol:

1. The bridge loads the WASM module (cached after first load).
2. The bridge calls the exported function with the serialized `HookContext` as
   input.
3. The function returns serialized `HookResult`.
4. If the function traps, the fail action applies.
5. The WASM module runs under the same sandbox constraints as capsules: memory
   limits, no network access, no filesystem access beyond its VFS mount.

### Agent (stubbed)

```rust
struct AgentHandler {
    /// Prompt template. Supports `{{variable}}` substitution from HookContext.data.
    prompt_template: String,

    /// Model to use.
    model: String,

    /// Maximum tokens for the response.
    max_tokens: u32,
}
```

This handler type is reserved for future implementation. The bridge rejects Agent
handler registrations with an "unsupported handler type" error until the
implementation lands. The type is defined now to reserve the configuration shape
and prevent breaking changes when it ships.

## Hook matchers

Matchers filter which hook invocations reach a handler. If no matcher is
specified, the handler receives all invocations of its subscribed hook.

### Glob

```rust
struct GlobMatcher {
    /// Glob pattern matched against the tool name or event-specific identifier.
    pattern: String,
}
```

Example: `{ "type": "glob", "pattern": "file_*" }` matches `file_read`,
`file_write`, `file_delete`.

### Regex

```rust
struct RegexMatcher {
    /// Regular expression matched against the tool name or event-specific identifier.
    pattern: String,
}
```

The regex is compiled once at registration time. Invalid patterns cause a
registration error. The regex is matched against the full string (anchored).

### ToolNames

```rust
struct ToolNamesMatcher {
    /// Exact tool names to match.
    names: Vec<String>,
}
```

Matches if the tool name in the event data is in the list. Only meaningful for
tool-related hooks. For non-tool hooks, this matcher rejects all invocations.

### ServerNames

```rust
struct ServerNamesMatcher {
    /// Exact MCP server names to match.
    names: Vec<String>,
}
```

Matches if the originating MCP server name is in the list. Useful for scoping
hooks to specific tool servers.

## Fail actions

```rust
enum FailAction {
    /// Log a warning and return HookResult::Continue. Default.
    Warn,

    /// Return HookResult::Block with the error message as the reason.
    Block,

    /// Silently return HookResult::Continue.
    Ignore,
}
```

The fail action applies when:

- A command handler exits with a non-zero code or times out.
- An HTTP handler receives a non-2xx response or times out.
- A WASM handler traps.
- Any handler returns output that cannot be parsed as a `HookResult`.

## Fan-out and error isolation

The hook bridge executes handlers sequentially in registration order. Each handler
invocation is wrapped in `catch_unwind` (for WASM and in-process handlers) or
process-level isolation (for command handlers) to prevent a failing hook from
crashing the kernel.

If a handler panics or fails:

1. The fail action determines the synthetic `HookResult`.
2. The synthetic result is appended to `previous_results` for subsequent handlers.
3. Execution continues with the next handler.

This guarantees that a misbehaving hook cannot prevent other hooks from running,
and cannot crash the kernel.

## Hook chaining semantics

Handlers execute in registration order. Each handler receives the full
`previous_results` vector, which contains the `HookResult` from every handler
that ran before it in this invocation.

This enables:

- **Conditional short-circuit**: A handler can check if a prior handler already
  blocked the operation and skip expensive work.
- **Incremental transformation**: A handler can read the modifications from a
  prior handler and apply further changes on top.
- **Audit trails**: A logging handler can see exactly what every prior handler
  decided.

The merge strategy runs after all handlers complete. It operates on the full
results vector, not on intermediate states.

## Host function: `astrid_trigger_hook`

The kernel triggers hooks through a single host function:

```
astrid_trigger_hook(event: &str, data: &[u8]) -> Result<Vec<u8>, HookError>
```

- `event`: The lifecycle event name (e.g., `tool_call_started`).
- `data`: Serialized event-specific payload (JSON bytes).
- Returns: Serialized merged `HookResult` (JSON bytes).

The host function is synchronous from the caller's perspective. The hook bridge
handles all fan-out internally.

## Configuration schema

Hook subscriptions are declared in the kernel configuration or in capsule
manifests:

```toml
[[hooks]]
event = "tool_call_started"
handler = { type = "command", command = "audit-log", args = ["--event", "{{tool_name}}"] }
on_fail = "warn"

[[hooks]]
event = "tool_call_started"
handler = { type = "wasm", module_path = "hooks/security.wasm", function = "check_tool" }
matcher = { type = "tool_names", names = ["shell_exec", "file_write", "http_request"] }
on_fail = "block"

[[hooks]]
event = "message_sending"
handler = { type = "http", url = "https://redaction.internal/scan", method = "POST" }
on_fail = "warn"
```

Multiple subscriptions to the same event are ordered by their position in the
configuration. First declared, first executed.

# Drawbacks
[drawbacks]: #drawbacks

- **Sequential execution adds latency.** Every hook in a chain runs before the
  kernel can proceed. A slow HTTP hook on `before_tool_call` delays every tool
  invocation it matches. Mitigation: timeouts, matchers to narrow scope, and
  the `Ignore` fail action for non-critical hooks.

- **Merge strategies are fixed per event.** If a future use case needs a different
  merge behavior for `before_tool_call`, it requires a new RFC. This is
  intentional: fixed merge rules are easier to reason about than configurable ones,
  but it trades flexibility for predictability.

- **Handler ordering is implicit.** Registration order determines execution order,
  which is configuration-file order. This is simple but fragile: reordering lines
  in a config file changes hook behavior. Explicit priority fields were considered
  and rejected (see Rationale).

- **The Agent handler is stubbed.** Defining the type now reserves the shape, but
  shipping an unimplemented variant creates a partial API. This is acceptable
  because the alternative (adding Agent later) would be a breaking schema change.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why a mapping table instead of direct event subscription?**

The indirection between lifecycle events and hook names decouples the kernel's
internal event naming from the user-facing hook API. The kernel can rename
`tool_call_started` to `tool_invocation_begin` internally without breaking any
hook configuration, because the hook name `before_tool_call` stays stable.

**Why fixed merge strategies instead of configurable ones?**

Configurable merge strategies sound flexible but create a combinatorial testing
burden. Every event-strategy pair would need its own test matrix. Fixed strategies
mean the merge behavior of every hook is documented in one table and cannot be
misconfigured.

**Why registration order instead of explicit priority?**

Priority numbers create ordering conflicts when two hooks declare the same
priority. Tie-breaking rules add complexity. Registration order is simple,
deterministic, and matches how middleware stacks work in Express.js and similar
frameworks. If you want a hook to run first, put it first in the config.

**Why sequential execution instead of parallel fan-out?**

Parallel execution would be faster but breaks hook chaining. A handler that needs
to see the previous handler's result cannot run concurrently with it. Sequential
execution preserves the `previous_results` guarantee. Future work could introduce
a "parallel-safe" annotation for handlers that do not read `previous_results`.

**Why `catch_unwind` instead of process isolation for all handlers?**

Process isolation (spawning a child process per handler) provides the strongest
isolation but adds 10-50ms of overhead per invocation. WASM handlers already run
in a sandbox. `catch_unwind` prevents panics from propagating while keeping
in-process handlers fast. Command handlers already run in separate processes.

**Alternative: Event bus with topic subscriptions.** This was considered. IPC
topics work well for decoupled observation but poorly for request-response
patterns like "should this tool call proceed?" The hook bridge needs synchronous
responses with merge semantics, which a pub-sub bus does not naturally provide.

**Alternative: Aspect-oriented interception.** Interceptors that wrap kernel
functions at compile time. This couples hooks to kernel internals and prevents
runtime configuration. Rejected.

# Prior art
[prior-art]: #prior-art

- **Git hooks**: Named scripts (`pre-commit`, `post-receive`) that fire at
  lifecycle boundaries. Single handler per hook, no chaining, no merge semantics.
  Astrid's model extends this to multiple handlers with explicit merge rules.

- **Webpack plugin system**: Tapable hooks with `SyncHook`, `SyncBailHook`,
  `SyncWaterfallHook`, `AsyncSeriesHook`. The `SyncBailHook` (first non-undefined
  return wins) and `SyncWaterfallHook` (each plugin transforms the value from the
  previous plugin) inspired the merge strategy design. Astrid's `ToolCallBefore`
  is closest to a bail hook (any Block wins), and `LastNonNull` is a simplified
  waterfall.

- **Express.js middleware**: Sequential execution with `next()` to continue the
  chain. Any middleware can short-circuit by sending a response without calling
  `next()`. Astrid's `Block` result serves the same purpose as not calling
  `next()`.

- **Linux netfilter hooks**: Five hook points in the packet path
  (`NF_INET_PRE_ROUTING`, etc.) with registered callback chains. Each callback
  returns `NF_ACCEPT`, `NF_DROP`, or `NF_QUEUE`. Astrid's `Continue`/`Block`/`Ask`
  map to `NF_ACCEPT`/`NF_DROP`/`NF_QUEUE`.

- **Claude Code hooks (Anthropic)**: Command-based hooks with matchers and
  `before_tool_call`/`after_tool_call` events. The hook name conventions and
  command handler protocol in this RFC draw directly from Claude Code's model.
  This RFC extends it with additional handler types, merge strategies, and hook
  chaining.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Timeout defaults**: The 30-second default for command and HTTP handlers may be
  too generous for latency-sensitive hooks like `before_tool_call`. Should there be
  per-event-type timeout defaults?

- **`Ask` result handling**: The `Ask` variant pauses execution and prompts the
  user. The exact mechanism for routing the question to the correct frontend and
  resuming after the user responds is not specified here. It depends on the
  approval gate system, which may warrant its own RFC.

- **Hook registration at runtime**: This RFC defines static configuration. Should
  capsules be able to register and deregister hooks at runtime via a host function?
  This has security implications (a compromised capsule could deregister security
  hooks) and needs careful capability scoping.

- **Ordering across configuration sources**: If hooks are declared in both the
  kernel config and a capsule manifest, what is the relative ordering? Kernel-first
  is the likely answer but needs explicit specification.

- **Payload schema versioning**: The `data` field in `HookContext` varies by event
  type. Should each event type have a versioned schema, or is the untyped
  `HashMap<String, Value>` sufficient for forward compatibility?

# Future possibilities
[future-possibilities]: #future-possibilities

- **Parallel-safe handlers**: An annotation that marks a handler as safe to run
  concurrently with other parallel-safe handlers. The bridge could fan out
  parallel-safe handlers simultaneously and only serialize when a
  chain-dependent handler appears.

- **Hook metrics**: Built-in latency and success-rate tracking per handler.
  Exposed via the audit system and surfaced in frontends.

- **Agent handler implementation**: The stubbed Agent handler could dispatch to
  an LLM to make policy decisions. Example: "Should this user be allowed to
  execute shell commands?" with the LLM evaluating context and returning
  Block/Continue.

- **Hook marketplace**: A registry of community-contributed hook handlers (WASM
  modules, HTTP endpoints) that capsule authors can reference by name instead of
  bundling.

- **Conditional chaining**: A handler could return a `Skip` variant that removes
  itself from the chain for the remainder of the session, reducing overhead for
  one-time initialization hooks.

- **Event batching**: For high-frequency events (message tokens in a stream),
  the bridge could batch multiple events into a single handler invocation to
  reduce per-call overhead.

- **Hook debugging**: A `--hook-trace` flag that logs every handler invocation,
  its input, its output, and the merge result. Essential for diagnosing why a
  tool call was blocked or a message was modified.
