from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


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
