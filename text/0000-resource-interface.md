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
`astrid-bus:resource@1.0.0` package of typed payload records carried on a
`resource.v1.*` topic family, served by handler-bound subscribers, fanned out
for discovery and routed per-URI for reads. Resources are **fetchable, not
events**: the operations are request/response *over* Astrid's pub/sub bus —
`read` a routed 1:1 exchange, `describe` a 1→N scatter-gather the broker
aggregates. They are pull-only, per-principal scoped, and never carry secrets.

The wire shape is sized for the *complete* MCP resource model as of the
2025-11-25 schema revision — multi-content reads (text/blob arrays), `title`,
`size`, `annotations`, `icons`, `_meta`, and cursor pagination are all present
in the records from 1.0, because a WIT record is not extensible and adding a
field post-1.0 is a breaking change. The push-shaped primitives (`subscribe` /
`resources/updated`) are **excluded by construction** — Astrid serves MCP
passively and cannot push to a client — and resource *templates* and
`completion/complete` are **deferred without precluding** (additive new topics,
no change to the records defined here). This RFC reconciles every feature of the
MCP resource surface explicitly; see the decision table in
[Reference-level explanation].

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

Four properties drive the design:

1. **Fetchable, not fire-and-forget.** Listing and reading are request/response
   operations — *interceptors*, in Astrid terms: a `[subscribe]` entry bound to a
   `handler` that replies, realized over the pub/sub bus as a request topic
   answered on a correlation-keyed response topic. `read` is a routed 1:1
   request/response; `describe` is a 1→N scatter-gather the broker fans out and
   aggregates (multiple capsules can serve resources). Resources are *fetched* on
   demand, not broadcast as events.
2. **Served by capsules, discovered by the broker.** Resources populate
   *automatically* from whatever capsules can serve them — the same fan-out the
   tool surface already uses. The broker stays a dumb aggregator; it never holds
   a hardcoded resource list and is blind to resource content. A capsule that
   holds per-principal state advertises and serves its own resources.
3. **A contract, not an implementation detail.** Hardcoding a resource set in
   the broker or the `mcp serve` shim would put capsule-domain knowledge in the
   router and leave third-party capsules with no way to contribute resources.
   Standardizing the contract is what lets any capsule expose governed,
   re-readable context with no change to the broker or the shim.
4. **1.0-safe against the full MCP model.** WIT records are immutable once
   published; a missing field is a future breaking change. So the records must
   carry the entire MCP `Resource`/`ResourceContents` shape now — even fields the
   reference shim cannot surface today — and every deferred feature must be
   addable as a *new* topic or record, never as a field on an existing one.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

A **resource** is a named, addressable, read-only blob of context: a `uri`
(conventionally `astrid://…`), a programmatic `name` and optional human `title`,
an optional `description`, declared `mime-type`, `size`, client `annotations`
(audience / priority / last-modified), `icons`, and a `_meta` extensibility
escape hatch. A capsule that holds context worth reading *exports the resource
interface*: it answers "what resources do you serve?" and "read this URI."

There are two operations, mirroring the tool interface's describe/execute:

- **describe** — enumerate. A *fan-out*: the broker asks every resource-serving
  capsule for its list and aggregates the responses. Cursor-paginated.
- **read** — fetch one URI. *Routed* to the capsule that owns the URI. A read
  returns a **list** of content chunks (text or blob), because one logical URI
  may expand into sub-resources and the chunks may mix types.

```
  MCP client            astrid mcp serve            broker (sage-mcp)         serving capsule
      │  resources/list        │                          │                         │
      ├───────────────────────►│  mcp.resources.list      │                         │
      │                        ├─────────────────────────►│  resource.v1.request.   │
      │                        │                          │      describe (fan-out) │
      │                        │                          ├────────────────────────►│
      │                        │                          │◄─ describe.<corr-id> ───┤
      │◄── list + nextCursor ──┤◄─ aggregated, uri-sorted ┤                         │
      │                        │                          │                         │
      │  resources/read <uri>  │  mcp.resource.read       │  resource.v1.request.   │
      ├───────────────────────►├─────────────────────────►│      read → owning src  │
      │◄─ contents[] (txt|blob)┤◄──── read-response ──────┤◄─ response.<corr-id> ───┤
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
at `initialize` (with `subscribe: false`, `listChanged: true`); `resources/list`
and `resources/read` map onto the broker front doors. The client lists and reads
on demand. **Nothing is pushed** — there is no resource-change subscription, only
the same best-effort `list_changed` notification the tool surface already emits
when capsules load or unload.

Worked example: a small introspection capsule serves `astrid://capabilities`
(the invoking principal's held grants) and `astrid://budget` (its CPU-fuel and
memory usage vs. limits). On `describe` it returns both definitions; on
`read("astrid://capabilities")` it returns a one-item `contents` list — a `text`
chunk of the live grant list for *that principal only*. The broker never learns
what the resource means — it discovers the URIs, routes the read, and forwards
the chunks.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Shared types (`astrid-bus:types`)

