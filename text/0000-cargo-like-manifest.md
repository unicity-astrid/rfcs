- Feature Name: `cargo_like_manifest`
- Start Date: 2026-05-10
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

Replace `Capsule.toml`'s overlapping topic surfaces (`[[topic]]` blocks, `[capabilities].ipc_publish` / `ipc_subscribe` arrays, `[[interceptor]]` blocks, nested `[exports.<ns>]` / `[imports.<ns>]`) with a single Cargo-shaped declaration. Topics become typed entries in `[publish]` / `[subscribe]` tables, each with a required `wit = "@scope/repo/<interface>/<record>"` reference resolving through a lightweight GitHub-backed registry. `Capsule.lock` pins resolved git refs and BLAKE3 content hashes for reproducible installs. Interface declarations and operator-facing descriptions get one canonical home each, and capsule-author-controlled prose never reaches the LLM unless an explicit operator-reviewed `description_for_llm` allowlist surfaces it.

# Motivation
[motivation]: #motivation

The v0.5 manifest accumulated four overlapping ways to declare what a capsule does on the bus, and the migration to wasmtime exposed every seam.

1. **Three places declare topics**: `[[topic]]` blocks (schema + description + direction), `[capabilities].ipc_publish` / `ipc_subscribe` arrays (ACL patterns), and `[[interceptor]]` blocks (subscribe-with-handler, implicit ACL grant). The same topic name appears in two or three of them; they fall out of sync; the parser tolerates partial declarations and errors at runtime.

2. **`[[topic]].direction` is a serde-required field with no `#[serde(default)]`** (`crates/astrid-capsule/src/manifest.rs:466`). Every capsule manifest on `main` that omits it fails to install on a fresh checkout. Five of the chat-stack capsules carried local "add `direction = \"publish\"`" patches that never made it back upstream.

3. **The `description` field on `[[topic]]` is free-form prose declared by the capsule author and surfaced to LLMs in some pipelines.** Capsule-author-controlled strings reaching the model unattended is a prompt-injection vector — adversarial text in a topic description gets sent to the LLM as part of the operator-trusted system context.

4. **Topic schemas (`wit_type`) are optional and rarely populated.** Most topics travel as untyped JSON. When a producer changes shape, no one notices until a consumer fails to deserialize. The bus carries 60+ topic families across 15 capsules; only 5 of them carried `wit_type` references on `main`.

5. **Interface declarations use nested-namespace TOML tables** (`[exports.astrid] foo = "1.0.0"`). When extended with extra options the pattern is awkward (`[exports.astrid] foo = { version = "1.0.0", optional = true }` — overlaps "value-or-table" form Cargo abandoned a decade ago). Migration to a future workspace inheritance model is harder than it needs to be.

6. **No reproducible installs.** A capsule pulled from a git source today resolves to whatever HEAD is. Tomorrow's install picks up a force-push and silently changes the contract.

This RFC replaces the four overlapping surfaces with one Cargo-shaped declaration:

- One source of truth for each topic: `[publish]` / `[subscribe]` table entries.
- Required typed payload reference (`wit = "@scope/repo/iface/record"`).
- One operator-facing description (`[package].description`) — never sent to the LLM.
- One LLM-facing description per tool (`[[tool]].description_for_llm`) — explicitly operator-reviewed at install.
- A `Capsule.lock` that pins resolved git SHAs and BLAKE3 content hashes for reproducibility.
- A lightweight GitHub-backed registry index (`unicity-astrid/registry`) that maps semver versions to commit SHAs, with direct-git-ref escape hatches for off-registry use.

Result: one place to declare each fact, no parser-required-but-undocumented fields, no capsule-author prose reaching the LLM, and reproducible installs without operating any new infrastructure beyond a git index repo.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

A capsule manifest looks like this:

