from collections.abc import Sequence
from datetime import datetime
from unittest.mock import MagicMock

import httplib2
import mcp.shared._otel as mcp_otel
import pytest
from googleapiclient.errors import HttpError
from mcp import Client
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, StatusCode

from google_calendar_mcp import calendar_client, server, telemetry


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
    monkeypatch.setattr(
        trace, "get_tracer_provider", lambda: tracer_provider
    )

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


def _calendar_api_spans(
    spans: list[ReadableSpan],
) -> list[ReadableSpan]:
    return [
        span
        for span in spans
        if span.name == "google-calendar.api"
    ]


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
async def test_get_event_tool_call_span_omits_event_id_argument(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_event_id = "sentinel-event-id-9f3c"

    fake_event = {
        "id": sensitive_event_id,
        "summary": "Team meeting",
        "start": "2026-08-10T10:00:00+01:00",
        "end": "2026-08-10T11:00:00+01:00",
        "is_all_day": False,
        "status": "confirmed",
    }

    monkeypatch.setattr(
        server,
        "fetch_event",
        MagicMock(return_value=fake_event),
    )

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "get_event",
            {
                "event_id": sensitive_event_id,
            },
        )

    assert result.is_error is False
    assert result.structured_content == fake_event

    span = _tool_call_span(exported_spans, "get_event")

    assert span.status.status_code == StatusCode.UNSET

    serialized_attributes = str(span.attributes)
    serialized_events = str(span.events)

    assert sensitive_event_id not in serialized_attributes
    assert sensitive_event_id not in serialized_events


@pytest.mark.anyio
async def test_get_event_not_found_tool_call_span_omits_event_id(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_event_id = "sentinel-event-id-9f3c"

    monkeypatch.setattr(
        server,
        "fetch_event",
        MagicMock(
            side_effect=ValueError(
                f"Event not found: {sensitive_event_id}"
            )
        ),
    )

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "get_event",
            {
                "event_id": sensitive_event_id,
            },
        )

    assert result.is_error is True

    span = _tool_call_span(exported_spans, "get_event")

    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "tool_error"

    serialized_attributes = str(span.attributes)
    serialized_events = str(span.events)

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


