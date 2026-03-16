- Feature Name: `registry_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#13](https://github.com/unicity-astrid/rfcs/pull/13)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `registry.v1` event contract defines the topic patterns and message schemas
for LLM provider registry operations on the Astrid event bus. The registry
tracks available providers and models, manages the active model selection, and
broadcasts model change notifications.

# Motivation
[motivation]: #motivation

Astrid supports multiple LLM providers simultaneously. The registry contract
must be standardized so that:

- Frontends can list available providers and models.
- Model switching is a runtime operation, not a restart.
- The orchestrator is notified when the active model changes.
- Interactive model selection flows through a generic picker UI.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Three actors participate:

1. **Frontend capsule** - queries providers, triggers model selection.
2. **Registry capsule** - maintains provider/model state, resolves selection.
3. **Orchestrator** - listens for model change notifications.

```text
Frontend                     Registry                  Orchestrator
    |                           |                           |
    |-- get_providers -------->|                           |
    |<-- providers list -------|                           |
    |                           |                           |
    |-- set_active_model ----->|                           |
    |<-- confirmation ---------|                           |
    |                           |-- active_model_changed ->|
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| request | `registry.v1.get_providers` | `Custom` | Frontend | Registry |
| response | `registry.v1.response.get_providers` | `Custom` | Registry | Frontend |
| request | `registry.v1.get_active_model` | `Custom` | Frontend | Registry |
| response | `registry.v1.response.get_active_model` | `Custom` | Registry | Frontend |
| request | `registry.v1.set_active_model` | `Custom` | Frontend | Registry |
| response | `registry.v1.response.set_active_model` | `Custom` | Registry | Frontend |
| broadcast | `registry.v1.active_model_changed` | `Custom` | Registry | Orchestrator, Frontend |
| selection | `registry.v1.response.models` | `SelectionRequired` | Registry | Frontend |
| callback | `registry.v1.selection.callback` | `Custom` | Frontend | Registry |

## Message schemas

### Get providers response

```json
{
  "type": "custom",
  "data": {
    "providers": [
      {
        "id": "claude-sonnet-4-20250514",
        "description": "Claude Sonnet 4",
        "capabilities": ["text", "vision", "tools"]
      }
    ]
  }
}
```

### Set active model request

```json
{
  "type": "custom",
  "data": {
    "model_id": "string"
  }
}
```

### Active model changed notification

```json
{
  "type": "custom",
  "data": {
    "provider_name": "string",
    "model_id": "string",
    "capabilities": ["string"]
  }
}
```

### Interactive model selection

When multiple models are available and the user has not chosen, the registry
publishes a `SelectionRequired` payload:

```json
{
  "type": "selection_required",
  "request_id": "string",
  "title": "Select a model",
  "options": [
    { "id": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4", "description": "Fast, capable" }
  ],
  "callback_topic": "registry.v1.selection.callback"
}
```

The frontend renders a picker UI and publishes the user's choice:

```json
{
  "type": "custom",
  "data": {
    "request_id": "string",
    "selected_id": "string"
  }
}
```

## Behavioral requirements

A conforming **registry** must:

1. Subscribe to `registry.v1.get_providers`, `registry.v1.get_active_model`,
   `registry.v1.set_active_model`, and `registry.v1.selection.callback`.
2. Query loaded capsule metadata to discover available providers.
3. Publish `registry.v1.active_model_changed` whenever the active model
   changes.
4. Use `SelectionRequired` for interactive model selection when needed.

## Error handling

| Condition | Behavior |
|-----------|----------|
| No providers available | Response with empty providers list |
| Unknown model_id on set | Response with error description |
| Registry not subscribed | Request to empty topic, caller times out |

# Drawbacks
[drawbacks]: #drawbacks

- No scoped reply topics. Responses go to a shared topic, which could cause
  issues with concurrent frontends.
- Interactive selection requires frontend support for `SelectionRequired`.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why a registry capsule instead of config?** Providers are dynamic. When a
provider capsule loads/unloads, the registry updates automatically.

**Why `SelectionRequired` instead of a registry-specific picker?** Reuses the
generic selection pattern that any frontend already implements.

# Prior art
[prior-art]: #prior-art

- **Kubernetes service discovery**: Dynamic registration of backends.
- **VS Code language server protocol**: Client discovers server capabilities
  at initialization.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should scoped reply topics be added for concurrent frontend safety?
- Should provider health status be exposed through the registry?

# Future possibilities
[future-possibilities]: #future-possibilities

- Provider health monitoring and automatic failover.
- Cost-aware model selection.
- Per-session model pinning.
