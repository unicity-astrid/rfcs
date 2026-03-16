- Feature Name: `kernel_management_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#15](https://github.com/unicity-astrid/rfcs/pull/15)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `astrid.v1.request` and `astrid.v1.response` event contract defines the
topic patterns and message schemas for the kernel management API on the Astrid
event bus. Frontends and capsules send management requests (list capsules,
reload, install, get commands) and receive structured responses.

# Motivation
[motivation]: #motivation

The kernel exposes management operations (capsule listing, reloading,
installation, command discovery) that frontends and other capsules need to
invoke. The contract must guarantee that:

- Management operations use a consistent request/response pattern.
- The kernel router handles dispatch without knowing specific operations.
- Responses are tagged by status (success, error, approval required).
- New management operations can be added without changing the routing logic.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Two actors participate:

1. **Caller** (frontend or capsule) - publishes a management request.
2. **Kernel router** - dispatches the request and publishes a response.

```text
Caller                        Kernel router
    |                              |
    |-- KernelRequest ----------->|
    |   (astrid.v1.request.       |-- dispatch
    |    {method})                 |
    |<-- KernelResponse -----------|
    |   (astrid.v1.response.      |
    |    {method})                 |
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| request | `astrid.v1.request.list_capsules` | `RawJson(KernelRequest::ListCapsules)` | Caller | Kernel router |
| request | `astrid.v1.request.reload_capsules` | `RawJson(KernelRequest::ReloadCapsules)` | Caller | Kernel router |
| request | `astrid.v1.request.get_commands` | `RawJson(KernelRequest::GetCommands)` | Caller | Kernel router |
| request | `astrid.v1.request.get_capsule_metadata` | `RawJson(KernelRequest::GetCapsuleMetadata)` | Caller | Kernel router |
| request | `astrid.v1.request.install_capsule` | `RawJson(KernelRequest::InstallCapsule)` | Caller | Kernel router |
| request | `astrid.v1.request.approve_capability` | `RawJson(KernelRequest::ApproveCapability)` | Caller | Kernel router |
| response | `astrid.v1.response.{method}` | `RawJson(KernelResponse)` | Kernel router | Caller |

The kernel router subscribes to `astrid.v1.request.*` to catch all management
requests.

## Message schemas

### KernelRequest

Serde-tagged by `method` with optional `params`:

```json
{ "method": "ListCapsules" }
{ "method": "ReloadCapsules" }
{ "method": "GetCommands" }
{ "method": "GetCapsuleMetadata" }
{ "method": "InstallCapsule", "params": { "source": "/path/to/capsule.tar.gz", "workspace": true } }
{ "method": "ApproveCapability", "params": { "request_id": "string", "signature": "string (ed25519 hex)" } }
```

| Method | Params | Description |
|--------|--------|-------------|
| `ListCapsules` | none | List currently loaded capsules |
| `ReloadCapsules` | none | Reload all capsules from filesystem |
| `GetCommands` | none | List registered slash commands |
| `GetCapsuleMetadata` | none | List capsule manifests, providers, interceptors |
| `InstallCapsule` | `source: String, workspace: bool` | Install a capsule from path/URL |
| `ApproveCapability` | `request_id: String, signature: String` | Approve a capability grant with ed25519 signature |

### KernelResponse

Serde-tagged by `status` with `data`:

```json
{ "status": "Success", "data": { "capsules": ["anthropic", "filesystem", "shell"] } }
{ "status": "Commands", "data": [{ "name": "/git", "description": "Git operations", "provider_capsule": "capsule-git" }] }
{ "status": "CapsuleMetadata", "data": [{ "name": "anthropic", "llm_providers": [...], "interceptor_events": [...] }] }
{ "status": "Error", "data": "capsule not found" }
{ "status": "ApprovalRequired", "data": { "request_id": "...", "description": "...", "capabilities": ["host_process"] } }
```

| Status | Data type | Description |
|--------|-----------|-------------|
| `Success` | `Value` | Generic success with JSON payload |
| `Commands` | `CommandInfo[]` | List of slash commands |
| `CapsuleMetadata` | `CapsuleMetadataEntry[]` | Capsule manifests and providers |
| `Error` | `String` | Error description |
| `ApprovalRequired` | `{ request_id, description, capabilities }` | Action needs human approval first |

### CommandInfo

```json
{
  "name": "/git",
  "description": "Git operations",
  "provider_capsule": "capsule-git"
}
```

### CapsuleMetadataEntry

```json
{
  "name": "anthropic",
  "llm_providers": [
    { "id": "claude-sonnet-4-20250514", "description": "Claude Sonnet 4", "capabilities": ["text", "vision", "tools"] }
  ],
  "interceptor_events": ["llm.v1.request.generate.anthropic"]
}
```

## Behavioral requirements

A conforming **kernel router** must:

1. Subscribe to `astrid.v1.request.*`.
2. Dispatch each request by `method` to the appropriate handler.
3. Publish the response to `astrid.v1.response.{method}` (lowercased,
   underscored method name).
4. For `InstallCapsule`, if capabilities need approval, respond with
   `ApprovalRequired` instead of proceeding.
5. For `ApproveCapability`, verify the ed25519 signature before granting.

A conforming **caller** must:

1. Subscribe to `astrid.v1.response.*` before publishing the request.
2. Handle all response status variants.
3. On `ApprovalRequired`, present the approval flow and follow up with
   `ApproveCapability`.

## Error handling

| Condition | Behavior |
|-----------|----------|
| Unknown method | Kernel router ignores (not subscribed to that topic segment) |
| Install source not found | `Error` response |
| Invalid signature | `Error` response |
| Kernel router not running | Request to empty topic, caller times out |

# Drawbacks
[drawbacks]: #drawbacks

- No scoped reply topics. Responses go to a shared `astrid.v1.response.*`
  namespace, which could cause issues with concurrent callers.
- The `RawJson` wrapping means these messages are not covered by
  `IpcPayload` type safety.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why method-based dispatch?** Mirrors JSON-RPC conventions. The kernel
router can dispatch by topic suffix without parsing the payload.

**Why `ApprovalRequired` as a response status?** Capability installation
is a two-phase operation: request, then approve. Modeling approval as a
response status keeps the flow in a single request/response exchange.

# Prior art
[prior-art]: #prior-art

- **JSON-RPC**: Method-based dispatch with structured responses.
- **Kubernetes API**: Resource-based management with status responses.
- **procfs**: Kernel exposes process information to user-space.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should management requests use scoped reply topics for concurrent safety?
- Should there be a `GetCapsuleStatus` method for health checking individual
  capsules?

# Future possibilities
[future-possibilities]: #future-possibilities

- Capsule upgrade/rollback management operations.
- Capability audit (list all granted capabilities).
- Resource usage queries (memory, CPU per capsule).
