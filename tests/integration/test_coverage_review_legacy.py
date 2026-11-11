"""
RELEASE GATE: a coverage review withdraws the coded platform's own false
legacy verdict - and nothing else - against real PostgreSQL.

The shape is AI in Project Control 2026-09-04, on synthetic evidence: the
coded writer published a NON_DELIVERED ("cancelled") row, and the canonical
evaluation has since become REVIEW_REQUIRED / TRANSCRIPT_COVERAGE_INCOMPLETE.
qa_doctors_sessions cannot say "ran, but not assessable", so the truthful
legacy state is no coded verdict at all. Every refusal is exercised too: a
foreign (n8n) row, another system's data on the row, a current evaluation that
is not a coverage review.
"""
from __future__ import annotations

import json
from datetime import timedelta

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.qa_writer import (
    LegacyQaTargetRepository,
    RenderedPayloadRepository,
    WriterOwnershipRepository,
)
from app.qa.delivery import TRANSCRIPT_COVERAGE_INCOMPLETE
from app.rendering.evidence import RENDERER_VERSION
from app.writer.modes import CANARY_NEW_ONLY, DRY_RUN, EXPLICIT_BACKFILL
from app.writer.service import (
    NOTHING_TO_WITHDRAW,
    WITHDRAW_REFUSED_FOREIGN_DATA_PRESENT,
    WITHDRAW_REFUSED_NOT_CONTRADICTED,
    WITHDRAWN,
    WOULD_WITHDRAW,
    LegacyQaWriter,
)
from tests.integration.seeding import (
    DAY,
    legacy_fingerprint,
    seed_evaluation,
    seed_lecture,
    seed_legacy_row,
    seed_render,
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


def _writer(lecture, mode=DRY_RUN):
    kwargs = {}
    if mode != DRY_RUN:
        kwargs = {"confirmed": True}
    return LegacyQaWriter(payload_repository=RenderedPayloadRepository(),
                          legacy_repository=LegacyQaTargetRepository(),
                          ownership_repository=WriterOwnershipRepository(),
                          mode=mode, renderer_version=RENDERER_VERSION,
                          lecture_ids=[str(lecture["lecture_id"])], **kwargs)


def _published_non_delivered(db):
    """A NON_DELIVERED verdict the coded writer really published, a day ago."""
    lecture = seed_lecture(db)
    old = seed_evaluation(db, lecture, qa_status="NON_DELIVERED",
                          delivery_status="NON_DELIVERED", label="old",
                          cancelled_session=True,
                          updated_at=DAY_START - timedelta(days=1))
    seed_render(db, lecture, evaluation_id=old, render_status="RENDERED_NON_DELIVERED",
                statuses={order: "Not Met" for order in range(1, 12)},
                met_count=0, partial_count=0, not_met_count=11, cancelled_session=True,
                duration="0 minutes", duration_score=0, engagement_score=0,
                teaching_quality_rating=1)
    written = _writer(lecture, CANARY_NEW_ONLY).plan_day(db, DAY)
    (row,) = [item for item in written["lectures"]
              if item["lecture_id"] == str(lecture["lecture_id"])]
    assert row.get("write_status") == "WRITTEN", row
    return lecture, old


def _coverage_review(db, lecture):
    return seed_evaluation(db, lecture, qa_status="REVIEW_REQUIRED",
                           review_reason=TRANSCRIPT_COVERAGE_INCOMPLETE, label="review")


DAY_START = None


@pytest.fixture(autouse=True)
def _now(db):
    global DAY_START
    DAY_START = db.execute("SELECT now()").fetchone()[0]


def _session(db, session_id):
    return db.execute("SELECT count(*) FROM public.qa_doctors_sessions WHERE session_id = %s",
                      (session_id,)).fetchone()[0]


def _checklist(db, session_id):
    return db.execute("SELECT count(*) FROM public.qa_doctors_checklist_items "
                      " WHERE session_id = %s", (session_id,)).fetchone()[0]


def test_a_superseded_render_is_no_longer_offered_to_the_writer_or_perfect(db):
    lecture, _old = _published_non_delivered(db)
    assert RenderedPayloadRepository().load_sessions_for_lecture(
        db, lecture["lecture_id"], RENDERER_VERSION)
    _coverage_review(db, lecture)
    assert RenderedPayloadRepository().load_sessions_for_lecture(
        db, lecture["lecture_id"], RENDERER_VERSION) == []
    assert [row for row in RenderedPayloadRepository().load_sessions(
        db, DAY, RENDERER_VERSION) if row["lecture_id"] == lecture["lecture_id"]] == []


def test_a_dry_run_plans_the_withdrawal_and_changes_nothing(db):
    lecture, _old = _published_non_delivered(db)
    _coverage_review(db, lecture)
    before = legacy_fingerprint(db)
    plan = _writer(lecture).withdraw_contradicted_session(db, lecture["lecture_id"])
    assert plan["decision"] == WOULD_WITHDRAW
    assert plan["foreign_columns_occupied"] == []
    assert plan["checklist_rows"] == 11
    assert plan["legacy_rows_deleted"] == 0
    assert legacy_fingerprint(db) == before


def test_the_confirmed_withdrawal_removes_only_the_coded_verdict(db):
    lecture, old = _published_non_delivered(db)
    review = _coverage_review(db, lecture)
    # An n8n row for a DIFFERENT lecture on the same day must be untouched.
    other = seed_lecture(db)
    seed_legacy_row(db, other["transcript_id"], meeting_id=other["meeting_id"])
    foreign_before = db.execute("SELECT md5(q::text) FROM public.qa_doctors_sessions q "
                                " WHERE session_id = %s", (other["transcript_id"],)).fetchone()

    result = _writer(lecture, EXPLICIT_BACKFILL).withdraw_contradicted_session(
        db, lecture["lecture_id"])

    assert result["decision"] == WITHDRAWN and result["legacy_rows_deleted"] == 1
    assert _session(db, lecture["transcript_id"]) == 0
    assert _checklist(db, lecture["transcript_id"]) == 0
    owner = db.execute("SELECT write_status, metadata FROM public.lecture_qa_legacy_writes "
                       " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchone()
    assert owner[0] == "ROLLED_BACK"
    audit = owner[1] if isinstance(owner[1], dict) else json.loads(owner[1])
    assert audit["reason"] == "WITHDRAWN_CONTRADICTED_BY_CURRENT_EVALUATION"
    assert audit["current_evaluation_id"] == str(review)
    assert audit["withdrawn_evaluation_id"] == str(old)
    assert audit["pre_withdraw_digest"]
    # History is kept: both evaluations and the superseded render remain.
    assert db.execute("SELECT count(*) FROM public.lecture_qa_evaluations "
                      " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM public.lecture_qa_rendered_sessions "
                      " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchone()[0] == 1
    assert db.execute("SELECT md5(q::text) FROM public.qa_doctors_sessions q "
                      " WHERE session_id = %s", (other["transcript_id"],)).fetchone() \
        == foreign_before

    again = _writer(lecture, EXPLICIT_BACKFILL).withdraw_contradicted_session(
        db, lecture["lecture_id"])
    assert again["decision"] == NOTHING_TO_WITHDRAW


def test_another_systems_data_on_the_row_blocks_the_withdrawal(db):
    lecture, _old = _published_non_delivered(db)
    _coverage_review(db, lecture)
    db.execute("UPDATE public.qa_doctors_sessions SET recording_url = %s "
               " WHERE session_id = %s", ("https://recording.invalid/x",
                                          lecture["transcript_id"]))
    before = legacy_fingerprint(db)
    result = _writer(lecture, EXPLICIT_BACKFILL).withdraw_contradicted_session(
        db, lecture["lecture_id"])
    assert result["decision"] == WITHDRAW_REFUSED_FOREIGN_DATA_PRESENT
    assert result["foreign_columns_occupied"] == ["recording_url"]
    assert legacy_fingerprint(db) == before


def test_a_foreign_n8n_row_is_never_reachable(db):
    lecture = seed_lecture(db)
    seed_legacy_row(db, lecture["transcript_id"], meeting_id=lecture["meeting_id"])
    _coverage_review(db, lecture)
    before = legacy_fingerprint(db)
    result = _writer(lecture, EXPLICIT_BACKFILL).withdraw_contradicted_session(
        db, lecture["lecture_id"])
    assert result["decision"] == NOTHING_TO_WITHDRAW
    assert legacy_fingerprint(db) == before


def test_nothing_is_withdrawn_unless_the_current_answer_is_a_coverage_review(db):
    lecture, _old = _published_non_delivered(db)
    before = legacy_fingerprint(db)
    result = _writer(lecture, EXPLICIT_BACKFILL).withdraw_contradicted_session(
        db, lecture["lecture_id"])
    assert result["decision"] == WITHDRAW_REFUSED_NOT_CONTRADICTED
    assert legacy_fingerprint(db) == before


def test_a_timestamp_tie_between_evaluations_refuses_rather_than_guesses(db):
    lecture, old = _published_non_delivered(db)
    review = _coverage_review(db, lecture)
    db.execute("UPDATE public.lecture_qa_evaluations SET updated_at = %s "
               " WHERE evaluation_id = ANY(%s)", (DAY_START, [old, review]))
    before = legacy_fingerprint(db)
    result = _writer(lecture, EXPLICIT_BACKFILL).withdraw_contradicted_session(
        db, lecture["lecture_id"])
    assert result["decision"] == WITHDRAW_REFUSED_NOT_CONTRADICTED
    assert legacy_fingerprint(db) == before
