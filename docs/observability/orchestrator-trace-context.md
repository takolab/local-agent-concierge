# Orchestrator Trace Context Propagation

How `services/orchestrator` joins the distributed trace, and how that
relates to the trace context work already documented for the Slack
Gateway (`docs/roadmap.md` Milestone 5), Hermes Agent
(`docs/observability/hermes-trace-context.md`), and Google Calendar MCP
(`docs/observability/google-calendar-mcp-telemetry.md`).

The full design rationale lives in `docs/orchestrator/domain-model.md`
("Trace Context Propagation (Slice 5)"). This file covers only the
cross-service picture and the one distinction that is easy to get wrong.

## The chain

```text
concierge.request                  Slack Gateway (CONSUMER)
  |
  +-- hermes.request               Slack Gateway (CLIENT)   [today's path]
  |     |
  |     +-- /v1/responses          Hermes Agent (SERVER)
  |
  +-- POST /dispatch               Orchestrator (SERVER)    [the new path]
        |
        +-- hermes.request         Orchestrator (CLIENT)
              |
              +-- /v1/responses    Hermes Agent (SERVER)
                    |
                    X  tools/call  Google Calendar MCP -- starts a NEW trace
```

Both paths are real. The Slack Gateway still calls Hermes Agent directly;
nothing calls the Orchestrator yet. The Orchestrator path is what a
caller gets today by sending `POST /dispatch` itself, and is what the
Slack Gateway will get when it is rewired — a separate change.

