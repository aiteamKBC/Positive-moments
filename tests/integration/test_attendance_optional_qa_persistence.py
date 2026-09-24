"""
RELEASE GATE: attendance-optional QA against real PostgreSQL.

The 2026-09-23 business day, on synthetic production-shaped evidence:

  1-4  Juliane, Keith Strategy, Steve EVM, G2 Keith CI - authoritative
       attendance; QA, render and qa_doctors_sessions exactly as before.
  5    Ray | PMP - generation 1 is INVALID_EVIDENCE (a real QA failure, and
       it BLOCKS: no render, no legacy row); a successful regeneration then
       renders and syncs normally.
  6    Andrew - the attendance source never answered. QA still runs from the
       transcript, renders, and reaches qa_doctors_sessions; the attendance
       values stay NULL (UNKNOWN != ZERO), Item 7 is the model's, and Perfect
       stays PENDING_ATTENDANCE_DATA with no qa_perfect_lectures row. When
       attendance arrives later: the stored model answer is reused (zero
       provider calls), Item 7 moves deterministically, and the SAME coded-owned
       row is updated in place - its foreign-owned columns untouched.

Everything below the resolver is real: the QA service and its repositories,
the renderer, the two-pass automated sync with its sync-safety gate, the
writer, the Perfect planner. Only the model provider is a stub. Every
connection is rolled back.
"""
from __future__ import annotations

import psycopg
import pytest

from app.config.settings import Settings
from app.orchestration.runner import (
    ManualReviewRequired,
    StageNotAutomatable,
    StageRunner,
)
from app.qa.checklist import CHECKLIST_ITEMS
from app.writer.service import DETERMINISTIC_REFRESH_UPDATE_POLICY
from tests.integration.seeding import (
    DAY,
    TRAINER,
    graph_id,
    insert,
    seed_attendance_snapshot,
    seed_engagement,
    seed_lecture,
    seed_transcript_document,
)


@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    connection = psycopg.connect(url)
    try:
        assert "test" in connection.execute("SELECT current_database()").fetchone()[0]
        yield connection
    finally:
        connection.rollback()
        connection.close()


class Provider:
    """A model that answers every item Met, or fabricates one clip."""

    def __init__(self, *, fabricate=False):
        self.calls = 0
        self.fabricate = fabricate

    def complete_json(self, *, system_message, user_message):
        self.calls += 1
        rows = [{"item": item, "status": "Met", "evidence_clips": []}
                for item in CHECKLIST_ITEMS]
        if self.fabricate:
            # A timestamp the transcript never reaches: evidence that does not
            # exist, which is a real QA failure.
            rows[3]["evidence_clips"] = [{"start": "09:59:00.000", "end": "09:59:30.000"}]
        return {"output": {
            "session_info": {"trainer": TRAINER, "date": DAY.isoformat()},
            "checklist_evaluation": rows,
            "overall_summary": {"strengths": [], "areas_for_improvement": [],
                                "overall_judgement": "Solid session."},
            "ksbs_covered": [],
            "teaching_quality": {"rating_1_5": 4, "comments": "Clear.",
                                 "evidence_clips": []},
        }, "provider": "stub", "model_requested": "stub", "model_reported": "stub",
            "response_id": "stub-1", "usage": {}, "attempts": 1}


LECTURES = (
    ("Juliane - Impact and Planning June 2026", "authoritative"),
    ("Keith | Strategy & Planning - June 2026", "authoritative"),
    ("Steve-Earned Value Management(EVM)2026", "authoritative"),
    ("G2 Keith-Commercial Intelligence-Oct 25", "authoritative"),
    ("Ray | PMP - June 2026", "invalid_evidence_first"),
    ("Andrew-Scheduling Professional (SP) Jan 2026", "attendance_missing"),
)


def _runner():
    return StageRunner(settings=Settings.from_environment())


# A two-hour lecture whose transcript spans the whole slot, so punctuality
# (Item 2) is Met and a clean answer is all-Met - the case where Perfect has
# to say PENDING_ATTENDANCE_DATA rather than NOT_ELIGIBLE.
FULL_LENGTH_CUES = [(minute * 60_000, minute * 60_000 + 30_000,
                     TRAINER if minute % 3 else f"Synthetic Learner {minute}",
                     f"Synthetic line {minute}.")
                    for minute in range(0, 120, 20)] + [
                    (7_170_000, 7_200_000, TRAINER, "Synthetic close.")]