`resource-definition` and the content types live in the shared `types` package
alongside `tool-definition`, since the broker and the shim both depend on them.
Every field past `name`/`uri` is optional (or an empty-able `list`), so a capsule
emits the minimum and the contract stays forward-compatible.

```wit
/// A resource a capsule exposes for reading. Mirrors the MCP `Resource`
/// object (2025-11-25): an addressable, read-only blob of context.
record resource-definition {
    /// Stable, addressable URI (conventionally `astrid://…`). RFC 3986.
    uri: string,
    /// Programmatic identifier; also the fallback display name.
    name: string,
    /// Human-readable display name, preferred over `name` for UI.
    title: option<string>,
    /// What the resource holds — a hint to the model.
    description: option<string>,
    /// Declared MIME type of the resource, if uniform and known.
    mime-type: option<string>,
    /// Raw content size in bytes (pre-base64), if known. For context-window
    /// estimation and file-size display.
    size: option<u64>,
    /// Optional client hints (audience / priority / last-modified).
    annotations: option<annotations>,
    /// Sized icons for UI display. Empty list = none.
    icons: list<icon>,
    /// Extensibility escape hatch — a JSON object encoded as a string (as with
    /// `tool-call.arguments`). Maps to MCP `_meta`; key-namespacing rules apply
    /// (see Semantics).
    meta: option<string>,
}

/// Optional client display/usage hints. Mirrors MCP `Annotations`; these three
/// fields are the complete set the MCP layer can round-trip.
record annotations {
    /// Intended audience(s). Empty = unspecified.
    audience: list<resource-role>,
    /// Importance in the inclusive range 0.0..=1.0 (1 = effectively required,
    /// 0 = entirely optional). Out-of-range values are clamped by the shim.
    priority: option<f32>,
    /// Last-modified moment as an ISO-8601 string, e.g. "2025-01-12T15:00:58Z".
    last-modified: option<string>,
}

enum resource-role { user, assistant }

/// A sized icon for UI display. Mirrors MCP `Icon` (2025-11-25).
record icon {
    /// HTTP(S) URL or `data:` URI to the icon. RFC 3986.
    src: string,
    /// MIME type override, e.g. "image/png", "image/svg+xml".
    mime-type: option<string>,
    /// Sizes the icon serves, "WxH" (e.g. "48x48") or "any". Empty = unspecified.
    sizes: list<string>,
    /// Theme the icon targets.
    theme: option<icon-theme>,
}

enum icon-theme { light, dark }

/// One chunk of a resource read. Discriminated text vs blob, mirroring the MCP
/// `TextResourceContents` / `BlobResourceContents` split (rmcp's untagged enum).
/// A single read returns a `list` of these: a resource may expand into
/// sub-resources, and chunks may mix text and blob.
variant resource-contents {
    text(resource-text),
    blob(resource-blob),
}

record resource-text {
    /// URI of this (sub-)resource — may differ from the requested URI.
    uri: string,
    /// Declared MIME type of this chunk, if known.
    mime-type: option<string>,
    /// UTF-8 text payload.
    text: string,
    /// Per-chunk `_meta` (MCP carries `_meta` on each contents item).
    meta: option<string>,
}

record resource-blob {
    uri: string,
    mime-type: option<string>,
    /// Base64-encoded binary payload.
    blob: string,
    meta: option<string>,
}
```

## Interface (`astrid-bus:resource@1.0.0`)

```wit
package astrid-bus:resource@1.0.0;

