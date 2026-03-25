- Feature Name: `agent_composed_ui`
- Start Date: 2026-03-25
- RFC PR: [rfcs#25](https://github.com/unicity-astrid/rfcs/pull/25)
- Tracking Issue: [astrid#629](https://github.com/unicity-astrid/astrid/issues/629)

# Summary
[summary]: #summary

Adopt Google's A2UI (Agent-to-User Interface) protocol as the rendering contract between capsules,
the agent, and frontends. The agent owns the full layout composition — there are no predefined zones,
slots, or hardcoded regions. The TUI (and any future frontend) is a dumb renderer that draws whatever
component tree the agent describes. Capsules provide components and data sources; the agent decides
what goes where.

# Motivation
[motivation]: #motivation

The current CLI/TUI has a hardcoded layout: chat area, input box, status bar. Colors are hardcoded
for dark terminals. Status indicators (model name, context usage) are fragile because the CLI owns
their rendering but doesn't own the data. There is no way for capsules to present structured UI
(forms, tables, panels) back to the user. And there is no way for the user to customize the layout
without code changes.

These are all symptoms of the same root problem: **the frontend owns both structure and content.**
The frontend should own neither. Content comes from capsules. Structure comes from the agent.

The agent is uniquely positioned to compose UI because it understands:
- What capsules are loaded and what components they offer
- What the user is currently doing (coding, chatting, configuring, debugging)
- What the user has asked for ("show me tools on the right", "minimal interface")
- What information is relevant right now vs. noise

By making the agent the UI composer, we get:
- **Adaptive layouts** — the agent reshapes the UI based on task context
- **Natural language customization** — "put my config on the right" just works
- **No hardcoded structure to maintain** — the default layout is just the agent's starting opinion
- **Multi-frontend for free** — each frontend renders A2UI components natively; the agent's layout
  description is frontend-agnostic
- **Personality-driven defaults** — different agent identities can have different default layouts

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## For capsule developers

Your capsule exports **components** — UI building blocks that the agent can place anywhere. You
declare what you can provide; you do not decide where it goes.

A component is registered via IPC and described as an A2UI component type with a data model:

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
data model via IPC events — the component re-renders automatically through A2UI's data binding.

```rust
// React capsule updates model status after each LLM response
sdk::publish("ui.v1.data_model.update", json!({
    "surfaceId": "root",
    "patch": [
        { "op": "replace", "path": "/model-status/model", "value": "gpt-5.4" },
        { "op": "replace", "path": "/model-status/context_usage", "value": 0.47 }
    ]
}));
```

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

Your layout preferences persist across sessions. Different agent personalities may have different
default layouts.

## For frontend implementors

Your frontend is a **dumb A2UI renderer**. You receive a component tree and render it using your
native widget toolkit. You do not decide layout, structure, or what content appears where.

Responsibilities:
- Map A2UI component types to native widgets (`Row` → flex row, `Text` → text widget, etc.)
- Handle user input events and route them back via IPC
- Advertise your component catalog (what A2UI types you can render)
- Gracefully degrade for unsupported component types (render as text or omit)

The CLI/TUI maps A2UI to ratatui. A future web frontend would map to React/HTML. A Discord frontend
would map to Discord components. Same agent layout description, different native rendering.

## The agent's role

The agent owns a single root A2UI surface that IS the entire screen. It composes the full component
tree using A2UI layout primitives (`Row`, `Column`, `Card`, `Tabs`, `List`) and capsule-provided
components.

On startup, the agent emits a default layout — this looks like a traditional TUI:

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
      { "id": "status", "component": "Row", "children": ["breadcrumb", "model-status"], "justify": "spaceBetween" },
      { "id": "breadcrumb", "component": "Text", "text": "~ > dev > astrid", "variant": "caption" },
      { "id": "model-status", "component": "Text", "text": "gpt-5.4 | 47%", "variant": "caption" },
      { "id": "main", "component": "Column", "weight": 1, "children": ["messages"] },
      { "id": "messages", "component": "List", "direction": "vertical", "children": [] },
      { "id": "input", "component": "TextField", "placeholder": "Message..." }
    ]
  }
}
```

This is the current TUI — recreated purely from A2UI primitives. But the agent can restructure it
at any time. When the user asks for a sidebar, the agent replaces the `main` component with a `Row`
containing the chat column and a new sidebar column. No code change. No predefined zones. Just a
different component tree.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Protocol

We adopt A2UI v0.10 (or latest stable at implementation time) as the wire protocol. No modifications
to the core spec. The protocol defines:

### Message types (A2UI standard)

| Message | Purpose |
|---------|---------|
| `createSurface` | Create a new rendering surface with a catalog reference |
| `updateComponents` | Add, modify, or remove components in a surface |
| `updateDataModel` | Patch the surface's data model (RFC 6902 JSON Patch) |
| `deleteSurface` | Destroy a surface |

### Component catalog

The CLI frontend advertises a catalog of A2UI component types it can render. The catalog is the
security boundary — only allowlisted types are rendered. The initial CLI catalog:

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

Unsupported components degrade to `Text` showing the component's text content, or are omitted.

### IPC integration

A2UI messages travel over the Astrid IPC event bus under the `ui.v1.*` topic namespace:

| IPC Topic | Payload | Direction |
|-----------|---------|-----------|
| `ui.v1.surface.create` | A2UI `createSurface` | Agent → Frontend |
| `ui.v1.components.update` | A2UI `updateComponents` | Agent → Frontend |
| `ui.v1.data_model.update` | A2UI `updateDataModel` | Capsule/Agent → Frontend |
| `ui.v1.surface.delete` | A2UI `deleteSurface` | Agent → Frontend |
| `ui.v1.action` | `{ surfaceId, componentId, action, data }` | Frontend → Agent |
| `ui.v1.catalog.query` | `{}` | Agent → Frontend |
| `ui.v1.catalog.response` | `{ components: [...] }` | Frontend → Agent |

### Component discovery — bus-based, same pattern as tools

UI component discovery follows the exact same pattern as tool discovery. Today, tools work like this:

1. Prompt-builder triggers `tool.v1.request.describe` (hook fan-out to all capsules)
2. Each capsule's `tool_describe` interceptor responds with its tool schemas
3. Prompt-builder collects and deduplicates

UI components work identically:

1. Agent (or a coordinator) triggers `ui.v1.request.describe` (hook fan-out)
2. Each capsule's `ui_describe` interceptor responds with its available components
3. Agent collects the component registry

No manifest extension is needed. The `Capsule.toml` declares the interceptor and IPC capabilities
(just like tools do today), and the actual component definitions are returned at runtime:

```toml
# In Capsule.toml — same pattern as tool_describe
[[interceptor]]
event = "ui.v1.request.describe"
action = "ui_describe"

