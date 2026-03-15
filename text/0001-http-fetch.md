- Feature Name: `http_fetch`
- Start Date: 2026-03-15
- RFC PR: [astrid-rfcs#0001](https://github.com/unicity-astrid/astrid-rfcs/pull/1)
- Tracking Issue: TBD

# Summary
[summary]: #summary

Define a standard tool interface for HTTP fetching in Astrid capsules. A
conforming capsule exposes a single `fetch_url` tool that agents use to make
HTTP requests, with SSRF prevention, method validation, and response truncation
handled uniformly.

# Motivation
[motivation]: #motivation

Agents need HTTP access for web browsing, API calls, and data retrieval. Without
a standard interface, every capsule author invents their own tool name, argument
shape, and error contract. This makes agents non-portable between runtimes and
forces LLM system prompts to be capsule-specific.

By standardizing the tool interface, any compliant runtime can swap HTTP capsule
implementations without changing agent behavior. Third-party capsule developers
can build alternative implementations (e.g., caching proxies, rate-limited
fetchers) that drop in seamlessly.

# Guide-level explanation
[guide-level-explanation]: #guide-level-explanation

An agent that needs to fetch a URL calls the `fetch_url` tool:

```json
{
  "name": "fetch_url",
  "arguments": {
    "url": "https://api.example.com/data",
    "method": "GET"
  }
}
```

The tool returns a JSON object with the HTTP status, response headers, and body:

```json
{
  "status": 200,
  "headers": {
    "content-type": "application/json",
    "x-request-id": "abc123"
  },
  "body": "{\"key\": \"value\"}"
}
```

HTTP error statuses (4xx, 5xx) are returned as data, not tool errors. The agent
can inspect the status code and reason about failures. Only infrastructure
failures (DNS resolution, timeouts, SSRF blocks) produce tool errors.

If the response body exceeds the implementation's soft limit, it is truncated at
a valid UTF-8 boundary and a `truncated: true` field is added to the response.

# Reference-level explanation
[reference-level-explanation]: #reference-level-explanation

## Tool definition

| Field | Value |
|-------|-------|
| Name | `fetch_url` |
| Description | Fetch a URL over HTTP/HTTPS. Supports standard HTTP methods. Returns status code, response headers, and body. Private and local IPs are blocked (SSRF-safe). |

## Input schema

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "description": "The URL to fetch (http:// or https:// only)"
    },
    "method": {
      "type": "string",
      "description": "HTTP method. Defaults to GET.",
      "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
    },
    "headers": {
      "type": "object",
      "description": "Optional HTTP headers as key-value pairs",
      "additionalProperties": { "type": "string" }
    },
    "body": {
      "type": "string",
      "description": "Optional request body (for POST/PUT/PATCH)"
    }
  },
  "required": ["url"]
}
```

## Output schema (success)

```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "integer",
      "description": "HTTP status code (100-599)"
    },
    "headers": {
      "type": "object",
      "description": "Response headers as key-value pairs",
      "additionalProperties": { "type": "string" }
    },
    "body": {
      "type": "string",
      "description": "Response body as text"
    },
    "truncated": {
      "type": "boolean",
      "description": "Present and true when body was truncated"
    }
  },
  "required": ["status", "headers", "body"]
}
```

## Validation requirements

Implementations MUST:

- Reject empty URLs with a tool error.
- Reject URLs with schemes other than `http://` and `https://` (case-insensitive).
- Reject HTTP methods not in the enum above with a tool error.
- Default to `GET` when `method` is omitted.
- Block requests to private and loopback IP addresses (SSRF prevention) at DNS
  resolution time, returning a tool error.

## Response handling

Implementations MUST:

- Return HTTP error statuses (4xx, 5xx) as successful tool results with the
  status code in the `status` field.
- Return infrastructure failures (DNS, timeout, connection refused, SSRF block)
  as tool errors.
- Include the full response header map in `headers`.

Implementations SHOULD:

- Enforce a soft body size limit (recommended: 200 KB) to prevent context window
  exhaustion.
- Truncate at a valid UTF-8 character boundary when the limit is exceeded.
- Set `truncated: true` and append a human-readable note to the body indicating
  the total size.
- Omit the `truncated` field entirely when the body is not truncated.

## Host function requirements

The capsule communicates with the host runtime via an `astrid_http_request` host
function that accepts a JSON-serialized request and returns a JSON-serialized
response. The host is responsible for:

- DNS resolution with SSRF filtering
- TLS certificate verification
- Connection and response timeouts (implementation-defined)
- Hard payload size limits (implementation-defined, recommended: 10 MB)

## Capabilities

A conforming capsule MUST declare in its `Capsule.toml`:

```toml
[capabilities]
net = ["*"]
```

## Security considerations

- Agent-provided headers are passed to the host unfiltered. Headers like `Host`,
  `Authorization`, and `Cookie` can be set by the agent. The SSRF layer blocks
  private IPs but header injection to public endpoints is within the accepted
  threat model.
- Full response headers (including `Set-Cookie` and auth tokens) are returned to
  the agent's context window. Operators should be aware that response secrets
  enter the LLM context.

# Drawbacks
[drawbacks]: #drawbacks

- The single-tool interface is simple but limited. Agents that need streaming
  responses, WebSocket connections, or cookie jars require a different standard.
- Returning full response headers to the LLM consumes context window tokens on
  every request.

# Rationale and alternatives
[rationale-and-alternatives]: #rationale-and-alternatives

**Why a single `fetch_url` tool?** Agents work best with simple, well-documented
tools. Splitting into `http_get`, `http_post`, etc. would increase tool count
without adding capability - the `method` parameter handles this cleanly.

**Why return errors as data?** LLMs can reason about HTTP 404s and 500s
productively ("the page doesn't exist, try a different URL"). Converting these
to tool errors forces a generic error path that loses the status code and body.

**Alternative: MCP resource interface.** Resources are read-only and don't
support methods, headers, or bodies. HTTP fetch is fundamentally a tool, not a
resource.

# Prior art
[prior-art]: #prior-art

- **MCP fetch tool** (Anthropic): Similar single-tool design with URL, method,
  headers, body. Returns markdown-rendered content by default. This RFC differs
  by returning raw response data and headers, giving the agent more control.
- **OpenAI browsing tool**: Higher-level abstraction that renders pages. Not
  suitable for API access.
- **curl**: The Unix standard. This RFC's interface maps closely to curl's
  conceptual model (URL + method + headers + body -> status + headers + body).

# Unresolved questions
[unresolved-questions]: #unresolved-questions

- Should the soft body size limit be configurable per-request via an optional
  `max_body_bytes` argument?
- Should response headers be filtered to reduce context window usage (e.g., strip
  `Set-Cookie`, `X-*` headers)?
- Should the tool support following redirects with a configurable limit, or
  should redirect following be host-only behavior?

# Future possibilities
[future-possibilities]: #future-possibilities

- **RFC for streaming HTTP**: Server-sent events and chunked responses for
  long-running API calls.
- **RFC for HTTP session management**: Cookie jars, authentication state, and
  connection pooling across multiple fetch calls.
- **Content-type aware rendering**: Optional markdown or text extraction for HTML
  responses, similar to MCP's fetch tool.
