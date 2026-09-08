"""OpenTelemetry tracing for the Orchestrator's two HTTP boundaries.

Two spans, and nothing else:

- `POST /dispatch` (SERVER) -- started from the trace context extracted
  from the *incoming* HTTP request's headers, so a caller that already
  has an active trace becomes this span's parent.
- `hermes.request` (CLIENT) -- the outgoing call to Hermes Agent, whose
  context is injected into that request's headers. Deliberately named the
  same as `apps/slack-gateway/src/slack_gateway/telemetry.py`'s own
  Hermes client span, because it is the same operation against the same
  API.

Together those make the caller -> Orchestrator -> Hermes Agent HTTP
boundaries one distributed trace. Hermes Agent's own SERVER span is
produced by the auto-instrumentation layered onto its image (see
docs/observability/hermes-trace-context.md); nothing here needs to know
about it.

## Header handling

`traceparent`/`tracestate` are never parsed here. `extract()` and
`inject()` hand the carrier to OpenTelemetry's configured propagator,
whose `TraceContextTextMapPropagator` is the only thing that interprets
the header -- including deciding that a missing or malformed one simply
yields no parent, which is standard behavior and not an error. The one
piece of carrier handling done here is lowercasing header names, because
HTTP header names are case-insensitive while a plain `dict` lookup is
not; that is carrier normalization, not trace-context parsing.

## What is deliberately not put on a span

Instruction text, Hermes' response text, `Authorization` values, API
keys, raw exception messages and tracebacks, and any per-user or
per-conversation identifier. Spans are created with
`record_exception=False` and `set_status_on_exception=False` -- matching
`apps/slack-gateway` and `mcp/google-calendar` -- specifically so the SDK
never attaches `exception.message`/`exception.stacktrace` attributes
built from an exception this service does not control. Errors are instead
recorded as a status plus one value from a small, fixed `error.type`
vocabulary defined in this module.

`agent_name` is deliberately *not* a span attribute: it is a
caller-supplied free string on an unauthenticated endpoint, so it is
neither bounded in cardinality nor guaranteed to be free of content the
caller should not have put there. It stays in the correlation *logs*
(`http_server.py`, Slice 4), which are local-only.

See docs/orchestrator/domain-model.md ("Trace Context Propagation") for
the full design notes.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

logger = logging.getLogger("orchestrator.telemetry")

TRACER_NAME = "orchestrator"

DISPATCH_SPAN_NAME = "POST /dispatch"
HERMES_SPAN_NAME = "hermes.request"

# The complete `error.type` vocabulary this service emits. Kept as named
# constants (rather than inline literals) so the set stays small and
# reviewable, and so a test can assert that nothing outside it is ever
# attached to a span.
ERROR_TYPE_DISPATCH_SERVER_ERROR = "dispatch.server_error"
ERROR_TYPE_HERMES_HTTP_STATUS = "hermes.http_status_error"
ERROR_TYPE_HERMES_CONNECTION = "hermes.connection_error"
ERROR_TYPE_HERMES_INVALID_RESPONSE = "hermes.invalid_response"

ERROR_TYPES = frozenset(
    {
        ERROR_TYPE_DISPATCH_SERVER_ERROR,
        ERROR_TYPE_HERMES_HTTP_STATUS,
        ERROR_TYPE_HERMES_CONNECTION,
        ERROR_TYPE_HERMES_INVALID_RESPONSE,
    }
)


def configure_tracing() -> TracerProvider | None:
    """Install a TracerProvider exporting to the OpenTelemetry Collector.

    Mirrors `apps/slack-gateway` and `mcp/google-calendar`: an OTLP/gRPC
    exporter configured entirely through the standard
    `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable, behind a
    `BatchSpanProcessor` (which exports on a background thread, so an
    unreachable Collector can never block or fail a dispatch).

    Returns the provider so the caller can shut it down -- and therefore
    flush the batch processor -- on graceful termination. Returns `None`,
    installing nothing, when `OTEL_SDK_DISABLED` is set to `true`; the
    OpenTelemetry API then hands out non-recording spans, which every code
    path in this package already tolerates.
    """
    if _sdk_disabled():
        logger.info("OTEL_SDK_DISABLED is set; Orchestrator tracing is off")
        return None

    resource = Resource.create(
        {
            "service.name": "orchestrator",
            "service.namespace": "local-agent-concierge",
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    trace.set_tracer_provider(tracer_provider)

    return tracer_provider


def _sdk_disabled() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true"


@contextmanager
def trace_dispatch_request(*, headers: Mapping[str, str]) -> Iterator[Span]:
    """Start the `POST /dispatch` SERVER span, parented by `headers`.

    Exiting the context ends the span and detaches it as the current
    span, on the success path and on an exception alike -- so a request
    can never leave its context attached for the next request handled on
    the same connection/thread.
    """
    tracer = trace.get_tracer(TRACER_NAME)

    with tracer.start_as_current_span(
        DISPATCH_SPAN_NAME,
        context=extract(_carrier(headers)),
        kind=SpanKind.SERVER,
        attributes={
            "concierge.operation": "dispatch",
            "http.method": "POST",
            "http.route": "/dispatch",
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        yield span


@contextmanager
def trace_hermes_request() -> Iterator[Span]:
    """Start the outgoing Hermes Agent CLIENT span."""
    tracer = trace.get_tracer(TRACER_NAME)

    with tracer.start_as_current_span(
        HERMES_SPAN_NAME,
        kind=SpanKind.CLIENT,
        attributes={
            "concierge.downstream.service": "hermes-agent",
            "concierge.operation": "create_response",
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        yield span


def trace_context_headers() -> dict[str, str]:
    """Return the current trace context as outgoing HTTP headers.

    Returned as a *fresh* dict rather than injected into a caller's
    existing header mapping, so the propagator can never overwrite a
    header the caller already set -- `Authorization` above all. The caller
    merges it explicitly (see `hermes_agent.py`).

    Empty when there is no recording span (tracing disabled, or no
    provider installed), which is the correct "send no `traceparent`"
    behavior rather than a special case to handle.
    """
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


def record_dispatch_status(span: Span, status_code: int | None) -> None:
    """Record the HTTP status `POST /dispatch` actually responded with.

    4xx is left unmarked: per OpenTelemetry's HTTP semantic conventions a
    client-caused status is not an error *of the server span*. 5xx, and
    the "no response was sent at all" case, are marked as errors with a
    bounded `error.type`, never with the underlying exception.
    """
    if status_code is None:
        _mark_error(span, ERROR_TYPE_DISPATCH_SERVER_ERROR)
        return

    span.set_attribute("http.status_code", status_code)

    if status_code >= 500:
        _mark_error(span, ERROR_TYPE_DISPATCH_SERVER_ERROR)


def mark_current_span_error(
    *,
    error_type: str,
    http_status_code: int | None = None,
) -> None:
    """Mark the currently active span as failed, with a bounded reason.

    A no-op when nothing is being recorded (no provider installed, or the
    span is not sampled): `get_current_span()` then returns the API's
    non-recording span, whose setters do nothing.
    """
    span = trace.get_current_span()

    if http_status_code is not None:
        span.set_attribute("http.status_code", http_status_code)

    _mark_error(span, error_type)


def _mark_error(span: Span, error_type: str) -> None:
    if error_type not in ERROR_TYPES:
        raise ValueError(f"Unknown error_type: {error_type!r}")

    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute("error.type", error_type)


def _carrier(headers: Mapping[str, str]) -> dict[str, str]:
    """Represent the HTTP headers faithfully as a propagator carrier.

    Two things happen here, both about *representing* the carrier, not
    about interpreting trace context:

    1. Header names are lowercased, because HTTP header names are
       case-insensitive while the propagator's `dict` lookup is not. A
       caller sending `Traceparent` would otherwise silently start a new
       trace.
    2. A header field that appears more than once is combined into one
       comma-separated value, in wire order. W3C Trace Context allows
       `tracestate` to be split across several header fields and requires
       a receiver to treat them as the combined list; a plain
       `{k: v for k, v in ...}` would keep only the last field and
       silently drop the rest.

    Combining is done from `items()` rather than `get_all()` so this
    works for both `http.server`'s `HTTPMessage` and an ordinary
    `Mapping` (as used in unit tests), and it is well-defined for both:
    `email.message.Message.items()` -- which `HTTPMessage` inherits --
    returns every field in the order it was parsed, duplicates included,
    while a plain `dict` cannot contain duplicates at all.

    A side effect worth knowing: two `traceparent` fields now combine
    into one value the propagator rejects, so such a request starts a
    fresh root trace instead of silently inheriting whichever field
    happened to arrive last. That is the safer of the two outcomes and is
    asserted in `test_trace_propagation.py`.
    """
    carrier: dict[str, str] = {}

    for name, value in headers.items():
        key = name.lower()
        existing = carrier.get(key)
        carrier[key] = value if existing is None else f"{existing},{value}"

    return carrier
