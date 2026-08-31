from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from google_calendar_mcp.calendar_client import (
    BusyPeriod,
    Event,
    FreePeriod,
    get_event as fetch_event,
    list_busy_periods as fetch_busy_periods,
    list_events as fetch_events,
    list_free_periods as fetch_free_periods,
    list_upcoming_events as fetch_upcoming_events,
)


mcp = MCPServer("Google Calendar")


@mcp.tool()
def get_server_status() -> dict[str, str]:
    """Return the current status of the Google Calendar MCP server."""
    return {
        "name": "Google Calendar",
        "status": "ready",
    }


@mcp.tool()
def get_current_datetime(
    time_zone: str = "Europe/Dublin",
) -> dict[str, str]:
    """Return the current date and time in an IANA time zone."""
    try:
        zone = ZoneInfo(time_zone)
    except ZoneInfoNotFoundError as error:
        raise ToolError(
            f"Unknown IANA time zone: {time_zone}"
        ) from error

    current_datetime = datetime.now(zone)

    return {
        "date": current_datetime.date().isoformat(),
        "date_time": current_datetime.isoformat(),
        "time_zone": time_zone,
    }


@mcp.tool()
def list_upcoming_events(
    max_results: int = 10,
) -> list[Event]:
    """Return upcoming events from the primary Google Calendar."""
    try:
        return fetch_upcoming_events(max_results=max_results)
    except ValueError as error:
        raise ToolError(str(error)) from error


@mcp.tool()
def list_events(
    time_min: str,
    time_max: str | None = None,
    max_results: int = 10,
) -> list[Event]:
    """Return primary calendar events within an ISO 8601 time range."""
    try:
        return fetch_events(
            time_min=_parse_datetime(time_min, "time_min"),
            time_max=(
                _parse_datetime(time_max, "time_max")
                if time_max is not None
                else None
            ),
            max_results=max_results,
        )
    except ValueError as error:
        raise ToolError(str(error)) from error


@mcp.tool()
def get_event(
    event_id: str,
) -> Event:
    """Return a single event from the primary Google Calendar."""
    try:
        return fetch_event(event_id=event_id)
    except ValueError as error:
        raise ToolError(str(error)) from error


@mcp.tool()
def list_busy_periods(
    time_min: str,
    time_max: str,
) -> list[BusyPeriod]:
    """Return busy periods within an ISO 8601 time range."""
    try:
        return fetch_busy_periods(
            time_min=_parse_datetime(time_min, "time_min"),
            time_max=_parse_datetime(time_max, "time_max"),
        )
    except ValueError as error:
        raise ToolError(str(error)) from error


@mcp.tool()
def list_free_periods(
    time_min: str,
    time_max: str,
) -> list[FreePeriod]:
    """Return free periods within an ISO 8601 time range."""
    try:
        return fetch_free_periods(
            time_min=_parse_datetime(time_min, "time_min"),
            time_max=_parse_datetime(time_max, "time_max"),
        )
    except ValueError as error:
        raise ToolError(str(error)) from error


def _parse_datetime(
    value: str,
    name: str,
) -> datetime:
    """Parse an ISO 8601 date-time with time zone information."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ToolError(
            f"{name} must be a valid ISO 8601 date-time"
        ) from error

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolError(
            f"{name} must include time zone information"
        )

    return parsed
