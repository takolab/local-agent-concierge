# Hermes Agent Trace Context Extraction

This document records how Hermes Agent joins the distributed trace that the
Slack Gateway starts, and the investigation that led to the approach. It is
the follow-up to the Slack Gateway's outgoing `traceparent` injection
(`apps/slack-gateway/src/slack_gateway/hermes_client.py`).

## Background

Hermes Agent runs as the unmodified official Docker image
[`nousresearch/hermes-agent`](https://github.com/NousResearch/hermes-agent),
not as source code in this repository. Its `/v1/responses` endpoint is
implemented in Hermes core (`gateway/platforms/api_server.py`) as an aiohttp
`web.Application`.

At the time of this investigation, Hermes' plugin system had no way to see
the incoming HTTP request:

- Observer hooks (`register_hook`) only cover internal agent-loop events —
  session lifecycle, LLM provider calls, tool calls. None of them carry the
  incoming HTTP headers.
- Plugin middleware (`register_middleware`) only supports `tool_request`,
  `tool_execution`, `llm_request`, `llm_execution` — also internal, and
  `api_server.py` never invokes this middleware system at all.

So extracting `traceparent` requires code running inside the aiohttp request
path, which is not reachable through Hermes' documented extension points
without forking or patching Hermes' own source.

## Approach: OpenTelemetry auto-instrumentation

Rather than patching Hermes, `apps/hermes-agent/Dockerfile` layers standard
OpenTelemetry auto-instrumentation packages onto the official image.
`opentelemetry-instrumentation-aiohttp-server` patches aiohttp's
request-handling machinery generically, independent of how Hermes constructs
its `web.Application` — the standard OpenTelemetry way to instrument a
third-party Python HTTP server without modifying its source.

The base image (`nousresearch/hermes-agent:v2026.8.19`) already ships
`opentelemetry-api`/`opentelemetry-sdk` 1.39.1 and
`opentelemetry-exporter-otlp-proto-http` 1.39.1, used internally by Hermes'
own optional gateway-health/diagnostics exporter
(`agent/monitoring/otlp_exporter.py` — metrics and diagnostic events only,
unrelated to per-request tracing). The added instrumentation packages are
pinned to the matching `0.60b1` contrib release train so installing them does
not upgrade or otherwise disturb those existing packages.

## Activation: PYTHONPATH, not a command wrapper — a real incident

Instrumentation is activated by setting `PYTHONPATH` as a build-time `ENV` in
the Dockerfile, pointing at the
`opentelemetry.instrumentation.auto_instrumentation` package directory (the
same `sitecustomize.py`-based mechanism the `opentelemetry-instrument`
launcher itself uses internally). It is deliberately **not** activated by
wrapping the service's `command:` with `opentelemetry-instrument`, which was
the first approach tried and shipped a real production incident.

**What went wrong.** Wrapping the container's `command:` as
`opentelemetry-instrument hermes gateway run` (plus
`HERMES_GATEWAY_NO_SUPERVISE=1`, since s6 supervision otherwise relaunches
the gateway without that wrapper's injected environment) worked in isolated
testing against an empty `/opt/data` volume, and appeared to work on the
first deploy against the real persistent volume. On every restart after
that, the container crash-looped with:

```text
✗ A gateway is already running under s6 (container supervisor) for this profile.
  Starting another one from a shell leaves an orphan dispatcher that
  escapes the service, survives restarts, and writes to the same kanban
  DB concurrently — which can corrupt it. Restart the supervised gateway
  instead:

    hermes gateway restart
```

Reading Hermes' own source (`hermes_cli/gateway.py`,
`_guard_supervised_gateway_conflict` and `get_gateway_runtime_snapshot`)
showed why: on any profile with prior run state (`desired_state: running` in
its persisted `gateway_state.json` — true for any profile that has ever run
normally, which is every real deployment), Hermes' `02-reconcile-profiles`
cont-init step restores a dynamically s6-supervised `gateway-<profile>`
service on every container start, **independently of whatever this
service's `command:` says.** That reconciled service runs its own
`hermes gateway run --replace`, with no relation to this container's CMD.

A `command:`-level wrapper only instruments the one foreground process that
CMD starts. Editing state files (`gateway_state.json`,
`state/gateway.heartbeat`) to try to suppress the reconciled service was a
dead end — a fresh instance of the same conflict re-appeared on every
restart. Disabling supervision (`HERMES_GATEWAY_NO_SUPERVISE=1`) to keep the
wrapped process in place instead put it in direct conflict with
reconcile-profiles' own supervised restart — the crash loop above.

**The fix.** A container-wide `PYTHONPATH` has neither problem: s6 (via
`with-contenv`) re-injects the full container environment into every process
it supervises, including the dynamically reconciled `gateway-<profile>`
service. Instrumentation activates on whichever process ends up actually
serving requests, without needing to control — or fight — Hermes' own
supervision behavior at all. `docker-compose.yml` runs the plain, unmodified
`gateway run` command; no `HERMES_GATEWAY_NO_SUPERVISE` is set.

Verified against the real deployment (persistent `./data/hermes` volume,
`desired_state: running` already present from prior runs) by stopping and
restarting the container and confirming it stays `Up` with no
"already running" error, then sending a `traceparent`-tagged request and
confirming the matching span reached the OpenTelemetry Collector. An
automated regression test
(`test_container_restart_with_prior_state_does_not_crash_loop` in
`apps/hermes-agent/tests/test_trace_context_propagation.py`) reproduces the
restart-with-prior-state path directly.

## Verified trace relationship

```text
concierge.request                (Slack Gateway)
  |
  +-- hermes.request              (Slack Gateway, CLIENT span, injects traceparent)
        |
        +-- /v1/responses         (Hermes Agent, SERVER span, extracted from traceparent)
```

The incoming trace ID is inherited exactly; the incoming parent span ID
becomes the parent of Hermes' server span. Hermes never generates its own
trace ID when a valid `traceparent` is present. Missing or malformed
`traceparent` headers fall back to a normal root trace and do not affect the
request's HTTP response — this is the standard behavior of OpenTelemetry's
`TraceContextTextMapPropagator`, not custom code.

### End-to-end verification (manual)

Verified by building `apps/hermes-agent/Dockerfile` from
`nousresearch/hermes-agent:v2026.8.19`, running it with
`OTEL_TRACES_EXPORTER=otlp_proto_http` pointed at a real
`otel-collector` container using this repository's actual
`infra/observability/otel-collector.yaml`, and sending
`POST /v1/responses` with a hand-constructed `traceparent` header. The
Collector's `debug` exporter recorded:

```text
InstrumentationScope opentelemetry.instrumentation.aiohttp_server
Span #0
    Trace ID  : 4bf92f3577b34da6a3ce929d0e0e4736
    Parent ID : 00f067aa0ba902b7
    Name      : /v1/responses
    Kind      : Server
Attributes:
     -> http.scheme: Str(http)
     -> http.host: Str(localhost)
     -> http.route: Str(_handle_responses)
     -> http.method: Str(POST)
     -> http.status_code: Int(200)
```

Trace ID and Parent ID matched the injected `traceparent` exactly. Missing
and malformed `traceparent` requests both returned HTTP 200 and produced a
fresh root trace (`parent_id: null`). Span attributes are limited to standard
HTTP semantic-convention fields (scheme, host, route, method, status code) —
no headers, request bodies, or `Authorization` values are attached. Automated
versions of these checks live in
`apps/hermes-agent/tests/test_trace_context_propagation.py`.

## Scope

This covers only the HTTP boundary: one `SERVER` span per incoming Hermes
Agent request, correctly parented. It does not add spans for Hermes-internal
processing (LLM calls, tool calls, memory retrieval) — that is tracked as
Milestone 9 (Extended Observability) in `docs/roadmap.md`, and could reuse
Hermes' observer-hook plugin contract (the pattern the bundled Langfuse
plugin already uses) without any further changes to how trace context enters
Hermes.

## Ownership boundary

Everything above is implemented entirely within `local-agent-concierge`
(`apps/hermes-agent/Dockerfile` + `docker-compose.yml`). No change to
NousResearch/hermes-agent source was needed, and none was made.
