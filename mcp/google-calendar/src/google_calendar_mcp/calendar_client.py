from datetime import datetime, timezone
from typing import Any

from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from google_calendar_mcp.auth import load_credentials
from google_calendar_mcp.telemetry import trace_calendar_api


Event = dict[str, str | bool | None]
BusyPeriod = dict[str, str]
FreePeriod = dict[str, str]

def create_calendar_service() -> Resource:
    """Create an authenticated Google Calendar API client."""
    credentials = load_credentials()

    return build(
        "calendar",
        "v3",
        credentials=credentials,
    )


def list_events(
    time_min: datetime,
    time_max: datetime | None = None,
    max_results: int = 10,
) -> list[Event]:
    """Return events from the primary calendar."""
    if max_results <= 0:
        raise ValueError("max_results must be greater than zero")

    _validate_datetime(time_min, "time_min")

    if time_max is not None:
        _validate_datetime(time_max, "time_max")

        if time_max <= time_min:
            raise ValueError(
                "time_max must be later than time_min"
            )

    request: dict[str, Any] = {
        "calendarId": "primary",
        "timeMin": time_min.isoformat(),
        "maxResults": max_results,
        "singleEvents": True,
        "orderBy": "startTime",
    }

    if time_max is not None:
        request["timeMax"] = time_max.isoformat()

    service = create_calendar_service()

    with trace_calendar_api(operation="events.list"):
        response = (
            service.events()
            .list(**request)
            .execute()
        )

    return [
        _format_event(event)
        for event in response.get("items", [])
    ]


def list_upcoming_events(
    max_results: int = 10,
) -> list[Event]:
    """Return upcoming events from the primary calendar."""
    return list_events(
        time_min=datetime.now(timezone.utc),
        max_results=max_results,
    )


def get_event(event_id: str) -> Event:
    """Return a single event from the primary calendar."""
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id must be a non-empty string")

    service = create_calendar_service()

    try:
        with trace_calendar_api(operation="events.get"):
            event = (
                service.events()
                .get(
                    calendarId="primary",
                    eventId=event_id,
                )
                .execute()
            )
    except HttpError as error:
        if error.resp.status == 404:
            raise ValueError(
                f"Event not found: {event_id}"
            ) from error

        raise

    return _format_event(event)


def list_busy_periods(
    time_min: datetime,
    time_max: datetime,
) -> list[BusyPeriod]:
    """Return busy periods from the primary calendar."""
    _validate_datetime(time_min, "time_min")
    _validate_datetime(time_max, "time_max")

    if time_max <= time_min:
        raise ValueError(
            "time_max must be later than time_min"
        )

    service = create_calendar_service()

    with trace_calendar_api(operation="freebusy.query"):
        response = (
            service.freebusy()
            .query(
                body={
                    "timeMin": time_min.isoformat(),
                    "timeMax": time_max.isoformat(),
                    "items": [
                        {
                            "id": "primary",
                        }
                    ],
                }
            )
            .execute()
        )

    calendars = response.get("calendars", {})

    if not calendars:
        raise RuntimeError(
            "Google Calendar Freebusy API returned no calendars"
        )

    calendar = next(iter(calendars.values()))
    errors = calendar.get("errors", [])

    if errors:
        reasons = ", ".join(
            error.get("reason", "unknown")
            for error in errors
        )
        raise RuntimeError(
            f"Google Calendar Freebusy API failed: {reasons}"
        )

    return [
        {
            "start": period["start"],
            "end": period["end"],
        }
        for period in calendar.get("busy", [])
    ]


def list_free_periods(
    time_min: datetime,
    time_max: datetime,
) -> list[FreePeriod]:
    """Return free periods within the requested time range."""
    _validate_datetime(time_min, "time_min")
    _validate_datetime(time_max, "time_max")

    if time_max <= time_min:
        raise ValueError(
            "time_max must be later than time_min"
        )

    busy_periods = list_busy_periods(
        time_min=time_min,
        time_max=time_max,
    )

    parsed_busy_periods = sorted(
        (
            (
                _parse_api_datetime(
                    period["start"]
                ).astimezone(time_min.tzinfo),
                _parse_api_datetime(
                    period["end"]
                ).astimezone(time_min.tzinfo),
            )
            for period in busy_periods
        ),
        key=lambda period: period[0],
    )

    free_periods: list[FreePeriod] = []
    cursor = time_min

    for busy_start, busy_end in parsed_busy_periods:
        if busy_end <= time_min or busy_start >= time_max:
            continue

        busy_start = max(busy_start, time_min)
        busy_end = min(busy_end, time_max)

        if busy_start > cursor:
            free_periods.append(
                {
                    "start": cursor.isoformat(),
                    "end": busy_start.isoformat(),
                }
            )

        if busy_end > cursor:
            cursor = busy_end

    if cursor < time_max:
        free_periods.append(
            {
                "start": cursor.isoformat(),
                "end": time_max.isoformat(),
            }
        )

    return free_periods


def _format_event(event: dict[str, Any]) -> Event:
    """Return the supported fields from a Google Calendar event."""
    start = event.get("start", {})
    end = event.get("end", {})

    return {
        "id": event.get("id"),
        "summary": event.get("summary", "(No title)"),
        "start": _get_event_time(start),
        "end": _get_event_time(end),
        "is_all_day": _is_all_day_event(start),
        "status": event.get("status"),
    }


def _get_event_time(event_time: dict[str, Any]) -> str | None:
    """Return the date-time or all-day date from an event time."""
    return event_time.get("dateTime") or event_time.get("date")


def _is_all_day_event(event_time: dict[str, Any]) -> bool:
    """Return whether the event uses an all-day date."""
    return "date" in event_time


def _parse_api_datetime(value: str) -> datetime:
    """Parse an RFC 3339 date-time returned by Google APIs."""
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )


def _validate_datetime(
    value: datetime,
    name: str,
) -> None:
    """Validate that a date-time includes time zone information."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{name} must include time zone information"
        )
