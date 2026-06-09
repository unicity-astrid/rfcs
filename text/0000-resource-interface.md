- Feature Name: `resource_interface`
- Start Date: 2026-06-09
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

Define the **resource interface** — a capsule-served contract for exposing
read-only, addressable *resources* (named blobs of context) over the bus, so
they surface to MCP clients connected through `astrid mcp serve` as MCP
`resources`. It is the structural parallel of the existing `tool` interface: an
`astrid-bus:resource@1.0.0` package of typed payload records
(`describe-request`/`describe-response`, `read-request`/`read-response`) carried
on a `resource.v1.*` topic family, served by handler-bound subscribers, fanned
out for discovery and routed per-URI for reads. Resources are **fetchable, not
events** — request/response, not broadcast. They are pull-only, per-principal
scoped, and never carry secrets.

This RFC defines the fetchable resource primitive only. The push-shaped MCP
server primitives (sampling, logging) are out of scope by construction —
Astrid serves MCP passively and cannot push to a client. The MCP `prompts`
primitive is deferred (see [Future possibilities]).

# Motivation
[motivation]: #motivation

An MCP server exposes three primitives: **tools** (actions the client invokes),
**resources** (context the client fetches and re-reads), and **prompts** (named
templates). Astrid already has a capsule-served `tool` interface. It has no
resource surface at all.

The consequence is concrete. A client connected through `astrid mcp serve` can
*act* through Astrid, but it cannot *attach Astrid-held context as a resource*.
To read something Astrid holds — the principal's capability set, its budget, the
installed capsule inventory — the client must spend a tool call and a context
slot on a one-shot result, rather than reference a cheap, re-fetchable resource
URI. Resources exist precisely for re-readable context that should not consume
the agent loop.

Three properties drive the design:

1. **Fetchable, not broadcast.** Listing resources and reading a resource are
   request/response operations — interceptors, in Astrid terms (a `[subscribe]`
   entry bound to a `handler`), not fire-and-forget events. The contract must
   model RPC, not pub/sub.
2. **Served by capsules, discovered by the broker.** Resources must populate
   *automatically* from whatever capsules can serve them — the same fan-out the
   tool surface already uses. The broker stays a dumb aggregator; it never holds
   a hardcoded resource list, and it is blind to resource content. A capsule
   that holds per-principal state advertises and serves its own resources.
3. **A contract, not an implementation detail.** Hardcoding a resource set in
   the broker or the `mcp serve` shim would put capsule-domain knowledge in the
   router and leave third-party capsules with no way to contribute resources.
   Standardizing the contract is what lets any capsule — including ones written
   by third parties — expose governed, re-readable context with no change to the
   broker or the shim.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

A **resource** is a named, addressable, read-only blob of context: a `uri`
(conventionally `astrid://…`), a human-facing `name`, and an optional
`description` and `mime-type`. A capsule that holds context worth reading
*exports the resource interface*: it answers "what resources do you serve?" and
"read this URI."

There are two operations, mirroring the tool interface's describe/execute:

- **describe** — enumerate. A *fan-out*: the broker asks every resource-serving
  capsule for its list and aggregates the responses.
- **read** — fetch one URI. *Routed* to the capsule that owns the URI.

```
  MCP client            astrid mcp serve            broker (sage-mcp)         serving capsule
      │  resources/list        │                          │                         │
      ├───────────────────────►│  mcp.resources.list      │                         │
      │                        ├─────────────────────────►│  resource.v1.request.   │
      │                        │                          │      describe (fan-out) │
      │                        │                          ├────────────────────────►│
      │                        │                          │◄─ describe.<corr-id> ───┤
      │◄──────── list ─────────┤◄──── aggregated list ────┤                         │
      │                        │                          │                         │
      │  resources/read <uri>  │  mcp.resource.read       │  resource.v1.request.   │
      ├───────────────────────►├─────────────────────────►│      read → owning src  │
      │◄──────── contents ─────┤◄──── read-response ──────┤◄── response.<corr-id> ──┤
```

