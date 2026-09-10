from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
)
from slack_sdk.errors import SlackApiError

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
        set_status_on_exception=False,
    ) as span:
        yield span

@contextmanager
def trace_orchestrator_request() -> Iterator[Span]:
    """The CLIENT span around the outgoing `POST /dispatch` call.

    Replaces this module's previous `hermes.request` span: the Slack
    Gateway's downstream service is now the Orchestrator, and it is the
    Orchestrator that emits its own `hermes.request` CLIENT span for the
    Hermes hop (`orchestrator.telemetry`). Naming this span after the
    service actually called keeps those two hops distinguishable in one
    trace instead of collapsing them under a shared name.
    """
    tracer = trace.get_tracer("slack_gateway")

    with tracer.start_as_current_span(
        "orchestrator.dispatch",
        kind=SpanKind.CLIENT,
        attributes={
            "concierge.downstream.service": "orchestrator",
            "concierge.operation": "dispatch",
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except RuntimeError:
            mark_span_error(
                span,
                error_type="orchestrator.request_error",
            )
            raise

@contextmanager
def trace_slack_response() -> Iterator[Span]:
    tracer = trace.get_tracer("slack_gateway")

    with tracer.start_as_current_span(
        "slack.response",
        kind=SpanKind.CLIENT,
        attributes={
            "concierge.downstream.service": "slack",
            "concierge.operation": "post_response",
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except SlackApiError:
            mark_span_error(
                span,
                error_type="slack.response_error",
            )
            raise

def mark_span_error(
    span: Span,
    *,
    error_type: str,
) -> None:
    span.set_status(Status(StatusCode.ERROR))
    span.set_attribute("error.type", error_type)
