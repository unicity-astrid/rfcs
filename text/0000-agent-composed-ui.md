- Feature Name: `agent_composed_ui`
- Start Date: 2026-03-25
- RFC PR: [rfcs#25](https://github.com/unicity-astrid/rfcs/pull/25)
- Tracking Issue: [astrid#629](https://github.com/unicity-astrid/astrid/issues/629)

# Summary
[summary]: #summary

Adopt Google's A2UI (Agent-to-User Interface) protocol as the rendering contract between capsules,
the agent, and frontends. The agent owns the full layout composition — there are no predefined zones,
slots, or hardcoded regions. Frontends are dumb renderers that draw whatever component tree the agent
describes. Capsules provide components and data sources; the agent decides what goes where.

# Motivation
[motivation]: #motivation

The frontend should not own structure or content. Content comes from capsules. Structure comes from
the agent.

The agent is uniquely positioned to compose UI because it understands:
- What capsules are loaded and what components they offer
- What the user is currently doing (coding, chatting, configuring, debugging)
- What the user has asked for ("show me tools on the right", "minimal interface")
- What information is relevant right now vs. noise

By making the agent the UI composer:
- **Adaptive layouts** — the agent reshapes the UI based on task context
- **Natural language customization** — "put my config on the right" just works
- **No hardcoded structure** — the default layout is just the agent's starting opinion
- **Multi-frontend for free** — each frontend renders A2UI components natively; the agent's layout
  description is frontend-agnostic
- **Personality-driven defaults** — different agent identities can have different default layouts
- **Theme as data** — colors and styling become data model patches the agent controls, adapting to
  terminal capabilities rather than assuming a dark background

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## For capsule developers

Your capsule exports **components** — UI building blocks that the agent can place anywhere. You
declare what you can provide; you do not decide where it goes.

A component is registered via a `ui_describe` interceptor (same pattern as `tool_describe`):

```rust
#[astrid::ui_component("model-status")]
fn model_status() -> UiComponent {
    UiComponent::new("model-status")
        .catalog_type("Text")
        .data_model(json!({
            "model": "unknown",
            "context_usage": 0.0
        }))
}
```

The agent discovers available components and composes them into a layout. Your capsule updates its
data model via IPC — the component re-renders automatically through A2UI data binding:

```rust
sdk::publish("ui.v1.data_model.update", json!({
    "surfaceId": "root",
    "patch": [
        { "op": "replace", "path": "/model-status/model", "value": "gpt-5.4" },
        { "op": "replace", "path": "/model-status/context_usage", "value": 0.47 }
    ]
}));
```

A capsule can offer multiple components of different shapes — a full config form, a compact status
line, a detailed dashboard. Same data, different presentations. The agent picks what fits.

## For users

The UI adapts to you. On first launch, you get a sensible default layout. But you can reshape it:

```
You: show me loaded tools in a sidebar
Agent: [restructures layout — adds a right column with the tool list]

You: actually, put that at the bottom instead
Agent: [moves tool list below the chat area]

You: minimal mode — just chat
Agent: [collapses to chat + input only]
```

Layout preferences persist across sessions. Different agent personalities may have different defaults.

## For frontend implementors

Your frontend is a **dumb A2UI renderer**. You receive a component tree and render it using your
native widget toolkit.

Responsibilities:
- Map A2UI component types to native widgets (`Row` → flex row, `Text` → text widget, etc.)
- Handle user input events and route them back via IPC
- Advertise your component catalog (what A2UI types you can render)
- Gracefully degrade for unsupported component types (render as text or omit)

## The agent's role

The agent owns a single root A2UI surface that IS the entire screen. It composes the full component
tree using A2UI layout primitives (`Row`, `Column`, `Card`, `Tabs`, `List`) and capsule-provided
components.

On startup, the agent emits a default layout:

```json
{
  "createSurface": {
    "surfaceId": "root",
    "catalogId": "astrid/cli/v1"
  }
}
```

```json
{
  "updateComponents": {
    "surfaceId": "root",
    "components": [
      { "id": "root", "component": "Column", "children": ["status", "main", "input"] },
      { "id": "status", "component": "Row", "children": ["breadcrumb", "model-info"], "justify": "spaceBetween" },
      { "id": "breadcrumb", "component": "Text", "text": "~ > dev > astrid", "variant": "caption" },
      { "id": "model-info", "component": "Text", "text": "gpt-5.4 | 47%", "variant": "caption" },
      { "id": "main", "component": "Column", "weight": 1, "children": ["messages"] },
      { "id": "messages", "component": "List", "direction": "vertical", "children": [] },
      { "id": "input", "component": "TextField", "placeholder": "Message..." }
    ]
  }
}
```

This is not special. It is not hardcoded. It is the agent's opening move. When the user asks for a
sidebar, the agent restructures the tree — replacing `main` with a `Row` containing the chat column
and a new sidebar column. No code change. No predefined zones. Just a different component tree.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Protocol

Adopt A2UI (latest stable at implementation time) as the wire protocol. No modifications to the
core spec.

### Message types (A2UI standard)

| Message | Purpose |
|---------|---------|
| `createSurface` | Create a rendering surface with a catalog reference |
| `updateComponents` | Add, modify, or remove components in a surface |
| `updateDataModel` | Patch the surface's data model (RFC 6902 JSON Patch) |
| `deleteSurface` | Destroy a surface |

### Component catalog

Each frontend advertises a catalog of A2UI component types it can render. The catalog is the
security boundary — only allowlisted types are rendered.

CLI catalog (A2UI → ratatui mapping):

| A2UI Component | ratatui Mapping | Notes |
|----------------|-----------------|-------|
| `Row` | `Layout::horizontal` | `justify`, `align`, `weight` supported |
| `Column` | `Layout::vertical` | `justify`, `align`, `weight` supported |
| `Text` | `Paragraph` | `variant` maps to styling (h1=bold, caption=dim) |
| `List` | `List` / scrollable | Vertical or horizontal |
| `Card` | `Block` with borders | Single child |
| `Tabs` | `Tabs` widget | Tab switching via keybind |
| `Button` | Keybind-triggered action | Rendered as `[label]`, activated by key |
| `TextField` | Input widget | Single-line text entry |
| `TextArea` | Multi-line input | For message composition |
| `ProgressBar` | `Gauge` | 0.0–1.0 value |
| `Table` | `Table` | Headers + rows |
| `Badge` | Styled `Span` | Inline colored label |
| `Select` | Popup list | Arrow-key selection |
| `Checkbox` | Toggle | `[x]` / `[ ]` |
| `Modal` | Overlay `Clear` + centered `Block` | Focus trap |

Unsupported components degrade to `Text` or are omitted.

### IPC topics

A2UI messages travel over the IPC event bus under `ui.v1.*`:

| IPC Topic | Payload | Direction |
|-----------|---------|-----------|
| `ui.v1.surface.create` | A2UI `createSurface` | Agent → Frontend |
| `ui.v1.components.update` | A2UI `updateComponents` | Agent → Frontend |
| `ui.v1.data_model.update` | A2UI `updateDataModel` | Capsule/Agent → Frontend |
| `ui.v1.surface.delete` | A2UI `deleteSurface` | Agent → Frontend |
| `ui.v1.action` | `{ surfaceId, componentId, action, data }` | Frontend → Agent |
| `ui.v1.catalog.query` | `{}` | Agent → Frontend |
| `ui.v1.catalog.response` | `{ components: [...] }` | Frontend → Agent |

### Component discovery

UI component discovery follows the same bus-based pattern as tool discovery:

1. Agent triggers `ui.v1.request.describe` (hook fan-out to all capsules)
2. Each capsule's `ui_describe` interceptor responds with its available components
3. Agent collects the component registry

The `Capsule.toml` declares the interceptor and IPC capabilities:

```toml
[[interceptor]]
event = "ui.v1.request.describe"
action = "ui_describe"

[capabilities]
ipc_publish = ["ui.v1.response.describe.*"]
```

The `ui_describe` handler returns what the capsule can provide:

```json
{
  "components": [
    {
      "id": "identity-config-form",
      "display_name": "Identity Configuration",
      "description": "Agent identity settings — callsign, class, tone, backstory",
      "schema": {
        "properties": {
          "callsign": { "type": "string", "description": "Agent's display name" },
          "class": { "type": "string", "description": "Agent class/role" },
          "tone": { "type": "string", "description": "Communication style" }
        }
      },
      "data_topic": "identity.v1.config",
      "actions": ["save", "reset"]
    },
    {
      "id": "identity-status",
      "display_name": "Identity Status",
      "description": "Current agent identity — compact name and class display",
      "schema": {
        "properties": {
          "callsign": { "type": "string" },
          "class": { "type": "string" }
        }
      },
      "data_topic": "identity.v1.status"
    }
  ]
}
```

The agent sees: "identity capsule has a config form and a status widget. Here are their schemas and
data topics." The agent decides if, when, and where to place them.

Two catalogs are in play:

- **Frontend catalog** (A2UI standard): what primitive types the renderer supports (Row, Text, etc.)
- **Capsule component registry** (bus-discovered): what data/interactions capsules provide

The agent bridges the two — mapping capsule components onto A2UI primitives.

### Composition flow

1. **Boot**: Frontend publishes `ui.v1.catalog.response` with supported A2UI component types.
2. **Discovery**: Agent triggers `ui.v1.request.describe` — capsules respond with available
   components.
3. **Compose**: Agent emits `createSurface` + `updateComponents` with the default layout.
4. **Interact**: On user input ("show tools sidebar"), agent emits `updateComponents` with a
   restructured tree.
5. **Live data**: Capsules publish `ui.v1.data_model.update` to push values to their components.
   Frontend re-renders via A2UI data binding.
6. **Persist**: Agent saves layout composition to session state. On reconnect, restores last layout.

### Error handling

- **Unknown component type**: Frontend renders as `Text` with the component's `id`, or omits.
  Agent sees `ui.v1.error` and can adapt.
- **Invalid tree structure**: Rejected with `ui.v1.error`; last valid tree retained.
- **Agent not ready**: Frontend renders a minimal loading state until the first `createSurface`
  arrives. This is the only hardcoded UI in the frontend.

### Capability gating

Surface creation and component updates are capability-gated. Only the orchestrating agent (or
capsules with `ui.surface.write` capability) can emit `createSurface` and `updateComponents`.
Any capsule can emit `updateDataModel` for components it owns (scoped by component ID prefix
matching its capsule namespace).

This prevents a rogue capsule from hijacking the layout while allowing capsules to update their data.

# Drawbacks
[drawbacks]: #drawbacks

- **Agent latency on boot**: First render depends on the agent composing a layout. Mitigation: cache
  the last layout and restore it immediately; the agent updates once ready.

- **LLM token cost**: Component registry, layout state, and A2UI grammar consume context window
  tokens. For simple chat, this is overhead.

- **Layout instability**: A poorly prompted agent could produce jarring layout changes. Mitigation:
  system prompt instructs conservative layout behavior; changes only on explicit user request.

- **A2UI dependency**: External spec (Google, Apache 2.0). Mitigation: pin to a version; Apache 2.0
  permits forking.

- **Testing complexity**: Dynamic layouts make snapshot testing harder.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why A2UI?

A2UI solves the component description problem with a well-designed catalog, surface lifecycle, data
binding, and security model. Building a custom protocol would duplicate this work. A2UI is
Apache 2.0, open-source, and has ecosystem adoption.

## Why not MCP Apps?

MCP Apps sends HTML/JS in sandboxed iframes — web-centric, incompatible with terminal rendering.
A2UI's declarative approach maps to any native widget toolkit.

## Why not predefined zones/slots?

Zones impose structure the agent should own. The agent CAN create a zone-like layout if it chooses,
but is not constrained to one. Structure is an agent choice, not a system constraint.

## What if the agent produces bad layouts?

The frontend validates component trees. Invalid structures are rejected; the last valid tree is
retained. Users can always say "reset to default."

# Prior art
[prior-art]: #prior-art

- **A2UI (Google)** — The protocol we adopt. Declarative JSON component descriptions, catalog-based
  security, surface lifecycle. Apache 2.0. Our contribution is the agent-as-composer pattern.

- **MCP Apps (Anthropic + OpenAI)** — HTML/JS in sandboxed iframes. Richer rendering but requires a
  web runtime. Not viable for terminals.

- **AG-UI (CopilotKit)** — Event-based agent-frontend streaming protocol. Complementary to A2UI
  (transport layer vs. component format). Worth evaluating for IPC transport in future work.

- **Zellij / tmux** — Terminal multiplexers with user-composed panes. Same concept but manual. This
  RFC is the AI-native version of terminal pane composition.

- **Emacs** — Editor as a Lisp-driven UI canvas. Windows, buffers, and frames composed
  programmatically. Philosophically similar, with the agent replacing Emacs Lisp.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **A2UI version pinning**: Which version do we adopt? v0.10 is latest draft. May need to pin
  v0.8 (public preview) and upgrade.

- **Streaming component trees**: How does incremental JSONL interact with double-buffered
  rendering? Render partial trees or wait for complete updates?

- **Focus management**: When the agent restructures the layout, where does keyboard focus go?
  A2UI lacks a focus primitive. May need a convention (e.g., `focused: true` property).

- **Layout persistence**: How is the last-known layout serialized? Raw A2UI component list or a
  higher-level representation?

- **Chat message rendering**: Is the message stream a `List` of A2UI `Text` components, or a
  special-cased renderer? Full A2UI enables richer messages but adds complexity.

- **Input handling**: How do A2UI actions map to the IPC event system? Is a keystroke in a
  `TextField` an A2UI action or an IPC event?

- **Performance**: How many components can ratatui render at 60fps? Large trees may need
  virtualization.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Saveable layouts**: Named layouts ("coding", "debugging", "minimal") the user switches between.

- **Layout marketplace**: Capsule authors publish recommended layouts alongside their capsules.

- **Multi-surface**: Multiple surfaces for multi-monitor or tabbed interfaces.

- **Sixel/Kitty graphics**: Terminals with image protocol support render `Image` components inline.

- **Web frontend**: A2UI components rendered as React/HTML. Same layout description, richer output.

- **Collaborative UI**: Multiple users in one session see the same agent-composed layout.

- **Theme as data model**: Colors and styling as data model patches. Dark/light/high-contrast
  become agent-controlled, adapting to terminal capabilities.