def test_calendar_api_span_for_list_events(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_service = MagicMock()
    fake_service.events.return_value.list.return_value.execute.return_value = {
        "items": []
    }

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    calendar_client.list_events(
        time_min=datetime.fromisoformat(
            "2026-08-10T09:00:00+01:00"
        ),
    )

    spans = _calendar_api_spans(exported_spans)
    assert len(spans) == 1

    span = spans[0]
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["google_calendar.operation"] == (
        "events.list"
    )
    assert span.status.status_code == StatusCode.UNSET
    assert "error.type" not in span.attributes


def test_calendar_api_span_for_get_event(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_service = MagicMock()
    fake_service.events.return_value.get.return_value.execute.return_value = {
        "id": "event-1",
        "summary": "Team meeting",
        "start": {
            "dateTime": "2026-08-10T10:00:00+01:00",
        },
        "end": {
            "dateTime": "2026-08-10T11:00:00+01:00",
        },
        "status": "confirmed",
    }

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    calendar_client.get_event(event_id="event-1")

    spans = _calendar_api_spans(exported_spans)
    assert len(spans) == 1

    span = spans[0]
    assert span.kind == SpanKind.CLIENT
    assert span.attributes["google_calendar.operation"] == (
        "events.get"
    )
    assert span.status.status_code == StatusCode.UNSET
    assert "error.type" not in span.attributes


def test_calendar_api_span_for_list_busy_periods(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_service = MagicMock()
    fake_service.freebusy.return_value.query.return_value.execute.return_value = {
        "calendars": {
            "primary": {
                "busy": [],
            }
        }
    }

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    calendar_client.list_busy_periods(
        time_min=datetime.fromisoformat(
            "2026-08-10T09:00:00+01:00"
        ),
        time_max=datetime.fromisoformat(
            "2026-08-10T18:00:00+01:00"
        ),
    )

    spans = _calendar_api_spans(exported_spans)
    assert len(spans) == 1

    span = spans[0]
    assert span.attributes["google_calendar.operation"] == (
        "freebusy.query"
    )
    assert span.status.status_code == StatusCode.UNSET


def test_calendar_api_span_marks_error_and_preserves_exception(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_error_message = (
        "synthetic Google API failure for "
        "refresh_token=sentinel-refresh-token-value"
    )

    class FakeApiError(Exception):
        pass

    fake_service = MagicMock()
    fake_service.events.return_value.list.return_value.execute.side_effect = (
        FakeApiError(raw_error_message)
    )

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    with pytest.raises(FakeApiError) as exc_info:
        calendar_client.list_events(
            time_min=datetime.fromisoformat(
                "2026-08-10T09:00:00+01:00"
            ),
        )

    assert str(exc_info.value) == raw_error_message

    spans = _calendar_api_spans(exported_spans)
    assert len(spans) == 1

    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == (
        "google_calendar.api_error"
    )
    assert len(span.events) == 0

    serialized_span = "".join(
        [
            str(span.attributes),
            str(span.events),
            str(span.status),
        ]
    )

    assert raw_error_message not in serialized_span
    assert "sentinel-refresh-token-value" not in serialized_span
    assert "FakeApiError" not in serialized_span


def test_calendar_api_span_marks_error_for_get_event_not_found(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_event_id = "sentinel-event-id-9f3c"

    not_found_response = httplib2.Response({"status": 404})

    fake_service = MagicMock()
    fake_service.events.return_value.get.return_value.execute.side_effect = (
        HttpError(
            not_found_response,
            b'{"error": {"code": 404, "message": "Not Found"}}',
            uri=(
                "https://www.googleapis.com/calendar/v3/calendars/"
                f"primary/events/{sensitive_event_id}"
            ),
        )
    )

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    with pytest.raises(
        ValueError,
        match=f"Event not found: {sensitive_event_id}",
    ):
        calendar_client.get_event(event_id=sensitive_event_id)

    spans = _calendar_api_spans(exported_spans)
    assert len(spans) == 1

    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == (
        "google_calendar.api_error"
    )
    assert len(span.events) == 0

    serialized_span = "".join(
        [
            str(span.attributes),
            str(span.events),
            str(span.status),
        ]
    )

    assert sensitive_event_id not in serialized_span
    assert "Not Found" not in serialized_span
    assert "HttpError" not in serialized_span


def test_calendar_api_span_omits_calendar_content(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_summary = "Therapy session with Dr. Sentinel-Summary"
    sensitive_event_id = "sentinel-event-id-9f3c"

    fake_service = MagicMock()
    fake_service.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": sensitive_event_id,
                "summary": sensitive_summary,
                "start": {
                    "dateTime": "2026-08-10T10:00:00+01:00",
                },
                "end": {
                    "dateTime": "2026-08-10T11:00:00+01:00",
                },
                "status": "confirmed",
            }
        ]
    }

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    calendar_client.list_events(
        time_min=datetime.fromisoformat(
            "2026-08-10T09:00:00+01:00"
        ),
    )

    spans = _calendar_api_spans(exported_spans)
    assert len(spans) == 1

    serialized_span = str(spans[0].attributes) + str(
        spans[0].events
    )

    assert sensitive_summary not in serialized_span
    assert sensitive_event_id not in serialized_span


def test_calendar_api_span_omits_event_id_and_content_for_get_event(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_summary = "Therapy session with Dr. Sentinel-Summary"
    sensitive_event_id = "sentinel-event-id-9f3c"

    fake_service = MagicMock()
    fake_service.events.return_value.get.return_value.execute.return_value = {
        "id": sensitive_event_id,
        "summary": sensitive_summary,
        "start": {
            "dateTime": "2026-08-10T10:00:00+01:00",
        },
        "end": {
            "dateTime": "2026-08-10T11:00:00+01:00",
        },
        "status": "confirmed",
    }

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    calendar_client.get_event(event_id=sensitive_event_id)

    spans = _calendar_api_spans(exported_spans)
    assert len(spans) == 1

    serialized_span = str(spans[0].attributes) + str(
        spans[0].events
    )

    assert sensitive_summary not in serialized_span
    assert sensitive_event_id not in serialized_span


@pytest.mark.anyio
async def test_calendar_api_span_is_child_of_tool_call_span(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_service = MagicMock()
    fake_service.events.return_value.list.return_value.execute.return_value = {
        "items": []
    }

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "list_events",
            {
                "time_min": "2026-08-10T09:00:00+01:00",
            },
        )

    assert result.is_error is False

    tool_span = _tool_call_span(exported_spans, "list_events")
    api_spans = _calendar_api_spans(exported_spans)
    assert len(api_spans) == 1

    api_span = api_spans[0]
    assert api_span.context.trace_id == tool_span.context.trace_id
    assert api_span.parent is not None
    assert api_span.parent.span_id == tool_span.context.span_id


@pytest.mark.anyio
async def test_calendar_api_span_for_list_busy_periods_tool_is_child(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_service = MagicMock()
    fake_service.freebusy.return_value.query.return_value.execute.return_value = {
        "calendars": {
            "primary": {
                "busy": [],
            }
        }
    }

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "list_busy_periods",
            {
                "time_min": "2026-08-10T09:00:00+01:00",
                "time_max": "2026-08-10T18:00:00+01:00",
            },
        )

    assert result.is_error is False

    tool_span = _tool_call_span(exported_spans, "list_busy_periods")
    api_spans = _calendar_api_spans(exported_spans)
    assert len(api_spans) == 1

    api_span = api_spans[0]
    assert api_span.attributes["google_calendar.operation"] == (
        "freebusy.query"
    )
    assert api_span.parent is not None
    assert api_span.parent.span_id == tool_span.context.span_id


@pytest.mark.anyio
async def test_calendar_api_span_for_get_event_tool_is_child(
    exported_spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_service = MagicMock()
    fake_service.events.return_value.get.return_value.execute.return_value = {
        "id": "event-1",
        "summary": "Team meeting",
        "start": {
            "dateTime": "2026-08-10T10:00:00+01:00",
        },
        "end": {
            "dateTime": "2026-08-10T11:00:00+01:00",
        },
        "status": "confirmed",
    }

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "get_event",
            {
                "event_id": "event-1",
            },
        )

    assert result.is_error is False

    tool_span = _tool_call_span(exported_spans, "get_event")
    api_spans = _calendar_api_spans(exported_spans)
    assert len(api_spans) == 1

    api_span = api_spans[0]
    assert api_span.attributes["google_calendar.operation"] == (
        "events.get"
    )
    assert api_span.context.trace_id == tool_span.context.trace_id
    assert api_span.parent is not None
    assert api_span.parent.span_id == tool_span.context.span_id
