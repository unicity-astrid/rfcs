# Astrid RFCs

This repository tracks RFCs (Request for Comments) for the Astrid agent runtime.

RFCs define standard interfaces, protocols, and type contracts that capsules and
host runtimes implement. They are the authoritative specification - reference
capsule implementations conform to them, not the other way around.

## When you need an RFC

- Defining a new capsule interface (tool schemas, input/output types)
- Changing an existing capsule protocol in a breaking way
- Introducing a new host function or IPC message format
- Standardizing cross-capsule communication patterns

## Process

1. Fork this repo and copy `0000-template.md` to `text/0000-my-feature.md`.
2. Fill in the RFC. Focus on motivation and the reference-level spec.
3. Open a pull request. The RFC number is the PR number.
4. Rename the file to `text/NNNN-my-feature.md` matching the PR number.
5. Discussion happens on the PR. Revise as needed.
6. When consensus is reached, the RFC is merged into `text/`.
7. Implementation proceeds in `astrid-sdk` (types behind feature flags) and
   reference capsules.

## Lifecycle

- **Draft** - PR open, under discussion.
- **Active** - Merged. Types being implemented in `astrid-sdk`.
- **Final** - Implemented and stable. Breaking changes require a new RFC.
- **Withdrawn** - Closed without merge.
- **Superseded** - Replaced by a newer RFC (noted in header).

## SDK Integration

Each RFC maps to an `astrid-sdk` feature flag:

```toml
# Individual RFC types
astrid-sdk = { version = "0.2", features = ["rfc-1"] }

# All RFC types
astrid-sdk = { version = "0.2", features = ["all-rfcs"] }
```

## Index

| RFC | Title | Status |
|-----|-------|--------|
| [0001](text/0001-cli-tool-interface.md) | CLI Tool Interface | Draft |