/// Resource interface — capsules serve read-only, addressable context.
///
/// A capsule exporting this interface answers `resource.v1.request.describe`
/// (enumerate, fan-out, cursor-paginated) and `resource.v1.request.read` (fetch
/// one URI, routed to the owning capsule, multi-content), replying on
/// `resource.v1.response.{describe,read}.<correlation-id>`. Resources are
/// fetchable, pull-only, per-principal scoped, and never carry secrets.
interface resource {
    use astrid-bus:types/types.{resource-definition, resource-contents};

    /// Ask a resource-providing capsule to enumerate its resources.
    /// Topic: `resource.v1.request.describe` (fan-out). Every responder returns
    /// its list scoped to the invoking principal.
    record describe-request {
        /// The capsule replies on `resource.v1.response.describe.<correlation-id>`.
        correlation-id: string,
        /// Opaque pagination position. None = first page. Cursors are
        /// broker-minted and opaque; a capsule receiving one it did not mint
        /// MUST ignore it and return its full list (see Pagination).
        cursor: option<string>,
    }

    /// The resources a capsule provides.
    /// Topic: `resource.v1.response.describe.<correlation-id>`.
    record describe-response {
        correlation-id: string,
        resources: list<resource-definition>,
        /// Opaque continuation token. None = this capsule returned everything.
        /// The broker reconciles per-capsule cursors into one client cursor.
        next-cursor: option<string>,
    }

    /// Read one resource by URI.
    /// Topic: `resource.v1.request.read`. The broker routes it to the capsule
    /// that owns the URI (from the describe snapshot).
    record read-request {
        uri: string,
        correlation-id: string,
    }

    /// Contents of a read.
    /// Topic: `resource.v1.response.read.<correlation-id>`.
    ///
    /// `contents` is a LIST: one logical URI may expand into sub-resources, and
    /// chunks may mix text and blob. On failure, `contents` is empty and `error`
    /// carries a human-readable reason (unknown URI, denied, unavailable).
    record read-response {
        correlation-id: string,
        /// Echoes the requested URI.
        uri: string,
        contents: list<resource-contents>,
        /// Present iff the read failed. Mutually exclusive with non-empty `contents`.
        error: option<string>,
        /// Reserved for result-level MCP `_meta`. Unsurfaced today (the reference
        /// rmcp pin has no slot); present so the read path is symmetric with the
        /// list path and a future pin needs no breaking record change.
        meta: option<string>,
    }
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

**Discovery is a 1→N scatter-gather, and the drain is the requester's job.** One
request, one shared `correlation-id`, and every serving capsule replies on the
same `…response.describe.<correlation-id>` topic; the broker tells the replies
apart by their `source-id`. A serving capsule implements only its
`resource_describe` handler — return its list (scoped to the principal), echo the
`correlation-id` — and **never drains or aggregates**. The collection
(subscribe-before-publish, drain the window, dedupe, sort, paginate) lives once,
on the requester side; capsules do not hand-roll it. The 1:1 `read` is the
degenerate case — a drain window that stops at the first reply, i.e.
`ipc::request_response`.

Because the responder count is unknown (capsule topology is open) and the window
is bounded, **`describe` is best-effort**: the aggregate is *every resource from
every capsule that answered within the window*, not a hard completeness
guarantee. A capsule that misses the window is absent from that round and
reappears on the next `describe` — driven by a `list_changed` nudge (capsule
load/unload) or a re-describe on a read miss. Eventual consistency, not snapshot
consistency.

**Reserved for templates (additive, not defined here).** Resource *templates*
(below, Future possibilities) get their own topic family
`resource.v1.request.describe-templates` /
`resource.v1.response.describe-templates.<correlation-id>` carrying a future
`resource-template` record (a `uri-template` field per RFC 6570). They are a
*new* topic and record — adding them changes nothing in the records defined
here. The name is reserved now to keep the eventual addition unambiguous.

## MCP bridge (broker front doors)

The `sage-mcp` broker exposes two front doors the `mcp serve` shim calls,
parallel to the existing `astrid.v1.request.mcp.tools.list` / `…tool.call`:

| Front door | Gating | Behaviour |
|------------|--------|-----------|
| `astrid.v1.request.mcp.resources.list` | ungated (read-only discovery, as with `tools.list`) | fan out `describe`, aggregate, sort by `uri`, page, reply with `nextCursor` |
| `astrid.v1.request.mcp.resource.read` | confused-deputy `trusted_ingress` gate (as with `tool.call`) | route the read to the owning capsule, reply |

## MCP shim (`astrid mcp serve`)

- `get_info` adds the `resources` capability. The builder ordering is
  load-bearing: call `enable_resources()` **before** `enable_resources_list_changed()`
  (the list-changed setter no-ops if `resources` is unset), and **do not** call
  the subscribe setter — leaving `subscribe` unset serializes as absent, which a
  client reads as `false`.
- `list_resources` → `astrid.v1.request.mcp.resources.list`, reshape the
  aggregated definitions to MCP `ListResourcesResult`. Honors the MCP
  `cursor`/`nextCursor`: the broker mints an opaque client cursor over the
  aggregated, `uri`-sorted snapshot; v1 returns a single page (`nextCursor`
  absent — see Pagination).
- `read_resource(uri)` → `astrid.v1.request.mcp.resource.read`. Reshape
  `read-response.contents` to `ReadResourceResult.contents` (a `Vec`), mapping
  each `resource-contents::text` → `TextResourceContents` and `::blob` →
  `BlobResourceContents`. The discriminant is the WIT variant arm — never
  inferred from `mime-type`.
- `notifications/resources/list_changed` reuses the existing `capsules_loaded`
  broadcast — the same nudge that already drives `tools/list_changed`. This is
  the *one* server → client signal Astrid has, and it is **list-granularity**
  (capsule load/unload), not a per-resource update.

## Semantics

- **Per-principal, always.** Every `describe` and `read` carries the invoking
  principal (kernel-stamped `source-id → principal`). A serving capsule MUST
  scope both the returned definitions and the read contents to that principal. A
  read addressing another principal's URI MUST fail closed (`error` set,
  `contents` empty).
