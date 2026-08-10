from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, strategies as st

from google_calendar_mcp import calendar_client


def test_format_event_with_datetime():
    event = {
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

    result = calendar_client._format_event(event)

    assert result == {
        "id": "event-1",
        "summary": "Team meeting",
        "start": "2026-08-10T10:00:00+01:00",
        "end": "2026-08-10T11:00:00+01:00",
        "is_all_day": False,
        "status": "confirmed",
    }


def test_format_event_with_all_day_event():
    event = {
        "id": "event-2",
        "summary": "Holiday",
        "start": {
            "date": "2026-08-10",
        },
        "end": {
            "date": "2026-08-11",
        },
        "status": "confirmed",
    }

    result = calendar_client._format_event(event)

    assert result == {
        "id": "event-2",
        "summary": "Holiday",
        "start": "2026-08-10",
        "end": "2026-08-11",
        "is_all_day": True,
        "status": "confirmed",
    }


@pytest.mark.parametrize(
    "max_results",
    [0, -1, -10],
)
def test_list_events_rejects_invalid_max_results(max_results):
    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00+01:00"
    )

    with pytest.raises(
        ValueError,
        match="max_results must be greater than zero",
    ):
        calendar_client.list_events(
            time_min=time_min,
            max_results=max_results,
        )


def test_list_events_rejects_datetime_without_timezone():
    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00"
    )

    with pytest.raises(
        ValueError,
        match="time_min must include time zone information",
    ):
        calendar_client.list_events(
            time_min=time_min,
        )


def test_list_events_uses_google_calendar_service(monkeypatch):
    fake_service = MagicMock()

    fake_service.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {
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
        ]
    }

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00+01:00"
    )
    time_max = datetime.fromisoformat(
        "2026-08-10T18:00:00+01:00"
    )

    result = calendar_client.list_events(
        time_min=time_min,
        time_max=time_max,
        max_results=5,
    )

    assert result == [
        {
            "id": "event-1",
            "summary": "Team meeting",
            "start": "2026-08-10T10:00:00+01:00",
            "end": "2026-08-10T11:00:00+01:00",
            "is_all_day": False,
            "status": "confirmed",
        }
    ]

    fake_service.events.return_value.list.assert_called_once_with(
        calendarId="primary",
        timeMin="2026-08-10T09:00:00+01:00",
        timeMax="2026-08-10T18:00:00+01:00",
        maxResults=5,
        singleEvents=True,
        orderBy="startTime",
    )


def test_list_busy_periods_uses_freebusy_api(monkeypatch):
    fake_service = MagicMock()

    fake_service.freebusy.return_value.query.return_value.execute.return_value = {
        "calendars": {
            "primary": {
                "busy": [
                    {
                        "start": "2026-08-10T10:00:00+01:00",
                        "end": "2026-08-10T11:00:00+01:00",
                    },
                    {
                        "start": "2026-08-10T14:00:00+01:00",
                        "end": "2026-08-10T15:00:00+01:00",
                    },
                ]
            }
        }
    }

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00+01:00"
    )
    time_max = datetime.fromisoformat(
        "2026-08-10T18:00:00+01:00"
    )

    result = calendar_client.list_busy_periods(
        time_min=time_min,
        time_max=time_max,
    )

    assert result == [
        {
            "start": "2026-08-10T10:00:00+01:00",
            "end": "2026-08-10T11:00:00+01:00",
        },
        {
            "start": "2026-08-10T14:00:00+01:00",
            "end": "2026-08-10T15:00:00+01:00",
        },
    ]

    fake_service.freebusy.return_value.query.assert_called_once_with(
        body={
            "timeMin": "2026-08-10T09:00:00+01:00",
            "timeMax": "2026-08-10T18:00:00+01:00",
            "items": [
                {
                    "id": "primary",
                }
            ],
        }
    )


