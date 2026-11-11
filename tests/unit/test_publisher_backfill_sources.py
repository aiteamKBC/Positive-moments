"""
Historical Backfill, two independent sources (2026-11-02): the Teams calendar AND the QA database
(qa_doctors_sessions) are read for every day; one inventory, de-duplicated by (Cairo date, Teams meeting) only.
Pure rules and the coordinator's per-day discovery, with fake sources - no database, Graph or LMS.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.publisher import backfill
from app.publisher import historical_sources as sources
from app.publisher.backfill import HistoricalBackfill
from app.lms import meeting_key

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
DAY = date(2026, 9, 2)
D = DAY.isoformat()


def graph_meeting(n: int) -> str:
    """A Graph onlineMeeting id, as Teams makes them: base64 of 1*<organizer>*0**<thread>."""
    return base64.b64encode(f"1*org-{n}*0**19:meeting_{n:04d}abc@thread.v2".encode()).decode()


def qa(n: int, *, day: str = D, meeting: int | None = None, cancelled=False, transcript=False, session=None,
       lms_keys=frozenset()) -> dict:
    """A QA entry as a preview stores it (historical_sources.entry), for meeting `meeting` (default n)."""
    mid = graph_meeting(n if meeting is None else meeting)
    record = {"qa_ref": sources.ref(session or f"transcript-{n}-{day}"), "meeting_ref": sources.ref(mid),
              "meeting_key": meeting_key.from_online_meeting_id(mid), "session_date": day,
              "subject": f"Lecture {n}", "module": f"Module {n}", "cancelled": cancelled,
              "transcript_reference": transcript, "recording_reference": True}
    return sources.entry(record, lms_keys=lms_keys)


def lms_keys(*meetings: int) -> frozenset:
    return frozenset(meeting_key.from_online_meeting_id(graph_meeting(n)) for n in meetings)


def teams(lid: str, n: int, *, day: str = D, lms="VERIFIED") -> dict:
    return {"lecture_id": lid, "session_date": day, "subject": f"Lecture {n}", "module": f"Module {n}",
            "scheduled_start": f"{day}T09:00:00+00:00", "scheduled_end": f"{day}T11:00:00+00:00",
            "meeting_resolved": True, "lms_dry": lms, "lms_occurrence_id": "OCC-1" if lms == "VERIFIED" else None,
            "meeting_ref": sources.ref(graph_meeting(n))}


def fact(lid: str, *, day=DAY, match="VERIFIED", recording=None, suppressed=False, videos=()) -> dict:
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=9)
    return {"lecture_id": lid, "session_date": day, "scheduled_start": start, "scheduled_end": start + timedelta(hours=2),
            "subject": "Stored", "module": "Stored", "cancelled": False, "suppressed": suppressed,
            "match_status": match, "match_review": False, "occurrence_id": None, "link_current": False,
            "recording_status": recording, "recording_reason": None, "recording_review_reason": None,
            "expected_parts": None, "located_parts": None, "next_check_after": None, "selection_status": None,
            "jobs": [], "videos": list(videos), "updated_at": None}


def registry(lid: str, n: int | None, *, day: str = D, suppressed=False) -> dict:
    return {"lecture_id": lid, "session_date": day, "suppressed": suppressed,
            "meeting_ref": sources.ref(graph_meeting(n)) if n is not None else None}


def coverage(*, qa_entries=(), day: str = D, status="COMPLETE", qa_status="COMPLETE", teams_status=None):
    out = {d.isoformat(): {"status": "COMPLETE", "qa": {"status": "COMPLETE", "entries": []}}
           for d in backfill.business_days(DAY, DAY)}
    out[day] = {"status": status, "teams": {"status": teams_status or ("COMPLETE" if status == "COMPLETE" else "FAILED")},
                "qa": {"status": qa_status, "entries": list(qa_entries) if qa_status == "COMPLETE" else []}}
    return out


def preview(facts=(), *, found=(), qa_entries=(), reg=(), **cov):
    return backfill.build_preview(list(facts), {}, {}, date_from=DAY, date_to=DAY, library_id=786045, now=NOW,
                                  discovered={e["lecture_id"]: e for e in found},
                                  coverage=coverage(qa_entries=qa_entries, **cov), registry=list(reg))


def by_source(p):
    return sorted(r["provenance"] for r in p["lectures"])


# =========================================================================== 1-3: the counting examples
def test_teams_seven_qa_five_overlap_three_is_nine_unique_lectures():
    found = [teams(f"T{n}", n) for n in range(1, 8)]                          # meetings 1..7
    entries = [qa(n) for n in (1, 2, 3)] + [qa(n, lms_keys=lms_keys(8)) for n in (8, 9)]   # 1..3 overlap
    p = preview(found=found, qa_entries=entries)
    s = p["summary"]
    assert s["total"] == 9 and s["qa_records"] == 5 and s["qa_matched"] == 3 and s["qa_only"] == 2
    assert s["teams_discovered"] == 7 and len({r["lecture_id"] or r["qa_ref"] for r in p["lectures"]}) == 9
    assert by_source(p) == ["QA"] * 2 + ["TEAMS"] * 4 + ["TEAMS + QA"] * 3
    assert s["per_day"][D]["outcome"] == "DISCOVERED" and s["per_day"][D]["qa_records"] == 5


def test_teams_zero_qa_five_is_five_candidates_not_an_empty_day():
    p = preview(qa_entries=[qa(n) for n in range(1, 6)])
    assert p["summary"]["total"] == 5 and p["summary"]["qa_only"] == 5
    assert p["summary"]["per_day"][D]["outcome"] == "DISCOVERED"
    assert all(r["origin"] == sources.QA_ONLY and r["lecture_id"] is None for r in p["lectures"])


def test_teams_zero_qa_zero_and_nothing_stored_is_a_completed_empty_day():
    p = preview()
    assert p["complete"] is True and p["summary"]["total"] == 0
    assert p["summary"]["per_day"][D]["outcome"] == backfill.COMPLETED_EMPTY_DAY


# =========================================================================== 4-5: one source failing
def test_teams_failing_while_qa_answers_is_partial_never_empty_and_keeps_qa_lectures():
    p = preview(qa_entries=[qa(1)], status=backfill.PARTIAL, teams_status="FAILED")
    day = p["summary"]["per_day"][D]
    assert p["complete"] is False and D in p["summary"]["incomplete_days"]
    assert day["outcome"] == "PARTIAL_DISCOVERY" and day["teams_status"] == "FAILED" and day["qa_status"] == "COMPLETE"
    assert p["summary"]["qa_only"] == 1                       # what QA did answer is shown, not dropped


def test_qa_failing_while_teams_answers_is_partial_not_complete():
    p = preview(found=[teams("T1", 1)], status=backfill.PARTIAL, teams_status="COMPLETE", qa_status="FAILED")
    day = p["summary"]["per_day"][D]
    assert p["complete"] is False and day["outcome"] == "PARTIAL_DISCOVERY" and day["qa_status"] == "FAILED"
    assert p["summary"]["total"] == 1


@pytest.mark.parametrize("teams_status, qa_status, expected", [
    ("COMPLETE", "COMPLETE", backfill.COMPLETE), ("FAILED", "COMPLETE", backfill.PARTIAL),
    ("COMPLETE", "FAILED", backfill.PARTIAL), ("FAILED", "FAILED", backfill.FAILED_DAY)])
def test_a_day_is_complete_only_when_both_sources_answered(teams_status, qa_status, expected):
    day = backfill._day({"status": teams_status}, {"status": qa_status}, 1)
    assert day["status"] == expected
    assert (expected == backfill.COMPLETE) == ("error" not in day)


# =========================================================================== 6-9: identity
def test_a_qa_lecture_absent_from_teams_is_retained_with_the_evidence_it_lacks():
    [row] = preview(qa_entries=[qa(4)])["lectures"]
    assert row["category"] == backfill.MISSING_LMS_MATCH and row["code"] == sources.NO_LMS_SESSION
    assert row["missing_evidence"] == ["TEAMS_CALENDAR_OCCURRENCE", "EXACT_SCHEDULED_START", "LMS_SESSION"]
    assert row["proposed_action"] == "NONE" and row["verification"]["recording"] == "NOT_CHECKED"


def test_a_qa_lecture_with_a_meeting_id_merges_into_the_teams_lecture_of_that_date():
    p = preview(found=[teams("T1", 1)], qa_entries=[qa(1, transcript=True)])
    [row] = p["lectures"]
    assert row["lecture_id"] == "T1" and row["sources"] == ["TEAMS", "QA"]
    assert row["qa"]["status"] == sources.MATCHED and row["qa"]["transcript_reference"] is True


def test_a_qa_lecture_without_a_meeting_is_preserved_and_never_marked_verified():
    record = {**qa(1), "meeting_ref": None, "parsable_meeting": False, "lms_session_on_day": None}
    [row] = preview(found=[teams("T1", 1)], qa_entries=[record])["lectures"][1:] or [None]
    assert row["code"] == sources.NO_MEETING and row["category"] == backfill.REVIEW_REQUIRED
    assert row["lms"]["status"] == "NOT_CHECKED" and row["verification"]["teams"] == "NOT_IN_TEAMS"


def test_one_lecture_in_teams_qa_and_the_publisher_is_one_row():
    p = preview([fact("L1")], found=[teams("L1", 1)], qa_entries=[qa(1)], reg=[registry("L1", 1)])
    [row] = p["lectures"]
    assert row["provenance"] == "TEAMS + PUBLISHER + QA" and p["summary"]["eligible"] == 1
    assert [i["lecture_id"] for i in backfill.run_items(p)] == ["L1"]


# =========================================================================== 10-12: duplicates
def test_duplicate_calendar_entries_keep_one_canonical_winner_and_qa_never_revives_the_loser():
    # Ray MSP 2026-10-30: the LI_DUPLICATE invitation shares the date; QA belongs to the winner only.
    reg = [registry("WIN", 1), registry("LOSER", 1, suppressed=True)]
    p = preview([fact("WIN"), fact("LOSER", suppressed=True)], qa_entries=[qa(1)], reg=reg)
    rows = {r["lecture_id"]: r for r in p["lectures"]}
    assert rows["WIN"]["sources"] == ["PUBLISHER", "QA"] and rows["LOSER"]["qa"] is None
    assert p["summary"]["suppressed"] == 1 and p["summary"]["qa_only"] == 0


def test_a_suppressed_duplicate_without_a_winner_keeps_its_qa_and_stays_suppressed():
    attached, qa_only = sources.merge([qa(1)], [registry("LOSER", 1, suppressed=True)])
    assert attached["LOSER"]["status"] == sources.MATCHED and qa_only == []


def test_duplicate_qa_records_for_one_lecture_are_one_lecture():
    p = preview(found=[teams("T1", 1)], qa_entries=[qa(1, session="a"), qa(1, session="b")])
    [row] = p["lectures"]
    assert row["qa"]["records"] == 2 and p["summary"]["total"] == 1 and p["summary"]["qa_matched"] == 2
    only = preview(qa_entries=[qa(1, session="a"), qa(1, session="b")])["lectures"]
    assert len(only) == 1 and only[0]["qa"]["records"] == 2


def test_same_subject_and_date_but_different_meetings_are_different_lectures():
    a, b = {**qa(1), "subject": "Same", "module": "Same"}, {**qa(2), "subject": "Same", "module": "Same"}
    p = preview(found=[{**teams("T1", 1), "subject": "Same"}], qa_entries=[a, b])
    assert p["summary"]["total"] == 2 and by_source(p) == ["QA", "TEAMS + QA"]


def test_several_live_lectures_sharing_a_meeting_on_one_day_are_a_conflict_never_a_guess():
    found = [teams("AM", 1), teams("PM", 1)]
    p = preview(found=found, qa_entries=[qa(1)])
    assert p["summary"]["total"] == 2 and p["summary"]["qa_conflicts"] == 2 and p["summary"]["qa_only"] == 0
    assert all(r["qa"]["status"] == sources.CONFLICT and "QA" not in r["sources"] for r in p["lectures"])


def test_a_qa_record_matches_only_its_own_cairo_date():
    p = preview(found=[teams("T1", 1)], qa_entries=[qa(1, day="2026-09-02")])
    assert p["summary"]["qa_matched"] == 1
    attached, qa_only = sources.merge([qa(1, day="2026-09-03")], [registry("T1", 1)])
    assert attached == {} and len(qa_only) == 1                 # the series' next lecture is another lecture


# =========================================================================== QA-only outcomes, never eligible
def test_a_qa_only_lecture_with_an_lms_session_that_day_needs_an_operator_never_a_guessed_start():
    [row] = preview(qa_entries=[qa(3, lms_keys=lms_keys(3))])["lectures"]
    assert row["code"] == sources.SCHEDULE_UNKNOWN and row["category"] == backfill.REVIEW_REQUIRED
    assert row["scheduled_start"] is None and row["lms"]["status"] == "SESSION_ON_DATE"


def test_qa_only_when_the_lms_was_not_readable_says_so():
    [row] = preview(qa_entries=[qa(3, lms_keys=None)])["lectures"]
    assert row["code"] == sources.LMS_UNCHECKED and row["category"] == backfill.REVIEW_REQUIRED


def test_a_cancelled_qa_only_lecture_is_skipped():
    [row] = preview(qa_entries=[qa(3, cancelled=True)])["lectures"]
    assert row["category"] == backfill.SKIPPED and row["missing_evidence"] == []


def test_qa_only_lectures_are_never_eligible_never_items_but_bound_into_the_fingerprint():
    p = preview(qa_entries=[qa(1), qa(2, lms_keys=lms_keys(2))])
    assert p["summary"]["eligible"] == 0 and backfill.run_items(p) == [] and backfill.preview_items(p) == []
    assert len(p["summary"]["qa_only_lectures"]) == 2
    other = preview(qa_entries=[qa(1)])
    assert other["fingerprint"] != p["fingerprint"]


def test_nothing_identifying_is_kept_from_the_qa_record():
    mid = graph_meeting(1)
    p = preview(qa_entries=[qa(1, session="TRANSCRIPT-SECRET-ID")])
    text = repr(p)
    assert mid not in text and "TRANSCRIPT-SECRET-ID" not in text and "meeting_key" not in text


# =========================================================================== 25, 28: courses gone from Teams
def test_a_lecture_of_a_course_removed_from_the_teams_calendar_still_appears():
    # registry knows it (stored), Teams no longer lists it, QA has it: one row, PUBLISHER + QA, processed as stored
    p = preview([fact("OLD", recording="LOCATED")], qa_entries=[qa(5)], reg=[registry("OLD", 5)])
    [row] = p["lectures"]
    assert row["origin"] == backfill.STORED_ONLY and row["sources"] == ["PUBLISHER", "QA"]
    assert row["category"] == backfill.RESUME_REQUIRED


def test_an_empty_calendar_day_with_valid_qa_records_is_not_empty():
    p = preview(qa_entries=[qa(1), qa(2)])
    assert p["summary"]["per_day"][D]["teams"] == 0 and p["summary"]["per_day"][D]["outcome"] == "DISCOVERED"


def test_multiple_lectures_on_one_day_are_counted_per_day_without_double_counting():
    p = preview([fact("L1")], found=[teams("L1", 1), teams("T2", 2)], qa_entries=[qa(1), qa(2), qa(3)],
                reg=[registry("L1", 1)])
    day = p["summary"]["per_day"][D]
    assert (day["total"], day["teams"], day["existing"], day["qa_records"], day["qa_matched"], day["qa_only"]) == \
        (3, 2, 1, 3, 2, 1)


# =========================================================================== the coordinator's day: both sources
class _Conn:
    read_only = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


class _Calendar:
    def __init__(self, lectures=(), fail=False):
        self.lectures, self.fail = list(lectures), fail

    def preview_day(self, connection, day):
        if self.fail:
            raise RuntimeError("calendar outage")
        return {"calendar_events_found": len(self.lectures), "teams_events_found": len(self.lectures)}, self.lectures


class _Lms:
    def __init__(self, *meetings):
        start = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)
        self.snapshot = SimpleNamespace(occurrences=[
            SimpleNamespace(scheduled_start=start, status="scheduled",
                            meeting_keys=frozenset({meeting_key.from_online_meeting_id(graph_meeting(n))}))
            for n in meetings])

    def load(self, *window):
        return self.snapshot


def coordinator(monkeypatch, *, calendar, records=None, qa_fails=False, lms=None):
    def load_records(connection, date_from, date_to):
        if qa_fails:
            raise RuntimeError("qa database outage")
        return records or []
    monkeypatch.setattr(sources, "load_records", load_records)
    monkeypatch.setattr("app.lms.sources.load_lectures", lambda *a, **k: [])
    config = SimpleNamespace(test_programmes="", backfill_discovery_days=2)
    planner = SimpleNamespace(calendar=calendar, lms_source=lms)
    return HistoricalBackfill(config=config, connect=_Conn, planner=planner, queue=object(), repository=object())


def raw(n, day=DAY):
    mid = graph_meeting(n)
    return {"qa_ref": sources.ref(f"t{n}"), "meeting_ref": sources.ref(mid),
            "meeting_key": meeting_key.from_online_meeting_id(mid), "session_date": day.isoformat(),
            "subject": f"Lecture {n}", "module": None, "cancelled": False, "transcript_reference": False,
            "recording_reference": False}


def test_the_coordinator_reads_qa_even_when_teams_fails_and_records_a_partial_day(monkeypatch):
    hb = coordinator(monkeypatch, calendar=_Calendar(fail=True), records=[raw(1), raw(2)], lms=_Lms(2))
    day = hb._preview_day(DAY, 1)
    assert day["status"] == backfill.PARTIAL and day["teams"]["status"] == "FAILED"
    assert day["qa"]["status"] == "COMPLETE" and day["qa"]["records"] == 2
    assert [e["lms_session_on_day"] for e in day["qa"]["entries"]] == [False, True]   # LMS still read for QA
    assert all("meeting_key" not in e for e in day["qa"]["entries"])


def test_the_coordinator_reports_a_qa_failure_as_partial_not_as_zero_records(monkeypatch):
    hb = coordinator(monkeypatch, calendar=_Calendar(), qa_fails=True)
    day = hb._preview_day(DAY, 1)
    assert day["status"] == backfill.PARTIAL and day["qa"]["status"] == "FAILED" and "entries" not in day["qa"]


def test_the_coordinator_marks_a_day_complete_only_when_both_answer_even_with_zero(monkeypatch):
    hb = coordinator(monkeypatch, calendar=_Calendar(), records=[])
    day = hb._preview_day(DAY, 1)
    assert day["status"] == backfill.COMPLETE and day["qa"]["records"] == 0 and day["teams"]["lectures_found"] == 0


def test_a_partial_day_is_retried_like_a_failed_one():
    run = {"date_from": DAY, "date_to": DAY}
    hb = HistoricalBackfill(config=SimpleNamespace(), connect=_Conn, planner=None, queue=object(), repository=object())
    assert hb._todo(run, {D: {"status": backfill.PARTIAL, "attempts": 1}}) == [D]
    assert hb._todo(run, {D: {"status": backfill.PARTIAL, "attempts": backfill.DAY_ATTEMPTS}}) == []


def test_the_display_coverage_drops_the_per_lecture_lists():
    day = backfill._day({"status": "COMPLETE", "lectures": [{"lecture_id": "x"}], "teams_events": 3},
                        {"status": "COMPLETE", "records": 1, "entries": [{"qa_ref": "r"}]}, 1)
    view = backfill.coverage_view(day)
    assert view["teams"] == {"status": "COMPLETE", "teams_events": 3} and view["qa"] == {"status": "COMPLETE", "records": 1}
    assert "lectures" not in view and view["teams_events"] == 3


# =========================================================================== 26, 29: dates and pagination
@pytest.mark.parametrize("instant, expected", [
    (datetime(2026, 4, 23, 21, 30, tzinfo=timezone.utc), date(2026, 4, 23)),   # the night DST starts: still UTC+2
    (datetime(2026, 4, 24, 21, 30, tzinfo=timezone.utc), date(2026, 4, 25)),   # the next night: UTC+3
    (datetime(2026, 10, 29, 21, 30, tzinfo=timezone.utc), date(2026, 10, 29)),  # after DST ended (UTC+2)
    (datetime(2026, 9, 1, 20, 59, tzinfo=timezone.utc), date(2026, 9, 1)),
    (datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc), date(2026, 9, 2)),       # Cairo midnight in summer = 21:00 UTC
])
def test_lms_sessions_count_on_their_cairo_date_across_dst_and_midnight(instant, expected):
    snap = SimpleNamespace(occurrences=[SimpleNamespace(scheduled_start=instant, status="scheduled",
                                                        meeting_keys=frozenset({"k"}))])
    days = [expected - timedelta(days=1), expected, expected + timedelta(days=1)]
    keys = sources.lms_keys_by_day(snap, days)
    assert keys[expected.isoformat()] == frozenset({"k"})
    assert all(not keys[d.isoformat()] for d in days if d != expected)


def test_cancelled_lms_sessions_never_count_as_a_session_that_day():
    snap = SimpleNamespace(occurrences=[SimpleNamespace(scheduled_start=datetime(2026, 9, 2, 8, tzinfo=timezone.utc),
                                                        status="cancelled", meeting_keys=frozenset({"k"}))])
    assert sources.lms_keys_by_day(snap, [DAY])[D] == frozenset()


def test_the_qa_source_reads_every_row_of_the_range_in_one_query():
    rows = [("s%d" % i, graph_meeting(i % 3), DAY, "S", None, "false", False, True) for i in range(250)]

    class Conn:
        def execute(self, sql, params):
            assert "FROM public.qa_doctors_sessions" in sql and params == (DAY, DAY) and "LIMIT" not in sql.upper()
            return SimpleNamespace(fetchall=lambda: rows)
    records = sources.load_records(Conn(), DAY, DAY)
    assert len(records) == 250 and len({r["qa_ref"] for r in records}) == 250
    assert records[0]["cancelled"] is False and records[0]["recording_reference"] is True


def test_the_qa_source_never_writes():
    import inspect
    text = inspect.getsource(sources).upper()
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "ALTER ", "TRUNCATE "):
        assert verb not in text.replace("NEVER WRITTEN", "")