[capabilities]
ipc_publish = ["ui.v1.response.describe.*"]
```

The capsule's `ui_describe` handler returns what it can provide:

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
      "description": "Current agent identity — name and class as text",
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

This tells the agent: "I have a config form and a status widget you can place anywhere. Here are
their schemas and the IPC topics for live data." The agent decides if, when, and where to use them.

A capsule can offer multiple components of different shapes — a full config form for a sidebar, a
compact status line for a header bar, a detailed dashboard for a modal. Same data, different
presentations. The agent picks what fits the current layout.

### Agent composition flow

1. **Boot**: Frontend publishes `ui.v1.catalog.response` with supported A2UI component types
   (what the renderer can draw: Row, Column, Text, TextField, etc.).
2. **Discovery**: Agent triggers `ui.v1.request.describe` — all capsules respond with their
   available components (what data/interactions they can provide).
3. **Compose**: Agent maps capsule components onto A2UI primitives and emits `createSurface` +
   `updateComponents` to render the default layout. The agent decides the tree structure.
4. **React**: On user input ("show tools sidebar"), agent emits new `updateComponents` with a
   restructured tree. Capsule components move, resize, appear, or disappear.
5. **Live data**: Capsules publish `ui.v1.data_model.update` to push new values to their
   components. Frontend re-renders affected components automatically via A2UI data binding.
6. **Persist**: Agent saves the current layout composition to session state. On reconnect, it
   restores the last layout rather than resetting to default.

There are two catalogs in play:

- **Frontend catalog** (A2UI standard): what primitive types the renderer supports (Row, Text, etc.)
- **Capsule component registry** (Astrid-specific): what data/interactions capsules can provide

The agent bridges the two — it takes capsule components and composes them into A2UI primitives that
the frontend can render.

### Pre-baked default

The agent's system prompt includes a default layout composition. This is what renders on first boot
before any user customization. It replicates the current hardcoded TUI:

- Top row: breadcrumb path (left), model name + context usage (right)
- Main area: scrollable message list
- Bottom: text input
- Status indicators update via data model patches from the react capsule

This default is not special. It is not hardcoded in the frontend. It is just the agent's opening
move, expressed as A2UI components. The agent can replace it entirely at any time.

### Error handling

- **Unknown component type**: Frontend logs a warning and renders as `Text` with the component's
  `id` as content, or omits. The agent sees a `ui.v1.error` event and can adapt.
- **Invalid tree structure**: Frontend validates parent-child relationships (e.g., `weight` only
  valid as direct child of `Row`/`Column`). Invalid structures are rejected with a
  `ui.v1.error` event; the last valid tree is retained.
- **Agent not ready**: Frontend renders a minimal loading state (spinner + "composing...") until
  the first `createSurface` arrives. This is the only hardcoded UI in the frontend.

## Capability gating

A2UI surface creation and component updates are capability-gated. Only the orchestrating agent (or
capsules with explicit `ui.surface.write` capability) can emit `createSurface` and
`updateComponents`. Any capsule can emit `updateDataModel` for components it owns (scoped by
component ID prefix matching its capsule namespace).

This prevents a rogue capsule from hijacking the layout while still allowing capsules to update
their own data.

# Drawbacks
[drawbacks]: #drawbacks

- **Agent latency on boot**: The first render depends on the agent composing a layout. Until the
  LLM responds, users see a loading state. This adds perceived startup time. Mitigation: cache
  the last layout and restore it immediately; the agent can update it once ready.

- **LLM token cost**: The component registry, current layout state, and A2UI grammar all consume
  context window tokens on every agent turn. For simple chat interactions, this is overhead that
  a hardcoded TUI doesn't have.

- **Layout instability**: A poorly prompted agent could produce jarring layout changes mid-
  conversation. Mitigation: layout changes should only happen on explicit user request or major
  context shifts, not on every turn. The system prompt should instruct the agent to be
  conservative with layout changes.

- **A2UI dependency**: We take a dependency on an external spec (Google, Apache 2.0). If the spec
  evolves in directions incompatible with our needs, we'd need to fork or freeze at a version.
  Mitigation: pin to a specific version; the spec is Apache 2.0 so forking is always an option.

- **Testing complexity**: UI tests must now account for dynamic layouts rather than fixed structure.
  Snapshot testing becomes harder when the layout can change between runs.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why A2UI over a custom protocol?

A2UI is a well-designed, open-source (Apache 2.0) protocol that solves the component description
problem. Its component catalog, surface lifecycle, data binding, and security model are exactly what
we need. Building a custom protocol would duplicate this work and cut us off from potential ecosystem
interop.

## Why not MCP Apps?

MCP Apps (Anthropic + OpenAI) sends HTML/JS rendered in sandboxed iframes. This is web-centric and
fundamentally incompatible with terminal rendering. A2UI's declarative approach maps naturally to any
native widget toolkit, including ratatui.

## Why not predefined zones/slots?

Zones impose structure that the agent should own. A zone system means every frontend must agree on a
zone vocabulary, every capsule must target specific zones, and layout customization is limited to
"which zones are visible." The agent-composed approach is strictly more flexible — the agent CAN
create a zone-like layout if it chooses to, but it is not constrained to one.

## Why not keep the hardcoded TUI?

The hardcoded TUI cannot adapt to context, cannot be customized by users without code changes,
creates tight coupling between the CLI and capsule data, and must be maintained as a parallel
rendering system alongside any future A2UI support. Starting from A2UI avoids this dual-maintenance
problem.

## What if the agent produces bad layouts?

The frontend validates the component tree before rendering. Invalid structures are rejected and the
last valid tree is retained. The system prompt instructs the agent to be conservative with layout
changes. And the user can always say "reset to default" to get back to the pre-baked layout.

# Prior art
[prior-art]: #prior-art

- **A2UI (Google)** — The protocol we adopt. Declarative JSON component descriptions, catalog-based
  security, surface lifecycle management. v0.8+ public preview, Apache 2.0. Our contribution is
  the agent-as-composer pattern on top of A2UI's primitives.

- **MCP Apps (Anthropic + OpenAI)** — HTML/JS in sandboxed iframes. Richer rendering but requires a
  web runtime. Not viable for terminal UIs. The "opaque payload" approach vs. A2UI's "declarative
  catalog" approach.

- **AG-UI (CopilotKit)** — Event-based protocol for real-time agent-frontend communication. Covers
  streaming, shared state, and human-in-the-loop. Complementary to A2UI (AG-UI = transport, A2UI =
  component format). Worth evaluating as the IPC transport layer in future work.

- **Zellij / tmux** — Terminal multiplexers that let users compose panes. Similar concept (user
  decides layout) but manual rather than agent-driven. Our approach is the AI-native version of
  terminal pane composition.

- **Emacs** — The original "editor as a Lisp-driven UI canvas." Windows, buffers, and frames are
  composed programmatically. Astrid's agent-composed UI is philosophically similar, with the agent
  replacing Emacs Lisp as the composition layer.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **A2UI version pinning**: Which exact A2UI version do we adopt? v0.10 is the latest draft but not
  yet stable. We may need to pin v0.8 (public preview) and upgrade later.

- **Streaming component trees**: A2UI supports incremental JSONL streaming. How does this interact
  with ratatui's double-buffered rendering? Do we render partial trees or wait for a complete update?

- **Focus management**: When the agent restructures the layout, where does keyboard focus go? The
  agent should be able to specify a focus target, but A2UI doesn't have a focus primitive. We may
  need a small extension or convention (e.g., a `focused: true` property).

- **Layout persistence format**: How is the "last known layout" serialized for session restore?
  The raw A2UI component list, or a higher-level representation?

- **Chat message rendering**: Is the message stream itself an A2UI `List` of `Text` components, or
  does it remain a special-cased renderer? Full A2UI message rendering enables richer messages
  (inline tables, cards, images on supported terminals) but adds complexity.

- **Input handling**: A2UI defines actions routed from client to agent. How does this map to the
  existing IPC event system? Is a keystroke in a `TextField` an A2UI action or an IPC event?

- **Performance budget**: How many components can ratatui render at 60fps? Large component trees
  (hundreds of items in a tool list) may need virtualization.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Saveable layouts**: Users save named layouts ("coding", "debugging", "minimal") and switch
  between them. The agent restores a saved layout by name.

- **Layout marketplace**: Capsule authors publish recommended layouts alongside their capsules.
  "Install the monitoring capsule and its dashboard layout."

- **Multi-surface**: Instead of one root surface, multiple surfaces for multi-monitor or tabbed
  interfaces. Each tab is an independent A2UI surface.

- **Sixel/Kitty graphics**: Terminals that support image protocols could render A2UI `Image`
  components as inline graphics. The catalog advertises this capability.

- **Web frontend**: A web-based Astrid frontend renders A2UI components as React/HTML. The same
  agent layout description works across CLI and web — the rendering quality just improves.

- **Collaborative UI**: Multiple users connected to the same session see the same layout. The agent
  composes for the group, not just one user.

- **Theme as data model**: The agent controls the color palette via A2UI data model updates. "Dark
  mode", "light mode", and "high contrast" become data model patches, not hardcoded theme structs.
  This solves the terminal background detection problem (#626) — the agent detects or asks, then
  patches the theme.
