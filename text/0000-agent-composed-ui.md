- Feature Name: `agent_composed_ui`
- Start Date: 2026-03-25
- RFC PR: [rfcs#25](https://github.com/unicity-astrid/rfcs/pull/25)
- Tracking Issue: [astrid#629](https://github.com/unicity-astrid/astrid/issues/629)

# Summary
[summary]: #summary

Adopt Google's A2UI (Agent-to-User Interface) protocol as the rendering contract between the agent
and frontends. The agent is a first-class bus participant — it observes IPC traffic, understands what
data capsules are producing, and composes A2UI surfaces directly on the bus. Frontends are dumb
renderers. Capsules don't change — their existing IPC events are the data sources.

# Motivation
[motivation]: #motivation

The frontend should not own structure or content. Content comes from capsules. Structure comes from
the agent.

The agent is uniquely positioned to compose UI because it:
- Observes the IPC bus and sees what capsules are communicating
- Understands what data is flowing and what's relevant to show
- Knows what the user is doing and what they've asked for
- Can adapt the layout to context without code changes

By making the agent the UI composer:
- **Adaptive layouts** — the agent reshapes the UI based on task context
- **Natural language customization** — "put my config on the right" just works
- **No hardcoded structure** — the default layout is the agent's starting opinion
- **Multi-frontend for free** — each frontend renders A2UI natively
- **Personality-driven defaults** — different agent identities can have different layouts
- **Theme as data** — colors and styling become data the agent controls
- **Zero capsule changes** — existing IPC data structures are the data sources

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## For capsule developers

Nothing changes. Your capsule already publishes data on IPC topics with defined structures. The
agent observes the bus, sees your data, and composes UI from it. You don't register UI components.
You don't declare what you can provide. Your existing IPC events are the data sources.

For example, the identity capsule already publishes spark config on `spark.v1.response.ready`. The
react capsule already publishes model info and usage on `agent.v1.response`. The agent sees this
traffic and can present it however it wants — as a status bar, a sidebar panel, a modal.

If you want the agent to have richer data to display, publish richer IPC events. That's it.

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

The agent is a first-class participant on the IPC bus. It:

1. **Observes** — subscribes to IPC topics and sees what data capsules produce
2. **Composes** — publishes A2UI messages (`a2ui.*`) to describe the UI layout
3. **Reacts** — restructures the UI based on bus traffic, user requests, or context changes

The agent owns a single root A2UI surface that IS the entire screen. It composes the full component
tree using A2UI layout primitives (`Row`, `Column`, `Card`, `Tabs`, `List`) and data from the bus.

On startup, the agent publishes a default layout:

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
sidebar, the agent restructures the tree. No code change. No predefined zones. Just a different
component tree.

The agent can publish this default layout on boot without an LLM call — it's a static starting
point. The LLM is consulted when the user requests layout changes, not for routine rendering.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Layers

Two layers:

1. **IPC bus** (`a2ui.*` topics) — the agent publishes A2UI messages and observes capsule traffic
   directly on the bus. The bus is the agent's native communication layer.
2. **A2UI protocol** — the wire format for rendering messages. Frontends consume these.

The agent does not need tools to compose UI. It publishes to `a2ui.*` topics directly, just as any
capsule publishes to its own topics. The LLM may influence layout decisions (via the react loop),
but the agent capsule handles A2UI publishing as a bus participant.

## Protocol

Adopt A2UI (latest stable at implementation time) as the wire format. No modifications to the
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

A2UI messages travel on the bus under `a2ui.*`:

| IPC Topic | Payload | Direction |
|-----------|---------|-----------|
| `a2ui.surface.create` | A2UI `createSurface` | Agent → Frontend |
| `a2ui.components.update` | A2UI `updateComponents` | Agent → Frontend |
| `a2ui.data_model.update` | A2UI `updateDataModel` | Agent → Frontend |
| `a2ui.surface.delete` | A2UI `deleteSurface` | Agent → Frontend |
| `a2ui.action` | `{ surfaceId, componentId, action, data }` | Frontend → Agent |
| `a2ui.catalog.query` | `{}` | Agent → Frontend |
| `a2ui.catalog.response` | `{ components: [...] }` | Frontend → Agent |

### Versioning

IPC topics carry no version number. A2UI messages self-describe their version via the `version`
field in every payload (e.g., `"version": "v0.10"`). Topics are stable routing addresses; the A2UI
spec version is a payload concern.

When A2UI evolves, the frontend reads the `version` field and handles accordingly. No topic changes,
no capsule manifest changes, no subscriber updates.

### Data sources — the bus IS the component registry

Capsules don't need to register UI components. Their existing IPC events are the data sources. The
agent observes the bus and knows what data is available:

| Existing IPC traffic | Data available | UI possibilities |
|---------------------|----------------|------------------|
| `spark.v1.response.ready` | Agent identity (callsign, class, tone) | Status badge, config panel |
| `agent.v1.response` | Model name, response text, completion status | Status bar, chat stream |
| `llm.v1.stream.*` | Token deltas, usage stats, tool calls | Progress bar, token counter |
| `tool.v1.request.describe` | Tool schemas from all capsules | Tool list panel |
| `registry.v1.active_model_changed` | Active model/provider switch | Model indicator |
| `session.v1.response.get_messages` | Conversation history | Chat view |

The agent maps this data onto A2UI components. No new protocol for capsules. No `ui_describe`
interceptor. Capsules keep doing exactly what they do today.

### Composition flow

1. **Boot**: Agent publishes a default A2UI layout on the bus. No LLM call needed — this is a
   static default the agent capsule emits on load.
2. **Observe**: Agent subscribes to relevant IPC topics and sees capsule data flowing.
3. **Update**: Agent publishes `a2ui.data_model.update` as bus data changes (e.g., model name
   changes, context usage updates).
4. **Interact**: When the user requests a layout change, the LLM is consulted (via the react
   loop). The react loop's response includes A2UI updates that the agent publishes.
5. **Persist**: Layout composition saved to KV. On reconnect, restored without LLM involvement.

### Error handling

- **Unknown component type**: Frontend renders as `Text` with the component's `id`, or omits.
  Agent sees `a2ui.error` and can adapt.
- **Invalid tree structure**: Rejected with `a2ui.error`; last valid tree retained.

### Capability gating

Surface creation and component updates are capability-gated. Only the orchestrating agent (or
capsules with `a2ui.surface.write` capability) can emit `createSurface` and `updateComponents`.

This prevents a rogue capsule from hijacking the layout.

## Multi-principal surfaces

Each principal (`home/agent1`, `home/agent2`) gets its own capsule instances, bus subscriptions, and
A2UI surfaces. The agent for each principal composes its own UI independently.

This means multiple agents can coexist in the same runtime, each with their own layout and data
sources. A2A (Agent-to-Agent) communication between principals is native IPC on the same bus —
agents can observe each other's traffic and coordinate.

A principal's A2UI surface is scoped to its own frontend connections. `home/agent1`'s UI doesn't
bleed into `home/agent2`'s frontend.

# Drawbacks
[drawbacks]: #drawbacks

- **LLM token cost**: When layout changes are requested, the A2UI grammar and current layout state
  consume context window tokens.

- **Layout instability**: A poorly prompted agent could produce jarring layout changes. Mitigation:
  system prompt instructs conservative layout behavior; changes only on explicit user request.

- **A2UI dependency**: External spec (Google, Apache 2.0). Mitigation: pin to a version; Apache 2.0
  permits forking.

- **Testing complexity**: Dynamic layouts make snapshot testing harder.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why A2UI?

A2UI solves the component description problem with a well-designed catalog, surface lifecycle, data
binding, and security model. Building a custom protocol would duplicate this. A2UI is Apache 2.0
and has ecosystem adoption.

## Why not MCP Apps?

MCP Apps sends HTML/JS in sandboxed iframes — web-centric, incompatible with terminal rendering.
A2UI's declarative approach maps to any native widget toolkit.

## Why not predefined zones/slots?

Zones impose structure the agent should own. The agent CAN create a zone-like layout if it chooses,
but is not constrained to one. Structure is an agent choice, not a system constraint.

## Why bus-native instead of tools?

Tools are an abstraction over the bus for LLM interaction. But UI composition is not purely an LLM
concern — the agent publishes a default layout on boot (no LLM), updates data bindings as bus
traffic flows (no LLM), and only consults the LLM for user-requested layout changes. Making the
agent a direct bus participant avoids forcing every UI update through the tool call → LLM → response
cycle.

## Why no component registration?

Capsules already publish structured data on IPC topics. The agent observes the bus and knows what
data exists. Adding a separate UI component registration protocol would duplicate what the bus
already provides and require every capsule to implement a new interceptor for no gain.

# Prior art
[prior-art]: #prior-art

- **A2UI (Google)** — The protocol we adopt. Declarative JSON component descriptions, catalog-based
  security, surface lifecycle. Apache 2.0. Our contribution is the agent-as-bus-participant
  composition model.

- **MCP Apps (Anthropic + OpenAI)** — HTML/JS in sandboxed iframes. Not viable for terminals.

- **AG-UI (CopilotKit)** — Event-based agent-frontend streaming protocol. Complementary to A2UI.

- **Zellij / tmux** — Terminal multiplexers with user-composed panes. Same concept but manual.

- **Emacs** — Editor as a Lisp-driven UI canvas. Philosophically similar, with the agent replacing
  Emacs Lisp as the composition layer.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Streaming component trees**: How does incremental JSONL interact with double-buffered
  rendering? Render partial trees or wait for complete updates?

- **Focus management**: When the agent restructures the layout, where does keyboard focus go?
  A2UI lacks a focus primitive. May need a convention (e.g., `focused: true` property).

- **Layout persistence**: How is the last-known layout serialized for session restore? What
  survives session clear vs. daemon restart?

- **Chat message rendering**: Is the message stream a `List` of A2UI `Text` components, or a
  special-cased renderer?

- **Input handling**: How do A2UI actions map to the IPC event system?

- **Performance**: How many components can ratatui render at 60fps? Large trees may need
  virtualization.

- **Multi-principal surface routing**: How does the frontend/proxy know which principal's A2UI
  surface to render for a given connection?

# Future possibilities
[future-possibilities]: #future-possibilities

- **Saveable layouts**: Named layouts ("coding", "debugging", "minimal") the user switches between.

- **A2A-driven UI**: Agent1 observes Agent2's bus traffic and composes a dashboard of Agent2's
  activity. Multi-agent monitoring via the same A2UI surface.

- **Layout marketplace**: Capsule authors publish recommended layouts alongside their capsules.

- **Multi-surface**: Multiple surfaces for multi-monitor or tabbed interfaces.

- **Sixel/Kitty graphics**: Terminals with image protocol support render `Image` components inline.

- **Web frontend**: A2UI components rendered as React/HTML. Same layout, richer output.

- **Theme as data model**: Colors and styling as data model patches. Dark/light/high-contrast
  become agent-controlled, adapting to terminal capabilities.
