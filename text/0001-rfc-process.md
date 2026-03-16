- Feature Name: `rfc_process`
- Start Date: 2026-03-16
- RFC PR: N/A (bootstrapped)
- Tracking Issue: N/A

# Summary
[summary]: #summary

This RFC establishes the process by which substantial changes to Astrid are
proposed, discussed, and accepted. It covers any change to the contract surface
between the kernel and user-space: the host ABI, IPC protocol, capability model,
manifest schema, VFS semantics, and capsule interface standards. It is the first
RFC and bootstraps itself.

# Motivation
[motivation]: #motivation

Astrid draws a hard line between kernel and user-space. The kernel is native Rust.
Capsules are isolated WASM processes. The contract between them is the host ABI
(`astrid-sys`), the IPC event bus, the capability token format, the manifest
schema, and the VFS path resolution rules. Every capsule author depends on this
contract being stable and well-specified.

A change to any part of this contract surface ripples across the entire ecosystem.
Adding a host function changes `astrid-sys`, which changes `astrid-sdk`, which
changes every capsule that might use it. Changing an IPC payload schema breaks
every capsule on both sides of that topic. Changing the manifest schema changes
what the kernel expects at boot.

Without a formal process:

- Contract changes happen ad-hoc in implementation PRs, buried in code
- Third-party capsule authors have no stable spec to build against
- Breaking changes lack visibility and cross-team review
- The SDK feature flags (`rfc-1`, `rfc-2`) have no corresponding specification

An RFC process gives these contracts a single source of truth that lives outside
any one crate's repository.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## When you need an RFC

You need an RFC for any substantial change to the contract between the kernel and
user-space. This includes:

- **Host ABI changes** - adding, removing, or changing an `astrid_*` host function
  in the syscall table (`astrid-sys`)
- **IPC protocol changes** - new topic naming conventions, payload schema changes,
  new message types
- **Capability model changes** - new capability scopes, changes to token format or
  validation semantics
- **Manifest schema changes** - new fields in `Capsule.toml`, changes to
  dependency resolution or capability declarations
- **VFS semantics changes** - path resolution rules, overlay behavior, new
  filesystem operations
- **Capsule interface standards** - standardized tool schemas, input/output types,
  cross-capsule communication patterns
- **SDK public API changes** - breaking changes to `astrid-sdk` module layout or
  typed wrappers

You do not need an RFC for:

- Bug fixes to existing implementations
- Internal refactoring that preserves the external contract
- Documentation improvements
- Performance optimizations that preserve existing behavior
- Adding a new capsule that implements an existing interface
- Kernel-internal changes that do not cross the ABI boundary

## How to submit

