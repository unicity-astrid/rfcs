- Feature Name: `pty_process_attachment`
- Start Date: 2026-06-11
- RFC PR: [rfcs#33](https://github.com/unicity-astrid/rfcs/pull/33)
- Tracking Issue: [astrid#0000](https://github.com/unicity-astrid/astrid/issues/0000)

# Summary
[summary]: #summary

Add PTY-backed host-owned processes plus a local file-descriptor-handover
attachment contract to `astrid:process@1.0.0`. A capsule may spawn a child with
a real controlling terminal; the kernel allocates the PTY pair, sandboxes the
child, audits it, and keeps it alive independently of any attached terminal. An
uplink (the CLI) then attaches to that PTY by receiving a duplicate of the
master file descriptor over its already-authenticated Unix-domain socket, and
pumps bytes directly between the user's terminal and the child — no per-keystroke
traffic crosses the IPC bus or the WASM guest. This is the interactive byte path
that lets Astrid own a full-screen TUI program (for example an external coding
agent) while retaining full lifecycle, capability, quota, and audit ownership of
the process.

All additions are optional and additive. Existing guests built against the prior
`astrid:process@1.0.0` shape are unaffected.

# Motivation
[motivation]: #motivation

Astrid's strategic position is that **the daemon owns the agent process**. A
spawned program runs inside the OS sandbox (`bwrap` on Linux,
`sandbox-exec`/Seatbelt on macOS), is gated by the spawning capsule's
capabilities and the principal's quotas, is recorded in the audit chain, and —
on the persistent tier — survives the detachment of whatever terminal launched
it. That ownership is the whole security argument: a capability-gated, audited,
reapable process is categorically safer than a binary the user shelled out to by
hand.

Two existing pieces almost get us there, and one gap remains.

- **Capsule-contributed CLI verbs** (astrid#891) let a capsule register
  noun-verb subcommands that the CLI proxies to the daemon. These cover
  non-interactive one-shots: the verb runs, produces output, and returns.
- **The persistent process tier** (`spawn-persistent`, RFC `host_abi`) already
  gives a host-owned, principal-scoped, reattachable process lifetime: start in
  one invocation, read/write/stop from later invocations, survive pooled-instance
  churn.

What is missing is an **interactive byte path**. A full-screen TUI program needs
a real controlling terminal, not a byte pipe. Pipes cannot carry termios state,
cannot put the terminal in raw mode, cannot do cursor addressing, and have no
window-size channel for `SIGWINCH`. Programs that drive the screen directly — an
editor, a pager, a REPL with line editing, or an external coding agent — detect
the absence of a TTY and either degrade to a dumb line mode or refuse to run.
The persistent tier gives such a program a durable lifecycle; this RFC gives it a
terminal.

## Why not let the uplink exec the binary directly

The obvious shortcut is to have the capsule tell the CLI "run this binary
yourself" — the CLI already owns the user's TTY, so it could `exec` the program
with no PTY plumbing at all. **This is rejected.** The uplink runs *unsandboxed,
as the user*. A capsule that can instruct the uplink to execute an arbitrary
binary is a confused-deputy bypass of the entire sandbox: the capability gate,
the quota ledger, the OS sandbox, and the audit chain all sit on the daemon side
of the socket, and an uplink-exec'd process touches none of them. The daemon
would never own, contain, or audit the very process Astrid claims to govern. The
whole point of this RFC is to keep the process on the daemon's side of the trust
boundary and hand the uplink only an interactive *view* of it — a duplicated PTY
master fd — never the authority to spawn.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Consider an `agent` capsule that boots an external coding-agent binary under
Astrid. The capsule registers a CLI verb (astrid#891) so the operator can type
`astrid agent start`. We walk the full lifecycle: spawn with a terminal, attach,
detach, reattach.

## Declaring the verb and spawning with a terminal

The capsule declares its verb in `Capsule.toml` as a `cli` command and
implements the handler. When the handler runs, it spawns the agent on the
persistent tier and asks for a controlling terminal by setting the new
`terminal` field on the spawn request:

```rust
// Inside the `agent start` verb handler (capsule guest code, via the SDK).
let id = astrid::process::spawn_persistent(SpawnRequest {
    cmd: "coding-agent".into(),
    args: vec![],
    terminal: Some(TerminalConfig { rows: 40, cols: 120 }),
    keep_stdin_open: Some(true),
    label: Some("coding-agent".into()),
    ..Default::default()
})?;

// Return the process id to the CLI in the command result so the proxy
// knows what to attach to.
CommandResult::ok().with_process_id(&id)
```

The host allocates a PTY pair, places the child in a fresh session (`setsid`)
with the PTY **slave** as its controlling terminal and as stdin/stdout/stderr,
and wraps the whole thing in the OS sandbox exactly as it would a pipe-based
child. The capsule gets back the same opaque, principal-scoped `process-id` it
would for any persistent spawn. Everything the persistent tier already offers
keeps working unchanged: `status`, `list-processes`, `read-since`, `watch`,
`stop`, quotas, idle-timeout reaping. The only difference is that the child's
output now arrives as a single merged terminal stream.

A subtlety worth internalising up front: **attachment is optional**. Because the
PTY output feeds the same ring buffers as a pipe, a capsule can drive a PTY-backed
process entirely programmatically — `read-since` to observe the screen bytes,
`write-stdin` to type, `resize` to change the window — without any terminal ever
attaching. Attachment exists solely to put a *human's* terminal on the other end
of the PTY at native latency. A purely machine-driven interaction never needs it.

## Attaching the operator's terminal

The CLI receives the process id in the verb's command result and decides to give
the operator an interactive session. It asks the uplink proxy (the capsule that
owns the CLI socket) to perform the attachment. The proxy calls:

```rust
// Inside the uplink proxy capsule, over the CLI's connected socket `stream`.
astrid::process::attach_terminal(&id, &stream)?;
```

The host duplicates the PTY master fd and sends that single fd to the socket peer
over `SCM_RIGHTS`. The CLI process — which the OS has already proven is the same
user (peer-credential check) and which completed the session-token handshake when
it connected — receives the fd, puts its own terminal in raw mode, and pumps
bytes both ways: keystrokes into the master, screen output out of it. From this
point the interactive traffic flows **directly between the CLI process and the
PTY master**, with the daemon no longer in the byte path and the WASM guest never
touching a keystroke. The operator is talking to the coding agent as if they had
run it themselves — except Astrid spawned, sandboxed, and is auditing it.

When the operator presses a resize / the terminal emits `SIGWINCH`, the CLI calls
back through the proxy to `resize(id, rows, cols)`, and the host applies the new
window size to the master and delivers `SIGWINCH` to the child.

## Detaching and reattaching

Detach is simply the peer closing its copy of the fd: the CLI drops out of raw
mode and closes the duplicated descriptor. **The child keeps running.** The
daemon still owns the master, the ring buffers keep filling, `watch` events keep
flowing, quotas and idle timeouts keep applying — the process is exactly as alive
as any other persistent process with no reader.

Later, the operator runs `astrid agent attach` (or simply reconnects). The CLI
reconnects its socket, the verb handler calls `list-processes` to find the agent
by label, recovers its `process-id`, and the proxy issues a fresh
`attach-terminal` against the still-running child. Each attach is an independent
dup of the master, so reattachment is just another handover. Multiple sequential
attaches over the life of one child are expected and supported.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

The canonical signatures live in
[`unicity-astrid/wit`](https://github.com/unicity-astrid/wit) as
`host/process@1.0.0.wit`. This RFC specifies the design; if this RFC and the WIT
disagree, the WIT is correct.

## Compatibility and the choice to fold into `1.0.0`

These additions are folded into the existing `astrid:process@1.0.0` package
rather than shipped as `@1.1.0`, following the precedent set for the read-only
file-injection primitive in RFC `host_abi`: while the host ABI is
pre-stabilisation and no third-party capsule has shipped against a frozen
`@1.0.0` file, additive shape growth lands in place under the end-to-end
coordinated-sweep discipline. Every addition here is either a new optional
`option<...>` field on an existing record or a brand-new top-level function.
Adding an `option` field and adding a function are both structurally backward
compatible under Component Model linking for *callers* — a guest built against
the prior shape that never names the new symbols instantiates and runs unchanged.
Once a real downstream consumer ships against the frozen file, this discipline
flips to the immutable-file rule (changes ship as `@1.1.0`); this RFC is written
on the assumption that has not yet happened.

## 1. `terminal` on `spawn-request`

`spawn-request` gains one optional field and one supporting record:

```wit
/// Window geometry for a PTY-backed spawn. When `spawn-request.terminal` is
/// set, the host allocates a pseudoterminal pair and gives the child the
/// slave as its controlling terminal.
record terminal-config {
    /// Initial terminal height in character rows.
    rows: u16,
    /// Initial terminal width in character columns.
    cols: u16,
}

record spawn-request {
    // ... all existing fields unchanged ...

    /// Request a controlling terminal for the child. `none` => the child gets
    /// pipe-based stdio (the existing behaviour). `some` => the host allocates
    /// a PTY pair, `setsid`'s the child into a fresh session, and wires the
    /// PTY slave to the child's stdin/stdout/stderr as its controlling
    /// terminal. Valid only on `spawn-background` and `spawn-persistent`;
    /// rejected with `invalid-input` on the synchronous `spawn`.
    terminal: option<terminal-config>,
}
```

When `terminal` is set the host:

1. Allocates a host-owned PTY master/slave pair.
2. Spawns the child under the OS sandbox (unchanged) with the **slave** as
   stdin, stdout, and stderr, in a fresh session (`setsid`) with the slave as
   its controlling terminal.
3. Retains the **master** in the host registry alongside the process entry.

**Merged stream.** A PTY has a single output channel: stdout and stderr are
delivered interleaved on the master and are not separable. This is inherent to
pseudoterminals, not a choice. Consequently, for a PTY-backed process the host
feeds the merged master output into the **same ring-buffer machinery** used for
pipe-based children. `read-logs`, `read-since`, and `watch` all operate
unchanged on this merged stream; `write-stdin` writes to the **master**, which
is exactly equivalent to a human typing at the terminal. Readers that key on
`log-stream::stderr` will simply find that stream empty for a PTY process — all
output is on `stdout`. The merged-stream property is the price of a real terminal
and is documented as such.

## 2. Tier validity

`terminal` is honoured on `spawn-background` (instance-owned `process-handle`)
and `spawn-persistent` (host-owned `process-id`). On the synchronous `spawn` it
is **rejected with `invalid-input`**: a blocking one-shot has no interactivity —
there is no later invocation to attach from and no concurrent reader to drive it —
so requesting a terminal there is a programming error, surfaced as the existing
invalid-input code rather than silently ignored.

## 3. `resize`

```wit
/// Resize the controlling terminal of a PTY-backed persistent process: apply
/// the new window geometry to the PTY master (TIOCSWINSZ) and deliver SIGWINCH
/// to the child so it can re-layout. Needed for capsule-mediated control and
/// for any transport that drives the terminal without holding the master fd
/// directly. `no-such-process` if the id is unknown to the caller (same
/// non-oracle collapse as every id-keyed call); `invalid-input` if the process
/// is not PTY-backed.
resize: func(id: process-id, rows: u16, cols: u16) -> result<_, error-code>;
```

Errors:

- `no-such-process` — unknown id, wrong principal, wrong capsule, or reaped
  (collapsed, no oracle, exactly as the other id-keyed functions).
- `invalid-input` — the target is a live process of the caller's but was spawned
  without `terminal` (not a PTY).

`resize` is an id-keyed persistent-tier function and re-resolves the calling
principal/capsule against the recorded creator before acting, like every other
id-keyed call. It is audited (op + principal + capsule + id-hash).

## 4. `attach-terminal` — the fd handover

```wit
/// Hand the PTY master of a persistent process to a connected socket peer so
/// that peer (a local uplink, e.g. the CLI) can drive the terminal directly.
/// The host duplicates the PTY master file descriptor and sends the single
/// dup'd fd to the peer over `SCM_RIGHTS` (one ancillary message) on the given
/// connected Unix-domain stream. After handover the peer pumps bytes between
/// its own terminal and the master with zero per-keystroke traffic through the
/// bus or the WASM guest. The host RETAINS the master and full lifecycle
/// ownership; the peer holds an independent duplicate that dies when the peer
/// process exits or closes it.
///
/// Gates (all fail closed):
///   - the caller MUST hold the `uplink` capability (this exfiltrates an fd to
///     a socket peer; only the socket-owning proxy may perform it);
///   - `principal` is the principal the uplink asserts it is acting for (the
///     authenticated principal of the requesting socket client), following the
///     `publish-as` trust precedent: the host validates the string as a
///     principal id and checks it against the process owner. The target
///     process MUST be owned by exactly that principal. Note this is
///     deliberately NOT the creator-capsule re-check used by the other
///     id-keyed calls — the proxy is never the creating capsule (the agent
///     capsule spawns; the proxy attaches), and the proxy's own effective
///     principal is its load-time owner, not the client it bridges. The
///     principal boundary is the security boundary here; capsule-creator
///     scoping continues to apply unchanged to every other id-keyed call;
///   - the target MUST be PTY-backed and still running;
///   - `stream` MUST be an AF_UNIX (Unix-domain) stream — SCM_RIGHTS fd passing
///     is AF_UNIX-only; a TCP stream is rejected.
///
/// Errors: `capability-denied` (no `uplink`); `invalid-input` (malformed
/// `principal`, not PTY-backed, child already exited, or `stream` is not a
/// Unix-domain socket); `no-such-process` (unknown id, owner mismatch, or
/// reaped — one uniform error, no oracle). Every successful attach emits an
/// audit entry (process id, asserted principal, capsule id).
attach-terminal: func(id: process-id, stream: borrow<tcp-stream>, principal: string) -> result<_, error-code>;
```

Semantics:

- **The fd is a duplicate.** The host `dup`s the master and sends the copy; the
  daemon's own master fd is untouched. The peer's copy is an independent open
  file description in the peer's process — closing it (detach, or peer exit)
  severs the peer's view and nothing else.
- **`borrow<tcp-stream>`.** The attach target stream is the same `tcp-stream`
  resource that `astrid:net`'s `unix-listener.accept` yields for an accepted
  Unix-domain connection. The host inspects the underlying socket and rejects it
  with `invalid-input` if it is not AF_UNIX. (See *Unresolved questions* for the
  cross-interface resource dependency this introduces.)
- **Sequential reattach is supported; concurrent attach is permitted but
  discouraged.** Each `attach-terminal` is an independent dup, so the operator
  can detach and reattach freely over the child's life. Two peers attached at
  once both read and both write the same master: this is *last-resize-wins* (the
  most recent `resize` sets the window) and input from the two peers interleaves
  byte-by-byte at the master. The host does not legislate against concurrent
  attach — it is occasionally useful (a shared session) — but capsule and uplink
  authors should treat it as a sharp edge, not a feature, and serialise attach in
  the common case.
- **Audit.** Every successful handover is recorded. Because the fd then leaves
  the daemon's audit horizon, the audit entry marks the *moment of delegation*;
  the bytes that subsequently flow over the duplicated fd are, by construction,
  not individually audited (they never re-enter the daemon). This is an explicit
  property of the design, not an oversight — see *Security model*.
- **Lifecycle is unchanged by attachment.** Reaping, ring buffers, quotas, idle
  timeouts, and `watch` events all proceed exactly as for an unattached
  persistent process. Attachment is a read/write *view*; it confers no ownership.

## 5. Transport specificity (explicit non-goal)

`attach-terminal` is inherently **local**: `SCM_RIGHTS` fd passing only works
between processes on the same host over an AF_UNIX socket. A **remote** uplink
(a web frontend, a Discord bridge) cannot receive a PTY fd and does not try to.
The remote interactive path is the *already-existing* bus surface:
`read-since` (or `watch` + `read-since`) to stream the merged terminal output
outward and `write-stdin` to stream keystrokes inward, with `resize` for window
changes. A streaming convenience wrapper that packages that loop into a single
bus subscription is plausible and useful, but it is **out of scope** for this
RFC — it is a pure capsule-space concern built on the primitives above and needs
no host ABI change.

## Error handling summary

No new `error-code` variant is introduced. All new failure modes reuse the
existing process error vocabulary:

| Condition | Error |
|---|---|
| `terminal` set on synchronous `spawn` | `invalid-input` |
| `attach-terminal` caller lacks `uplink` | `capability-denied` |
| `attach-terminal` asserted `principal` is malformed | `invalid-input` |
| `attach-terminal` id unknown / owner ≠ asserted principal / reaped | `no-such-process` |
| any other id-keyed call: id unknown / wrong owner / wrong capsule / reaped | `no-such-process` |
| `resize` / `attach-terminal` target is not PTY-backed | `invalid-input` |
| `attach-terminal` target child already exited | `invalid-input` |
| `attach-terminal` stream is not AF_UNIX (e.g. a TCP stream) | `invalid-input` |

# Security model
[security-model]: #security-model

The fd handover crosses a trust boundary, so the gate chain is the load-bearing
part of this design. It composes four independent checks, all fail-closed.

## The chain

1. **The socket peer is the same user.** A connection reaches the CLI uplink only
   after the daemon's accept path verifies, via `SO_PEERCRED` /
   `getpeereid`-equivalent, that the connecting process runs as the **same UID**
   as the daemon, and after a **session-token handshake** over that connection.
   The OS itself has proven the peer is the same user before any capsule logic
   runs. The fd we hand over therefore goes to a process the kernel already
   guarantees is the user we are acting for.
2. **Only the uplink proxy can request handover.** `attach-terminal` requires the
   `uplink` capability. In the deployed fleet exactly one capsule holds `uplink`
   and owns the CLI socket: the uplink proxy. No ordinary capsule — not the
   `agent` capsule that spawned the process, not any tool capsule — can call
   `attach-terminal`. This confines fd exfiltration to the single component whose
   entire job is bridging the authenticated socket.
3. **The process belongs to the asserted principal.** `attach-terminal` takes
   the principal the uplink is acting for as an explicit parameter — the same
   trusted-uplink assertion model as `publish-as` — and the host checks it
   against the recorded process owner. The parameter is required because the
   proxy cannot rely on its own invocation identity for this check: it is a
   run-loop capsule (its effective principal is its load-time owner, not the
   client it bridges) and it is never the creating capsule (the agent capsule
   spawns; the proxy attaches). The principal boundary, not the creator-capsule
   boundary, is the security boundary for attachment; an owner mismatch
   collapses to `no-such-process` with no oracle. When per-connection principal
   binding moves into the kernel (astrid#852), the asserted parameter can be
   replaced by the kernel's own record of the connection's bound principal —
   the gate's shape is forward-compatible with that upgrade.
4. **Audit.** The handover is recorded (process id, owner principal, capsule id)
   at the moment of delegation.

The net effect: an fd that grants live read/write to a sandboxed child crosses
only from the daemon to a process the OS has already proven is the same user,
only via the one capability-holding proxy, only for that user's own process, and
only with an audit record.

## PTY and sandbox interaction

The child runs under `SandboxCommand` (`bwrap` on Linux, `sandbox-exec` +
Seatbelt on macOS) with the PTY **slave** as its stdio, identically to a
pipe-based child — the sandbox profile is unchanged, and the slave is just
another fd the sandboxed process inherits.

The one PTY-specific hazard is **terminal input injection** (`TIOCSTI` and
friends): a process holding a controlling terminal can, on some configurations,
push characters into that terminal's input queue as if typed. The mitigation is
structural and already present: the PTY pair is **host-allocated and dedicated to
this one child**. The child's controlling terminal is *its own* freshly created
slave, not the operator's real terminal and not any parent's terminal. There is
no shared tty for the child to inject into — the operator's terminal is on the
far side of the duplicated *master* fd, in a different process, never made the
child's controlling terminal. `setsid` placing the child in a fresh session
reinforces this: the child cannot reach back to a parent session's terminal. On
Linux the new-session / namespace posture of the sandbox is consistent with this
isolation.

## Windows is out of scope

Windows has neither AF_UNIX `SCM_RIGHTS` fd passing nor PTY parity in the form
this design assumes. ConPTY is the eventual path to terminal support on Windows
and is **explicitly deferred** (see *Future possibilities*). On Windows,
`terminal` spawns and `attach-terminal` are not available; the `astrid:process`
package is desktop-kernel-only to begin with.

## Threat model — concrete abuses

- **A capsule attaches someone else's process.** Blocked by the owner check
  (gate 3): the asserted principal must match the process owner, and only the
  `uplink`-holding proxy can assert a principal at all (gate 2). For any other
  capsule the call dies at `capability-denied`; for the proxy asserting the
  wrong principal the id resolves to `no-such-process`.
- **A compromised proxy asserts an arbitrary principal.** True — and identical
  in scope to the existing `publish-as` posture: the proxy is already the
  component trusted to assert client principals per message, it is gated by the
  `uplink` capability, and every assertion is audited. Kernel-owned
  per-connection binding (astrid#852) is the planned upgrade that shrinks this
  trust to one assertion per connection.
- **A non-uplink capsule reaches `attach-terminal`.** Blocked by the `uplink`
  capability gate (gate 2): `capability-denied`. The `agent` capsule that owns
  the process *still cannot* hand its fd out — it lacks `uplink` — which is
  intentional: spawning authority and fd-exfiltration authority are deliberately
  held by different capsules.
- **A capsule tries to pass the fd to a remote/TCP peer.** Blocked by the AF_UNIX
  check (`invalid-input`): SCM_RIGHTS is meaningless over TCP and the host refuses
  it rather than failing obscurely.
- **fd leak lifetime.** The duplicated fd lives only as long as the peer process
  that holds it; on peer exit or close it is reclaimed by the OS. The daemon's
  master is never handed out, so the daemon retains the canonical handle and the
  child stays daemon-owned and reapable regardless of how many peers attached or
  how they died.
- **A leaked `process-id`.** Possession of the id is necessary but not sufficient:
  the creator re-check means a token that crosses the principal boundary is inert,
  exactly as for every other id-keyed call. A leaked id does not let an attacker
  attach a terminal.

# Drawbacks
[drawbacks]: #drawbacks

- **Platform-divergent surface.** PTY allocation and semantics differ between
  macOS and Linux (and are absent on Windows). The contract is uniform but the
  host implementation is genuinely two code paths, and subtle behaviours (signal
  delivery on master close, slave hangup semantics) differ per OS.
- **Merged stdout/stderr.** A PTY-backed process loses the stdout/stderr
  distinction. A capsule that relies on separating the two streams must spawn
  without `terminal` and forgo interactivity. This is inherent to PTYs but is a
  real expressiveness loss versus the pipe tier.
- **fd handover is untestable on non-Unix CI.** The `SCM_RIGHTS` path cannot be
  exercised on a Windows runner and is awkward to exercise in fully hermetic
  sandboxes. Coverage leans on the Linux/macOS matrix, and the handover step in
  particular needs integration tests with a real connected socket pair rather
  than unit tests.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why this design.** It keeps the process on the daemon's side of the trust
boundary — spawned, sandboxed, quota-gated, audited, and reapable — while giving
the operator a native-latency interactive view. The fd handover is the minimal
delegation that achieves zero per-keystroke overhead: a single dup'd descriptor,
handed once, over a socket the OS has already authenticated. Everything else
(lifecycle, observation, programmatic drive) reuses machinery the persistent tier
already provides.

**Alternative: uplink exec's the binary.** Rejected as a confused-deputy bypass
of the sandbox (detailed in *Motivation*). The uplink is unsandboxed and runs as
the user; letting a capsule direct it to exec a binary moves the process entirely
outside everything Astrid claims to govern.

**Alternative: bus byte-streaming as the only path.** Stream every keystroke and
every screen update as bus events through the WASM guest. Rejected as the
*primary* path on latency grounds: a full-screen TUI updates the screen on every
keystroke, and round-tripping each byte through the IPC bus and a WASM
instance — with the pool checkout, audit, and serialisation that entails —
imposes per-keystroke overhead a human notices. It is, however, retained as the
**remote** fallback (§5): for a non-local uplink that cannot receive an fd, the
bus path is the only option, and the merged-stream-over-ring-buffer design makes
it work without any additional host surface.

**Alternative: a separate `astrid:pty@1.0.0` interface.** Rejected. A PTY-backed
process has an *identical lifecycle* to an existing persistent process — same
registry, same `process-id`, same `status`/`watch`/`stop`/quota/idle-timeout
machinery. Only the byte path differs (PTY master vs pipe). A separate package
would duplicate the entire persistent-tier surface to change one stdio detail.
Folding `terminal` into `spawn-request` and adding two functions reuses
everything and keeps a single source of truth for process lifecycle.

**Impact of not standardising.** Without this, interactive agents can only be run
by the uplink shelling out directly (the rejected confused-deputy path) or not at
all under Astrid. The strategic goal — Astrid owning the agent process — is
unreachable for any program that needs a terminal.

# Prior art
[prior-art]: #prior-art

- **`ssh` / `mosh` PTY allocation.** `ssh -t` allocates a remote PTY and
  forwards it over an authenticated channel — the same shape as this RFC (host
  owns the PTY, client drives it remotely), with the channel here being a local
  fd handover rather than an encrypted network stream.
- **`tmux` / `screen`.** A server process owns the PTY and outlives any attached
  client; clients attach and detach without killing the program. This RFC's
  detach/reattach semantics are deliberately the same — the daemon plays the
  `tmux` server role, the CLI plays the client.
- **`systemd` socket / fd passing.** Passing file descriptors over `SCM_RIGHTS`
  between a manager and a worker is a long-established Unix pattern; this RFC
  applies it to a PTY master.
- **Docker / containerd `exec -it`.** A container runtime allocates a PTY for an
  exec'd process and proxies it to the client. The runtime, like Astrid, retains
  lifecycle ownership; the client gets an interactive view. The difference is that
  Astrid hands a local fd rather than proxying bytes through the daemon, trading
  remote-reachability (covered by the bus fallback) for zero local overhead.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **WIT representation of the attach target.** This RFC proposes
  `attach-terminal(id, stream: borrow<tcp-stream>, principal)`, reusing the `tcp-stream`
  resource that `astrid:net`'s `unix-listener.accept` already yields for accepted
  Unix-domain connections. This introduces a **cross-interface resource
  dependency**: `astrid:process` would `use astrid:net/host.{tcp-stream}`. That
  dependency must be confirmed at the WIT level (Component Model resource sharing
  across host packages, and the kernel's ability to recover the underlying socket
  from a borrowed handle of another package's resource). The alternative is an
  **opaque stream-token** minted by the uplink proxy and passed as a plain
  string/`u64`, which avoids the cross-package resource `use` at the cost of a
  side channel for the proxy to register the socket with the host. The proposal
  is `borrow<tcp-stream>`; the cross-interface dependency is the open item.
- **`TERM` and terminal environment.** `terminal-config` carries only geometry
  (`rows`, `cols`). Whether the `TERM` string (and other terminal-related
  environment such as `COLORTERM`) should be a field on `terminal-config` or left
  to the existing `env` mechanism on `spawn-request` is open. The proposal is to
  **use the existing `env` mechanism** — `TERM` is ordinary environment and the
  spawn request already carries operator-reviewable env — and to *not* add a
  field. Noted here in case a future need for host-validated terminal metadata
  argues otherwise.

# Future possibilities
[future-possibilities]: #future-possibilities

- **A bus streaming wrapper for remote uplinks.** A capsule-space convenience that
  packages `watch` + `read-since` + `write-stdin` + `resize` into a single
  subscription, so a web or Discord frontend gets an interactive terminal over the
  bus with the same ergonomics as a local attach. Pure capsule-space; no host ABI
  change (§5).
- **ConPTY on Windows.** A Windows terminal path via ConPTY plus a non-`SCM_RIGHTS`
  handle-duplication mechanism (`DuplicateHandle` across processes) would bring
  `terminal` spawns to Windows. Substantial and explicitly deferred.
- **Recording / replay.** Because the merged terminal stream already flows through
  the ring buffers and `read-since`, an `asciinema`-style session recording of an
  attached agent is a natural capsule-space feature with no new host surface.
- **Separate-stream PTY variants.** Some platforms support splitting stderr off a
  PTY via a second fd; if a concrete need arises, a future `terminal-config`
  option could expose it. Out of scope now — the merged stream is the honest
  default.