- **Never a secret.** A serving capsule MUST NOT expose secret material —
  credentials, tokens, key bytes — as a resource. Resources are non-sensitive
  context by contract.
- **Read-through.** A `read` reflects live state at read time. The broker MAY
  cache the *describe* snapshot with a TTL (as it does for tools); it MUST NOT
  cache `read` contents.
- **Gating.** Discovery (`list`) is ungated. Reads pass the broker's
  confused-deputy ingress allow-set, identical to tool execution — a read can
  touch per-principal state, so it is gated like an action.
- **Errors.** Unknown URI, denied, or unavailable → `read-response.error` set
  with a human-readable reason and `contents` empty. A broker-side drain timeout
  surfaces as a JSON-RPC error to the client.
- **`_meta` namespacing.** A capsule populating `meta` (on a definition or a
  content chunk) MUST namespace its keys under `com.unicity-astrid/` (or another
  domain it controls). The second-label prefixes `mcp` and `modelcontextprotocol`
  are reserved by MCP and MUST NOT be used.
- **Annotation bounds.** `annotations.priority`, if present, is in `[0.0, 1.0]`;
  the shim clamps out-of-range values. `last-modified` is an ISO-8601 string.
- **Concurrency.** A fresh `correlation-id` scopes every request's responses, so
  concurrent describes and reads never collide on a response topic.
- **No subscriptions.** The contract defines no resource-change subscription;
  `list_changed` is a coarse capability nudge, not a per-resource signal.

## Pagination

MCP's client sees **one** `resources/list` with **one** opaque `cursor` /
`nextCursor`. Astrid's describe is a **fan-out**: N capsules, each returning its
own list. The reconciliation:

The broker fans out `describe`, drains the window, aggregates all definitions,
and **sorts by `uri`** (a total, stable order). It returns a page and mints an
**opaque** client cursor encoding `{snapshot-id, last-uri}` (base64). On the next
`resources/list` with that cursor, the broker resumes from its TTL'd snapshot
after `last-uri`. Because MCP guarantees the cursor is opaque to the client and
the server owns its encoding, the per-capsule `next-cursor` is an *internal*
concern the broker hides; the `cursor`/`next-cursor` fields on the WIT records
exist so that a future capsule with a large resource set can paginate its own
contribution (the broker forwards the client cursor's per-capsule component and
stitches the per-capsule `next-cursor`s back) — non-breaking precisely because
the fields are present from 1.0.

