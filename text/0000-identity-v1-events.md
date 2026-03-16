- Feature Name: `identity_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#12](https://github.com/unicity-astrid/rfcs/pull/12)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `identity.v1` event contract defines the topic patterns and message schemas
for agent identity resolution on the Astrid event bus. An identity capsule
receives a build request and returns system prompt context that establishes the
agent's persona, capabilities, and behavioral constraints.

# Motivation
[motivation]: #motivation

An agent's identity (system prompt, persona, behavioral constraints) must be
decoupled from the orchestrator so that:

- Identity can be configured per-workspace or per-deployment without changing
  the orchestrator.
- Identity capsules can pull context from external sources (files, APIs,
  databases).
- The orchestrator treats identity as an opaque context blob.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Two actors participate:

1. **Caller** (orchestrator) - requests identity context at session start.
2. **Identity capsule** - resolves and returns the system prompt context.

```text
Caller                          Identity capsule
    |                                  |
    |-- build request --------------->|
    |   (identity.v1.request.build)   |-- resolve identity
    |                                  |
    |<-- ready response ---------------|
    |   (identity.v1.response.ready)  |
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| request | `identity.v1.request.build` | `Custom` | Caller | Identity capsule |
| response | `identity.v1.response.ready` | `Custom` | Identity capsule | Caller |

## Message schemas

### Build request

```json
{
  "type": "custom",
  "data": {
    "session_id": "string",
    "workspace_path": "string | null"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | yes | The session requesting identity context. |
| `workspace_path` | string/null | no | Workspace path for workspace-specific identity. |

### Ready response

```json
{
  "type": "custom",
  "data": {
    "system_context": "string (system prompt text)",
    "persona_name": "string | null",
    "capabilities": ["string"]
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `system_context` | string | yes | System prompt text to prepend/merge into the prompt. |
| `persona_name` | string/null | no | Display name for the agent persona. |
| `capabilities` | `string[]` | no | Declared capabilities for capability-aware frontends. |

## Behavioral requirements

A conforming **identity capsule** must:

1. Subscribe to `identity.v1.request.build` via an interceptor.
2. Resolve the agent identity (from config, files, or external sources).
3. Publish the identity context to `identity.v1.response.ready`.

A conforming **caller** must:

1. Publish the build request at session start.
2. Wait for the ready response before proceeding with prompt assembly.
3. Handle timeout if the identity capsule does not respond.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Identity capsule not subscribed | Request to empty topic, caller times out and uses default identity |
| Invalid workspace path | Capsule returns default identity, not an error |

# Drawbacks
[drawbacks]: #drawbacks

- Only a request/response pair with no caching mechanism. The orchestrator
  must request identity every session.
- The response schema is minimal. Complex identity (multi-persona, role-based)
  would need to be encoded in `system_context` as text.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why a separate identity capsule?** Keeps the orchestrator generic. The
orchestrator does not parse config files or read `.astrid/` directories.
A capsule handles that, and the contract is just "give me text."

**Why not just a config file?** Identity can be dynamic. A workspace-aware
identity capsule might scan the project, read `.astrid.yml`, or call an
API. A config file is one possible implementation, not the only one.

# Prior art
[prior-art]: #prior-art

- **Claude system prompts**: Static text set by the API caller. The identity
  capsule makes this dynamic.
- **ChatGPT custom instructions**: User-configured persona. Similar concept,
  applied at the platform level rather than per-workspace.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should identity be cacheable with a TTL, or always freshly resolved?
- Should there be a `identity.v1.response.error` topic for explicit failures?

# Future possibilities
[future-possibilities]: #future-possibilities

- Multi-persona support (switching personas mid-conversation).
- Identity inheritance for sub-agents (child inherits parent's identity
  with additional constraints).
- Identity signing for cryptographic proof of persona authorization.
