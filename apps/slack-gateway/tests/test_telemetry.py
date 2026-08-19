from collections.abc import Sequence

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from slack_sdk.errors import SlackApiError
from opentelemetry.trace import SpanKind, StatusCode

from slack_gateway import telemetry


class RecordingSpanExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(
        self,
        spans: Sequence[ReadableSpan],
    ) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


@pytest.fixture
def exported_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> list[ReadableSpan]:
    exporter = RecordingSpanExporter()

    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(
        SimpleSpanProcessor(exporter)
    )

    tracer = tracer_provider.get_tracer(
        "slack_gateway.test"
    )

    monkeypatch.setattr(
        telemetry.trace,
        "get_tracer",
        lambda _: tracer,
    )

    return exporter.spans


def test_hermes_request_success_has_no_error(
    exported_spans: list[ReadableSpan],
) -> None:
    with telemetry.trace_hermes_request():
        pass

    span = exported_spans[-1]

    assert span.name == "hermes.request"
    assert span.status.status_code == StatusCode.UNSET
    assert "error.type" not in span.attributes
    assert len(span.events) == 0


def test_hermes_request_error_is_sanitized(
    exported_spans: list[ReadableSpan],
) -> None:
    raw_error = "synthetic sensitive failure detail"

    with pytest.raises(RuntimeError):
        with telemetry.trace_hermes_request():
            raise RuntimeError(raw_error)

    span = exported_spans[-1]

    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is None

    assert (
        span.attributes["error.type"]
        == "hermes.request_error"
    )

    assert len(span.events) == 0

    assert raw_error not in str(span.attributes)

def test_slack_request_has_safe_attributes(
    exported_spans: list[ReadableSpan],
) -> None:
    with telemetry.trace_slack_request(threaded=True):
        pass

    span = exported_spans[-1]

    assert span.name == "concierge.request"
    assert span.kind == SpanKind.CONSUMER
    assert span.status.status_code == StatusCode.UNSET

    assert span.attributes["concierge.request.source"] == "slack"
    assert span.attributes["slack.event.type"] == "message"
    assert span.attributes["slack.message.threaded"] is True

    assert "error.type" not in span.attributes
    assert len(span.events) == 0

def test_request_child_spans_share_trace(
    exported_spans: list[ReadableSpan],
) -> None:
    with telemetry.trace_slack_request(
        threaded=False,
    ):
        with telemetry.trace_hermes_request():
            pass

        with telemetry.trace_slack_response():
            pass

    spans_by_name = {
        span.name: span
        for span in exported_spans
    }

    request_span = spans_by_name["concierge.request"]
    hermes_span = spans_by_name["hermes.request"]
    slack_span = spans_by_name["slack.response"]

    assert (
        hermes_span.context.trace_id
        == request_span.context.trace_id
    )
    assert (
        slack_span.context.trace_id
        == request_span.context.trace_id
    )

    assert hermes_span.parent is not None
    assert slack_span.parent is not None

    assert (
        hermes_span.parent.span_id
        == request_span.context.span_id
    )
    assert (
        slack_span.parent.span_id
        == request_span.context.span_id
    )

    assert hermes_span.kind == SpanKind.CLIENT
    assert slack_span.kind == SpanKind.CLIENT

def test_slack_response_error_is_sanitized(
    exported_spans: list[ReadableSpan],
) -> None:
    raw_error = "synthetic sensitive slack failure"

    with pytest.raises(SlackApiError):
        with telemetry.trace_slack_response():
            raise SlackApiError(
                message=raw_error,
                response={"ok": False},
            )

    span = exported_spans[-1]

    assert span.name == "slack.response"
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is None

    assert (
        span.attributes["error.type"]
        == "slack.response_error"
    )

    assert len(span.events) == 0

    assert raw_error not in str(span.attributes)
    assert raw_error not in str(span.status)
    assert raw_error not in str(span.events)
