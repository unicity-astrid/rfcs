- Feature Name: `users_capsule`
- Start Date: 2026-05-20
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#747](https://github.com/unicity-astrid/astrid/issues/747)

# Summary
[summary]: #summary

Extract the cross-platform user identity directory out of the kernel into a
first-party capsule (`astrid-capsule-users`) that publishes the
`astrid:users@1.0.0` IPC interface. The kernel keeps principal-level tenancy;
the capsule owns within-principal user attribution.

The interface is built around a **two-layer identity model**:

1. **Identity layer** — `FrontendLink` maps a platform's stable opaque ID
   (Discord snowflake, Telegram user-id, Nostr pubkey, Slack workspace+user)
   to a canonical `AstridUserId`. Global per
   `(platform, platform-instance, platform-user-id)` triple.
2. **Presentation layer** — `ContextIdentity` overlays a per-context display
   name on a link (Discord guild nickname, Matrix room display name, Slack
   channel profile). Optional and orthogonal to identity.

`resolve` is context-aware: returns the layered display name in one
round-trip (context > link > canonical).

This RFC defines the IPC topic schema (`users.v1.*`), the request envelope,
the KV storage layout, validation invariants, the cutover plan from the
kernel-side surface, and a documented set of follow-up topics.

# Motivation
[motivation]: #motivation

The kernel rule is "the kernel routes events and enforces capabilities;
everything else is capsule-space business logic." Today the kernel violates
that rule with the identity subsystem:

- `astrid-core::identity::types` owns `AstridUserId` and `FrontendLink`.
- `astrid-storage::identity` owns the `IdentityStore` trait and a KV-backed
  implementation with eight async methods.
- `astrid-capsule::engine::wasm::host::identity` exposes five `identity_*`
  host functions to WASM guests.
- `astrid-capsule::manifest::capabilities` declares the `identity = [...]`
  capability with a three-tier hierarchy (`resolve` / `link` / `admin`).

Every new uplink that wants to attribute messages to a user — sphere-capsule
today, Discord/Slack/Telegram/Matrix/Nostr tomorrow — touches kernel-side
types. That's domain logic in the kernel.

A platform-API audit (Discord, Slack, Telegram, X, Matrix, Mastodon, IRC,
Email, SMS, Nostr, Passkey, GitHub) surfaced two recurring shape mismatches
the legacy kernel store could not express:

1. **Instance scoping.** Slack `U01234` in workspace A is not the same human
   as `U01234` in workspace B. IRC and XMPP have the same per-network split.
   The legacy `(platform, platform-user-id)` composite key cannot express
   this without conventions every uplink must agree on by hand.
2. **Per-context presentation.** A Discord user has different nicknames per
   guild, a Matrix user different display names per room, a Slack user
   different profiles per workspace. Identity is global; presentation is
   contextual. Forcing one display name per link loses the per-context view.

The capsule design takes both into account from 1.0 — they are wire-format
changes that would be breaking to add later.

The move also unlocks adjacent work:

- Uplinks can register new platforms (Nostr, browser extensions, third-party
  webhooks) without kernel changes.
- Identity-as-domain growth (role markers, denylists, identity recovery)
  lives in capsule-space behind a versioned interface contract.
