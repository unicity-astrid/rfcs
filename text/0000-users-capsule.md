- Feature Name: `users_capsule`
- Start Date: 2026-05-20
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#747](https://github.com/unicity-astrid/astrid/issues/747)

# Summary
[summary]: #summary

Extract the cross-platform user identity directory out of the kernel into a
first-party capsule (`astrid-capsule-users`) that publishes the
`astrid:users@1.0.0` IPC interface. The kernel keeps principal-level tenancy;
the capsule owns within-principal user attribution — `AstridUserId`,
`FrontendLink`, and the resolve / link / unlink / create / list surface.

This RFC defines the IPC topic schema (`users.v1.*`), the request/response
envelope (correlation IDs for multi-tenant uplinks), the KV storage layout the
capsule must use, and the cutover plan that strips the legacy identity surface
out of `astrid-core`, `astrid-storage`, the WASM host fn module, and the
manifest capability declaration.

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
today, Discord/Telegram/web-passkey tomorrow — touches kernel-side types.
That's domain logic in the kernel.

The capsule design also unlocks adjacent work:

- Uplinks can register new platforms (Nostr, browser extensions, third-party
  webhooks) without kernel changes.
- Identity-as-domain growth (admin/role markers, denylists, identity
  recovery) lives in capsule-space where it belongs.