The `X` is the known upstream gap: Hermes Agent's outbound MCP calls do
not carry trace context, so a Calendar tool call starts an unrelated
trace. That is
[tracked upstream](https://github.com/NousResearch/hermes-agent/issues/60177)
and unchanged by anything here — see "Known gap" in
`docs/observability/hermes-trace-context.md`.

## Two different mechanisms, often confused

| | `AgentRequest.trace_id` | W3C Trace Context |
|---|---|---|
| Carried in | the JSON request body | the `traceparent` / `tracestate` HTTP headers |
| Format | any non-empty string; none enforced | W3C format, validated by OpenTelemetry's propagator |
| Purpose | logical correlation, for log lines | parenting spans across services |
| Who sets it | nobody in this repository yet | the Slack Gateway; any instrumented caller |

The Orchestrator keeps them separate: it never builds an OpenTelemetry
parent context out of `AgentRequest.trace_id`, never lets that field
override the incoming HTTP context, and never writes it back into the
request. A request whose JSON `trace_id` disagrees with its HTTP
`traceparent` is propagated according to the HTTP context and forwarded
to the Agent exactly as received.

`traceparent` is caller-supplied and unauthenticated. It is never treated
as evidence of anything — neither is `AgentRequest.permissions`. Joining
a caller's trace grants no capability.

## Redaction

Same discipline as `docs/observability/collector-redaction.md`, which the
Collector then backstops. The Orchestrator emits only these attributes:

- `POST /dispatch` (SERVER): `concierge.operation`, `http.method`,
  `http.route`, `http.status_code`, plus `error.type` on a 5xx.
- `hermes.request` (CLIENT): `concierge.downstream.service`,
  `concierge.operation`, `http.status_code` on an HTTP failure, plus
  `error.type` on any failure.

`error.type` is a closed four-value vocabulary. No instruction text,
Hermes response text, `Authorization` value, API key, exception message,
traceback, or per-user/conversation identifier is ever attached — and
`agent_name` is deliberately excluded too, being an unbounded string from
an unauthenticated caller.

There is no auto-instrumentation in this service: no
`opentelemetry-instrument`, no `sitecustomize` hook, no instrumented HTTP
library. Every span is created by hand, which is what makes that closed
attribute list assertable at all — unlike Hermes Agent, which is
auto-instrumented and therefore relies on the Collector's redaction
processor as a second line of defense.

## Baggage

The default propagator is `tracecontext,baggage`, so a caller's `baggage`
header is parsed — but it is not forwarded to Hermes and never becomes
span data (see `docs/orchestrator/domain-model.md`, "Baggage", for why,
and for the test that pins it). Set the standard
`OTEL_PROPAGATORS=tracecontext` if you want that guaranteed by
configuration rather than observed.

## Configuration

`OTEL_EXPORTER_OTLP_ENDPOINT` (`http://otel-collector:4317` in
`docker-compose.yml`), the same OTLP/gRPC endpoint the Slack Gateway and
Google Calendar MCP use. Spans go to the Collector only; Phoenix and
MLflow are the Collector's business.

`OTEL_SDK_DISABLED=true` turns tracing off entirely. Dispatch behavior is
identical either way, and the `orchestrator` service deliberately has no
`depends_on: otel-collector` — an absent or failing Collector must never
delay or fail a dispatch.

## Verification status

Automated, in CI (`services/orchestrator/tests/test_trace_propagation.py`,
40 tests): parent/child relationships and trace ids across the SERVER
span, the CLIENT span, and the `traceparent` header actually received by
a stub Hermes server; fallback for missing and malformed headers;
repeated `tracestate` header fields surviving in order all the way to the
header Hermes receives, and repeated `traceparent` fields starting a
fresh root trace instead of resolving to either of them; context
isolation between requests and after exceptions; the closed attribute and
`error.type` sets; and sentinel-based redaction checks.

Also in CI, against the real container
(`.github/workflows/pytest.yml`): `POST /dispatch` with a `traceparent`
header returning the identical response, with no Collector reachable at
the configured endpoint — a live check that telemetry export failure does
not change dispatch behavior.

### End-to-end verification (manual)

Run on 2026-09-10 against the real stack — the actual `orchestrator`,
`hermes-agent` and `ollama` containers, exporting through the real
`otel-collector` to Phoenix and MLflow. No stubs. Two `POST /dispatch`
requests with `agent_name: "hermes"`, differing only in whether they
carried an incoming `traceparent`.

**With an incoming `traceparent`.** Sent
`00-7075ec6bb22fa7f31f5840bceaa7850f-40d6e99612795bc1-01`; HTTP 200 in
9.4s with the model's real answer. Phoenix's span API
(`GET /v1/projects/{project}/spans?trace_id=...`) returned three spans on
that one trace, whose `span_id`/`parent_id` values chain exactly:

```text
40d6e99612795bc1                             <- the traceparent that was sent
  └─ a6019548472c8eaf   POST /dispatch       orchestrator   SERVER
       └─ 99f4b9329c8063be   hermes.request  orchestrator   CLIENT
            └─ 31e467eae5fc6fb2   /v1/responses   hermes-agent   SERVER
```

The last link is the one that could not be proven before: every earlier
check of `hermes.request` → Hermes' own SERVER span used a stub Hermes
server, not the real, auto-instrumented one.

**Without a `traceparent`.** `POST /dispatch` became a true root span and
the same three-span tree formed beneath it.

**Attributes actually stored in Phoenix.** The two Orchestrator spans
carried exactly the closed sets documented above, and nothing else:

```text
POST /dispatch   concierge.operation, http.method, http.route,
                 http.status_code
hermes.request   concierge.downstream.service, concierge.operation
```

(`hermes.request` shows no `http.status_code` because that is only set on
a failure, and this call succeeded.) Hermes Agent's own `/v1/responses`
span — produced by its auto-instrumentation, not by this repository —
carried standard HTTP semantic-convention fields only: scheme, host,
method, route, target, url, status code, flavor, server name, port and
user agent. No request or response body, and no `Authorization`.

No span carried `redaction.masked.count`, only `redaction.ignored.count`
— nothing was flagged sensitive by the Collector because nothing
sensitive was sent.

**Redaction, checked with sentinels.** The instruction text, the model's
response text, `user_id`, `conversation_id`, and any `Bearer` /
`Authorization` string were each searched for across both Phoenix's
stored spans and the Collector's debug output: **zero occurrences**.

The request also carried a deliberately mismatched JSON
`"trace_id": "json-side-correlation-id"`. It appears nowhere in the
telemetry, and propagation followed the HTTP `traceparent` — confirming
on the live stack what "`AgentRequest.trace_id` is not W3C Trace Context"
above asserts.

Incidentally, `/v1/responses` carries
`http.user_agent: Python-urllib/3.12`, which identifies the caller as the
Orchestrator rather than the Slack Gateway (which uses `httpx`) — useful
when telling the two paths apart in a backend.

### MLflow shows `IN_PROGRESS` for a synthetic parent — expected

The first request's trace sat at `state: IN_PROGRESS` in MLflow while the
second showed `state: OK`. This is not a defect and not something to fix:
the first request's `traceparent` named a parent span that does not
exist, because the "caller" was a hand-written header rather than a real
instrumented service, so MLflow waits for a root span that will never
arrive. The second request sent no `traceparent`, making the
Orchestrator's own SERVER span the root, and MLflow settled immediately.

Confirmed by running exactly that second request rather than assuming.
Worth knowing before wiring the Slack Gateway: with a real caller the
root span does exist, so this should not appear — if it does, the caller's
own span is not reaching the Collector, which is a different problem.

### Still not verified

- **The Slack Gateway as the caller.** It still calls Hermes Agent
  directly (`apps/slack-gateway/src/slack_gateway/hermes_client.py`), so
  nothing links `concierge.request` to `POST /dispatch` yet. A Slack
  message produces the Milestone 5 trace, not this one. That link is the
  next slice's to prove.
- **The error paths, on the live stack.** Hermes returning a non-success
  status, an unreachable Hermes, and an unusable response body are all
  covered by automated tests, including the `error.type` values recorded
  — but none has been observed live.
- **Hermes Agent's outbound MCP calls**, which remain an upstream gap
  (see "The chain" above) and are unaffected by any of this.
