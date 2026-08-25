from datetime import datetime
from unittest.mock import MagicMock

import pytest
from mcp import Client

from google_calendar_mcp import server


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_server_exposes_expected_tools():
    async with Client(server.mcp) as client:
        result = await client.list_tools()

    tool_names = {
        tool.name
        for tool in result.tools
    }

    assert tool_names == {
        "get_server_status",
        "get_current_datetime",
        "list_upcoming_events",
        "list_events",
        "list_busy_periods",
        "list_free_periods",
    }


@pytest.mark.anyio
async def test_get_server_status_through_mcp():
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


@pytest.mark.anyio
async def test_list_events_through_mcp(
    monkeypatch,
):
    fake_events = [
        {
            "id": "event-1",
            "summary": "Team meeting",
            "start": "2026-08-10T10:00:00+01:00",
            "end": "2026-08-10T11:00:00+01:00",
            "is_all_day": False,
            "status": "confirmed",
        }
    ]

    fetch_events = MagicMock(
        return_value=fake_events,
    )

    monkeypatch.setattr(
        server,
        "fetch_events",
        fetch_events,
    )

    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "list_events",
            {
                "time_min": (
                    "2026-08-10T09:00:00+01:00"
                ),
                "time_max": (
                    "2026-08-10T18:00:00+01:00"
                ),
                "max_results": 5,
            },
        )

    assert result.is_error is False

    assert result.structured_content == {
        "result": fake_events,
    }

    fetch_events.assert_called_once_with(
        time_min=datetime.fromisoformat(
            "2026-08-10T09:00:00+01:00"
        ),
        time_max=datetime.fromisoformat(
            "2026-08-10T18:00:00+01:00"
        ),
        max_results=5,
    )


@pytest.mark.anyio
async def test_list_events_rejects_datetime_without_timezone():
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "list_events",
            {
                "time_min": "2026-08-10T09:00:00",
            },
        )

    assert result.is_error is True

    assert any(
        "time_min must include time zone information"
        in getattr(content, "text", "")
        for content in result.content
    )


@pytest.mark.anyio
async def test_list_events_rejects_non_positive_max_results():
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "list_events",
            {
                "time_min": "2026-08-10T09:00:00+01:00",
                "max_results": 0,
            },
        )

    assert result.is_error is True

    assert any(
        "max_results must be greater than zero"
        in getattr(content, "text", "")
        for content in result.content
    )


@pytest.mark.anyio
async def test_list_busy_periods_rejects_time_max_before_time_min():
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "list_busy_periods",
            {
                "time_min": "2026-08-10T18:00:00+01:00",
                "time_max": "2026-08-10T09:00:00+01:00",
            },
        )

    assert result.is_error is True

    assert any(
        "time_max must be later than time_min"
        in getattr(content, "text", "")
        for content in result.content
    )
