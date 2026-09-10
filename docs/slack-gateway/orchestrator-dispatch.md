# Slack Gateway → Orchestrator Dispatch

The Slack Gateway no longer calls Hermes Agent. It dispatches through the
Orchestrator's existing `POST /dispatch` boundary, which runs the request
through the Orchestrator's registered Agent boundary.

**The Gateway still selects the Agent.** It sends the fixed `agent_name`
`"hermes"`. The Orchestrator owns dispatch; it does not yet classify
requests or select Agents. What moved is the dispatch, not the choice.

```text
Before                          After

Slack Gateway                   Slack Gateway
  |                               |
  | POST /v1/responses            | POST /dispatch
  v                               v
Hermes Agent                    Orchestrator
                                  |
                                  | POST /v1/responses
                                  v
                                Hermes Agent
```

This is Milestone 7's "the Slack Gateway sends all normalized requests to
the Orchestrator" completion criterion. It is **not** its
agent-*selection* criterion: the Gateway still names one Agent explicitly
(`"hermes"`), because the Orchestrator has no request classification yet.
See "What this does not do" below.

## Where each piece lives

| Concern | Code |
|---|---|
| Slack event → `AgentRequest`, reply delivery | `apps/slack-gateway/src/slack_gateway/slack_app.py` (`handle_slack_message`) |
| `POST /dispatch` HTTP call, contract (de)serialization, failure mapping | `apps/slack-gateway/src/slack_gateway/orchestrator_client.py` |
| The `orchestrator.dispatch` CLIENT span | `apps/slack-gateway/src/slack_gateway/telemetry.py` |
| Orchestrator address | `apps/slack-gateway/src/slack_gateway/config.py` (`ORCHESTRATOR_BASE_URL`) |
| The receiving end (unchanged by this change) | `services/orchestrator/src/orchestrator/http_server.py` |

Nothing in `services/orchestrator` or `packages/agent-contracts` changed:
the Gateway was written to the boundary that already existed.

## The request

The body is exactly the shape `POST /dispatch` already accepts — an
`agent_name` plus a serialized `AgentRequest`, produced by
`agent_contracts`' own `agent_request_to_dict`:

```json
{
  "agent_name": "hermes",
  "request": {
    "task_id": "Ev09ABCDEF",
    "user_id": "U01234567",
    "conversation_id": "slack:T0123:C0123:1700000000.000100",
    "instruction": "the Slack message text",
    "memory_scopes": [],
    "permissions": [],
    "trace_id": null
  }
}
```

### How Slack state maps onto the contract

| Field | Value | Why |
|---|---|---|
| `task_id` | the Slack `event_id` | Already this Gateway's unit of work: it is what the deduplicator claims, and what every correlation log line is keyed by. One Slack event is one task. |
| `user_id` | the Slack `user` id | The opaque author identifier. `AgentRequest` attaches no identity or authorization semantics to it, and neither does anything downstream. |
| `conversation_id` | `slack:<workspace>:<channel>:<thread root ts>` | **Byte-for-byte the string the Gateway previously sent to Hermes as `conversation`**, and the Orchestrator's Hermes adapter forwards `conversation_id` into that same field — so Hermes-side thread continuity is preserved across the rewiring, not re-keyed. |
| `instruction` | the stripped message text | Same value, same stripping, as before. |
| `memory_scopes` | `[]` | No memory scope grammar exists yet (Milestone 8). Sending a guess would be inventing one. |
| `permissions` | `[]` | Nothing in this path authorizes anything, and `permissions` is explicitly *not* an authorization boundary (`docs/orchestrator/domain-model.md`). An empty list is the honest value. |
| `trace_id` | `null` | See below. |

### `AgentRequest.trace_id` is not the trace context

The Gateway deliberately leaves `trace_id` unset rather than copying the
active W3C trace id into it.

| | `AgentRequest.trace_id` | W3C Trace Context |
|---|---|---|
| Travels in | the JSON body | the `traceparent` / `tracestate` headers |
| Set by the Gateway | no — stays `null` | yes — injected by the propagator |
| Read by | correlation log lines only | OpenTelemetry, to parent a span |