A serving capsule declares the contract in its `Capsule.toml`, exactly as a tool
capsule declares `tool.v1.*`:

```toml
[subscribe]
"resource.v1.request.describe" = { wit = "@unicity-astrid/wit/resource/describe-request", handler = "resource_describe" }
"resource.v1.request.read"     = { wit = "@unicity-astrid/wit/resource/read-request",     handler = "resource_read" }

[publish]
"resource.v1.response.describe.*" = { wit = "@unicity-astrid/wit/resource/describe-response" }
"resource.v1.response.read.*"     = { wit = "@unicity-astrid/wit/resource/read-response" }
```

From the client's side, `astrid mcp serve` advertises the `resources` capability
at `initialize`; `resources/list` and `resources/read` map onto the broker front
doors. The client lists and reads on demand. **Nothing is pushed** — there is no
resource-change subscription, only the same best-effort `list_changed` nudge the
tool surface already emits when capsules load or unload.

Worked example: a small introspection capsule serves `astrid://capabilities`
(the invoking principal's held grants) and `astrid://budget` (its CPU-fuel and
memory usage vs. limits). On `describe` it returns both definitions; on
`read("astrid://capabilities")` it returns the live grant list for *that
principal only*. The broker never learns what the resource means — it discovers
the URIs, routes the read, and forwards the bytes.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Interface (WIT)

```wit
package astrid-bus:resource@1.0.0;

/// Resource interface — capsules serve read-only, addressable context.
///
/// A capsule exporting this interface answers `resource.v1.request.describe`
/// (enumerate, fan-out) and `resource.v1.request.read` (fetch one URI, routed
/// to the owning capsule), replying on `resource.v1.response.{describe,read}.<correlation-id>`.
interface resource {
    use astrid-bus:types/types.{resource-definition};

    /// Request asking a resource-providing capsule to enumerate its resources.
    /// Topic: `resource.v1.request.describe`. Every responder returns its full
    /// list, scoped to the invoking principal.
    record describe-request {
        /// Correlation ID for the response. The capsule replies on
        /// `resource.v1.response.describe.<correlation-id>`.
        correlation-id: string,
    }

    /// Response listing the resources a capsule provides.
    /// Topic: `resource.v1.response.describe.<correlation-id>`.
    record describe-response {
        correlation-id: string,
        resources: list<resource-definition>,
    }

    /// Request to read one resource by URI.
    /// Topic: `resource.v1.request.read`. The broker routes it to the capsule
    /// that owns the URI (determined from the describe snapshot).
    record read-request {
        uri: string,
        correlation-id: string,
    }

    /// Contents of a single resource.
    /// Topic: `resource.v1.response.read.<correlation-id>`.
    ///
    /// `contents` is UTF-8 text, or base64 when `mime-type` denotes a binary
    /// type. `is-error` true means the read failed (unknown URI, denied,
    /// unavailable); `contents` then carries a human-readable reason.
    record read-response {
        correlation-id: string,
        uri: string,
        contents: string,
        mime-type: option<string>,
        is-error: bool,
    }
}
```

`resource-definition` lives in the shared `astrid-bus:types` package alongside
`tool-definition`, since the broker and shim both depend on it:

```wit
/// A resource a capsule exposes for reading.
record resource-definition {
    uri: string,
    name: string,
    description: option<string>,
    mime-type: option<string>,
}
```

## Topics

| Topic | Direction | Payload |
|-------|-----------|---------|
| `resource.v1.request.describe` | broker → serving capsules (fan-out) | `describe-request` |
| `resource.v1.response.describe.<correlation-id>` | serving capsule → broker | `describe-response` |
| `resource.v1.request.read` | broker → owning capsule (routed) | `read-request` |
| `resource.v1.response.read.<correlation-id>` | owning capsule → broker | `read-response` |