```toml
[package]
name        = "astrid-capsule-prompt-builder"
version     = "0.1.0"
description = "Assembles LLM prompts with plugin interceptor hooks"   # operator-facing only
authors     = ["Joshua J. Bouw <dev@joshuajbouw.com>"]
astrid-version = ">=0.6.0"

[[component]]
id   = "prompt-builder"
file = "astrid_capsule_prompt_builder.wasm"

# WIT interface contracts — versioned, semver-resolved at boot.
[exports]
"astrid:prompt" = "1.0.0"

[imports]
"astrid:session" = "^1.0"
"astrid:llm"     = { version = "^1.0", optional = true }   # long form when extras needed

# Outbound topics — every entry carries a typed WIT payload reference.
# Short form: bare wit string when no other fields needed.
[publish]
"prompt_builder.v1.response.assemble" = "@unicity-astrid/wit/prompt/assemble-response"

# Long form: inline table when extras (registry version, source override) needed.
"prompt_builder.v1.hook.before_build" = { wit = "@unicity-astrid/wit/prompt/before-build-hook", version = "^1.0" }

# Inbound topics — `handler` binds to a #[astrid::interceptor("...")] export.
[subscribe]
"prompt_builder.v1.assemble" = { wit = "@unicity-astrid/wit/prompt/assemble-request", handler = "handle_assemble" }
"session.v1.response.get_messages.*" = "@unicity-astrid/wit/session/get-messages-response"

# LLM-facing surface — operator must approve `description_for_llm` at install.
[[tool]]
name                = "search"
description_for_llm = "Search the project knowledge base for matching documents."
```

Every fact about how this capsule interacts with the bus appears exactly once. There is no `[[topic]]` block. There is no `direction` field. There are no `ipc_publish` / `ipc_subscribe` arrays in `[capabilities]`. There are no `[[interceptor]]` blocks.

## What the operator sees on install

```
$ astrid capsule install ./dist/astrid-capsule-prompt-builder.capsule

Installing astrid-capsule-prompt-builder 0.1.0
  Description: Assembles LLM prompts with plugin interceptor hooks

  Exports: astrid:prompt 1.0.0
  Imports: astrid:session ^1.0, astrid:llm ^1.0 (optional)

  Publishes 4 topic(s) to the bus:
    prompt_builder.v1.response.assemble  → @unicity-astrid/wit/prompt/assemble-response
    prompt_builder.v1.hook.before_build  → @unicity-astrid/wit/prompt/before-build-hook
    prompt_builder.v1.hook.after_build   → @unicity-astrid/wit/prompt/after-build-event
    session.v1.request.get_messages      → @unicity-astrid/wit/session/get-messages-request

  Subscribes to 2 topic(s):
    prompt_builder.v1.assemble                  → @unicity-astrid/wit/prompt/assemble-request
    session.v1.response.get_messages.*          → @unicity-astrid/wit/session/get-messages-response

  Will surface 1 tool to the LLM (description shown verbatim):
    search — "Search the project knowledge base for matching documents."

  Continue? [y/N]
```

The operator sees, *verbatim*, every string that will be sent to a model. Capsule-author prose that doesn't appear in this dialog never reaches the LLM. Operator-facing prose (`[package].description`) shows here too, but it never enters LLM context.

## What gets resolved through the registry

References under `[publish]` / `[subscribe]` / `[exports]` / `[imports]` use a Cargo-shaped form:

```toml
"topic" = { wit = "@unicity-astrid/wit/llm/generate-request", version = "^1.0" }     # registry-resolved
"topic" = { wit = "@unicity-astrid/wit/llm/generate-request", tag = "v1.2.0" }      # git tag
"topic" = { wit = "@unicity-astrid/wit/llm/generate-request", rev = "abc1234" }     # git SHA
"topic" = { wit = "@unicity-astrid/wit/llm/generate-request", branch = "main" }     # floating
"topic" = { wit = "@unicity-astrid/wit/llm/generate-request", path = "../wit" }     # local dev
```

`version` queries the registry index. The other forms bypass it. Exactly one of `version` / `tag` / `rev` / `branch` / `path` per entry.

A bare type name (no `@`-prefix) means "in this capsule's own `wit/` directory":

```toml
"topic" = "my-local-record"
```

## What `Capsule.lock` does

`astrid capsule install` resolves every reference, fetches the snapshot, computes the BLAKE3 of its content, and writes a `Capsule.lock` next to `Capsule.toml`:

