- Feature Name: `governed_domain_audit_append`
- Start Date: 2026-08-10
- RFC PR: [rfcs#39](https://github.com/astrid-runtime/rfcs/pull/39)
- Tracking Issue: [astrid#1472](https://github.com/astrid-runtime/astrid/issues/1472)

# Summary
[summary]: #summary

Add a typed `astrid:audit@1.0.0` host interface through which a capsule can append a
bounded **domain assertion** to Astrid's durable, runtime-signed audit log and receive
an inclusion receipt. The kernel, not the capsule, stamps the effective principal,
capsule identity, component ID, component digest, session, sequence, time,
authorization basis, chain link, runtime key, and signature. The capsule supplies only a schema-scoped,
typed assertion and content digests. Appends are authorized by an install-approved
`audit_append` manifest allowlist, are idempotent, quota-bounded, transactionally
committed, and never travel through the droppable event bus.

# Motivation
[motivation]: #motivation

Astrid already records kernel-observed security actions such as filesystem access,
network connections, process spawning, and management requests. Those entries are
durable, signed by the runtime, hash-linked, and principal-scoped. A capsule cannot,
however, record a domain decision that only it understands. Codewall is the immediate
example: the kernel can observe that Codewall read a file or published an event, but
it cannot infer that policy revision `H1` evaluated input `H2`, denied it under rules
`R1` and `R7`, and produced result `H3`.

Applications need to add those domain facts without gaining custody of the runtime
signing key or the ability to forge kernel provenance. A generic `append(string)` is
not acceptable. It would permit source spoofing, unbounded secret dumping, ambiguous
serialization, replay amplification, namespace impersonation, and claims that look
like kernel-observed facts. Sending records over IPC is also insufficient: the bus is
broadcast-oriented and may drop messages under lag, and publish success cannot prove
that a record was durably included in the signed chain.

The desired boundary is narrower:

- a capsule may assert facts only inside namespaces approved at installation;
- the assertion is visibly a capsule assertion, not a kernel observation;
- identity, authority, ordering, and provenance are exclusively host-stamped;
- the request is bounded and typed, with large or sensitive values represented by
  digests rather than copied into the log;
- a successful return is a durable inclusion receipt, suitable for later proof;
- retry after a crash, trap, or lost response does not duplicate the assertion; and
- old capsules and existing audit entries continue to work unchanged.

This interface is useful beyond Codewall. Policy engines, payment workflows, build
systems, data pipelines, agent harnesses, and ordinary applications can all preserve
domain evidence while retaining Astrid's authority rule: the application reports an
assertion; the runtime attests who reported it, under whose authority, from which exact
component, and where it appears in the log.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## Declaring intent

A capsule declares the domain schema prefixes it wants to append:

```toml
[capabilities]
audit_append = ["codewall.decision@1", "codewall.policy-change@1"]
```

This is requested authority, not self-granted authority. The normal capsule
installation or upgrade review records the approved capability snapshot. Adding or
widening a prefix is a capability expansion and requires approval. The kernel uses the
approved snapshot, not an untrusted manifest reparsed from the archive at invocation
time.

Exact schema identifiers are recommended. A trailing `*` is allowed only as the final
character, so `codewall.*` covers future Codewall schemas while `*wall.*` and interior
wildcards are invalid. Installers SHOULD display wildcard requests as elevated risk.

## Appending an assertion

A Codewall capsule can record a decision without placing source code, prompts, or
other sensitive material in the audit database:

```rust,ignore
let receipt = audit::append(&audit::AppendRequest {
    schema: "codewall.decision@1".into(),
    idempotency_key: decision_id.to_string(),
    subject: Some("workspace:4f6c...".into()),
    disposition: audit::Disposition::Denied,
    attributes: vec![
        audit::Attribute::text("policy_revision", "2026-08-10.3"),
        audit::Attribute::text("rule_ids", "R1,R7"),
        audit::Attribute::boolean("enforced", true),
    ],
    evidence: vec![
        audit::EvidenceRef::blake3("input", input_digest),
        audit::EvidenceRef::blake3("policy", policy_digest),
        audit::EvidenceRef::blake3("result", result_digest),
    ],
})?;
```

The capsule does not provide a principal, capsule/package ID, component ID, component
hash, timestamp, session, sequence, previous hash, authorization proof, public key, or
signature. Those fields cannot be overridden. The host obtains the effective principal
from the kernel-populated invocation context and the package ID, selected component ID,
and component digest from the exact verified component loaded by the runtime.

`disposition` and every attribute remain capsule assertions. A verifier can prove
that a particular Codewall component asserted `denied`; it cannot infer merely from
that assertion that the kernel itself prevented an effect. Kernel enforcement remains
represented by kernel-observed audit action types.

## Using the receipt

`append` returns only after the entry, chain head, sequence, session index, and
idempotency mapping have been durably committed. The receipt contains the entry ID,
chain position, hashes, runtime public key, and signature. An application may return
the receipt to its caller, attach it to a result, or retain it for later export.

If the call is retried with the same idempotency key and byte-for-byte equivalent
canonical request, the kernel returns the original receipt with `duplicate = true`.
If the key is reused with different content, the kernel returns `conflict` and appends
nothing.

## What belongs in the log

The domain assertion is an evidence index, not a general blob store. It supports a
small set of scalar attributes and bounded digest references. Full source files,
transcripts, prompts, tool results, credentials, and customer payloads do not belong
in attributes. Store sensitive or large material through an appropriately governed
storage path and include a digest that commits to the exact bytes.

The host cannot determine whether every small string is sensitive. Capsule authors
remain responsible for data classification, while hard size, count, and rate limits
bound accidental and adversarial amplification.

## Threat model and claim boundary

The capsule is untrusted. Its attributes, disposition, subject, schema selection, and
evidence digests may be false even when perfectly signed. The runtime is trusted for
identity stamping, approved-capability enforcement, sequencing, durable storage, and
key custody. Storage is trusted for availability but not integrity: signatures and
links must detect mutation, deletion, insertion, reordering, and forks. Other
principals and capsules are mutually distrusted.

| Threat | Required defense | What remains possible |
|---|---|---|
| Capsule names another principal | Principal absent from request; host requires verified invocation context | Capsule can mention a misleading principal in an ordinary attribute |
| Capsule impersonates another application schema | Install-approved schema allowlist plus stamped capsule ID and component digest | An operator can deliberately approve a broad or foreign prefix |
| Capsule claims it enforced a denial it did not enforce | Fixed `capsule-assertion` provenance; kernel observations remain separate | The capsule can lie within its assertion; consumers must evaluate provenance |
| Retry or response loss duplicates an entry | Transactional idempotency index and stable scoped key | A capsule can intentionally use fresh keys to create distinct assertions, within quota |
| Concurrent appends fork or reorder the chain | One durable sequencer and atomic head/sequence update | Commit order may differ from invocation start order |
| Guest floods disk or signing CPU | Structural bounds, per-principal/component quotas, reserved kernel capacity | Work within approved quotas still consumes operator resources |
| Guest leaks secrets into the log | No opaque payload, small typed fields, digest references, review guidance | The host cannot recognize a secret placed in a valid short string |
| Storage edits or removes entries | Runtime signatures, entry hashes, sequences, link verification | Truncation after the last externally known head is not detectable without a checkpoint |
| Malicious archive is swapped after approval | Runtime stamps package ID, component ID, and digest of verified instantiated component; approved snapshot binds upgrade | A compromised installer/operator can approve malicious bytes |
| Runtime or signing key is compromised | Out of scope for this ABI; future hardware attestation/checkpoints can narrow exposure | Compromised runtime can forge entries and receipts |

This RFC does **not** prove that a capsule assertion is true, that referenced evidence
is available, or that the asserted business effect occurred. It proves the exact
bounded statement, its source component, effective principal, approved append
authority, durable position, and runtime signature.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## 1. Host interface

The canonical contract is a new host package:

```wit
package astrid:audit@1.0.0;

interface host {
    enum disposition {
        observed,
        succeeded,
        failed,
        denied,
    }

    enum digest-algorithm {
        blake3-256,
        sha2-256,
    }

    record digest {
        algorithm: digest-algorithm,
        bytes: list<u8>,
    }

    variant attribute-value {
        text(string),
        signed(s64),
        unsigned(u64),
        boolean(bool),
        digest(digest),
    }

    record attribute {
        key: string,
        value: attribute-value,
    }

    record evidence-ref {
        relation: string,
        digest: digest,
        media-type: option<string>,
        size-bytes: option<u64>,
    }

    record append-request {
        schema: string,
        idempotency-key: string,
        subject: option<string>,
        disposition: disposition,
        attributes: list<attribute>,
        evidence: list<evidence-ref>,
    }

    record receipt {
        entry-id: string,
        session-id: string,
        sequence: u64,
        signed-entry: list<u8>,
        entry-hash: list<u8>,
        previous-hash: list<u8>,
        runtime-key: list<u8>,
        signature: list<u8>,
        duplicate: bool,
    }

    enum error-code {
        invalid-request,
        capability-denied,
        quota-exceeded,
        conflict,
        cancelled,
        unavailable,
        internal,
    }

    record append-error {
        code: error-code,
        message: string,
        retryable: bool,
    }

    append: func(request: append-request) -> result<receipt, append-error>;
}
```

The SDK MAY provide builders and strongly typed wrappers, but the WIT above is the
normative ABI. An SDK wrapper MUST NOT add unstamped identity fields or weaken any
bound.

## 2. Request validation and bounds

The host MUST reject the complete request before reserving a sequence or writing any
state unless all of the following hold:

| Element | Constraint |
|---|---|
| `schema` | 1..=128 ASCII bytes; grammar `[a-z][a-z0-9.-]*@[1-9][0-9]*`; no consecutive dots |
| `idempotency-key` | 1..=128 bytes of printable ASCII excluding whitespace |
| `subject` | absent or 1..=256 UTF-8 bytes; NUL forbidden |
| attributes | at most 32; unique keys |
| attribute key | 1..=64 ASCII bytes; grammar `[a-z][a-z0-9_.-]*` |
| text attribute | at most 1,024 UTF-8 bytes; NUL forbidden |
| evidence references | at most 16; unique `(relation, algorithm, digest)` tuples |
| evidence relation | 1..=64 bytes under the attribute-key grammar |
| media type | absent or 1..=128 printable ASCII bytes |
| digest | exactly 32 bytes for both algorithms defined in 1.0.0 |
| canonical encoded request | at most 16,384 bytes |

There is no opaque `list<u8>` payload. Future payload forms require a new compatible
interface version and must preserve the evidence-index posture.

The error message is diagnostic and MUST be bounded to 512 UTF-8 bytes. Callers MUST
branch on `error-code`, not message text.

## 3. Authorization and namespace matching

`CapabilitiesDef` gains `audit_append: Vec<String>`, fail-closed when absent or empty.
Each value is either an exact schema identifier satisfying section 2 or a prefix with
one trailing `*`. Prefixes MUST end on a separator boundary (`.` or `@` before `*`),
and no other wildcard is accepted.

The authority used at runtime is the install-approved capability snapshot for the
loaded component. Re-reading only the component-supplied manifest is forbidden. An
upgrade that changes the component digest or expands `audit_append` follows the
existing capability-expansion approval flow.

Authorization succeeds only when all are true:

1. an effective, kernel-verified invocation principal is present;
2. the loaded component has a verified capsule/package ID, component ID, and digest;
3. an approved `audit_append` entry covers the exact request schema; and
4. principal and capsule audit quotas permit the append.

Principal identity MUST come from host-populated invocation state, never a guest
payload. Principal-less lifecycle, load-time, test-placeholder, and system contexts
receive `capability-denied`; they cannot silently fall back to the capsule owner or a
default principal for this interface.

Schema authorization does not confer permission to act on a resource. It permits only
the recording of a namespaced assertion. A domain assertion MUST NOT be consumed by
the kernel as an authorization proof or enforcement result.

## 4. Signed entry model

An accepted call creates an audit entry of semantic type `CapsuleDomainAssertionV2`.
The persisted entry contains:

- format version `2`;
- runtime-generated entry ID;
- runtime timestamp with nanosecond precision when available;
- runtime session ID;
- effective principal ID;
- monotonically increasing per-`(session, principal)` sequence;
- previous entry hash for that chain;
- source capsule ID;
- source component ID from the selected `[[component]]` manifest entry;
- source component digest of the verified bytes actually instantiated;
- provenance fixed to `capsule-assertion`;
- matched approved `audit_append` pattern;
- the validated request fields; and
- runtime public key and signature.

The capsule cannot supply or override any host-stamped field. In particular, an
attribute named `principal`, `source`, `authorization`, or `kernel-enforced` remains an
ordinary capsule-provided attribute and MUST NOT be projected into host-stamped
fields. SDKs SHOULD warn on those misleading attribute names.

Existing kernel-observed audit variants remain distinct. Consumers MUST render the
provenance prominently enough that a capsule assertion cannot be mistaken for a
kernel observation.

## 5. Canonical encoding, hashing, and signatures

Version 2 entries MUST NOT reuse the current version 1 ad-hoc concatenation or JSON
serialization. The entry body is deterministic CBOR under RFC 8949 section 4.2.1 with
integer map keys defined by the implementation specification. The signature input is
the ASCII domain separator `astrid.audit.entry.v2\0` followed by the exact entry-body
bytes.

Normative canonicalization before encoding:

- attributes are sorted by UTF-8 byte order of `key`;
- evidence references are sorted by `(relation, algorithm discriminant, digest
  bytes)`;
- duplicate attribute keys and duplicate evidence tuples have already been rejected;
- enum values use fixed unsigned integer discriminants documented with the WIT
  mirror; and
- optional fields are encoded explicitly as `null`, not omitted.

Let `B` be the canonical CBOR entry body and `S` be the exact domain-separated bytes
`"astrid.audit.entry.v2\0" || B`. The runtime produces
`signature = Ed25519.sign(runtime_key, S)` and
`entry_hash = BLAKE3-256(S || signature)`. The receipt's `signed-entry` is exactly `B`,
so the receipt is independently verifiable without retrieving the entry from runtime
storage. The next entry's `previous_hash` is this 32-byte `entry_hash`. Receipt byte
lengths are therefore 32 for both hashes and the Ed25519 public key, and 64 for the
signature. `signed-entry` is bounded by the validated entry limit and MUST byte-match
the durably stored entry body.

The complete disposition and every diagnostic attribute are included in `S`. A
verifier MUST reject an unknown format version, invalid canonical encoding, wrong
digest length, signature failure, sequence discontinuity, or chain-link mismatch.

Version 1 entries retain their existing signature and content-hash algorithms, but
their verifier MUST stop using timestamp sort. It reconstructs the unique chain from
zero `previous_hash` through content-hash links and rejects forks, cycles, duplicate
parents, disconnected entries, and multiple genesis entries. A mixed log uses each
entry version's hash algorithm for its outgoing chain link; all version 2 entries use
sequence, never wall-clock time.

## 6. Sequence and ordering

Sequence is scoped to `(session_id, principal_id)`, begins at zero on a new chain, and
increments by exactly one for every committed version 2 entry in that chain, regardless
of whether the entry is a domain assertion or kernel-observed action. Sequence is the
canonical order among version 2 entries. Timestamp is signed metadata only and MUST
NOT determine chain order.

When an existing version 1 chain is migrated, the runtime first verifies its unique
hash-link topology and initializes the durable sequence counter to the number of
verified legacy entries. The first version 2 entry receives that value. Historical
version 1 entries do not acquire a retroactive signed sequence; their order remains
proven by their hash links. Migration MUST fail closed if the legacy chain is invalid
or ambiguous.

All audit append paths must converge on one sequencer. Adding this interface while
leaving existing kernel append paths without sequence would create two incompatible
orders and is non-conforming.

Concurrent calls may complete in either order. Their committed sequence determines
the order. A caller that needs domain ordering must serialize calls or encode and
validate its own domain revision in attributes; it cannot assume invocation start
order.

## 7. Idempotency

The idempotency scope is:

```text
(session_id, principal_id, source_capsule_id, source_component_id,
 source_component_digest, schema, idempotency_key)
```

The host computes `request_hash = BLAKE3-256("astrid.audit.request.v1\0" ||
canonical_append_request)`. The request uses the same deterministic-CBOR rules and
sorting as section 5, excluding every host-stamped field. The durable idempotency
index stores `(scope, request_hash) -> entry_id`.

- A missing scope creates one entry and index record.
- An existing scope with the same request hash returns the original receipt with
  `duplicate = true`; it does not reserve a sequence or write an entry.
- An existing scope with a different request hash returns `conflict`, with
  `retryable = false`; it writes nothing.

Including the component ID and digest prevents another component or a newly approved
upgrade from inheriting an old component's retry keys. Including principal and session
prevents cross-tenant and cross-session collisions.

## 8. Atomicity, durability, and recovery

A successful append is one atomic durable transition covering:

1. entry bytes;
2. per-chain sequence and head;
3. session/principal index; and
4. idempotency index.

An implementation MUST use a storage transaction, atomic batch, or a write-ahead
journal with deterministic recovery that provides the equivalent property. A series
of independently durable key-value writes is not conforming. After crash recovery the
entry is either absent from all four structures or present in all four.

The host returns `receipt` only after the transition is durably committed under the
configured storage durability policy. `unavailable` or `internal` proves only that no
receipt was delivered; because a transport trap can occur after commit, callers retry
with the same idempotency key to learn the authoritative result.

## 9. Cancellation, retries, and backpressure

The call is synchronous from the guest's perspective. Before entering the commit
critical section, invocation cancellation returns `cancelled` and writes nothing.
Once the critical section begins, cancellation does not interrupt it; the host
finishes commit or rollback to preserve atomicity, then returns if the invocation is
still able to receive the result.

The host applies a bounded append-concurrency limit and bounded queue. It MUST NOT
spawn an unbounded thread or task per request. Queue exhaustion returns
`quota-exceeded` or `unavailable` without reserving a sequence. Callers SHOULD use
bounded exponential backoff only when `retryable = true`, always retaining the same
idempotency key.

There is no asynchronous fire-and-forget mode in version 1.0.0. A capsule that does
not need a receipt may discard it, but successful return still has the same durability
meaning.

## 10. Quotas and abuse resistance

In addition to section 2 bounds, the kernel enforces independently configurable:

- appends per second and burst per principal;
- appends per second and burst per source component;
- audit bytes per rolling window per principal;
- audit bytes per rolling window per source component; and
- total audit-store operator ceiling with reserved capacity for kernel security
  events.

Domain assertions MUST NOT be able to exhaust the capacity needed to record denials,
revocations, administrative changes, or storage-degradation alerts. When the domain
budget is exhausted, `append` fails with `quota-exceeded`; it does not evict prior
entries or borrow the kernel-reserved budget.

Repeated invalid, denied, conflicting, or quota-exceeded calls are metered through a
bounded security counter. Implementations MUST avoid recursively appending one full
audit entry for every rejected audit append, which would turn rejection into a storage
amplifier. Threshold crossings may produce a coalesced kernel-observed security entry.

## 11. Failure semantics

The interface records evidence; it does not perform the domain effect. Therefore the
kernel cannot universally decide whether failure to audit should fail the caller's
business operation. `append` itself always fails explicitly: it never reports success
before durable commit and never silently degrades to tracing.

Capsules whose safety claim requires an audit receipt MUST append before releasing the
corresponding allow/result and MUST fail closed when `append` fails. Codewall policy
enforcement is in this category: an unaudited allow is forbidden. A denial already
chosen by Codewall remains a denial if its audit append fails; the capsule reports the
audit failure separately and MUST NOT convert the denial to allow.

Capsules using the interface only for supplementary observability MAY continue their
domain operation after an append error, but must not claim that the operation is
audited. SDKs SHOULD expose explicit `required` and `observe` wrappers that differ only
in caller-side error handling; the host ABI and receipt semantics remain identical.

## 12. Audit feed and retrieval

After commit, the kernel MAY publish the canonical entry on
`astrid.v1.audit.entry`. Feed publication is secondary and best-effort; the durable
entry is the system of record. Feed loss cannot undo or invalidate a receipt.

Existing principal scoping applies to domain assertions. Without `audit:read_all`, a
subscriber may observe only entries for its own effective principal. This RFC does
not add payload dereference because assertions contain no opaque payload. Evidence
digests are commitments, not capabilities; any future digest-to-value API requires
separate read authorization.

Receipt verification requires no storage access when the verifier already trusts the
runtime public key: it reconstructs `S` from `signed-entry`, verifies the signature,
recomputes `entry-hash`, and checks that the convenience fields equal their values in
the signed body. Proving chain inclusion relative to a later head or external anchor
requires the intervening entries or a future checkpoint proof; that is outside this
RFC.

## 13. Compatibility and rollout

This change is additive:

- capsules that do not import `astrid:audit@1.0.0` continue unchanged;
- absent `audit_append` is deny-by-default;
- existing `.capsule` archives remain installable;
- version 1 audit entries retain their current decoder and verifier; and
- the event feed retains its existing topic and principal-scoping rules.

Implementation must land in this order:

1. introduce explicit audit entry format versioning and a mixed-version verifier,
   including topology-based verification for legacy entries;
2. add persistent per-chain sequence and migrate verified legacy chain lengths;
3. make entry/head/sequence/index/idempotency commit atomic and add crash-recovery
   tests;
4. add the canonical version 2 codec and independent golden verification vectors;
5. add `audit_append` parsing, expansion review, and approved-snapshot threading;
6. add the host interface, bounds, quotas, and receipt;
7. add SDK bindings/builders and conformance fixtures; and
8. migrate selected applications such as Codewall.

The interface MUST NOT ship on top of timestamp ordering or a non-atomic multi-write
append and call the result a durable inclusion receipt.

## 14. Current substrate gaps

This section records the implementation audit performed while drafting the RFC. It is
non-normative evidence for the rollout order, not a permanent characterization of
Astrid after implementation:

- `astrid-audit/src/log.rs` serializes concurrent append under a mutex, which prevents
  ordinary in-process forks, but `astrid-audit/src/storage.rs` currently persists the
  entry, session index, and chain head as independent key-value writes. A crash can
  expose a partial append.
- Chain verification currently sorts entries by timestamp before checking links. A
  wall-clock collision or reversal can make a valid stored chain appear invalid or
  obscure the only canonical traversal. Version 1 verification needs topology-based
  traversal before migration.
- Version 1 `AuditEntry::signing_data` serializes action and authorization through JSON
  and signs only the success/failure discriminator for `AuditOutcome`; failure detail
  is not covered. The domain interface therefore requires a versioned canonical entry
  rather than adding another variant to that encoding.
- The existing capsule `HostAuditSink` intentionally uses `continue + alert` when
  persistence fails and returns no receipt. That is appropriate for its current
  observational contract but cannot implement required domain evidence.
- The current loader can derive a synthetic name/version hash when verified component
  content metadata is absent. Domain receipts require a digest computed from the exact
  bytes instantiated and must fail closed if that provenance cannot be established.
- The install subsystem already stores an approved `CapabilitiesDef` snapshot and
  detects expansions. The new host state must receive that approved snapshot (including
  the effective component-specific request), rather than authorize against the live
  untrusted manifest alone.

These are prerequisites, not reasons to weaken the receipt definition.

## 15. Conformance tests

A conforming implementation includes at least these regressions:

- principal, capsule/package ID, component ID, component digest, session, time, and
  sequence cannot be supplied or forged by a guest;
- principal-less invocation fails closed;
- exact and trailing-prefix grants work; malformed/interior wildcard grants fail;
- unapproved schema is denied even when the untrusted manifest requests it;
- capability expansion is detected on install and upgrade;
- every request bound rejects at boundary values and accepts the maximum valid value;
- UTF-8, NUL, duplicate keys, digest lengths, and canonical sorting are covered;
- same idempotency key plus same request returns the same entry and sequence;
- same idempotency key plus changed request conflicts;
- idempotency scope separates principals, sessions, capsules, sibling components, and
  component upgrades;
- concurrent appends produce one contiguous sequence and one unforked chain;
- rollback/fault injection at every storage write yields all-or-nothing recovery;
- cancellation before commit writes nothing; cancellation during commit cannot leave
  partial state;
- quota rejection does not consume a sequence or kernel-reserved capacity;
- full disposition, attributes, evidence, provenance, and stamped identity are covered
  by signature mutation tests;
- version 1 fixtures still verify; mixed version 1/version 2 logs verify;
- version 2 verification ignores timestamp order and rejects sequence gaps;
- event-feed loss does not affect durable lookup or receipt verification; and
- Codewall's required-audit allow path cannot release an allow without a receipt.

Canonical codec tests MUST include language-independent hex fixtures for signing
bytes, signature, entry hash, and receipt, plus at least one independent verifier
implementation or separately implemented test decoder.

# Drawbacks
[drawbacks]: #drawbacks

- This is a real host ABI and manifest expansion, not a capsule-only feature. It
  requires coordinated changes to canonical WIT, Rust SDK bindings, builder tooling,
  kernel host state, audit storage, and verification.
- A durable synchronous append adds latency to safety-critical paths. Batching may
  improve storage throughput, but it cannot weaken the per-call receipt semantics.
- Strings can still contain secrets. Typed fields and hard bounds reduce the blast
  radius but cannot replace application data classification.
- A signed capsule assertion proves provenance and inclusion, not truth. Consumers
  that ignore the `capsule-assertion` provenance can still misrepresent it.
- The prerequisite hardening exposes shortcomings in the current audit format and
  storage append. That makes the implementation larger, but avoiding the work would
  make the advertised guarantee false.
- Per-component schema approval and quotas add operator surface area.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why a host call rather than a reserved IPC topic

The event bus is appropriate for routing and observation, not proof of durable
inclusion. A reserved topic still has two distinct success points—publish accepted and
audit consumer committed—and cannot synchronously return the signed receipt without a
second correlation protocol. Lag/drop semantics, consumer restart, spoofable payload
identity, and queue backpressure all enlarge the proof boundary. A host call crosses
directly into the key-custody and storage boundary, uses host state for identity, and
has one typed result.

This rejects the reserved-topic path proposed by the draft audit-state-replication
RFC for domain audit append. That proposal's content-addressed value-store ideas remain
orthogonal and can be built later behind separate read authority.

## Why typed assertions rather than arbitrary bytes or JSON

Arbitrary payloads make the audit store a secret-dumping and storage-amplification
surface. JSON also leaves canonicalization, number representation, duplicate keys,
and semantic equivalence ambiguous. The scalar attribute plus evidence-digest model
covers audit decisions while keeping signed entries small, deterministic, and
inspectable. New value forms can be added through versioned WIT rather than smuggled
through an opaque field.

## Why host-stamped component identity and digest

Capsule/package ID alone identifies a logical application, not which component or
which bytes made a claim. A multi-component archive, upgraded component, or substituted
component could make different assertions under the same package ID. Binding the
selected manifest component ID and the digest of the exact instantiated bytes turns
the receipt into useful software provenance and scopes idempotency across siblings and
upgrades. A synthetic name/version digest is not sufficient.

## Why installation-approved schema namespaces

Letting every capsule write every schema enables impersonation (`codewall.decision@1`
from a non-Codewall capsule) and makes audit consumers trust names with no provenance
policy. An allowlist fits Astrid's existing capability-review model and gives upgrades
an explicit authority delta. Namespace permission is intentionally separate from
principal action capabilities because append records an assertion; it does not grant
the asserted action.

## Why no guest-supplied timestamp or sequence

Both are provenance. A guest-supplied time can backdate a decision, and a
guest-supplied sequence can create gaps or collisions. Domain revisions may be
ordinary signed attributes, but canonical audit ordering belongs only to the runtime.

## Why sequence rather than timestamp order

Wall clocks can move backward and multiple entries can share a timestamp. Hash-chain
append already serializes a canonical order, so persisting that order as a signed
sequence makes verification deterministic. It also detects deletion and duplication
without relying on timestamp tie-breaking.

## Why transactional idempotency

Exactly-once delivery cannot be promised across process crashes and lost responses.
Idempotent retry gives the useful equivalent: one committed assertion per scoped key,
and a reliable way to recover the original receipt. Keeping the mapping in the same
atomic transition as the entry avoids both duplicate entries and keys pointing to
missing entries.

## Why not let capsules sign their own entries

A capsule key could attest the capsule's own statement, but it would not attest the
effective human principal, host session, approved authority, runtime inclusion, or
chain order. Giving a capsule the runtime key would destroy the trust boundary. The
runtime signature deliberately wraps a visibly capsule-originated assertion.

## Why not add Codewall-specific audit variants to the kernel

The kernel should not learn Codewall policy semantics. A stable domain assertion
envelope lets Codewall evolve its versioned schema at the edge while the kernel
enforces only provenance, bounds, authorization, ordering, and durability.

## Impact of not standardizing

Applications will continue publishing best-effort `astrid.v1.audit.*` events or
writing private logs. Those records have inconsistent identity, durability, schema,
and proof semantics. Consumers will eventually treat an application-authored message
as equivalent to a runtime-signed fact, which is the exact ambiguity this RFC removes.

# Prior art
[prior-art]: #prior-art

- **Linux audit and system call provenance**: the kernel stamps process identity and
  observed operation rather than trusting a process to describe its own caller. This
  RFC preserves that distinction while adding a separate userspace-assertion type.
- **Windows Event Tracing and journald structured fields**: applications can emit
  structured domain events, but those systems generally do not provide a per-principal
  signed hash chain or durable inclusion receipt.
- **Certificate Transparency and other append-only transparency logs**: signed tree
  heads and inclusion proofs motivate receipts and external verification. Astrid's
  current linear chain is simpler; checkpoint proofs remain future work.
- **Sigstore transparency records**: software identity and content digests demonstrate
  why a logical package name is insufficient without an exact artifact digest.
- **Cloud audit APIs using request tokens**: idempotency tokens are established prior
  art for recovering one authoritative write across retries and uncertain responses.
- **OpenTelemetry logs**: structured attributes and explicit semantic conventions are
  useful, but telemetry's sampling/drop posture is not suitable for evidence required
  before releasing a security decision.
- **Event sourcing and write-ahead logs**: a persisted sequence, atomic head update,
  and deterministic recovery are standard prerequisites for claiming an ordered,
  durable log.
- **The draft Astrid audit-state-replication RFC**: it identifies the need for
  capsule-originated evidence and content hashes. This RFC narrows the first primitive
  to a governed typed host ABI and rejects a reserved IPC topic for durable append.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- The exact default quota values are operational policy and should be benchmarked
  before implementation. The dimensions and fail-closed behavior are normative here.
- Astrid must choose a transaction mechanism supported by its audit storage or define
  the exact journal recovery protocol. The all-or-nothing property is not optional.
- Whether the receipt should later carry a checkpoint/Merkle inclusion proof is left
  for an external anchoring RFC. Version 1.0.0 proves the entry signature and immediate
  chain link, not inclusion under a later anchored head.
- The canonical integer-key table for deterministic CBOR must be published alongside
  implementation golden vectors. This RFC fixes the encoding rules; the assigned RFC
  number can namespace the final table.
- A future policy may reserve schema prefixes such as `astrid.*` exclusively for
  kernel or first-party components. Version 1.0.0 SHOULD reserve it, but the complete
  registry governance belongs in the implementation tracking issue.

# Future possibilities
[future-possibilities]: #future-possibilities

- Periodic Merkle checkpoints and Unicity anchoring can turn receipts into compact
  third-party-verifiable inclusion proofs.
- A separately authorized encrypted content-addressed evidence store can resolve
  digests without putting sensitive bytes in the audit entry.
- Schema packages may publish machine-readable attribute definitions and SDK newtypes,
  while the kernel continues treating the assertion as domain-opaque.
- Selective-disclosure proofs can reveal chosen attributes while retaining a proof
  against an anchored entry.
- Fleet-level replication can preserve the original runtime signature and add a
  receiving-runtime countersignature without rewriting provenance.
- Hardware-backed runtime keys and remote attestation can strengthen the statement
  about which Astrid build produced a receipt.
- A batched append API may reduce transaction overhead, provided each item retains
  independent idempotency and the returned receipt set has unambiguous atomicity.
