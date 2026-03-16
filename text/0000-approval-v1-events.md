- Feature Name: `approval_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#13](https://github.com/unicity-astrid/rfcs/pull/13)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `astrid.v1.approval` event contract defines the topic patterns and message
schemas for the human-in-the-loop approval gate on the Astrid event bus. When
a capsule requests a sensitive action, the host function publishes an approval
request. A frontend renders the prompt, collects the user's decision, and
publishes the response to a scoped reply topic.

# Motivation
[motivation]: #motivation

Agents must not perform sensitive actions (shell commands, file writes, network
requests) without human consent. The approval contract must guarantee that:

- The host function blocks until a human decision arrives.
- Scoped reply topics prevent cross-request response theft.
- Decision granularity (once, session, always, deny) is part of the contract.
- Risk level is communicated so frontends can adjust UI emphasis.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Two actors participate:

1. **Host function** (`astrid_request_approval`) - publishes the request,
   blocks until response.
2. **Frontend capsule** - renders the approval prompt, collects decision.

```text
Host function                     Frontend
    |                                |
    |-- ApprovalRequired ---------->|
    |   (astrid.v1.approval)        |-- render prompt
    |   [blocks]                     |-- user decides
    |                                |
    |<-- ApprovalResponse ----------|
    |   (astrid.v1.approval.        |
    |    response.{request_id})     |
    |-- unblocks                     |
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| request | `astrid.v1.approval` | `ApprovalRequired` | Host function | Frontend |
| response | `astrid.v1.approval.response.{request_id}` | `ApprovalResponse` | Frontend | Host function |

## Message schemas

### ApprovalRequired

```json
{
  "type": "approval_required",
  "request_id": "string (opaque correlation ID)",
  "action": "string (e.g. \"git push\")",
  "resource": "string (e.g. full command string)",
  "reason": "string (justification from the capsule)",
  "risk_level": "string (low | medium | high | critical)"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | string | yes | Opaque correlation ID. The host function blocks until a response with this ID arrives. |
| `action` | string | yes | The action being requested (e.g. "git push", "rm -rf"). |
| `resource` | string | yes | The resource target (e.g. full command string, file path). |
| `reason` | string | yes | Justification for the action. |
| `risk_level` | string | yes | One of: `low`, `medium`, `high`, `critical`. |

### ApprovalResponse

```json
{
  "type": "approval_response",
  "request_id": "string (matches request)",
  "decision": "string (approve | approve_session | approve_always | deny)",
  "reason": "string | null"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | string | yes | Must match the originating request. |
| `decision` | string | yes | The user's decision. See below. |
| `reason` | string/null | no | Optional reason for the decision. |

### Decision values

| Decision | Meaning |
|----------|---------|
| `approve` | Approve this one action. |
| `approve_session` | Approve this action for the rest of the session. |
| `approve_always` | Approve this action permanently (persisted). |
| `deny` | Deny the action. |

## Behavioral requirements

A conforming **host function** must:

1. Publish `ApprovalRequired` to `astrid.v1.approval`.
2. Subscribe to `astrid.v1.approval.response.{request_id}` before publishing.
3. Block until the response arrives or a timeout is reached.
4. Interpret the `decision` field to allow or deny the action.

A conforming **frontend** must:

1. Subscribe to `astrid.v1.approval`.
2. Render the approval prompt with `action`, `resource`, `reason`, and
   `risk_level`.
3. Publish the user's decision to `astrid.v1.approval.response.{request_id}`.
4. Support all four decision values.

## Error handling

| Condition | Behavior |
|-----------|----------|
| No frontend subscribed | Request to empty topic, host function times out, action denied |
| Frontend crashes before responding | Host function times out, action denied |
| Unknown request_id in response | Ignored by host function (no matching subscription) |

# Drawbacks
[drawbacks]: #drawbacks

- The host function blocks a thread while waiting for human input. This
  limits concurrency for capsules awaiting approval.
- `approve_always` persistence is not defined in this contract. It is an
  implementation detail of the approval subsystem.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why scoped reply topics?** Prevents response theft. Two concurrent approval
requests cannot intercept each other's responses.

**Why block the host function?** The capsule must not proceed until the human
decides. Returning a "pending" result would push approval tracking into
every tool capsule.

**Why risk_level as a string, not an enum?** Extensible. Future risk
classifications can be added without breaking the schema.

# Prior art
[prior-art]: #prior-art

- **Claude Code permission prompts**: "Allow tool X to access Y?" with
  allow/deny/always. Same decision model.
- **Android runtime permissions**: Request, user decides, decision cached.
- **sudo**: Block until password/approval, then execute.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should `approve_always` persistence semantics be part of this contract?
- Should there be a `approve_tool` decision that approves all invocations
  of a specific tool, not just the specific resource?

# Future possibilities
[future-possibilities]: #future-possibilities

- Approval delegation to a remote approver (Telegram, Slack, email).
- Approval policies (auto-approve low-risk, require human for high-risk).
- Cryptographic proof of approval (signed decision tokens).