1. Fork the [rfcs](https://github.com/unicity-astrid/rfcs) repository.
2. Copy `0000-template.md` to `text/0000-my-feature.md` (descriptive name,
   do not assign an RFC number).
3. Fill in the RFC. Focus on motivation and the reference-level specification.
4. Open a pull request.
5. Discussion happens on the PR. Revise as needed. Build consensus.
6. When accepted, a maintainer assigns the next sequential RFC number, renames
   the file to `text/NNNN-my-feature.md`, updates the README index, and merges.
7. Implementation proceeds in `astrid-sdk` (types behind feature flags) and
   the relevant kernel or capsule crates.

## Lifecycle

| Status | Meaning |
|--------|---------|
| **Draft** | PR open, under discussion. |
| **Active** | Merged. Being implemented. |
| **Final** | Implemented and stable. Breaking changes require a new RFC. |
| **Withdrawn** | Closed without merge. |
| **Superseded** | Replaced by a newer RFC (noted in header). |

## After acceptance

An accepted RFC is a design document, not a rubber stamp. Implementation may
reveal issues that require amendments. Amendments are submitted as follow-up PRs
to this repository that modify the original RFC file. Substantial changes to an
active RFC should be discussed on a new PR before merging.

Each accepted RFC that defines types maps to an `astrid-sdk` feature flag:

```toml
astrid-sdk = { version = "0.2", features = ["rfc-1"] }
```

The `all-rfcs` feature enables every RFC's types. Not all RFCs produce SDK types
(this one does not, for example).

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## RFC numbering

RFC numbers are assigned sequentially by a maintainer at merge time. Numbers are
never reused. Withdrawn or superseded RFCs retain their number.

The number is not tied to the GitHub PR number. This prevents spam PRs from
burning numbers and keeps the sequence dense.

## File format

RFCs use the template at `0000-template.md`. The header fields are:

| Field | Description |
|-------|-------------|
| Feature Name | A unique `snake_case` identifier. |
| Start Date | The date the RFC was first submitted (YYYY-MM-DD). |
| RFC PR | Link to the pull request(s) where the RFC was discussed. |
| Tracking Issue | Link to the implementation tracking issue in `unicity-astrid/astrid`, if applicable. |

## Required sections

Every RFC must include: Summary, Motivation, Guide-level explanation,
Reference-level explanation, Drawbacks, Rationale and alternatives, Prior art,
Unresolved questions, and Future possibilities.

For RFCs that define interfaces (tools, IPC messages, host functions), the
Reference-level explanation must specify:

- Function signatures or tool schemas with exact semantics
- Input types (JSON schemas with field types, required/optional, constraints)
- Output types (success and error shapes)
- Host function requirements (if any)
- IPC event types and topic patterns (if any)
- Ordering and concurrency guarantees
- Error handling contract

The spec must be precise enough that an independent developer can implement a
conforming component from this section alone.

## Scope boundaries

The RFC process governs the contract surface. To be explicit:

**In scope:**

| Area | Examples |
|------|----------|
| Host ABI | `astrid_fs_read`, `astrid_ipc_publish`, `astrid_request_approval` |
| IPC protocol | Topic naming (`llm.v1.stream.*`), payload schemas (`IpcPayload` variants) |
| Capability model | Token fields, scope patterns, validation rules |
| Manifest schema | `Capsule.toml` fields, dependency declarations, capability prefixes |
| VFS semantics | Path resolution, overlay commit/discard, handle types |
| Capsule interfaces | Standard tool schemas, standard IPC contracts between capsules |
| SDK public API | Module layout, typed wrappers, breaking changes to `astrid-sdk` |

**Out of scope:**

| Area | Why |
|------|-----|
| Kernel internals | Implementation detail, no external contract |
| Capsule internals | Private to the capsule author |
| CLI/frontend UX | Does not cross the ABI boundary |
| CI/tooling | Infrastructure, not contract |

## Merge checklist

When merging an accepted RFC, the maintainer:

1. Assigns the next sequential RFC number.
2. Renames the file from `text/0000-*.md` to `text/NNNN-*.md`.
3. Updates the RFC PR and Tracking Issue fields in the header.
4. Adds an entry to the index table in `README.md`.
5. Commits and merges.

# Drawbacks
[drawbacks]: #drawbacks

- Adds friction to contract changes. Small protocol tweaks now require a PR to a
  separate repository.
- Pre-1.0, the contract surface is still in flux. Requiring RFCs for every change
  could slow iteration.

Both are acceptable trade-offs. Pre-1.0, the bar for acceptance can be lower and
the process lighter. The structure exists so it scales when the community grows.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why not use GitHub issues?** Issues lack the structured format needed for
interface specifications. An RFC as a versioned markdown file provides diffs,
line-level review, and a permanent record.

**Why sequential numbers instead of PR numbers?** Prevents spam from burning
numbers and keeps the index dense. Python PEPs use editor-assigned numbers for
the same reason.

**Why a separate repository?** Keeps specifications decoupled from implementation.
A capsule author should be able to read the spec without navigating the kernel
codebase.

**Why scope to the contract surface, not all changes?** Kernel internals and
capsule internals can move fast without coordination. The contract surface is
where stability matters because it affects everyone on both sides of the boundary.

**Alternative: no formal process.** This is what most agent frameworks do. It
works when one team controls everything. It breaks when third-party developers
need a stable contract to build against.

# Prior art
[prior-art]: #prior-art

- **Rust RFCs** (`rust-lang/rfcs`): The direct inspiration. PR-based, numbered,
  merged into `text/`. Rust covers all substantial language/stdlib changes. We
  narrow scope to the kernel/user-space contract surface. Rust uses PR numbers as
  RFC numbers; we assign on merge.
- **Python PEPs** (`python/peps`): Editor-assigned numbers, similar lifecycle
  states. Heavier process with designated PEP editors.
- **TC39 Proposals**: Stage-based (0-4) with a champion system. More structured
  than needed at Astrid's current scale.
- **IETF RFCs**: The original. Too heavyweight for a single project but the
  naming convention is universal.
- **POSIX**: Standardizes the syscall interface between kernel and user-space.
  Astrid's host ABI is the analogous boundary.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should there be a "Final Comment Period" (FCP) before merge, as Rust uses?
  Currently omitted to keep the process lightweight pre-1.0.
- Should RFC amendments beyond a certain size require a new RFC instead of a
  modification to the original?
- Should there be a formal deprecation process for superseded RFCs?

# Future possibilities
[future-possibilities]: #future-possibilities

- A bot that auto-checks RFC formatting on PR submission.
- A conformance test suite generated from RFC specifications.
- An RFC status dashboard on the documentation site.
- Formal FCP process once the contributor base grows beyond the core team.