**v1 behavior:** the broker aggregates everything and returns a single page
(`nextCursor` absent). Enabling windowing later is a **broker-only** change — the
wire shape already paginates. A cursor that outlives its snapshot degrades safely
to a fresh first page, exactly MCP's "cursor may be invalid" allowance. Templates
paginate identically when added.

## Shim coverage (rmcp 1.7.0)

The *contract* (what the WIT carries) is distinct from what `astrid mcp serve`
emits on the reference rmcp pin (1.7.0). rmcp is broadly spec-current, with two
edges the RFC calls out so implementers do not chase phantom fields:

- **Read result has no result-level `_meta` and no cursor.** rmcp's
  `ReadResourceResult` is `{ contents: Vec<ResourceContents> }`. So
  `read-response.meta` is **reserved-but-unsurfaced** today (left `none`); per-
  chunk `meta` on `resource-text`/`resource-blob` *is* surfaced (rmcp's contents
  carry `_meta`). Read is non-paginated by design — correct, the read records
  carry no cursor.
- **`Annotations` is `#[non_exhaustive]` with exactly `audience` / `priority` /
  `lastModified`.** The WIT `annotations` mirrors precisely these three; future
  annotation fields ride `meta`, not new typed fields.

Everything else the contract carries — multi-content, `title`, `size`, `icons`,
per-chunk `mime-type`/`meta`, and the paginated shape — is surfaceable on rmcp
1.7.0 today. The shim builds rmcp's `#[non_exhaustive]` params/results via their
constructors (not struct literals) and tolerates rmcp fields it does not set.

## Capability

Serving a resource requires **no new capability** — a capsule serves only what it
can already read. A capsule serving per-principal kernel state (e.g.
`astrid://capabilities` or `astrid://budget`) needs whatever self-scoped view
capability that data already requires (e.g. a `self:…:view` grant); that is the
serving capsule's concern, not the contract's. The contract adds no privilege.

# Drawbacks
[drawbacks]: #drawbacks

- A third capsule interface to maintain (`tool`, `resource`, and the existing
  prompt-assembly interface), each with its own records, topics, and SDK
  surface. The resource records are notably larger than tools' (the full MCP
  shape), though most fields are optional.
- Read routing depends on the broker's `uri → source-id` table from the describe
  snapshot; a stale snapshot can misroute a read (mitigated by re-describe on
  miss, at the cost of latency on the miss path).
- Resources overlap conceptually with read-only tools. Without guidance, a
  capsule author may be unsure whether to model a read as a tool or a resource.
- Carrying fields the reference shim cannot surface today (`read-response.meta`)
  is a deliberate forward-compat cost: a small amount of dead surface now to
  avoid a breaking record change later.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

- **Why mirror the `tool` interface.** The fan-out / aggregate / route machinery
  already exists and is proven for tools. An interceptor request/response
  contract over a `*.v1.*` topic family keeps the broker uniform. An interceptor
  is the correct primitive because resources are *fetchable*; modelling them as
  events would force request/response onto a broadcast mechanism.
- **Why `resource-contents` is a `variant`, not a scalar string or two optional
  fields.** MCP returns `(TextResourceContents | BlobResourceContents)[]` — an
  *array* of a *discriminated union*. A scalar `string` (the naive shape) cannot
  represent the array, and inferring text-vs-blob from `mime-type` is undecidable
  (`application/json` is text, `image/svg+xml` is text, `application/octet-stream`
  is blob). The WIT `variant` makes the discriminant explicit and lossless and
  maps 1:1 onto rmcp's untagged enum. This is the load-bearing shape decision.
- **Why the full field set now.** A WIT record is immutable; adding a field
  post-1.0 breaks the package. `title`, `size`, `annotations`, `icons`, and
  `_meta` are all in the current `Resource` and surfaceable by the reference rmcp
  pin. Omitting any of them would force a breaking change the first time it is
  wanted. `_meta` in particular makes most *future* MCP additions non-breaking
  (they ride the escape hatch), so it is the highest-leverage field to include.
- **Alternative: expose resources as read-only tools.** Rejected. A tool read
  costs a tool-call slot and an agent-loop turn, and MCP clients treat resources
  and tools as distinct surfaces (resources attach as re-readable context). It
  also cannot express multi-content reads naturally.
- **Alternative: hardcode a fixed resource set in the broker or shim.** Rejected.
  Puts capsule-domain knowledge in the router, is not extensible to third-party
  capsules, and violates the broker-is-dumb invariant.
