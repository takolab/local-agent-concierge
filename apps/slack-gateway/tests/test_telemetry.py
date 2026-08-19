from collections.abc import Sequence

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import StatusCode

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
