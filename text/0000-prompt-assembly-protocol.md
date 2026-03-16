- Feature Name: `prompt_assembly_protocol`
- Start Date: 2026-03-16
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

This RFC defines the IPC contract for prompt assembly in Astrid. The prompt
builder capsule acts as a linker: it collects contributions from plugin capsules
via a hook system, merges them according to deterministic ordering and permission
rules, and produces the final prompt sent to the LLM provider. The protocol
covers the assembly request, before-build and after-build hooks, hook response
fields, permission gating for system prompt injection, and the contracts for two
first-party capsules (identity and memory) that participate in assembly.

# Motivation
[motivation]: #motivation

An agent runtime must assemble a prompt from many sources before every LLM call.
The system prompt, user messages, workspace context, memory, tool usage
guidelines, and plugin-injected context all converge into a single payload. Today
there is no standard contract for how these contributions arrive, how they merge,
or how untrusted capsules are prevented from injecting into the system prompt.

Without a protocol:

- Every capsule that needs to contribute context invents its own ad-hoc channel.
  There is no ordering guarantee, no timeout contract, and no central merge
  point.
- Untrusted third-party capsules can inject arbitrary text into the system prompt.
  This is a prompt injection vector with no permission gate.
- The identity resolution logic (reading workspace config, platform context, tool
  guidelines) is coupled to the prompt builder implementation instead of living in
  its own composable capsule.
- Memory injection has no size cap, no truncation contract, and no defined
  position in the final prompt.

This RFC standardizes the assembly pipeline so that:

1. Plugin capsules have a stable IPC contract for contributing to prompts.
2. System prompt injection requires an explicit capability grant.
3. The identity and memory capsules have defined, auditable contracts.
4. Assembly is deterministic, ordered, and bounded by timeout.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## The prompt builder as a linker

Think of the prompt builder as a linker in a compilation toolchain. Multiple
object files (capsule contributions) arrive with named sections. The linker
merges them into one executable (the final prompt) according to fixed rules. Each
capsule contributes to well-defined sections, and the linker controls the final
layout.

In the OS model, the prompt builder is `/usr/bin/ld`. The identity capsule is
`/etc/profile` - it reads workspace configuration and injects environment context
on every shell session. The memory capsule is `~/.bashrc` - it loads persistent
state from disk.

## Assembly lifecycle

A typical assembly cycle proceeds as follows:

1. **Trigger.** A frontend or orchestrator publishes an assemble request on
   `prompt_builder.v1.assemble` containing the current conversation messages,
   base system prompt, target model, and provider.

2. **Fan-out.** The prompt builder publishes a before-build hook on
   `prompt_builder.v1.hook.before_build` to all subscribed plugin capsules. Each
   plugin receives the current messages, system prompt, model, and provider.

3. **Collect.** Plugins respond with hook contributions. Each response can
   include one or more contribution fields: `prependContext`, `systemPrompt`,
   `prependSystemContext`, or `appendSystemContext`. The prompt builder collects
   responses until either all plugins have responded, the timeout expires, or 50
   responses have arrived.

4. **Gate.** The prompt builder checks each responding capsule's capabilities.
   Capsules without the `allow_prompt_injection` capability have their
   `systemPrompt`, `prependSystemContext`, and `appendSystemContext` fields
   stripped. Only `prependContext` (user-visible) survives the gate.

5. **Merge.** Contributions merge in deterministic order:
   - `prependContext` values concatenate in response arrival order, forming a
     user-visible context prefix above the conversation.
   - `systemPrompt` uses last-writer-wins: the last non-null value from a
     permissioned capsule replaces the base system prompt entirely.
   - `prependSystemContext` values concatenate in order and prepend to the
     (possibly overridden) system prompt.
   - `appendSystemContext` values concatenate in order and append to the system
     prompt.

6. **Emit.** The prompt builder publishes the final assembled prompt on
   `prompt_builder.v1.response.assemble`.

7. **Notify.** The prompt builder fires `prompt_builder.v1.hook.after_build` as a
   fire-and-forget notification. No response is expected or collected.

## Writing a plugin that contributes context

A capsule that wants to inject context into every prompt subscribes to the
before-build hook topic and publishes a response:

```rust
use astrid_sdk::ipc::{subscribe, publish, Message};
use astrid_sdk::types::HookResponse;

fn handle_before_build(msg: Message) {
    let response = HookResponse {
        prepend_context: Some("Relevant context from my plugin.".into()),
        system_prompt: None,
        prepend_system_context: None,
        append_system_context: None,
    };
    publish("prompt_builder.v1.hook.before_build", &response);
}

fn main() {
    subscribe("prompt_builder.v1.hook.before_build", handle_before_build);
}
```

