"""
Lecture Publishing Monitor: classification and KPI definitions (no database).

`classify()` reads the delivery rule's answer (a lecture_delivery()-shaped view) - these tests build
that shape directly; tests/integration/test_publishing_monitor.py runs the real rule end to end.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.publisher import monitor

NOW = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
START = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)
LIBRARY = 786045


def fact(**over):
    base = {"lecture_id": "11111111-1111-1111-1111-111111111111", "session_date": date(2026, 10, 25),
            "scheduled_start": START, "scheduled_end": START + timedelta(hours=2), "subject": "Data Analysis",
            "module": "L4 Data", "cancelled": False, "suppressed": False, "match_status": "VERIFIED",
            "match_review": False, "occurrence_id": "OCC-1", "link_current": True, "recording_status": "LOCATED",
            "recording_reason": "LOCATED", "recording_review_reason": None, "expected_parts": 1,
            "located_parts": 1, "next_check_after": None, "selection_status": None, "jobs": [],
            "videos": [{"part": 1, "import": "IDENTIFIED", "encoding": "VERIFIED", "visibility": "READY_FOR_LMS"}],
            "updated_at": NOW}
    base.update(over)
    return base


def view(*, state="READY", release="RELEASED", caption_status="NOT_AVAILABLE", encoding="VERIFIED",
         visibility="READY_FOR_LMS", captions=(), blocking=()):
    return {"state": state, "release": release, "caption_status": caption_status, "blocking": list(blocking),
            "released_at": "2026-10-25T12:00:00+00:00" if release == "RELEASED" else None, "withdrawn_at": None,
            "parts": [{"encoding": encoding, "visibility": visibility, "captions": list(captions),
                       "bunny": {"library_id": LIBRARY, "video_id": "a1a8fc1d-0000-0000-0000-000000000000"}}],
            "excluded_parts": []}


def row(f, v=None, enabled=True, now=NOW):
    return monitor.classify(f, v, playback_enabled=enabled, library_id=LIBRARY, now=now)


def job(kind, status, **extra):
    return {"kind": kind, "status": status, "error": extra.get("error"), "attempts": 1, "max_attempts": 8,
            "next_attempt_at": extra.get("next_attempt_at")}


# =========================================================================== captions are optional
def test_a_playable_video_with_no_transcript_is_playable_not_failed():
    r = row(fact(jobs=[job("ACQUIRE_TRANSCRIPT", "REVIEW_REQUIRED", error="DEADLINE_PASSED")]), view())
    assert r["video"]["status"] == "VERIFIED" and r["bunny"]["status"] == "UPLOADED"
    assert r["playback"]["available"] is True and r["release"]["status"] == "RELEASED"
    assert r["transcript"]["status"] == "MISSING" and r["captions"]["status"] == "NOT_AVAILABLE"
    assert r["state"] == monitor.PLAYABLE_NO_CAPTIONS and r["needs_attention"] is False


def test_a_failed_caption_job_never_makes_the_lecture_need_attention():
    r = row(fact(selection_status="SELECTED", jobs=[job("PUBLISH_CAPTION", "REVIEW_REQUIRED", error="X")]), view())
    assert r["transcript"]["status"] == "AVAILABLE" and r["captions"]["status"] == "NOT_AVAILABLE"
    assert r["state"] == monitor.PLAYABLE_NO_CAPTIONS and not r["needs_attention"]


def test_verified_captions_make_it_fully_playable():
    r = row(fact(selection_status="SELECTED"), view(caption_status="VERIFIED"))
    assert r["state"] == monitor.PLAYABLE and r["captions"]["status"] == "VERIFIED"


def test_a_caption_on_its_way_and_an_unverified_one_are_reported_as_such():
    pending = row(fact(jobs=[job("PUBLISH_CAPTION", "RETRY_WAIT", next_attempt_at="2026-10-25T19:00:00+00:00")]),
                  view())
    assert pending["captions"]["status"] == "PENDING" and pending["state"] == monitor.PLAYABLE_NO_CAPTIONS
    unverified = row(fact(), view(captions=[{"language": "en", "status": "VERIFIED", "content_verified": False}]))
    assert unverified["captions"]["status"] == "UNVERIFIED"


# =========================================================================== release and playback
def test_verified_but_not_released_waits_for_automatic_release():
    r = row(fact(videos=[{"part": 1, "import": "IDENTIFIED", "encoding": "VERIFIED", "visibility": "HIDDEN"}]),
            view(release="NOT_RELEASED", visibility="HIDDEN"))
    assert r["state"] == monitor.READY_TO_RELEASE and r["automatic"] and not r["playback"]["available"]


def test_released_with_playback_switched_off_is_not_reported_as_a_lecture_problem():
    r = row(fact(), view(), enabled=False)
    assert r["state"] == monitor.RELEASED_PLAYBACK_OFF and not r["needs_attention"]
    assert r["playback"] == {"available": False, "reason": "PLAYBACK_DISABLED"}


def test_released_but_no_longer_ready_needs_attention():
    r = row(fact(link_current=False), view(state="REVIEW_REQUIRED", blocking=["LMS_LINK_NOT_CURRENT"]))
    assert r["state"] == monitor.RELEASED_NOT_PLAYABLE and r["needs_attention"]
    assert "LMS_LINK_NOT_CURRENT" in r["reason"]


def test_withdrawn_is_its_own_state():
    r = row(fact(videos=[{"part": 1, "import": "IDENTIFIED", "encoding": "VERIFIED", "visibility": "WITHDRAWN"}]),
            view(release="WITHDRAWN", state="WITHDRAWN", visibility="WITHDRAWN"))
    assert r["state"] == monitor.WITHDRAWN and not r["playback"]["available"]


# =========================================================================== waiting is not failure
def test_no_recording_yet_is_waiting_never_a_failure():
    r = row(fact(recording_status="WAITING", recording_reason="NO_RECORDING_YET", videos=[],
                 next_check_after=NOW + timedelta(minutes=15), jobs=[job("RESOLVE_RECORDING", "RETRY_WAIT")]))
    assert r["state"] in (monitor.WAITING_FOR_RECORDING, monitor.RETRY_SCHEDULED)
    assert r["automatic"] and not r["needs_attention"]
    assert r["bunny"]["status"] == "NOT_IMPORTED" and r["playback"]["reason"] == "NO_VIDEO"


def test_waiting_with_no_job_retry_is_waiting_for_recording_with_the_next_check():
    r = row(fact(recording_status="WAITING", videos=[], next_check_after=NOW + timedelta(minutes=15)))
    assert r["state"] == monitor.WAITING_FOR_RECORDING and r["retry_at"]


def test_a_lecture_that_has_not_ended_is_scheduled():
    r = row(fact(recording_status=None, videos=[], match_status=None), now=START - timedelta(hours=1))
    assert r["state"] == monitor.SCHEDULED and not r["needs_attention"]


def test_no_publisher_evidence_is_not_tracked_and_never_counted_as_a_missing_recording():
    r = row(fact(recording_status=None, videos=[], match_status=None))
    assert r["state"] == monitor.NOT_TRACKED and not r["needs_attention"]
    assert not monitor.KPIS["missing"][2](r) and monitor.apply_filters([r], kpi="missing") == []


def test_a_retry_is_reported_with_its_time():
    r = row(fact(recording_status="RETRY_SCHEDULED", videos=[], next_check_after=NOW + timedelta(minutes=5)))
    assert r["state"] == monitor.RETRY_SCHEDULED and r["retry_at"] == (NOW + timedelta(minutes=5)).isoformat()


def test_encoding_in_progress_is_processing():
    r = row(fact(videos=[{"part": 1, "import": "IDENTIFIED", "encoding": "IN_PROGRESS", "visibility": "HIDDEN"}]),
            view(state="PROCESSING", release="NOT_RELEASED", encoding="IN_PROGRESS", visibility="HIDDEN"))
    assert r["state"] == monitor.PROCESSING and r["video"]["status"] == "IN_PROGRESS" and r["automatic"]


# =========================================================================== attention
@pytest.mark.parametrize("over,v,expected", [
    ({"match_status": "AMBIGUOUS", "match_review": True, "videos": []}, None, monitor.BLOCKED_IDENTITY),
    ({"videos": [{"part": 1, "import": "IDENTIFIED", "encoding": "FAILED", "visibility": "HIDDEN"}]},
     view(state="PROCESSING", release="NOT_RELEASED", encoding="FAILED", visibility="HIDDEN"), monitor.VIDEO_FAILED),
    ({"recording_status": "REVIEW_REQUIRED", "recording_review_reason": "RECORDING_SET_CHANGED", "videos": []},
     None, monitor.REVIEW_REQUIRED),
    ({"videos": [{"part": 1, "import": "IDENTIFIED", "encoding": "VERIFIED", "visibility": "HIDDEN"}]},
     view(state="REVIEW_REQUIRED", release="NOT_RELEASED", visibility="HIDDEN",
          blocking=["PART_2_FRAGMENT_NEEDS_DECISION"]), monitor.BLOCKED_IDENTITY),
])
def test_operator_states_need_attention(over, v, expected):
    r = row(fact(**over), v)
    assert r["state"] == expected and r["needs_attention"] and not r["automatic"]


@pytest.mark.parametrize("over", [{"cancelled": True}, {"suppressed": True}, {"match_status": "TEST_EXCLUDED"}])
def test_not_applicable_lectures_are_neither_failures_nor_missing(over):
    r = row(fact(videos=[], recording_status="WAITING", **over))
    assert r["state"] == monitor.NOT_APPLICABLE and not r["needs_attention"]
    assert not monitor.KPIS["missing"][2](r)


# =========================================================================== KPIs agree with the table
def test_every_kpi_count_is_exactly_its_filter():
    rows = [row(fact(selection_status="SELECTED"), view(caption_status="VERIFIED")),
            row(fact(), view()),
            row(fact(), view(), enabled=False),
            row(fact(recording_status="WAITING", videos=[])),
            row(fact(match_status="AMBIGUOUS", match_review=True, videos=[])),
            row(fact(cancelled=True, videos=[])),
            row(fact(videos=[{"part": 1, "import": "SUBMITTED", "encoding": "NOT_STARTED", "visibility": "HIDDEN"}]),
                view(state="PROCESSING", release="NOT_RELEASED", encoding="NOT_IMPORTED", visibility="HIDDEN"))]
    summary = monitor.summarize(rows)
    counts = {k["key"]: k["count"] for k in summary["kpis"]}
    for key in monitor.KPIS:
        assert counts[key] == len(monitor.apply_filters(rows, kpi=key)), key
    assert counts == {"discovered": 7, "located": 6, "uploaded": 3, "verified": 3, "released": 3, "playable": 2,
                      "missing": 1, "attention": 1}           # the AMBIGUOUS lecture's recording is located
    assert summary["playable_without_captions"] == 1 and sum(summary["states"].values()) == 7


def test_filters_search_and_attention():
    rows = [row(fact(subject="Data Analysis"), view()),
            row(fact(lecture_id="2", subject="Leadership", match_status="CONFLICT", match_review=True, videos=[]))]
    assert [r["subject"] for r in monitor.apply_filters(rows, q="lead")] == ["Leadership"]
    assert [r["subject"] for r in monitor.apply_filters(rows, attention=True)] == ["Leadership"]
    assert [r["subject"] for r in monitor.apply_filters(rows, playback="AVAILABLE")] == ["Data Analysis"]
    assert [r["subject"] for r in monitor.apply_filters(rows, video="NOT_IMPORTED")] == ["Leadership"]
    assert [r["subject"] for r in monitor.apply_filters(rows, transcript="NOT_STARTED")] == [
        "Data Analysis", "Leadership"]
