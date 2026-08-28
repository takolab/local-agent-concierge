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
instrumentation produce the `tools/call` span. No custom span-creation code
was needed for that span in `server.py` or `calendar_client.py`, and no
application error-handling behavior was changed. (`calendar_client.py` does
now create one child span of its own, for the one boundary the SDK's
middleware does not cover — the outbound Google API request itself; see
"Google Calendar API child span" below.)

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

### Google Calendar API child span

The actual Google Calendar API request — the `googleapiclient` `.execute()`
call inside `calendar_client.py` — now has its own `CLIENT` child span,
created with application code (`telemetry.trace_calendar_api`), unlike the
SDK-provided `tools/call` span above:

```text
tools/call list_events
└── google-calendar.api
```

`trace_calendar_api()` wraps only the `.execute()` call itself — not
`create_calendar_service()` (credential loading and client construction) and
not request validation or response formatting — so the span name stays
constant and low-cardinality regardless of which tool triggered it:

```text
google-calendar.api
```

The specific Google API operation is recorded as a single bounded attribute
with exactly two possible values:

```text
google_calendar.operation = "events.list" | "freebusy.query"
```

`list_events` and `list_busy_periods` are the only two call sites
(`.events().list()` and `.freebusy().query()`); `list_upcoming_events` and
`list_free_periods` call into those same two functions rather than issuing
their own API request, so they produce the same span with no additional
instrumentation.

Because the span is created with `tracer.start_as_current_span(...)` while
the SDK's `tools/call` span is still the active OpenTelemetry span (ordinary
ambient context propagation, no manual trace/span ID handling), it nests
under whichever `tools/call` span triggered it whenever one is active, and
becomes a root span otherwise.

On failure, the span status becomes `ERROR` with the same sanitized,
low-cardinality style as the tool-level span:

```text
error.type = "google_calendar.api_error"
```

`record_exception` and `set_status_on_exception` are both disabled on the
span (the same choice `apps/slack-gateway` makes for its own child spans),
so the SDK's default exception-recording behavior never attaches a raw
exception message or exception type as a span event; the original exception
is re-raised unchanged (a bare `raise`, no wrapping) so caller-visible error
handling is unaffected by this instrumentation.

Verified by `tests/test_telemetry.py`:
`test_calendar_api_span_for_list_events`,
`test_calendar_api_span_for_list_busy_periods`,
`test_calendar_api_span_marks_error_and_preserves_exception`,
`test_calendar_api_span_omits_calendar_content`,
`test_calendar_api_span_is_child_of_tool_call_span`, and
`test_calendar_api_span_for_list_busy_periods_tool_is_child` — the last two
drive a real `tools/call` request through the MCP `Client` (rather than
constructing a span directly) to confirm the parent/child relationship end
to end, with only `create_calendar_service()` mocked to avoid a real Google
API call.

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