If the capsule's `Capsule.toml` does not declare `allow_prompt_injection`, only
`prepend_context` takes effect. The other fields are silently stripped.

To also inject into the system prompt, the capsule manifest must declare the
capability:

```toml
[capabilities]
allow_prompt_injection = true
```

The runtime prompts the user for approval when this capability is first
requested.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## IPC topics

### `prompt_builder.v1.assemble`

Triggers the prompt assembly pipeline. Published by the frontend or orchestrator.

**Payload schema:**

```json
{
  "type": "object",
  "required": ["messages", "model", "provider"],
  "properties": {
    "messages": {
      "type": "array",
      "description": "Ordered conversation messages.",
      "items": {
        "type": "object",
        "required": ["role", "content"],
        "properties": {
          "role": { "type": "string", "enum": ["system", "user", "assistant", "tool"] },
          "content": { "type": "string" }
        }
      }
    },
    "system_prompt": {
      "type": "string",
      "description": "Base system prompt. May be null if no base prompt is configured."
    },
    "model": {
      "type": "string",
      "description": "Target model identifier (e.g., 'claude-opus-4-20250514')."
    },
    "provider": {
      "type": "string",
      "description": "Provider identifier (e.g., 'anthropic', 'openai')."
    }
  }
}
```

**Semantics:** The prompt builder must process exactly one assembly request at a
time. Concurrent requests queue and execute sequentially. This prevents
interleaving of hook responses across assembly cycles.

### `prompt_builder.v1.hook.before_build`

Fan-out hook sent to all subscribed plugin capsules before assembly.

**Payload schema:** Identical to the `prompt_builder.v1.assemble` payload. The
prompt builder forwards the assemble request fields verbatim.

**Response schema (hook contribution):**

```json
{
  "type": "object",
  "properties": {
    "prependContext": {
      "type": "string",
      "description": "User-visible context prepended above the conversation. Concatenated in arrival order across all responders."
    },
    "systemPrompt": {
      "type": "string",
      "description": "Full override of the base system prompt. Last non-null value wins. Requires allow_prompt_injection capability."
    },
    "prependSystemContext": {
      "type": "string",
      "description": "Text prepended to the system prompt. Concatenated in arrival order. Requires allow_prompt_injection capability."
    },
    "appendSystemContext": {
      "type": "string",
      "description": "Text appended to the system prompt. Concatenated in arrival order. Requires allow_prompt_injection capability."
    }
  }
}
```

All fields are optional. A response with no fields is a valid no-op
acknowledgment.

**Semantics:** The prompt builder publishes this topic once per assembly cycle,
then waits for responses up to `hook_timeout_ms`. Capsules that do not respond
within the timeout are excluded from that cycle with no error. The prompt builder
collects a maximum of 50 responses per cycle.

### `prompt_builder.v1.hook.after_build`

Fire-and-forget notification published after assembly completes.

**Payload schema:**

```json
{
  "type": "object",
  "required": ["assembled_prompt", "model", "provider", "contributor_count"],
  "properties": {
    "assembled_prompt": {
      "type": "object",
      "description": "The final assembled prompt (system prompt + messages).",
      "properties": {
        "system_prompt": { "type": "string" },
        "messages": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "role": { "type": "string" },
              "content": { "type": "string" }
            }
          }
        }
      }
    },
    "model": { "type": "string" },
    "provider": { "type": "string" },
    "contributor_count": {
      "type": "integer",
      "description": "Number of capsules that contributed to this assembly cycle."
    }
  }
}
```

**Semantics:** No response is expected. The prompt builder does not wait after
publishing this topic. Subscribers use it for logging, metrics, or audit trails.

### `prompt_builder.v1.response.assemble`

The final assembled prompt, published by the prompt builder after merge
completes.

**Payload schema:** Identical to the `assembled_prompt` object in the after-build
notification, promoted to the top level:

```json
{
  "type": "object",
  "required": ["system_prompt", "messages", "model", "provider"],
  "properties": {
    "system_prompt": { "type": "string" },
    "messages": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["role", "content"],
        "properties": {
          "role": { "type": "string" },
          "content": { "type": "string" }
        }
      }
    },
    "model": { "type": "string" },
    "provider": { "type": "string" }
  }
}
```