- The naming collision with `astrid-capsule-identity` (the *agent's* persona)
  goes away.

The cost of the move is small because there are no external consumers
pre-launch: a clean cutover replaces the in-process host fn with IPC RPC.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## What is the users capsule

`astrid-capsule-users` is a first-party capsule that maintains a directory
of canonical `AstridUserId`s and the platform identities linked to them.
Every Astrid principal has its own independent store; different principals
never share user records.

Within a principal, the store answers questions like:

- "Discord user `12345` just messaged me. Who is that?" — `resolve`.
- "Pair Telegram chat `tg:abc` with AstridUser `0000…0001`." — `link`.
- "List every platform Alice is reachable on." — `links`.
- "What should I call this Discord user in *this* guild right now?" —
  `resolve` with `context-id` set.

The capsule's entire public surface is IPC pub/sub. There is no host
function, no shared in-process state, and no privilege beyond the
per-capsule KV scope every capsule already has.

## Identity model

```
   AstridUser           — canonical Astrid identity (UUID + canonical display_name)
       ▲
       │
       │ many-to-one
       │
   FrontendLink         — global per (platform, instance?, platform_user_id)
                          Discord snowflake, Telegram user-id, Slack U+T pair, etc.
       ▲
       │
       │ many-to-one (one overlay per context)
       │
   ContextIdentity      — per-context presentation override
                          Discord per-guild nickname, Matrix per-room display name
```

A user has **one** `AstridUser` record, **many** `FrontendLink`s (one per
platform identity), and **optionally many** `ContextIdentity` overlays per
link (one per context the link is presented in).

## How a multi-tenant uplink talks to it

A multi-tenant uplink (one Astrid principal serving N humans —
sphere-capsule, future Discord-capsule) routes responses back to the
originating end-user using correlation IDs. Each request carries a `source`
envelope:

```json
{
  "uplink": "discord",
  "user-id": "00000000-0000-4000-8000-deadbeefdead",
  "correlation-id": "9d8a7f3e-..."
}
```

* `uplink` — free-form identifier of the calling capsule (audit, routing).
* `user-id` — the requester's own `AstridUserId` when known. `None` for
  pre-login flows.
* `correlation-id` — the token the uplink filters the response topic by.

The uplink:

1. Generates a fresh `correlation-id`.
2. Publishes `users.v1.<op>.request` with that correlation.
3. Subscribes to `users.v1.<op>.response`.
4. Drops every response whose `correlation-id` does not match.
5. Routes the matched response back to the originating end-user.

The response topic is **fixed** (no per-correlation suffix), so one
long-lived subscription per operation suffices for an uplink's lifetime.

## What lives in the kernel still

- `PrincipalId` (the tenancy primitive).
- Per-principal KV scoping (the capsule's storage substrate).
- Topic ACL enforcement.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## IPC topic surface

Fifteen request/response pairs across five operation groups.

**Identity / link CRUD (4):**

| Request | Response | Operation |
|---|---|---|
| `users.v1.resolve.request` | `users.v1.resolve.response` | `(platform, instance?, user-id, context-id?) → option<AstridUser>` + layered display name |
| `users.v1.link.request`    | `users.v1.link.response`    | Upsert a platform link |
| `users.v1.unlink.request`  | `users.v1.unlink.response`  | Remove a link (cascades context overlays) |
| `users.v1.create.request`  | `users.v1.create.response`  | Create a new `AstridUser` |

**AstridUser mutation (2):**

| Request | Response | Operation |
|---|---|---|
| `users.v1.set_display_name.request` | `users.v1.set_display_name.response` | Change canonical display name without rotating UUID |
| `users.v1.set_public_key.request`   | `users.v1.set_public_key.response`   | Set / clear public key without rotating UUID |

**Lookup / admin (4):**

| Request | Response | Operation |
|---|---|---|
| `users.v1.links.request`  | `users.v1.links.response`  | List every link for one user |
| `users.v1.get.request`    | `users.v1.get.response`    | Fetch user by UUID |
| `users.v1.delete.request` | `users.v1.delete.response` | Delete user + cascade links + cascade overlays |
| `users.v1.list.request`   | `users.v1.list.response`   | Paginated user listing |

**Per-context presentation overlay (5):**

| Request | Response | Operation |
|---|---|---|
| `users.v1.context.set.request`               | `users.v1.context.set.response`               | Upsert a per-context display-name override |
| `users.v1.context.clear.request`             | `users.v1.context.clear.response`             | Clear one overlay |
| `users.v1.context.get.request`               | `users.v1.context.get.response`               | Fetch one overlay + resolved AstridUser |
| `users.v1.context.list_for_user.request`     | `users.v1.context.list_for_user.response`     | Paginated overlays for one user across contexts |
| `users.v1.context.list_in_context.request`   | `users.v1.context.list_in_context.response`   | Paginated members of one context |

Each request carries a `source` envelope. Each response echoes the
`correlation-id` plus operation-specific success fields and an optional
`error` string.

## WIT schema

Defined in [`unicity-astrid/wit/interfaces/users.wit`](https://github.com/unicity-astrid/wit/blob/main/interfaces/users.wit) as the `astrid:users@1.0.0` package. Records:

- `source` — request envelope `{ uplink, user-id?, correlation-id }`.
- `astrid-user` — `{ id, display-name?, public-key?, created-at }`.
- `frontend-link` — `{ platform, platform-instance?, platform-user-id,
  astrid-user-id, linked-at, method, display-name? }`.
- `context-identity` — `{ platform, platform-instance?, platform-user-id,
  context-id, display-name, updated-at }`.
- `context-member` — `{ context-identity, astrid-user-id? }` (returned by
  `context.list_in_context`).
- `{op}-request` / `{op}-response` records for every operation above.

The bundle is mirrored into both SDKs via `scripts/sync-contracts-wit.sh`,
emitted as `astrid_sdk::contracts::users::*` in Rust and
`import { users } from '@astrid-os/sdk/contracts'` in TypeScript.

## Platform-instance scoping

Federated and multi-tenant platforms (Slack workspace, IRC network, XMPP
server, Mattermost instance) carry an explicit `platform-instance: option<string>`
on every link-keyed record. `None` for globally-scoped platforms (Discord,
Telegram, X) and for federated platforms whose identifier already embeds
the homeserver (Matrix `@alice:server.org`, Mastodon `@alice@server.social`).

Examples:

| Platform | platform | platform-instance | platform-user-id |
|---|---|---|---|
| Discord | `"discord"` | `None` | `"123456789012345678"` |
| Slack | `"slack"` | `Some("T01234ABCDE")` | `"U01234ABCDE"` |
| Telegram | `"telegram"` | `None` | `"123456789"` |
| Matrix | `"matrix"` | `None` | `"@alice:server.org"` |
| IRC | `"irc"` | `Some("libera.chat")` | `"alice"` |
| Nostr | `"nostr"` | `None` | `"npub1..."` |
| Email | `"email"` | `None` | `"alice@example.com"` |

A dedicated field (vs. composing instance into `platform-user-id`) prevents
convention drift across uplinks: two independently-written Slack uplinks
cannot disagree on a separator and silently fail to resolve each other's
records.

## Context-aware resolve

`resolve` accepts an optional `context-id`. When set, the response's
`display-name` and `display-name-source` use the first non-empty layer:

1. `context-identity.display-name` (if `context-id` matches an overlay)
2. `frontend-link.display-name` (the platform's global name at link time)
3. `astrid-user.display-name` (the operator/canonical Astrid name)

`display-name-source` reports which layer (`"context"`, `"link"`,
`"canonical"`) produced the result, so the caller can audit and the
prompt-builder can rendre the right text for the right context in one
round-trip.

`context-id` is **opaque to the capsule** — uplinks define their own
schemes:

| Platform | Suggested `context-id` scheme |
|---|---|
| Discord per-guild | `"guild:{guild-id}"` |
| Matrix per-room | `"room:{room-id}"` |
| Slack per-channel | `"channel:{channel-id}"` (workspace lives in `platform-instance`) |
| Mattermost per-team | `"team:{team-id}"` |
| Single-context platforms (X, SMS) | not used |

## Source envelope vs. issue #748

The `source` envelope intentionally mirrors the multi-tenant correlation
convention that admin-API issue [astrid#748](https://github.com/unicity-astrid/astrid/issues/748)
will eventually formalize for `cli.v1.command.execute`. Two things to note:

1. **This RFC does not depend on #748 landing first.** Defining `source`
   inside `astrid:users@1.0.0` was the cheapest path to ship; the convention
   is local to this interface until #748 hoists it.
2. **When #748 lands, the convention hoists.** A future shared envelope
   package (working name `astrid:envelope@1.0.0`) replaces the in-interface
   record and `users.wit` rewrites its imports. The wire shape stays
   compatible — kebab-case field names already match #748's draft.

## Storage layout

The capsule's per-principal KV scope holds four key families:

| Key | Value | Purpose |
|---|---|---|
| `user/{uuid}` | JSON `AstridUser` | Canonical user record. |
| `link/{platform}/{instance\|_}/{platform-user-id}` | JSON `FrontendLink` | Identity link, composite key. |
| `name/{display-name}` | UTF-8 UUID string | Best-effort display-name lookup index (last writer wins). |
| `context/{platform}/{instance\|_}/{context-id}/{platform-user-id}` | JSON `ContextIdentity` | Per-context overlay. |

`_` (single underscore) is the sentinel for `platform-instance = None`,
chosen because the validation layer rejects `_` from being a real instance
value, so it cannot collide with a legitimate Slack workspace / IRC network
identifier.

### Validation invariants

`platform`, `platform-user-id`, `platform-instance`, and `context-id` are
all validated to reject `/` and `\0` before key construction. Without that
gate, a caller passing `platform = "../user"` could collide with
`user/{uuid}` records through the link path. `platform-instance` additionally
rejects the literal `"_"` (reserved sentinel).

These are **contract-level** invariants — every consumer that re-implements
this store (a future kernel fallback, a non-Rust port) must enforce them
identically.

### Cascade semantics

| Operation | Cascades |
|---|---|
| `unlink` | Every `context/.../{platform-user-id}` overlay attached to that link |
| `delete_user` | Every link pointing at the user, *then* every overlay tied to those links, *then* the name-index entry (only if it still points at the deleted UUID) |

### Value-shape divergence from the legacy kernel store

The key scheme matches the legacy `astrid-storage::identity` store
byte-for-byte. Value shapes follow the WIT contract rather than the
kernel's Rust serialization — three deliberate divergences:

* `AstridUser.public_key` is `option<list<u8>>` (the WIT shape); the
  kernel encodes the same bytes as a base64 string.
* `created_at` / `linked_at` are millisecond-precision RFC 3339
  strings; the kernel uses chrono's default microsecond precision.
* `AstridUser` carries no `principal` field. The capsule's per-
  principal KV scope already encodes the principal, so the kernel
  record's `principal: PrincipalId` is redundant and dropped on the
  first capsule-side re-write.

Pre-launch there are no production records to migrate. A migration tool
added at cutover time transforms kernel records into capsule records
(base64-decode public keys, reformat timestamps, strip principal).

## Pagination

`list`, `context.list_for_user`, and `context.list_in_context` are
paginated by an opaque `cursor: option<string>` (passed back from prior
`next-cursor`) plus an optional `limit: option<u32>` (default 100,
capped at 1000). On the wire pagination is uniform across topics.

## Error semantics

Each response has an optional `error: string`. A clean "not found"
result (`resolve` of an unlinked identity, `get` of a missing UUID,
`clear_context` of an absent overlay) returns `error = none` with the
success field set to its empty form. `error` is populated only when the
operation itself failed — input validation, storage errors, or
`UserNotFound` returned by mutations targeting a missing UUID.

## Capsule manifest declarations

```toml
[exports]
"astrid:users" = "1.0.0"

[publish]
"users.v1.resolve.response" = { wit = "@unicity-astrid/wit/users/resolve-response" }
# … 15 entries, one per operation.

[subscribe]
"users.v1.resolve.request" = { wit = "@unicity-astrid/wit/users/resolve-request", handler = "handle_resolve" }
# … 15 entries, one per operation.
```

No host capabilities are declared. The capsule needs only its per-capsule
KV scope, granted by default.

## Migration / cutover plan

The kernel-side identity surface is removed in a separate PR once the SDK
wrappers cut over:

1. **Land within-principal correlation convention** ([astrid#748](https://github.com/unicity-astrid/astrid/issues/748))
   — *not a hard dependency.* This RFC ships its own envelope. Hoist later.
2. **Build `astrid-capsule-users`** — this RFC.
3. **Rewrite SDK wrappers.** `astrid_sdk::identity::*` and
   `@astrid-os/sdk` identity helpers stop calling the host fn and start
   publishing IPC RPCs (subscribe-and-poll with timeout). Consumer code
   keeps the same external API.
4. **Strip kernel-side identity.** Delete:
   - `astrid-core::identity::{types, mod}` (modules and re-exports).
   - `astrid-storage::identity` (trait + KV impl).
   - `astrid-capsule::engine::wasm::host::identity` (host fn module).
   - `IdentityResolveRequest`/`IdentityLinkRequest`/… records from
     `contracts/host/astrid-capsule.wit` (kernel host ABI).
   - `CapabilitiesDef::identity` + the `identity_capability_satisfies`
     hierarchy from `astrid-capsule::manifest::capabilities`.
   - Every `[capabilities].identity = ["…"]` declaration (replaced
     with `[publish]`/`[subscribe]` entries on the `users.v1.*` topics).

Pre-launch, no external consumers. No deprecation window.

# Drawbacks
[drawbacks]: #drawbacks

- **Latency.** Identity calls become async IPC RPCs instead of in-process
  host fns. Roundtrip cost is one bus hop plus serialization. Sync SDK
  wrappers add a per-call subscription churn (or a long-lived subscription
  with internal request dispatch).
- **Two-way contract change.** Splitting a host fn into an IPC RPC pair
  is a bigger change than tweaking an existing topic. Mitigated by
  pre-launch timing — no external consumers exist.
- **Per-principal isolation.** Two principals on the same kernel cannot
  share user records. The kernel's existing tenancy model already enforces
  this, but the move makes it explicit: there is no "principal-zero" or
  cross-principal admin path. If a federation / cross-principal flow is
  ever needed, it has to be designed top-down.
- **Surface size.** Fifteen topic pairs is larger than the kernel's
  five-host-fn surface. Each is a discrete operation; the alternative
  (one omnibus `users.v1.exec` topic) trades schema clarity for fewer
  ACL entries. We chose discrete topics.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why a capsule, not "fix the kernel module"

Identity-as-domain *will* grow (admin/role markers, denylists, identity
recovery flows). Every new identity feature added to the kernel module
inflates the kernel surface for capsule authors who don't use it. Putting
the surface in capsule-space puts evolution behind the same versioned
interface contract every other capsule uses.

## Why a two-layer identity model

Identity is global; presentation is contextual. The same human has one
stable `AstridUserId` everywhere, but how the agent should address them
depends on which Discord guild / Matrix room / Slack channel they're in
right now.

Three places per-context presentation could live; only one is principled:

| Approach | Trade-off |
|---|---|
| Encode context into `platform-user-id` (`"guild-A:user-123"`) | One link per (context, user). Resolving "this Discord user anywhere on Discord" requires prefix scans; cross-context unification is ambiguous. |
| Uplink-side cache | Each new uplink reimplements per-context naming. No cross-uplink coherence. The users capsule isn't actually the source of truth. |
| **Two-layer model** (identity link + per-context overlay) | More surface, but the only design where "who am I talking to" and "what do I call them right now" both have a single owner. |

Per-context display **belongs in this capsule** because it lets `resolve`
return the right name in one round-trip; pushing it uplink-side means the
prompt-builder hops twice.

## Why fixed response topics + correlation IDs

Two designs were considered:

1. **Per-request response topic.** Request payload carries
   `response_topic = "users.v1.resolve.response.<correlation>"`; capsule
   publishes there.
2. **Fixed response topic + correlation ID** (chosen). Capsule always
   publishes to `users.v1.resolve.response`; requester filters by
   `correlation-id`.

Fixed-topic is cheaper for the requester: one long-lived subscription per
operation type instead of one short-lived subscription per inflight
request. For high-rate operations (`resolve` on every incoming message),
the difference adds up. Per-request topics also create a fan-out problem
in topic ACL evaluation — the kernel walks an ACL set every publish, and
a per-correlation-ID topic name churns the ACL cache.

The downside is that every subscriber sees every response. Within a
principal, this is fine — same-principal capsules can already see each
other's traffic, and the correlation-ID filter is trivially cheap.

## Why `platform-instance` as a dedicated field

Two designs considered for Slack-style workspace scoping:

1. **Convention**: uplinks compose `platform-user-id = "{workspace}.{user}"`.
   Cheap, no schema change. Drift risk: every Slack uplink must agree
   on the separator or resolution breaks across uplinks.
2. **Dedicated field**: `platform-instance: option<string>` (chosen).
   Explicit, never ambiguous. Cost: one optional field on every record.

Slack is a real near-term target; one field, one-time cost, prevents an
entire class of resolution bugs.

## Why include `get` / `delete` / `list` / `set_*` beyond a minimal CRUD

The kernel's `IdentityStore` already has `get_user`, `delete_user`, and
`list_users`. The kernel uses `delete_user` from admin handlers. Without
these in the capsule surface, the cutover would leave the admin handlers
in a partial state. `set_display_name` and `set_public_key` close the
mutation gap the legacy store didn't have but that real lifecycle needs
(name changes, key rotation) — without rotating the UUID and breaking
every existing link.

## Why a `source` envelope on every request

Three options considered:

1. Optional `source`, fill in only where needed. Makes the protocol depend
   on which operations have multi-tenant callers today, which is brittle.
2. Required `source` with sentinel values (`uplink = "system"`,
   `correlation-id = "—"`) — chosen. Forces every caller to think about
   attribution.
3. Two parallel topic namespaces (single-tenant vs. multi-tenant). Doubles
   the surface — not worth it.

# Prior art
[prior-art]: #prior-art

- **Matrix's identity service** — a separate service from the homeserver,
  maps third-party identifiers (email, phone) to Matrix user IDs. Same
  architectural decision: identity-as-domain is its own service, not part
  of the routing kernel.
- **OAuth resource servers** — opaque identifier mapping is owned by a
  resource server, not the authorization server.
- **`urn:` URN registries** — platform-namespaced identifier mapping is a
  well-trodden pattern; this RFC reinvents none of that.
- **IRC NickServ / Slack profiles** — per-server/per-workspace identity
  registries are the same kind of overlay design.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **`resolve_or_create` follow-up.** Filed as [astrid#749](https://github.com/unicity-astrid/astrid/issues/749).
  Common-case bootstrap is currently three round-trips (resolve → create
  → link); the new topic collapses it to one. Atomic-from-the-uplink's-POV,
  avoids the orphan-user-on-step-3-failure case.
- **Role markers / ACLs.** A `principal-relation` enum
  (`admin` / `peer` / `unknown`) could go on `FrontendLink`, OR be owned
  by a separate `astrid:authz@1.0.0` interface that consumes
  `users.v1.*` outputs. Current intent: separate interface. Open which.
- **SDK sync vs async.** The kernel-side host fn is synchronous from the
  caller's perspective. IPC RPC is inherently async. The SDK can keep
  sync semantics (internal subscribe + poll loop with timeout) or expose
  async fns. Recommend async for greenfield; offer a sync facade for the
  cutover so existing call sites don't all rewrite at once.
- **`merge-users` operation.** If an uplink creates a duplicate
  `AstridUser` before discovering the existing one, recovery today is
  `unlink-everything → delete-user → re-link` per platform. Works, but
  operators will hit this. A `merge` topic would cascade reassignment.
- **#748 envelope hoisting.** If #748's final shape diverges from
  `users.wit`'s `source`, the hoist becomes a breaking change. Mitigated
  by keeping field names identical in both drafts; revisit at #748 merge.

# Future possibilities
[future-possibilities]: #future-possibilities

- **`resolve_or_create.request` topic** — atomic bootstrap (astrid#749).
- **`resolve_many.request`** — batch lookup for group-member roster
  queries that today require N round-trips.
- **Cryptographic link verification** — a `users.v1.verify.request` flow
  where the platform-side endpoint signs a challenge to attest a link
  claim. Today `method` is audit-only.
- **Mutation event stream** — fan out every link/unlink/create/delete/
  set_* / context.* to `users.v1.audit.event` so audit capsules observe
  the full mutation stream without polling.
- **Public-key authentication.** `AstridUser.public_key` is reserved in
  the type but unused. A future RFC defines verification flow (e.g.
  ed25519 signed payload authentication).
- **Federation / cross-principal lookup.** A separate capsule could
  publish federation requests across principals (with appropriate
  cross-principal capability gating). The users capsule would not change.
- **`get_by_display_name` topic.** The kernel had this internally; not
  exposed today. Add if a real use case appears.
- **Capsule-to-capsule queries with explicit grants.** Once the authz
  story (above) lands, an admin capsule could be granted read-only access
  to `users.v1.list.*` for reporting while consumer capsules only see
  `resolve`.

Group/room/channel entities are explicitly **not** modelled in this
capsule — they are routing context, not identity. If a real need surfaces,
they live in a separate `astrid:groups@1.0.0` capsule.