Whether Hermes Agent's own MCP client actually sends that `_meta` was out of
scope to change (Hermes Agent is the unmodified `nousresearch/hermes-agent`
image; see `docs/observability/hermes-trace-context.md`), but it was
possible to confirm with a real, live Hermes Agent deployment and a real
Slack message ("今日の予定を教えて", asking about today's schedule). The
Collector received, among others:

```text
service.name: hermes-agent      Kind: Client   Name: MCP send tools/call list_events
service.name: google-calendar-mcp  Kind: Server  Name: tools/call list_events
  (same Trace ID, Google Calendar MCP span parented under Hermes Agent's)
service.name: hermes-agent      Kind: Client   Name: MCP send tools/call list_upcoming_events
service.name: google-calendar-mcp  Kind: Server  Name: tools/call list_upcoming_events
  (same Trace ID, same parenting)
```

Both tool calls happened to fail (the account's stored Google OAuth refresh
token was invalid — an unrelated, pre-existing credential issue, not caused
by this change), which incidentally also confirmed the sensitive-data
boundary against a real Google API error rather than just a synthetic one:
both spans carried `error.type: tool_error` and `Status code: Error` with an
**empty** status message — the real `google.auth.exceptions.RefreshError`
text (visible in the container's stdout logs, which are unrelated to
tracing) never reached the span.

So: **the MCP-level propagation mechanism between Hermes Agent and Google
Calendar MCP is confirmed working** — Hermes Agent's MCP client does send a
`traceparent`, and Google Calendar MCP correctly joins as a child, for real
`tools/call` requests, not just session-handshake traffic.

However, this same verification also showed that chain does **not** extend
back to the Slack-originated trace. The same request produced a *separate*
Slack Gateway trace (`concierge.request` → `hermes.request` →
`/v1/responses` on Hermes Agent → `slack.response`) with its own, different
trace ID — Hermes Agent's `/v1/responses` `SERVER` span (which does inherit
the Slack Gateway's trace, per `docs/observability/hermes-trace-context.md`)
and its outgoing `MCP send tools/call ...` `CLIENT` span are both rooted
independently (`Parent ID` empty on both), rather than the latter nesting
under the former. This means Hermes Agent's own internal request-handling
code does not propagate its current span into its MCP client calls — a gap
inside Hermes Agent itself (a closed, unmodified vendor image), not
something addressable from the Google Calendar MCP side. Closing it would
require a change to Hermes Agent's own source or a further layer of
auto-instrumentation on top of it, which is out of scope here and is
recorded as follow-up.

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
(sentinel event summary/ID),
`tests/test_telemetry.py::test_unexpected_failure_omits_raw_message_and_credentials`
(sentinel raw exception text containing a fake `refresh_token=...` value),
and confirmed again with a real Google API failure during live verification
(a real `RefreshError` from an invalid stored OAuth token — see "Incoming
trace context" above): the span carried only `error.type: tool_error`, no
message.

The same guarantee extends to the `google-calendar.api` span (see "Google
Calendar API child span" above): `trace_calendar_api()` only ever receives a
fixed `operation` string literal written at the two `calendar_client.py`
call sites (`"events.list"` / `"freebusy.query"`), never a value derived
from calendar content, credentials, or the API response, and its error path
attaches only the fixed string `google_calendar.api_error` — never
`str(exception)` — with both `record_exception` and `set_status_on_exception`
disabled so the OpenTelemetry SDK's default exception-recording behavior
cannot attach one either. Exercised by
`tests/test_telemetry.py::test_calendar_api_span_omits_calendar_content` and
`tests/test_telemetry.py::test_calendar_api_span_marks_error_and_preserves_exception`
(sentinel event summary/ID and a sentinel `refresh_token=...` value inside a
synthetic exception message, respectively).

## Scope

Implemented:

- OpenTelemetry SDK configuration (`telemetry.py`), matching Slack Gateway's
  pattern.
- Export to the shared OpenTelemetry Collector over OTLP/gRPC.
- Automated tests for successful, validation-error, and unexpected-failure
  tool calls, sensitive-data absence, and incoming trace-context joining.
- A `google-calendar.api` child span around the actual Google Calendar API
  `.execute()` call (`events.list` / `freebusy.query`), nested under the
  active `tools/call` span via ordinary OpenTelemetry context propagation.
  See "Google Calendar API child span" above.

Confirmed live (real Hermes Agent, real Slack message, real — if currently
failing — Google credentials): Hermes Agent's MCP client does propagate a
`traceparent` into its `tools/call` requests, and Google Calendar MCP
correctly joins as a child, for both a successful (`get_current_datetime`)
and a failing (`list_events`, `list_upcoming_events`) tool call. See
"Incoming trace context" above.

Not implemented (tracked as follow-up under Milestone 5 / Milestone 9 in
`docs/roadmap.md`):

- A trace connecting Slack's `concierge.request` all the way into a Google
  Calendar MCP `tools/call <tool>` span. Live verification confirmed the
  Hermes Agent ↔ Google Calendar MCP leg works, but also confirmed that leg
  is *not* currently joined to the Slack-originated trace — Hermes Agent's
  own internal code does not carry its `/v1/responses` server span's context
  into its outgoing MCP client calls. This is a gap inside Hermes Agent
  itself (the unmodified vendor image), not something the Google Calendar
  MCP side can address; closing it would require a change to Hermes Agent's
  own source or a further auto-instrumentation layer on top of it (compare
  `apps/hermes-agent/Dockerfile`'s existing aiohttp-server instrumentation),
  which is out of scope here.
- Any change to Hermes Agent itself.

## Ownership boundary

Everything above is implemented entirely within `local-agent-concierge`
(`mcp/google-calendar/**` and `docker-compose.yml`). No change was made to
the `mcp` SDK, to Hermes Agent, or to Slack Gateway.

## Manual follow-up

- ~~Re-run the OAuth bootstrap to restore a valid Google Calendar refresh
  token, then confirm a successful `tools/call list_events` span end to
  end.~~ Done: after re-running `python -m google_calendar_mcp.bootstrap`,
  a real Slack question ("今日の予定を教えて") produced a successful
  `tools/call list_events`/`list_upcoming_events` span with real calendar
  data, with no calendar content in the exported span.
- Continuing the distributed trace from Slack through to Google Calendar MCP
  requires a change on the Hermes Agent side (its outbound MCP calls don't
  propagate trace context at all, independent of Google Calendar MCP) — this
  is tracked as a known upstream gap with a planned path forward in
  `docs/observability/hermes-trace-context.md`, not something to fix here.

The first item above was not performed as part of the original PR since it
required a live Slack message and real Google Calendar data unavailable at
the time; it was completed as manual verification afterward. The second
item remains open and depends on upstream Hermes Agent work outside this
repository's control.
