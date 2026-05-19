- Feature Name: `net_connect_tcp`
- Start Date: 2026-05-19
- RFC PR: [rfcs#0000](https://github.com/unicity-astrid/rfcs/pull/0000)
- Tracking Issue: [astrid#745](https://github.com/unicity-astrid/astrid/issues/745)

# Summary
[summary]: #summary

Add a single outbound TCP host function — `astrid:capsule/net.net-connect-tcp(host, port) -> stream-handle` — capability-gated against a per-capsule `net_connect` allowlist in `Capsule.toml`. Reuses the existing stream-handle plumbing (`net-read` / `net-write` / `net-close-stream` already in production for the Unix accept path), the existing SSRF airlock (`is_safe_ip`) already used by `http-request`, and the existing per-capsule active-streams cap. The SDKs expose a `std::net::TcpStream`-shaped facade on top — capsule authors write `astrid_sdk::net::TcpStream::connect("host:port")` and get `Read + Write` for free; the JS SDK exposes the same primitive as `astrid.net.connect(host, port)` with `AsyncIterable<Uint8Array>` ergonomics. WebSocket and every higher protocol stays in user-space; the host gives capsules the same primitive `std::net::TcpStream` gives a Rust binary, no more.

# Motivation
[motivation]: #motivation

The host ABI today has two network primitives and a gap between them.

1. **`astrid:capsule/http`** — buffered and streaming HTTP fetch (`http-request`, `http-stream-start`). Request/response shaped: one round-trip, server-initiated payloads only inside the body of a single response. No way to send a second frame after receiving the first.

2. **`astrid:capsule/net`** — inbound Unix-socket accept loop (`net-bind-unix`, `net-accept`, `net-read`, `net-write`, `net-close-stream`). Built for the daemon's CLI proxy uplink: kernel pre-provisions one listener, the capsule accepts client connections, reads framed messages, writes framed responses. Inbound only; the connect side does not exist.

The gap is **outbound persistent TCP**. Every capsule that needs to hold a long-lived bidirectional connection to a remote host has no path:

| Workload | Transport | Status today |
|---|---|---|
| WebSocket client (Fulcrum JSON-RPC, Nostr relays, Discord gateway) | persistent TCP + RFC 6455 framing | **blocked** |
| Postgres / Redis / MQTT / IRC | persistent TCP | **blocked** |
| TLS connection to anything not behind a request/response server | TCP + TLS | **blocked** |
| One-shot HTTP GET / POST / streaming response | TCP + HTTP/1.1 or HTTP/2 | works via `http-request` / `http-stream-start` |
| Inbound socket from a frontend uplink | Unix socket | works via `net-accept` |

The immediate forcing function is a JS/TS capsule porting [openclaw-unicity](https://github.com/unicitynetwork/openclaw-unicity) — Unicity wallet + Nostr DMs over [Sphere SDK](https://github.com/unicity-sphere/sphere-sdk). It needs:

- **Fulcrum WebSocket** for ALPHA blockchain JSON-RPC (every wallet / L1 op).
- **Nostr relays** for NIP-29 groups + NIP-17 gift-wrapped DMs (channel uplink, messaging, payment requests, swaps).

Both are persistent WebSocket over TCP. Without `net-connect-tcp` the only landing options are:

- **Reduced scope** — drop the 12 of 15 Sphere tools that need WebSocket. Throws away most of the value.
- **Tier-2 sidecar** — spawn a Node subprocess via `process.spawnBackground`. Re-creates OpenClaw Tier 2 with extra wrapping, leaks WASM-sandbox isolation, and forks the runtime story for capsules that want long-lived connections (some capsules in WASM, some out-of-process).

Both are workarounds for a missing primitive. The cost of *adding* the primitive is one host function and ~50 lines of capability-parser code; the cost of *not* adding it is duplicating Tier 2 every time a capsule needs anything other than HTTP.

The architectural argument is straightforward — TCP is exactly the level of abstraction the host ABI should expose. WebSocket is RFC 6455 framing (~500 LOC of JS or Rust) over TCP; Discord gateway is a framed text protocol over TCP; MQTT is a binary framing format over TCP. Putting any one of those in the host ABI would couple a wire protocol to the kernel and force every other persistent-network protocol to either piggyback on a mismatched shape or wait for its own host function. Putting TCP in the host ABI matches what `std::net::TcpStream` does for a Rust binary: a connect, a read, a write, a close. Everything else builds on top in user-space.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

## Declaring the capability

A capsule that needs outbound TCP declares its destinations in `Capsule.toml`:

```toml
[capabilities]
net_connect = [
  "fulcrum.unicity.network:443",   # exact host + exact port
  "relay.unicity.network:*",       # exact host, any port
  "*.nostr.example:443",           # any subdomain, exact port (future, see Unresolved questions)
]
```

The allowlist is **fail-closed**: a capsule with `net_connect` omitted or empty cannot open any outbound TCP connection. Patterns are matched on the literal `host:port` pair the capsule passes to `connect`; the host resolves DNS *after* the capability check passes, so the capability surface stays declarative.

`net_connect` is independent of `net_bind` (inbound Unix listener), `http` (one-shot fetch / streaming response), and `host_process` (subprocess airlock override). A capsule that needs both inbound and outbound declares both.

## From a Rust capsule

The SDK exposes a `std::net::TcpStream`-shaped facade:

```rust
use astrid_sdk::net::TcpStream;
use std::io::{Read, Write};

let mut sock = TcpStream::connect("fulcrum.unicity.network:443")?;
sock.write_all(b"GET / HTTP/1.0\r\nHost: fulcrum.unicity.network\r\n\r\n")?;

let mut buf = vec![0u8; 4096];
let n = sock.read(&mut buf)?;
// sock dropped at scope end → host closes stream
```

`TcpStream` implements `std::io::Read` and `std::io::Write` directly. WebSocket libraries that take any `Read + Write` (e.g. `tungstenite::client_with_config`) work unmodified. The low-level handle is still available for non-std code:

```rust
let handle: StreamHandle = astrid_sdk::net::connect("host", 443)?;
astrid_sdk::net::send(&handle, b"...")?;
let frame: Vec<u8> = astrid_sdk::net::recv(&handle)?;
astrid_sdk::net::close(&handle)?;
```

`connect` is the same call shape as the existing `accept` — both return a `StreamHandle` that flows through the same `send` / `recv` / `try_recv` / `close` surface. A capsule that mixes inbound and outbound connections handles them uniformly.

## From a JavaScript / TypeScript capsule

```typescript
import { connect } from '@astrid-os/sdk/net';

const sock = await connect('fulcrum.unicity.network', 443);

await sock.write(new TextEncoder().encode('hello'));

for await (const chunk of sock) {
  console.log(decoder.decode(chunk));  // chunk: Uint8Array
}
// `for await` exits → sock auto-closed
```

The `Stream` returned implements `AsyncIterable<Uint8Array>` so `for await` reads frame-by-frame, plus an explicit `write` and `close`. A `node:net` shim — about 200 lines, shipped as a follow-up — wraps `astrid.net.connect` in the `node:net.Socket` surface, which is what every `npm` library that talks raw TCP expects. That single shim makes the npm `ws` library work unmodified on Astrid.

## Capability rejection

A capsule that calls `connect("evil.com", 80)` without a matching `net_connect` entry gets a clear synchronous error:

```
HostError: net.connect-tcp denied: "evil.com:80" not in capsule's net_connect allowlist
```

A capsule with a matching entry that tries to connect to a private / loopback / link-local destination (the host's DNS resolver returns `127.0.0.1`, `10.0.0.5`, `169.254.169.254`, …) gets:

```
HostError: net.connect-tcp denied: resolved IP 127.0.0.1 is in a private/loopback/link-local range (SSRF protection)
```

This mirrors the airlock already running on `http-request`. The capability check and SSRF check both fire before any TCP `connect` syscall, so a misconfigured capsule cannot probe the host's internal network.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## WIT change

Add one function to the canonical `astrid:capsule/net` interface (`host/astrid-capsule.wit` in `unicity-astrid/wit`):

```wit
interface net {
    use types.{net-read-status};

    // ... existing fns: net-bind-unix, net-accept, net-poll-accept, ...

    /// Open an outbound TCP connection to `host:port`.
    ///
    /// Returns a stream handle compatible with `net-read` / `net-write` /
    /// `net-close-stream` — the same handle type returned by `net-accept`.
    ///
    /// Security:
    /// - The capsule's `net_connect` capability allowlist must contain a
    ///   pattern matching `host:port` (exact or trailing-wildcard segment).
    /// - DNS resolution runs after the capability check. The resolved IP
    ///   is checked against the same SSRF airlock that gates `http-request`
    ///   (loopback / private / link-local / multicast / unspecified all
    ///   rejected).
    /// - The capsule's per-instance active-stream cap (default 8) is
    ///   enforced; exceeding it returns an error rather than closing an
    ///   existing stream.
    /// - Connect timeout is bounded (default 10s); a stalled DNS or TCP
    ///   handshake returns an error rather than holding the WASM guest
    ///   in a host fn indefinitely.
    net-connect-tcp: func(host: string, port: u16) -> result<u64, string>;
}
```

No new types. No new interface. No change to existing functions. A capsule that does not call `net-connect-tcp` is byte-identical at the WIT-bindgen layer.

## Manifest schema change

Add `net_connect` to `CapabilitiesDef` in `crates/astrid-capsule/src/manifest.rs`:

```rust
#[serde(default)]
pub net_connect: Vec<String>,
```

Each entry is a `host:port` pattern where:

- `host` is a literal DNS name or `*` (universal; only meaningful behind an `unsafe_admin`-style rail — see Unresolved questions). Dots are allowed in `host` because hostnames have dots; this is distinct from the capability-grammar segment rule, which applies to `[capabilities]` items not allowlist entries.
- `port` is a decimal `u16` or `*` (any port for the named host).

Empty list → no outbound TCP. Missing field → no outbound TCP. Same fail-closed shape as `host_process`, `fs_read`, `fs_write`, `net_bind`.

A new `effective_net_connect_patterns()` helper on `CapsuleManifest` parses each entry into a `(HostPattern, PortPattern)` pair at install time, exactly matching the `effective_ipc_publish_patterns` / `effective_ipc_subscribe_patterns` helpers added by the cargo-like-manifest parser work.

## Kernel implementation

In `crates/astrid-capsule/src/engine/wasm/host/net.rs`:

```rust
#[async_trait]
impl net::Host for HostState {
    async fn net_connect_tcp(
        &mut self,
        host: String,
        port: u16,
    ) -> wasmtime::Result<Result<u64, String>> {
        // 1. Capability check — literal host:port against manifest allowlist.
        if !self.manifest_allows_connect(&host, port) {
            return Ok(Err(format!(
                "net.connect-tcp denied: \"{host}:{port}\" not in capsule's net_connect allowlist"
            )));
        }

        // 2. Active-stream cap (analogous to net_accept).
        if self.active_streams.len() >= MAX_ACTIVE_STREAMS {
            return Ok(Err(format!(
                "connection cap reached ({}/{MAX_ACTIVE_STREAMS})",
                self.active_streams.len()
            )));
        }

        // 3. DNS resolve + SSRF airlock (same is_safe_ip used by http-request).
        let addrs: Vec<SocketAddr> = tokio::net::lookup_host((host.as_str(), port))
            .await
            .map_err(|e| Ok::<_, wasmtime::Error>(Err(format!("dns: {e}"))))??
            .collect();
        for addr in &addrs {
            if !http::is_safe_ip(addr.ip()) {
                return Ok(Err(format!(
                    "net.connect-tcp denied: resolved IP {} is in a private/loopback/link-local range (SSRF protection)",
                    addr.ip()
                )));
            }
        }

        // 4. TCP connect with bounded timeout.
        let stream = tokio::time::timeout(
            CONNECT_TIMEOUT,
            tokio::net::TcpStream::connect(&addrs[..]),
        )
        .await
        .map_err(|_| Ok::<_, wasmtime::Error>(Err("connect timeout".into())))?
        .map_err(|e| Ok::<_, wasmtime::Error>(Err(format!("connect: {e}"))))??;

        // 5. Wrap in a length-prefixed frame codec, install into active_streams.
        let handle = self.next_handle();
        self.active_streams.insert(handle, NetStream::from_tcp(stream));
        Ok(Ok(handle))
    }
}
```

The framing layer (`NetStream::from_tcp`) wraps the raw `TcpStream` in the same length-prefixed envelope the inbound `net-accept` path already uses, so `net-read` and `net-write` work without further changes. A capsule that wants raw bytes (e.g. HTTP/1.1, where length-prefixing would be wrong) wraps its own framing on top — same as it would today over `net-accept`.

`CONNECT_TIMEOUT` is a kernel constant (`10s` default, env-var override). `MAX_ACTIVE_STREAMS` is the existing per-capsule cap (`8`) and counts inbound + outbound together — a capsule running a CLI proxy that holds 5 inbound clients can open at most 3 outbound connections.

## SDK shapes

### Rust (`astrid_sdk::net`)

Low-level (mirrors existing `accept`):

```rust
pub fn connect(host: &str, port: u16) -> Result<StreamHandle, SysError> {
    let handle = wit_net::net_connect_tcp(host, port).map_err(SysError::HostError)?;
    Ok(StreamHandle(handle))
}
```

`std::net::TcpStream`-shaped facade in a new submodule `astrid_sdk::net::tcp`:

```rust
pub struct TcpStream {
    handle: StreamHandle,
}

impl TcpStream {
    /// Opens a TCP connection to a remote host.
    ///
    /// `addr` parses as `host:port` (the only `ToSocketAddrs`
    /// implementation provided — DNS resolution is performed host-side,
    /// not in the WASM guest).
    pub fn connect<A: AsRef<str>>(addr: A) -> std::io::Result<Self> {
        let (host, port) = parse_host_port(addr.as_ref())?;
        let handle = crate::net::connect(host, port)
            .map_err(io_error_from)?;
        Ok(Self { handle })
    }
}

impl std::io::Read for TcpStream { /* delegates to net::recv */ }
impl std::io::Write for TcpStream { /* delegates to net::send */ }
impl Drop for TcpStream { fn drop(&mut self) { let _ = net::close(&self.handle); } }
```

The std-style facade is the surface most capsule code uses. Crucially, anything generic over `Read + Write` works without per-runtime adapters — `tungstenite::client_with_config(url, stream, …)` for WebSocket, `rustls::ClientConnection` for TLS, `postgres::Client::connect_raw(stream, …)` for postgres.

### JavaScript (`@astrid-os/sdk/net`)

```typescript
/// Open a TCP connection to `host:port`.
///
/// Returns a duplex Stream that implements AsyncIterable<Uint8Array> for
/// reads and exposes `write(chunk)` and `close()` for writes / shutdown.
export async function connect(host: string, port: number): Promise<Stream>;

export interface Stream extends AsyncIterable<Uint8Array> {
    write(chunk: Uint8Array): Promise<void>;
    close(): Promise<void>;
}
```

A follow-up `node:net` shim (~200 LOC) wraps `Stream` in the `node:net.Socket` event-emitter shape — once that lands, the unmodified npm `ws` library can be used inside a capsule via `import WebSocket from 'ws'`.

## Tests

Integration tests live in `crates/astrid-capsule/tests/net_connect_tcp.rs`:

- **Happy path** — local TCP echo server bound to `127.0.0.1:<random>`; capsule with `net_connect = ["127.0.0.1:<port>"]` connects, writes, reads back, closes. (The SSRF check makes a one-line test-only exception here — `cfg(test)` flag on `is_safe_ip` permitting loopback.)
- **Capability denial** — capsule with `net_connect = []` (or `["other.host:443"]`) calling `connect("example.com", 80)` returns the expected denial error. No TCP socket opened.
- **SSRF rejection** — capsule with `net_connect = ["*:80"]` (overly broad allowlist) calling `connect("169.254.169.254", 80)` is rejected by the airlock. No TCP socket opened.
- **Active-stream cap** — capsule with 8 inbound streams open calls `connect`; gets the `connection cap reached (8/8)` error.
- **Connect timeout** — capsule connects to a sinkhole (`example.com:1` with iptables drop, or a `tcpdrop` test fixture); gets the timeout error within `CONNECT_TIMEOUT + 100ms`.
- **DNS failure** — capsule connects to `nonexistent.example.invalid`; gets a clean `dns:` error.
- **End-to-end through SDK facade** — Rust capsule uses `TcpStream::connect("127.0.0.1:<port>") + Read::read`. Closes on drop.

## Wire format

`net-connect-tcp` is fundamentally a TCP socket — no length-prefixing applied at the socket layer. The framing layer (`net-read` / `net-write`) treats every `write` as one frame and every `read` returns one frame; for outbound TCP this means the application is responsible for its own framing.

For protocols that are intrinsically framed (WebSocket, MQTT, Postgres wire protocol) this is the right shape — the protocol library does its own framing. For HTTP/1.1 over a connected socket, the application reads until it sees the headers / chunked terminators. The host does not assume any application-layer protocol.

The `net-accept` path *does* length-prefix because the Unix proxy capsule uses framed JSON envelopes. That framing belongs to the proxy protocol, not the `net` interface — and the framing is wrong for raw TCP. A follow-up RFC may split `net-read-framed` (length-prefixed, today's behaviour) from `net-read-raw` (byte stream); for this RFC, `net-connect-tcp` returns a handle that the existing length-prefixed `net-read` / `net-write` operates on (a capsule that wants raw bytes wraps its own length-prefix on top, which is a 4-byte overhead per frame — acceptable for protocols that frame themselves anyway).

# Drawbacks
[drawbacks]: #drawbacks

- **Adds a new SSRF attack surface.** Every outbound-network primitive is a potential server-side request forgery vector. The mitigation is the same airlock `http-request` already runs — but bugs in `is_safe_ip`, DNS rebinding races (a name resolves to a public IP at capability check and a private IP at connect), and TOCTOU between resolve and connect all become reachable from one more host fn. We accept the added surface because `http-request` already carries the same airlock and the bugs would be shared; widening the surface from "HTTP only" to "HTTP and TCP" does not introduce a new class of vulnerability, only a new place to express it.

- **Length-prefix framing is wrong for raw TCP.** The `net-read` / `net-write` interface assumes framed envelopes (its first consumer was the Unix CLI proxy). Reusing it for outbound TCP means every byte-stream consumer pays a 4-byte framing overhead per read/write. We accept this for the first version — splitting framed and unframed reads is a strictly-additive follow-up, and the framing overhead is invisible for protocols that frame themselves (WebSocket, MQTT, Postgres). HTTP/1.1 consumers pay 8 bytes per request/response, which is in the noise.

- **The capability surface grows.** `net_connect` is the seventh capability declaration on `[capabilities]` (after `fs_read`, `fs_write`, `host_process`, `net_bind`, `ipc_publish`, `ipc_subscribe`). Each one is a small thing on its own; the cumulative cognitive load on capsule authors is real. We accept this because the alternative is "the host has no outbound TCP" — capsule authors who need TCP would have to write that capability declaration anyway, just on a different ABI.

- **A capsule can hold a TCP connection open indefinitely** until its active-stream cap or invocation timeout fires. A misbehaving capsule consumes one slot of `MAX_ACTIVE_STREAMS = 8` and one inode-equivalent in the kernel for as long as the connection stays up. The mitigation is the existing per-capsule cap; the absolute cap on host-wide connection count comes from `MAX_ACTIVE_STREAMS × loaded_capsules`, which is bounded by the workspace's overall design.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

## Why TCP and not WebSocket

WebSocket is RFC 6455 framing over TCP. Its full implementation is ~500 LOC of JS or Rust and exists in every language's package ecosystem:

- Rust: `tungstenite`, `tokio-tungstenite`, `fastwebsockets`
- JavaScript: `ws` (npm), the WHATWG `WebSocket` global in browsers and modern Node
- Python: `websockets`, `aiohttp.ClientWebSocketResponse`

Putting WebSocket framing in the host ABI would:

1. **Couple a wire protocol to the kernel.** Updates to RFC 6455 (e.g. permessage-deflate extension negotiation, draft compression schemes) require a kernel release. Today, capsule authors pull a newer `tungstenite` and ship.
2. **Force every persistent-network protocol to either piggyback on a mismatched shape or get its own host fn.** Discord gateway is framed text over TCP, Nostr is line-delimited JSON over WebSocket-over-TCP, MQTT is binary framing over TCP, postgres is its own wire protocol over TCP, Redis is `RESP` over TCP. None of those would fit on a "WebSocket" host fn; each would either need its own kernel-side adapter or a less-elegant workaround.
3. **Block features the kernel doesn't yet implement.** Permessage-deflate, ping/pong interval tuning, subprotocol negotiation — every one of these is a host-fn signature change. The user-space implementation evolves at its own pace.

`std::net::TcpStream` exists for exactly this reason. The Rust standard library exposes the primitive; `tungstenite` builds on it; nothing in the standard library knows WebSocket exists. We mirror that.

## Why outbound TCP and not `wasi:sockets/tcp`

WASI Preview 2 standardises `wasi:sockets/tcp` — the WASI-native interface for outbound TCP. Adopting it would let WASI-ecosystem libraries (e.g. `wasi-async-net`) drop into Astrid capsules unchanged.

Two reasons against:

- **Authority model mismatch.** WASI's network authority is `preopens`-based: the host hands the guest a set of pre-opened network handles, and the guest can only use those. Astrid's authority model is capability-based: the guest names a host:port pattern in its manifest, and the host either allows or denies each connect at runtime. Bridging the two would either (a) ignore WASI's preopen semantics and treat the WASI surface as a thin shim over our capability check, or (b) reshape our capability model to be preopen-based. Neither lands cleanly.
- **No actual demand.** A search of `crates.io` for capsule-oriented WASI-sockets consumers turns up `wasi-async-net` and not much else; the libraries the immediate motivator needs (`tungstenite`, `tokio-tungstenite`, npm `ws`) all build directly on standard-library TCP, not on WASI sockets. Adopting `wasi:sockets/tcp` would buy access to a corner of the ecosystem we don't have callers for.

If ecosystem demand emerges later, `wasi:sockets/tcp` can be adopted *alongside* `net-connect-tcp` (both as parallel interfaces, with the capability gate shared) without breaking the SDK surface. Today's RFC is the minimum to unblock real workloads.

## Why not extend `http-stream-start`

`http-request` and `http-stream-start` are request/response shaped. The streaming variant lets the server send a body of arbitrary length, but the client cannot send more bytes after the initial request line + headers. WebSocket is bidirectional after the initial Upgrade handshake; persistent JSON-RPC over WebSocket requires sending many messages. Coercing this onto an HTTP-shaped surface would mean keeping a request channel open and somehow flushing additional bytes — possible in principle, but it would mean writing a wire-protocol-aware splicer in the host, which is exactly what we're trying to avoid.

## Why the capability syntax is `net_connect = ["host:port"]` and not structured

The structured alternative looks like:

```toml
[[capabilities.net_connect]]
host = "fulcrum.unicity.network"
port = 443
```

This buys field validation at parse time but adds boilerplate, breaks visual scannability when a capsule has 6+ destinations, and mirrors the failed `[[topic]]` schema from the cargo-like-manifest RFC's motivation section. The string form `"host:port"` is what every operator writes in `iptables` rules and shell pipes; it parses unambiguously; and it stays consistent with the rest of the `[capabilities]` block (which is a flat list of strings everywhere else).

# Prior art
[prior-art]: #prior-art

- **WASI `wasi:sockets/tcp`** ([proposal](https://github.com/WebAssembly/wasi-sockets)). Defines `tcp-create-socket`, `start-connect`, `finish-connect`, `start-listen`, `finish-listen`, `accept`. Preopen-based authority. The capability primitive is the WASI default-deny-with-explicit-grant model rather than a manifest allowlist. Discussed above; not adopted here for authority-model reasons.

- **Cloudflare Workers `connect()`** ([docs](https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/)). Returns a `Socket` with `readable: ReadableStream` and `writable: WritableStream`. Capability surface is the Workers `compatibility_flags` + bindings system, which is workers-platform-specific. Closest analogue to what this RFC proposes in shape, though the JavaScript-streams interface is heavier than a `Read + Write` Rust trait.

- **Deno `Deno.connect`** ([docs](https://docs.deno.com/api/deno/~/Deno.connect)). Permission-gated (`--allow-net=host:port`) with a flat-allowlist that maps cleanly onto our `net_connect`. Returns a `Conn` implementing `Reader + Writer + Closer`. The permission grammar is essentially identical to what this RFC proposes.

- **Node.js `node:net.createConnection`**. The reference shape for the JS SDK ergonomics — `Socket` extends `Duplex`, fires `'data'` / `'end'` / `'close'` events, has `write(chunk)` / `end()`. The follow-up `node:net` shim mentioned in Future possibilities is what lets unmodified npm libraries reach Astrid capsules.

- **Rust standard library `std::net::TcpStream`**. The reference shape for the Rust SDK ergonomics — `Read + Write` plus `connect<A: ToSocketAddrs>(addr) -> io::Result<Self>`. The SDK's `TcpStream::connect` is a one-to-one mirror.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- **Wildcard host patterns** (`*.example.com`, `*:443`). The first version of this RFC supports only literal hosts and `host:*` (any port for a named host). Wildcard hosts open per-operator decisions (does `*.example.com` match `evil.example.com`? what about `evil.com`?) that the literal allowlist doesn't carry. Worth a follow-up after the literal form has been exercised against real workloads.

- **IP-literal allowlist entries** (`net_connect = ["10.0.0.5:443"]`). The SSRF airlock fires on the resolved IP regardless of what the manifest names. An operator who genuinely wants a capsule to reach a private IP (e.g. an in-cluster service in a containerised deployment) has no escape hatch today. A future `unsafe_private_net` capability rail, opt-in per capsule by an operator at install time, would let the airlock be bypassed for an explicit allowlist — but the bypass needs an unambiguous audit story before this gets exposed.

- **TLS in the host vs in user-space.** The issue lists TLS as out of scope; the working assumption is "capsules can use `rustls` or a pure-JS TLS lib over the raw TCP stream." This works in principle but doubles the WASM binary size for any TLS-using capsule, and a host-side `connect_tls(host, port, sni)` would dedupe the TLS stack across capsules at the cost of the kernel taking a dependency on a TLS library. Worth revisiting if TLS-using capsules become numerous.

- **Connect timeout configurability.** `CONNECT_TIMEOUT = 10s` is a kernel constant in the first version. Some protocols (long-poll, satellite uplink) reasonably want longer; some operator policies reasonably want shorter. Promoting this to a per-capsule manifest field or a per-call argument is a follow-up.

- **Per-destination connection cap.** `MAX_ACTIVE_STREAMS` is per-capsule, summed across inbound + outbound. A capsule that needs 5 inbound and 8 outbound today has to either request a higher cap (no mechanism exists) or batch its outbound work. Splitting the cap into per-direction or per-destination is a future refinement.

- **Outbound UDP.** Mentioned as out-of-scope in the issue. The connection model is different enough (connectionless, no stream-handle equivalent) that it warrants its own RFC if real demand emerges.

# Future possibilities
[future-possibilities]: #future-possibilities

- **`@astrid-os/ws` package** — pure-TS WebSocket client over `astrid.net.connect`. Either a from-scratch RFC 6455 implementation (~500 LOC) or a thin facade over the npm `ws` library with a `node:net` shim underneath.

- **`node:net` shim** — wraps `astrid.net.connect` in the `node:net.Socket` event-emitter API. Once shipped, any npm library that builds on `node:net` (postgres, redis, mqtt.js, ws, ftp, …) reaches Astrid capsules without per-library porting.

- **`net_connect` operator policy template** — a curated allowlist (e.g. `astrid distro` extension for "this distro lets capsules talk to the Unicity network endpoints") so operators don't reinvent the same `Capsule.toml` block for every install.

- **Per-host connection accounting** — `astrid quota set --net-connect-per-host` so an operator can cap how many simultaneous Fulcrum connections a capsule can hold open. Currently the cap is total-streams-per-capsule.

- **Connection re-use across invocations** — today a `TcpStream` is owned by the invocation that opened it. A long-lived run-loop capsule (`#[astrid::run]`) keeps the stream alive across IPC ticks; a non-daemon capsule re-connects on every invocation. A future "pinned connection" capability would let a capsule reserve a stream slot across invocation boundaries.

- **Outbound TLS in the host** — `net-connect-tls(host, port, sni) -> stream-handle` would dedupe `rustls` across every TLS-using capsule. Trade-off: kernel takes a TLS-library dependency. Worth revisiting once user-space-TLS capsules show up in real numbers.

- **Outbound UDP / QUIC** — separate connection models, separate RFC. The motivating use case (Sphere SDK, Nostr) does not need them today.

- **`wasi:sockets/tcp` parallel surface** — implement the WASI standard alongside `net-connect-tcp` sharing the capability gate. Lets WASI-ecosystem libraries drop into Astrid capsules unchanged once ecosystem demand emerges.

- **Port [openclaw-unicity](https://github.com/unicitynetwork/openclaw-unicity) to an Astrid-native capsule.** The immediate motivator for this RFC; the work is unblocked the moment `net-connect-tcp` lands on `main` and an SDK release ships.