Populating it from trace context would make an application-level
correlation field into a second, redundant propagation mechanism whose
format nothing validates — and the Orchestrator would still (correctly)
ignore it, since it propagates from the HTTP headers only. What a caller
*should* put in `trace_id` is still an open schema question
(`docs/agent-contracts/domain-model.md`, open question 1); this change
does not resolve it, it just does not answer it wrongly.
`test_dispatched_request_trace_id_is_not_the_w3c_trace_id` pins the
behavior.

## Trace context

```text
concierge.request        Slack Gateway (CONSUMER)
  |
  +-- orchestrator.dispatch   Slack Gateway (CLIENT)
  |     |
  |     +-- POST /dispatch    Orchestrator (SERVER)
  |           |
  |           +-- hermes.request   Orchestrator (CLIENT)
  |                 |
  |                 +-- /v1/responses   Hermes Agent (SERVER)
  |
  +-- slack.response       Slack Gateway (CLIENT)
```

The Gateway's outbound span was renamed from `hermes.request` to
`orchestrator.dispatch` (attributes `concierge.downstream.service:
"orchestrator"`, `concierge.operation: "dispatch"`), because the service
it calls changed. `hermes.request` still exists in the trace — emitted by
the Orchestrator, for the hop it now owns — so the two hops stay
distinguishable instead of collapsing under one name.

Propagation itself is unchanged in mechanism from what this package
already did: the active context is handed to the configured propagator
(`opentelemetry.propagate.inject`) into a fresh dict, which is then
merged over the outgoing request's headers. No `traceparent` is
constructed or parsed by hand anywhere in this Gateway. The Orchestrator
extracts it with the propagator on the other side, exactly as it already
did for any caller.

**Tracing availability is not dispatch availability.** With no recording
span — tracing disabled, or no provider installed — `inject()` writes
nothing, the request carries no `traceparent`, and the dispatch proceeds
and is interpreted identically
(`test_dispatch_without_active_span_still_succeeds`). Trace context is
also never read as trust evidence: it grants nothing, and `POST /dispatch`
has no authentication either way.

## The response

The body is parsed with `agent_contracts`' own `agent_response_from_dict`
and returned as an `AgentResponse`. `summary` is what gets posted to the
Slack thread.

`status` is logged but deliberately **not** branched on. No
`AgentResponse.status` vocabulary is defined anywhere in this repository
(`docs/agent-contracts/domain-model.md`, open question), and
`Orchestrator.dispatch()` itself returns whatever status the Agent
produced without inspecting it. Inventing a mapping here would be this
service guessing at a contract that does not exist yet; delivering the
summary is what this path did before the rewiring for every successful
response. This is the seam where a future `needs_approval` status would
be handled — that is Milestone 6/7 work, not this change.

## Failure semantics

A failure of this boundary is not always evidence that nothing happened,
so the Gateway distinguishes two outcomes and says a different thing for
each. Both are `RuntimeError` subclasses, so the handler's existing
`except RuntimeError` still catches everything; the type only decides what
the user is told.

