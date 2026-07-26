- Feature Name: `capsule_catalog`
- Start Date: 2026-07-26
- RFC PR: [rfcs#0000](https://github.com/astrid-runtime/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/astrid-runtime/astrid/issues/0000)

# Summary
[summary]: #summary

Define the **capsule catalog**: a signed, statically-served index that maps capsule
names to verified releases, and the client-side resolution and verification rules
that consume it. Publishers stay on their own infrastructure — a release is a
signed tag in their own repository — and a catalog is a *derived, signed
materialization* of many publishers' releases. Astrid specifies the document
formats, the trust model, and the verification a conforming client performs; it
operates no index. Any party may run a catalog, and clients may configure
several.

This RFC owns **distribution and trust**. It depends on
[rfcs#26](https://github.com/astrid-runtime/rfcs/pull/26) (`cargo_like_manifest`)
for manifest shape and WIT references, and **supersedes that RFC's "What the
registry actually is" section**, which sketched an unsigned git index.

# Motivation
[motivation]: #motivation

## What exists today

Capsule installation resolves a source string directly against GitHub, with no
index and no signature verification
(`astrid-cli/src/commands/capsule/install.rs`):

1. `./path` or `/path` → local directory.
2. `@org/repo` → literally `https://github.com/{org}/{repo}`. The `@` prefix is
   sugar for a GitHub path, not a namespace with an owner record.
3. `github.com/org/repo` → the same path.
4. Anything else → assumed to be a local folder.

A trailing `@version` suffix is folded into a ref spec
(`split_version_suffix`). Ref resolution (`install_github.rs`) prefers an
explicit `tag`, then tries `v{version}` and `{version}`, and otherwise falls
back to the GitHub `releases/latest` endpoint. The release's `.capsule` assets
are downloaded under a 50 MB ceiling; a release may carry **several** capsules,
selected by `--capsule <name>`. When no release asset resolves, the installer
may clone and build `HEAD`.

Three consequences motivate this RFC.

**There is no discovery.** No document lists the capsules that exist. The
website cannot show a catalogue, `astrid capsule search` cannot exist, and
finding a capsule means already knowing its repository.

**There is no integrity guarantee on the release path, only a record.**
`Distro.lock` stores the BLAKE3 of the WASM that was installed
(`LockedCapsule.hash`), but no install path takes an *expected* hash as input:
`ExpectedCapsule` carries an id and a version, never a digest. The lock
documents what arrived; it does not constrain what may arrive next time.

Signing does exist, but only one level up. A sealed `.shuttle` distro archive is
signed by its maintainer's ed25519 key and verified against a pinned trust store
at install, and the resulting `CapsuleMeta.signature` records that provenance
descriptively. Nothing signs an individual capsule release: a capsule pulled
from `@org/repo` is trusted because GitHub served it over TLS. There is no
publisher identity, so there is nothing a signature could be checked against.

**"Latest" is defined by GitHub, by date.** `releases/latest` returns the most
recently *published* non-draft, non-prerelease release, which is not the highest
semver. Republishing an old release changes what "latest" means. Two clients
resolving at different moments can disagree with no way to detect it.

## What this RFC adds

- A discovery surface: signed, static, cacheable, browsable, mirrorable.
- Publisher-signed releases, so bytes are trusted because their author signed
  them and never because an index listed them.
- A deterministic `latest` rule that every client computes identically.
- Namespace continuity: a name keeps meaning the same publisher over time.
- Yank and advisory channels, so a bad release can be withdrawn without breaking
  pinned deployments.

## Why Astrid specifies this but runs nothing

Astrid is the upstream runtime; distributions are products built on it. Linux
has no package index; Debian does. If Astrid operated *the* capsule index, the
runtime would acquire a governance surface and a hosting bill, and every
downstream distribution would inherit a dependency on a project that must stay
neutral to be adopted.

The split this RFC draws is therefore: **Astrid defines the protocol and ships
the verifier; a catalog is an instance, operated by whoever wants one.** The
first instance is expected to be operated by Unicity for AOS. Nothing in this
document privileges it.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## Publishing a capsule

You publish from your own repository. There is no account, no upload, and no
service that holds a credential of yours.

```
$ astrid publish
  generated publisher key      ed25519:9f3c…  (stored in the OS keychain)
  generated successor key      ed25519:41ba…  (SAVE THIS OFFLINE)
  wrote .astrid/publisher.toml
  built  acme-widget.wasm      blake3:7d92…
  validated                    ok (validator 3)
  signed release manifest
  tagged v1.3.0, pushed, created release
  signalled catalog aos
  waiting for catalog…         published as acme/widget 1.3.0 (serial 4172)
```

On first run the CLI mints two keys. The working key signs releases and lives in
the OS keychain. The **successor key** is your recovery and revocation key; the
command prints it once and expects you to store it offline. Both public keys go
into `.astrid/publisher.toml`, committed on your default branch:

```toml
schema-version = 1
pubkey    = "ed25519:9f3c…"
successor = "ed25519:41ba…"
expires   = "2028-07-26T00:00:00Z"
```

Every later release is `astrid publish` again: build, validate, sign, tag, push,
signal, wait. The signal is a doorbell — it tells a catalog to go look, and
carries nothing the catalog believes.

## Getting into a catalog

The first accepted release under a source binds that source's namespace to your
key, recorded in the catalog's transparency log. Later releases must verify
against that key or a properly rotated successor. Nobody reviews anything.

Two things do take a pull request against the catalog repository, because both
are scarce shared resources: a **short name** (`acme/widget` as an alias for
`github.com/acme/capsule-widget`) and a **new category**. Everything else is
self-service.

## Consuming a catalog

```
$ astrid capsule search memory
acme/widget          1.3.0   memory, knowledge     wants: kv
unicity/mnemos       0.4.1   memory               wants: kv, net

$ astrid capsule install acme/widget
  catalog aos          serial 4172, fresh (signed 3h ago)
  publisher            acme  (first seen 2026-02-11, 14 releases)
  capsule              acme/widget 1.3.0   blake3:7d92…
  capabilities         kv
  installed
```

The client fetches the catalog root, checks its signature and freshness, fetches
the one shard for that name, verifies its hash against the root, verifies the
publisher's signature over the release manifest, downloads the WASM, and checks
its BLAKE3 before anything is written. A stale, forked, or unsigned catalog
fails closed.

## Curated bundles

A **constellation** is a curated set drawn from catalogs — this is what
`Distro.toml` already describes (rfcs#21). Listing in a catalog implies nothing;
inclusion in a constellation is somebody vouching. Constellation entries name
capsules with catalog-qualified references so a bundle can never resolve to a
different publisher's capsule of the same name.

## The website

A catalog's website is an ordinary client. It fetches the same signed documents
over the same rules and renders them. It holds no privileged path and no private
data, and it must display the catalog serial and freshness it is rendering.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

Requirement levels are RFC 2119. Sections marked **[reserved]** define document
fields and semantics that conforming v1 implementations MAY omit; the formats
accommodate them so that adding them later is not a breaking change.

## 1. Source grammar

A **source** identifies where a capsule's releases come from. Sources are
host-agnostic; a *profile* defines how one host is addressed.

```
source     := profile ":" path | legacy
profile    := "github" | "git" | "https"
legacy     := "@" org "/" repo | "github.com/" org "/" repo
```

`github:acme/capsule-widget` is the canonical form of the existing
`@acme/capsule-widget`. Both legacy forms MUST continue to resolve identically
to the canonical form; they are the same identity, not aliases.

A **capsule name** is `<source>#<capsule-id>` when a release carries several
capsules, and `<source>` alone when it carries exactly one. The existing
multi-capsule release behaviour (`--capsule <name>`) is preserved: one source
may publish many capsule names, all under one publisher key.

**Catalog scoping.** A name is meaningful only within a catalog. Clients that
configure several catalogs MUST NOT merge their namespaces and MUST NOT fall
back from one to another. A reference resolves as `(catalog, name)`, using the
qualified syntax established by rfcs#26 (`@catalog:name`). A lockfile MUST
record which catalog resolved each entry, and resolution MUST fail if a name
appears in a catalog other than the recorded one. This closes dependency
confusion, which reaches Astrid through constellation manifests — the only place
a reference is resolved on someone else's behalf.

## 2. Catalog documents

A catalog is a set of static documents under one base URL.

```
catalog.json          signed root
c/<escaped-name>.json per-name shard
search.json           browse/search projection
log/checkpoint.json   signed log head
log/entries.jsonl     append-only log
blob/<blake3>.wasm    mirrored artifact
src/<blake3>.tar.zst  mirrored source snapshot
```

Shard filenames escape the name by percent-encoding every character outside
`[A-Za-z0-9._-]`, so URL-form names map to flat files unambiguously.

### 2.1 Root

```json
{
  "schema_version": 1,
  "catalog_id": "aos",
  "serial": 4172,
  "issued_at": "2026-07-26T14:03:11Z",
  "valid_until": "2026-07-28T14:03:11Z",
  "hard_expiry": "2026-08-02T14:03:11Z",
  "shards": { "github%3Aacme%2Fcapsule-widget": "blake3:1a2b…" },
  "aliases": { "acme/widget": "github:acme/capsule-widget" },
  "search": "blake3:9c8d…",
  "log_checkpoint": "blake3:5e6f…",
  "signature": { "key": "ed25519:cc11…", "sig": "…" }
}
```

`serial` is strictly monotonic. Clients MUST reject a root whose serial is lower
than the highest they have seen from that catalog.

### 2.2 Shard

```json
{
  "name": "github:acme/capsule-widget",
  "alias": "acme/widget",
  "publisher": { "key": "ed25519:9f3c…", "first_seen": "2026-02-11T…" },
  "versions": [{
    "version": "1.3.0",
    "capsule_id": "acme-widget",
    "wasm": { "blake3": "7d92…", "size": 481920,
              "urls": ["blob/7d92….wasm",
                       "https://github.com/acme/capsule-widget/releases/download/v1.3.0/acme-widget.capsule"] },
    "source": { "blake3": "3f41…", "url": "src/3f41….tar.zst", "ref": "v1.3.0" },
    "manifest_sig": "…",
    "license": "Apache-2.0 OR MIT",
    "categories": ["memory"],
    "keywords": ["kv", "cache"],
    "imports": { "astrid:kv": "^1.0" },
    "exports": { "astrid:memory": "1.0.0" },
    "capabilities": ["kv"],
    "astrid_version": ">=0.9.0",
    "validator": 3,
    "published_at": "2026-07-26T13:58:02Z",
    "yanked": null,
    "reproduced": "unknown"
  }]
}
```

Shards are the unit of lazy loading: one cacheable GET per name, independent of
catalog size.

### 2.3 Resolution of `latest`

`latest` is the highest `version` by semver precedence among entries that are
not yanked and not prereleases. It MUST be computed from shard contents, never
from a host API. This deliberately diverges from GitHub's `releases/latest`,
which orders by publication date; the current behaviour is therefore a
behavioural change for unpinned installs and MUST be noted in release notes.

## 3. Trust model

Two key roles, with disjoint authority.

**Publisher key** — authority over *content*. A capsule's bytes are trusted
because its publisher signed a release manifest covering them. Clients MUST
verify the publisher signature independently of catalog membership. Listing in a
catalog is not evidence about content.

**Catalog key** — authority over *freshness and consistency* only: which
versions exist, that the view is current, and that all clients see one set. A
compromised catalog key can serve a stale or incomplete view; it MUST NOT be
able to forge a release, because no client accepts a release on catalog
authority alone.

Both roles reuse the existing `ed25519:<base64>` wire form and trust-store
machinery that `[distro.signing]` and `.shuttle` verification already use, so
this RFC adds a second signing surface to the runtime rather than a second
signing *system*. A distro signature continues to attest a bundle; a publisher
signature attests one capsule release, and neither substitutes for the other.

Because the catalog re-signs on every publish and on every freshness refresh,
its signing key is necessarily online. Catalogs therefore SHOULD use an offline
**root key** that signs a rotatable online catalog key, so recovery from CI
compromise does not require re-bootstrapping clients.

**Client bootstrap.** A client ships the offline root public key of each catalog
it trusts by default, compiled in. Everything else chains from it. This makes
binary distribution the root of trust, which the existing `self_update`
verification path already governs; the two MUST NOT be circular — self-update
never resolves through a catalog.

### 3.1 Namespace binding (TOFU)

The first accepted release under a source binds that source's namespace to the
publisher key found in `.astrid/publisher.toml` **on the source's default
branch**. The requirement to be on the default branch is load-bearing: it proves
control of the repository, not merely the ability to attach a release asset.

The binding is recorded in the transparency log. Later releases MUST verify
against the bound key or a validly rotated successor.

First contact is the one moment without cryptographic continuity. Three
mitigations apply:

- For the `github` profile, ingest MUST record a GitHub artifact attestation
  when present and SHOULD require one on first binding, corroborating the claim
  with the host's OIDC identity.
- A newly bound namespace is marked `new_publisher` for a quarantine period
  (RECOMMENDED 7 days). It is fully installable; clients MUST surface the mark
  at install. **[reserved]**
- Ingest MUST NOT accept a binding for a source whose namespace was previously
  bound to a different key without an explicit, logged, human-gated override.

### 3.2 Rotation, revocation, and theft

A stolen working key is strictly worse than a lost one, because naive rotation
lets the thief install their own successor and lock the owner out permanently.
Therefore:

- **Rotation to the declared successor** (the `successor` recorded at binding)
  takes effect immediately.
- **Rotation to any undeclared key**, and any **change of the declared
  successor**, takes effect only after a delay (RECOMMENDED 14 days), during
  which it is visible in the log and MAY be overridden.
- **Revocation by the declared successor** takes effect immediately and is not
  delayed. The successor is the stronger claim by prior declaration, so it is
  the emergency brake against a thief holding the working key.

This inverts the race: an attacker must go unnoticed for the full delay against
an owner holding a key that can cancel them at once.

Revocation marks a key compromised as of a claimed time. The log identifies
every version that key signed; those versions MAY then be yanked. The true
moment of compromise is not knowable, so implementations MUST NOT present the
boundary as exact.

Key **expiry** (`expires` in `publisher.toml`) blocks new releases from an
expired key. It MUST NOT invalidate versions already published, which the log
binds to the key that was valid at publication.

### 3.3 Transparency log

An append-only record of namespace bindings, key rotations and revocations,
publications, yanks, advisories, and alias grants.

Log entries carry **hashes and metadata only, never content**. Removing a
mirrored blob or source snapshot in response to a legal demand therefore leaves
the log consistent, and takedown is a routine operation rather than a conflict
with append-only semantics.

Clients SHOULD persist the last log position they observed and verify a
consistency proof against it on each fetch, which defeats rollback against any
returning client. **[reserved]**

Checkpoints and all catalog metadata MUST be committed to the catalog's public
git repository, and the served metadata MUST be byte-identical to the repository
state at the checkpoint commit. Every clone is then a passive witness and a
rewrite is detectable by anyone who has ever pulled. Mirrored blobs and source
snapshots MAY be served from other storage, because they are hash-verified and
carry no trust — but metadata MUST NOT move out of git, or the witness property
is lost silently.

Residual, stated plainly: a split view presented to a client with no prior state
survives until cross-checked. Browser clients with clearable storage sit
permanently closer to this line than the CLI. Witness and gossip ecosystems are
out of scope. **[reserved]**

## 4. Ingest

Ingest is whatever process materializes a catalog. It is not a trusted input
path: it re-fetches from the source and derives every recorded fact itself.

**Signals.** A signal names a source and carries nothing else. Signal channels
are unauthenticated, unordered, and at-least-once; implementations MAY offer any
number of them, because nothing downstream trusts them. A catalog MUST also
poll, so that no release depends on a signal arriving.

**Gates.** A release MUST be rejected unless all hold:

1. The release manifest verifies against the namespace's bound key.
2. `.astrid/publisher.toml` is present on the default branch and consistent with
   the binding.
3. The declared capsule name lies within the source's namespace.
4. The version does not already exist. **Published versions are immutable.**
   Errors are corrected by yanking and publishing a new version.
5. The artifact's BLAKE3 matches the signed manifest.
6. The artifact is a valid WASM component whose imports lie entirely within the
   audited `astrid:*` surface. Any `wasi:*` import MUST be rejected.
7. Declared `[imports]`/`[exports]` match the component's actual interfaces, and
   WIT references resolve.
8. `license` is a valid SPDX expression.
9. Size is within the catalog's ceiling, and per-publisher rate and version-count
   quotas are not exceeded.
10. `astrid-version` is a parseable semver requirement.

Ingest MUST NOT use the clone-and-build fallback: bytes that no publisher signed
cannot be admitted.

Gates 6 and 7 are the strongest cheap defence available, and they run against
bytes the catalog fetched itself. The same validator runs in `astrid publish`,
as a convenience, so failures surface at the publisher's desk — but CI output is
publisher-controlled and is never the gate. The validator is versioned; each
entry records the version that admitted it, so tightening a check later does not
retroactively invalidate history and grandfathering is auditable.

A name within a small edit distance of an existing name MUST NOT be auto-
admitted; it is held for the human-gated channel. **[reserved]**

Ingest mirrors the artifact and a source snapshot of the tag. Mirroring makes
installs survive deleted upstream repositories, and archiving source keeps
auditability and the reproducibility signal from depending on publishers' repo
hygiene.

**Derived data is machine-written.** Only derived paths are bot-writable. The
human-gated files — category vocabulary, alias claims, blocklist — change by
reviewed pull request only, and that separation SHOULD be enforced by write
scope so a compromised ingest run cannot grant itself an alias.

## 5. Client verification

A conforming client, given a name and a catalog, MUST:

1. Fetch the root; verify its signature chains to the trusted root key.
2. Reject a serial lower than the highest seen for that catalog.
3. Evaluate freshness (§6).
4. Fetch the shard; verify its BLAKE3 against the root.
5. Resolve the version (§2.3), refusing yanked versions for new installs.
6. Verify the publisher's signature over the release manifest, against the key
   bound for that namespace, honouring revocation.
7. Fetch the artifact from any listed URL and verify its BLAKE3 before use.
8. Surface, before any write: publisher and first-seen date, quarantine mark if
   any, the capability set, and any advisory.

Verification MUST be implemented once, in a single crate (`astrid-catalog`),
compiled natively for the CLI and to WASM for browser clients, so there is one
audit surface rather than one per client. This RFC ships **conformance test
vectors** — valid, expired, rolled-back, forked-log, wrong-key, tampered-shard,
and yanked fixtures — and an implementation is conforming only if it produces
the specified outcome for each.

## 6. Freshness

Two thresholds:

- Past `valid_until`, clients MUST warn and MAY proceed.
- Past `hard_expiry`, clients MUST refuse new installs unless explicitly
  overridden (`--allow-stale`).

Already-installed, hash-pinned capsules are unaffected by staleness at any point;
freshness governs discovery, not execution.

Comparisons MUST allow a small clock-skew tolerance (RECOMMENDED 5 minutes).
Catalogs SHOULD drive the freshness re-sign from more than one trigger, since
with a single trigger a CI failure becomes an availability incident.

## 7. Yank and advisories

A yank marks a version withdrawn. It MAY be issued by the publisher key or the
declared successor, and by the catalog operator. It is a new log entry, never an
erasure.

Clients MUST refuse a yanked version for new installs, MUST warn when a lockfile
pins one, and MUST NOT break an existing installation. An advisory is a
catalog-level statement about a version range, carrying severity and prose, and
is surfaced on install and on `astrid capsule audit`. **[reserved]**

## 8. Manifest additions

Adds to `Capsule.toml` (coordinating with rfcs#26):

- `categories` — at most 5, each from the catalog's controlled vocabulary,
  reserving `parent::child` grammar for future use. Unknown values are rejected
  at ingest, which records the vocabulary version applied.
- `keywords` — at most 5, free-form, at most 32 characters each. Uncontrolled,
  and the signal from which new categories are promoted.

Categories are publisher-asserted and unverified, so they are capped and never a
ranking input on their own. The higher-value facets are **derived** at ingest —
capabilities, imports/exports, CLI verbs — because they cannot be overstated
relative to what the sandbox will enforce, and they support the query that
matters most on a browse surface: *what does this want access to?*

## 9. Install handoff

A catalog website MAY offer a one-click install through an `astrid:` URL scheme
registered by the CLI:

```
astrid:install?catalog=<id>&name=<name>&version=<semver>
```

The handler MUST treat every parameter as untrusted, MUST resolve and verify
through §5 exactly as any other install, and MUST NOT act without explicit
confirmation. A link therefore cannot install anything; at worst it opens a
dialog the user did not ask for.

Because a constellation install is a capability grant, the confirmation MUST
present the **union** of capabilities being granted, ordered by severity, rather
than a per-capsule enumeration. Clients MUST also offer the equivalent command
for copying, since the scheme handler is unavailable before first install.

## 10. Conformance tiers

**v1-normative:** §1 source grammar and catalog scoping; §2 documents and the
`latest` rule; §3 two-key model, TOFU binding, rotation/revocation/timelock, log
entries as hashes; §4 gates and immutability; §5 client verification and the
shared crate; §6 freshness; §7 yank; §8 manifest fields; §9 handoff.

**Reserved:** log consistency proofs, quarantine marks, edit-distance review,
advisories, reproducibility verification, witness ecosystems. Documents carry
their fields from serial 1; implementations may defer the behaviour.

# Drawbacks
[drawbacks]: #drawbacks

**Publishers hold keys, and key management is hard.** There is no password
reset, because there is no authority holding a second factor. Pre-declared
successors reduce this to negligence rather than a default outcome, but some
publishers will lose both keys and each is a manual procedure.

**First contact is unprotected.** TOFU secures every publish after the first.
A repository hijacked before its owner ever publishes binds the namespace to the
attacker, and unwinding that is the messy human path the design otherwise avoids.

**Moderation is reactive.** An open namespace admits typosquats and malicious
capsules first and withdraws them after. Import scanning cannot see a capsule
misusing capabilities it was legitimately granted. The constellation tier exists
precisely because the catalog tier promises nothing.

**Freshness couples availability to our own CI.** A broken re-sign is an
outage after `hard_expiry`. The grace band and redundant triggers bound this,
but it is self-inflicted and permanent.

**No dynamic surface.** Download counts, popularity, dependents, instant
takedown UX, and server-side search all require infrastructure this design does
not have. Client-side search degrades past a few thousand entries.

**Verification complexity moves into clients.** Signature chains, log
consistency, and expiry become code Astrid ships, where a bug is a
vulnerability. One shared crate and conformance vectors bound the surface; they
do not remove it.

**A slower publish loop.** Seconds become tens of seconds, and failure feedback
arrives out-of-band rather than as an API error.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why not a registry service?** An upload service needs accounts, tokens, OAuth,
and session storage — an apparatus that exists to authenticate writes. With
publishing defined as "sign a tag in your own repository", there is nothing to
authenticate to and no secret for the catalog to hold. The litmus test is that
**a catalog never learns a secret**: it verifies public keys against public logs.
A change that requires storing something confidential to authenticate someone has
rebuilt crates.io by accident.

**Why not review submissions like Debian?** Debian's trust unit is a person,
established over months. That model produces high assurance and a gatekeeper
corps this project does not have.

**Why not sign only at the index, like apt?** apt roots content trust in the
archive key, so compromising the archive forges packages. Splitting content
authority (publisher) from freshness authority (catalog) keeps an online CI key
from being able to forge anything.

**Why not build from source ourselves?** It is stronger and it requires a build
farm, which each capsule's per-repository target configuration complicates. The
`reproduced` field carries the honest signal without putting a build on the
critical path.

**Why not NOSTR?** It would replace the index but not the artifact store, still
needs an aggregator for usable search, and lacks a namespace authority. The pain
this design addresses is maintenance toil, which a generated index removes
entirely. `nostr:` remains available as a future profile precisely because
transport is not load-bearing: the lock pins hashes regardless.

**Why not extend rfcs#26?** Its registry section sketches an unsigned git index
as a supporting mechanism for WIT resolution. Folding key management, a
transparency log, and a trust model into a manifest-shape RFC would double a
document already long open. The two are layered instead: #26 owns what a manifest
says, this RFC owns how bytes travel and why they are believed.

# Prior art
[prior-art]: #prior-art

- **apt** — static signed metadata over dumb transport (`InRelease` → `Packages`
  → `pool/`), and `Valid-Until` as an anti-freeze measure. Adopted. Its
  archive-key-as-content-root is deliberately not adopted.
- **TUF** — separation of signing roles and offline root signing an online key.
  Adopted in reduced form: two roles rather than the full set.
- **Go modules** — the URL is the name, publishing is tagging, and
  `sum.golang.org` binds module@version to a hash. Adopted, minus the assumption
  of a live proxy.
- **Certificate Transparency** — append-only logs, consistency proofs, and
  witnessing. Adopted in reduced form; git clones stand in for a witness
  ecosystem.
- **crates.io** — the sparse HTTP index, after the git index failed to scale.
  Adopted. Its accounts-and-tokens layer is not, being an artefact of having an
  upload endpoint.
- **Homebrew taps** — a git repository as a registry, PR only for scarce
  resources. Adopted.
- **Ubuntu PPAs** — an uncurated open tier beside a curated archive. Adopted as
  catalog versus constellation.
- **npm dependency confusion** — the failure mode that makes catalog-scoped
  names normative here.
- **Sigstore** — keyless signing via OIDC. Recorded as corroboration for the
  `github` profile, not adopted as the identity, since it would weld trust to one
  host.

The closest single ancestor is the Go module ecosystem wearing apt's serving
format: Go's trust shape, apt's delivery shape. Every deviation from both is the
same deviation — removing standing infrastructure — and each removal is paid for
with a cryptographic substitute.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- The delay constants (rotation 14 days, quarantine 7 days) are placeholders
  pending review; both trade recovery speed against attacker dwell time.
- Whether alias grants should expire if unused, to avoid a squatted short-name
  namespace accumulating.
- How a catalog attests its own operator identity to a client adding it for the
  first time, beyond a compiled-in key for defaults.
- Whether `search.json` shards by category at a threshold, or whether the growth
  cliff is handled by the advisory-only service in future possibilities.
- Interaction with rfcs#26's `[[registry]]` configuration block: whether catalog
  configuration reuses it verbatim or supersedes it.
- Recovery procedure when both publisher keys are lost — the one deliberately
  human path, whose evidentiary bar is undefined.

# Future possibilities
[future-possibilities]: #future-possibilities

- **An advisory-only statistics service.** Downloads, popularity, and dependents,
  under one hard rule: nothing trust-bearing may depend on it. Sketching it now
  keeps it from being bolted on under growth pressure.
- **Reproducible-build verification** promoted from a badge to a gate once the
  rebuild path is stable.
- **`nostr:` as a profile**, once third-party self-service publishing is real.
- **Witness and gossip** for split-view detection, if the ecosystem grows past
  the point where git clones are adequate.
- **Interface-level discovery** — "this needs a provider of `astrid:llm@^1`;
  none is installed; candidates are…" — which the shard's imports/exports data
  makes possible without a dependency solver, since capsules dedup by hash and
  may coexist per principal.
- **Private and air-gapped catalogs**, which the design already permits: a
  catalog is static files and an offline key.