- **Alternative: a host function rather than a bus contract.** Rejected.
  Resources are capsule-served domain data, not a kernel primitive.
- **Alternative: multiplex templates onto `describe` via a `kind` field.**
  Rejected in favour of a separate reserved topic — templates are a distinct MCP
  endpoint with a distinct record (`uriTemplate`, not `uri`); a separate topic
  keeps both records clean and the addition purely additive.

# Prior art
[prior-art]: #prior-art

- **Model Context Protocol — `resources` (2025-11-25 schema).** The complete
  surface and this RFC's verdict on each: `resources/list` (**adopt**),
  `resources/read` with a text/blob content *array* (**adopt/adapt** — the array
  is the key adaptation), `ResourceTemplate` + `resources/templates/list`
  (**defer, non-precluding**), `resources/subscribe` + `notifications/
  resources/updated` (**exclude by construction** — needs server→client push
  Astrid lacks), `notifications/resources/list_changed` (**adopt** — broadcast-
  shaped, already present for tools), `Annotations`, `_meta`, `size`, `title`,
  `icons` (**adopt**, all carried), cursor pagination (**adapt** to the fan-out),
  `completion/complete` for template variables (**defer** with templates). The
  RFC reconciles the whole model, not a subset.
- **Astrid's own `tool` interface** (`astrid-bus:tool@1.0.0`). The direct
  structural precedent: a record-only WIT interface bound to a `*.v1.*` topic
  family, served by handler-bound subscribers and aggregated by the broker.
- **rmcp** (the reference shim's MCP library) — its `Resource` / `RawResource`,
  untagged `ResourceContents`, and `Annotations` types fix the concrete field
  shapes the WIT must reshape to, and its lag from the spec (no result-level read
  `_meta`) bounds what the shim surfaces today.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Pagination behavior.** Ship v1 single-page (`next-cursor: none`) behind the
  paginated shape (recommended), or window from day one? The shape is fixed
  either way; this is a broker-behavior choice, deferrable without contract risk.
- **Read routing.** Targeted (broker publishes to the owning capsule, as written)
  vs. fan-out-and-self-filter (every capsule sees the read, the owner answers).
  The targeted form mirrors `tool.v1.execute.<name>`; the fan-out form is simpler
  but noisier.
- **Describe snapshot TTL / drain window.** Match the tool surface's exactly, or
  tune independently for resources?
- **`read-response.meta` reservation.** Carried now as forward-compat insurance
  against a future rmcp pin gaining result-level `_meta`. If the maintainers
  prefer a strictly-surfaceable contract, it can be dropped — at the cost of a
  breaking change if read-level `_meta` is ever wanted.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Resource templates** — `resources/templates/list` with RFC 6570
  `uriTemplate`s. Additive: a new `resource.v1.request.describe-templates` topic
  (reserved above) and a new `resource-template` record (`uri-template`,
  `name`/`title`/`description`/`mime-type`/`annotations`/`meta`). No change to the
  records defined here.
- **`completion/complete`** for template URI variables — rides with templates as
  a new completion-shaped topic + a `resource-template-reference`. Meaningless
  without templates; deferred together.
- **A subscribe surface** — `resources/subscribe` + per-resource update
  notifications — *if and when* Astrid grows a genuine server → client push
  channel. Re-addable as a new `resource.v1.request.subscribe` topic plus
  flipping the shim's `subscribe` capability to `true`; the records here do not
  preclude it. The absence today is by construction, not oversight.
- **An SDK `#[astrid::resource(...)]` macro** mirroring `#[astrid::tool(...)]`,
  generating the describe/read handlers from an attribute.
- **A reference introspection capsule** serving `astrid://capabilities`,
  `astrid://budget`, and `astrid://capsules` — the first concrete consumer of
  this contract, and the per-principal self-awareness surface for agent runtimes
  hosted on Astrid.
- **The MCP `prompts` primitive** as a sibling interface for named prompt
  templates. Distinct from the existing `astrid-bus:prompt` interface, which is
  internal prompt *assembly*, not a client-facing template library; that interface
  should be renamed (e.g. `prompt-builder`, aligning the package with the
  `prompt_builder.v1.*` topics and the `prompt-builder` capsule it already uses)
  before `prompt` is reused for the MCP sense.
