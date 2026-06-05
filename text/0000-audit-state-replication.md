- Feature Name: `audit_state_replication`
- Start Date: 2026-06-06
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

Turn the audit log from an observability record into a substrate for **agent-state
replicability**: forking, replay, forensic inspection, rollback, and state sharing
with cryptographic proof of actions. The signed audit chain becomes the replay
*spine/index* — it proves "value `H` existed and was used at entry `N`" — while a new
kernel-internal **content-addressed value store** holds the actual bytes, keyed by the
BLAKE3 `ContentHash` the chain already carries. Most of this is kernel-internal and
needs no contract change. This RFC specifies only the parts that cross the
kernel-to-user-space boundary: (1) the mechanism by which a capsule emits a
replay-grade value into the chain (a reserved, kernel-consumed IPC topic with
verified-principal trust rules), (2) a new `audit:replay` capability gating
hash→value dereference, and (3) the privacy, deletion, and deterministic-replay
contract that capture-everything fidelity requires.

# Motivation
[motivation]: #motivation

Today the audit chain is replay-grade for exactly one action kind (`AdminRequest`,
which carries `params`). Every other entry records the *fact* of an action plus a
hash that stands in for the value the action consumed or produced — and the bytes
behind that hash are computed and thrown away. The two dominant determinants of an
agent's behaviour, conversation history and KV state, are captured *not at all*: they
live in capsule-space and never cross into the signed chain. An audit log in that
shape can tell you *that* something happened; it cannot tell you *what the agent saw
or did* with enough fidelity to reconstruct the agent.

We want the audit log to support five concrete capabilities:

- **Replayability** — re-run an agent's history from the log and arrive at the same
  state. The basis for everything below.
