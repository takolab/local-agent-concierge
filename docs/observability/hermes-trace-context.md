# Hermes Agent Trace Context Extraction

This document records how Hermes Agent joins the distributed trace that the
Slack Gateway starts, and the investigation that led to the approach. It is
the follow-up to the Slack Gateway's outgoing `traceparent` injection,
which at the time lived in that service's own direct Hermes client
(`apps/slack-gateway/src/slack_gateway/hermes_client.py`, since removed).

Nothing about Hermes' side changed when the Slack Gateway was rewired to
dispatch through the Orchestrator: the `traceparent` Hermes extracts is now
injected by `services/orchestrator/src/orchestrator/hermes_agent.py`, one
hop further up the same trace, using the same standard propagator. See
`docs/slack-gateway/orchestrator-dispatch.md` and
`docs/observability/orchestrator-trace-context.md`.

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

As verified at the time, with the Slack Gateway calling Hermes directly:

```text
concierge.request                (Slack Gateway)
  |
  +-- hermes.request              (Slack Gateway, CLIENT span, injects traceparent)
        |
        +-- /v1/responses         (Hermes Agent, SERVER span, extracted from traceparent)
```

The injecting span is now the Orchestrator's `hermes.request`, one hop
further down (`Slack Gateway → Orchestrator → Hermes Agent`). Hermes'
extraction side — everything this document is about — is unchanged.

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

## Known gap: Hermes' outbound MCP calls don't propagate trace context

Live verification while adding Google Calendar MCP telemetry (see
`docs/observability/google-calendar-mcp-telemetry.md`) confirmed that the
gap goes the other direction too: Hermes Agent's own outbound MCP tool calls
(e.g. to Google Calendar MCP) do not carry the trace context of the
`/v1/responses` request that triggered them. Each `tools/call` from Hermes
starts a fresh, unrelated trace rather than continuing the Slack-originated
one, even though Hermes' MCP client *is* independently instrumented and
correctly parents whichever server it calls.

This is a confirmed, tracked upstream issue, not something specific to this
deployment. Upstream status below is **as of 2026-09-18**, checked via the
GitHub REST API (`gh api repos/NousResearch/hermes-agent/...`); it will go
stale, so re-check before relying on it:

- [NousResearch/hermes-agent#60177](https://github.com/NousResearch/hermes-agent/issues/60177)
  (issue, open) — Hermes has no OpenTelemetry SDK in its own source, and its
  outbound MCP HTTP client sends no `traceparent`. Root cause per upstream
  triage: MCP tool calls run on a separate event-loop ("daemon") thread, and
  Python `contextvars` — which OpenTelemetry's active-span context relies
  on — do not cross a `run_coroutine_threadsafe` thread boundary. This
  matches exactly what was observed here: the trace visible on Hermes' MCP
  client span has no parent, even while Hermes' HTTP server span (for the
  same request) does correctly inherit the Slack Gateway's trace.
- [NousResearch/hermes-agent#78965](https://github.com/NousResearch/hermes-agent/pull/78965)
  (PR, **closed without merging** on 2026-09-06 — withdrawn by its author) —
  proposed an opt-in `mcp.trace_propagation: true` setting that injected a
  W3C `traceparent` HTTP header per MCP RPC. The author's withdrawal comment
  explains, in upstream's words and **not verified in this repository**:
  - it targeted the wrong layer: the MCP Python SDK Hermes pins
    (`mcp==2.0.0`) already propagates trace context in-protocol via the
    JSON-RPC `_meta` field (SEP-414), so the header was redundant;
  - what is actually missing is only the *parent* of the SDK's
    `MCP send tools/call` span — `_run_on_mcp_loop` hands the RPC to the
    loop thread via `run_coroutine_threadsafe`, where the agent thread's
    active span is not visible, so that span is always a root;
  - a plugin alone cannot fix it, because hook callbacks run on worker
    threads under `contextvars.copy_context()`;
  - the author said they would separately propose an opt-in
    `mcp_call_context` hook instead. As of 2026-09-18 no such proposal has
    been filed (a GitHub issue/PR search of NousResearch/hermes-agent for
    `mcp_call_context` returns zero results).

  The in-protocol part is consistent with what this repository has itself
  observed: Google Calendar MCP's extraction of a `traceparent` from `_meta`
  is covered by an in-repo test, and a live run showed Hermes Agent's
  `MCP send tools/call` spans and Google Calendar MCP's server spans sharing
  a trace ID with correct parenting (see "Incoming trace context" in
  `docs/observability/google-calendar-mcp-telemetry.md`). Which code path in
  Hermes or the SDK injects that context was not inspected here. The
  `mcp.trace_propagation` setting existed only on that closed PR and is not
  something this repository plans around.
- [NousResearch/hermes-agent#60466](https://github.com/NousResearch/hermes-agent/pull/60466)
  (PR, open, unmerged; no activity since 2026-07-15) — an earlier attempt
  that also adds a `traceparent` HTTP header, i.e. the same layer the #78965
  withdrawal comment calls wrong. Upstream's own review of it (an automated
  `hermes-sweeper` review, 2026-07-15, verdict "keep open") says it does not
  yet propagate an agent call's active trace context: it injects the header
  once at connection time rather than per tool call, and it does so on the
  MCP event-loop thread, where the caller's span is not visible. A merge of
  #60466 in its current shape would therefore not by itself resolve #60177.
- [briancaffey/hermes-otel](https://github.com/briancaffey/hermes-otel) — a
  separate third-party plugin providing a `get_current_traceparent` provider
  hook for the same problem. #78965 had been designed to accept it as a
  pluggable override; with that PR closed, there is no upstream mechanism for
  it to plug into. Per the #78965 withdrawal comment (not verified here),
  observer plugins such as this one cannot make the SDK's MCP span a child of
  their own tool span, for the thread-boundary reason above.

The release notes of the five most recent Hermes releases as of 2026-09-18
(v2026.8.27, v2026.8.31, v2026.9.7, v2026.9.11, v2026.9.14) do not mention
MCP trace propagation.

**Planned approach for this repository:** wait for an upstream change that
resolves #60177 by preserving the caller's active trace context across the
MCP event-loop thread boundary, and ships in a Hermes release. That change
could be a revised #60466, the announced (not yet filed) `mcp_call_context`
hook proposal, or something else. The acceptance condition is that behavior,
not a particular PR number merging. Once such a release exists, bump the
pinned tag in `apps/hermes-agent/Dockerfile` (currently
`nousresearch/hermes-agent:v2026.8.19`) and apply whatever configuration that
fix requires. What that configuration will be is not known yet. No Hermes
source patch or fork, and no auto-instrumentation beyond the existing
derived-image layer described above, is planned. This keeps the invariant the
rest of this file follows: Hermes' own source is never modified, even though
the image itself is a derived one. This is deliberately not implemented
yet: the timing and shape of an upstream fix are not in this repository's
control, and forking/vendoring an upstream patch directly (rather than
waiting) would break that invariant for an otherwise-untested integration
against this project's pinned version.

Joining these traces would be an observability improvement only: it would
show Hermes' MCP calls under the request's trace. It would not by itself
prove that a given tool call did or did not have side effects.

## Ownership boundary

Everything above is implemented entirely within `local-agent-concierge`
(`apps/hermes-agent/Dockerfile` + `docker-compose.yml`). No change to
NousResearch/hermes-agent source was needed, and none was made.