def _seed(db, index, subject, shape):
    missing = shape == "attendance_missing"
    lecture = seed_lecture(db, transcript_id=graph_id(700 + index),
                           meeting_id=f"MTG-0923-{index}", subject=subject)
    document = seed_transcript_document(db, lecture, cues=FULL_LENGTH_CUES)
    members = 0 if missing else 7
    snapshot = seed_attendance_snapshot(db, lecture, source_rows=members,
                                        present_rows=members, members=members)
    engagement = seed_engagement(db, lecture, document, snapshot, attended=members)
    lecture = {**lecture, **document, "snapshot_id": snapshot,
               "engagement_id": engagement}
    if missing:
        # Exactly what Phase 2C4 writes for an empty snapshot: legacy's
        # encoding of "nobody attended". It must never reach the QA result.
        db.execute("UPDATE public.lecture_engagement_metrics SET engagement_score = 1,"
                   " engagement_percentage = 0.00 WHERE engagement_id = %s",
                   (lecture["engagement_id"],))
    for student in range(2):
        insert(db, "kbc_users_data", ID=str(900000 + index * 10 + student),
               FullName=f"Synthetic Student {index}-{student}", Group=subject,
               **{"Program-Status": "Active"})
    return lecture


def _qa(db, lecture, provider):
    service = _runner()._qa_service(provider=provider,
                                    lecture_ids=[str(lecture["lecture_id"])])
    (result,) = service.run_day(db, DAY, execute=True)["lectures"]
    return result


def _render_and_sync(db, lecture):
    """The real render, then the real two-pass automated sync."""
    runner = _runner()
    runner._render_qa(db, lecture["lecture_id"], DAY)
    result = runner._sync_legacy_qa(db, lecture["lecture_id"], DAY)
    # A write is reported through `_outcome`, which nests the sync's own
    # account under `summary`; a NOOP is returned as that account directly.
    return result["summary"] if "qa_decision" not in result else result


def _one(db, sql, *params):
    return db.execute(sql, params).fetchone()


def _session(db, lecture):
    return db.execute(
        'SELECT session_id, "Engagement", engagement_score, met_count, partial_count,'
        '       not_met_count FROM public.qa_doctors_sessions WHERE meeting_id = %s',
        (lecture["meeting_id"],)).fetchall()


def _item7(db, session_id):
    return _one(db, "SELECT status, evidence FROM public.qa_doctors_checklist_items"
                    " WHERE session_id = %s AND checklist_order = 7", session_id)


def _checklist(db, session_id):
    return {row[0]: row[1:] for row in db.execute(
        "SELECT checklist_order, status, evidence FROM public.qa_doctors_checklist_items"
        " WHERE session_id = %s", (session_id,)).fetchall()}


def _perfect_rows(db, lecture):
    return _one(db, "SELECT count(*) FROM public.qa_perfect_lectures p"
                    " JOIN public.lecture_perfect_lecture_legacy_writes w"
                    "   ON w.legacy_lecture_key = p.lecture_key"
                    " WHERE w.lecture_id = %s", lecture["lecture_id"])[0]


