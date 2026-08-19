from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind


def configure_tracing() -> None:
    resource = Resource.create(
        {
            "service.name": "slack-gateway",
            "service.namespace": "local-agent-concierge",
        }
    )

    tracer_provider = TracerProvider(resource=resource)

    span_exporter = OTLPSpanExporter()

    tracer_provider.add_span_processor(
        BatchSpanProcessor(span_exporter)
    )

    trace.set_tracer_provider(tracer_provider)


@contextmanager
def trace_slack_request(
    *,
    threaded: bool,
) -> Iterator[Span]:
    tracer = trace.get_tracer("slack_gateway")

    with tracer.start_as_current_span(
        "concierge.request",
        kind=SpanKind.CONSUMER,
        attributes={
            "concierge.request.source": "slack",
            "slack.event.type": "message",
            "slack.message.threaded": threaded,
        },
        record_exception=False,
        set_status_on_exception=True,
    ) as span:
        yield span

@contextmanager
def trace_hermes_request() -> Iterator[Span]:
    tracer = trace.get_tracer("slack_gateway")

    with tracer.start_as_current_span(
        "hermes.request",
        kind=SpanKind.CLIENT,
        attributes={
            "concierge.downstream.service": "hermes-agent",
            "concierge.operation": "create_response",
        },
        record_exception=False,
        set_status_on_exception=True,
    ) as span:
        yield span