**Semantics:** Exactly one response message is published per assembly request.
The requester (frontend or orchestrator) subscribes to this topic to receive the
assembled prompt.

## Hook response merge order

Hook responses merge in the order they arrive, subject to permission gating.
The merge algorithm is:

1. Initialize `prepend_context_parts` as an empty list.
2. Initialize `system_prompt_override` as null.
3. Initialize `prepend_system_parts` as an empty list.
4. Initialize `append_system_parts` as an empty list.
5. For each response, in arrival order:
   a. Look up the responding capsule's UUID. Check (with caching) whether it
      holds `allow_prompt_injection`.
   b. If `prependContext` is non-null, append it to `prepend_context_parts`.
   c. If the capsule holds `allow_prompt_injection`:
      - If `systemPrompt` is non-null, set `system_prompt_override` to this
        value.
      - If `prependSystemContext` is non-null, append it to
        `prepend_system_parts`.
      - If `appendSystemContext` is non-null, append it to
        `append_system_parts`.
   d. If the capsule does not hold `allow_prompt_injection`, silently discard
      `systemPrompt`, `prependSystemContext`, and `appendSystemContext`.
6. Compute the effective system prompt:
   - If `system_prompt_override` is non-null, use it. Otherwise use the base
     system prompt from the assemble request.
   - Prepend `prepend_system_parts` (joined with `\n\n`).
   - Append `append_system_parts` (joined with `\n\n`).
7. Compute the final messages:
   - If `prepend_context_parts` is non-empty, join them with `\n\n` and insert
     as a user message at position 0 of the messages array.
   - All other messages remain in their original order.

## Permission gating

### The `allow_prompt_injection` capability

This capability gates write access to the system prompt. Without it, a capsule
can only contribute user-visible context via `prependContext`.

**Rationale:** The system prompt controls the agent's behavior, safety
constraints, and tool usage rules. Allowing arbitrary capsules to modify it is a
prompt injection vector. The capability model makes this an explicit, auditable
permission grant.

**Capability check caching:** During a single assembly cycle, the prompt builder
caches capability check results keyed by capsule UUID. This avoids redundant
capability store lookups when a capsule publishes multiple hook responses within
one cycle. The cache is discarded at the end of each cycle.

**Behavior when capability is absent:** Fields that require the capability are
silently discarded. No error is published to the contributing capsule. This
prevents information leakage about whether the capability was expected.

### Audit trail

Every assembly cycle logs:

- The capsule UUIDs that contributed.
- Which fields each capsule attempted to set.
- Which fields were stripped due to missing `allow_prompt_injection`.
- The final effective system prompt hash (Blake3).

This log is published to the audit event bus, not to the IPC bus.

## Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hook_timeout_ms` | u64 | 2000 | Maximum milliseconds to wait for before-build hook responses. |
| `max_hook_responses` | u32 | 50 | Maximum number of hook responses collected per assembly cycle. |

The prompt builder stops collecting responses when either the timeout expires or
the response count reaches `max_hook_responses`, whichever comes first.

## Identity capsule contract

The identity capsule is a first-party capsule that participates in prompt
assembly as a before-build hook contributor. It resolves workspace identity and
injects it into the system prompt.

### IPC topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `identity.v1.request.build` | Inbound | Triggers identity resolution. |
| `identity.v1.response.ready` | Outbound | Identity context assembled and ready. |
| `prompt_builder.v1.hook.before_build` | Inbound | Hook trigger from prompt builder. |

When the identity capsule receives a before-build hook, it performs identity
resolution and responds with hook contributions.

### Resolution order

1. **Workspace config.** Read `spark.toml` from the workspace root via VFS. If
   not found, fall back to built-in defaults. Extract:
   - `callsign` - the agent's name
   - `class` - agent class/role
   - `aura` - personality/tone descriptors
   - `signal` - behavioral signals
   - `core_directives` - mandatory instructions

2. **Environment context.** Read the current working directory and platform
   information from the host. Format as structured context.

3. **Tool usage guidelines.** Append tool usage instructions relevant to the
   current model and provider.

4. **Workspace instructions.** Read `AGENTS.md` from the workspace root via VFS.
   If not found, try `ASTRID.md` as a fallback. If neither exists, skip.

5. **Ignore patterns.** Read `.astridignore` from the workspace root via VFS.
   Parse glob patterns for file exclusion rules.

### Hook response

The identity capsule holds `allow_prompt_injection` and responds with:

- `systemPrompt`: The fully assembled identity system prompt (callsign, class,
  aura, signal, core directives, environment, tool guidelines, workspace
  instructions).

