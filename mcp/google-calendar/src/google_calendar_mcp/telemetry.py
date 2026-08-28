from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode


def _build_resource() -> Resource:
    return Resource.create(
        {
            "service.name": "google-calendar-mcp",
            "service.namespace": "local-agent-concierge",
        }
    )


def configure_tracing() -> None:
    tracer_provider = TracerProvider(resource=_build_resource())

    span_exporter = OTLPSpanExporter()

    tracer_provider.add_span_processor(
        BatchSpanProcessor(span_exporter)
    )

    trace.set_tracer_provider(tracer_provider)


@contextmanager
def trace_calendar_api(
    *,
    operation: str,
) -> Iterator[Span]:
    tracer = trace.get_tracer("google_calendar_mcp")

    with tracer.start_as_current_span(
        "google-calendar.api",
        kind=SpanKind.CLIENT,
        attributes={
            "google_calendar.operation": operation,
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except Exception:
            mark_span_error(
                span,
                error_type="google_calendar.api_error",
            )
            raise


def mark_span_error(
    span: Span,
    *,
    error_type: str,
) -> None:
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute("error.type", error_type)