```toml
# Capsule.lock — autogenerated, commit alongside Capsule.toml
[[package]]
source       = "@unicity-astrid/wit"
version      = "1.2.0"
git_ref      = "abc1234567890abcdef1234567890abcdef12345"
content_hash = "blake3:7d92c4f1a3..."
```

Subsequent installs compare the BLAKE3 of the fetched content against `content_hash`. Mismatch = abort + clear error. Upstream force-push, registry tampering, transport fault — all caught.

## What the registry actually is

`unicity-astrid/registry` is a single git repo. One TOML file per published package:

```toml
# packages/unicity-astrid/wit.toml
[[version]]
version      = "1.2.0"
ref          = "abc1234..."          # full git SHA at the time of publish
content_hash = "blake3:7d92..."
yanked       = false

[[version]]
version      = "1.1.0"
ref          = "def4567..."
content_hash = "blake3:8e03..."
yanked       = false
```

Anyone can clone the index. Writes are PR-reviewed (or auto-published via a GitHub Action when the source repo tags `vX.Y.Z`). Operating cost: zero infrastructure beyond the index repo. Matches Cargo's [crates.io-index](https://github.com/rust-lang/crates.io-index) topology, just smaller.

Multiple registries supported via `~/.astrid/etc/config.toml`:

```toml
[[registry]]
name  = "default"
index = "https://github.com/unicity-astrid/registry"

[[registry]]
name  = "myorg"
index = "https://github.com/myorg/astrid-registry"
```

References can be qualified `@myorg:unicity-astrid/wit/...` to pick a non-default registry; otherwise the default index resolves them.

## Migration

Every capsule on the chat stack carries, on a `migrate/cargo-manifest` branch, a hand-converted manifest in this format. The kernel parser doesn't yet understand it. When the parser lands (Implementation step 1), every capsule-side branch can PR independently and the migration completes capsule-by-capsule with no flag-day.

The `[[interceptor]]` block, `[[topic]]` block, `[capabilities].ipc_publish` / `ipc_subscribe` arrays, and `direction` field are removed. The new parser **does not** accept the legacy form.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## `Capsule.toml` schema

### `[package]`

Unchanged from v0.5 except: `description` is the **only** capsule-author-controlled prose field that can be surfaced to operators. It is never sent to an LLM.

### `[[component]]`

Unchanged.

### `[exports]` and `[imports]`

Flat tables keyed by `"<namespace>:<interface>"` (single TOML key, quoted because of the colon).

Short form (just version):

```toml
[exports]
"astrid:prompt" = "1.0.0"

[imports]
"astrid:session" = "^1.0"
```

Long form (when extras needed):

```toml
[imports]
"astrid:llm" = { version = "^1.0", optional = true }
```

Fields:

- `version: VersionReq` — required for `[imports]`, treated as `Version` (exact) for `[exports]`.
- `optional: bool` — `[imports]` only. Defaults `false`.

Capsules with `uplink = true` MUST NOT declare `[imports]` (uplinks are entry points; depending on other capsules creates a cycle).

### `[publish]` and `[subscribe]`

Tables keyed by topic name. Topic name MAY be a wildcard (`foo.bar.*`) per the existing bus matcher rules.

Short form (bare WIT string):

```toml
[publish]
"prompt_builder.v1.response.assemble" = "@unicity-astrid/wit/prompt/assemble-response"
```

Long form (inline table):

```toml
[publish]
"prompt_builder.v1.response.assemble" = { wit = "@unicity-astrid/wit/prompt/assemble-response", version = "^1.0" }
```

Fields:

- `wit: String` — **required**. WIT type reference.
- `version: VersionReq` — registry-resolved version constraint.
- `tag: String` — git tag (registry bypass).
- `rev: String` — full git SHA (registry bypass).
- `branch: String` — floating branch (registry bypass; lockfile pins SHA at lock-time).
- `path: String` — local filesystem path (no version pinning, no checksum verification — for development only).
- `handler: String` — `[subscribe]` only. Binds to a `#[astrid::interceptor("name")]` export.
- `fanout: bool` — `[publish]` only, defaults `false`. Marks a wildcard publish where the suffix segment names a recipient (e.g., `llm.v1.request.generate.*` per provider).

Validation:

- Exactly one of `version` / `tag` / `rev` / `branch` / `path` for any external (`@scope/...`) reference. Bare local refs need none.
- A `[subscribe]` entry without `handler` binds no interceptor — the entry is a pure ACL grant for guest-initiated `ipc::subscribe()` calls.
- Wildcards in `[publish]` / `[subscribe]` must be type-uniform: every member of the topic family carries the same WIT record. Heterogeneous wildcards (e.g., a uplink that proxies many distinct payloads) MUST use `wit = "opaque"` instead of a real ref. The kernel skips schema enforcement on opaque entries.

### `[capabilities]`

Unchanged except: `ipc_publish` and `ipc_subscribe` arrays are removed. They are superseded by `[publish]` / `[subscribe]` entries (which both grant ACL and declare schema). All other capability fields (`fs_read`, `fs_write`, `http`, `net`, `host_process`, `uplink`, `net_bind`, `allow_prompt_injection`, etc.) are unchanged.

### `[[tool]]`

A capsule that surfaces tools to the LLM declares them explicitly:

```toml
[[tool]]
name                = "search"
description_for_llm = "Search the project knowledge base for matching documents."
input_schema_wit    = "@unicity-astrid/wit/types/tool-input"   # or path to a JSON schema file
```

Fields:

- `name: String` — tool name as it appears to the LLM.
- `description_for_llm: String` — **required**. Verbatim string passed to the LLM. Operator approves at install.
- `input_schema_wit: String` — optional. WIT record describing the tool's input shape.
- `mutable: bool` — defaults `false`. Whether this tool may mutate state (drives the approval-prompt copy).

`description_for_llm` is the **only** capsule-author-controlled string that reaches the LLM by default. The operator sees it verbatim during install. Any other capsule-author prose (package description, comments, doc strings) is operator-facing only.

### `wit = "opaque"`

Used by uplink/proxy capsules that route opaque bytes between a transport (Unix socket, HTTPS, etc.) and the bus, and don't typecheck payloads. Skips schema enforcement on the entry. Distinguishes "I deliberately don't care about the type" from "I haven't filled in the type yet."

```toml
[publish]
"astrid.v1.request.*" = { wit = "opaque" }   # CLI proxy fan-out
```

## Reference resolution

```
@<scope>/<repo>/<path-inside-repo>...
```

- `@unicity-astrid/wit/llm/generate-request`
  - Scope: `unicity-astrid`
  - Repo: `wit`
  - Resolves to: that repo's `interfaces/llm.wit` → record `generate-request`
  - Layout convention: `interfaces/<interface-name>.wit` for repos following the canonical layout. Override via `[wit] path = "..."` in the target repo's `Capsule.toml` or `astrid.toml`.

- `@unicity-astrid/wit` (registry-resolved)
  - Resolver looks up `unicity-astrid/wit` in the index.
  - Picks the highest version satisfying the constraint.
  - Fetches the snapshot at the version's `ref`.
  - Verifies BLAKE3 content hash.
  - Looks in `interfaces/<iface>.wit` for the record.

- Bare `record-name` (no scope) — looks in this capsule's own `wit/` directory. Self-reference.

### WIT-only repos

A repo containing only WIT files (no Rust, no `Capsule.toml`) can carry a small `astrid.toml`:

```toml
[wit]
path = "wit"   # default

[dependencies]
"astrid:types" = { version = "^1.0", source = "@unicity-astrid/wit" }   # transitive deps
```

The resolver reads this file (if present) to find the WIT directory and resolve transitive WIT dependencies. Capsule repos use `Capsule.toml` for the same; WIT-only repos use `astrid.toml`.

## `Capsule.lock`

Generated by `astrid capsule install` (or `astrid lock`). Committed alongside `Capsule.toml`.

```toml
# Capsule.lock
version = 1

[[package]]
source       = "@unicity-astrid/wit"
version      = "1.2.0"
git_ref      = "abc1234567890abcdef1234567890abcdef12345"
content_hash = "blake3:7d92c4f1a3..."

[[package]]
source       = "@unicity-astrid/capsule-session"
version      = "1.0.3"
git_ref      = "def4567..."
content_hash = "blake3:8e03..."

# Path sources have no version/git_ref/content_hash — they're inherently mutable.
[[package]]
source = "../shared-wit"
type   = "path"
```