def test_list_busy_periods_raises_when_google_returns_error(monkeypatch):
    fake_service = MagicMock()

    fake_service.freebusy.return_value.query.return_value.execute.return_value = {
        "calendars": {
            "primary": {
                "errors": [
                    {
                        "reason": "notFound",
                    }
                ]
            }
        }
    }

    monkeypatch.setattr(
        calendar_client,
        "create_calendar_service",
        lambda: fake_service,
    )

    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00+01:00"
    )
    time_max = datetime.fromisoformat(
        "2026-08-10T18:00:00+01:00"
    )

    with pytest.raises(
        RuntimeError,
        match="Google Calendar Freebusy API failed: notFound",
    ):
        calendar_client.list_busy_periods(
            time_min=time_min,
            time_max=time_max,
        )


def test_list_free_periods_returns_whole_range_when_no_busy_periods(
    monkeypatch,
):
    monkeypatch.setattr(
        calendar_client,
        "list_busy_periods",
        lambda time_min, time_max: [],
    )

    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00+01:00"
    )
    time_max = datetime.fromisoformat(
        "2026-08-10T18:00:00+01:00"
    )

    result = calendar_client.list_free_periods(
        time_min=time_min,
        time_max=time_max,
    )

    assert result == [
        {
            "start": "2026-08-10T09:00:00+01:00",
            "end": "2026-08-10T18:00:00+01:00",
        }
    ]


def test_list_free_periods_between_busy_periods(monkeypatch):
    busy_periods = [
        {
            "start": "2026-08-10T10:00:00+01:00",
            "end": "2026-08-10T11:00:00+01:00",
        },
        {
            "start": "2026-08-10T12:30:00+01:00",
            "end": "2026-08-10T13:30:00+01:00",
        },
    ]

    monkeypatch.setattr(
        calendar_client,
        "list_busy_periods",
        lambda time_min, time_max: busy_periods,
    )

    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00+01:00"
    )
    time_max = datetime.fromisoformat(
        "2026-08-10T18:00:00+01:00"
    )

    result = calendar_client.list_free_periods(
        time_min=time_min,
        time_max=time_max,
    )

    assert result == [
        {
            "start": "2026-08-10T09:00:00+01:00",
            "end": "2026-08-10T10:00:00+01:00",
        },
        {
            "start": "2026-08-10T11:00:00+01:00",
            "end": "2026-08-10T12:30:00+01:00",
        },
        {
            "start": "2026-08-10T13:30:00+01:00",
            "end": "2026-08-10T18:00:00+01:00",
        },
    ]


def test_list_free_periods_merges_overlapping_busy_periods(monkeypatch):
    busy_periods = [
        {
            "start": "2026-08-10T10:00:00+01:00",
            "end": "2026-08-10T12:00:00+01:00",
        },
        {
            "start": "2026-08-10T11:00:00+01:00",
            "end": "2026-08-10T13:00:00+01:00",
        },
    ]

    monkeypatch.setattr(
        calendar_client,
        "list_busy_periods",
        lambda time_min, time_max: busy_periods,
    )

    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00+01:00"
    )
    time_max = datetime.fromisoformat(
        "2026-08-10T18:00:00+01:00"
    )

    result = calendar_client.list_free_periods(
        time_min=time_min,
        time_max=time_max,
    )

    assert result == [
        {
            "start": "2026-08-10T09:00:00+01:00",
            "end": "2026-08-10T10:00:00+01:00",
        },
        {
            "start": "2026-08-10T13:00:00+01:00",
            "end": "2026-08-10T18:00:00+01:00",
        },
    ]


def test_list_free_periods_handles_adjacent_busy_periods(monkeypatch):
    busy_periods = [
        {
            "start": "2026-08-10T10:00:00+01:00",
            "end": "2026-08-10T11:00:00+01:00",
        },
        {
            "start": "2026-08-10T11:00:00+01:00",
            "end": "2026-08-10T12:00:00+01:00",
        },
    ]

    monkeypatch.setattr(
        calendar_client,
        "list_busy_periods",
        lambda time_min, time_max: busy_periods,
    )

    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00+01:00"
    )
    time_max = datetime.fromisoformat(
        "2026-08-10T18:00:00+01:00"
    )

    result = calendar_client.list_free_periods(
        time_min=time_min,
        time_max=time_max,
    )

    assert result == [
        {
            "start": "2026-08-10T09:00:00+01:00",
            "end": "2026-08-10T10:00:00+01:00",
        },
        {
            "start": "2026-08-10T12:00:00+01:00",
            "end": "2026-08-10T18:00:00+01:00",
        },
    ]