def test_19_the_2026_09_23_day_reaches_qa_doctors_sessions_for_all_six(db):
    lectures = {subject: (_seed(db, index, subject, shape), shape)
                for index, (subject, shape) in enumerate(LECTURES)}
    provider = Provider()

    # --- 1-4: authoritative attendance, unchanged behaviour ------------------
    for subject, (lecture, shape) in lectures.items():
        if shape != "authoritative":
            continue
        result = _qa(db, lecture, provider)
        assert result["qa_status"] == "COMPLETED", subject
        assert result["attendance_flag"] is None
        outcome = _render_and_sync(db, lecture)
        assert outcome["qa_decision"] == "WOULD_INSERT", subject
        (row,) = _session(db, lecture)
        assert row[1] is not None and row[2] is not None      # real engagement

    # --- 5: Ray - INVALID_EVIDENCE is a real failure and it blocks ------------
    ray, _ = lectures["Ray | PMP - June 2026"]
    first = _qa(db, ray, Provider(fabricate=True))
    assert first["qa_status"] == "INVALID_EVIDENCE"
    assert first["attendance_flag"] is None          # a QA failure, not attendance
    render = _runner()._render_qa(db, ray["lecture_id"], DAY)["summary"]
    assert [item["render_status"] for item in render["lectures"]] == [
        "SOURCE_QA_NOT_READY"]
    with pytest.raises(StageNotAutomatable, match="got 0"):
        _runner()._sync_legacy_qa(db, ray["lecture_id"], DAY)   # nothing publishable
    assert _session(db, ray) == []
    regenerated = _qa(db, ray, provider)
    assert regenerated["qa_status"] == "COMPLETED"
    assert _render_and_sync(db, ray)["qa_decision"] == "WOULD_INSERT"
    assert len(_session(db, ray)) == 1

    # --- 6: Andrew - no attendance, QA still runs ------------------------------
    andrew, _ = lectures["Andrew-Scheduling Professional (SP) Jan 2026"]
    calls_before = provider.calls
    result = _qa(db, andrew, provider)
    assert provider.calls == calls_before + 1
    assert result["qa_status"] == "COMPLETED"
    assert result["attendance_coverage_status"] == "SOURCE_MISSING"
    assert result["attendance_flag"] == "PENDING_ATTENDANCE"
    evaluation = _one(db, "SELECT attended_count, spoke_count, engagement_percentage,"
                          " engagement_score, item7_override_applied,"
                          " ai_item7_status, final_item7_status,"
                          " metadata ->> 'attendance_flag'"
                          " FROM public.lecture_qa_evaluations"
                          " WHERE lecture_id = %s", andrew["lecture_id"])
    assert evaluation[:4] == (None, None, None, None)          # UNKNOWN != ZERO
    assert evaluation[4] is False                              # no override
    assert evaluation[5] == evaluation[6] == "Met"             # the model's Item 7
    assert evaluation[7] == "PENDING_ATTENDANCE"

    outcome = _render_and_sync(db, andrew)
    assert outcome["qa_decision"] == "WOULD_INSERT"
    # All eleven Met: without attendance, Perfect is withheld - not decided.
    assert outcome["perfect_decision"] == "PERFECT_PENDING_ATTENDANCE_DATA"
    assert outcome["auto_sync"]["perfect"]["action"] == "WAITING"
    render = _one(db, "SELECT engagement, engagement_score, attended_count, render_status"
                      " FROM public.lecture_qa_rendered_sessions WHERE lecture_id = %s",
                  andrew["lecture_id"])
    assert render == (None, None, None, "RENDERED")
    (session,) = _session(db, andrew)
    session_id = session[0]
    assert session_id == andrew["transcript_id"]
    assert session[1] is None and session[2] is None           # nothing fabricated
    assert _one(db, "SELECT count(*) FROM public.qa_doctors_checklist_items"
                    " WHERE session_id = %s", session_id)[0] == 11
    assert _item7(db, session_id)[0] == "Met"
    assert _perfect_rows(db, andrew) == 0
    perfect = _one(db, "SELECT reason, eligibility_version"
                       " FROM public.lecture_perfect_lecture_results"
                       " WHERE lecture_id = %s ORDER BY computed_at DESC LIMIT 1",
                   andrew["lecture_id"])
    assert perfect == ("PENDING_ATTENDANCE_DATA", "kbc_perfect_v2_attendance_required")

    # --- the day, as the acceptance case states it ----------------------------
    synced = db.execute("SELECT count(*) FROM public.qa_doctors_sessions"
                        " WHERE meeting_id LIKE 'MTG-0923-%%'").fetchone()[0]
    assert synced == 6
    owners = db.execute(
        "SELECT write_status, count(*) FROM public.lecture_qa_legacy_writes"
        " WHERE lecture_id = ANY(%s) GROUP BY 1",
        ([lecture["lecture_id"] for lecture, _ in lectures.values()],)).fetchall()
    assert owners == [("WRITTEN", 6)]
    assert provider.calls == 4 + 1 + 1          # 1-4, Ray's regeneration, Andrew