### Lockfile semantics

- One `[[package]]` entry per unique source actually consumed by some `[publish]` / `[subscribe]` / `[exports]` / `[imports]` reference.
- `git_ref` is the resolved 40-char SHA. Used as the *handle* to fetch.
- `content_hash` is BLAKE3 over a normalized snapshot manifest:
  1. List every regular file in the resolved tree, sorted by path (depth-first, canonical separators).
  2. For each file: BLAKE3 of the bytes.
  3. Build a manifest of `<path>\t<blake3-hex>\n` lines.
  4. BLAKE3 of the manifest = `content_hash`.
- On install, the resolver fetches the SHA, recomputes the BLAKE3, and aborts if they differ from the lockfile.
- `astrid update` refreshes the lockfile (and the registry index cache).

### Resolver algorithm

For each external reference in `Capsule.toml`:

1. **`path = "..."`** → no resolution; record as path-source in lockfile.
2. **`rev = "..."`** → fetch SHA directly; recompute BLAKE3.
3. **`tag = "..."`** → resolve tag to SHA via git ls-remote; fetch; recompute BLAKE3.
4. **`branch = "..."`** → resolve branch HEAD to SHA at lock-time; fetch; recompute BLAKE3.
5. **`version = "..."`** → look up package in registry index; pick highest matching version; fetch SHA from version entry; verify BLAKE3 against `content_hash` declared in the index; use as canonical.

For multiple references to the same `(scope, repo)` with overlapping version constraints, the resolver picks **one** version that satisfies all consumers (Cargo-style version unification). Conflicting `tag` / `rev` pins on the same source are an error.

## Registry index format

```
unicity-astrid/registry/        (one git repo)
├── README.md
└── packages/
    └── unicity-astrid/
        ├── wit.toml
        ├── capsule-react.toml
        └── capsule-session.toml
```

Each `<repo>.toml`:

```toml
schema = 1

[[version]]
version      = "1.2.0"
ref          = "abc1234..."          # full git SHA
content_hash = "blake3:7d92..."
yanked       = false
dependencies = [
    { source = "@unicity-astrid/wit", version = "^1.0" },
]
```

The registry's `ref` + `content_hash` pair lets fresh installs (no existing lockfile) verify integrity without trusting the registry, the git transport, or the source repo. A compromised registry can yank or revise; it cannot forge content for an already-published version.

### Yanking

Setting `yanked = true` makes the version invisible to new resolutions but does not break existing lockfiles (they pin the SHA + content_hash directly, registry independence).

### Auto-publish action

A reference GitHub Action (in `unicity-astrid/registry/.github/actions/publish/`) reads a tagged release in the source repo, computes the BLAKE3, and opens a PR against the index with the new `[[version]]` block. Reviewers approve. This is the recommended path; manual PRs are also fine.

## Operator-facing prose vs LLM-facing prose

- `[package].description` — operator-facing. Shown in `astrid capsule list` and on install. Never reaches the LLM.
- `[[tool]].description_for_llm` — LLM-facing. Operator approves verbatim at install. Never silently changed.
- WIT doc-comments — developer-facing. Surfaced in dev tools (`astrid wit inspect`). Never sent to LLM, never shown to operator on install.
- Capsule README, package metadata in registry — operator/developer-facing. Never sent to LLM.

This split is the structural mitigation for the prompt-injection vector. There is no path from a capsule-author-controlled string to the LLM that doesn't pass through an operator-reviewed `description_for_llm` field at install.

## Validation enforced at install

