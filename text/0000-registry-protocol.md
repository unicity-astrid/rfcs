- Feature Name: `registry_protocol`
- Start Date: 2026-03-16
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

This RFC defines the IPC contract for the LLM provider registry capsule. The
registry dynamically discovers LLM provider capsules at runtime, maintains a
list of available models, manages active model selection, and exposes a topic-
based API for querying and switching providers. It is the device manager in
Astrid's OS model: capsules register themselves as hardware, and the registry
makes them addressable.

# Motivation
[motivation]: #motivation

Astrid supports multiple LLM backends (Anthropic, OpenAI, local models, etc.)
as isolated capsules. Today there is no standardized way for a frontend or
orchestration capsule to answer basic questions:

- What providers are available right now?
- Which model is active?
- How do I switch to a different model?
- What happens when a provider capsule is added or removed at runtime?

Without a registry protocol, every consumer must hard-code provider knowledge,
poll for capsule presence, or rely on out-of-band configuration. This creates
tight coupling between frontends and specific providers, makes hot-reload
impossible, and forces users to manually configure model routing even in single-
provider deployments.

The registry solves this by providing a single, stable IPC surface that abstracts
over provider capsule lifecycle. Frontends subscribe to registry topics and
receive structured provider metadata. The registry handles discovery, selection
persistence, and hot-reload transparently.

This is critical for:

- **CLI model switching** - the `/models` command needs a discoverable provider
  list without hard-coding capsule names
- **Multi-provider deployments** - users running both cloud and local models need
  a unified selection mechanism
- **Hot-reload** - adding a new provider capsule at runtime should make it
  immediately available without restarting the session
- **Single-provider UX** - when only one provider exists, the registry auto-
  selects it so the user never has to think about model configuration

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## The registry as device manager

Think of the registry capsule as a device manager in an operating system. When
you plug in a USB device, the OS detects it, loads the right driver, and makes
the device available to applications. Applications do not scan the PCI bus
themselves. They ask the device manager.

The registry works the same way. Provider capsules are the "devices." When the
kernel boots and loads all capsules, the registry discovers which ones are LLM
providers by inspecting their metadata. It builds a provider list and publishes
it on the IPC bus. Frontends and orchestration capsules consume this list without
needing to know anything about individual provider capsules.

## Lifecycle

1. The kernel boots and loads all capsules.
2. The kernel publishes `astrid.v1.capsules_loaded` on the IPC bus.
3. The registry receives this event and queries the kernel for capsule metadata.
4. For each capsule that exposes LLM provider metadata, the registry creates a
   provider entry with the model ID, description, request/stream topics, and
   capabilities.
5. The registry persists the provider list and active model selection in its
   capsule KV store.
6. If exactly one provider exists, the registry auto-selects it.
7. Frontends query the registry via `registry.v1.get_providers` and
   `registry.v1.get_active_model` to populate their UI.

## Switching models

A user switches models in one of two ways:

**Direct switch** - the frontend already knows the model ID (e.g., the user
typed `/models claude-sonnet-4-20250514`). The frontend publishes
`registry.v1.set_active_model` with the model ID. The registry validates the ID,
updates its KV store, and emits `registry.v1.active_model_changed`.

**Interactive selection** - the user types `/models` with no argument. The
frontend publishes `registry.v1.get_providers`. The registry responds with the
provider list. The frontend renders a TUI picker using the `SelectionRequired`
payload pattern. When the user picks a model, the frontend publishes
`registry.v1.set_active_model`.

## Hot-reload

When the kernel reloads capsules (e.g., a new provider capsule is installed), it
publishes `astrid.v1.capsules_loaded` again. The registry re-runs discovery from
scratch:

1. Queries fresh capsule metadata.
2. Builds a new provider list.
3. If the previously active model is still present, it remains active.
4. If the previously active model is gone, the registry clears the selection. If
   exactly one provider remains, it auto-selects.