Discovery mirrors `tool.v1.request.describe`: the broker subscribes to the
response pattern, publishes one `describe-request` with a fresh `correlation-id`,
drains responses for a bounded window, aggregates, and dedupes by `uri` (first
responder wins; a collision is logged). The kernel-stamped `source-id` on each
response envelope identifies the owning capsule, so the broker builds a
`uri → source-id` routing table from the snapshot. A `read` for a URI not in the
table triggers a re-describe before failing.

## MCP bridge (broker front doors)

The `sage-mcp` broker exposes two front doors that the `mcp serve` shim calls,
parallel to the existing `astrid.v1.request.mcp.tools.list` / `…tool.call`:

| Front door | Gating | Behaviour |
|------------|--------|-----------|
| `astrid.v1.request.mcp.resources.list` | ungated (read-only discovery, as with `tools.list`) | fan out `resource.v1.request.describe`, aggregate, reply |
| `astrid.v1.request.mcp.resource.read` | confused-deputy `trusted_ingress` gate (as with `tool.call`) | route the read to the owning capsule, reply |

## MCP shim (`astrid mcp serve`)

- `get_info` adds `capabilities.resources = { list_changed: true, subscribe:
  false }`. `subscribe: false` is load-bearing — Astrid cannot honour
  server-initiated resource-update notifications.
- `list_resources` → `astrid.v1.request.mcp.resources.list`, reshape the
  aggregated definitions to the MCP `ListResourcesResult`.
- `read_resource(uri)` → `astrid.v1.request.mcp.resource.read`, reshape the
  `read-response` to the MCP `ReadResourceResult` (`text` or `blob` by
  `mime-type`).
- `notifications/resources/list_changed` reuses the existing `capsules_loaded`
  broadcast — the same nudge that already drives `tools/list_changed`.

## Semantics

- **Per-principal, always.** Every `describe` and `read` carries the invoking
  principal (kernel-stamped `source-id → principal`). A serving capsule MUST
  scope both the returned definitions and the read contents to that principal. A
  read addressing another principal's URI MUST fail closed (`is-error: true`).
- **Never a secret.** A serving capsule MUST NOT expose secret material —
  credentials, tokens, key bytes — as a resource. Resources are non-sensitive
  context by contract.
- **Read-through.** A `read` reflects live state at read time. The broker MAY
  cache the *describe* snapshot with a TTL (as it does for tools); it MUST NOT
  cache `read` contents.
- **Gating.** Discovery (`list`) is ungated. Reads pass the broker's
  confused-deputy ingress allow-set, identical to tool execution — a read can
  touch per-principal state, so it is gated like an action.
- **Errors.** Unknown URI, denied, or unavailable → `read-response.is-error =
  true` with a human-readable `contents`. A broker-side drain timeout surfaces as
  an MCP error to the client.
- **Concurrency.** A fresh `correlation-id` scopes every request's responses, so
  concurrent describes and reads never collide on a response topic.
- **No subscriptions.** The contract defines no resource-change subscription.
  `list_changed` is a best-effort capability nudge, not a per-resource signal.

## Capability

Serving a resource requires **no new capability** — a capsule serves only what it
can already read. A capsule that serves per-principal kernel state (for example
`astrid://capabilities` or `astrid://budget`) needs whatever self-scoped view
capability that data already requires (e.g. a `self:…:view` grant); that is the
serving capsule's concern, not the contract's. The contract adds no privilege.

# Drawbacks
[drawbacks]: #drawbacks

- A third capsule interface to maintain (`tool`, `resource`, and the existing
  prompt-assembly interface), each with its own records, topics, and SDK
  surface.
- Read routing depends on the broker's `uri → source-id` table from the describe
  snapshot; a stale snapshot can misroute a read (mitigated by re-describe on
  miss, at the cost of latency on the miss path).
- Resources overlap conceptually with read-only tools. Without guidance, a
  capsule author may be unsure whether to model a read as a tool or a resource.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

