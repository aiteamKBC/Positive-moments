"""Historical Backfill: the pure rules (range, categories, preview, priorities, scheduling). No database."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.publisher import backfill
from app.publisher.config import ConfigError, PublisherConfig
from app.publisher.daemon import AUTO_RELEASE, DISCOVERY, HISTORICAL_BACKFILL, PublisherDaemon
from app.publisher.handlers import (
    BUNNY_PRIORITY, HISTORICAL_PRIORITY, HISTORICAL_TAG, LIVE_PRIORITY, REFRESH_PRIORITY, follow_up,
)

LONDON, CAIRO = ZoneInfo("Europe/London"), ZoneInfo("Africa/Cairo")
NOW = datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 29)


# --------------------------------------------------------------------------- range
def test_a_single_day_is_one_inclusive_day():
    assert backfill.validate_range(date(2026, 9, 1), date(2026, 9, 1), today=TODAY, max_days=31) == 1
    assert backfill.business_days(date(2026, 9, 1), date(2026, 9, 1)) == [date(2026, 9, 1)]


def test_a_multi_day_range_includes_both_boundaries():
    days = backfill.business_days(date(2026, 9, 1), date(2026, 9, 10))
    assert backfill.validate_range(date(2026, 9, 1), date(2026, 9, 10), today=TODAY, max_days=31) == 10
    assert days[0] == date(2026, 9, 1) and days[-1] == date(2026, 9, 10) and len(days) == 10


@pytest.mark.parametrize("date_from, date_to, code", [
    (date(2026, 9, 10), date(2026, 9, 1), "INVALID_RANGE"),
    (date(2026, 10, 20), date(2026, 10, 30), "FUTURE_DATE"),
    (date(2026, 11, 1), date(2026, 11, 1), "FUTURE_DATE"),
    (date(2026, 8, 1), date(2026, 9, 1), "RANGE_TOO_LONG"),
])
def test_invalid_reversed_future_and_oversized_ranges_are_refused(date_from, date_to, code):
    with pytest.raises(backfill.RangeRefused) as refused:
        backfill.validate_range(date_from, date_to, today=TODAY, max_days=31)
    assert refused.value.code == code


def test_the_maximum_range_is_configurable_and_inclusive():
    assert backfill.validate_range(date(2026, 9, 1), date(2026, 9, 7), today=TODAY, max_days=7) == 7
    with pytest.raises(backfill.RangeRefused):
        backfill.validate_range(date(2026, 9, 1), date(2026, 9, 8), today=TODAY, max_days=7)


def test_today_is_allowed_and_dates_must_parse():
    assert backfill.validate_range(TODAY, TODAY, today=TODAY, max_days=31) == 1
    for bad in ("", "2026-13-01", "01/09/2026", None):
        with pytest.raises(backfill.RangeRefused) as refused:
            backfill.parse_date(bad, "From Date")
        assert refused.value.code == "INVALID_DATE"


def test_ranges_across_uk_and_egypt_daylight_saving_changes_count_calendar_days():
    # UK clocks go back on 2026-10-25, Egypt's on 2026-10-29/30, UK clocks go forward on 2026-03-29.
    assert len(backfill.business_days(date(2026, 10, 24), date(2026, 10, 31))) == 8
    assert len(backfill.business_days(date(2026, 3, 28), date(2026, 3, 30))) == 3


@pytest.mark.parametrize("instant, expected", [
    # 23:30 UK time on the night the UK clocks change: already the next day in Cairo.
    (datetime(2026, 10, 25, 23, 30, tzinfo=LONDON), date(2026, 10, 26)),
    (datetime(2026, 3, 29, 23, 30, tzinfo=LONDON), date(2026, 3, 30)),
    # Just before / after Cairo midnight on either side of Egypt's own DST change.
    (datetime(2026, 10, 29, 23, 59, tzinfo=CAIRO), date(2026, 10, 29)),
    (datetime(2026, 10, 30, 0, 1, tzinfo=CAIRO), date(2026, 10, 30)),
])
def test_the_business_date_is_the_cairo_date_whatever_the_dst_state(instant, expected):
    assert backfill.cairo_today(instant.astimezone(timezone.utc)) == expected


# --------------------------------------------------------------------------- categories / preview
def _fact(lecture_id="L1", *, day=date(2026, 9, 3), match=None, recording=None, reason=None, jobs=(), videos=(),
          cancelled=False, end_offset=timedelta(hours=-1)):
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=9)
    return {"lecture_id": lecture_id, "session_date": day, "scheduled_start": start,
            "scheduled_end": start + timedelta(hours=2), "subject": "Synthetic", "module": "Synthetic",
            "cancelled": cancelled, "suppressed": False, "match_status": match, "match_review": False,
            "occurrence_id": None, "link_current": False, "recording_status": recording,
            "recording_reason": reason, "recording_review_reason": reason, "expected_parts": None,
            "located_parts": None, "next_check_after": None, "selection_status": None, "jobs": list(jobs),
            "videos": list(videos), "updated_at": None}


def _preview(facts, recordings=None, **kwargs):
    return backfill.build_preview(facts, {}, recordings or {}, date_from=date(2026, 9, 1), date_to=date(2026, 9, 10),
                                  library_id=786045, now=NOW, **kwargs)


def _category(fact, recordings=None):
    return _preview([fact], recordings)["lectures"][0]["category"]


def test_a_lecture_the_publisher_never_saw_is_ready_to_queue():
    assert _category(_fact()) == backfill.READY_TO_QUEUE


def test_a_verified_located_lecture_without_work_is_queued_for_import():
    recordings = {"L1": {"verified_count": 2, "candidates": 2, "selected_part": 2,
                         "selection_rule": "longest_recording", "selected_duration_seconds": 7348.5}}
    preview = _preview([_fact(match="VERIFIED", recording="LOCATED")], recordings)
    row = preview["lectures"][0]
    assert row["category"] == backfill.READY_TO_QUEUE and row["proposed_action"] == "QUEUE"
    assert row["recording"]["selected_part"] == 2 and row["recording"]["selected_duration_seconds"] == 7348.5
    assert preview["summary"]["estimated_imports"] == {"known": 1, "possible": 0, "upper_bound": 1}
    assert preview["summary"]["known_selected_duration_seconds"] == 7348.5


def test_live_work_is_already_processing_and_never_counted_as_an_import():
    jobs = [{"kind": "RESOLVE_RECORDING", "status": "RETRY_WAIT", "error": None, "next_attempt_at": None,
             "attempts": 1, "max_attempts": 100}]
    preview = _preview([_fact(match="VERIFIED", recording="WAITING", jobs=jobs)])
    assert preview["lectures"][0]["category"] == backfill.ALREADY_PROCESSING
    assert preview["summary"]["eligible"] == 0 and preview["summary"]["estimated_imports"]["upper_bound"] == 0


@pytest.mark.parametrize("match", ["NO_LMS_SESSION", "AMBIGUOUS", "CONFLICT"])
def test_a_missing_or_ambiguous_lms_match_is_blocked_never_queued(match):
    assert _category(_fact(match=match)) == backfill.MISSING_LMS_MATCH


def test_a_recording_that_was_never_found_is_missing_recording():
    fact = _fact(match="VERIFIED", recording="REVIEW_REQUIRED", reason="DEADLINE_PASSED:RECORDING_NOT_FOUND")
    assert _category(fact) == backfill.MISSING_RECORDING


def test_an_ambiguous_recording_selection_needs_review():
    fact = _fact(match="VERIFIED", recording="REVIEW_REQUIRED", reason="AMBIGUOUS_OCCURRENCE_RECORDINGS")
    assert _category(fact) == backfill.REVIEW_REQUIRED


def test_cancelled_out_of_scope_and_unfinished_lectures_are_skipped():
    assert _category(_fact(cancelled=True)) == backfill.SKIPPED
    assert _category(_fact(match="TEST_EXCLUDED")) == backfill.SKIPPED
    today = _fact(day=TODAY)
    today["scheduled_end"] = NOW + timedelta(hours=3)
    today["scheduled_start"] = NOW + timedelta(hours=1)
    assert _category(today) == backfill.SKIPPED


def test_a_resolution_error_is_reported_as_error_not_hidden():
    preview = _preview([_fact()], errors={"L1": "RESOLUTION_FAILED:OperationalError"})
    assert preview["lectures"][0]["category"] == backfill.ERROR
    assert preview["summary"]["blocked"] == 1 and preview["summary"]["eligible"] == 0


def test_the_preview_summarises_every_day_of_the_range_including_empty_days():
    preview = _preview([_fact("A", day=date(2026, 9, 1)), _fact("B", day=date(2026, 9, 10)),
                        _fact("C", day=date(2026, 9, 10), match="NO_LMS_SESSION")])
    per_day = preview["summary"]["per_day"]
    assert list(per_day) == [f"2026-09-{d:02d}" for d in range(1, 11)]
    assert per_day["2026-09-01"] == {"total": 1, "eligible": 1}
    assert per_day["2026-09-10"] == {"total": 2, "eligible": 1}
    assert per_day["2026-09-05"] == {"total": 0, "eligible": 0}
    assert preview["summary"]["total"] == 3 and preview["summary"]["eligible"] == 2
    assert preview["summary"]["blocked"] == 1 and preview["data_source"] == "DATABASE"
    assert {r["data_source"] for r in preview["lectures"]} == {"DATABASE"}


def test_the_fingerprint_binds_range_library_and_every_category():
    facts = [_fact("A"), _fact("B")]
    base = _preview(facts)["fingerprint"]
    assert _preview(list(reversed(facts)))["fingerprint"] == base               # order-independent
    assert _preview([_fact("A"), _fact("B", match="NO_LMS_SESSION")])["fingerprint"] != base
    assert _preview([_fact("A")])["fingerprint"] != base
    other = backfill.build_preview(facts, {}, {}, date_from=date(2026, 9, 1), date_to=date(2026, 9, 11),
                                   library_id=786045, now=NOW)
    assert other["fingerprint"] != base


def test_run_items_start_eligible_lectures_pending_and_record_the_rest():
    preview = _preview([_fact("A"), _fact("B", match="NO_LMS_SESSION"), _fact("C", cancelled=True)])
    items = {i["lecture_id"]: i for i in backfill.run_items(preview)}
    assert items["A"]["status"] == "PENDING" and items["A"]["reason"] is None
    assert items["B"]["status"] == "BLOCKED" and items["B"]["reason"] == backfill.MISSING_LMS_MATCH
    assert items["C"]["status"] == "SKIPPED"


def test_the_pipeline_lists_every_stage_in_order():
    row = {"recording": {"status": "LOCATED"}, "lms": {"status": "VERIFIED"}, "bunny": {"status": "UPLOADED"},
           "video": {"status": "VERIFIED"}, "captions": {"status": "PENDING"}, "release": {"status": "NOT_RELEASED"},
           "playback": {"available": False, "reason": "NOT_RELEASED"}, "state": "READY_TO_RELEASE"}
    steps = backfill.pipeline(row)
    assert [s["label"] for s in steps] == ["Teams", "LMS Match", "Bunny Import", "Encoding", "Captions", "Release",
                                           "LMS Playback"]
    assert [s["done"] for s in steps] == [True, True, True, True, False, False, False]
    assert backfill.stage_of(row) == "RELEASE"


# --------------------------------------------------------------------------- priorities
def test_every_historical_priority_is_below_every_daily_priority():
    daily = [LIVE_PRIORITY, REFRESH_PRIORITY, *BUNNY_PRIORITY.values()]
    assert max(HISTORICAL_PRIORITY.values()) < min(daily)


def test_follow_ups_keep_the_historical_tag_and_priority_and_daily_ones_are_unchanged():
    historical = {"payload": {HISTORICAL_TAG: "run-1"}}
    assert follow_up(historical, "POLL_ENCODING") == (HISTORICAL_PRIORITY["POLL_ENCODING"], {HISTORICAL_TAG: "run-1"})
    assert follow_up(historical, "PUBLISH_CAPTION") == (HISTORICAL_PRIORITY["PUBLISH_CAPTION"], {HISTORICAL_TAG: "run-1"})
    for kind, priority in BUNNY_PRIORITY.items():
        assert follow_up({"payload": {}}, kind) == (priority, None)
        assert follow_up({"payload": {"operator_retry": True}}, kind) == (priority, None)


# --------------------------------------------------------------------------- configuration / scheduling
def _config(**kwargs):
    values = dict(database_url="postgresql://u@h/d", graph_tenant_id="t", graph_client_id="c",
                  graph_client_secret="s", start_date=date(2026, 10, 1), lms_database_url="x",
                  discover_calendar=False)
    return PublisherConfig(**{**values, **kwargs})


def test_backfill_settings_default_safely_and_are_validated():
    config = PublisherConfig.from_mapping({})
    assert (config.backfill_max_days, config.backfill_batch_size, config.backfill_max_active,
            config.backfill_max_imports) == (31, 10, 20, 1)
    assert config.lookback_days == 3                         # the daily lookback is not touched
    with pytest.raises(ConfigError):
        _config(backfill_max_imports=5, max_imports_in_flight=2).validate()
    with pytest.raises(ConfigError):
        _config(backfill_max_days=0).validate()
    assert _config().describe()["backfill"]["max_imports"] == 1


def test_the_backfill_task_is_scheduled_last_and_only_with_a_coordinator():
    config = _config(workers=1)
    daemon = PublisherDaemon(config=config, connect=None, handlers={}, planner=None, reconciler=None,
                             backfill=SimpleNamespace(run=lambda: {}), auto_releaser=SimpleNamespace())
    connection = SimpleNamespace()
    daemon.schedule = SimpleNamespace(all=lambda c: [])
    due = daemon.due_tasks(connection, NOW)
    assert due[-1] == HISTORICAL_BACKFILL and due.index(DISCOVERY) < due.index(AUTO_RELEASE) < due.index(HISTORICAL_BACKFILL)
    recently = [{"task": HISTORICAL_BACKFILL, "last_started_at": NOW - timedelta(seconds=30)}]
    daemon.schedule = SimpleNamespace(all=lambda c: recently)
    assert HISTORICAL_BACKFILL not in daemon.due_tasks(connection, NOW)
    plain = PublisherDaemon(config=config, connect=None, handlers={}, planner=None, reconciler=None)
    plain.schedule = SimpleNamespace(all=lambda c: [])
    assert HISTORICAL_BACKFILL not in plain.due_tasks(connection, NOW)
