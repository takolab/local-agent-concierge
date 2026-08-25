# Google Calendar MCP Telemetry

This document records how the Google Calendar MCP service is instrumented
with OpenTelemetry, why the implementation is small, and what remains
follow-up work.

## Background

The Google Calendar MCP service (`mcp/google-calendar`) depends on `mcp>=2`,
the official Python MCP SDK. Investigating that dependency for this work
showed that the SDK already ships built-in OpenTelemetry instrumentation:

- `mcp.server._otel.OpenTelemetryMiddleware` wraps every inbound JSON-RPC
  message (including every `tools/call`) in an OpenTelemetry span. It is
  enabled unconditionally — `mcp.server.lowlevel.server.Server.__init__` sets
  `self.middleware = [OpenTelemetryMiddleware()]` by default, so `MCPServer`
  (used by `google_calendar_mcp.server`) already produces spans for tool
  calls, with no application code required to create them.
- `mcp.shared._otel.extract_trace_context` extracts a W3C `traceparent` from
  the JSON-RPC request's `_meta` field (via the standard
  `opentelemetry.propagate.extract`) and parents the span under it when
  present, falling back to a root span otherwise.
- The middleware already sanitizes tool failures: `MCPServer.call_tool()`
  (via `mcp.server.mcpserver.server._handle_call_tool`) catches every
  exception raised by a tool — `ToolError` and any other exception, which it
  wraps as `UnexpectedToolError` — and always returns a
  `CallToolResult(is_error=True, ...)` rather than letting the exception
  propagate. The OTel middleware only ever sees that sanitized result for a
  tool failure; it never calls `record_exception` or embeds `str(exception)`
  for the `tools/call` path, so raw exception text never reaches the span.

Given this, the natural, minimal-footprint design is to configure the
OpenTelemetry SDK (a `TracerProvider` with a `Resource` and an OTLP exporter)
the same way `apps/slack-gateway` already does, and let the SDK's own
instrumentation produce the spans. No custom span-creation code was added to
`server.py` or `calendar_client.py`, and no application error-handling
behavior was changed.

## Implementation

`mcp/google_calendar_mcp/telemetry.py` mirrors
`apps/slack-gateway/src/slack_gateway/telemetry.py`:

```python
def configure_tracing() -> None:
    tracer_provider = TracerProvider(resource=_build_resource())
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter())
    )
    trace.set_tracer_provider(tracer_provider)
```

`_build_resource()` sets:

```text
service.name      = google-calendar-mcp
service.namespace = local-agent-concierge
```

matching the Slack Gateway's `service.namespace` convention.
`configure_tracing()` is called once, in `__main__.py`, before
`mcp.run(transport="streamable-http", ...)` starts. The OTLP exporter uses
the gRPC exporter (`opentelemetry-exporter-otlp-proto-grpc`), the same
package Slack Gateway uses, reading its endpoint from the standard
`OTEL_EXPORTER_OTLP_ENDPOINT` environment variable. `docker-compose.yml` sets
this to `http://otel-collector:4317` for the `google-calendar-mcp` service,
identical to the Slack Gateway's configuration.

`opentelemetry-api`, `opentelemetry-sdk`, and
`opentelemetry-exporter-otlp-proto-grpc` were added to
`mcp/google-calendar/pyproject.toml` pinned to the same `>=1.44.0,<2.0` range
Slack Gateway uses (`opentelemetry-api` was already an indirect dependency of
`mcp`, but is now also declared directly since `telemetry.py` imports it).

## Trace behavior

Every `tools/call` request produces a `SERVER` span. Its name and attributes
come entirely from the SDK's own middleware, not application code:

```text
tools/call get_current_datetime
```

Observed attributes (all low-cardinality, bounded by the tool/method
vocabulary):

```text
mcp.method.name        = "tools/call"
mcp.protocol.version    = "<negotiated MCP protocol version>"
gen_ai.operation.name   = "execute_tool"
gen_ai.tool.name        = "<one of the 6 known tool names>"
jsonrpc.request.id      = "<request id>"
```

On a tool failure (validation error or an unexpected exception, e.g. a
Google API failure), the span status becomes `ERROR` with:

```text
error.type = "tool_error"
```

and no other attribute, event, or status description — matching the
sanitized, low-cardinality classification style already used by
`error.type = "hermes.request_error"` / `"slack.response_error"` in the
Slack Gateway.

### Verified span export

Verified with `docker compose up -d google-calendar-mcp otel-collector` and a
real streamable-HTTP MCP client call from a separate container on the same
Compose network (no Google credentials required for `get_server_status` /
`get_current_datetime`). The Collector's `debug` exporter recorded, among
others:

