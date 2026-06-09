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
`resource.v1.*` topic family, served by handler-bound subscribers. **Discovery
is a 1→N scatter-gather** (the broker broadcasts, every serving capsule replies
on its own per-source topic, the broker drains a window and aggregates);
**reads are a broadcast-with-self-filter** (every read-serving capsule receives
the read, only the URI's owner replies). Resources are fetchable, pull-only,
per-principal scoped, and never carry secrets.

The records are sized for the *complete* MCP resource model as of the 2025-11-25
schema revision — multi-content reads (text/blob arrays), `title`, `size`,
`annotations`, `icons`, and `_meta` are all present from 1.0, because a WIT
record is not extensible and adding a field post-1.0 is a breaking change. The
push-shaped primitives (`subscribe` / `resources/updated`) are **excluded by
construction** — Astrid serves MCP passively and cannot push to a client.
Resource *templates* and `completion/complete` are **deferred without
precluding** (additive new topics). Pagination ships as **single-page in 1.0**
(the realistic resource cardinality is tiny); the cursor is a broker↔client
concern only and never reaches a capsule.

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
   answered on a response topic. `describe` is a 1→N scatter-gather (the broker
   drains every serving capsule's reply); `read` is a broadcast where only the
   owning capsule answers. Resources are *fetched* on demand, not broadcast as
   events.
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
   published; a missing field is a future breaking change. So the records carry
   the entire MCP `Resource`/`ResourceContents` shape now — even fields the
   reference shim cannot surface today — and every deferred feature is addable as
   a *new* topic or record, never as a field on an existing one.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

A **resource** is a named, addressable, read-only blob of context: a `uri`
(conventionally `astrid://…`), a programmatic `name` and optional human `title`,
an optional `description`, declared `mime-type`, `size`, client `annotations`,
`icons`, and a `_meta` escape hatch. A capsule that holds context worth reading
*exports the resource interface*: it answers "what resources do you serve?" and
"read this URI."

Two operations, mirroring the tool interface's describe/execute:

- **describe** — enumerate. A *fan-out*: the broker broadcasts one request; every
  serving capsule replies on its own `…response.describe.<source-id>` topic; the
  broker drains a bounded window and aggregates. The invoking principal rides the
  **envelope** (kernel-stamped), not the payload.
- **read** — fetch one URI. There is no source-id-targeted publish on the bus, so
  the read is **broadcast** on a single topic; every read-serving capsule
  receives it, **only the URI's owner replies**, non-owners stay silent. A read
  returns a **list** of content chunks (text or blob), because one logical URI
  may expand into sub-resources and the chunks may mix types.

```
  MCP client            astrid mcp serve            broker (sage-mcp)         serving capsules
      │  resources/list        │                          │                         │
      ├───────────────────────►│  mcp.resources.list      │   describe (broadcast)  │
      │                        ├─────────────────────────►├────────────────────────►│ (each replies on
      │                        │                          │◄ …describe.<source-id> ─┤  its own source topic)
      │◄──── list (1 page) ────┤◄ drained, per-principal ─┤                         │
      │                        │   filtered, deduped      │                         │
      │  resources/read <uri>  │  mcp.resource.read       │   read (broadcast)      │
      ├───────────────────────►├─────────────────────────►├────────────────────────►│ owner replies;
      │◄─ contents[] (txt|blob)┤◄── read-response ────────┤◄ …read.<correlation-id>─┤ non-owners silent
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
at `initialize` (`subscribe: false`, `listChanged: true`); `resources/list` and
`resources/read` map onto the broker front doors. The client lists and reads on
demand. **Nothing is pushed** — only the best-effort `list_changed` notification
the shim emits when the resource surface changes (capsule load/unload).

Worked example: an introspection capsule serves `astrid://capabilities` and
`astrid://budget`. On `describe` it returns both definitions, scoped to the
principal on the envelope. On `read("astrid://capabilities")` it returns a
one-item `contents` list — a `text` chunk of the live grant list for *that
principal only*. The broker never learns what the resource means — it discovers
the URIs, routes the read by broadcast-and-self-filter, and forwards the chunks.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Shared types (`astrid-bus:types`)

`resource-definition` and the content types live in the shared `types` package
alongside `tool-definition`. Every field past `name`/`uri` is optional (or an
empty-able `list`), so a capsule emits the minimum and stays forward-compatible.

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
    /// Raw content size in bytes (pre-base64), if known. `u32` to round-trip
    /// rmcp's `RawResource.size: Option<u32>` losslessly (≤ 4 GiB).
    size: option<u32>,
    /// Optional client hints (audience / priority / last-modified).
    annotations: option<annotations>,
    /// Sized icons for UI display. Empty list = none.
    icons: list<icon>,
    /// Extensibility escape hatch — a JSON object encoded as a string (as with
    /// `tool-call.arguments`). Maps to MCP `_meta`; key-namespacing rules apply
    /// (see Semantics → `_meta`).
    meta: option<string>,
}

/// Optional client display/usage hints. Mirrors MCP `Annotations`; these three
/// fields are the complete set rmcp 1.7.0 round-trips (`#[non_exhaustive]`).
record annotations {
    /// Intended audience(s). Empty = unspecified.
    audience: list<resource-role>,
    /// Importance in the inclusive range 0.0..=1.0. Out-of-range → clamped by the shim.
    priority: option<f32>,
    /// Last-modified as an ISO-8601 string, e.g. "2025-01-12T15:00:58Z". The shim
    /// parses to `DateTime<Utc>`; a malformed value drops ONLY this field.
    last-modified: option<string>,
}

enum resource-role { user, assistant }

/// A sized icon for UI display. Mirrors MCP `Icon` (2025-11-25).
record icon {
    src: string,
    mime-type: option<string>,
    sizes: list<string>,
    theme: option<icon-theme>,
}

enum icon-theme { light, dark }

/// One chunk of a resource read. Discriminated text vs blob, mirroring the MCP
/// `TextResourceContents` / `BlobResourceContents` split (rmcp's untagged enum).
/// A single read returns a `list` of these.
variant resource-contents {
    text(resource-text),
    blob(resource-blob),
}

record resource-text {
    /// URI of this (sub-)resource — may differ from the requested URI.
    uri: string,
    mime-type: option<string>,
    /// UTF-8 text payload.
    text: string,
    /// Per-chunk `_meta`.
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

The capsule-facing records carry **no correlation-id on `describe`** and **no
cursor anywhere** — both were errors in an earlier draft. `describe` attributes
replies by the kernel-stamped **source-id** of each per-source response topic
(this is how the live `tool.v1` fan-out actually works — `tool.wit`'s
`correlation-id` field is vestigial, unused by the broker). `read` carries a
`correlation-id` because it is a genuine per-request exchange (the broker mints
one to tell concurrent reads apart). Pagination is a broker↔client concern that
never reaches a capsule, so the cursor appears in neither record (see Pagination).

```wit
package astrid-bus:resource@1.0.0;

/// Resource interface — capsules serve read-only, addressable context.
///
/// `describe` (`resource.v1.request.describe`) is a fan-out: the broker
/// broadcasts an empty request and each capsule replies on its own
/// `resource.v1.response.describe.<source-id>` topic; the broker drains the
/// `…describe.*` wildcard and attributes by source-id. `read`
/// (`resource.v1.request.read`) is a broadcast where only the URI's owner
/// replies, on `resource.v1.response.read.<correlation-id>`. Resources are
/// pull-only, per-principal scoped, and never carry secrets.
interface resource {
    use astrid-bus:types/types.{resource-definition, resource-contents};

    /// Ask resource-providing capsules to enumerate their resources.
    /// Topic: `resource.v1.request.describe` (broadcast). The payload is EMPTY —
    /// the invoking principal rides the kernel-stamped envelope, not the body.
    /// Each capsule MUST scope its reply to that principal (see Semantics).
    record describe-request {
        // intentionally empty; reserved for future request-scoping fields.
    }

    /// The resources a capsule provides.
    /// Topic: `resource.v1.response.describe.<source-id>`.
    record describe-response {
        resources: list<resource-definition>,
    }

    /// Read one resource by URI.
    /// Topic: `resource.v1.request.read` (broadcast). Only the URI's owner
    /// replies; non-owners stay silent (do NOT reply with an error).
    record read-request {
        uri: string,
        /// Broker-minted; the owner echoes it in the reply topic
        /// `resource.v1.response.read.<correlation-id>` so concurrent reads
        /// stay distinct.
        correlation-id: string,
    }

    /// Contents of a read.
    /// Topic: `resource.v1.response.read.<correlation-id>`.
    ///
    /// `contents` is a LIST: one URI may expand into sub-resources, chunks may
    /// mix text and blob. On failure `contents` is empty and `error` is set.
    record read-response {
        /// Echoes the requested URI.
        uri: string,
        contents: list<resource-contents>,
        /// Present iff the read failed; mutually exclusive with non-empty `contents`.
        error: option<string>,
        /// Reserved for result-level MCP `_meta`. Unsurfaced today (rmcp 1.7.0's
        /// `ReadResourceResult` has no slot); present so a future pin needs no
        /// breaking record change.
        meta: option<string>,
    }
}
```

## Topics

| Topic | Direction | Payload |
|-------|-----------|---------|
| `resource.v1.request.describe` | broker → serving capsules (broadcast) | `describe-request` (empty) |
| `resource.v1.response.describe.<source-id>` | serving capsule → broker | `describe-response` |
| `resource.v1.request.read` | broker → read-serving capsules (broadcast) | `read-request` |
| `resource.v1.response.read.<correlation-id>` | owning capsule → broker | `read-response` |

**Describe (fan-out).** The broker subscribes to `resource.v1.response.describe.*`,
publishes one empty `describe-request`, and drains a bounded window. Each capsule
replies on `…describe.<source-id>` (its kernel-stamped source-id); the broker
attributes each reply by that source-id segment. This is the live `tool.v1`
mechanism verbatim. At most **one** response per source-id is honoured per round
(a second is dropped and logged); a full re-describe — not an incremental
broadcast — is what evicts a removed URI from the snapshot.

**Read (broadcast + self-filter).** The bus has only topic-keyed publish
(`publish`/`publish_as`) — there is no source-id-targeted send, so the broker
*cannot* deliver a read to only the owner. The read is broadcast on the single
`resource.v1.request.read` topic; every read-serving capsule receives it and
**self-filters by URI**; only the owner replies (on `…read.<correlation-id>`);
non-owners stay silent so the broker knows the owner answered when it gets one
non-empty reply. The describe-derived `uri → source-id` map is a
**disambiguation hint** (for logging/audit), not a routing primitive.

**Dedup + collision.** On a `uri` collision across capsules, the broker keeps the
entry whose **source-id sorts lexicographically first** — a *deterministic*
tie-break independent of bus arrival order — and logs a collision audit. (See
Capability for the trust model around collisions.) The aggregated snapshot MUST
carry each entry's source-id so this is constructible.

**Reserved for templates (additive, not defined here).** Resource *templates*
get their own topic family `resource.v1.request.describe-templates` /
`resource.v1.response.describe-templates.<source-id>` carrying a future
`resource-template` record (`uri-template` per RFC 6570). Adding them changes
nothing in the records here.

## MCP bridge (broker front doors)

| Front door | Gating | Behaviour |
|------------|--------|-----------|
| `astrid.v1.request.mcp.resources.list` | **ungated** — but *only* because describe is per-principal-scoped (below); that scoping is load-bearing for list's safety | fan out describe, **filter to the invoking principal**, dedupe, sort by `uri`, reply one page |
| `astrid.v1.request.mcp.resource.read` | confused-deputy gate, at the fidelity of `tool.call`: resolve `runtime::caller()`, **fail closed on no caller context**, require `source_id ∈ trusted_ingress_ids` (a `trusted_ingress` membership, **not** merely `verified()`), reply error + empty on rejection (never dispatch) | broadcast the read, collect the owner's reply, reshape |

The read reply travels back to the shim on the standard
`astrid.v1.response.<req_id>` envelope. Its JSON carries an explicit
`type: "text" | "blob"` discriminant per content chunk — the broker needs a new
content-shaping path; the tool surface's text-only `mcp_content` cannot be reused.

## MCP shim (`astrid mcp serve`)

- `get_info` sets the `resources` capability by **field mutation**, mirroring the
  existing `capabilities.tools = …`:
  `capabilities.resources = Some(ResourcesCapability { subscribe: None, list_changed: Some(true) })`.
  Leaving `subscribe` unset serializes absent, which a client reads as `false`.
- **Advertising `resources` REQUIRES overriding `read_resource`** — rmcp 1.7.0's
  default `read_resource` returns `method_not_found`, so a listable surface with
  an unwired read 404s every read. (`list_resources` must also be overridden; the
  default is empty-OK.)
- `list_resources` → `astrid.v1.request.mcp.resources.list`, reshape to MCP
  `ListResourcesResult`. Single page in 1.0 (`nextCursor` absent — see Pagination).
- `read_resource(uri)` → `astrid.v1.request.mcp.resource.read`. Reshape
  `read-response.contents` to `ReadResourceResult.contents` (a `Vec`), mapping
  each `resource-contents::text` → `TextResourceContents` and `::blob` →
  `BlobResourceContents`. The discriminant is the WIT variant arm — never inferred
  from `mime-type`. `ReadResourceResult` has **no `isError`**; a read failure is a
  JSON-RPC error (see Semantics → Errors), never an empty-contents success.
- `resources/templates/list` falls through to rmcp's default **empty-OK**
  (`{resourceTemplates: []}`) — the intentional, conformant representation of
  "templates deferred."
- `notifications/resources/list_changed` requires an **independent
  resource-URI-set baseline** in the shim's watcher, diffed on `capsules_loaded`
  and pushed when the resource set changes. The existing watcher diffs *tool*
  names only; a resource-only change would otherwise be suppressed. The two diffs
  are independent (a reload may change tools, resources, both, or neither).
- **Reshape table (WIT → rmcp):** `size: option<u32>` → `RawResource.size`
  (lossless); `annotations` → the `Annotated<RawResource>` *wrapper* (not a
  `RawResource` field); `last-modified` (ISO-8601 string) → `DateTime<Utc>`,
  dropping only `last-modified` on a parse failure; `priority` clamped to `[0,1]`;
  `meta` (JSON string) → the rmcp `_meta` map.

## Semantics

- **Per-principal, and the broker MUST enforce it — it is not free.** The bus
  self-scope fires only for the *audit* topic (`pattern_covers_audit`); an
  ordinary subscription like `resource.v1.response.describe.*` is unscoped, so a
  concurrent describe for principal B can land B-scoped replies in A's drain. The
  broker therefore MUST filter every drained describe-response to the invoking
  principal (drop any whose kernel-stamped principal ≠ the invoker), and the
  serving capsule MUST scope its own reply. Do **not** rely on the implicit bus
  self-scope.
- **The serving capsule MUST derive the principal from the kernel attribution**
  (`caller().principal` / `Verified`), MUST NOT read it from any request-body
  field, and MUST fail closed when it is absent or `Claimed`. This is
  capsule-untrusted-input territory: the records deliberately carry no principal
  field. The `#[astrid::resource]` macro (Future possibilities) SHOULD inject a
  Verified-principal-or-fail-closed prologue so the common case is correct by
  construction.
- **Any broker-side describe-snapshot cache, and the `uri → source-id` map
  derived from it, MUST be keyed by invoking principal.** A cache hit for
  principal B MUST NOT serve principal A's snapshot. The existing tool cache's
  single global key (`tools.cache`) is **not** a safe template — for tools the
  descriptors are principal-uniform; for resources the per-principal scoping is
  the entire premise, so a shared cache leaks exactly the payload the design
  protects. (Re-fanning-out per principal is also acceptable, and simplest.)
- **Never a secret.** A serving capsule MUST NOT expose secret material as a
  resource. Resources are non-sensitive context by contract.
- **Resource content is not inherently trusted.** A capsule may serve external,
  attacker-influenced data; the broker does not vet content; an
  `annotations.priority` near 1.0 does not make content authoritative. A capsule
  serving external data assumes the injection responsibility. (Provenance is not
  a 1.0 record field; if one is later wanted it rides a reserved
  `com.unicity-astrid/provenance` `_meta` key — additive.)
- **Shapes are implicit.** A capsule MAY answer reads for a URI *family* it owns
  (e.g. `astrid://logs/<id>`) without enumerating every member in `describe`;
  describe is a discovery hint, **not** a mandatory exhaustive read-allowlist.
  URIs are flat: there is **no implicit hierarchy or `..` traversal** — owning
  `astrid://logs/` grants no authority over `astrid://capabilities`, and a capsule
  MUST reject a read outside the family it actually serves (fail closed). A
  capsule that *wants* a closed allowlist MAY enforce describe-membership; that is
  its choice, not a contract requirement.
- **Gating.** Discovery (`list`) is ungated *because* its results are
  per-principal-scoped. Reads pass the confused-deputy gate (MCP bridge table).
- **Errors.** Every read failure is a JSON-RPC error, never an empty-contents
  success: unknown URI → `-32002` (resource-not-found), denied / fail-closed →
  `-32603`, broker drain timeout → `-32603`, each carrying `read-response.error`
  as the message.
- **`_meta` namespacing — enforced at the shim.** A capsule MUST namespace `meta`
  keys under a domain it controls (`com.unicity-astrid/`, `dev.astrid/`); the
  second labels `mcp` / `modelcontextprotocol` are MCP-reserved. The **shim**
  (the one trusted choke before the client) MUST strip/reject reserved-prefixed
  `_meta` keys, clamp `priority` to `[0,1]`, and cap `meta` byte size — the broker
  is content-blind and cannot.
- **Concurrency.** Concurrent describes are isolated by the **per-principal drain
  filter** above (not by a correlation-id, which describe does not carry).
  Concurrent reads are distinguished by the broker-minted `correlation-id` in the
  read reply topic.
- **No subscriptions.** The contract defines no resource-change subscription;
  `list_changed` is a coarse capability nudge, not a per-resource signal.

## Limits

Resource payloads are strictly larger than tool descriptors (unbounded text/blob)
and the tool path bounds every axis the hard way; the resource contract MUST
mirror it. Normative caps (values track `discovery.rs`/`cache.rs`):

- `MAX_RESOURCES_PER_DESCRIBE_RESPONSE` (≈ 256) and `MAX_CACHED_RESOURCES` (≈ 512);
  an over-cap describe-response is dropped + logged.
- Per-field byte caps: `description`/`title` (≈ 4096), `meta` (bounded), max icons
  per definition.
- `MAX_CONTENTS_PER_READ` and a hard per-read aggregate byte cap, **base64 counted
  pre-expansion**. An over-cap read **fails closed** (`error` set, `contents`
  empty) — never silent truncation.

## Pagination

MCP `resources/list` is cursor-paginated; Astrid's describe is a fan-out across N
capsules with no per-capsule pagination. The reconciliation, and why 1.0 is
single-page:

**Single-page in 1.0.** The broker fans out describe, filters to the principal,
dedupes, sorts by `uri`, and returns **everything in one page** (`nextCursor`
absent). The realistic resource cardinality is tiny (a handful per capsule), so
windowing is not needed; the WIT carries **no cursor in any capsule-facing
record** (the per-capsule cursor stitching a prior draft imagined is mechanically
impossible over a single broadcast payload). The cursor exists only at the
broker↔client (MCP) boundary, and in 1.0 the broker never mints one.

**Windowing is NOT a "broker-only change over the existing shape."** It would
require new immutable snapshot retention the current cache cannot express: the
live describe cache is a single mutable map, wholesale-replaced each fan-out and
mutated outside any window by the standing collector, so it cannot freeze a
stable page-set behind a cursor. If windowing is ever built, it needs a separate
immutable per-`(principal, snapshot-id)` store with its own TTL, the cursor
encoded as `{snapshot-id, last-index}`, **no re-describe between pages of one
cursor chain**, and a stale/unknown cursor that terminates (empty page or
`-32602` invalid-cursor) rather than silently restarting at page 1. This is the
*B* option, and it is deferred: the resource cardinality does not motivate it.

**Best-effort, stated honestly.** Because the describe window is bounded and the
responder count is unknown, a single page is "every resource from every capsule
that answered within the window." A capsule that misses the window is absent from
that round and reappears on the next `describe` (a `list_changed` nudge or a
re-describe-on-read-miss closes the gap). This is eventual, not snapshot,
consistency — and it is *compatible* with single-page (there is no mid-iteration
completeness promise to break, precisely because there is no iteration in 1.0).

## Shim coverage (rmcp 1.7.0)

The *contract* (what the WIT carries) is distinct from what `astrid mcp serve`
emits on the reference rmcp pin. Verified against the pinned 1.7.0 source:

- **`ReadResourceResult` is `{ contents: Vec<ResourceContents> }` — no
  result-level `_meta`, no `isError`, no cursor.** So `read-response.meta` is
  reserved-but-unsurfaced (left `none`); read failures are JSON-RPC errors; per-
  chunk `meta` *is* surfaced (rmcp's `ResourceContents` carry it). Read is
  non-paginated — correct, the read records carry no cursor.
- **`RawResource.size: Option<u32>`** — the WIT `size: option<u32>` round-trips
  losslessly.
- **`Annotations` (`#[non_exhaustive]`) = `audience` / `priority` /
  `last_modified: Option<DateTime<Utc>>`.** The WIT mirrors exactly these three;
  future annotation fields ride `meta`.
- **Default handlers:** `read_resource`/`subscribe`/`unsubscribe` →
  `method_not_found`; `list_resources`/`list_resource_templates` → empty-OK. So
  the shim MUST override `read_resource` and `list_resources`; the
  templates-empty-OK default is the conformant "deferred" representation.

Everything else the contract carries — multi-content, `title`, `icons`, per-chunk
`mime-type`/`meta` — is surfaceable on 1.7.0 today.

## Capability

Serving a resource requires **no new capability** — a capsule serves only what it
can already read. A capsule serving per-principal kernel state (e.g.
`astrid://capabilities`) needs whatever self-scoped view capability that data
already requires (e.g. a `self:…:view` grant); that is the serving capsule's
concern, not the contract's.

**Collision trust model (v1 = operator-trust).** Capsule installation is
operator-controlled: a capsule's resources are as trusted as the operator's
decision to install it, and a new capsule *replacing* another's URI may be
intentional. Within that boundary, URI collision is resolved by the deterministic
source-id tie-break (Topics) plus a loud audit — not a security control, a
predictability control. The stronger model — a **signed-registry URI authority**
for an *untrusted* third-party ecosystem — is Future possibilities, not 1.0.

# Drawbacks
[drawbacks]: #drawbacks

- A third capsule interface to maintain. The resource records are larger than
  tools' (the full MCP shape), though most fields are optional.
- Read is broadcast-and-self-filter, not targeted — every read-serving capsule
  wakes for every read and self-filters. Acceptable at the expected capsule count;
  a content-addressed `resource.v1.read.<hash(uri)>` per-URI subscription is the
  escape hatch if it ever matters.
- Per-principal correctness rests on the broker's drain filter and per-principal
  cache keying being implemented — the conformance tests exist to keep them honest.
- `read-response.meta` is carried but unsurfaceable today (forward-compat cost).

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

- **Why mirror the `tool` interface — and where it does NOT.** The
  fan-out/aggregate machinery is proven for tools, so the *structure* is reused.
  But the RFC mirrors the **live mechanism** (empty broadcast request, per-source
  response topics, source-id attribution), not `tool.wit`'s vestigial
  `correlation-id` field — an earlier draft of this RFC mis-copied that dead field
  and is corrected here.
- **Why `resource-contents` is a `variant`.** MCP returns
  `(TextResourceContents | BlobResourceContents)[]` — an array of a discriminated
  union; rmcp models it as an untagged enum. A scalar string cannot represent the
  array, and inferring text-vs-blob from `mime-type` is undecidable. The variant
  is explicit and lossless. (Verified sound.)
- **Why read is broadcast-self-filter, not targeted.** The bus has no
  source-id-targeted publish — only topic-keyed `publish`/`publish_as`. The tool
  path routes only because the name is *in the topic* (`tool.v1.execute.<name>`).
  Targeted read delivery is unimplementable without a per-URI topic, so the read
  is broadcast and capsules self-filter.
- **Why single-page pagination.** Resource cardinality is small; real windowing
  needs immutable snapshot retention the current cache cannot express (it would be
  new machinery, not a broker-only flip). Shipping single-page behind a
  cursor-free capsule contract avoids freezing a pagination model that does not
  work into the 1.0 records.
- **Alternative: expose resources as read-only tools.** Rejected — a tool read
  costs a tool-call slot, clients treat resources and tools differently, and tools
  cannot model multi-content reads.
- **Alternative: hardcode a resource set in the broker/shim.** Rejected —
  capsule-domain knowledge in the router, not extensible, violates broker-is-dumb.

# Prior art
[prior-art]: #prior-art

- **MCP `resources` (2025-11-25 schema), reconciled in full:** `resources/list`
  (**adopt**), `resources/read` with a text/blob content *array* (**adopt/adapt**),
  `ResourceTemplate` + `resources/templates/list` (**defer, non-precluding**),
  `resources/subscribe` + `resources/updated` (**exclude by construction** — no
  server→client push), `list_changed` (**adopt** — broadcast-shaped),
  `Annotations`/`_meta`/`size`/`title`/`icons` (**adopt**, all carried), cursor
  pagination (**single-page in 1.0**), `completion/complete` (**defer** with
  templates).
- **Astrid's own `tool` interface** — the structural precedent; this RFC follows
  its *live* fan-out mechanism (source-id response topics, name/uri dedup), not its
  WIT's vestigial correlation-id.
- **rmcp 1.7.0** (the shim's library) — fixes the concrete reshape targets and the
  default-handler behaviour the shim must override.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **`describe-request` is an empty record.** Keep it as an explicit empty record
  (named, documented, reserved) or drop it and specify "empty payload"? (Lean:
  keep the named record for symmetry and a reserved extension point.)
- **Dedup tie-break direction** — source-id-sorts-*first* vs *last*; either is
  deterministic. Pick one and pin it in the conformance test.
- **Read-serving fan-out cost** at scale — if the read-serving capsule count grows
  large, move to a per-URI content-addressed topic (`resource.v1.read.<hash>`); not
  needed at the expected count.
- **Drain window / TTL values** — adopt the tool surface's (≈ 500 ms describe,
  100 ms slice) as v1 defaults, or tune for resources? Bound the read-miss
  re-describe to a shorter window than full discovery.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Signed-registry URI authority** (the robust answer to URI squatting for an
  *untrusted* ecosystem). A capsule declares its owned `astrid://<namespace>/` in
  its manifest; the registry binds that namespace to the capsule's **signed
  release identity** (releases signed with the author's key); the broker rejects a
  describe/read for a namespace the responding source-id does not own. Ties to the
  registry capsule and the marketplace; out of scope for 1.0's operator-trust
  model.
- **Resource templates** — `resources/templates/list` (RFC 6570 `uriTemplate`).
  Additive: a new `…describe-templates` topic + a `resource-template` record. No
  change to the records here. `completion/complete` rides with it.
- **A subscribe surface** — `resources/subscribe` + update notifications — *if and
  when* Astrid grows a server→client push channel. Re-addable as a new topic +
  flipping the shim's `subscribe` capability; the records here do not preclude it.
- **An SDK `#[astrid::resource(...)]` macro** mirroring `#[astrid::tool(...)]`,
  injecting the Verified-principal-or-fail-closed prologue so per-principal scoping
  is correct by construction.
- **A reference introspection capsule** serving `astrid://capabilities`,
  `astrid://budget`, `astrid://capsules` — the first consumer and the
  per-principal self-awareness surface for agent runtimes on Astrid.
- **The MCP `prompts` primitive** as a sibling interface — distinct from the
  existing `astrid-bus:prompt` *assembly* interface (which should be renamed,
  e.g. `prompt-builder`, before `prompt` is reused for the MCP sense).