- **Why mirror the `tool` interface.** The fan-out / aggregate / route machinery
  already exists and is proven for tools. Reusing the shape — an interceptor
  request/response contract over a `*.v1.*` topic family — keeps the broker
  uniform and the learning curve flat. An interceptor is the correct primitive
  because resources are *fetchable*; modelling them as events would force a
  request/response shape onto a broadcast mechanism.
- **Alternative: expose resources as read-only tools.** Rejected. A tool read
  costs a tool-call slot and an agent-loop turn, and MCP clients treat resources
  and tools as distinct surfaces (resources can be attached as re-readable
  context; tools cannot). Collapsing them loses the property that motivates the
  feature.
- **Alternative: hardcode a fixed resource set in the broker or shim.** Rejected.
  It puts capsule-domain knowledge in the router, is not extensible to
  third-party capsules, and violates the broker-is-dumb invariant.
- **Alternative: a host function rather than a bus contract.** Rejected.
  Resources are capsule-served domain data, not a kernel primitive; the kernel
  has no business holding a resource registry.
- **Impact of not standardizing.** Every capsule that wants to surface context
  invents an ad-hoc read-only tool, and clients get no uniform, attachable
  resource surface — the exact fragmentation the `tool` interface was created to
  avoid.

# Prior art
[prior-art]: #prior-art

- **Model Context Protocol — `resources`.** `resources/list`, `resources/read`,
  resource templates, and `resources/subscribe`. This RFC adopts list and read,
  defers templates, and excludes subscribe — the latter requires a server →
  client push channel Astrid does not have.
- **Astrid's own `tool` interface** (`astrid-bus:tool@1.0.0`). The direct
  structural precedent: a record-only WIT interface bound to a `*.v1.*` topic
  family, served by handler-bound subscribers and aggregated by the broker.
- **LSP** document/resource model and content-addressed stores (loose analogy):
  named, addressable, re-fetchable read surfaces consumed by a client that does
  not own the underlying storage.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Does `resource-definition` belong in the shared `astrid-bus:types` package
  (the position taken here, matching `tool-definition`) or inline in the
  `resource` interface?
- Read routing: targeted (broker publishes to the owning capsule, as written) vs.
  fan-out-and-self-filter (every capsule sees the read, the owner answers). The
  targeted form mirrors `tool.v1.execute.<name>`; the fan-out form is simpler but
  noisier.
- Binary contents: `contents: string` (UTF-8, or base64 keyed by `mime-type`) as
  written, vs. a dedicated `list<u8>` blob arm. The string form keeps the record
  flat; the blob arm avoids base64 overhead for large binaries.
- Whether the broker's describe-snapshot TTL and drain window should match the
  tool surface's exactly, or be tuned independently.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Resource templates** — parameterized URIs (`astrid://capsule/{name}/config`)
  for addressing a family of resources without enumerating each.
- **A subscribe surface** — `resources/subscribe` and per-resource update
  notifications — *if and when* Astrid grows a genuine server → client push
  channel. It cannot today; the absence is by construction, not oversight.
- **The MCP `prompts` primitive** as a sibling interface for named prompt
  templates. This is distinct from the existing `astrid-bus:prompt` interface,
  which is internal prompt *assembly*, not a client-facing template library; that
  interface should be renamed (e.g. `prompt-builder`, aligning the package with
  the `prompt_builder.v1.*` topics and the `prompt-builder` capsule it already
  uses) before `prompt` is reused for the MCP sense.
- **An SDK `#[astrid::resource(...)]` macro** mirroring `#[astrid::tool(...)]`,
  so a capsule declares served resources with an attribute and the SDK generates
  the describe/read handlers.
- **A reference introspection capsule** serving `astrid://capabilities`,
  `astrid://budget`, and `astrid://capsules` — the first concrete consumer of
  this contract, and the per-principal self-awareness surface for agent runtimes
  hosted on Astrid.
