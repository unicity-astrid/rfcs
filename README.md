# Astrid RFCs

[![License: MIT OR Apache-2.0](https://img.shields.io/badge/License-MIT%20OR%20Apache--2.0-blue.svg)](LICENSE-MIT)

**Request for Comments for the [Astrid](https://github.com/unicity-astrid/astrid) agent runtime.**

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
3. Open a pull request. Use the filename `0000-my-feature.md` in the PR.
4. Discussion happens on the PR. Revise as needed.
5. When consensus is reached, a maintainer assigns the next sequential RFC
   number, renames the file to `text/NNNN-my-feature.md`, and merges.
6. Implementation proceeds in `astrid-sdk` (types behind feature flags) and
   reference capsules.

## Lifecycle

- **Draft** - PR open, under discussion.
- **Active** - Merged. Types being implemented in `astrid-sdk`.
- **Final** - Implemented and stable. Breaking changes require a new RFC.
- **Withdrawn** - Closed without merge.
- **Superseded** - Replaced by a newer RFC (noted in header).

## SDK integration

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
| | No RFCs accepted yet. | |

## License

Dual-licensed under [MIT](LICENSE-MIT) and [Apache 2.0](LICENSE-APACHE).

Copyright (c) 2025-2026 Joshua J. Bouw and Unicity Labs.