| Condition | Orchestrator's answer | Exception | Message |
|---|---|---|---|
| Orchestrator unreachable (`ConnectError`, `ConnectTimeout`, `PoolTimeout`, `ProxyError`, `UnsupportedProtocol`, `LocalProtocolError`) | — (no response) | `DispatchFailedError`: `Failed to connect to the Orchestrator` | `ERROR_MESSAGE` |
| Unknown `agent_name`, or an unknown path | `404 unknown_agent` / `not_found` | `DispatchFailedError`: `Orchestrator returned HTTP 404` | `ERROR_MESSAGE` |
| Malformed request body | `400 invalid_request` / `invalid_json` | `DispatchFailedError`: `Orchestrator returned HTTP 400` | `ERROR_MESSAGE` |
| Agent raised (Hermes unreachable, non-2xx, unusable body) | `500 internal_error` | `DispatchOutcomeUnknownError`: `Orchestrator returned HTTP 500` | `UNKNOWN_OUTCOME_MESSAGE` |
| Any other status (an intermediary's `502`, an undefined `4xx`, …) | — | `DispatchOutcomeUnknownError`: `Orchestrator returned HTTP <code>` | `UNKNOWN_OUTCOME_MESSAGE` |
| Read/write timeout | — (no response) | `DispatchOutcomeUnknownError`: `Orchestrator request timed out` | `UNKNOWN_OUTCOME_MESSAGE` |
| Transport error after the request was written (`ReadError`, `WriteError`, `CloseError`, `RemoteProtocolError`, …) | — (no response) | `DispatchOutcomeUnknownError`: `Lost contact with the Orchestrator` | `UNKNOWN_OUTCOME_MESSAGE` |
| `2xx` body is not JSON | `200` | `DispatchOutcomeUnknownError`: `Orchestrator response was not valid JSON` | `UNKNOWN_OUTCOME_MESSAGE` |
| `2xx` body is not a valid `AgentResponse` | `200` | `DispatchOutcomeUnknownError`: `Orchestrator response was not a valid AgentResponse` | `UNKNOWN_OUTCOME_MESSAGE` |

None of the messages carries the underlying exception, the response body,
or the instruction text. The distinctions above stay visible in the logs
and on the span (`error.type` is `orchestrator.request_error` or
`orchestrator.outcome_unknown`).

### Why two messages, and not one

The Agent reachable through this path is **tool-capable**, not text-only.
Hermes Agent's `/v1/responses` runs its configured toolsets and MCP
servers: this repository has verified a real Terminal Tool side effect
(`docs/roadmap.md` Milestone 2, "A controlled file-writing test confirmed
that the Terminal Tool side effect occurred exactly once"), the runtime
config carries a `terminal:` backend, and a real Slack message has been
observed producing live `tools/call list_events` requests to the Google
Calendar MCP (`docs/observability/google-calendar-mcp-telemetry.md`).

So a dispatch whose outcome is unknown may have already executed a tool.
Telling the user *"Please try again"* in that state invites an immediate
retry that can duplicate a side effect. The two messages are:

```text
ERROR_MESSAGE            :warning: I couldn't complete that request. Please try again.
UNKNOWN_OUTCOME_MESSAGE  :warning: I lost contact while the request was being
                         processed. The result is unknown, so please check
                         before retrying.
```

This is the one place the rewiring **deliberately changes user-facing
Slack behavior**. Everything else on the failure path — the processing
status cleanup, the log lines, the span handling — is unchanged.

### Classification is fail-safe

Only two things are treated as definite failures, both allowlists in
`orchestrator_client.py`:

- `_NOT_DELIVERED_ERRORS` — the httpx errors that provably occur before
  any byte of the request is delivered.
- `_AGENT_NOT_STARTED_STATUSES` — `400` and `404`, the only statuses the
  Orchestrator emits strictly *before* `Orchestrator.dispatch()` runs
  (body parsing, `AgentRequest` validation, registry lookup, unknown
  path). No Agent has been called when either is returned.

Everything else — `500`, any other status, and a `RuntimeError` from code
this module did not classify — is reported as an unknown outcome.

That direction is deliberate: showing "the result is unknown" when nothing
actually ran costs the user an unnecessary check, while showing "please
try again" after a tool ran can duplicate a real side effect. The
allowlist shape also means a future httpx release adding an error class,
or an intermediary returning a status this contract never defined, cannot
silently make an ambiguous outcome look safe.

The two exception types are **siblings**, not parent and child, so an
`except DispatchFailedError` cannot silently swallow the unknown case.

### The timeout values do not establish an ordering

The client timeout (330s) is set above the Orchestrator's own timeout on
its Hermes call (300s) so that, in the ordinary case, the Gateway is still
waiting when the Orchestrator gives up and answers. **That is best-effort
ordering, not a guarantee, and nothing should be built on it as one.**

`httpx.Client(timeout=...)`'s single value configures connect / read /
write / pool *inactivity* timeouts — not a total end-to-end request
deadline — and the Orchestrator's own `urllib` timeout is socket-level in
the same way. Neither side has an execution deadline, so `330 > 300` does
not establish that the Orchestrator always finishes first. That is
precisely why the unknown-outcome path above has to exist rather than
being argued away.

### Why `500 internal_error` is an unknown outcome

`500` is the Orchestrator's single generic answer for two very different
things:

```text
HermesAgent.handle()
  ├─ _call_hermes() raises          -> Hermes never ran      -> 500
  └─ _call_hermes() returns, then
     _extract_output_text() raises  -> Hermes RAN, tools too -> 500
```

The second path is not hypothetical: `HermesAgent.handle()` extracts the
output text only after the Hermes call has returned, so an extraction
failure means Hermes completed a full run — tool calls included — and only
the text could not be read out. `http_server.py` maps any Agent exception
to the same `{"error": "internal_error"}` body, so the Gateway has nothing
to tell them apart with.

A `500` therefore does not prove that nothing happened, and classifying it
as a definite failure would contradict the fail-safe rule above. It is an
unknown outcome.

**The cost of this is accepted, not hidden.** When Hermes is simply down —
the most common failure in practice — nothing ran, and the user is still
told to check before retrying. That is false-caution, which is the
direction this boundary errs in on purpose.

The precise fix belongs on the other side: the Orchestrator returning
failure *provenance* — "agent not started" versus "agent outcome unknown"
— instead of one generic `500`. That is an API-surface change, out of
scope for this slice, and recorded in `docs/roadmap.md` alongside the
deadline/idempotency prerequisite.

### The larger contract this defers

Splitting the message is the smallest correct change; it is not an
execution-deadline or idempotency protocol. Neither side of this boundary
has an execution deadline, and nothing here makes a retry safe — it only
stops the Gateway from *claiming* one is. Before an Agent that performs
consequential writes is reachable through this path, this boundary needs
an explicit deadline or idempotency contract (see `docs/roadmap.md`
Milestone 6).

## Credentials

The Slack Gateway no longer holds a Hermes credential at all.
`HERMES_API_BASE_URL` and `HERMES_API_SERVER_KEY` were removed from its
configuration and from its Compose service; the Orchestrator, which owns
the Hermes hop, is the only service that still has them. No
`Authorization` header is sent to `POST /dispatch` — that endpoint has no
authentication (`docs/orchestrator/domain-model.md`, "Authorization
boundary"), and forwarding a bearer credential to an endpoint that does
not use it would only widen its blast radius.
`test_dispatch_sends_no_credential_header` pins this.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `ORCHESTRATOR_BASE_URL` | `http://orchestrator:8700` | Validated as an HTTP(S) URL at startup, same as the removed `HERMES_API_BASE_URL` was. |

One variable controls this path, not two: the removed Hermes settings are
not kept as a fallback, so there is no second dispatch authority and no
silent way back to the direct path.

The Compose service's `depends_on` now points at `orchestrator`
(`condition: service_healthy`, which is affordable because `orchestrator`
itself depends on nothing) instead of `hermes-agent`. The image now
builds from the repository root, because it installs
`packages/agent-contracts` before its own package — the same reason and
the same shape as `services/orchestrator/Dockerfile`.

## Telemetry safety

Unchanged boundary, re-checked for the new path: no span carries Slack
message text, the agent summary, the Slack user / channel / workspace /
event ids, the message timestamp, or any credential
(`test_no_span_carries_slack_content_or_identifiers`, using synthetic
sentinels). Spans are still created with `record_exception=False` and
`set_status_on_exception=False`, so no exception message or stack trace
becomes a span attribute; failures are recorded as a status plus a bounded
`error.type` (`orchestrator.request_error`).

The correlation *log lines* still carry Slack identifiers, as before —
they are local-only and never exported. The instruction text has never
been logged and still is not.

No Collector configuration change was needed: `error.type` and the
`concierge.*` attribute keys are already on
`infra/observability/otel-collector.yaml`'s `ignored_keys` list, and only
their values changed.

## Verification

**Automated only.** 82 tests in `apps/slack-gateway/tests`, run in the
service's own container (`docker compose --profile test run --rm
slack-gateway-test`), which is what `.github/workflows/pytest.yml`
executes:

- `test_orchestrator_client.py` (43) — the request goes to `POST
  /dispatch` and not to `/v1/responses`; the body is exactly
  `{"agent_name", "request"}` with the 7 canonical `AgentRequest` fields,
  and round-trips back through `agent_request_from_dict` to the identical
  `AgentRequest`; the injected `traceparent` matches the active span's
  ids, computed through the OpenTelemetry API rather than hard-coded;
  no active span still dispatches successfully; no credential header is
  ever sent; a valid `AgentResponse` (including `proposed_actions` /
  `memory_candidates`) is returned unwrapped; and each failure above maps
  to its own bounded exception that leaks neither the body nor the
  instruction text; every httpx error that provably precedes delivery is a
  `DispatchFailedError` and every one that can follow it is a
  `DispatchOutcomeUnknownError`; the two types are siblings, so neither
  can be swallowed by a handler written for the other; and
  `dispatch_error_type` classifies anything unrecognized — including a
  bare `RuntimeError` — as an unknown outcome. On the status side, `400`
  and `404` (each with both of the Orchestrator's documented error bodies)
  are definite failures, while `500` and five unexpected statuses
  (`401`, `403`, `429`, `502`, `503`) are unknown outcomes.
- `test_slack_message_routing.py` (24) — a Slack message reaches the
  Orchestrator under `agent_name: "hermes"`; the `AgentRequest` carries
  the mapping in the table above (including `trace_id is None` and empty
  `permissions`); thread-root conversation identity; the summary is posted
  in-thread and the processing status removed; a dispatch failure shows
  the existing error message and never the failure detail; duplicate
  events dispatch once; the eight ignored-event shapes reach neither the
  Orchestrator nor Slack; the span active during `dispatch()` is the
  `orchestrator.dispatch` CLIENT span and a child of `concierge.request`;
  and the sentinel-based telemetry checks above. A definite failure shows
  the retry message while an unknown outcome shows the
  "result is unknown" one — asserted on the property (says "unknown", does
  not say "try again"), not only on the exact wording — and an
  unclassified `RuntimeError` takes the unknown branch.
- `test_telemetry.py` (8) — the renamed span's name, kind, attributes,
  sanitized error, parent/child relationships, and the `error.type`
  recorded for each of the two outcome classifications plus an
  unclassified error.
- `test_config.py` (7) — `ORCHESTRATOR_BASE_URL`'s default, override and
  URL validation, and that setting the removed `HERMES_API_*` variables
  resurrects neither a credential nor a second dispatch authority.

`services/orchestrator` (94 passed, 1 skipped) was re-run unchanged, since
this change is written against its existing boundary.

**Live: one successful run.** On 2026-09-10 a real Slack message was sent
through this path at repository SHA `0bcceb95` and produced the joined
trace `ff731430ed03161076ae1857d8dea219` — all six expected spans, correct
parent/child links, present in MLflow with `state=OK`, with no Slack
identifier, conversation id, message timestamp or bearer credential
reaching either backend.

```text
real Slack -> Slack Gateway -> Orchestrator -> Hermes Agent -> Ollama
           -> Collector -> Phoenix / MLflow
```

That is **one run of the success path**, not a verified failure surface:
the timeout, unknown-outcome and non-2xx paths above are covered by tests
only and have never been observed live. The repeatable procedure, the
evidence record, and what that run did and did not settle are in
`docs/observability/slack-orchestrator-live-validation.md`.

## What this does not do

- **No agent selection.** The Gateway names `"hermes"` explicitly. The
  Orchestrator still has no request classification, so moving the *choice*
  of Agent out of the Gateway is still open in Milestone 7 — what moved
  here is the *dispatch*.
- **No authentication** on `POST /dispatch`, unchanged. The hop is
  container-to-container on `concierge-network`.
- **No approval, memory, or multi-agent behavior**, and no change to
  `AgentRequest` / `AgentResponse`, the Orchestrator, or Hermes Agent.
- **No retry, fallback, or circuit breaking.** One request, one
  Orchestrator, one failure message.
- **No execution-deadline or idempotency contract.** The Gateway now
  *reports* an ambiguous outcome honestly, but nothing makes a retry safe
  — see "Failure semantics" above. This must be resolved before an Agent
  performing consequential writes is reachable through this path.
- **No failure provenance from the Orchestrator.** `500` covers both "the
  Agent was never called" and "the Agent ran, then failed", so the Gateway
  must treat every `500` as an unknown outcome — false-cautious when
  Hermes was merely down. Making this precise means the Orchestrator
  reporting which of the two happened.
