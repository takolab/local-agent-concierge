"""Shared span-recording fixtures.

`slack_gateway.telemetry` creates its spans through
`trace.get_tracer("slack_gateway")`. Patching that one lookup to return a
tracer backed by an in-memory exporter lets a test read the spans the
production code path actually produced, without installing a global
TracerProvider or exporting anything.
"""

from collections.abc import Sequence

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

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
