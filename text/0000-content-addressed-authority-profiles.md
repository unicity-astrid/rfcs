- Feature Name: `content_addressed_authority_profiles`
- Start Date: 2026-07-14
- RFC PR: [rfcs#0000](https://github.com/astrid-runtime/rfcs/pull/0000)
- Tracking Issue: [astrid#1228](https://github.com/astrid-runtime/astrid/issues/1228)

# Summary
[summary]: #summary

Astrid will replace wildcard administrative authority with immutable,
content-addressed authority profiles composed only of exact registered
capabilities. The administrator profile has a stable name and a frozen reviewed
revision identified by its content digest; capabilities added later require an
explicit profile update.

Every enforcement capability, including current hard-coded wildcard bypasses,
will be represented in one authoritative registry. Every path that creates or
assigns privileged authority will require an explicit profile-management
capability. Existing wildcard state in profiles, groups, devices, pair tokens and
invitations will be converted by a journaled, crash-recoverable migration.

Fresh bootstrap becomes atomic and fail-closed. Missing or malformed identity no
longer falls back to the default administrator. A last-enabled-administrator
invariant and narrow offline recovery flow preserve complete system control
without keeping an online ambient root credential.

# Motivation
[motivation]: #motivation

Astrid's static capability grammar currently permits wildcard patterns. Bare `*`
matches every capability string, including capabilities introduced by a future
runtime or extension. The built-in `admin` group is defined as `*`, and the
default principal is seeded into that group. A full-scope default credential
therefore has ambient authority over present and future operations.

The problem is broader than one built-in constant. Wildcards can persist in:

- direct principal grants and revokes;
- custom groups accepted with `unsafe_admin` acknowledgement;
- device scopes and legacy full-scope device records;
- pair tokens and outstanding invitations;
- cloned or newly created privileged principals.

Bare `*` also activates enforcement shortcuts that do not appear in the current
45-entry management catalog: unbounded resources, network binding, uplink
operation, invocation of any capsule, global capsule inventory, administrator
clone tests, and gateway audit/firehose access. A catalog-only replacement would
silently remove required control or leave bypasses behind.

Several privilege-creation paths treat `unsafe_admin` or `allow_admin_clone` as
acknowledgement rather than independent authorization. A caller able to create or
modify an agent may be able to assign `admin`/`*` without a second exact
capability for changing the authority boundary.

Finally, existing persistence is atomic only per file. Profiles, groups, devices,
tokens and invitations do not share a transaction or migration journal. A crash
during a naive rewrite can leave mixed policy semantics and make recovery less
safe than the original state.

The expected outcome is administrator convenience with explicit, inspectable and
upgrade-stable authority. A system owner can still install, repair, configure,
recover and control the complete system. Ordinary services and routine admin
sessions do not carry a credential that silently acquires tomorrow's powers.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## Immutable authority profiles

An authority profile is a named, content-addressed set of exact capability IDs:

```text
AuthorityProfileRef {
    id: "administrator",
    digest: sha256(canonical profile definition)
}
```

The stable name `administrator` is not a mutable role. Its installed revision is
the definition with a particular digest, and that definition never changes. A
new runtime capability is not added to an existing revision. A later release can
publish a new reviewed digest under the same stable name; an operator reviews the
capability diff and explicitly updates selected principals, groups or
credentials.

The friendly `admin` group resolves to a digest-pinned profile reference, not to
a pattern. A principal assigned to `admin` on an upgraded system remains on the
same digest until an authorized update.

Authority profiles are not bearer tokens. The ordinary principal and device
credential still authenticate each session, and revokes/device scope continue to
win over profile grants.

## Complete capability registration

Every capability checked by runtime code must have one registry entry. The
registry includes management requests, resource exemptions and non-request
enforcement. The first administrator profile includes the exact capabilities
required for full current administration, including explicit replacements for
today's wildcard-only bypasses.

In particular:

- `capsule:access:any` replaces the ability to invoke any capsule because of `*`;
- `authority:profile:manage` controls assignment, cloning and mutation of
  privileged authority profiles;
- resource exemption, socket binding and uplink capabilities become registered
  exact IDs rather than invisible additions to administrator authority.

Future extensions register exact capabilities before enforcement uses them. An
unregistered check is a build/test failure in-tree and a fail-closed runtime
error for a dynamic extension.

## Assigning privileged authority

Creating an agent, modifying a group, issuing an invitation or cloning a profile
still requires its normal operation capability. If the result would add,
remove, clone or delegate a privileged profile/capability, the caller must also
hold `authority:profile:manage`, subject to the caller's credential scope and
revokes.

`unsafe_admin` and `allow_admin_clone` may remain temporarily as explicit human
confirmations for compatibility. They never authorize an operation.

The kernel prevents removal or disablement of the last enabled administrator
with a live recovery path. This invariant applies across principal deletion,
disable, group/profile removal, direct revoke and device revocation.

## Updating an administrator profile

The runtime shows a deterministic diff between the pinned profile and the target
digest: added/removed exact capabilities and danger classification. The operator
explicitly approves the target digest with a credential that holds
profile-management authority. The audit log records both digests and the
approving device.

No daemon or distribution update automatically changes a principal's pinned
profile digest.

## Legacy migration

Before enabling wildcard-free policy semantics, Astrid inventories every
authority-bearing store under an exclusive migration lock. It creates
mode-preserving backups, a journal and canonical hashes.

Each legacy wildcard grant is expanded against the complete capability registry
at the migration registry digest. Capabilities from installed extensions are
placed in a host-specific immutable migration-snapshot profile so current control
is preserved without granting future extensions. A wildcard that cannot be
expanded deterministically blocks migration and produces a report; it is never
silently dropped or retained.

Bare wildcard deny becomes an explicit deny-all state. Other wildcard revokes
and grants are expanded to their exact current matches. New-format authority
stores contain no wildcard patterns.

Outstanding privileged invitations are revoked by default. An operator may
explicitly pin one to an immutable profile and digest before migration completes.

The migration stages every output, fsyncs files and parent directories, then
commits the journal atomically. On interruption, the next boot deterministically
completes or restores byte-identical backups. A current binary recognizes old
authority data only to self-heal it before accepting a control connection; it
never exposes a selectable old-policy mode and never writes wildcard authority.

## Bootstrap and recovery

A fresh installation atomically creates the bootstrap principal, its credential
and a pinned administrator profile. Failure to create or persist any member
aborts startup. The daemon does not log and continue with a partial identity.

An absent, malformed, unknown or disabled principal is anonymous/denied. It never
resolves to the default administrator. Legacy local compatibility, while present,
must be separately authenticated, visible in audit and explicitly sunsetted.

Recovery is offline and narrow. With the daemon stopped, a locally present
recovery credential can restore or rotate administrator profile assignment and
administrator credentials. It cannot invoke capsules, read arbitrary audit data,
run ordinary agent work or become a network service credential. Recovery writes
the same journaled policy format and produces a signed recovery audit record on
next boot.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Authority profile schema

The canonical logical schema is:

```text
AuthorityProfileDefinition {
    id: AuthorityProfileId,
    capabilities: sorted set<CapabilityRef>,
    created_by_release: string,
    digest_algorithm: "sha256",
    digest: 32 bytes,
}

CapabilityRef {
    id: ExactCapabilityId,
    entry_digest: 32 bytes,
}

AuthorityProfileRef {
    id: AuthorityProfileId,
    digest: 32 bytes,
}
```

Profile IDs follow the existing bounded static identifier grammar without `*`.
Capability entries are concrete validated IDs and may not contain any wildcard
segment. The canonical digest is:

```text
SHA-256("astrid-authority-profile\0" || deterministic-CBOR(
    id, created_by_release, sorted capability ID + entry-digest bindings
))
```

Descriptions, UI labels, danger presentation and release prose are excluded from
the authority digest. Every authorization-relevant capability semantic is covered
by the bound entry digest. Adding a new registry entry does not invalidate an old
profile; changing the meaning of an entry referenced by that profile does.

Definitions are immutable and stored by `(id, digest)`. Multiple reviewed
revisions may share a stable ID, but the bytes at a given digest can never
change. A reference whose digest is absent or does not match the canonical
definition fails closed.

## Registered capability schema

The authoritative registry extends the current catalog entry with:

```text
RegisteredCapability {
    id: ExactCapabilityId,
    scope: enum,
    target_kinds: sorted set<TargetKind>,
    danger: enum,
    delegable: bool,
    privileged: bool,
    source: Kernel | SignedExtension(PackageDigest),
    entry_digest: 32 bytes,
}

CapabilityRegistryManifest {
    schema_revision: nonzero u32,
    entries: sorted set<RegisteredCapability>,
    digest_algorithm: "sha256",
    digest: 32 bytes,
}
```

`entry_digest` uses domain-separated deterministic CBOR over the ID, scope,
target kinds, delegability, privileged flag and source identity.
The manifest digest covers the sorted entry encodings. A materially new action,
target class, resource exemption or delegation meaning requires a new capability
ID. Authorization-relevant meaning under an existing ID is immutable; a release
that presents the same ID with a different entry digest fails closed.

Every production call to the static authority evaluator accepts a typed
`CapabilityRef { id, entry_digest }`, not an arbitrary string or bare ID. Raw
strings exist only at the legacy parser/migration boundary and bind to the
current registry entry digest before evaluation. Registry drift tests enumerate
all request tables, resource exemption checks, capsule access checks, inventory
gates, audit/event gates and authority mutation paths. In-tree unregistered
checks fail CI. Runtime extension
checks whose signed extension definition is absent or invalid are denied.

For each capability with `delegable = true`, the registry generator emits a
separate exact `delegate:<capability-id>` entry. This authority-registry
generator is the sole declaration owner for all derived delegation entries and
for `delegate:session:open`; the control manifest references but does not redeclare
them. Every derived entry and `delegate:session:open` has `privileged = true` and
`delegable = false`. The sealed delegation evaluator accepts only these typed
derived IDs by exact membership: legacy `*`, `delegate:*` and scoped patterns
can never satisfy it. Profile management, recovery, credential issuance,
service provisioning and any capability explicitly marked nondelegable have no
derived delegation entry.

The migration-baseline capability manifest is generated once from the reviewed
current enforcement inventory. It includes all current management capabilities,
registered resource/uplink capabilities, `capsule:access:any`,
`authority:profile:manage` and the online repair capability. It does not include
arbitrary signed extension capabilities.

The normative migration-baseline manifest contains these 51 IDs, sorted
canonically when hashed:

```text
system:shutdown
system:status
capsule:install
self:capsule:install
capsule:reload
self:capsule:reload
capsule:remove
self:capsule:remove
self:workspace:promote
self:workspace:rollback
capsule:list
self:capsule:list
agent:create
agent:create:inherit
agent:create:clone
agent:delete
agent:enable
agent:disable
agent:modify
agent:list
self:agent:list
quota:set
self:quota:set
quota:get
self:quota:get
group:create
group:delete
group:modify
group:list
self:group:list
caps:grant
caps:revoke
caps:token:mint
caps:token:revoke
caps:token:list
invite:issue
invite:redeem
invite:list
invite:revoke
audit:read_all
self:approval:respond
self:auth:pair
self:auth:pair:admin
auth:pair:redeem
auth:pair
system:resources:unbounded
net_bind
uplink
capsule:access:any
authority:profile:manage
authority:repair
```

One initially activated `administrator` profile is generated from the union of
this baseline and the companion control-operation capability manifest, excluding
`delegate:session:open` and generated `delegate:C` service grants. That final
union has one immutable golden profile digest. It preserves complete kernel
administration without including offline recovery authority or a
distribution/service delegation grant. Exact delegation companions required to
preserve an actually enforced pre-migration grant are carried by the migration
snapshot, not inferred from future entries.

## Effective authority

A principal's effective authority is calculated from content-bound direct
grants, immutable profile references and content-bound group/profile membership.
Revokes are applied last and always win. Grants are exact, while revocation uses
typed negative selectors:

```text
DenySelector = Exact(CapabilityRef)
             | Namespace(CapabilityNamespace)
             | All
```

`Namespace` is a validated colon-segment prefix and is restrictive only; it can
never grant authority. This preserves the future-deny intent of legacy revokes
such as `self:*` without allowing future grants to expand.

`DeviceScope::Full` means the device may exercise the exact effective authority
of the principal's currently pinned profile digests and direct grants. It does
not mean all capability strings and does not follow an unassigned newer profile
revision.

The existing device-scope model self-heals to this wildcard-free representation:

```text
DeviceScope = Full
            | Scoped {
                  allow_capabilities: set<CapabilityRef>,
                  allow_profiles: set<AuthorityProfileRef>,
                  deny: set<DenySelector>,
              }
```

The allowed capability/profile set attenuates the principal; it never grants
authority the principal lacks. The stable `use-only` preset contains the exact
current safe self-service allow set and deny selectors for `self:auth:pair`,
`self:auth:pair:admin` and the `delegate:self` namespace.

```text
self:capsule:install
self:capsule:reload
self:capsule:remove
self:workspace:promote
self:workspace:rollback
self:capsule:list
self:agent:list
self:quota:set
self:quota:get
self:group:list
self:approval:respond
self:authority:read
self:device:list
self:credential:list
self:capsule:inspect
self:capsule:config:read
self:capsule:config:write
self:audit:read
self:agent:prompt
self:agent:requests:read
self:interaction:respond
self:session:list
self:session:read
self:session:modify
self:session:delete
self:model:list
self:model:active:read
self:model:active:write
```

Every persisted positive authority edge—direct grants, group capabilities,
profile contents, device allows, preserved pair-token allows and invitation
authority—contains a `CapabilityRef` or `AuthorityProfileRef`, never a bare
capability ID. Bare and scoped wildcard grants are rejected. Exact revokes also
bind an entry digest; namespace/all denies remain restrictive selectors.

Manifest IPC topic patterns and capability-token resource patterns are separate
namespaces and are not changed by this RFC.

## Privileged authority mutation

An operation is authority-privileged if it can:

- assign/remove an authority profile;
- grant/revoke a capability marked `privileged`;
- add/remove membership in a group containing a privileged profile/capability;
- clone a principal containing privileged authority;
- create an invitation or pair token that conveys privileged authority;
- publish a new authority profile revision or update an alias;
- expand or update a pinned profile digest.

The kernel must require both the operation's existing capability and
`authority:profile:manage`. This is enforced inside the same write lock and
transaction that persists the mutation, after device attenuation and revoke
evaluation. Preflight UI confirmation is not enforcement.

An existing `(id, digest)` definition can never be modified. Changes publish a
new content-addressed revision. Every referenced `administrator` revision remains
loadable. Aliases may be changed only through an audited authority transaction.
A definition cannot be removed while referenced by a principal, group, device,
token, invitation, rollback generation or audit record.

## Migration inventory

The migrator inventories and hashes at least:

- built-in and custom groups;
- every principal profile grant, revoke and profile/group reference;
- every device scope and credential record;
- outstanding pair tokens;
- outstanding invitations;
- installed signed extension capability definitions;
- bootstrap/default principal state and credential records.

It records source schema versions, paths/keys, modes, hashes and expansion
results in a private journal. Secret/token values are not copied into logs.

## Wildcard conversion

For each legacy policy pattern `P`:

1. validate `P` with the legacy grammar;
2. enumerate concrete registered capabilities matching `P` at the migration
   registry digest;
3. include matching installed extension capabilities in an immutable
   `migration-snapshot-<host-digest>-<pattern-digest>` definition pinned by digest;
4. convert grant matches to sorted `CapabilityRef` values bound to the migration
   registry's entry digests or to digest-pinned profile references;
5. convert revokes to `Exact`, `Namespace` or `All` deny selectors without
   weakening future restriction;
6. if `P` has no deterministic finite expansion, stop before committing and
   report the owning object and pattern.

Every legacy wildcard-bearing subject receives the exact matching kernel and
installed-extension snapshot. Direct `grants = ["*"]`, the built-in `admin`
group and custom universal-wildcard groups receive the final activated `administrator`
revision plus the complete host migration-snapshot profile. Scoped patterns
receive only their matching kernel/extension subset. New extensions installed
later enter neither profile.

The migrated host-local `admin` alias pins both the final activated
`administrator` digest and the complete migration snapshot. Fresh
installations point `admin` only to that final `administrator` digest. Custom
unsafe groups receive the appropriate profile references and retain their other
exact grants and deny selectors.

The self-healed built-in `agent` base allow set is the exact current expansion
of `self:*`:

```text
self:capsule:install
self:capsule:reload
self:capsule:remove
self:workspace:promote
self:workspace:rollback
self:capsule:list
self:agent:list
self:quota:set
self:quota:get
self:group:list
self:approval:respond
self:authority:read
self:device:list
self:device:revoke
self:credential:list
self:credential:rotate
self:credential:revoke
self:capsule:inspect
self:capsule:config:read
self:capsule:config:write
self:audit:read
self:agent:prompt
self:agent:requests:read
self:interaction:respond
self:session:list
self:session:read
self:session:modify
self:session:delete
self:model:list
self:model:active:read
self:model:active:write
self:delegation:policy:manage
self:auth:pair
self:auth:pair:admin
```

Its legacy `delegate:self:*` entry is reserved policy state that has no current
enforcement site. Migration expands it only to exact derived delegation entries
that are registered and marked delegable at the migration registry digest. If no
such entry exists, the expansion is empty and the migration report records that
no active authority was removed. It never grants future delegation companions.

Active device credentials are migrated in place and are not revoked. `Full`
retains the pinned-principal intersection semantics above. Scoped allows expand
to exact current capabilities/profile references, and revokes become typed deny
selectors. The `use-only` preset becomes an immutable exact scope pinned by
digest.

Outstanding pair tokens are distinct short-lived records. They are revoked by
default; explicit preservation rewrites their scope to exact references plus a
scope digest. Outstanding invitations are separately resolved through their
target group's post-migration authority. Privileged invitations are revoked by
default or explicitly pinned to that exact group/profile digest.

## Journal and crash recovery

Migration is boot-only. No authority store is made live and no control connection
is accepted until the selected generation is complete and verified. The phases
are:

1. `inventory`: acquire the exclusive migration lock and write/fsync inventory;
2. `backup`: write mode-preserving backups and fsync files/directories;
3. `stage`: write all new-schema objects to non-active paths and fsync;
4. `verify`: reload staged data, verify hashes/digests and run invariants;
5. `guard`: first replace and fsync the retired `etc/groups.toml` with a
   mandatory poison object whose known `groups` field is an incompatible scalar,
   then install/fsync every other fixed-path poison object;
6. `commit`: fsync the forward-only record naming the staged generation;
7. `activate`: atomically replace the active generation pointer with the fully
   verified generation directory and fsync its parent;
8. `cleanup`: retain a documented rollback backup, then mark complete.

Before the commit record, current recovery always restores the original fixed
paths and rolls back. After the commit record, recovery always completes
forward. It never chooses nondeterministically. Mixed fixed-path files may exist
only as old-reader poison objects while the daemon is
boot-locked and are never loaded as current authority. Staging/backup directories
are `0700`; secret-bearing files are `0600`; ownership and modes are preserved;
every file and parent directory is fsynced. Journals contain hashes and token
identifiers, never raw token or private-key material.

The committed generation contains a rollback watermark. Byte-identical
pre-migration rollback is allowed only before accepting any control connection
and before the first current-format authority mutation. After that mutation,
normal old-binary rollback is refused. An explicit disaster-recovery override
must warn that it can resurrect credentials/tokens and require mandatory
rotation/revocation during the next migration.

## Bootstrap transaction

Fresh bootstrap writes principal identity, active credential and
the reviewed `administrator` digest through one logical transaction/journal.
Startup continues only after reloading and verifying all three. An existing
installation with partial bootstrap state fails with a recovery instruction
instead of silently reseeding authority.

The bootstrap client or local-console installer generates the device key through
an injected credential provider. The transaction receives only the public key,
`DeviceId` and `CredentialId`; the private key stays in the platform keychain,
secure enclave, TPM, hardware authenticator or owner-only fallback selected by
the client. The daemon never generates an exportable shared administrator key for
ordinary clients or services.

## Last-administrator invariant

Astrid exposes one narrow online `administrator.repair` transaction. It may
atomically create or re-enable one principal, register a proof-of-possession
credential for it and assign one already installed, reviewed `administrator`
digest. It cannot assign arbitrary profiles, invoke capsules or change unrelated
policy. The caller must hold both content-bound `authority:repair` and
`authority:profile:manage`; the operation is nondelegable and its ordinary and
conditional checks run under the authority write lock.

Before committing a mutation that can reduce administrative access, the kernel
simulates the resulting state and proves at least one enabled principal has:

- a valid pinned administrator profile;
- a non-expired, non-revoked credential whose actual device scope admits
  both `authority:repair` and `authority:profile:manage` after group/profile
  resolution;
- no effective deny selector blocking either capability;
- a complete executable path through the `administrator.repair` operation.

The check runs under the same global authority write lock and covers delete,
disable, group membership removal, group edit/deletion affecting members,
profile/alias update, direct revoke, active-device revoke/rotation and profile
garbage collection. Multi-operation transactions evaluate the final state
atomically. Offline recovery does not count as the remaining online administrator
unless the operator explicitly enters break-glass recovery.

## Offline recovery

Recovery requires the daemon singleton lock to be exclusively held by the
recovery tool. The tool accepts only recovery operations:

- inspect authority schema/journal health without secret output;
- restore the last verified policy backup;
- assign a known installed administrator profile to an existing/new recovery
  principal;
- register or rotate that principal's credential;
- complete or roll back an interrupted migration.

The installer generates the recovery key outside the daemon through its
credential provider and proves possession during bootstrap. Bootstrap stores
only the recovery public-key trust anchor outside routine authority generations.
Recovery private material never enters daemon memory, authority generations,
backups, journals or audit records. Hardware backing is optional; separation
from routine keys and owner-only custody are mandatory. A missing or lost key
fails closed and is never silently regenerated, substituted or replaced.

Recovery cannot call the runtime event bus or capsule interfaces. Each recovery
operation is signed over the previous policy digest, target generation,
monotonic sequence and fresh nonce. Stale-base, duplicate and replayed records
are rejected.

The separate recovery trust-anchor state stores the public key, an accepted
sequence high-water mark, the last consumed signed-record digest and any pending
reserved record. It is outside authority generations and rollback backups. Before
applying a recovery mutation, the tool records `(sequence, record digest)` as
pending, advances the high-water mark and fsyncs that state. It then applies the
exact signed mutation and marks the record complete. A crash with a pending
record completes that same record forward; it never accepts the caller's record
again. Policy restore, migration rollback and disaster recovery never decrement
or replace the high-water mark.

The recovery tool signs a bounded deferred audit record with that recovery key.
The next boot verifies the trust anchor, signature, sequence and resulting policy
digest before importing the record into the audit chain. Restoring
pre-migration state forces self-healing migration again before accepting a
control connection.

## Compatibility

The loader reads current authority data directly. When it encounters the
pre-migration wildcard format, startup self-heals it under the boot lock before
accepting any control connection. All successful writes use only the current
wildcard-free representation. There is no runtime switch, dual-write period or
selectable old evaluator. `unsafe_admin` and `allow_admin_clone` retain their
existing wire fields and serde defaults as confirmation flags, never authority.

A legacy wildcard grant/group request with the required confirmation maps to the
pinned migrated `admin` alias and persists no wildcard. Legacy agent create,
modify, clone, group and direct-grant operations that would convey privileged
authority additionally require the caller's attenuated
`authority:profile:manage`. `AgentCreate` direct grants receive the same check
even though the old request has no wildcard acknowledgement, and the response
contains a compatibility warning.

Legacy bare `*` never means the latest administrator profile. The compatibility
adapter preserves the existing strict response shape for an old client; it does
not serialize new profile-reference fields unless the negotiated control feature
admits them. Current typed summaries may add optional profile references with
serde defaults, while existing fields remain readable. New authority-profile
operations use additive request variants instead of changing existing enum tags.
CLI deprecation text, HTTP mapping, response schemas and OpenAPI documents have
golden compatibility tests.

Gateway audit/firehose authorization must call the kernel-side evaluator with
the authenticated principal and device/credential context. It must not infer
authority from the `admin` group name or serialized direct grants. Capsule
invocation uses `capsule:access:any`; global inventory uses `capsule:list`; clone
classification evaluates effective privileged profile membership and never
tests `.has("*")`.

Rollback to a pre-migration binary follows the watermark rule above and is an
explicit offline operation. A current runtime never writes wildcard authority
for compatibility, and an older binary must fail closed on the current data
format. This does not rely on unknown-field rejection. Before activation, the
migrator writes a mandatory boot-loaded poison guard at a fixed authority path
that the previous released daemon necessarily parses before binding
`system.sock`; the guard changes a known field to an old-reader-incompatible
type. Current binaries verify the guard and then resolve all authority stores
through the generation pointer.

Every retired fixed-path store also receives a format-specific poison object.
For pair-token and invitation wrappers that ignore unknown fields, the known
`pair_token`/`invite` value is deliberately an incompatible scalar rather than
the list their old deserializers require. The migrator installs and fsyncs the
mandatory boot guard and every other poison object before the forward-only
commit record and before the active generation pointer change. Pre-commit
rollback restores the original fixed paths; post-commit recovery completes
forward. CI boots the previous released binary against the complete migrated
fixture and proves it exits before binding the socket, in addition to exercising
each actual old store deserializer.

## Required tests

- golden migration-baseline 51-ID list and registry-manifest digest;
- golden capability-registry manifest and entry digests for all 51 base entries
  plus the companion control-operation entries;
- one golden final `administrator` digest plus final `agent` and `use-only`
  digests after unioning the companion control manifest, with no implicit
  service delegation grant;
- a synthetic future capability denied to an administrator pinned to the
  final activated digest;
- registry drift tests covering every enforcement site and request table;
- a same-ID/different-entry-digest registry entry rejected against an old direct
  grant, group, profile, device scope, pair token and exact deny;
- load rejection for wildcard grants in every new-schema field;
- revokes overriding direct grants, group refs, administrator and migration-snapshot
  profiles;
- exact built-in `agent` and `use-only` replacement tests;
- migration fixtures for direct grants/revokes, scoped patterns including
  `delegate:self:*`, custom groups, active devices, outstanding pair tokens,
  invitations, admin clone and installed extension capabilities;
- extension installed before versus after migration;
- built-in admin/custom wildcard groups retain pre-migration extension authority,
  while post-migration extensions remain denied;
- `DeviceScope::Full` follows pinned digests but not future unassigned revisions;
- unknown/unexpandable wildcard blocking before commit;
- fault injection at every journal write, fsync, rename and phase transition;
- byte-identical rollback with permissions preserved;
- privilege-escalation rejection on create, modify, group, grant, invite, pair
  and clone paths without `authority:profile:manage`;
- device attenuation and revoke precedence on profile management;
- explicit capsule-access and audit/event authorization replacing wildcard
  branches;
- legacy socket/HTTP request deserialization, response, CLI deprecation and
  OpenAPI golden compatibility;
- gateway firehose checks through custom profiles/groups with device attenuation
  and deny precedence;
- atomic bootstrap failure at every persistence step;
- missing/malformed/unknown principal denied without default fallback;
- group edits/alias changes and every destructive path preserve the exact
  last-administrator predicate;
- a last credential scoped only to `authority:profile:manage` cannot satisfy the
  repair predicate and causes the destructive mutation to be rejected;
- rollback rejected after the first current-format authority mutation;
- recovery tamper, replay, stale-base-digest, nonce/sequence and daemon-lock tests;
- accept recovery record N, restore an older policy backup, then prove N and
  every lower sequence remain rejected while a pending N completes only forward;
- recovery proof-of-possession, substituted/missing anchor, lost-key fail-closed
  and private-material-absence tests across memory, backups, journals and audit;
- offline recovery command allowlist and proof it cannot invoke runtime work;
- complete legacy-home migration and rollback end-to-end tests.
- previous-released-binary boot against the migrated generation proving it exits
  before binding `system.sock`, plus actual old profile/group/device/pair-token
  and invitation deserializers rejecting their poison fixtures;
- previous-binary launch after every guard write, commit-record write and
  generation-pointer fault-injection point;

# Drawbacks
[drawbacks]: #drawbacks

- Capability registration and immutable profiles add maintenance work whenever
  enforcement changes.
- Administrators must explicitly review profile updates to gain new powers.
- The migration is substantially more complex than replacing `*` in one group.
- Exact expansion can produce larger stored policy sets.
- Last-administrator protection and offline recovery add transaction paths that
  require extensive fault testing.

These are acceptable costs for preventing an ordinary upgrade or service
compromise from silently expanding authority.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why immutable content-addressed profiles?** A mutable `admin` role is still
ambient future authority. Pinning a stable ID to an immutable digest makes
updates explicit and auditable while preserving an ergonomic administrator
assignment.

**Why not retain `*` only for humans?** Credentials are compromised and services
are misclassified. The semantic flaw is future automatic expansion, regardless
of who currently holds the pattern.

**Why exact capabilities instead of scoped patterns such as `self:*`?** A pattern
can inherit a future capability in its namespace. Exact stored authority makes
the review boundary deterministic. IPC topic patterns and resource-token globs
remain separate, purpose-built namespaces.

**Why a complete registry?** Unregistered enforcement is invisible to profile
review and migration. The current wildcard-only bypasses demonstrate that a
partial catalog cannot prove completeness.

**Why a separate profile-management capability?** `agent:create` should not imply
authority to manufacture a new administrator. Combining the normal operation
check with `authority:profile:manage` prevents privilege-bearing parameters from
changing the meaning of a lower-risk command.

**Why journal the migration?** Multiple files and stores participate in effective
authority. Per-file atomic rename cannot prevent a crash from activating a mixed
policy generation.

**Why offline recovery instead of an online root key?** Recovery must exist, but
an online universal credential recreates the original risk. Offline custody,
narrow operations and daemon exclusion preserve recoverability without making
the HTTP/service plane omnipotent.

# Prior art
[prior-art]: #prior-art

- Linux capabilities split traditional superuser authority into named units,
  while its documentation warns that broad catch-all capabilities recreate root:
  <https://man7.org/linux/man-pages/man7/capabilities.7.html>.
- Kubernetes RBAC roles are explicit rule sets and support separate role binding;
  its documentation also warns that wildcards automatically grant future
  resources and verbs, while aggregate roles illustrate why this RFC pins
  content digests: <https://kubernetes.io/docs/reference/access-authn-authz/rbac/>.
- Cloud IAM managed-policy versions and permission boundaries separate a
  convenient policy bundle from the effective intersection at use time.
- The Update Framework separates trusted metadata versions and rollback checks;
  this RFC applies similar explicit version/digest discipline to authority data.

These are conceptual references. Astrid's profile and migration formats remain
Astrid-owned.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- The 51 base IDs are normative above. Their entry/registry digests, the
  companion control entries, generated delegation entries and the single final
  `administrator` profile digest must be attached as reproducible golden vectors
  before acceptance.
- The backup retention period and operator-visible cleanup command need release
  policy, without changing the crash-safety requirements.
- Hardware-backed recovery integration is platform-specific. The software
  recovery format must remain portable and fail closed when a provider is absent.
- Dynamic extension capability registration needs a signed manifest schema; the
  registry and migration semantics are fixed here, while distribution mechanics
  may be specified separately.

# Future possibilities
[future-possibilities]: #future-possibilities

- Organization-defined immutable authority profiles signed by an enterprise
  policy authority.
- Multi-party approval for upgrades to selected extreme capabilities.
- Time-bounded profile assignments and just-in-time administrative sessions.
- Hardware-attested recovery and administrator credentials.
- Generated UI/profile diffs directly from the capability registry.