5. Emits `registry.v1.active_model_changed` if the active model changed.

Frontends subscribed to `registry.v1.active_model_changed` update their state
automatically.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## IPC topics

All registry topics use the `registry.v1` namespace. Per-request response topics
use `registry.v1.response.*` with a unique request ID suffix.

### `registry.v1.get_providers`

Query the list of available LLM providers.

**Request payload:**

```json
{
  "request_id": "string",
  "response_topic": "registry.v1.response.<request_id>"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | String | Yes | Unique identifier for this request. Used to route the response. |
| `response_topic` | String | Yes | Topic where the response will be published. Must follow the pattern `registry.v1.response.<request_id>`. |

**Response payload:**

```json
{
  "request_id": "string",
  "providers": [
    {
      "model_id": "string",
      "description": "string",
      "capsule_name": "string",
      "request_topic": "string",
      "stream_topic": "string",
      "capabilities": ["string"]
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `request_id` | String | Echoed from the request. |
| `providers` | Array | List of discovered provider entries. May be empty if no providers are loaded. |

**Provider entry fields:**

| Field | Type | Description |
|-------|------|-------------|
| `model_id` | String | Unique model identifier (e.g., `claude-sonnet-4-20250514`, `gpt-4o`). |
| `description` | String | Human-readable description of the model. |
| `capsule_name` | String | Name of the capsule that provides this model. |
| `request_topic` | String | IPC topic for sending generation requests (e.g., `llm.v1.request.generate.anthropic`). |
| `stream_topic` | String | IPC topic for receiving streaming responses (e.g., `llm.v1.stream.anthropic`). |
| `capabilities` | Array of String | List of supported features (e.g., `["streaming", "tool_use", "vision"]`). |

### `registry.v1.get_active_model`

Query the currently active model.

**Request payload:**

```json
{
  "request_id": "string",
  "response_topic": "registry.v1.response.<request_id>"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | String | Yes | Unique identifier for this request. |
| `response_topic` | String | Yes | Topic where the response will be published. |

**Response payload:**

```json
{
  "request_id": "string",
  "active_model": {
    "model_id": "string",
    "description": "string",
    "capsule_name": "string",
    "request_topic": "string",
    "stream_topic": "string",
    "capabilities": ["string"]
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `request_id` | String | Echoed from the request. |
| `active_model` | Provider entry or `null` | The currently active provider. `null` if no model is selected. |

### `registry.v1.set_active_model`

Set the active model by model ID.

**Request payload:**

```json
{
  "request_id": "string",
  "response_topic": "registry.v1.response.<request_id>",
  "model_id": "string"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | String | Yes | Unique identifier for this request. |
| `response_topic` | String | Yes | Topic where the response will be published. |
| `model_id` | String | Yes | The `model_id` of the provider to activate. Must match an entry in the current provider list. |

**Success response payload:**

```json
{
  "request_id": "string",
  "success": true,
  "active_model": {
    "model_id": "string",
    "description": "string",
    "capsule_name": "string",
    "request_topic": "string",
    "stream_topic": "string",
    "capabilities": ["string"]
  }
}
```

**Error response payload:**

```json
{
  "request_id": "string",
  "success": false,
  "error": "string"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `request_id` | String | Echoed from the request. |
| `success` | Boolean | `true` if the model was activated, `false` on error. |
| `active_model` | Provider entry | Present only on success. The newly active provider. |
| `error` | String | Present only on failure. Human-readable error message (e.g., `"unknown model_id: gpt-5"`). |

On success, the registry also emits `registry.v1.active_model_changed` (see
below).

### `registry.v1.active_model_changed`

Broadcast event emitted whenever the active model changes. This includes
explicit switches via `set_active_model`, auto-selection, and clearing due to
hot-reload.

**Payload:**

```json
{
  "previous_model_id": "string | null",
  "active_model": {
    "model_id": "string",
    "description": "string",
    "capsule_name": "string",
    "request_topic": "string",
    "stream_topic": "string",
    "capabilities": ["string"]
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `previous_model_id` | String or null | The model ID that was active before this change. `null` if no model was previously selected. |
| `active_model` | Provider entry or null | The newly active provider. `null` if the active model was cleared (e.g., the provider capsule was removed and no auto-selection occurred). |

This is a broadcast topic. Any capsule or frontend subscribed to it receives the
event. There is no request/response pattern here.

### `registry.v1.response.*`

Per-request response topics. The wildcard segment is the `request_id` from the
original request. Consumers subscribe to their specific response topic before
publishing the request, then unsubscribe after receiving the response.

The registry publishes exactly one message to each response topic per request.

## Provider discovery

When the registry receives `astrid.v1.capsules_loaded`, it executes the
following sequence:

1. **Query capsule metadata.** Send `GetCapsuleMetadata` to the kernel. This
   returns a list of all loaded capsules with their metadata.

2. **Filter LLM providers.** A capsule is an LLM provider if its metadata
   contains a `provider` section with at minimum `model_id`, `request_topic`,
   and `stream_topic` fields.

3. **Build provider entries.** For each qualifying capsule, construct a provider
   entry:
   - `model_id` - from capsule metadata `provider.model_id`
   - `description` - from capsule metadata `provider.description` (defaults to
     empty string if absent)
   - `capsule_name` - the capsule's registered name
   - `request_topic` - from capsule metadata `provider.request_topic`
   - `stream_topic` - from capsule metadata `provider.stream_topic`
   - `capabilities` - from capsule metadata `provider.capabilities` (defaults to
     empty list if absent)

4. **Persist.** Write the provider list to the capsule KV store under the key
   `providers`. Write the active model ID (if any) under the key
   `active_model_id`.

5. **Reconcile active model.** If an active model was previously set:
   - If the model ID still exists in the new provider list, keep it active.
   - If the model ID no longer exists, clear the active model.
6. **Auto-select.** If no model is active and exactly one provider exists, auto-
   select it.
7. **Emit change event.** If the active model changed during reconciliation or
   auto-selection, emit `registry.v1.active_model_changed`.

## SelectionRequired payload

When a frontend needs to present a model picker to the user (e.g., the `/models`
command with no argument), it uses the `SelectionRequired` payload pattern. This
is not a registry-specific type; it is a general UI pattern that the registry
populates.

```json
{
  "request_id": "string",
  "title": "string",
  "options": [
    {
      "id": "string",
      "label": "string",
      "description": "string"
    }
  ],
  "callback_topic": "string"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `request_id` | String | Unique identifier for this selection flow. |
| `title` | String | Title for the picker UI (e.g., `"Select a model"`). |
| `options` | Array of SelectionOption | The choices to display. |
| `callback_topic` | String | Topic to publish the user's selection to (e.g., `registry.v1.set_active_model`). |

**SelectionOption:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | String | The value to send back (maps to `model_id`). |
| `label` | String | Display label (e.g., `"Claude Sonnet 4"`). |
| `description` | String | Additional context (e.g., `"Anthropic - streaming, tool_use, vision"`). |

The frontend is responsible for:

1. Rendering the options as a TUI picker (or equivalent).
2. Capturing the user's selection.
3. Publishing `registry.v1.set_active_model` with the chosen `id` as
   `model_id`.

## CLI integration

The CLI frontend exposes model management via the `/models` command:

- **`/models`** (no argument) - the CLI publishes `registry.v1.get_providers`,
  receives the provider list, constructs a `SelectionRequired` payload, and
  renders a TUI picker. When the user selects a model, the CLI publishes
  `registry.v1.set_active_model`.

- **`/models <model_id>`** (with argument) - the CLI publishes
  `registry.v1.set_active_model` directly with the given model ID. On success,
  it displays a confirmation. On error, it displays the error message.

## KV store layout

The registry persists its state in the capsule KV store using the following keys:

| Key | Value type | Description |
|-----|-----------|-------------|
| `providers` | JSON array of provider entries | The current provider list. |
| `active_model_id` | String or null | The model ID of the currently active provider. |

Persistence ensures that the active model survives capsule restarts. On startup
(before `capsules_loaded` triggers fresh discovery), the registry reads
`active_model_id` from KV to restore the previous selection if the corresponding
provider is still available.

## Security

The registry accepts capsule metadata responses only from the kernel's system
session UUID. This prevents a malicious capsule from injecting fake provider
entries.

Specifically:

- When the registry sends `GetCapsuleMetadata`, it records the kernel's session
  UUID.
- When it receives the metadata response, it verifies the sender's session UUID
  matches.
- Messages from any other session UUID are logged at `warn` level and discarded.
- The registry never acts on provider metadata that did not originate from the
  kernel.

This is the only trust boundary in the protocol. All other topics
(`get_providers`, `set_active_model`, etc.) are consumable by any capsule or
frontend with IPC access, since provider metadata is not sensitive.

## Ordering and concurrency

- **Discovery is serialized.** If the registry receives a second
  `astrid.v1.capsules_loaded` while processing the first, it queues the second
  and processes it after the first completes. This prevents race conditions
  during provider list construction.
- **Request handling during discovery.** Requests to `get_providers`,
  `get_active_model`, and `set_active_model` that arrive during an active
  discovery cycle are held until discovery completes, then processed against the
  new provider list. This ensures consumers never see a partially constructed
  provider list.
- **set_active_model is atomic.** The KV write for `active_model_id` and the
  emission of `active_model_changed` happen as a single logical operation. A
  concurrent `get_active_model` will see either the old or new value, never an
  intermediate state.
- **active_model_changed ordering.** The broadcast is published after the KV
  store is updated. Any consumer that queries `get_active_model` after receiving
  the change event is guaranteed to see the new value.

## Error handling

| Condition | Behavior |
|-----------|----------|
| `GetCapsuleMetadata` fails | Log error, retain previous provider list, do not clear active model. |
| `set_active_model` with unknown model ID | Return error response with `success: false`. |
| KV store write fails | Log error, continue with in-memory state. Retry on next mutation. |
| Metadata response from untrusted session | Log warning, discard message. |
| No providers discovered | Provider list is empty, active model is `null`. |

# Drawbacks
[drawbacks]: #drawbacks

- **Single point of coordination.** The registry becomes a required intermediary
  for model selection. If the registry capsule crashes, frontends cannot switch
  models until it restarts. However, the last-known active model (persisted in
  KV) continues to function for existing sessions since routing topics are cached
  by consumers.

- **Discovery latency.** The registry must wait for `capsules_loaded` and then
  query metadata before providers become available. This adds startup latency
  compared to static configuration. In practice, this is sub-second on typical
  deployments.

- **Additional IPC traffic.** Every model query and switch generates IPC
  messages. For the expected frequency of these operations (user-initiated, not
  per-token), the overhead is negligible.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why dynamic discovery instead of static configuration?**

Static config (e.g., a `[providers]` section in a config file) is simpler but
breaks the capsule isolation model. The kernel should not need to know which
provider capsules exist at compile time. Dynamic discovery means installing a new
provider capsule is sufficient to make it available. No config changes, no
restarts.

**Why a dedicated registry capsule instead of kernel-side logic?**

The kernel should be minimal and unopinionated. Provider management is a policy
decision (which model to default to, how to present choices). Policy belongs in
user-space. The registry capsule runs in the same sandbox as any other capsule,
follows the same capability model, and can be replaced or extended without
modifying the kernel.

**Why topic-based IPC instead of host functions?**

Host functions are reserved for operations that require kernel privilege (file
I/O, IPC primitives, capability validation). Model selection is a user-space
concern. Using IPC topics keeps the host ABI surface small and allows any capsule
to participate in the protocol.

**Why auto-select the sole provider?**

Most users run a single LLM backend. Forcing them to manually select a model on
every session start is friction that provides no value. Auto-selection eliminates
this while remaining transparent: the `active_model_changed` event fires
regardless of whether selection was manual or automatic.

**Alternative: direct capsule-to-capsule negotiation.**

Frontends could discover providers by subscribing to a well-known topic and
waiting for provider capsules to announce themselves. This eliminates the registry
as a single point of coordination but introduces broadcast storms, timing issues
(what if a frontend starts before a provider?), and no single source of truth for
the active model. The centralized registry is a better trade-off at Astrid's
current scale.

# Prior art
[prior-art]: #prior-art

- **Linux udev** - the kernel emits device events, udev matches them against
  rules, and creates device nodes in `/dev`. The registry follows the same
  pattern: the kernel emits capsule lifecycle events, the registry matches them
  against provider criteria, and publishes structured provider entries on the IPC
  bus. udev also handles hot-plug (add/remove at runtime), which maps directly to
  Astrid's capsule hot-reload.

- **Docker registry** - a centralized index of available images with metadata.
  Docker clients query the registry to discover what is available before pulling.
  The Astrid registry similarly serves as a centralized index, though it discovers
  providers from the local runtime rather than a remote server.

- **npm registry** - packages declare their capabilities via `package.json`
  metadata. The registry indexes this metadata and makes it queryable. Astrid
  provider capsules declare capabilities via capsule metadata, and the registry
  indexes them similarly.

- **D-Bus / systemd** - D-Bus provides a message bus for service discovery on
  Linux. Services register on well-known names, and clients query the bus to find
  them. The registry's topic-based IPC pattern is analogous, with the registry
  acting as the name service.

- **Kubernetes service discovery** - Kubernetes uses labels and selectors to match
  pods to services. The registry uses capsule metadata fields to match capsules to
  provider entries. Both are declarative: the provider does not register itself
  explicitly, it simply declares its metadata and the discovery system finds it.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Provider health checks.** Should the registry actively monitor whether a
  provider capsule is healthy (responding to requests), or is liveness detection
  out of scope for the registry protocol? If the registry owns health, it becomes
  a service mesh. If it does not, stale providers may linger in the list until the
  next `capsules_loaded` event.

- **Multi-model capsules.** Some providers may expose multiple models from a
  single capsule (e.g., an OpenAI capsule offering both GPT-4o and o3). Should
  `model_id` be unique per capsule, or should a single capsule be able to
  register multiple provider entries? The current design allows it (metadata can
  contain multiple provider sections), but the discovery algorithm needs explicit
  handling.

- **Capability schema.** The `capabilities` field is currently a list of free-
  form strings. Should there be a standardized capability vocabulary (e.g.,
  `streaming`, `tool_use`, `vision`, `code_execution`), or is free-form
  sufficient for now?

- **Provider priority/preference.** When multiple providers are available, should
  the registry support a preference ordering beyond "last selected"? This might
  matter for fallback scenarios (primary provider down, fall back to secondary).

# Future possibilities
[future-possibilities]: #future-possibilities

- **Provider routing rules.** Beyond simple active-model selection, the registry
  could support routing rules: "use model A for code tasks, model B for
  creative tasks." This turns the registry into a model router.

- **Capability-based selection.** A consumer could request "a model that supports
  tool_use and vision" and the registry would resolve it to a matching provider.
  This decouples consumers from specific model names.

- **Provider groups.** Grouping providers by vendor or capability profile for
  bulk operations (e.g., "disable all local models").

- **Usage tracking.** The registry is well-positioned to track which provider
  handled each request, enabling per-model usage dashboards and cost attribution.

- **Federated registries.** For multi-node deployments, registries on different
  nodes could synchronize provider lists, enabling cross-node model selection.

- **Provider versioning.** As provider capsules evolve, the registry could track
  capsule versions and warn about incompatible updates.