- Every `[publish]` / `[subscribe]` entry MUST have a `wit` field.
- Every external `wit` ref MUST resolve via lockfile (or be path-sourced).
- Every `handler` value on a `[subscribe]` entry MUST match an `#[astrid::interceptor("...")]` (or `#[astrid::tool(...)]`) export in the WASM component (verified by inspecting the component's exports).
- Every `[[tool]].description_for_llm` MUST be present (no silent default).
- Topic names MUST follow the existing bus matcher rules (max 8 segments, max 256 bytes, trailing-suffix wildcards only).
- Wildcards in `[publish]` / `[subscribe]` are type-uniform OR `wit = "opaque"`; mixed schemas under a non-opaque wildcard are rejected.
- `Capsule.lock` MUST exist and be consistent with `Capsule.toml` (every external reference accounted for).

## CLI surface

- `astrid capsule install <source>` — resolve, verify, install. Generates / updates `Capsule.lock`.
- `astrid lock` — refresh lockfile without installing.
- `astrid update [<package>]` — refetch registry, bump versions per constraints, rewrite lockfile.
- `astrid registry update` — refetch all configured registry indices.
- `astrid wit inspect <ref>` — print the resolved record (debugging).

## Migration path

1. Kernel parser ships v2 schema (this RFC) under a feature flag, defaulting on. v1 manifests are rejected at install with an error pointing at the migration guide.
2. Each `unicity-astrid/capsule-*` repo PRs its `migrate/cargo-manifest` branch, opening a per-capsule conversion that the parser can validate.
3. SDK macro (`#[astrid::capsule]`) emits the `handler` names that `[subscribe]` entries reference. No change to the macro API.
4. `astrid capsule migrate <path>` provides a best-effort one-shot conversion for third-party capsules.

# Drawbacks
[drawbacks]: #drawbacks

- **Hard break with the v0.5 manifest.** Every existing capsule must convert. Mitigations: in-place migrate command, deprecation period not viable because the parser surfaces are mutually exclusive (you can't accept both forms without ambiguity in `[capabilities]`).
- **Registry is a piece of infrastructure to operate.** Even though it's just a git repo, it has a maintainer surface (PR review, version curation). Mitigations: GitHub Action for auto-publish, federated registries via config, direct git refs as escape hatches.
- **BLAKE3 isn't standard in the wider WASM/wit ecosystem.** Cargo uses SHA-256, Rustls uses SHA-2, etc. We've picked BLAKE3 for speed; reviewers may push back on consistency. Mitigations: BLAKE3 is well-specified, mature, and faster than SHA-256 for the snapshot-manifest case where input is mostly small files.
- **Heterogeneous wildcards force capsule authors to choose** between `wit = "opaque"` (no schema enforcement) and splitting into concrete topics (verbose, less extensible). Mitigations: opaque is honest; the alternative (a list-of-types) creates parser ambiguity and complicates wildcard matching.
- **WIT type references span repo boundaries.** A capsule's manifest references a WIT record in another repo. If the WIT repo evolves the record incompatibly, every consuming capsule needs a coordinated bump. Mitigations: semver pinning + lockfile + the registry's `yanked` flag for incident response.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why Cargo-shaped instead of incremental cleanup

Each piece of the v0.5 manifest can be defended in isolation. The combined surface — three places to declare topics, a serde-required-but-undocumented field, optional schemas, free-form prose reaching LLMs — is what fails at scale. An incremental cleanup risks adding a fifth surface. A clean break against a well-understood model (Cargo) is faster to land and easier to teach.

Cargo's manifest also solves problems we will hit: workspace inheritance (deferred to v0.7), cross-source dependency resolution, lockfile reproducibility, alt-registries. Adopting the shape now keeps those doors open without redesigning around them later.

## Why git-backed registry instead of a Cargo-style HTTP service

Operating a registry HTTP service has a real per-month cost (uptime, storage, mirror, abuse). A git-backed index inherits GitHub's uptime, transit, and DDoS protection for free, costs nothing to operate, and can be cloned for offline use. The only tradeoff is index size scaling — Cargo's index has dealt with this by sparse-fetching, which we'd inherit if we ever hit the same scale.

## Why BLAKE3 over SHA-256

BLAKE3 hashes the snapshot manifest 6-10× faster than SHA-256, parallelises across files, and is fully specified. The lockfile's hash is the single thing that runs on every install; speed matters. Cryptographic strength is equivalent for our use.

## Why `description_for_llm` instead of letting the operator audit `[[topic]].description`

Forcing the LLM-facing string to a separately-named field means there is a structural difference between strings the LLM might see and strings only humans see. An auditor (or a security tool) can grep for `description_for_llm` and find every potential injection vector in the manifest. With overloaded `description`, the audit requires understanding which downstream pipelines pass which descriptions where. Explicit beats implicit at the contract surface.

## Why path-sources have no checksum

`path = "../shared-wit"` exists for development. The shared-wit directory is mutable in the working tree; pinning a hash means every edit invalidates the lockfile. Path sources are inherently unreproducible; the lockfile records that fact explicitly (`type = "path"`).

## Alternative: leave `[[topic]]` and add `[publish]` / `[subscribe]` alongside

Considered. Rejected because the `[capabilities].ipc_publish` array would still need to encode the same topic patterns (for ACL), creating exactly the four-place duplication this RFC eliminates.

## Alternative: derive direction from WIT imports/exports

Considered. WIT exports/imports describe **interfaces** (function signatures, host syscalls), not bus topics. There is no natural mapping from WIT's import/export model to bus publish/subscribe semantics — they are orthogonal concerns. Conflating them would force every bus topic into a synthetic WIT interface.

# Prior art
[prior-art]: #prior-art

- **Cargo (Rust)**: dependency declarations, lockfile, registry index. The shape of `[publish]` / `[subscribe]` mirrors `[dependencies]`. The registry index repo mirrors `crates.io-index`. The lockfile semantics mirror `Cargo.lock`.
- **npm (Node)**: package scoping (`@scope/name`). The reference syntax `@unicity-astrid/wit/...` adopts the same convention, already used in our distro lock.
- **Go modules**: GitHub-as-source-of-truth for module resolution. Validates that operating no central registry can scale to a large ecosystem.
- **WIT (Component Model)**: one-package-per-file convention with `use <package>/<interface>.{<record>}` cross-package references.
- **MCP (Anthropic)**: tool descriptions as explicit operator-reviewed strings. `description_for_llm` follows the same pattern.
- **Sigstore / Cargo's checksum verification**: the BLAKE3 content-hash mechanism mirrors Cargo's lockfile checksum (which uses SHA-256), Sigstore's transparency logs (which use SHA-256), and any number of supply-chain integrity systems.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **`description_for_llm` length / formatting limits.** Should we cap length to bound the prompt-injection surface? Should we strip control characters? Defer to v1 — adopt limits later if abuse appears.
- **Workspace inheritance** (`[publish.workspace = true]` style) — punt to v0.7. Out of scope.
- **Self-hosted registry mirroring / caching** — punt. Direct git refs cover the offline / mirror case for now.
- **WIT-record version pinning at the entry level.** Today every `wit` ref pins through the source's version (one version per `(scope, repo)`). Should an entry be able to pin a *specific record version* against the rest of the source moving forward? Probably not — it complicates the model — but worth flagging.
- **Registry trust model.** The index is publicly readable and PR-controlled. Compromise of a registry maintainer's GitHub account allows malicious version entries. Mitigation paths (capsule-author signing, transparency log, sigstore-style verification) are future work, not blocking.
- **Heterogeneous wildcard `wit = "opaque"` semantics.** Does the kernel still enforce ACL match on the topic pattern? (Yes — opaque only skips schema verification, not ACL. Worth being explicit in the implementation.)

# Future possibilities
[future-possibilities]: #future-possibilities

- **`[[tool]]` schema enforcement** — verify `input_schema_wit` against actual tool calls at runtime.
- **Workspace inheritance** — `[workspace] members = [...]` for capsule families that share dependencies.
- **Registry mirrors** — read-only mirrors of `unicity-astrid/registry` for offline / regulated environments.
- **Capsule-author signing** — versions in the registry carry a signature from the capsule author; resolver verifies against a known public key per source. Defense against compromised registry maintainers.
- **Transitive WIT dep visualization** — `astrid wit graph` showing the full WIT type graph for a capsule, including all transitive references.
- **`astrid registry publish`** — CLI command that opens the index PR for you (parallel to `cargo publish`).
- **Schema evolution warnings** — `astrid update` flags when a registry update introduces a breaking record change.
- **A2UI integration** — the `[[tool]].description_for_llm` review surface naturally extends to A2UI components a capsule wants to surface to the LLM (operator approves the same way).
