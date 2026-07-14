- Feature Name: `authenticated_control_connections`
- Start Date: 2026-07-14
- RFC PR: [rfcs#37](https://github.com/astrid-runtime/rfcs/pull/37)
- Tracking Issue: [astrid#1229](https://github.com/astrid-runtime/astrid/issues/1229)

# Summary
[summary]: #summary

Astrid will expose one neutral, versioned control protocol owned by the native
daemon. Every accepted connection receives a host-generated identity and every
request receives a host-owned context. Local clients authenticate directly as
their principal. A generic service may act for a separately authenticated
subject only through an explicit delegated session whose effective authority is
the intersection of the service, subject, both credentials, target policy and
the concrete operation capability.

The same request context is propagated across capsule hops through a new
versioned IPC reply-context resource. Replies and stream frames return only to
the connection and request that own them. This replaces principal-wide response
routing and rejects any product-specific edge role, router, topic or bypass.

Activation depends on the companion `content_addressed_authority_profiles` RFC.
Delegated sessions remain unavailable until its wildcard-free registry,
self-healing migration and sealed exact evaluator are active.

# Motivation
[motivation]: #motivation

Astrid's current native daemon binds `system.sock`, but passes the listener to a
WASM CLI proxy. The proxy forwards external JSON to the event bus and fans bus
messages back to socket clients. Principal and, for chat, conversation session
are its routing keys. Many correlated replies remain principal-wide. Two clients
using the same principal or service identity can therefore observe frames that
belong to another connection and rely on client-side correlation to ignore them.
That is not an authorization boundary.

The current path also has an intentionally compatible anonymous handshake. A
client that lacks a principal signing key can complete the local session-token
round trip without an authenticated principal. That is useful for old clients,
but a new typed client or service must never silently downgrade to it.

A distribution needs to run ordinary system services above Astrid. A local CLI
can authenticate directly as its user. An HTTP or automation service is
different: the socket actor is the service, while the effective subject is a
human or automation principal authenticated by that service. Treating the
distribution as a special trusted edge would create a product bypass in a
neutral runtime. Giving the service a universal delegation capability would
turn compromise of one network process into ambient runtime root.

The event bus has a second ambiguity. A capsule can receive batches, process
work asynchronously, and publish later. Invocation-local caller context is not
enough to prove which delivered request a response belongs to. Correlation IDs
inside caller-controlled JSON are useful application data, but they are
forgeable and cannot route privileged output.

The expected outcome is a small stable control boundary with the following
properties:

- Astrid remains useful without any particular distribution.
- A distribution uses the same public facilities as any other system service.
- Direct clients do not impersonate themselves through a service.
- Delegation never amplifies either party's authority.
- A request and every response frame have one host-owned route and audit chain.
- Revocation, deadlines and responder checks remain enforceable during streams.
- Raw topics and internal router enums do not become the public control API.
- Existing clients have an explicit compatibility path rather than a flag day.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## Direct local clients

An interactive `astrid` client connects to `system.sock`, negotiates the control
protocol, and proves possession of a credential registered on its principal. The
daemon assigns a `ConnectionId`. The connection's actor and subject are the same
principal and credential.

An interactive client from a distribution behaves identically. Installing a
distribution does not create a different trust rule. Product branding, process
name, executable path and repository origin are not authentication inputs.

Every request is a typed operation. The daemon assigns a `RequestId`, resolves
the exact capability required by that operation, evaluates the subject and
credential, then dispatches internal work. The client receives an acknowledgement
and zero or more events followed by exactly one terminal result.

## Services and delegated subjects

A long-running HTTP, automation or fleet service has its own service principal
and credential. The profile is marked as a service so policy and audit can reject
using a human credential where a workload identity is required; the identifier
still belongs to Astrid's ordinary principal namespace.

After authenticating an external caller, the service requests a delegated
session. It names:

- the subject principal;
- a mandatory active subject credential registered for delegation by this
  service;
- the external authentication event identifier or digest;
- an expiry no later than the subject credential, service credential or
  connection expiry.

Opening the session requires `delegate:session:open`. It grants no operation by
itself. For an operation requiring capability `C`, Astrid requires all of:

1. the service principal and its authenticating credential are active;
2. `C` is registered as delegable and the service holds the registered exact
   capability `delegate:C`;
3. the subject principal and delegated credential are active;
4. the subject holds `C`;
5. both credential scopes, revokes, target constraints and policy allow it;
6. the delegated session, connection and request are live.

Any deny wins. Delegation uses a sealed exact-only evaluator introduced by the
`content_addressed_authority_profiles` RFC. Neither the legacy `*` grant, a namespace
selector, nor the built-in agent's legacy `delegate:self:*` string can satisfy
`delegate:session:open` or `delegate:C`. Delegated sessions remain disabled
until that evaluator and the wildcard migration are active. The subject
credential cannot be omitted to mean full authority. Delegation is thus an
intersection, never impersonation that discards the actor.

Astrid does not interpret the service's external authentication protocol. The
subject credential record says which service may assert it and its exact scope.
Provisioning or expanding that record is a separate privileged operation. The
service is trusted only to make assertions within the explicit delegation policy
it was granted, analogous to a login service with narrowly defined system
privileges.

## Request and reply ownership

The control server generates the request route. Client request IDs and
idempotency keys are never used as authorization keys. The route contains the
system, connection, request, actor, subject, both credentials, delegated session,
deadline, primary/conditional capabilities and expected responder policy.

When the request enters a capsule, the host gives that capsule an unforgeable
reply-context resource. Publishing through the resource carries the route to the
next capsule. Finishing through the resource emits a response on the same route.
The guest cannot name another connection or request.

Fan-out creates independent child resources that all refer to the same request
and record their branch source. Single-responder operations accept a terminal
reply only from the expected capsule/source. Multi-responder operations declare
their responder set and completion rule in the typed operation definition.

Interactive prompts are request-owned too. The host assigns an `InteractionId`
whose response is accepted once, before its deadline, only from the connection
and delegated session that own the original request. A descriptor may mark an
interaction resumable; after a disconnect, `interaction.respond` freshly
authenticates and authorizes an equivalent actor/subject/target under a new
connection and delegated session before accepting the response. A prompt
response cannot be replayed into another request or delivered through a
principal-wide topic.

Before each protected dispatch and before delivering each stream frame, the host
checks that the route remains live. Credential, capability or delegated-session
revocation closes the route with an authorization error. A deadline closes it
with a timeout. Closing the external connection cancels work that has not
committed. Work that has committed detaches delivery, completes, and stores its
durable outcome for an authenticated retry.

## Retry and idempotency

Every request has a host-generated `RequestId`. Mutating operations additionally
require a client-generated idempotency key. The key is scoped to system, actor,
subject and operation. Reusing a key with a different canonical request is an
error. Reusing it with the same request attaches to the live request or returns
the stored outcome after reauthorizing the caller. This is not a general
exactly-once promise: Astrid promises one durable idempotency outcome and never
repeats a mutation that reached its declared commit point.

Read-only operations may omit an idempotency key. They still have request IDs,
deadlines and connection ownership.

## Compatibility

The native daemon continues to listen at the existing `system.sock` path. It
detects the legacy handshake shape and serves the existing protocol through a
compatibility adapter. New control clients negotiate protocol version 2 and must
fail if authenticated mode is unavailable.

The default composition retires the WASM socket proxy only after the native
adapter passes old-client, new-client and packaged upgrade tests. Legacy support
has an explicit compatibility window and telemetry; it is not a silent fallback
from protocol 2.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Terminology and types

- **System**: one Astrid installation, identified by immutable `SystemId`.
- **Actor**: the principal authenticated on the control connection.
- **Subject**: the principal on whose behalf an operation executes.
- **Direct session**: actor and subject are identical.
- **Delegated session**: a service actor acts for a separately authenticated
  subject without erasing the actor.
- **Credential**: a revocable proof-of-possession record. A `DeviceId` identifies
  the device/workload; a `CredentialId` identifies one key or authenticator on it.
- **Connection**: one authenticated transport instance, identified by a
  host-generated `ConnectionId`.
- **Request**: one typed operation, identified by a host-generated `RequestId`.
- **Reply context**: an unforgeable host resource that carries a request route
  through capsule work.

`SystemId`, `ConnectionId`, `RequestId`, `InteractionId` and
`DelegatedSessionId` are distinct opaque types. Each contains 128 bits produced
by the operating-system CSPRNG. Its wire form is a type prefix followed by
exactly 26 lowercase RFC 4648 base32 characters without padding, encoding the
identifier in big-endian byte order with zero trailing pad bits. Prefixes are
`sys_`, `con_`, `req_`, `int_` and `del_`. Decoders reject uppercase, padding,
the wrong prefix, nonzero trailing pad bits and any non-canonical spelling. A
generated collision is retried up to eight times and then fails closed. The
identifiers are not filesystem paths, hostnames or secrets.

`ApplicationSessionId` is a separate validated newtype that preserves the
already-published bounded string wire representation used by application/chat
sessions. It is application data, not a host route or an alias for
`DelegatedSessionId`, and can never select a connection. Existing session IDs do
not churn merely to acquire a new prefix.

`ServiceId` is a validated view of a `PrincipalId` whose profile kind is
`service`; it does not create a parallel product identity namespace. Existing
principal IDs retain their wire representation. The principal profile schema
adds `kind = "human" | "service"`; missing legacy values read as `human`.
Creating or converting a service profile is an explicit privileged operation,
not an inference from an executable name, socket peer or product package.

`DeviceId` and `CredentialId` are separate. Existing device-key fingerprints
may migrate to a credential ID attached to a generated device record, but new
code must not treat a key rotation as a new physical/logical device.

## Native transport ownership

The native daemon owns the listener, accepted stream, handshake, `ConnectionId`,
connection cancellation token, pending request table and final socket write.
Neither a capsule nor an IPC message may choose a connection destination.

The socket path and framing are host concerns, not the stable `RuntimeTransport`
API. A future TLS/remote transport must implement the same typed semantics and
conformance suite without exposing the event bus.

The public client contract is the following transport-neutral shape:

```text
RuntimeTransport::connect(
    target: SystemTarget,
    credential: CredentialProvider,
    options: ConnectOptions,
) -> RuntimeConnection

RuntimeConnection::request<T: Operation>(
    request: T::Request,
    options: RequestOptions,
) -> RequestStream<T::Event, T::Result>

RuntimeConnection::open_delegated_session(
    request: OpenDelegatedSession,
) -> DelegatedSession
RuntimeConnection::refresh_delegated_session(...)
RuntimeConnection::close_delegated_session(...)
RuntimeConnection::respond(interaction_id, typed_response)
RuntimeConnection::cancel(request_id)
```

`SystemTarget` is a validated local installation or a future remote system
identity. `CredentialProvider` selects a registered credential and signs the
server challenge without exposing key bytes to the transport. `ConnectOptions`
sets supported protocol versions and features, not raw socket paths, topics or
principal overrides. `RequestOptions` carries deadline, idempotency key,
application session and optional delegated session. Generated clients bind a
typed request, event and result DTO to one versioned operation descriptor.

Local and future remote implementations must pass the same authentication,
authorization, cancellation, ordering, backpressure, idempotency and error-code
conformance suite. A transport cannot weaken those semantics because it is local.

## Credential custody and service provisioning

The interactive CLI generates its device credential through a
`CredentialProvider`, registers only the public key during bootstrap or pairing,
and stores the private key in the platform keychain, secure enclave, TPM,
hardware authenticator or owner-only fallback. A target installation records the
credential IDs available for that system; the CLI selects one explicitly when
more than one is eligible. It never reads a shared daemon administrator key.

A headless client enrolls through a one-time, scoped pairing record and then owns
its own key. A generic system service similarly generates its workload key in
the service manager's credential store or a hardware-backed provider. A
nondelegable service-provisioning operation registers the public key and service
profile under an administrator's explicit authority. The service receives only
the direct and `delegate:C` capabilities its operation map needs. Rotation
registers a new `CredentialId`, switches the service atomically and revokes the
old credential and its live sessions.

Fresh bootstrap is exclusively an out-of-band local-console transaction while
the daemon is boot-locked and before `system.sock` accepts connections. The
installer's credential provider generates the administrator and recovery keys,
proves possession, and supplies only their public keys to the atomic bootstrap
transaction. There is no anonymous "become the first administrator" operation.

Invite and pair-device redemption use a separate bounded enrollment handshake,
not an anonymous control request and not a delegated session. An
`enrollment_hello` carries the one-time token, desired public key, client nonce
and enrollment kind. The daemon returns a runtime-signed challenge; the client
signs the transcript with the desired private key. Under the authority write
lock, the daemon verifies and atomically consumes the token, registers the new
credential/device (and invite principal when applicable), records audit, then
closes the enrollment connection. It never exposes another control operation on
that connection. Tokens are rate-limited, redacted and single-use. A product
service may relay these opaque frames but receives no special trust and never
learns the client's private key. The enrolled client reconnects through the
ordinary authenticated protocol.

The enrollment state machine binds its internal consume actions to the exact,
nondelegable `invite:redeem` and `auth:pair:redeem` registry entries. Those
entries have no ordinary control `OperationDescriptor`; possession and proof of
the corresponding one-time token are mandatory.

## Protocol negotiation and authentication

Protocol 2 continues to use length-prefixed frames with a maximum handshake
frame size of 4096 bytes. JSON field names below are normative. Unknown fields
are ignored only when the negotiated feature declares that extension; otherwise
they are rejected.

The client sends:

```json
{
  "type": "client_hello",
  "min_version": 2,
  "max_version": 2,
  "features": ["delegated_sessions", "reply_context_1"],
  "signature_algorithms": ["ed25519"],
  "actor": "service-or-principal-id",
  "credential_id": "credential-id",
  "client_nonce": "canonical-unpadded-base64url-32-bytes",
  "local_session_token": "64-lowercase-hex"
}
```

For a local Unix-socket transport, the daemon requires all of: a socket parent
owned by the daemon user with mode `0700`, matching peer user credentials, and
the current owner-only session token. The session token binds the client to the
running daemon instance but never replaces proof of the actor credential. A
future remote transport defines an equivalent channel-binding factor.

The daemon validates version overlap, frame bounds, peer policy and the actor and
credential record, then returns:

```json
{
  "type": "server_challenge",
  "version": 2,
  "features": ["delegated_sessions", "reply_context_1"],
  "system_id": "sys_...",
  "connection_id": "con_...",
  "signature_algorithm": "ed25519",
  "server_identity_key_id": "runtime-identity-key-id",
  "capability_registry_digest": "canonical-unpadded-base64url-sha256",
  "operation_manifest_digest": "canonical-unpadded-base64url-sha256",
  "server_nonce": "canonical-unpadded-base64url-32-bytes",
  "challenge_expires_unix_ms": 1784000000000,
  "server_signature": "canonical-unpadded-base64url-signature"
}
```

The server signature authenticates the selected system and endpoint. A local
installation stores its immutable `SystemId`, runtime public identity key and
key ID in owner-controlled installation metadata. `RuntimeTransport` refuses a
different system or key unless the operator performs an explicit re-enrollment.
The runtime identity private key remains in platform key custody or an owner-only
daemon key file and is never returned through the control protocol.

The feature hash is SHA-256 over deterministic CBOR of the sorted, duplicate-free
selected feature strings. Nonces are exactly 32 random bytes. Base64url fields
use the canonical RFC 4648 URL-safe alphabet without padding or case conversion.
The signed server transcript is deterministic CBOR of the complete selected
version, feature hash, both signature algorithms, system ID, connection ID,
actor, credential ID, both manifest digests, both nonces and challenge expiry,
prefixed by the ASCII domain `astrid-control-server:v2\0`.

After verifying the server signature and pin, the client signs the same fields,
including the verified server signature digest, prefixed by
`astrid-control-client:v2\0`, then sends:

```json
{
  "type": "client_proof",
  "signature_algorithm": "ed25519",
  "signature": "base64url-signature"
}
```

Each challenge is single-use and expires 30 seconds after creation. The daemon
marks it consumed before proof verification so a failed proof cannot be replayed.
It sends `server_accept` only after signature verification and a fresh policy
check:

```json
{
  "type": "server_accept",
  "version": 2,
  "features": ["delegated_sessions", "reply_context_1"],
  "system_id": "sys_...",
  "connection_id": "con_...",
  "actor": "principal-id",
  "credential_id": "credential-id",
  "device_id": "device-id",
  "capability_registry_digest": "canonical-unpadded-base64url-sha256",
  "operation_manifest_digest": "canonical-unpadded-base64url-sha256",
  "policy_epoch": 42,
  "connection_expires_unix_ms": 1784003600000,
  "max_frame_bytes": 1048576,
  "max_in_flight_requests": 64,
  "default_initial_event_credit": 64,
  "max_event_credit_per_request": 256
}
```

The client converts the connection expiry to a monotonic deadline when it
receives this frame. The daemon does the same for request deadlines at
acceptance. An already expired deadline or one more than 24 hours ahead is
rejected; a client clock offset does not move an accepted monotonic deadline.
Generated clients verify that the signed operation-manifest digest is one they
were built to consume before sending a typed request. A registry or manifest
digest mismatch never falls back to raw JSON or an internal enum.

An authenticated protocol-2 constructor must reject missing keys, wrong keys,
revoked keys, anonymous results and protocol downgrade. The legacy constructor
is separately named and cannot be selected by retry logic.

## Delegated credential records

A delegated credential record contains:

```text
credential_id: CredentialId
subject: PrincipalId
issuer_service: ServiceId
device_id: DeviceId
allowed_capabilities: set<CapabilityRef>
allowed_targets: set<TargetConstraint>
issued_at: Timestamp
expires_at: Timestamp
revoked_at: optional<Timestamp>
authentication_class: opaque bounded string
policy_digest: 32 bytes
```

The record is created or expanded only by an already-authenticated direct
`delegation.policy.set-self` operation from the subject or by the direct
administrative `delegation.policy.set` operation. The latter additionally
requires `authority:profile:manage`; confirmation fields alone are insufficient.
Invite and pair-token redemption create ordinary principal/device credentials
through the isolated enrollment handshake and never create delegation policy.
Capability entries are exact; wildcard entries are invalid. The opaque
authentication class is audit metadata, not an authorization shortcut.
`policy_digest` is domain-separated deterministic CBOR over the issuer, subject
credential, exact capability and target ceilings, validity interval and
authentication class.

Opening `delegated_session.open` version 1 requires the actor to be a service
profile and to hold exact `delegate:session:open`. The request carries the
delegated credential ID, an external-authentication event digest, authentication
and evidence-expiry timestamps, requested exact capability/target ceilings and
session expiry. The host creates:

```text
DelegatedSession {
    id: DelegatedSessionId,
    connection_id: ConnectionId,
    actor_service: ServiceId,
    actor_credential_id: CredentialId,
    actor_device_id: DeviceId,
    subject: PrincipalId,
    subject_credential_id: CredentialId,
    subject_device_id: DeviceId,
    external_auth_event_digest: 32 bytes,
    external_auth_authenticated_at: Timestamp,
    external_auth_expires_at: Timestamp,
    capability_ceiling: sorted set<CapabilityRef>,
    target_ceiling: sorted set<TargetConstraint>,
    credential_policy_digest: 32 bytes,
    effective_policy_digest: 32 bytes,
    created_at: Timestamp,
    expires_at: Timestamp,
    revoked_at: optional<Timestamp>,
}
```

The open request carries when the external authentication completed and its
bounded expiry. Astrid rejects evidence from the future beyond bounded clock
skew, already-expired evidence or an expiry beyond the delegated credential's
policy. The event digest is single-use for one issuer, subject credential and
canonical open request. Its spent record is retained through evidence expiry
plus the maximum accepted clock skew; cleanup never makes an old digest usable
again. Reusing an idempotency key with the identical open request returns the
same live session; reusing the digest or key with different input is rejected.

Session expiry is the minimum of the requested expiry, evidence expiry,
connection expiry, actor credential expiry and subject credential expiry.
Session authority is the exact intersection of the requested ceilings and both
credentials' current authority, targets and denies. The
`effective_policy_digest` commits to that intersection, the capability-registry
manifest and the current policy epoch.

A delegated session is bound to its originating connection and does not survive
reconnection. `delegated_session.refresh` reauthenticates both credentials and
fresh external authentication evidence and creates a replacement bound to the
new connection; `delegated_session.close` revokes the old session. Rotation or
revocation of either credential immediately revokes every route and delegated
session authenticated by it while retaining the stable `DeviceId`.

Profile management, recovery, credential issuance, service provisioning and any
registry entry marked `delegable = false` cannot be placed in a delegated
session ceiling. Every `delegate:C` grant is privileged authority whose grant,
revoke or profile inclusion also requires `authority:profile:manage`.

## Control request framing

After authentication, a client sends typed request frames:

```json
{
  "type": "request",
  "client_request_id": "opaque-bounded-client-value",
  "application_session_id": "existing-session-id-or-null",
  "deadline_unix_ms": 1784000000000,
  "idempotency_key": "opaque-bounded-key-or-null",
  "delegated_session_id": "del_...-or-null",
  "operation": "capsule.list",
  "operation_version": 1,
  "initial_event_credit": 64,
  "attachments": [],
  "payload": {}
}
```

`client_request_id` is echoed for client bookkeeping and is never trusted for
routing. It is 1-128 printable ASCII bytes. Deadlines may be at most 24 hours in
the future; individual operations may impose a lower maximum. Idempotency keys
are 16-128 bytes after UTF-8 encoding. Mutating operation definitions mark them
required. A live `client_request_id` cannot be reused on the same connection;
after terminal delivery it may be reused as a bookkeeping label, but it has no
retry semantics. Only the idempotency key and canonical request digest identify
a mutating retry.

All post-authentication frames are at most 1 MiB. A request may declare bounded
attachments as `{attachment_id, media_type, byte_length, sha256}`. Bytes arrive
in ordered `attachment_chunk` frames of at most 64 KiB and are dispatched only
after the declared size and digest verify. The operation descriptor sets the
total attachment limit; the initial capsule-install operation permits at most
256 MiB. Frames and DTOs never carry a host filesystem path. A local CLI streams
an opened file as bytes under its own authority.

The daemon returns an acknowledgement before nontrivial work:

```json
{
  "type": "accepted",
  "client_request_id": "...",
  "request_id": "req_...",
  "operation": "capsule.list",
  "operation_version": 1,
  "deadline_unix_ms": 1784000000000,
  "sequence": 0
}
```

The complete post-authentication frame family is:

- client to server: `request`, `attachment_chunk`, `cancel`, `window_update` and
  `interaction_response`;
- server to client: `accepted`, `event`, `interaction`, `result`, `error`,
  `cancel_ack` and `connection_expiring`.

The client-owned frame fields are normative:

```json
{
  "type": "attachment_chunk",
  "request_id": "req_...",
  "attachment_id": "bounded-id",
  "offset": 0,
  "data": "canonical-unpadded-base64url-bytes"
}
```

```json
{
  "type": "window_update",
  "request_id": "req_...",
  "event_credit_increment": 32
}
```

```json
{
  "type": "cancel",
  "request_id": "req_..."
}
```

```json
{
  "type": "interaction_response",
  "request_id": "req_...",
  "interaction_id": "int_...",
  "response_kind": "registered-response-kind",
  "payload": {}
}
```

All four frames are accepted only on the connection that owns `request_id`.
Attachment IDs are unique within the request and must have been declared in its
descriptor list. Offsets are contiguous from zero; duplicate, overlapping,
out-of-order, excess-length and post-completion chunks are rejected. Incomplete
attachments are deleted when the request deadline or a descriptor-bounded upload
timeout expires. The final size and digest must match before authorization and
dispatch.

`event_credit_increment` is an integer from 1 through the negotiated remaining
credit ceiling. The request's initial credit defaults to the server value and
cannot exceed `max_event_credit_per_request`; updates cannot raise outstanding
credit above that maximum. `interaction_response` must match the registered
response kind/schema for that live interaction in addition to the ownership,
deadline and single-use rules below.

Every request-owned server frame carries `client_request_id`, `request_id`,
operation name/version and a monotonically increasing sequence number. `event`
adds a registered event kind and typed payload. `result` adds the registered
result kind and typed payload. `error` carries the stable error contract below.
Exactly one terminal `result` or `error` is permitted. A sequence gap is a
protocol error, never silently ignored.

An `interaction` frame carries a host-generated `InteractionId`, interaction
kind, typed schema, bounded payload and deadline. `interaction_response` carries
that ID and its typed response. It is accepted exactly once, before the deadline,
from the owning connection and delegated session, with the same actor/subject
equivalence as the parent request. Approval, elicitation, selection, onboarding
and tool interactions use this mechanism instead of response topics. For an
interaction explicitly marked resumable, the separate `interaction.respond`
operation may answer after reconnect only after fresh authentication,
actor/subject/target equivalence and capability checks against the stored
interaction record.

`cancel` names the host `RequestId`; `cancel_ack` reports
`cancelled_before_commit`, `already_terminal` or `delivery_detached`. A
`window_update` adds event credit for one request. The server never emits more
events than credited. Each connection admits at most 64 in-flight requests by
default, and each descriptor may set a lower limit. Bounded responder queues
return `backpressure` when full. Droppable telemetry events must be explicitly
marked; operation results, interactions and nondroppable events are never
silently discarded. Stream resumption across a connection loss is not supported
by protocol 2; durable outcomes are retrieved through idempotent retry.

Operation names and their DTOs are independently versioned public types. They
must not serialize `KernelRequest`, `AdminRequestKind` or another internal enum.
An unknown operation or unsupported operation version fails before dispatch with
a stable error and the supported version range, without exposing internal enums.

The checked-in operation manifest is normative and generated clients consume it:

```text
OperationDescriptor {
    name: OperationName,
    version: nonzero u32,
    request_codec: SchemaDigest,
    event_codecs: sorted set<(EventKind, SchemaDigest)>,
    result_codecs: sorted set<(ResultKind, SchemaDigest)>,
    allowed_modes: set<Direct | Delegated | Anonymous>,
    primary_capability: optional<CapabilityRef>,
    conditional_capabilities: sorted set<ConditionalCapabilityRule>,
    target_extractor: HostTargetExtractorRef,
    mutation: ReadOnly | CommitPoint(CommitPointId),
    idempotency: Forbidden | Optional | Required { retention },
    route_policy: RoutePolicy,
    limits: OperationLimits,
    redaction: RedactionPolicy,
    legacy_mapping: optional<LegacyOperationId>,
    descriptor_digest: 32 bytes,
}
```

```text
ConditionalCapabilityRule {
    predicate: HostAuthorizationPredicateRef,
    capability: CapabilityRef,
}

OperationManifest {
    schema_revision: nonzero u32,
    descriptors: sorted set<(OperationName, version, descriptor_digest)>,
    digest_algorithm: "sha256",
    digest: 32 bytes,
}
```

The primary capability is always evaluated for a protected operation. A
conditional rule is evaluated from the validated DTO and current authority
state by a registered host predicate, never from a client-provided boolean. For
example, create/modify/grant/pair operations that would convey privileged
authority additionally require the content-bound
`authority:profile:manage` capability. Every evaluated capability and predicate
outcome is recorded in audit.

Operation descriptors, host predicates and target extractors are themselves
content-addressed. The descriptor digest is domain-separated deterministic CBOR
over every field and referenced schema/predicate/extractor/route-policy digest.
Loading the same operation name/version with a different descriptor digest fails
closed; an authorization-relevant change requires a new operation version.

The aggregate manifest digest is:

```text
SHA-256("astrid-operation-manifest\0" || deterministic-CBOR(
    schema_revision,
    descriptors sorted by canonical operation name then numeric version
))
```

Each tuple contains only the canonical name, version and already verified
descriptor digest. Duplicate `(name, version)` entries, noncanonical ordering or
a descriptor absent from the manifest fail closed. The generated client and
runtime use this exact construction for the digest signed in the authentication
transcript.

Delegability has one source of truth: the registry entry bound by
`primary_capability`. `allowed_modes` may further remove delegated mode but
cannot add it. An operation with a nondelegable conditional capability is itself
nondelegable. Profile management, administrator repair, credential issuance,
service provisioning and recovery operations therefore exclude `Delegated`.

The initial neutral operation inventory covers:

- runtime status, readiness, lifecycle and capsule reload;
- principal list/get/create/update/delete/enable/disable;
- device and credential list, rotation and revocation;
- capability catalog, effective-authority inspection, grant and revoke;
- quota get/set and usage;
- group list/create/update/delete;
- invitation and pair-token issue/list/revoke, with redemption confined to the
  enrollment handshake;
- capsule list/get/install/reload/remove, topic/schema inspection and environment
  configuration;
- audit query and authorized event subscription;
- agent prompt/request/event streams and typed interaction responses;
- application-session list/search/get/update/delete and message history;
- model list, active-model get and active-model set;
- delegated-session open/refresh/close.

In addition to the 51 migration-baseline capabilities in the companion RFC, the
initial control registry adds these exact IDs. Their entry digests and the final
`administrator` profile digest are generated from the accepted manifests before
activation:

```text
capability:catalog:read
authority:read
self:authority:read
device:list
self:device:list
device:revoke
self:device:revoke
credential:list
self:credential:list
credential:rotate
self:credential:rotate
credential:revoke
self:credential:revoke
auth:pair:list
auth:pair:revoke
service:provision
service:modify
service:revoke
delegation:policy:manage
self:delegation:policy:manage
capsule:inspect
self:capsule:inspect
capsule:config:read
self:capsule:config:read
capsule:config:write
self:capsule:config:write
self:audit:read
agent:prompt
self:agent:prompt
agent:requests:read
self:agent:requests:read
agent:events:read
self:agent:events:read
interaction:respond
self:interaction:respond
session:list
self:session:list
session:read
self:session:read
session:modify
self:session:modify
session:delete
self:session:delete
model:list
self:model:list
model:active:read
self:model:active:read
model:active:write
self:model:active:write
```

The companion authority-registry generator is the sole owner of
`delegate:session:open` and every derived `delegate:C` entry. This manifest only
references their content-bound entries. They are privileged and nondelegable.

Profile/authority repair, service/credential provisioning and
`delegation:policy:manage` are nondelegable. The self-scoped prompt, request,
event, interaction, session, model, capsule-inspection/configuration, audit and
identity-read entries, plus `self:delegation:policy:manage`, are included in the
exact built-in `agent` revision. The `use-only` preset includes the safe run-time
entries but excludes pairing, credential mutation, service provisioning and
delegation. The reviewed
`administrator` revision includes every base and control capability except
`delegate:session:open` and generated `delegate:C` service grants; an
administrator can assign those deliberately through profile management without
receiving service authority itself.

The initial manifest contains these operation bindings. `D` means direct and
`G` means delegated; the registry entry still decides whether `G` is possible.
`profile-manage-if-privileged` is the host predicate described above.

```text
runtime.status@1, runtime.readiness@1
  -> system:status; D; read-only
runtime.shutdown@1
  -> system:shutdown; D; mutation
runtime.capsules.reload@1
  -> capsule:reload; D; mutation

capability.catalog@1
  -> capability:catalog:read; D|G; read-only
authority.read@1 / authority.read-self@1
  -> authority:read / self:authority:read; D|G; read-only
authority.profile.list@1, authority.profile.diff@1
  -> authority:read; D; read-only
authority.profile.assign@1
  -> authority:profile:manage; D; mutation, nondelegable
administrator.repair@1
  -> authority:repair; D; +authority:profile:manage, nondelegable

principal.list@1, principal.get@1
  -> agent:list; D|G; read-only
principal.create@1
  -> agent:create; D|G; +agent:create:inherit when inheritance is requested;
     +agent:create:clone when cloning is requested;
     +authority:profile:manage when resulting authority is privileged
principal.update@1
  -> agent:modify; D|G; profile-manage-if-privileged
principal.delete@1 / enable@1 / disable@1
  -> agent:delete / agent:enable / agent:disable; D|G; mutation
principal.capability.grant@1 / revoke@1
  -> caps:grant / caps:revoke; D|G; profile-manage-if-privileged
quota.get@1 / quota.get-self@1
  -> quota:get / self:quota:get; D|G; read-only
quota.set@1 / quota.set-self@1
  -> quota:set / self:quota:set; D|G; mutation
usage.get@1 / usage.get-self@1
  -> quota:get / self:quota:get; D|G; read-only

group.list@1 / group.list-self@1
  -> group:list / self:group:list; D|G; read-only
group.create@1 / update@1 / delete@1
  -> group:create / group:modify / group:delete; D|G;
     profile-manage-if-privileged
invitation.issue@1 / list@1 / revoke@1
  -> invite:issue / invite:list / invite:revoke; D|G;
     profile-manage-if-privileged
pair-token.issue@1 / list@1 / revoke@1
  -> auth:pair / auth:pair:list / auth:pair:revoke; D|G;
     profile-manage-if-privileged

device.list@1 / device.list-self@1
  -> device:list / self:device:list; D|G; read-only
device.revoke@1 / device.revoke-self@1
  -> device:revoke / self:device:revoke; D|G; mutation
credential.list@1 / credential.list-self@1
  -> credential:list / self:credential:list; D|G; read-only
credential.rotate@1 / credential.rotate-self@1
  -> credential:rotate / self:credential:rotate; D; mutation, nondelegable
credential.revoke@1 / credential.revoke-self@1
  -> credential:revoke / self:credential:revoke; D; mutation, nondelegable
service.provision@1 / modify@1 / revoke@1
  -> service:provision / service:modify / service:revoke; D;
     +authority:profile:manage, nondelegable
delegation.policy.set@1 / revoke@1
  -> delegation:policy:manage; D; +authority:profile:manage, nondelegable
delegation.policy.set-self@1 / revoke-self@1
  -> self:delegation:policy:manage; D; nondelegable

capsule.list@1 / capsule.list-self@1
  -> capsule:list / self:capsule:list; D|G; read-only
capsule.get@1 / inspect@1 / inspect-self@1
  -> capsule:list / capsule:inspect / self:capsule:inspect; D|G; read-only
capsule.install@1 / install-self@1
  -> capsule:install / self:capsule:install; D|G; mutation
capsule.reload@1 / reload-self@1
  -> capsule:reload / self:capsule:reload; D|G; mutation
capsule.remove@1 / remove-self@1
  -> capsule:remove / self:capsule:remove; D|G; mutation
capsule.config.read@1 / read-self@1
  -> capsule:config:read / self:capsule:config:read; D|G; read-only
capsule.config.write@1 / write-self@1
  -> capsule:config:write / self:capsule:config:write; D|G; mutation

audit.query@1 / audit.query-self@1
  -> audit:read_all / self:audit:read; D|G; read-only
audit.subscribe@1 / audit.subscribe-self@1
  -> audit:read_all / self:audit:read; D|G; stream
agent.prompt@1 / agent.prompt-self@1
  -> agent:prompt / self:agent:prompt; D|G; stream
agent.requests@1 / agent.requests-self@1
  -> agent:requests:read / self:agent:requests:read; D|G; stream
agent.events@1 / agent.events-self@1
  -> agent:events:read / self:agent:events:read; D|G; stream
interaction.respond@1 / interaction.respond-self@1
  -> interaction:respond / self:interaction:respond; D|G; mutation
session.list@1 / list-self@1
  -> session:list / self:session:list; D|G; read-only
session.search@1 / search-self@1
  -> session:list / self:session:list; D|G; read-only
session.read@1 / read-self@1
  -> session:read / self:session:read; D|G; read-only
session.modify@1 / modify-self@1
  -> session:modify / self:session:modify; D|G; mutation
session.delete@1 / delete-self@1
  -> session:delete / self:session:delete; D|G; mutation
model.list@1 / list-self@1
  -> model:list / self:model:list; D|G; read-only
model.active.read@1 / read-self@1
  -> model:active:read / self:model:active:read; D|G; read-only
model.active.write@1 / write-self@1
  -> model:active:write / self:model:active:write; D|G; mutation

delegated-session.open@1 / refresh@1 / close@1
  -> delegate:session:open; D service actor; mutation
```

Provider `list`, `show` and `configure` are a typed product composition of
`capsule.list`, `capsule.inspect` and `capsule.config.read/write`; Astrid does not
add provider-specific topics or an untyped registry escape hatch. A downstream
distribution's generated route matrix must classify every route as
distribution-owned, direct-service or delegated-subject and name the exact
operation(s) above before that distribution's CLI or HTTP cutover starts. It is
not an Astrid activation prerequisite. A distribution HTTP route that requests
runtime-wide capsule reload binds `runtime.capsules.reload@1` as direct-service;
it cannot relabel that direct-only operation as a delegated-subject request.

Each manifest entry binds its exact DTOs, capabilities, host target extraction,
mutation commit point, responder policy and legacy adapter mapping. Product HTTP
routes and product-only operations remain outside Astrid; their repositories
carry a generated coverage table proving that every runtime-backed route maps to
one descriptor and every distribution-owned route is explicitly classified.
Adding a new operation is additive only when its name/version and DTO schema do
not change an existing contract.

Canonical authorization targets are typed as `System`, `Principal`, `Group`,
`Credential`, `CapsulePackage`, `CapsuleInstance`, `ApplicationSession`, `Model`
or `AuditScope`. The host derives them from a validated request DTO using the
descriptor's extractor. A service cannot provide a second free-form target that
differs from the operation payload.

```text
TargetConstraint = Exact(TargetRef)
                 | LocalSystem
                 | DelegatedSubject
                 | OwnedByDelegatedSubject(TargetKind)
                 | AllOfKind(TargetKind)
```

`TargetRef` contains a `TargetKind` and its canonical typed identifier.
`DelegatedSubject` and ownership constraints resolve when the host builds the
route, not from a service assertion. `AllOfKind` is an explicit privileged
constraint over one registered target kind; it is not a string wildcard and
does not match a target kind introduced later. List operations normally target
the local `System`, while object get/mutate operations extract the concrete
object target.

## Authorization algorithm

Each protected operation definition resolves to exactly one concrete primary
capability, zero or more exact conditional capabilities and optional target
constraints. An anonymous descriptor has no
capability, must be read-only, exposes no principal/system secrets and cannot
dispatch to a privileged capsule. Authorization follows this order:

1. reject unknown/disabled actor, actor credential, subject or subject credential;
2. reject expired/revoked connection or delegated session;
3. reject an operation not admitted by protocol features;
4. for direct requests, evaluate the subject principal, credential scope, exact
   capability, target policy and revokes;
5. for delegated requests, additionally require actor service kind,
   every evaluated capability is delegable, exact `delegate:session:open`, exact
   `delegate:C` for each evaluated capability `C`, actor credential scope,
   service target policy and the delegated session's capability/target ceilings;
6. apply subject and actor denies after grants; any deny rejects;
7. record the decision before dispatch.

For delegated request `R` with evaluated capability set `C` and extracted
targets `T`, authorization is the conjunction of: the actor principal and actor
credential/device scope admit exact `delegate:<c>` for every `c` in `C` and `T`;
the subject principal and subject credential/device scope admit every `c` for
`T`; the delegated-session ceilings admit `C` and `T`; and the current
operation/policy epoch admits the request. Every actor or subject deny wins. The
service does not need direct grants of `C`, which would also let it perform the
operations as itself.

Wildcard delegation capability strings are invalid at load and mutation time,
and the sealed delegation evaluator never calls the compatibility pattern
matcher. An operation whose capability is unregistered or nondelegable cannot be
delegated and fails closed. Missing principal profiles and missing policy
objects fail closed rather than resolving to a default profile.

The same effective check runs before every protected host call associated with
the request. Stream delivery rechecks route liveness, expiry and revocation. A
policy epoch on each route makes cache invalidation explicit; profile, credential
or policy mutation increments the epoch and forces re-evaluation. The policy
write and epoch increment commit atomically. Credential rotation revokes routes
and delegated sessions tied to the old `CredentialId`; the `DeviceId` remains
stable.

## Idempotency store

Mutating requests are keyed by the tuple `(SystemId, actor, subject, operation,
operation version, idempotency_key)`. The canonical request digest is
`SHA-256("astrid-control-request\0" || deterministic-CBOR(...))` over the operation
name/version, validated DTO, attachment IDs/sizes/digests, actor, subject and
host-extracted targets. The store retains the digest, request state and terminal
outcome.

The durable state machine is:

```text
accepted -> authorized -> dispatched -> committed -> terminal
```

Each mutating descriptor names the exact storage transition that constitutes
`committed`. Before that transition, cancellation or connection loss may abort
work and records `cancelled_before_commit`. After it, delivery detaches but work
must complete or recover forward into one stored terminal outcome. The RFC does
not promise exactly-once execution outside that declared mutation boundary.

A duplicate with the same digest attaches to the live request on the same
authenticated connection or returns the stored outcome after freshly
authorizing the actor, subject, credentials, targets and operation. A revoked
caller cannot retrieve an old sensitive result. A duplicate with a different
digest returns `idempotency_conflict`. Terminal entries live for at least the
operation's documented retry window and survive daemon restart for durable
mutations.

## IPC/WIT reply context

This RFC introduces `astrid:ipc@1.1.0`. Version 1.0 remains frozen. Version 1.1
adds a host-owned resource and a routed message shape conceptually equivalent to:

```wit
record request-metadata {
    system-id: string,
    request-id: string,
    application-session-id: option<string>,
    delegated-session-id: option<string>,
    actor: string,
    actor-credential-id: string,
    subject: string,
    subject-credential-id: string,
    operation: string,
    operation-version: u32,
    deadline-unix-ms: u64,
}

resource reply-context {
    metadata: func() -> request-metadata;
    forward: func(topic: string, payload: string) -> result<_, error-code>;
    emit: func(event-kind: string, payload: string) -> result<_, error-code>;
    finish: func(result-kind: string, payload: string) -> result<_, error-code>;
}

record routed-ipc-message {
    topic: string,
    payload: string,
    source-instance-id: string,
    source-package-digest: string,
    principal: principal-attribution,
    context: option<reply-context>,
}

record routed-ipc-envelope {
    messages: list<routed-ipc-message>,
    dropped: u32,
    lagged: bool,
}
```

The final WIT uses normal ownership/borrowing syntax accepted by the component
toolchain, but these semantics are normative:

- only the host creates a reply-context;
- a context is scoped to the receiving capsule instance and cannot be serialized;
- `metadata` exposes audit/correlation data but not `ConnectionId`;
- `forward` validates a registered typed edge, normal topic ACLs, payload schema
  and target preservation before giving an independently scoped child context to
  an allowed next-hop instance;
- `emit` validates the registered event kind/schema and that the current typed
  capsule instance is an allowed intermediate source;
- `finish` validates the registered result kind/schema, expected terminal source
  and completion rule before attempting terminal completion;
- fan-out produces separate child resources with the same route and distinct
  branch/source identity;
- expired, revoked, closed, wrong-source and already-finished contexts return a
  typed error;
- dropping the last live branch without completion yields `no_responder` or
  cancellation according to the operation definition.

The host stamps `CapsuleInstanceId` and signed package digest from the loaded
instance; neither comes from manifest display text or a guest payload. Each
descriptor's route policy contains typed edges:

```text
RouteEdge {
    source: CapsuleSelector,
    topic: ConcreteTopic,
    destination: CapsuleSelector,
    payload_schema: SchemaDigest,
    target_extractor: HostTargetExtractorRef,
    target_rule: Preserve | Attenuate,
    fan_out: bool,
}
```

Before forwarding, the host decodes the payload under `payload_schema`, extracts
its targets and proves they equal the current authorized target set for
`Preserve` or are a subset for `Attenuate`. A payload that substitutes a
principal, session, capsule, credential or other target is rejected before a
child context exists. The complete `RoutePolicy` also contains allowed
intermediate event sources, expected terminal source, allowed event/result
codecs, completion rule and responder restart/upgrade behavior. An in-flight
route stays pinned to the loaded package digest unless the descriptor explicitly
defines a compatible handover; otherwise it terminates
`responder_unavailable`.

IPC 1.0 messages may continue to serve legacy workflows, but new protocol-2
control operations that require routed output are delivered only through the
IPC 1.1 subscription that returns `routed-ipc-envelope` and must terminate
through reply context. A capsule importing only IPC 1.0 is never selected for a
routed operation and cannot silently consume a message while discarding its
context. Caller-provided correlation IDs cannot substitute for it.

## Error contract

Terminal control errors have a stable machine code, safe message, retryability
flag and optional bounded details object. Initial codes are:

- `authentication_required`
- `authentication_failed`
- `server_identity_mismatch`
- `protocol_mismatch`
- `contract_mismatch`
- `feature_required`
- `invalid_request`
- `unsupported_operation`
- `unsupported_operation_version`
- `invalid_deadline`
- `frame_too_large`
- `payload_too_large`
- `too_many_in_flight_requests`
- `unknown_principal`
- `unknown_credential`
- `credential_device_mismatch`
- `credential_revoked`
- `delegated_session_required`
- `delegated_session_expired`
- `delegated_session_revoked`
- `delegation_denied`
- `capability_denied`
- `target_denied`
- `deadline_exceeded`
- `quota_exhausted`
- `rate_limited`
- `idempotency_required`
- `idempotency_conflict`
- `cancelled_before_commit`
- `delivery_detached`
- `interaction_expired`
- `interaction_already_answered`
- `responder_rejected`
- `responder_unavailable`
- `no_responder`
- `backpressure`
- `audit_unavailable`
- `internal`

Authentication failures remain deliberately coarse on the unauthenticated wire.
Secrets, key material, raw internal errors, filesystem paths and policy contents
must not appear in safe messages.

Public Rust error enums are `#[non_exhaustive]` and generated clients preserve
an `Unknown { code, safe_message, details }` variant so adding a wire error does
not break exhaustive downstream matches.

## Audit contract

Every connection authentication, delegated-session change, authorization
decision, request transition, reply rejection and terminal result records:

- `SystemId`, `ConnectionId`, `RequestId` and optional application session;
- actor, actor credential/device, subject and subject credential/device;
- direct or delegated mode and delegation policy/profile digest;
- operation, every evaluated capability/predicate, target and idempotency
  request digest;
- protocol version/features, deadline, policy epoch and decision;
- responder capsule/source and reply sequence;
- cancellation, revocation or terminal reason.

Audit entries never contain private keys, bearer tokens, session tokens, raw
credential proofs or unredacted secrets.

Audit persistence is part of authorization for every delegated-session change,
every mutating authorization decision and every mutation commit transition. If
the audit record cannot be durably accepted, the operation fails closed with
`audit_unavailable`; a commit cannot occur first and be audited later. A
read-only descriptor may explicitly opt into best-effort audit only when its
result cannot disclose secrets or alter authority. Authentication failures are
recorded best-effort because an attacker must not make the listener unavailable
by exhausting audit storage; rate limits and a bounded security counter remain
mandatory.

## Legacy compatibility adapter

Authority self-healing completes before the daemon accepts either legacy or
protocol-2 connections. The native daemon is the sole accept owner and each
accepted stream has exactly one frame reader for its lifetime. The compatibility
adapter fully implements the legacy handshake and frame protocol inside that
native connection task; the native acceptor never competes with a WASM accept
loop and never hands a partially read stream back to a capsule.

An old client presenting a valid registered principal proof receives an ordinary
direct authenticated connection. An old unsigned client may complete its
historical session-token handshake for wire compatibility, but is anonymous and
limited to descriptors explicitly marked public; it never inherits the default
principal or administrator authority. A protocol-2 constructor never retries as
legacy or anonymous.

Legacy frames are translated to typed operation descriptors before dispatch.
Their responses remain bound to the accepted connection. When a legacy request
lacks a trustworthy correlation field, the adapter permits only one in-flight
request of that class on the connection; a second fails with backpressure rather
than using principal-wide routing. Legacy application session IDs remain typed
`ApplicationSessionId` data and never select an external connection.

The WASM CLI proxy remains in an upgrade fixture until signed old clients, new
clients, clean installs and packaged in-place upgrades pass the same operation
and non-owner-delivery suite. The default composition then removes the proxy; it
does not retain two live control implementations.

## Conformance and regression tests

The reference implementation must include:

- parser/property/fuzz tests for every new ID, frame and bounded field;
- transcript tests for downgrade, nonce reuse, replay, wrong audience/system,
  endpoint substitution, wrong actor, wrong credential, feature mismatch,
  signature algorithm confusion and identifier collision injection;
- two connections with the same actor proving non-owner delivery is zero;
- concurrent requests on one connection with deliberately swapped IDs/frames;
- direct and delegated authorization truth tables, including both device scopes,
  exact delegation, target policy and deny precedence;
- `principal.create` inheritance, cloning and privileged-result predicates
  independently requiring their exact conditional capability references;
- bare `*`, `delegate:*` and `delegate:self:*` attempts proving none can satisfy
  the sealed delegation evaluator;
- missing/unknown/disabled principals and credentials failing closed;
- revocation and key rotation before dispatch and during streams;
- idempotent mutation restart/replay/conflict tests, including connection loss
  immediately before and after the declared commit point and replay after
  credential rotation;
- batched and asynchronous capsule replies using distinct reply contexts;
- malicious capsule attempts to reuse, serialize, retarget or finish another
  capsule's context;
- leaked/dropped branch, unauthorized intermediate hop, mixed IPC 1.0/1.1 graph
  and wrong event/result codec tests;
- an otherwise allowed intermediate edge attempting to substitute each target
  kind in a schema-valid payload;
- responder-source substitution and competing well-shaped response tests;
- responder restart and upgrade during an in-flight request;
- slow-client, zero-credit, credit-overflow, queue saturation, wrong-owner or
  duplicate/out-of-order/excess attachment chunks, upload-timeout cleanup,
  attachment truncation/digest and maximum-frame tests;
- delegated-session open replay, refresh, evidence time/skew/expiry retention,
  session expiry intersection, connection binding and external-auth-event
  single-use tests;
- interaction replay, wrong connection/subject and deadline tests;
- audit/database disk-full before authorization and at every mutation transition;
- clock skew, wall-clock rollback and operation-version compatibility vectors;
- signed capability-registry/operation-manifest digest mismatch and same
  operation name/version with a different descriptor digest;
- golden operation-manifest vectors proving every descriptor capability/conditional
  digest exists, delegability is derived from the registry, and no operation is
  reachable through an internal enum or raw topic;
- a generic consumer-coverage tool that verifies a downstream command/route map
  against manifest operations and explicitly classified consumer-owned actions;
- local and simulated remote `RuntimeTransport` conformance;
- legacy-client compatibility and explicit no-downgrade tests;
- real daemon, signed capsule and packaged CLI end-to-end tests on supported
  macOS and Linux targets.

## Required implementation order

The implementation sequence is normative because later surfaces consume the
earlier contracts:

1. land the content-bound registry, boot self-healing, bootstrap/enrollment and
   old-binary guard behind a nonactivated authority gate;
2. check in every exact operation descriptor/DTO and the generic downstream
   coverage schema/tool;
3. implement native direct protocol 2 plus its single-owner legacy adapter;
4. add IPC 1.1 reply contexts and migrate every capsule selected by a routed
   operation;
5. move the native Astrid and distribution CLIs to `RuntimeTransport`;
6. provision generic services, delegated credentials/sessions and interactions;
7. require each distribution's own route matrix, then cut every runtime-backed
   product HTTP route to the same descriptors;
8. retire the WASM socket proxy and old gateway wiring only after packaged
   compatibility, concurrency, fault-injection and route-parity gates pass.

Transport, CLI and HTTP code cannot freeze a provisional raw operation before
step 2, and authority migration cannot activate before the full conformance suite
for steps 1 through 4 is green.

# Drawbacks
[drawbacks]: #drawbacks

- Moving socket service into the native daemon increases native code and expands
  the kernel-adjacent transport TCB.
- The IPC 1.1 resource requires SDK, WIT mirror and capsule migrations.
- Exact operation-scoped delegation is more configuration than one universal
  service grant.
- Persistent idempotency and midstream revocation add state and failure modes.
- Legacy compatibility temporarily means two protocol paths in one listener.

These costs buy a connection-safe and distribution-neutral boundary. Leaving
routing in a privileged guest or trusting caller JSON would make every product
adapter repeat the same security work.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why native socket ownership?** The daemon already binds the socket and owns the
event bus, capability engine, audit log and runtime lifetime. It is the only
component that can route a reply without trusting a guest to name or filter the
external recipient. Transport demultiplexing is mechanism, not capsule business
logic.

**Why not keep the WASM proxy and add a connection ID?** A guest that can write
every accepted socket remains able to misroute privileged bytes. Carrying an ID
through WIT improves observability but does not make the final write host-owned.
It also leaves batching and asynchronous reply context ambiguous.

**Why not create a trusted product edge?** Astrid must not know which
distribution is installed. A product-specific role or router would be equivalent
to a vendor bypass and would prevent another distribution from using the same
runtime contract.

**Why not give the service the subject's private key?** It destroys credential
separation, makes attribution false, and lets compromise outlive a service
session. Delegation records both parties and can be independently revoked.

**Why not use one universal delegation capability?** It creates a confused-deputy
root. Exact `delegate:C` grants allow a service to expose only the operations it
is designed to mediate and do not automatically expand with future capabilities.

**Why a reply-context resource instead of JSON correlation?** Correlation IDs are
client-controlled data. A host resource is unforgeable, can be scoped to one
capsule/source, carries revocation state, and survives batched processing without
relying on one mutable invocation-global caller slot.

**Why preserve the existing socket path?** Stable local identifiers and upgrade
compatibility matter. Protocol negotiation allows implementation replacement
without churning installers, service units and already published clients.

# Prior art
[prior-art]: #prior-art

- Linux separates process credentials from filesystem/network APIs and checks
  credentials at protected operations rather than asking a distribution name.
- OAuth 2.0 Token Exchange distinguishes the subject from the actor and preserves
  actor identity in delegated tokens: <https://www.rfc-editor.org/rfc/rfc8693>.
- OAuth DPoP binds proof to a sender and request context to reduce replay:
  <https://www.rfc-editor.org/rfc/rfc9449>.
- SPIFFE distinguishes workload identity from location and uses a local Workload
  API plus short-lived verifiable identities:
  <https://spiffe.io/docs/latest/spiffe-specs/spiffe_workload_api/>.
- Capability-oriented systems use unforgeable handles to couple possession and
  authority. WIT resources provide the right local representation for an opaque
  reply route.

These are design references, not wire compatibility claims.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- The exact retention window and storage backend for per-operation idempotency
  results must be selected during implementation, with a documented lower bound.
- The final WIT ownership spelling for reply contexts must be validated against
  the pinned component toolchain without weakening the resource semantics.
- The legacy protocol support window and removal signal require release-policy
  agreement; protocol 2 must not depend on that schedule.
- Remote TLS transport, device pairing UX and fleet routing are out of scope, but
  must pass the same transport conformance contract when added.

# Future possibilities
[future-possibilities]: #future-possibilities

- A mutually authenticated remote transport using the same operation and request
  context model.
- Hardware-backed service and device credentials.
- Federated workload identities mapped to ordinary Astrid service principals.
- Cross-system request forwarding that replaces `ConnectionId` at each hop while
  retaining the original `SystemId`/`RequestId` chain in audit.
- Generated SDK clients and conformance vectors from the typed operation catalog.