- **Forkability** — reconstruct state at entry `N` and branch a new execution from it
  (a new session seeded from the parent's state, with lineage recorded).
- **State rollback** — reconstruct state at entry `N` and discard the tail, reverting
  an agent to a known-good point.
- **Understanding what went wrong** — forensic inspection of the exact inputs,
  outputs, and ordering around a failure, with an honest distinction between values
  that were captured, never captured, or later redacted.
- **State sharing with proof of actions** — hand another party a slice of an agent's
  history that they can *verify*: the chain's ed25519 signatures and hash-linking are
  the proof; the referenced values are shared (or fetched under capability) alongside.
  Anchoring the chain tip to the Unicity blockchain makes the proof-of-actions
  third-party-verifiable.

Fork and rollback are the demanding cases, and they impose a requirement the
observability framing never had: **completeness of recorded state**. Replay, fork, and
rollback do *not* re-execute the agent — there is no attempt to re-run the model under
captured inputs and arrive back at a state. They *reconstruct* state by reading what
was recorded. So the property that matters is that every value which constituted the
agent's state — the messages it exchanged, the tool results it received, the model
outputs it produced — was captured as it occurred. A gap in the recorded values is a
gap in the reconstructable state. This is why capture must be complete, and it is why
the LLM completion is recorded as state in its own right: it is part of the history,
not something to be regenerated.

Crucially, the bulk of the machinery is kernel-internal and out of scope for an RFC:
the content-addressed value store, persisting the preimages behind hash fields that
*already exist* on `AuditAction` variants, wiring the host functions that mutate state
(KV, fs), and the cost-structured batching that keeps the signer from melting under
high write volume. Those preserve the guest-visible contract and ship as ordinary
kernel work. This RFC isolates the ~20% that genuinely touches the contract surface,
so that the contract change is made deliberately and in the open rather than smuggled
in alongside the internal work.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## The spine and the value store

There are two stores. The **audit chain** is the spine: an append-only, ed25519-signed,
hash-linked log of *what happened*, keyed per principal. Its signature is computed over
the action's serialized form, which contains a `ContentHash` — never the value itself.
The **value store** is a content-addressed blob store (`put(bytes) -> ContentHash`,
`get(&ContentHash) -> Option<bytes>`, addresses are BLAKE3 digests so the store is
self-verifying and auto-deduplicating). It holds *what the agent saw and produced*.

Replay walks a principal's signed chain in order; for each entry that carries a hash,
it dereferences the hash in the value store and re-hashes the returned bytes to prove
they match. Two integrity proofs that never trust each other: the chain signature binds
the metadata and the hash; the value-store read-check binds the bytes to the hash. A
tampered value fails the read-check; a tampered hash fails the chain signature.

## What a capsule author needs to do

Almost nothing changes for most capsules. The values behind tool calls, file writes,
and KV mutations are captured *host-side* — the capsule keeps calling `astrid:kv`,
`astrid:fs`, MCP tools exactly as before, and the kernel records the values
transparently. No capsule code, no manifest change.

The one capsule-visible obligation falls on the capsules that *own conversation state*
(the session/react capsules). They are the only component that sees the message stream,
so they must emit it for it to be replayable. A conversation-owning capsule, when it
appends a message to its history, **also emits the message value** on a reserved audit
topic:

```
// pseudo-code in the session capsule, on appending message M to session S
audit::emit_value(AuditValue {
    session_id: S,
    parent_session_id: S.parent,   // for fork lineage
    seq: next_seq,                 // monotonic per session
    class: "conversation.message",
    value: serde_json::to_vec(&M)?,  // the actual message bytes
})?;
```

The kernel consumes this, hashes the value into the value store, and writes a signed
`MessageRecorded` entry carrying the hash (and a coarse `role` index, never the
content) into the principal's chain. The capsule never holds the signing key, never
writes the chain, and never names a hash — the kernel computes the digest so a capsule
cannot poison the address space. The kernel treats the message as opaque bytes;
conversation *semantics* stay capsule-owned. This is the only piece that requires a
new contract surface, because a capsule is now writing into the signed audit record.

The same obligation applies to any value that constitutes agent state but that the
kernel cannot otherwise see — most importantly **LLM completions**. The capsule (or the
LLM-invoking host path) emits the completion as a value because it *is* part of the
recorded history: what the assistant actually produced. Reconstruction reads it back;
nothing is re-sampled or re-run.

## Reading values back

Reading the *chain* keeps today's model: holders of `audit:read_all` see every
entry; everyone else sees only entries attributed to their own principal. Resolving a
hash to its *value* is strictly more sensitive (the chain says "alice wrote file X";
the value is the bytes of X), so it requires a new **`audit:replay`** capability,
composed with the existing per-principal chain-visibility scope. The set of values a
caller can read is `(entries the caller may see) ∩ (caller holds audit:replay)`.
Knowing a hash is never sufficient — content addressing is an address, not a
capability; the read path resolves hash → referencing entry → principal check before
returning bytes.

## Forking, rollback, sharing

Given a complete, deterministic chain plus its values, the higher-level operations are
reconstructions over it: **fork** = replay to entry `N`, snapshot the reconstructed
state, start a new session whose genesis records `parent_session_id = N`'s session and
the fork point; **rollback** = replay to `N`, discard the tail; **share** = export the
chain segment (the proof) plus the referenced values (or just the hashes for a
recipient who holds `audit:replay`). These reconstruction operations are capsule-space
tooling built on the primitives this RFC and its kernel-internal companions provide;
they are not themselves new contract surface, but they are *why* the contract is shaped
this way.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

This section specifies only the contract surface. The kernel-internal companions
(the value store type, the internal intake seam, persisting preimages behind existing
hashes, KV/fs host-fn coverage, Merkle-batched checkpoints, GC) are normatively
described in the tracking issue and ship without an RFC because they preserve the
guest-visible contract.

## C1. Capsule value emission

Two designs were considered (see Rationale). This RFC specifies **Path A: a reserved
kernel-consumed IPC topic**, with Path B (a host function) as a documented fallback.

### Topic reservation

The topic namespace `astrid.v1.audit.value.*` is **reserved**: the kernel runs an
internal consumer subscribed to it, and capsule *publish* to it is deny-by-default.
A capsule may publish on this namespace only if its manifest declares the
`audit_emit` capability (new; see C3) and the kernel has granted it. Subscribe by
capsules is forbidden (the namespace is write-only from capsule-space; only the kernel
consumer reads it).

### Emission payload

A value-emission message carries a JSON object:

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `class` | string | yes | Value class, e.g. `conversation.message`, `llm.completion`, `tool.result`. Drawn from a kernel-known registry; unknown classes are rejected. |
| `session_id` | string | yes | The session this value belongs to. |
| `parent_session_id` | string | no | Parent session, for fork lineage. |
| `seq` | u64 | yes | Monotonic per-`session_id` sequence. A gap with no chain entry is a detectable drop. |
| `value` | bytes (base64 in JSON, or a chunk ref; see C4) | yes | The opaque value bytes. The kernel never interprets them. |
| `role` | string | no | A coarse, non-sensitive index (e.g. `user`/`assistant`/`tool`). Stored in the entry; the content lives only behind the hash. |

### Trust rule (normative)

The emitting message's principal attribution **MUST be `verified`** (kernel-verified
from the publishing capsule's invocation context). The consumer **MUST reject** a
message whose attribution is `claimed` (uplink-asserted, not kernel-verified) — a
`claimed` principal could forge another principal's conversation history into the
signed record. This is the load-bearing security property of Path A: the signed audit
record may only be written on behalf of a principal the kernel has authenticated.

### Consumer behaviour (normative)

The kernel consumer, for each accepted emission:

1. Computes `H = BLAKE3(value)`. The capsule does **not** supply `H`.
2. Writes the value to the content-addressed store **before** appending the chain entry
   (pin-before-reference: a crash leaves a collectible orphan, never a dangling
   pointer).
3. Appends a signed `MessageRecorded { session_id, parent_session_id, seq, content_hash: H, role }`
   (or the class-appropriate variant) to the emitting principal's chain via the
   existing per-principal append path, and publishes it on `astrid.v1.audit.entry`
   using the canonical feed payload.
4. On any failure, degrades **continue + alert** (the audit log's existing posture):
   the agent is not blocked, the gap is logged and metered.

`AuditAction` variants (`MessageRecorded`, etc.) are the kernel's internal log schema
and live in no `.wit`; adding them is not a contract change (the existing
`AdminRequest.params` field was added the same way). They are mentioned here only
because C1 is what *produces* them from capsule-space.

## C2. `audit:replay` value-read capability

A new catalogued capability, `audit:replay` (category `System`, danger `Elevated`).
It gates **dereferencing a `ContentHash` to its preimage**, and only that — it does
not widen chain (metadata) visibility. Effective readable values:

```
readable_values(caller) = { H : exists entry E. caller_may_read_chain(E)
                                 and H in hashes(E)
                                 and caller holds audit:replay }
```

`caller_may_read_chain(E)` is the existing rule (`audit:read_all` ⇒ all; else
`E.principal == caller`). The value-fetch path resolves `H → referencing entries →
principal check → audit:replay check → read-verify → bytes`. A per-principal value ACL
index prevents a cross-principal hash collision from leaking one principal's value to
another (content addressing is global by digest; the dereference ACL is not). Adding
`audit:replay` to the capability catalog is the contract change; it composes
multiplicatively with the per-principal scope already shipped for the audit feed.

## C3. `audit_emit` manifest capability

A new manifest capability `audit_emit` permits a capsule to publish on the reserved
`astrid.v1.audit.value.*` namespace (C1). Default-deny. It is granted only to
conversation/state-owning capsules (session, react, and the LLM-invoking path). Like
all capabilities it is operator-controlled and resolved the privileged way, never
self-granted from the manifest alone. Quotas (max value size, emission rate) mirror
`astrid:kv` limits.

## C4. Large values and the IPC payload cap

The IPC payload cap (1 MiB) is smaller than some values (long transcripts, tool
outputs, file contents). A conforming large-value emission **MUST** either:

- **chunk**: emit the value as an ordered set of `value.chunk` messages under one
  `(session_id, seq)`, the consumer reassembling and hashing the whole before storing;
  or
- **spill**: write the bytes via an existing bulk path (e.g. a CAS-backed `astrid:fs`
  write) and emit a reference, the consumer resolving and hashing it.

Silent truncation is forbidden: an oversize value that is neither chunked nor spilled
**MUST** be rejected with a typed error, never dropped. The exact chunking framing is
specified here so producers and the consumer agree.

## C5. State-completeness capture (normative)

Replay, fork, and rollback reconstruct state by *reading the recorded values*; they do
not re-execute the agent. Reconstruction is therefore only as complete as the recorded
state. Every value that constitutes agent state and is not already captured host-side
**MUST** be emitted, in particular:

- **LLM completions** — the model's output is emitted as an `llm.completion` value,
  referenced from an `LlmResponse` entry, because it is part of the conversation history
  the agent acted on. It is recorded, not regenerated.
- **Tool results** the agent consumed, where not already captured at the host tool
  boundary.

There is no determinism or re-execution requirement: the log is a record of what
occurred, and a value that was never recorded reconstructs as `ValueState::Missing`
(C6) — it is never re-derived. **Completeness, not determinism, is the property fork
and rollback depend on.**

## C6. Privacy, deletion, and the capture posture (normative)

The capture posture is **capture-everything by default** (lead decision): the value
behind every hash is persisted, reversing the original "store the hash, not the value,
for privacy" stance. Because there is no capture filter, the compensating controls are
load-bearing and are **mandatory** under this RFC:

- **Read gating** — values are inaccessible without `audit:replay` ∩ per-principal
  scope (C2). Absent the capability, a caller sees exactly today's hash-only view.
- **At-rest encryption** — the value store **MUST** be encrypted at rest. Capturing
  every value broadens data-at-rest exposure; encryption + the read gate are the only
  remaining confidentiality controls.
- **Deletion (right-to-be-forgotten)** — resolved by design asymmetry, not by
  rewriting history. The chain is append-only and its signature is over the *hash*, so
  deleting a value cannot break chain integrity. Deletion targets only the value store;
  the result is a dangling `ContentHash` that replay surfaces as a typed
  `ValueState::Redacted` (distinct from `Missing` — never captured — and `Present`).
  A value **MUST** live only in the value store, never inline in a signed entry, so a
  single delete is sufficient (the erasure-completeness invariant). A deletion is
  itself recorded as a signed `ValueRedacted` entry, so redaction is forward-auditable
  — the chain only ever grows.

The `ValueState` typing and any operator-facing deletion surface that becomes
capsule-visible are contract surface and are specified here; the kernel-internal delete
mechanism is not.

# Drawbacks
[drawbacks]: #drawbacks

- **Data-at-rest exposure.** Capture-everything means the value store holds the full
  content of conversations, tool I/O, and file writes. Even with at-rest encryption and
  read gating, this is a large, sensitive corpus and a high-value target. The original
  hash-only design existed precisely to avoid this.
- **Storage growth.** Values dwarf metadata. Dedup helps, but conversation/LLM streams
  are large and GC is now load-bearing rather than optional.
- **A new write path into the signed record.** C1 lets a capsule cause a signed audit
  entry to be written. The `verified`-attribution trust rule contains the risk, but it
  is a genuine new authority and a forgery surface if that rule is ever weakened.
- **Reconstruction is only as complete as capture.** Fork/rollback reconstruct recorded
  state, so any state-constituting value that was not emitted (an unaudited host read, a
  best-effort emission that was dropped under load) leaves a `Missing` hole in the
  reconstructed state. The honest `Missing` typing surfaces the gap rather than hiding
  it, but the gap is real.
- **Cost.** Hashing and persisting every value adds latency and I/O to hot paths; the
  Merkle-batched checkpoint design (kernel-internal) is needed to keep signing
  affordable.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why spine + value rather than fat entries.** Putting values inline in signed entries
would bloat the chain, make `verify_all` slow, and — fatally — make
right-to-be-forgotten impossible (you cannot delete from an append-only signed log).
The hash-pointer split makes deletion a value-store operation that leaves the integrity
proof intact. It also gives free dedup and lets the chain stay small and fast.

**Why Path A (reserved topic) over Path B (host function).** Path B — a new
`astrid:audit` host fn `record-value(class, bytes, lineage) -> content-hash` — is a
heavier ABI surface (a new WIT package, a syscall, versioning) for the same effect. Path
A reuses the existing `astrid:ipc` publish surface; the only new contract is a reserved
topic namespace, a trust rule, and the `audit_emit` capability. Path A is the smaller,
more reversible change and proves the model. Path B remains the documented fallback if
topic-ACL forgery defense or ABI-level size enforcement proves insufficient — promotion
to B would itself be a follow-up RFC.

**Why a new `audit:replay` capability rather than reusing `audit:read_all`.**
`audit:read_all` widens *metadata* visibility (a firehose of who-did-what).
Dereferencing values is a different, more sensitive axis (the actual bytes). Conflating
them would mean either every metadata reader gets values (too wide) or no one does (too
narrow). A separate, composable capability keeps the value blast radius `≤` the metadata
blast radius.

**Why capture-everything rather than opt-in.** Opt-in (default-off per action-class)
was the conservative recommendation and preserves the original privacy stance, but
forkability and rollback require *completeness* — a per-class gap is a hole in the
reconstructable state. The lead chose capture-everything to make fork/rollback sound by
default, accepting the exposure in exchange for the compensating controls in C6. This
is the single highest-stakes decision in the design and is called out as such.

**Impact of not standardizing.** Without C1, conversation history — the dominant
behaviour determinant — can never enter the signed record, so replay/fork/rollback are
permanently partial. Without C2, capture-everything has no read control. The contract
parts are what make the difference between a richer observability log and an actual
state-replication substrate.

# Prior art
[prior-art]: #prior-art

- **Event sourcing / CQRS** — reconstructing state by replaying an ordered event log is
  the foundational pattern here; the audit chain is the event log, the value store holds
  the event payloads.
- **Content-addressed stores (git objects, IPFS, Nix store)** — BLAKE3-keyed,
  self-verifying, dedup-by-content blobs with a separate index referencing them. The
  value store is git-objects-shaped.
- **Merkle/transparency logs (Certificate Transparency, Trillian)** — append-only,
  signed, hash-linked logs whose tip can be anchored externally for third-party
  verification; directly informs the Unicity anchoring future work.
- **Database write-ahead logs and point-in-time recovery; VM and filesystem snapshots**
  — reconstruct state from a recorded base plus a log, *not* by re-executing.
  Fork/rollback are the agent analogue, and this is the model here.
- **Deterministic record-replay debuggers (`rr`, hypervisor record/replay)** — included
  as an explicit *contrast*: those re-execute a program under captured non-determinism to
  reproduce a run exactly. This design is **not** that — there is no re-execution; state
  is reconstructed by reading recorded values.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Emitter of record for conversation** — the session capsule (owns the persisted
  message history + parent lineage) versus react (owns the turn boundary). C1 assumes
  the session capsule; confirm.
- **Value-store sharding** — per-principal namespaces (trivial ACL, per-principal GC and
  deletion; recommended) versus a single shared dedup pool (better dedup, harder
  per-principal right-to-be-forgotten refcounting).
- **KV key privacy** — KV *keys* can carry sensitive material; capture keys plaintext in
  the entry, or hash the key too with the plaintext in the value store? (Kernel-internal,
  but the policy should be decided alongside C6.)
- **At-rest encryption keying** — per-principal keys (enables crypto-shredding a
  principal's values by dropping the key) versus a single store key. Interacts with C6
  deletion.
- **Capture-completeness boundary** — which state-constituting values beyond
  conversation, LLM completions, tool results, and KV/fs must be emitted for a practical
  reconstruction, and how `Missing` holes are surfaced to fork/rollback tooling.
- **The exact fork/rollback/share API** — these reconstruction operations are downstream
  of this RFC; their capsule-facing surface (if any) may warrant its own RFC.

# Future possibilities
[future-possibilities]: #future-possibilities

- **Unicity anchoring.** Periodically anchor the signed chain tip (a Merkle root over a
  window of entries) to the Unicity blockchain, making proof-of-actions
  third-party-verifiable and tamper-evident beyond the runtime. This closes the loop to
  the original motivating use case (minting against audit activity): the chain becomes
  the mintable, externally-provable record. O(1) anchoring transactions cover an entire
  window of entries.
- **Fork/rollback as first-class operations** — a capsule-facing API to fork an agent at
  entry `N`, or revert one, built on the reconstruction primitives.
- **Cross-principal / cross-runtime state sharing** — export a verifiable chain segment +
  values for another Astrid instance (or an external verifier) to ingest, gated by
  `audit:replay`, enabling delegation hand-offs and audit federation.
- **Selective disclosure** — share a chain segment with values redacted to hashes,
  letting a recipient verify the *structure* of actions without seeing their contents
  (a natural fit with the `ValueState::Redacted` typing).
- **Differential replay** — replay two forks and diff their state to localize where a
  behaviour diverged (forensics at scale).