FOREIGN = {"recording_url": "https://recording.invalid/keep",
           "recording_link_status": "LINKED", "recording_item_id": "item-keep",
           "recording_drive_id": "drive-keep", "recording_id": "rec-keep",
           "recap_url": "https://recap.invalid/keep",
           "transcript_url": "https://transcript.invalid/keep",
           "positive_clips": '{"clips": [1, 2]}', "clips_status": "done",
           "clips_count": 2, "clips_analysis_completeness": "COMPLETE"}


def _foreign(db, session_id):
    return _one(db, "SELECT " + ", ".join(FOREIGN) + " FROM public.qa_doctors_sessions"
                    " WHERE session_id = %s", session_id)


def _published_then_attendance_arrives(db, index):
    """
    Andrew's journey up to the moment the late answer is ready to publish:
    published without attendance, another workflow fills its own columns,
    attendance arrives (new authoritative snapshot + its engagement row, as
    RESOLVE_ATTENDANCE and CALCULATE_ENGAGEMENT persist them), and the
    deterministic refresh runs.
    """
    andrew = _seed(db, index, "Andrew-Scheduling Professional (SP) Jan 2026",
                   "attendance_missing")
    provider = Provider()
    _qa(db, andrew, provider)
    _render_and_sync(db, andrew)
    ((session_id, *_),) = _session(db, andrew)

    db.execute("UPDATE public.qa_doctors_sessions SET " + ", ".join(
        f"{column} = %s" + ("::jsonb" if column == "positive_clips" else "")
        for column in FOREIGN) + " WHERE session_id = %s",
        (*FOREIGN.values(), session_id))

    # 3 attended, 1 spoke, Item 7 Not Met. Later in time than the first
    # snapshot, as it would be in production (a later transaction).
    document = {"document_id": andrew["document_id"], "speaker_id": andrew["speaker_id"]}
    snapshot = seed_attendance_snapshot(db, andrew, source_rows=3, present_rows=3,
                                        members=3, label="late")
    db.execute("UPDATE public.lecture_attendance_snapshots"
               " SET created_at = created_at + interval '1 hour' WHERE snapshot_id = %s",
               (snapshot,))
    engagement = seed_engagement(db, andrew, document, snapshot, attended=3, spoke=1)
    db.execute("UPDATE public.lecture_engagement_metrics SET engagement_score = 2,"
               " learner_engagement_status = 'Not Met' WHERE engagement_id = %s",
               (engagement,))

    calls_before = provider.calls
    refresh = _runner()._refresh_deterministic_qa(db, andrew["lecture_id"], DAY)["summary"]
    assert provider.calls == calls_before
    db.execute("UPDATE public.lecture_qa_evaluations"
               " SET updated_at = updated_at + interval '1 hour' WHERE evaluation_id = %s",
               (refresh["evaluation_id"],))
    return andrew, session_id, provider, refresh


def test_10_to_15_late_attendance_updates_the_same_row_without_a_model_call(db):
    andrew, session_id, provider, refresh = _published_then_attendance_arrives(db, 50)
    foreign_before = _foreign(db, session_id)
    ((_, _, _, met, partial, not_met),) = _session(db, andrew)
    published = _checklist(db, session_id)
    calls_before = provider.calls

    # --- 11/12: REFRESH_DETERMINISTIC_QA - never a provider call ---------------
    assert refresh["refresh_status"] == "QA_DETERMINISTIC_REFRESHED"
    assert refresh["provider_calls"] == 0 and refresh["ai_called"] is False
    refreshed = _one(db, "SELECT attended_count, engagement_score, item7_override_applied,"
                         " final_item7_status, metadata ->> 'attendance_flag'"
                         " FROM public.lecture_qa_evaluations WHERE evaluation_id = %s",
                     refresh["evaluation_id"])
    assert refreshed == (3, 2, True, "Not Met", None)          # 10/13

    # --- 14/15: the same coded-owned row, updated in place ---------------------
    outcome = _render_and_sync(db, andrew)
    assert outcome["qa_decision"] == "WOULD_UPDATE"
    assert outcome["auto_sync"]["qa"]["update_kind"] == "DETERMINISTIC_REFRESH_UPDATE"
    update = outcome["summary"]["deterministic_refresh_update"]
    assert update["eligible"] is True, update
    assert update["policy_version"] == DETERMINISTIC_REFRESH_UPDATE_POLICY
    assert update["changed_checklist_orders"] == [7]
    assert set(update["changed_session_columns"]) <= {
        "Engagement", "engagement_score", "met_count", "partial_count", "not_met_count"}

    rows = _session(db, andrew)
    assert len(rows) == 1 and rows[0][0] == session_id          # no duplicate
    assert rows[0][2] == 2 and rows[0][1] is not None           # real engagement now
    # The model said Met; the deterministic override now says Not Met.
    assert published[7][0] == "Met"
    assert _item7(db, session_id)[0] == "Not Met"
    assert rows[0][3:] == (met - 1, partial, not_met + 1)
    # Every item other than 7 - status AND evidence - is the published analysis.
    current = _checklist(db, session_id)
    assert {order: row for order, row in current.items() if order != 7} ==         {order: row for order, row in published.items() if order != 7}
    assert _foreign(db, session_id) == foreign_before
    owner = db.execute("SELECT write_status FROM public.lecture_qa_legacy_writes"
                       " WHERE lecture_id = %s", (andrew["lecture_id"],)).fetchall()
    assert owner == [("UPDATED",)]
    assert _perfect_rows(db, andrew) == 0          # Item 7 Not Met: not Perfect
    assert provider.calls == calls_before          # the whole refresh bought nothing