```text
Resource attributes:
     -> service.name: Str(google-calendar-mcp)
     -> service.namespace: Str(local-agent-concierge)
Span
    Name  : tools/list
    Kind  : Server
Attributes:
     -> mcp.method.name: Str(tools/list)
```

(`tools/list` is shown because it is what the verification client's session
setup calls; `tools/call <tool>` spans use the same shape, confirmed by the
automated tests below.)

### Incoming trace context

The Google Calendar MCP side of context extraction requires no code — it is
the SDK's built-in `extract_trace_context`, exercised directly by
`tests/test_telemetry.py::test_incoming_trace_context_is_joined_when_present`,
which sends a synthetic `traceparent` in a tool call's `_meta` and confirms
the resulting span's trace ID and parent span ID match it.

Whether Hermes Agent's own MCP client actually sends that `_meta` today was
out of scope to change (Hermes Agent is the unmodified
`nousresearch/hermes-agent` image; see
`docs/observability/hermes-trace-context.md`), but it was possible to
*observe* real Hermes Agent traffic during this work's Docker Compose
verification: when the locally running Hermes Agent container reconnected to
a recreated `google-calendar-mcp` container, its own OpenTelemetry
instrumentation (the same `mcp-python-sdk` instrumentation scope, `service.name
= hermes-agent`) emitted `Kind: Client` spans named `MCP send initialize`
etc., and the corresponding Google Calendar MCP `Kind: Server` spans (`initialize`,
`tools/list`) shared the same trace ID and were parented under them —
without any change on either side. This shows the mechanism already works
for MCP session-management requests between the real Hermes Agent and
Google Calendar MCP.

This was an incidental observation of session-handshake traffic
(`initialize`, `ping`, `tools/list`), not a `tools/call` triggered by an
actual user request end-to-end through Slack. Confirming that a real
Slack-triggered `concierge.request` → `hermes.request` chain also continues
into a `tools/call <tool>` span on Google Calendar MCP is recorded below as
manual follow-up — nothing in the mechanism suggests it would behave
differently, since the same MCP client code path is used for every request
type, but it has not been directly observed.

## Privacy / sensitive-data boundary

None of the following are ever placed in a span attribute, span name, event,
or status description, because the SDK's built-in middleware never inspects
tool arguments or return values — it only records the method name, the tool
name, the protocol version, the request id, and (on failure) the fixed
string `"tool_error"`:

- Event title / summary, event ID, attendees, email addresses, calendar ID
- Raw Google Calendar API responses
- OAuth access/refresh tokens, client secrets, credentials file content
- Authorization headers, request/response bodies
- Raw exception messages
- The specific date/time values passed to a tool

This is exercised directly by
`tests/test_telemetry.py::test_list_events_span_omits_calendar_content`
(sentinel event summary/ID), and
`tests/test_telemetry.py::test_unexpected_failure_omits_raw_message_and_credentials`
(sentinel raw exception text containing a fake `refresh_token=...` value).

## Scope

Implemented:

- OpenTelemetry SDK configuration (`telemetry.py`), matching Slack Gateway's
  pattern.
- Export to the shared OpenTelemetry Collector over OTLP/gRPC.
- Automated tests for successful, validation-error, and unexpected-failure
  tool calls, sensitive-data absence, and incoming trace-context joining.

Not implemented (tracked as follow-up under Milestone 5 / Milestone 9 in
`docs/roadmap.md`):

- Instrumentation of the Google Calendar API HTTP request itself (a
  `google-calendar.api` child span). Tool spans currently cover only the MCP
  request boundary, not the outbound Google API call latency separately.
- A confirmed, real, Slack-triggered end-to-end trace
  (`concierge.request` → `hermes.request` → `tools/call <tool>`) — the
  session-handshake-level observation above is strong evidence the
  mechanism works, but this specific chain has not been captured with a real
  Slack message.
- Any change to Hermes Agent itself. If the incidental observation above
  turns out not to generalize to `tools/call`, closing that gap would
  require changes to Hermes Agent's MCP client, which is out of scope here
  and would need to be scoped separately.

## Ownership boundary

Everything above is implemented entirely within `local-agent-concierge`
(`mcp/google-calendar/**` and `docker-compose.yml`). No change was made to
the `mcp` SDK, to Hermes Agent, or to Slack Gateway.

## Manual follow-up

With real Google OAuth credentials configured, run a representative
read-only query (e.g. through Slack: "what's on my calendar today?") and
confirm in the Collector / Phoenix / MLflow that the resulting
`tools/call list_events` (or similar) span on `google-calendar-mcp`:

1. Shares a trace ID with the originating `concierge.request` /
   `hermes.request` spans.
2. Carries no calendar content, matching the sensitive-data boundary above.

This was not performed as part of this change since it requires a live
Slack message and real Google Calendar data; the synthetic and
session-handshake-level verification above covers what is possible without
that.