def test_list_free_periods_clips_busy_periods_to_requested_range(
    monkeypatch,
):
    busy_periods = [
        {
            "start": "2026-08-10T08:00:00+01:00",
            "end": "2026-08-10T10:00:00+01:00",
        },
        {
            "start": "2026-08-10T17:00:00+01:00",
            "end": "2026-08-10T19:00:00+01:00",
        },
    ]

    monkeypatch.setattr(
        calendar_client,
        "list_busy_periods",
        lambda time_min, time_max: busy_periods,
    )

    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00+01:00"
    )
    time_max = datetime.fromisoformat(
        "2026-08-10T18:00:00+01:00"
    )

    result = calendar_client.list_free_periods(
        time_min=time_min,
        time_max=time_max,
    )

    assert result == [
        {
            "start": "2026-08-10T10:00:00+01:00",
            "end": "2026-08-10T17:00:00+01:00",
        }
    ]


def test_list_free_periods_returns_empty_when_whole_range_is_busy(
    monkeypatch,
):
    busy_periods = [
        {
            "start": "2026-08-10T08:00:00+01:00",
            "end": "2026-08-10T19:00:00+01:00",
        }
    ]

    monkeypatch.setattr(
        calendar_client,
        "list_busy_periods",
        lambda time_min, time_max: busy_periods,
    )

    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00+01:00"
    )
    time_max = datetime.fromisoformat(
        "2026-08-10T18:00:00+01:00"
    )

    result = calendar_client.list_free_periods(
        time_min=time_min,
        time_max=time_max,
    )

    assert result == []


@st.composite
def busy_period_lists(draw):
    period_count = draw(
        st.integers(
            min_value=0,
            max_value=10,
        )
    )

    periods = []

    for _ in range(period_count):
        start_minute = draw(
            st.integers(
                min_value=-120,
                max_value=600,
            )
        )

        duration_minutes = draw(
            st.integers(
                min_value=1,
                max_value=240,
            )
        )

        start = datetime.fromisoformat(
            "2026-08-10T09:00:00+01:00"
        )
        start = start + timedelta(
            minutes=start_minute
        )

        end = start + timedelta(
            minutes=duration_minutes
        )

        periods.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        )

    return periods


@given(busy_periods=busy_period_lists())
def test_list_free_periods_always_returns_valid_intervals(
    busy_periods,
):
    time_min = datetime.fromisoformat(
        "2026-08-10T09:00:00+01:00"
    )
    time_max = datetime.fromisoformat(
        "2026-08-10T18:00:00+01:00"
    )

    with patch.object(
        calendar_client,
        "list_busy_periods",
        return_value=busy_periods,
    ):
        result = calendar_client.list_free_periods(
            time_min=time_min,
            time_max=time_max,
        )

    parsed_free_periods = [
        (
            datetime.fromisoformat(period["start"]),
            datetime.fromisoformat(period["end"]),
        )
        for period in result
    ]

    parsed_busy_periods = [
        (
            datetime.fromisoformat(period["start"]),
            datetime.fromisoformat(period["end"]),
        )
        for period in busy_periods
    ]

    for free_start, free_end in parsed_free_periods:
        assert time_min <= free_start
        assert free_end <= time_max
        assert free_start < free_end

    for previous, current in zip(
        parsed_free_periods,
        parsed_free_periods[1:],
    ):
        previous_start, previous_end = previous
        current_start, current_end = current

        assert previous_start <= current_start
        assert previous_end <= current_start
        assert current_start < current_end

    for free_start, free_end in parsed_free_periods:
        for busy_start, busy_end in parsed_busy_periods:
            if busy_end <= time_min:
                continue

            if busy_start >= time_max:
                continue

            busy_start = max(
                busy_start,
                time_min,
            )
            busy_end = min(
                busy_end,
                time_max,
            )

            assert (
                free_end <= busy_start
                or free_start >= busy_end
            )
