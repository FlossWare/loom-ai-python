# Loom Python HTTP API contract inventory

This document records the current observable HTTP contract implemented by
`loom_ai.server.LoomServer`. It is an implementation inventory, not the
language-neutral definition of Intent, Worker, Arbiter, Result, or Evidence.

The API evolution review is tracked in issue #23 and references
[FlossWare ADR-0031: REST Representation Contracts and API Evolution](https://github.com/FlossWare/engineering-standards/blob/main/adr/ADR-0031-rest-representation-contracts-and-api-evolution.md).
ADR-0031 is currently Proposed. This inventory does not introduce a breaking
representation change ahead of that decision.

## Routes

| Method and path | Purpose | Success response |
|---|---|---|
| `GET /health` | Check transport availability | `200 application/json`, `{"status":"ok"}` |
| `POST /intents` | Execute an Intent | `200 application/json` with Worker Result fields; includes `execution_id` when a durable store is configured |
| `GET /executions/{execution_id}` | Read durable execution state | `200 application/json` with execution identity, Intent identity, status, state, evidence, metadata, representation version, and provenance |
| `POST /executions/{execution_id}/continue` | Continue a durable execution | `200 application/json` with the stable `execution_id` and Worker Result fields |

The durable routes require an `ExecutionStateStore`. If no store is configured,
`POST /intents` executes without returning a durable execution identity and
the execution lookup/continue routes return `404`.

## Request representation

`POST /intents` currently accepts a JSON object with these fields:

| Field | Requirement | Meaning |
|---|---|---|
| `goal` | Required | Desired outcome |
| `title` | Optional; defaults to `Intent` | Human-readable task title |
| `requirements` | Optional array of strings | Requirements |
| `constraints` | Optional array of strings | Constraints |
| `acceptance` | Optional array of strings | Acceptance criteria |
| `intent_id` | Optional | Caller-supplied Intent identity; generated when absent/empty |
| `execution_id` | Optional | Caller-supplied execution identity; generated when absent/empty |
| `provenance` | Optional object of string keys and string values | Caller-supplied provenance |

The server requires a `Content-Length`, accepts a maximum body size of 10 MiB,
and rejects a non-object JSON body or non-string provenance values. The current
implementation does not validate the request's `Content-Type` header and does
not negotiate the response representation from `Accept`.

## Response representations

Successful execution responses use JSON and include:

- `worker_id`
- `status`
- `output`
- `evidence`
- `error`
- `metadata`

When durable state is configured, submission and continuation responses also
include `execution_id`. Execution lookup returns the durable state representation,
including the Intent identity and caller provenance.

All current responses, including errors, use `Content-Type: application/json`.
Error bodies use the legacy shape `{"error":"..." }` (without the illustrative
spacing). They are not currently RFC 9457 Problem Details. The API does not
currently publish a vendor media type or media-type contract version.

## Status and error behavior

| Condition | Current status |
|---|---:|
| Health check | 200 |
| Intent accepted and executed | 200 |
| Durable execution found | 200 |
| Invalid/malformed Intent request, missing Content-Length, invalid provenance | 400 |
| Request body larger than 10 MiB | 413 |
| Unknown route, unknown execution, or durable route used without a store | 404 |
| Corrupt durable execution state or unexpected server-side failure | 500 |

The current API uses unversioned resource paths (`/intents` and
`/executions/{execution_id}`). There is no formal representation-version
negotiation or published deprecation window yet. Existing clients include the
generic MCP adapter and the dogfood scripts; they send/consume JSON and depend on
these routes and the current response fields.

The server defaults to loopback binding, but can be configured to bind elsewhere.
It does not implement authentication or authorization itself. Do not expose it
outside a trusted environment without an explicit security boundary.

## ADR-0031 gap assessment

The current binding has several gaps relative to the proposed common standard:

1. **Media type negotiation:** response media type is fixed to `application/json`;
   `Accept` is not evaluated.
2. **Request media type validation:** `Content-Type` is not checked before parsing
   JSON; unsupported media types are not rejected with 415.
3. **Problem Details:** errors use the existing `{"error": ...}` JSON object,
   not `application/problem+json`.
4. **Schemas:** the request and response structures are documented here and tested
   behaviorally, but are not yet published as standalone machine-readable HTTP
   schemas.
5. **Compatibility policy:** paths are unversioned and no formal deprecation
   window is published. The current MCP adapter and dogfood clients are the
   known in-repository consumers.

## Incremental adoption plan

Because ADR-0031 is still Proposed, this audit does not change the wire contract.
Once the standard is accepted, implementation should proceed incrementally:

1. Add machine-readable request/response schemas and explicit compatibility tests.
2. Add RFC 9457 Problem Details for clients that request
   `application/problem+json`, while retaining the existing JSON error form for
   legacy clients during a documented transition.
3. Validate `Content-Type` and negotiate `Accept` with explicit 415/406 behavior
   only after supported representations and fallback behavior are specified.
4. Introduce a vendor media type only if a durable domain representation needs
   explicit contract identification; do not add a media type or URL version for
   every additive field or implementation release.
5. Preserve the existing unversioned routes and current JSON defaults unless an
   approved compatibility decision specifies a migration path.

The language-neutral semantics remain authoritative in `FlossWare/loom-ai` and
`FlossWare/loom`; this document covers only this Python HTTP binding.