- The naming collision with `astrid-capsule-identity` (the *agent's* persona)
  goes away.

The cost of the move is small because there are no external consumers
pre-launch: a clean cutover replaces the in-process host fn with IPC RPC.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## What is the users capsule

`astrid-capsule-users` is a first-party capsule that maintains a directory of
canonical `AstridUserId`s and the platform identities linked to them. Every
Astrid principal has its own independent store: different principals never
share user records.

Within a principal, the store answers questions like:

- "Discord user `12345` just messaged me. Who is that?" — `resolve`.
- "Pair Telegram chat `tg:abc` with AstridUser `0000…0001`." — `link`.
- "List every platform Alice is reachable on." — `links`.

The capsule's entire public surface is IPC pub/sub. There is no host function,
no shared in-process state, and no special privilege beyond the per-capsule
KV scope every capsule already has.

## How a multi-tenant uplink talks to it

A multi-tenant uplink (one Astrid principal serving N humans — sphere-capsule,
future Discord-capsule) routes responses back to the originating end-user
using correlation IDs. Each request carries a `source` envelope:

```json
{
  "channel": "discord",
  "user-id": "00000000-0000-4000-8000-deadbeefdead",
  "correlation-id": "9d8a7f3e-..."
}
```

* `channel` — free-form uplink identifier, recorded for audit.
* `user-id` — the requester's own `AstridUserId` when known. `None` for
  pre-login flows.
* `correlation-id` — the token the uplink filters the response topic by.

The uplink:

1. Generates a fresh `correlation-id` (UUID is the obvious choice).
2. Publishes `users.v1.resolve.request` with that correlation.
3. Subscribes to `users.v1.resolve.response`.
4. Drops every response whose `correlation-id` does not match.
5. Routes the matched response back to the originating end-user.

The same correlation pattern works across every operation. The response
topic is **fixed** — no per-correlation suffix — so the uplink only needs
one subscription per operation type for its entire lifetime.

## What lives in the kernel still

- `PrincipalId` (the tenancy primitive).
- Per-principal KV scoping (the capsule's storage substrate).
- Topic ACL enforcement (the capsule declares `[publish]` and `[subscribe]`
  in its manifest; the kernel forbids any other capsule from publishing the
  `users.v1.*.response` topics).

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## IPC topic surface

| Request topic | Response topic | Operation |
|---|---|---|
| `users.v1.resolve.request` | `users.v1.resolve.response` | `(platform, platform-user-id) → option<AstridUser>` |
| `users.v1.link.request`    | `users.v1.link.response`    | Upsert a platform link to an existing user |
| `users.v1.unlink.request`  | `users.v1.unlink.response`  | Remove a platform link |
| `users.v1.create.request`  | `users.v1.create.response`  | Create a new `AstridUser` (optionally with display name) |
| `users.v1.links.request`   | `users.v1.links.response`   | List every link for one user |
| `users.v1.get.request`     | `users.v1.get.response`     | Fetch a user by UUID |
| `users.v1.delete.request`  | `users.v1.delete.response`  | Delete a user and cascade every link pointing at it |
| `users.v1.list.request`    | `users.v1.list.response`    | List every user record in the principal's store |

Each request carries a `source` envelope (above). Each response echoes the
`correlation-id` plus operation-specific success fields and an optional
`error` string.

## WIT schema

Defined in [`unicity-astrid/wit/interfaces/users.wit`](https://github.com/unicity-astrid/wit/blob/main/interfaces/users.wit) as the
`astrid:users@1.0.0` package. Records:

- `source` — request envelope (channel, user-id, correlation-id).
- `astrid-user` — `{ id, display-name?, public-key?, created-at }`.
- `frontend-link` — `{ platform, platform-user-id, astrid-user-id, linked-at, method }`.
- `{op}-request` / `{op}-response` for every operation above.

The bundle is mirrored into both SDKs via `scripts/sync-contracts-wit.sh`,
emitted as `astrid_sdk::contracts::users::*` in Rust and
`import { users } from '@astrid-os/sdk/contracts'` in TypeScript.

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

The capsule's per-principal KV scope holds three key families. Layout is
identical to the legacy `astrid-storage::identity` store so the cutover
reads existing records without migration:

| Key | Value | Purpose |
|---|---|---|
| `user/{uuid}` | JSON `AstridUser` | Canonical user record. |
| `link/{platform}/{platform_user_id}` | JSON `FrontendLink` | Platform link, composite key. |
| `name/{display_name}` | UTF-8 UUID string | Best-effort display-name lookup index (last writer wins). |

`platform` and `platform_user_id` MUST be validated to reject `/` and `\0`
before key construction. Without that gate, a caller passing
`platform = "../user"` could collide with `user/{uuid}` records through the
link path. The validation is part of the contract, not an implementation
detail — every consumer that re-implements this store (e.g. a future kernel
fallback or a non-Rust port) must enforce it identically.

## Error semantics

Each response has an optional `error: string` field. A clean "not found"
result (`resolve` of an unlinked platform identity, `get` of a missing UUID)
returns `error = none` with the success field set to its empty form (`user
= none`, `users = []`, etc.). `error` is populated only when the operation
itself failed — input validation, storage errors, or a `UserNotFound`
returned by `link` when the target UUID does not exist.

## Capsule manifest declarations

```toml
[exports]
"astrid:users" = "1.0.0"

[publish]
"users.v1.resolve.response" = { wit = "@unicity-astrid/wit/users/resolve-response" }
# … one entry per operation, eight total.

[subscribe]
"users.v1.resolve.request" = { wit = "@unicity-astrid/wit/users/resolve-request", handler = "handle_resolve" }
# … one entry per operation, eight total.
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
   - `astrid-capsule::engine::wasm::host::identity` (host fn module + 5 fns).
   - `IdentityResolveRequest`/`IdentityLinkRequest`/… records from
     `contracts/host/astrid-capsule.wit` (kernel host ABI).
   - `CapabilitiesDef::identity` and the
     `identity_capability_satisfies` hierarchy from
     `astrid-capsule::manifest::capabilities`.
   - The `[capabilities].identity = ["…"]` declarations on every existing
     capsule (replaced with `[publish]`/`[subscribe]` entries on the
     `users.v1.*` topics).

Pre-launch, no external consumers. No deprecation window.

# Drawbacks
[drawbacks]: #drawbacks

- **Latency.** Identity calls become async IPC RPCs instead of in-process
  host fns. The roundtrip cost is one bus hop plus serialization — small,
  but non-zero. The SDK wrappers stay sync via subscribe-and-poll-with-
  timeout, which adds a per-call subscription churn (or a long-lived
  subscription with internal request dispatch).
- **Two-way contract change.** Splitting a host fn into an IPC RPC pair is
  a bigger change than tweaking an existing topic. Every uplink that
  resolves user IDs has to migrate. Mitigated by pre-launch timing — no
  external consumers exist.
- **Per-principal isolation.** Two principals on the same kernel cannot
  share user records. The kernel's existing tenancy model already enforces
  this, but the move makes it explicit: there is no "principal-zero" or
  cross-principal admin path. If a federation / cross-principal flow is
  ever needed, it has to be designed top-down.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why a capsule, not "fix the kernel module"

Identity-as-domain *will* grow: admin/role markers, denylists, identity
recovery flows. Every new identity feature added to the kernel module
inflates the kernel surface for capsule authors who don't use it. Putting
the surface in capsule-space puts evolution behind the same versioned
interface contract every other capsule uses.

## Why fixed response topics + correlation IDs

Two designs were considered for the response routing:

1. **Per-request response topic.** Request payload carries
   `response_topic = "users.v1.resolve.response.<correlation>"`; capsule
   publishes there. Used by `prompt_builder.v1.hook.before_build`.
2. **Fixed response topic + correlation ID** (chosen). Capsule always
   publishes to `users.v1.resolve.response`; requester filters by
   `correlation-id`.

The fixed-topic design is cheaper for the requester: one long-lived
subscription per operation type instead of one short-lived subscription per
inflight request. For high-rate operations (`resolve` on every incoming
message), the difference adds up. Per-request topics also create a fan-out
problem in topic ACL evaluation — the kernel walks an ACL set every publish,
and a per-correlation-ID topic name churns the ACL cache.

The downside is that every subscriber sees every response. Within a
principal, this is fine — same-principal capsules can already see each
other's traffic, and the correlation-ID filter is trivially cheap. Across
principals, the per-principal KV scope keeps stores isolated regardless.

## Why include `get` / `delete` / `list` beyond the issue's stated five

The kernel's `IdentityStore` trait already has `get_user`, `get_user_by_name`,
`delete_user`, and `list_users`. The kernel uses `delete_user` from the
admin handlers (`kernel_router::admin::handlers`). Without these in the
capsule surface, the cutover would leave the admin handlers in a partial
state, OR the SDK would need a fallback path. Including them now keeps the
migration mechanical.

`get_user_by_name` is the lone holdout — it's an internal kernel call site,
not part of the SDK surface, and the name index is best-effort
last-writer-wins. The capsule keeps the underlying KV entry but doesn't
expose a topic for it. Add later if a use case appears.

## Why a `source` envelope on every request, not just where uplinks need it

Three options:

1. Optional `source` on every request, fill in only where needed.
2. Required `source` on every request, with sentinel values
   (`channel = "system"`, `correlation-id = "—"`).
3. Two parallel topic namespaces (single-tenant vs. multi-tenant).

(3) doubles the surface — not worth it. (1) makes the protocol depend on
which operations happen to have multi-tenant callers today, which is the
brittle option. (2) is simple and forces every caller to think about
attribution.

# Prior art
[prior-art]: #prior-art

- **Matrix's identity service** — a separate service from the homeserver,
  maps third-party identifiers (email, phone) to Matrix user IDs. Same
  architectural decision: identity-as-domain is its own service, not part
  of the routing kernel.
- **OAuth resource servers** — opaque identifier mapping is owned by a
  resource server, not the authorization server. Astrid's split mirrors
  this: principal tenancy (kernel) is distinct from within-principal
  identity directory (capsule).
- **`urn:` URN registries** — platform-namespaced identifier mapping is a
  well-trodden pattern; this RFC reinvents none of that, just declares
  `platform` as the namespace and `platform_user_id` as the opaque value.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Auto-create on first resolve.** Should the capsule expose a
  `resolve-or-create` topic that handles "I see Discord user `12345`,
  give me an `AstridUser` (creating one if needed)" in one round trip?
  Today the caller does `resolve` then `create` then `link` — three
  hops. Likely yes; punt to a follow-up RFC once a concrete consumer
  needs it.
- **Role markers / ACLs.** The issue floats a `principal_relation` enum
  (`admin` / `peer` / `unknown`) on `FrontendLink`. This RFC does not
  include it. The intent is to add a separate `astrid:authz@1.0.0`
  interface that consumes `users.v1.*` outputs, rather than baking
  policy into the identity record itself. Open question whether that
  separate interface lives in this capsule or another.
- **SDK sync vs async.** The kernel-side host fn is synchronous from
  the caller's perspective. IPC RPC is inherently async (publish,
  await response). The SDK can:
    1. Keep sync semantics — internal subscribe + poll loop with timeout.
    2. Expose async fns — cleaner, matches reality.
  Recommend (2) for greenfield code; expose a (1) facade for the
  cutover so existing call sites don't all rewrite at once.
- **#748 envelope hoisting.** If #748's final envelope shape diverges
  from `users.wit`'s, the hoist becomes a breaking change against
  `astrid:users@1.0.0`. Mitigated by keeping the field names identical
  in both drafts; revisit at #748 merge.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Federation / cross-principal lookup.** A separate capsule could
  publish federation requests across principals (with appropriate
  cross-principal capability gating). The users capsule would not change
  — federation is a layer above identity.
- **Webhook-driven link verification.** A future `users.v1.verify.request`
  could carry a cryptographic challenge the platform-side endpoint signs.
  This RFC explicitly does not include verification — `method` is just a
  free-form audit string today.
- **Capsule-to-capsule queries with explicit grants.** Once the
  authorization story (above) lands, an admin capsule could be granted
  read-only access to `users.v1.list.*` for reporting, while consumer
  capsules only see `resolve`.
- **Audit log topic.** Every mutation (`link`, `unlink`, `create`,
  `delete`) could fan out to `users.v1.audit.event` so an audit capsule
  observes the full mutation stream without polling. Out of scope here.
- **Public-key authentication.** `AstridUser.public_key` is reserved in
  the type but unused by this RFC. A future RFC defines the verification
  flow (e.g. ed25519 signed `users.v1.verify.request` payloads).
