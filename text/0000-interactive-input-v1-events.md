- Feature Name: `interactive_input_v1_events`
- Start Date: 2026-03-16
- RFC PR: [rfcs#13](https://github.com/unicity-astrid/rfcs/pull/13)
- Tracking Issue: N/A
- Status: Documents existing implementation. Open for changes until 1.0.

# Summary
[summary]: #summary

The `astrid.v1.elicit` and `astrid.v1.onboarding` event contracts define the
topic patterns and message schemas for interactive user input on the Astrid
event bus. Capsules request structured input (text, secrets, enum selection,
array collection) from frontends during installation, upgrade, or runtime.

# Motivation
[motivation]: #motivation

Capsules often need configuration values (API keys, relay URLs, network
selection) that cannot be hardcoded. The interactive input contract must
guarantee that:

- Capsules can request typed input during install/upgrade lifecycle hooks.
- Frontends render appropriate input widgets (text field, masked secret,
  dropdown, multi-value array).
- The contract supports cancellation (user presses Escape).
- Onboarding can be triggered at capsule load time for missing configuration.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

Two related protocols:

**Elicit** - Runtime request for a single field:

```text
Host function                     Frontend
    |                                |
    |-- ElicitRequest ------------->|
    |   (astrid.v1.elicit)          |-- render input
    |   [blocks]                     |-- user types
    |                                |
    |<-- ElicitResponse ------------|
    |   (astrid.v1.elicit.          |
    |    response.{request_id})     |
```

**Onboarding** - Batch request for multiple fields at capsule load:

```text
Capsule engine                    Frontend
    |                                |
    |-- OnboardingRequired -------->|
    |   (astrid.v1.onboarding.      |-- render form
    |    required)                   |-- user fills fields
```

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Topic patterns

| Direction | Topic | Payload | Publisher | Subscriber |
|-----------|-------|---------|-----------|------------|
| request | `astrid.v1.elicit` | `ElicitRequest` | Host function | Frontend |
| response | `astrid.v1.elicit.response.{request_id}` | `ElicitResponse` | Frontend | Host function |
| notify | `astrid.v1.onboarding.required` | `OnboardingRequired` | Capsule engine | Frontend |

## Message schemas

### ElicitRequest

```json
{
  "type": "elicit_request",
  "request_id": "uuid",
  "capsule_id": "string",
  "field": {
    "key": "api_url",
    "prompt": "Enter API URL",
    "description": "The backend endpoint",
    "field_type": "Text",
    "default": "https://example.com",
    "placeholder": "https://..."
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | UUID | yes | Correlation ID. Host function blocks until response. |
| `capsule_id` | string | yes | The capsule requesting input. |
| `field` | `OnboardingField` | yes | Field descriptor. |

### OnboardingField

```json
{
  "key": "string (env var key)",
  "prompt": "string (display text)",
  "description": "string | null",
  "field_type": "Text | Secret | { \"Enum\": [\"a\", \"b\"] } | Array",
  "default": "string | null",
  "placeholder": "string | null"
}
```

### OnboardingFieldType

| Variant | JSON | Frontend rendering |
|---------|------|-------------------|
| `Text` | `"Text"` | Free-form text input |
| `Secret` | `"Secret"` | Masked input (for API keys, passwords) |
| `Enum` | `{ "Enum": ["opt1", "opt2"] }` | Dropdown or picker from fixed choices |
| `Array` | `"Array"` | Multi-value input (user adds items one at a time) |

### ElicitResponse

```json
{
  "type": "elicit_response",
  "request_id": "uuid",
  "value": "string | null",
  "values": ["string"] | null
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `request_id` | UUID | yes | Must match the originating request. |
| `value` | string/null | no | The user's input. `null` if cancelled. |
| `values` | `string[]`/null | no | For `Array`-type fields, the collected items. |

Cancellation is represented by both `value` and `values` being `null`.

### OnboardingRequired

```json
{
  "type": "onboarding_required",
  "capsule_id": "string",
  "fields": [{ "key": "...", "prompt": "...", "field_type": "..." }]
}
```

Published by the capsule engine when a capsule declares required environment
variables that are not yet configured. The frontend renders a multi-field
form.

## Behavioral requirements

A conforming **host function** (elicit) must:

1. Publish `ElicitRequest` to `astrid.v1.elicit`.
2. Subscribe to `astrid.v1.elicit.response.{request_id}` before publishing.
3. Block until the response arrives or a timeout is reached.
4. Handle cancellation (both `value` and `values` are `null`).

A conforming **capsule engine** (onboarding) must:

1. Check each capsule's declared environment variables at load time.
2. If any are missing, publish `OnboardingRequired` to
   `astrid.v1.onboarding.required`.
3. The engine does not block. It is the frontend's responsibility to
   collect values and configure them before the capsule is fully operational.

A conforming **frontend** must:

1. Subscribe to `astrid.v1.elicit` and `astrid.v1.onboarding.required`.
2. Render appropriate input widgets based on `field_type`.
3. For `Secret` fields, mask user input.
4. For `Enum` fields, show a picker with the provided options.
5. For `Array` fields, allow the user to add/remove items.
6. Publish `ElicitResponse` to the scoped reply topic.
7. Support cancellation (user presses Escape).

## Error handling

| Condition | Behavior |
|-----------|----------|
| No frontend subscribed | Elicit: host function times out. Onboarding: capsule loads without config. |
| User cancels | `value` and `values` are both `null`. Host function treats as no input. |
| Empty Enum choices | Frontend renders empty picker. User cannot select anything. |

# Drawbacks
[drawbacks]: #drawbacks

- Onboarding is fire-and-forget. The capsule engine does not know when the
  user has finished filling in fields.
- `ElicitResponse` overloads `value` and `values` for different field types.
  A single `result` field with type discrimination would be cleaner.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why reuse `OnboardingField` for elicit?** The same input types (text, secret,
enum, array) apply to both onboarding and runtime input. One schema serves
both.

**Why `Enum` as a tuple variant?** Carries the allowed choices inline. The
frontend does not need a separate lookup to render the picker.

**Why separate elicit and onboarding?** Elicit is synchronous
(host function blocks). Onboarding is asynchronous (engine fires and
continues loading).

# Prior art
[prior-art]: #prior-art

- **MCP elicitation**: Server requests input from the host. Same
  request/response pattern, different transport.
- **Docker environment variables**: Declared in Dockerfile, provided at
  runtime. Onboarding is the interactive equivalent.
- **Terraform `variable` prompts**: Prompts the user for undefined
  variables at plan time.

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should the `OnboardingField` schema support validation rules (regex,
  min/max length)?
- Should elicit support multi-field requests (batch input)?

# Future possibilities
[future-possibilities]: #future-possibilities

- Rich input types (file picker, color picker, date picker).
- Conditional fields (show field B only if field A has value X).
- Remote onboarding (collect values via web form, not just local frontend).
