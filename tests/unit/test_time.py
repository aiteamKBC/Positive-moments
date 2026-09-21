"""
Business date policy.

    session_date = timezone-aware scheduled_start -> Africa/Cairo -> .date()

Never the UTC date directly, never a naive datetime, never a string slice.
Cairo runs at UTC+2 (EET) and UTC+3 (EEST), so from 21:00/22:00 UTC onwards the
UTC date and the Cairo business date disagree.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.common.time import (
    CAIRO,
    UTC,
    business_date_trace,
    cairo_business_date,
    cairo_day_utc_window,
    parse_graph_datetime,
    require_aware,
)


def test_cairo_day_window_uses_dst_and_next_local_midnight():
    start, end = cairo_day_utc_window(date(2026, 9, 4))
    assert start.isoformat() == "2026-09-03T21:00:00+00:00"
    assert end.isoformat() == "2026-09-04T21:00:00+00:00"


def test_cairo_day_window_outside_dst_is_utc_plus_two():
    start, end = cairo_day_utc_window(date(2026, 1, 15))
    assert start.isoformat() == "2026-01-14T22:00:00+00:00"
    assert end.isoformat() == "2026-01-15T22:00:00+00:00"


def test_utc_to_cairo_business_date():
    assert cairo_business_date(datetime(2026, 9, 3, 22, tzinfo=UTC)) == date(2026, 9, 4)


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        require_aware(datetime(2026, 9, 4))


def test_business_date_is_never_derived_from_a_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        cairo_business_date(datetime(2026, 9, 4, 22))


# --- midnight / UTC offset boundaries ----------------------------------------


@pytest.mark.parametrize("utc_instant, expected", [
    # Summer (EEST, UTC+3): the Cairo day rolls over at 21:00 UTC.
    (datetime(2026, 9, 3, 20, 59, 59, tzinfo=UTC), date(2026, 9, 3)),
    (datetime(2026, 9, 3, 21, 0, 0, tzinfo=UTC), date(2026, 9, 4)),
    (datetime(2026, 9, 4, 20, 59, 59, tzinfo=UTC), date(2026, 9, 4)),
    (datetime(2026, 9, 4, 21, 0, 0, tzinfo=UTC), date(2026, 9, 5)),
    # Winter (EET, UTC+2): it rolls over at 22:00 UTC instead.
    (datetime(2026, 1, 14, 21, 59, 59, tzinfo=UTC), date(2026, 1, 14)),
    (datetime(2026, 1, 14, 22, 0, 0, tzinfo=UTC), date(2026, 1, 15)),
    # Exact Cairo midnight belongs to the day that is starting.
    (datetime(2026, 9, 4, 0, 0, tzinfo=CAIRO), date(2026, 9, 4)),
    # UTC midnight is already 03:00 on the SAME Cairo day, not the day before.
    (datetime(2026, 9, 4, 0, 0, tzinfo=UTC), date(2026, 9, 4)),
])
def test_business_date_at_midnight_boundaries(utc_instant, expected):
    assert cairo_business_date(utc_instant) == expected


def test_utc_date_and_cairo_business_date_genuinely_diverge():
    instant = datetime(2026, 9, 4, 21, 30, tzinfo=UTC)
    assert instant.date() == date(2026, 9, 4)          # naive UTC reading
    assert cairo_business_date(instant) == date(2026, 9, 5)  # correct business date


def test_a_non_utc_offset_start_is_converted_not_truncated():
    """An event carrying +05:30 must not be date-sliced from its raw string."""
    instant = datetime(2026, 9, 5, 1, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert instant.isoformat().startswith("2026-09-05")
    assert cairo_business_date(instant) == date(2026, 9, 4)


def test_all_day_occurrence_overlapping_the_window_keeps_its_own_cairo_date():
    """
    calendarView returns OVERLAPPING occurrences. A 2026-09-03 all-day event
    running to 2026-09-04T00:00Z overlaps the Cairo 2026-09-04 window, but its
    business date is still 2026-09-03. Retaining that is correct, not a bug.
    """
    start = parse_graph_datetime("2026-09-03T00:00:00.0000000", "UTC")
    window_start, window_end = cairo_day_utc_window(date(2026, 9, 4))
    end = parse_graph_datetime("2026-09-04T00:00:00.0000000", "UTC")
    assert start < window_start < end <= window_end      # genuinely overlapping
    assert cairo_business_date(start) == date(2026, 9, 3)


# --- Graph parsing -----------------------------------------------------------


def test_graph_naive_cairo_datetime_is_localized_then_normalized():
    parsed = parse_graph_datetime("2026-09-04T09:00:00.0000000", "Africa/Cairo")
    assert parsed.isoformat() == "2026-09-04T06:00:00+00:00"
    assert cairo_business_date(parsed) == date(2026, 9, 4)


def test_graph_datetime_without_a_usable_timezone_is_rejected():
    with pytest.raises(ValueError, match="unsupported naive Graph timezone"):
        parse_graph_datetime("2026-09-04T09:00:00.0000000", "Pacific Standard Time")


def test_business_date_trace_documents_the_derivation():
    trace = business_date_trace(datetime(2026, 9, 3, 21, 30, tzinfo=UTC))
    assert trace["rule"] == "AWARE_SCHEDULED_START_TO_AFRICA_CAIRO_DATE"
    assert trace["aware_scheduled_start_utc"] == "2026-09-03T21:30:00+00:00"
    assert trace["cairo_datetime"] == "2026-09-04T00:30:00+03:00"
    assert trace["cairo_utc_offset"] == "+0300"
    assert trace["session_date"] == "2026-09-04"