def test_the_automated_update_is_refused_unless_the_model_answer_is_unchanged(db):
    """The one automated update is ONLY the deterministic refresh of our own
    published answer. Anything else on a published row stays a human's call."""
    andrew, session_id, _provider, refresh = _published_then_attendance_arrives(db, 55)
    db.execute("UPDATE public.lecture_qa_evaluations SET ai_raw_output ="
               " jsonb_set(ai_raw_output, '{overall_summary,overall_judgement}',"
               "           '\"A different answer.\"') WHERE evaluation_id = %s",
               (refresh["evaluation_id"],))
    before = _session(db, andrew), _foreign(db, session_id)
    with pytest.raises(ManualReviewRequired, match="LEGACY_ROW_WOULD_BE_UPDATED"):
        _render_and_sync(db, andrew)
    assert (_session(db, andrew), _foreign(db, session_id)) == before


def test_the_writer_itself_refuses_an_unproven_update_in_the_automated_mode(db):
    """Defence in depth: even if the gate were bypassed, the automated writer
    re-checks the proof at write time and refuses."""
    from app.writer.modes import PRODUCTION_NEW_ONLY, REVIEW_REQUIRED
    from app.writer.service import DETERMINISTIC_REFRESH_ONLY

    andrew, session_id, _provider, refresh = _published_then_attendance_arrives(db, 57)
    db.execute("UPDATE public.lecture_qa_evaluations SET metadata = metadata - "
               "'deterministic_refresh' WHERE evaluation_id = %s",
               (refresh["evaluation_id"],))
    runner = _runner()
    runner._render_qa(db, andrew["lecture_id"], DAY)
    before = _session(db, andrew)
    plan = runner._writer(andrew["lecture_id"], mode=PRODUCTION_NEW_ONLY, confirmed=True,
                          update_policy=DETERMINISTIC_REFRESH_ONLY).plan_day(db, DAY)
    (row,) = plan["lectures"]
    assert row["decision"] == REVIEW_REQUIRED
    assert "MODEL_ANSWER_NOT_PROVEN_UNCHANGED" in \
        row["deterministic_refresh_update"]["refusal_reasons"]
    assert _session(db, andrew) == before


def test_18_a_foreign_row_for_an_attendance_pending_lecture_stays_protected(db):
    lecture = _seed(db, 70, "Andrew-Scheduling Professional (SP) Jan 2026",
                    "attendance_missing")
    insert(db, "qa_doctors_sessions", session_id=lecture["transcript_id"],
           meeting_id=lecture["meeting_id"], trainer="n8n", subject="n8n row",
           date=DAY)
    before = _one(db, "SELECT md5(q::text) FROM public.qa_doctors_sessions q"
                      " WHERE session_id = %s", lecture["transcript_id"])
    _qa(db, lecture, Provider())
    with pytest.raises(ManualReviewRequired, match="LEGACY_ROW_NOT_CODED_OWNED"):
        _render_and_sync(db, lecture)
    assert _one(db, "SELECT md5(q::text) FROM public.qa_doctors_sessions q"
                    " WHERE session_id = %s", lecture["transcript_id"]) == before
