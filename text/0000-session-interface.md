- Feature Name: `session_interface`
- Start Date: 2026-06-24
- RFC PR: [rfcs#34](https://github.com/unicity-astrid/rfcs/pull/34)
- Tracking Issue: [astrid#974](https://github.com/unicity-astrid/astrid/issues/974)

# Summary
[summary]: #summary

A standard `astrid-bus:session` interface defining the **conversation-thread
contract**: the verbs, payloads, topic conventions, metadata model, lifecycle
events, and per-principal scoping that any conforming *session capsule* must
implement. It lets the kernel, the HTTP gateway, the operator CLI, the agent
loop, and third-party clients depend on the **contract**, not on a particular
implementation — so an alternative session store (encrypted-at-rest,
cloud-synced, ephemeral/in-memory, or a bespoke per-deployment backend) is a
drop-in replacement.

This RFC specifies version `1.1.0` of the interface. It is a `astrid-bus:*`
(capsule-to-capsule) contract, governed by the topic-versioning convention per
[the Host ABI RFC](https://github.com/unicity-astrid/rfcs/pull/22), not by the
kernel-to-capsule host ABI. This document is its written standard.

# Motivation
[motivation]: #motivation

A conversation runtime needs durable, per-principal conversation history: the
agent loop appends turns and reads history when building an LLM request; a
multi-device client lists threads, renders a transcript, renames/archives/deletes
threads, searches across them, and mirrors in-flight turns live; the agent
itself recalls its own past threads.

Today a single capsule, `astrid-capsule-session`, is the de-facto store, and its
shape is *implicit*: the gateway routes, the CLI, and the agent loop hardcode
topic strings and JSON shapes that exist only in that capsule's source. There is
no published contract, so:

- A third party cannot write a conforming session capsule (e.g. one that
  encrypts transcripts at rest, syncs across a fleet, or enforces a retention
  policy) without reverse-engineering the incumbent.
- A client cannot know which capabilities a deployed session capsule supports,
  or what a thread's metadata looks like, without reading capsule source.
- Two session implementations cannot be reasoned about as interchangeable.

Standardising the interface fixes this. The contract is small, additive-friendly
(a deliberate `meta` escape hatch and topic-versioning keep it evolvable), and
strictly per-principal, so it is safe to expose across devices and to untrusted
agents.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## What a session capsule is

A session capsule is a **per-principal conversation store**. It owns *threads*:
ordered lists of messages, addressed by a `session-id` string, plus lightweight
metadata. It never transforms message content — clean in, clean out. Prompt
assembly, system-prompt injection, and context compaction are per-turn concerns
that belong elsewhere; the session capsule only persists and serves history.

A conforming capsule **exports** `astrid:session` and handles the request verbs
on the IPC bus, replying on per-request scoped topics and fanning out lifecycle
notifications.

## The thread model

- A thread is `(session-id, ordered messages, metadata)`.
- The well-known id `"default"` is the implicit thread used when a caller omits
  a `session-id`.
- Threads form a backward linked list via `parent-session-id`: clearing a thread
  mints a *new* thread whose parent points at the old one, which is preserved.
  History is never silently truncated.
- Metadata per thread: an optional user-set `title`, a derived `preview` (first
  user message) and `last-message-preview` (most recent message), a
  `message-count`, `created-at`/`updated-at` timestamps, an `archived` flag, the
  `parent-session-id`, and an opaque client `meta` blob.

## Per-principal isolation (the central invariant)

Every operation is scoped to the **invoking principal**. The kernel namespaces a
capsule's key-value store per `(principal, capsule)`, and stamps the acting
principal onto every event a capsule publishes. A conforming capsule **MUST**
rely on this — it keys storage only within its kernel-scoped namespace and never
trusts a principal named in a request payload. Consequently a caller can only
ever list, read, search, or mutate *its own* threads; cross-principal access is
structurally impossible, not policy-enforced. This is what makes the surface
safe to expose over HTTP to paired devices and to untrusted agent tools.

## Talking to a session capsule

`append` is fire-and-forget: a capsule subscribes `session.v1.append` and
publishes no reply, so it carries no correlation id. Every **other** verb is
request/response over string topics:

- The request is published on `session.v1.request.<verb>` carrying a
  `correlation-id`.
- The reply is published on the **per-request scoped** topic
  `session.v1.response.<verb>.<correlation-id>`. Scoping the reply topic to the
  correlation id (rather than a shared response topic filtered by id) means a
  concurrent caller's reply can never be delivered to the wrong waiter.

For these request/response verbs the `correlation-id` is an opaque,
caller-chosen token that is a **single dot-free topic segment** (a UUIDv4 is the
canonical choice); a capsule MUST reject a missing, empty, or dot-containing
correlation id.

## Live multi-device sync

After a thread is created (via `clear`), updated, or deleted, a conforming
capsule fans out a `session.v1.event.<kind>` notification (`created` /
`updated` / `deleted`). Because the bus stamps it with
the acting principal, a per-principal subscriber — e.g. a gateway's live feed
shared across a principal's devices — receives only its own events, and a second
device updates its thread list the instant the first renames or deletes a thread,
without polling.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## IPC topic surface

| Topic | Shape | Direction | Reply |
|-------|-------|-----------|-------|
| `session.v1.append` | `append-request` | → capsule | none (fire-and-forget) |
| `session.v1.request.get_messages` | `get-messages-request` | → capsule | `session.v1.response.get_messages.<corr>` |
| `session.v1.request.clear` | `clear-request` | → capsule | `session.v1.response.clear.<corr>` |
| `session.v1.request.list` | `list-request` | → capsule | `session.v1.response.list.<corr>` |
| `session.v1.request.get_meta` | `get-meta-request` | → capsule | `session.v1.response.get_meta.<corr>` |
| `session.v1.request.update` | `update-request` | → capsule | `session.v1.response.update.<corr>` |
| `session.v1.request.delete` | `delete-request` | → capsule | `session.v1.response.delete.<corr>` |
| `session.v1.request.search` | `search-request` | → capsule | `session.v1.response.search.<corr>` |
| `session.v1.event.created` / `.updated` / `.deleted` | `session-event` | capsule → (fan-out) | none |

## WIT schema

The contract is the `astrid-bus:session@1.1.0` package, defined canonically as
`interfaces/session.wit` in the [`unicity-astrid/wit`](https://github.com/unicity-astrid/wit)
repository (and mirrored into each SDK's contracts bundle) — it does not live in
this RFC repo. A capsule does **not** bind these via the component
linker (bus interfaces are advisory payload specs); it publishes and subscribes
by string topic and serialises with serde JSON. The records define the wire
shape:

```wit
package astrid-bus:session@1.1.0;

interface session {
    use astrid-bus:types/types.{message};

    // — 1.0.0 (unchanged) —
    record get-messages-request  { session-id: string, correlation-id: string,
                                    append-before-read: option<list<message>> }
    record get-messages-response { correlation-id: string, messages: list<message> }
    record clear-request   { session-id: string, correlation-id: string }
    record clear-response  { correlation-id: string, new-session-id: string,
                             old-session-id: string }
    record append-request  { session-id: string, messages: list<message> }
    // session-cleared is a legacy 1.0.0 fan-out the AGENT LOOP (not the session
    // capsule) publishes on `session.v1.clear` after a clear, so unrelated
    // capsules can drop per-session ephemeral state — distinct from this
    // capsule's clear-response and from the session.v1.event.* lifecycle events.
    record session-cleared { session-id: string }

    // — 1.1.0 (additive) —
    record list-request {
        correlation-id: string, cursor: option<string>, limit: option<u32>,
        include-archived: bool,
    }
    record session-summary {
        session-id: string, title: option<string>, preview: option<string>,
        last-message-preview: option<string>, message-count: u32,
        created-at: option<u64>, updated-at: option<u64>, archived: bool,
        parent-session-id: option<string>, meta: option<string>,
    }
    record list-response {
        correlation-id: string, sessions: list<session-summary>,
        next-cursor: option<string>, total: option<u32>,
    }
    record get-meta-request  { correlation-id: string, session-id: string }
    record get-meta-response { correlation-id: string, session: option<session-summary> }
    record update-request {
        correlation-id: string, session-id: string, title: option<string>,
        archived: option<bool>, meta: option<string>,
    }
    record update-response { correlation-id: string, session: option<session-summary> }
    record delete-request  { correlation-id: string, session-id: string }
    record delete-response { correlation-id: string, deleted: bool }
    record search-request {
        correlation-id: string, query: string, limit: option<u32>,
        cursor: option<string>, include-archived: bool,
    }
    record search-result {
        session-id: string, title: option<string>, snippet: option<string>,
        match-count: u32, updated-at: option<u64>,
    }
    record search-response {
        correlation-id: string, results: list<search-result>,
        next-cursor: option<string>,
    }
    variant session-event-kind { created, updated, deleted }
    record session-event {
        kind: session-event-kind, session-id: string,
        summary: option<session-summary>,
    }
}
```

JSON field names are the snake_case form of the WIT kebab-case (`session_id`,
`include_archived`, `last_message_preview`, …). For every verb except `update`,
an `option<T>` field is interchangeably absent or JSON `null`. **`update` is
patch-by-presence** (see its semantics below): an *omitted* key means "leave
unchanged", a present empty string (`title`/`meta`) means "clear" to `null`, an
explicit JSON `null` is treated the same as omission ("leave unchanged"), and a
present non-empty value sets the field.

## Verb semantics

A conforming capsule MUST implement these. `<id>` defaults to `"default"` when a
request omits `session-id`.

- **`append`** — append `messages` to a thread's history; create the thread if
  absent. Fire-and-forget. Bumps `updated-at`; sets `created-at` on first write.
- **`get_messages`** — return the thread's full message list. May carry an
  optional `append-before-read` to atomically append then read (eliminating the
  append/read race). An unknown thread returns an empty list (a capsule cannot
  distinguish "never existed" from "exists but empty").
- **`clear`** — mint a new thread (fresh `session-id`) whose `parent-session-id`
  is the cleared thread; the old thread's data is preserved. Returns
  `new-session-id`/`old-session-id`. Publishes a `created` lifecycle event for
  the new thread.
- **`list`** — return a page of `session-summary` ordered by session key, with a
  `next-cursor` (opaque, absent on the last page) and a best-effort `total`
  (absent if the namespace is too large to count cheaply). `include-archived`
  defaults to `false` (archived threads omitted). Each summary carries
  `updated-at` so a client can recency-sort within a page.
- **`get_meta`** — return one thread's `session-summary`, or `session: null` if
  no such thread exists in the caller's namespace. MUST NOT create the thread.
- **`update`** — patch a thread's mutable metadata. **Patch-by-presence:** a
  field whose key is *absent* from the request is left unchanged; a field that is
  *present* is set; a present empty string (`title`/`meta`) clears that attribute
  to `null`. `archived` present sets the flag. Bumps `updated-at`. Returns the
  updated `session-summary`, or `session: null` if the thread does not exist
  (MUST NOT create it). Publishes an `updated` event on success. `rename` and
  `archive` operations are both expressed through this single verb. A capsule
  MUST reject a `meta` value larger than the size bound (see below) **before**
  any write, leaving the thread untouched.
- **`delete`** — hard-purge a thread (transcript and metadata) from the caller's
  namespace; irreversible. Returns `deleted: true` if a thread existed, else
  `false` (this reports only whether the *caller's own* thread existed, so it is
  not a cross-principal existence oracle). Publishes a `deleted` event when
  something was deleted. To hide a thread reversibly, callers use `update`'s
  `archived` flag instead.
- **`search`** — case-insensitive substring search of `query` across the caller's
  thread transcripts, returning `search-result` hits (id, title, a truncated
  `snippet` around the first match, `match-count`, `updated-at`). Skips archived
  threads unless `include-archived`. Bounded and paginated (see below). An empty
  `query` matches nothing. v1 is literal matching only — relevance ranking is out
  of scope.

## Metadata model and the `meta` escape hatch

`session-summary` is the canonical metadata view. Display name fallback chain:
`title` → `preview` → `session-id`.

`meta` is an **opaque, client-defined JSON string**. A conforming capsule stores
it verbatim and **never interprets it** — it is the forward-compatibility escape
hatch for per-thread attributes the contract does not name (tags, pin state,
per-device read markers, client-side labels). A capsule MUST enforce a size bound
on `meta` to prevent storage abuse; the reference implementation rejects values
over **8 KiB**. Because it is opaque, new per-thread attributes can be prototyped
client-side with zero contract churn; an attribute that proves universal can be
promoted to a first-class field in a later minor version.

## Pagination

`list` and `search` page over an **opaque cursor**. The cursor is the capsule's
internal resume token (the reference implementation uses the key-store cursor);
clients MUST treat it as opaque and pass it back verbatim. `limit` is capped by
the capsule (the reference caps `list` and bounds `search` to a default of 50 /
20 respectively, with hard ceilings). `search` additionally bounds the number of
threads scanned per call so a no-match query over a large namespace returns
promptly with a resume cursor rather than scanning everything.

## Per-principal scoping (normative)

A conforming capsule MUST scope all storage and all results to the invoking
principal via the kernel's per-`(principal, capsule)` namespace, and MUST publish
lifecycle events from within the request invocation so the bus stamps them with
the acting principal. It MUST NOT accept a principal identifier from a request
payload, and MUST NOT expose any cross-principal read, list, search, or
enumeration path.

## Error handling

Errors are returned as the capsule host's error type (a request that cannot be
served — malformed correlation id, oversize `meta`, serialization failure — is an
error, not a silent drop). For request/response verbs, a "not found" outcome is
modelled in the response shape (`session: null`, `deleted: false`, empty
`messages`), not as an error. Lifecycle-event publication is best-effort: a
failed event publish MUST be logged but MUST NOT fail or roll back the mutation
that produced it.

## Concurrency

Per-thread writes MUST be atomic against concurrent writers (the reference
implementation uses compare-and-swap with bounded retry). `update`/`delete`
operate only on an existing thread: a thread deleted concurrently with an
in-flight update results in "not found", never a recreated thread.

## Capsule manifest declarations

A conforming capsule declares, in `Capsule.toml`:

```toml
[exports]
"astrid:session" = "1.1.0"

[subscribe]
"session.v1.append"             = { handler = "…" }
"session.v1.request.get_messages" = { handler = "…" }
"session.v1.request.clear"      = { handler = "…" }
"session.v1.request.list"       = { handler = "…" }
"session.v1.request.get_meta"   = { handler = "…" }
"session.v1.request.update"     = { handler = "…" }
"session.v1.request.delete"     = { handler = "…" }
"session.v1.request.search"     = { handler = "…" }

[publish]
"session.v1.response.get_messages.*" = { … }
"session.v1.response.clear.*"        = { … }
"session.v1.response.list.*"         = { … }
"session.v1.response.get_meta.*"     = { … }
"session.v1.response.update.*"       = { … }
"session.v1.response.delete.*"       = { … }
"session.v1.response.search.*"       = { … }
"session.v1.event.*"                 = { … }
```

## Optional companion surfaces (conventions, not conformance)

These are recommended conventions a session capsule MAY expose so the agent and
operators reach the same store; they are not required for *interface*
conformance:

- **Agent tools** — read-only `tool.v1.execute.{list_threads, get_thread,
  search_conversations}`, so an agent can introspect its own conversations.
  Management (rename/delete/archive) is deliberately not exposed to the agent.
- **Operator CLI** — a `session` command (`list`, `show`, `rename`, `archive`,
  `unarchive`, `delete`, `search`) via `cli.v1.command`.

# Drawbacks
[drawbacks]: #drawbacks

- The interface is broader than a minimal CRUD; a trivial in-memory session
  capsule must still implement `search`, pagination, and lifecycle events to
  conform. (Mitigated: each has a small, well-defined floor — substring scan,
  cursor passthrough, fire-and-forget publish.)
- The `meta` escape hatch is opaque, so two clients can disagree on its shape;
  the contract gives no schema for it. This is the intended trade for evolvability.
- Bus interfaces are not linker-typed, so a non-conforming capsule's mismatched
  payload surfaces only at runtime (deserialization error / unmatched reply), not
  at load time.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

- **One `update` patch verb vs. separate `rename`/`set-title`/`archive`.** A
  single patch verb keyed on field *presence* means a new mutable attribute never
  requires a new verb (or RFC). Ergonomic surfaces (HTTP `PATCH`, CLI `rename`)
  layer over it. Separate verbs would multiply the contract and re-bump it for
  every new mutable field.
- **An opaque `meta` field vs. first-class fields for everything.** Naming every
  conceivable per-thread attribute (tags, pin, read-state, model-used) bloats the
  contract and still misses the next one. `meta` lets clients evolve without
  contract churn; genuinely universal attributes graduate to first-class fields.
- **Per-request scoped reply topics vs. a shared response topic + id filter.**
  Scoping the reply to the correlation id removes the cross-instance "response
  theft" failure mode under concurrency for free, at the cost of a slightly
  larger topic space.
- **Per-principal-by-KV-namespace vs. a principal field in the payload.** Deriving
  isolation from the kernel's namespace (and the kernel's principal stamping on
  publish) makes cross-principal access *structurally* impossible and unforgeable,
  rather than a policy check the capsule could get wrong. It is the whole reason
  the surface is safe to expose to paired devices and untrusted agents.
- **Lifecycle events vs. polling for multi-device sync.** Fan-out events stamped
  with the principal give live cross-device updates without each device polling a
  list endpoint; the principal stamp makes per-principal scoping automatic.
- **Impact of not standardising:** the incumbent capsule remains the only
  implementable one; clients stay coupled to its source; no alternative backend
  (encrypted, synced, retention-bounded) can be written against a stable contract.

# Prior art
[prior-art]: #prior-art

- [`astrid:users` (RFC, rfcs#28)](https://github.com/unicity-astrid/rfcs/pull/28)
  standardises another `astrid-bus:*` capsule interface — fixed response topics +
  correlation ids, per-instance scoping, paginated list, explicit error semantics.
  This RFC follows the same shape.
- [Capsule interface system (RFC, rfcs#20)](https://github.com/unicity-astrid/rfcs/pull/20)
  defines how interfaces are exported/imported, versioned, and conflict-resolved;
  `astrid:session` is one such interface.
- [Host ABI (RFC, rfcs#22)](https://github.com/unicity-astrid/rfcs/pull/22)
  establishes that `astrid-bus:*` capsule-to-capsule contracts are governed by the
  topic-versioning convention, distinct from the kernel-to-capsule host ABI — the
  governance basis for this interface's evolution.
- Conversation/thread models in chat protocols (Matrix rooms, the OpenAI
  conversations/threads APIs) inform the metadata surface (title, archive,
  recency) without adopting their server-centric identity assumptions.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Relevance-ranked / semantic search** is explicitly out of scope; v1 is literal
  substring. Ranked recall is tracked separately in
  [astrid#1042](https://github.com/unicity-astrid/astrid/issues/1042).
- **Per-device read state** (unread markers across a principal's devices) is left
  to client-side `meta` for now; whether it warrants a first-class field is open.
- **`total` beyond the count cap** — for very large namespaces `total` is `null`;
  whether an exact count is worth a maintained counter is unresolved.
- **Title auto-generation** (deriving a title from the first turn) is an agent-loop
  concern that *calls* `update`; it is not part of this contract.

# Future possibilities
[future-possibilities]: #future-possibilities

- A `2.0` could add incremental transcript fetch (since-cursor / last-N over a
  dedicated verb) for very long threads, and a server-side recency index enabling
  globally recency-ordered pagination.
- Cross-thread compaction / summarisation hooks could be expressed as additional
  optional verbs.
- A retention-policy / TTL attribute could graduate from `meta` to a first-class
  field once its shape settles.
- Encrypted-at-rest and fleet-synced session capsules become writable against this
  contract — the primary point of standardising it.