This means the identity capsule's system prompt replaces the base system prompt.
Other capsules that also set `systemPrompt` and arrive later in the response
order will override it (last-writer-wins). In practice, only the identity capsule
should set `systemPrompt`. Other capsules should use `prependSystemContext` or
`appendSystemContext` to layer on top.

## Memory capsule contract

The memory capsule is a first-party capsule that participates in prompt assembly
as a before-build hook contributor. It injects persistent memory into the system
prompt.

### Behavior

1. On receiving `prompt_builder.v1.hook.before_build`, read
   `.astrid/memory.md` from VFS.
2. If the file exists and is non-empty, publish a hook response with
   `appendSystemContext` containing the memory content.
3. If the file does not exist or is empty, publish an empty hook response (no-op).

### Size limits

- **Hard cap:** 32,768 bytes (32 KB).
- **Truncation:** If the file exceeds the hard cap, truncate to the last valid
  UTF-8 character boundary at or before the 32,768-byte mark. Do not split
  multi-byte characters.
- **No partial section detection.** The memory capsule does not attempt to
  truncate at markdown section boundaries. It truncates at the byte level
  (respecting UTF-8) and appends a `\n\n[truncated]` marker.

### Capability

The memory capsule holds `allow_prompt_injection` because it writes to
`appendSystemContext`.

## Error handling

| Condition | Behavior |
|-----------|----------|
| No capsules respond to before-build hook | Assembly proceeds with base system prompt and original messages. No error. |
| Hook timeout expires with partial responses | Assembly proceeds with responses received so far. Late responses are discarded. |
| A capsule publishes an invalid hook response (schema violation) | Response is discarded. An audit event is logged with the capsule UUID and error. |
| The assemble request has an invalid schema | The prompt builder publishes an error on `prompt_builder.v1.response.assemble` with `"error"` field set. |
| VFS read fails in identity or memory capsule | The capsule skips the failed step and continues with remaining steps. Logs a warning. |

## Concurrency

- The prompt builder processes one assembly request at a time. Requests queue
  in arrival order.
- Hook fan-out is concurrent: all subscribed capsules receive the before-build
  hook simultaneously and may respond in any order.
- The merge algorithm is deterministic given a fixed response arrival order.
- Capability check caching is scoped to a single assembly cycle and does not
  require synchronization across cycles.

# Drawbacks
[drawbacks]: #drawbacks

- **Added latency.** Every LLM call now waits up to `hook_timeout_ms` for plugin
  responses. A misbehaving capsule that sleeps for 2 seconds delays every prompt.
  Mitigation: the timeout is a hard cap, not a soft target. The prompt builder
  does not retry.

- **Last-writer-wins for systemPrompt is fragile.** If two capsules both set
  `systemPrompt`, the winner depends on response arrival order. This is
  technically deterministic (arrival order) but not intuitive. Mitigation: only
  the identity capsule should use `systemPrompt` in practice. The capability gate
  limits the blast radius.

- **Silent stripping hides misconfiguration.** A capsule author who forgets to
  declare `allow_prompt_injection` will not receive an error when their
  `appendSystemContext` is stripped. Mitigation: the audit log records every
  stripped field. Future tooling can surface warnings during capsule development.

- **50-response cap is arbitrary.** A deployment with many plugins could hit this
  limit. Mitigation: the cap is configurable and can be raised. 50 is a safe
  default that prevents runaway fan-in.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why a hook system instead of direct IPC calls?**

Direct calls (prompt builder calls each plugin) require the prompt builder to
know every plugin at compile time. A pub/sub hook system allows plugins to opt in
at runtime by subscribing to the before-build topic. This is more composable and
follows the UNIX philosophy of small, independent components.

**Why fan-out instead of a chain?**

A chain (plugin A calls plugin B calls plugin C) introduces ordering dependencies
and makes the pipeline fragile. If one link breaks, everything downstream fails.
Fan-out is parallel and independent. Each capsule contributes without knowing
about the others.

**Why last-writer-wins for systemPrompt instead of concatenation?**

The system prompt is semantically a single document, not a list. Concatenating
multiple full system prompts produces incoherent instructions. Last-writer-wins
gives the identity capsule (or a trusted override) full control over the base
document. Other capsules use `prependSystemContext` and `appendSystemContext` to
layer additions without replacing the core.

**Why silent stripping instead of error responses?**

