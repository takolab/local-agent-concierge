from collections.abc import Sequence
from unittest.mock import MagicMock

import mcp.shared._otel as mcp_otel
import pytest
from mcp import Client
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import StatusCode

from google_calendar_mcp import server, telemetry


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
def anyio_backend():
    return "asyncio"


@pytest.fixture
def exported_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> list[ReadableSpan]:
    exporter = RecordingSpanExporter()

    tracer_provider = TracerProvider(
        resource=telemetry._build_resource()
    )
    tracer_provider.add_span_processor(
        SimpleSpanProcessor(exporter)
    )

    tracer = tracer_provider.get_tracer(
        "google_calendar_mcp.test"
    )

    monkeypatch.setattr(mcp_otel, "_tracer", tracer)

    return exporter.spans


def _tool_call_span(
    spans: list[ReadableSpan],
    tool_name: str,
) -> ReadableSpan:
    matches = [
        span
        for span in spans
        if span.attributes is not None
        and span.attributes.get("gen_ai.tool.name") == tool_name
    ]
    assert len(matches) == 1
    return matches[0]


def test_configure_tracing_builds_expected_resource() -> None:
    resource = telemetry._build_resource()

    assert (
        resource.attributes["service.name"]
        == "google-calendar-mcp"
    )
    assert (
        resource.attributes["service.namespace"]
        == "local-agent-concierge"
    )


@pytest.mark.anyio
async def test_successful_tool_call_produces_span(
    exported_spans: list[ReadableSpan],
) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "get_server_status",
            {},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "Google Calendar",
        "status": "ready",
    }

    span = _tool_call_span(exported_spans, "get_server_status")

    assert span.resource.attributes["service.name"] == (
        "google-calendar-mcp"
    )
    assert span.resource.attributes["service.namespace"] == (
        "local-agent-concierge"
    )

    assert span.attributes["gen_ai.operation.name"] == (
        "execute_tool"
    )
    assert span.attributes["mcp.method.name"] == "tools/call"

    assert span.status.status_code == StatusCode.UNSET
    assert "error.type" not in span.attributes
    assert span.end_time is not None
    assert span.end_time >= span.start_time


@pytest.mark.anyio
async def test_list_events_span_omits_calendar_content(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_summary = "Therapy session with Dr. Sentinel-Summary"
    sensitive_event_id = "sentinel-event-id-9f3c"

    fake_events = [
        {
            "id": sensitive_event_id,
            "summary": sensitive_summary,
            "start": "2026-08-10T10:00:00+01:00",
            "end": "2026-08-10T11:00:00+01:00",
            "is_all_day": False,
            "status": "confirmed",
        }
    ]

    monkeypatch.setattr(
        server,
        "fetch_events",
        MagicMock(return_value=fake_events),
    )

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "list_events",
            {
                "time_min": "2026-08-10T09:00:00+01:00",
                "time_max": "2026-08-10T18:00:00+01:00",
            },
        )

    assert result.is_error is False
    assert result.structured_content == {"result": fake_events}

    span = _tool_call_span(exported_spans, "list_events")

    assert span.status.status_code == StatusCode.UNSET

    serialized_attributes = str(span.attributes)
    serialized_events = str(span.events)

    assert sensitive_summary not in serialized_attributes
    assert sensitive_summary not in serialized_events
    assert sensitive_event_id not in serialized_attributes
    assert sensitive_event_id not in serialized_events


@pytest.mark.anyio
async def test_validation_error_produces_sanitized_error_span(
    exported_spans: list[ReadableSpan],
) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "list_events",
            {
                "time_min": "2026-08-10T09:00:00",
            },
        )

    assert result.is_error is True

    span = _tool_call_span(exported_spans, "list_events")

    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "tool_error"
    assert len(span.events) == 0

    assert (
        "time_min must include time zone information"
        not in str(span.attributes)
    )
    assert span.status.description is None


@pytest.mark.anyio
async def test_unexpected_failure_omits_raw_message_and_credentials(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_error = (
        "synthetic Google API failure for "
        "refresh_token=sentinel-refresh-token-value"
    )

    monkeypatch.setattr(
        server,
        "fetch_upcoming_events",
        MagicMock(side_effect=RuntimeError(raw_error)),
    )

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "list_upcoming_events",
            {},
        )

    assert result.is_error is True

    span = _tool_call_span(exported_spans, "list_upcoming_events")

    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "tool_error"
    assert len(span.events) == 0

    serialized_span = "".join(
        [
            str(span.attributes),
            str(span.events),
            str(span.status),
        ]
    )

    assert raw_error not in serialized_span
    assert "sentinel-refresh-token-value" not in serialized_span
    assert "RuntimeError" not in serialized_span


@pytest.mark.anyio
async def test_incoming_trace_context_is_joined_when_present(
    exported_spans: list[ReadableSpan],
) -> None:
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    parent_span_id = "00f067aa0ba902b7"

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "get_server_status",
            {},
            meta={
                "traceparent": (
                    f"00-{trace_id}-{parent_span_id}-01"
                ),
            },
        )

    assert result.is_error is False

    span = _tool_call_span(exported_spans, "get_server_status")

    assert f"{span.context.trace_id:032x}" == trace_id
    assert span.parent is not None
    assert f"{span.parent.span_id:016x}" == parent_span_id


@pytest.mark.anyio
async def test_missing_trace_context_falls_back_to_root_span(
    exported_spans: list[ReadableSpan],
) -> None:
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "get_server_status",
            {},
        )

    assert result.is_error is False

    span = _tool_call_span(exported_spans, "get_server_status")

    assert span.parent is None
