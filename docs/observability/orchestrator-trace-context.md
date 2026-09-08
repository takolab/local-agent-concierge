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
37 tests): parent/child relationships and trace ids across the SERVER
span, the CLIENT span, and the `traceparent` header actually received by
a stub Hermes server; fallback for missing and malformed headers; context
isolation between requests and after exceptions; the closed attribute and
`error.type` sets; and sentinel-based redaction checks.

Also in CI, against the real container
(`.github/workflows/pytest.yml`): `POST /dispatch` with a `traceparent`
header returning the identical response, with no Collector reachable at
the configured endpoint — a live check that telemetry export failure does
not change dispatch behavior.

**Not verified end-to-end against the live stack.** No run of a real
Slack Gateway → Orchestrator → Hermes Agent request has been observed in
Phoenix or MLflow, because nothing calls the Orchestrator yet. The
Orchestrator's half of the chain is verified by the tests above; the
joined trace across all three services is not, and should be confirmed
when the Slack Gateway is rewired.