Returning an error to a capsule that attempts unauthorized system prompt injection
reveals security boundaries. A malicious capsule could probe which fields are
gated. Silent stripping follows the principle of least information: the capsule
cannot distinguish between "my contribution was accepted" and "my contribution
was stripped."

**Why a separate identity capsule instead of built-in logic?**

Separating identity resolution into its own capsule makes it replaceable. An
enterprise deployment can swap in a corporate identity capsule that reads from
LDAP. A personal deployment can use the default that reads `spark.toml`. The
prompt builder does not care where the system prompt comes from - it only knows
the hook contract.

**Alternative: monolithic prompt builder.**

A single component that handles identity, memory, tool guidelines, and plugin
context internally. This is simpler but not composable. Adding a new context
source requires modifying the prompt builder. The hook system makes the prompt
builder a stable kernel that delegates to user-space capsules.

**Alternative: no permission gating.**

Trust all capsules equally. This is a prompt injection vulnerability. Any
third-party capsule could override the system prompt to bypass safety constraints.
The capability model is a defense-in-depth layer.

# Prior art
[prior-art]: #prior-art

**OpenClaw prompt assembly.** OpenClaw uses a middleware pipeline where each
middleware can modify the prompt before it reaches the LLM. Contributions are
ordered by middleware registration. Astrid's hook system is similar in spirit but
uses pub/sub rather than a fixed pipeline, and adds permission gating for system
prompt access.

**LangChain prompt templates.** LangChain composes prompts from templates with
variable substitution. This is a compile-time pattern: the template structure is
fixed, and only values change. Astrid's protocol allows runtime-dynamic
contributions from capsules that may not exist at build time.

**MCP server prompts.** The Model Context Protocol defines server-provided
prompts that clients can discover and invoke. MCP prompts are pull-based (the
client requests a prompt by name). Astrid's hook system is push-based (the prompt
builder fans out to all subscribers). Both approaches solve the problem of
external components contributing to prompts.

**VSCode extensions contributing to Copilot context.** VSCode allows extensions
to register context providers that inject workspace-specific information into
Copilot prompts. This is analogous to the before-build hook, with the extension
host acting as the fan-out coordinator.

**UNIX shell profile system.** `/etc/profile`, `~/.bashrc`, `/etc/profile.d/*.sh`
are evaluated in order to build the shell environment. The identity capsule fills
the `/etc/profile` role, and plugin capsules fill the `/etc/profile.d/` role. The
prompt builder is the shell that sources them all.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Hook response priority.** Should capsules declare a numeric priority that
  controls merge order instead of relying on arrival order? Arrival order is
  deterministic within one cycle but depends on capsule startup time and IPC
  latency.

- **Token budget awareness.** The prompt builder does not currently enforce a
  total token budget. If the assembled prompt exceeds the model's context window,
  the LLM call fails. Should the prompt builder truncate contributions to fit
  within a budget? If so, which contributions get truncated first?

- **Versioned hook payloads.** The `v1` in topic names implies versioning, but
  the protocol does not define how a capsule negotiates which version it supports.
  This is deferred to a future RFC on IPC versioning.

- **Streaming assembly.** The current protocol is batch-oriented: collect all
  contributions, then merge. A streaming variant that emits partial prompts as
  contributions arrive could reduce latency for time-sensitive use cases.

- **Memory capsule section-aware truncation.** The current spec truncates at byte
  boundaries. A smarter approach would respect markdown heading boundaries. This
  is deferred to implementation experience.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Prompt assembly visualization.** A debug mode that renders the assembled
  prompt with color-coded regions showing which capsule contributed each section.
  Useful for capsule developers and for auditing.

- **Conditional hooks.** Capsules declare predicates (e.g., "only trigger when
  model is claude-*") in their manifest. The prompt builder evaluates predicates
  before fan-out, reducing unnecessary IPC traffic.

- **Token budget allocation.** A follow-up RFC could define a token budget
  protocol where the prompt builder allocates token budgets to each contributor
  and contributors are responsible for staying within their allocation.

- **Hook chaining.** Allow a capsule to declare dependencies on other capsules'
  hook responses, enabling ordered composition (e.g., "my contribution should
  appear after the memory capsule's contribution").

- **Prompt caching hints.** The prompt builder could annotate sections of the
  assembled prompt with cache-friendliness hints, allowing the LLM provider layer
  to use prompt caching features (e.g., Anthropic's prompt caching) effectively.

- **Schema evolution.** When IPC versioning lands, the prompt builder can support
  multiple hook payload versions simultaneously, allowing gradual migration of
  capsule ecosystems.
