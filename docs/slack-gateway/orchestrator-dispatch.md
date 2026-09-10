# Slack Gateway → Orchestrator Dispatch

The Slack Gateway no longer calls Hermes Agent. It dispatches through the
Orchestrator's existing `POST /dispatch` boundary, and the Orchestrator
decides which Agent runs the request.

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

Every failure of this boundary becomes a `RuntimeError` — the same
exception type the message handler already caught around its previous
outbound call — so the **user-facing Slack behavior is unchanged**: the
existing `ERROR_MESSAGE` is posted in-thread and the processing status is
removed.

| Condition | Orchestrator's answer | Gateway's `RuntimeError` |
|---|---|---|
| Orchestrator unreachable | — (no response) | `Failed to connect to the Orchestrator` |
| Request exceeds the client timeout | — (no response) | `Orchestrator request timed out` |
| Unknown `agent_name` | `404 unknown_agent` | `Orchestrator returned HTTP 404` |
| Malformed request body | `400 invalid_request` | `Orchestrator returned HTTP 400` |
| Agent raised (e.g. Hermes unreachable, non-2xx, unusable body) | `500 internal_error` | `Orchestrator returned HTTP 500` |
| Body is not JSON | — | `Orchestrator response was not valid JSON` |
| Body is not a valid `AgentResponse` | — | `Orchestrator response was not a valid AgentResponse` |

These stay distinct in the *logs* rather than being collapsed into one
generic message, but all of them produce the same single Slack reply,
because a Slack user cannot act on the difference. None of the messages
carries the underlying exception, the response body, or the instruction
text.

Timeout is deliberately its own case rather than part of the
connection-failure one: they are materially different states, and the
client's own timeout (330s) is set slightly longer than the Orchestrator's
timeout on its Hermes call (300s) so a slow model run ends as the
Orchestrator's deliberate failure response rather than as a client-side
timeout racing it.

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

**Automated only.** 52 tests in `apps/slack-gateway/tests`, run in the
service's own container (`docker compose --profile test run --rm
slack-gateway-test`), which is what `.github/workflows/pytest.yml`
executes:

- `test_orchestrator_client.py` (19) — the request goes to `POST
  /dispatch` and not to `/v1/responses`; the body is exactly
  `{"agent_name", "request"}` with the 7 canonical `AgentRequest` fields,
  and round-trips back through `agent_request_from_dict` to the identical
  `AgentRequest`; the injected `traceparent` matches the active span's
  ids, computed through the OpenTelemetry API rather than hard-coded;
  no active span still dispatches successfully; no credential header is
  ever sent; a valid `AgentResponse` (including `proposed_actions` /
  `memory_candidates`) is returned unwrapped; and each failure above maps
  to its own bounded `RuntimeError` that leaks neither the body nor the
  instruction text.
- `test_slack_message_routing.py` (21) — a Slack message reaches the
  Orchestrator under `agent_name: "hermes"`; the `AgentRequest` carries
  the mapping in the table above (including `trace_id is None` and empty
  `permissions`); thread-root conversation identity; the summary is posted
  in-thread and the processing status removed; a dispatch failure shows
  the existing error message and never the failure detail; duplicate
  events dispatch once; the eight ignored-event shapes reach neither the
  Orchestrator nor Slack; the span active during `dispatch()` is the
  `orchestrator.dispatch` CLIENT span and a child of `concierge.request`;
  and the sentinel-based telemetry checks above.
- `test_telemetry.py` (5) — the renamed span's name, kind, attributes,
  sanitized error, and parent/child relationships.
- `test_config.py` (7) — `ORCHESTRATOR_BASE_URL`'s default, override and
  URL validation, and that setting the removed `HERMES_API_*` variables
  resurrects neither a credential nor a second dispatch authority.

`services/orchestrator` (94 passed, 1 skipped) was re-run unchanged, since
this change is written against its existing boundary.

**Not verified: the live stack.** No real Slack message has been sent
through this path, and no trace from it has been observed in Phoenix or
MLflow. Automated tests establish that the Gateway calls the Orchestrator
correctly and that a valid trace context is active when it does; they do
not establish that

```text
real Slack -> Slack Gateway -> Orchestrator -> Hermes Agent -> Ollama
           -> Collector -> Phoenix / MLflow
```

works end to end. That is a separate, human-controlled operational
validation gate, to be recorded the way
`docs/observability/orchestrator-trace-context.md`'s "End-to-end
verification (manual)" records the previous one — pinned to an exact
repository SHA and image digests.

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
