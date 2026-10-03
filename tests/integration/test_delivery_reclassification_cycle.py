"""
RELEASE GATE: a lecture scored as delivered and published, then reclassified
by delivery_speech_guard_v2, through the REAL orchestrator, two cycles.

The production shape is Ray | PMP - June 2026, 2026-09-30: a 26-minute cue
span holding about ten minutes of speech, and a call that covered only 19
minutes of the scheduled slot. Cycle 1 runs under the legacy gate (the guard
patched out) and publishes a scored qa_doctors_sessions row. Cycle 2 runs the
shipped code: it must re-run QA as NON_DELIVERED, re-render and replace the
SAME coded-owned row - and above all it must not fail the cycle.

Every connection is rolled back.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.db.repositories.transcript_artifacts import TranscriptArtifactRepository
from app.orchestration import state as state_module
from app.orchestration.stages import QA_EVALUATION
from app.qa import service as service_module
from tests.integration.seeding import TRAINER, graph_id, insert, seed_lecture, sha, webvtt
from tests.integration.test_late_attendance_orchestrator import (  # noqa: F401 - fixtures
    CountingProvider,
    _commit_point,
    _evaluations,
    _orchestrator,
    _ownership,
    _sessions,
    _stage,
    db,
    provider,
)

SUBJECT = "Ray | PMP - June 2026"
# A day of its own: the shared fixture day may hold other seeded lectures, and
# selection is a day-scoped stage.
SESSION_DATE = date(2031, 9, 30)


def _mostly_silent_cues():
    """26-minute span, 50 seconds of speech every two minutes (~11 minutes)."""
    cues = [(minute * 60_000, minute * 60_000 + 50_000,
             TRAINER if minute % 4 else "Synthetic Learner 1", f"Synthetic line {minute}.")
            for minute in range(0, 26, 2)]
    cues.append((1_510_000, 1_560_000, TRAINER, "Synthetic close."))
    return cues


def _discovered_lecture(db):
    lecture = seed_lecture(db, transcript_id=graph_id(930), meeting_id="MTG-RECLASSIFY",
                           subject=SUBJECT, session_date=SESSION_DATE, select=False)
    # The call ended 19 minutes 55 seconds into the slot, as on 2026-09-30.
    db.execute("UPDATE public.lecture_transcript_artifacts SET provider_end_at = %s"
               " WHERE artifact_id = %s",
               (lecture["start"] + timedelta(minutes=19, seconds=55),
                lecture["artifact_ids"][0]))
    content = webvtt(_mostly_silent_cues())
    TranscriptArtifactRepository().store_content(
        db, lecture["artifact_ids"][0], raw_text=content, content_sha256=sha(content),
        content_bytes=len(content.encode()), content_format="text/vtt",
        speaker_attribution=True)
    for student in range(2):
        insert(db, "kbc_users_data", ID=str(930000 + student),
               FullName=f"Synthetic Student {student}", Group=SUBJECT,
               **{"Program-Status": "Active"})
    return lecture


def _cycle(orchestrator, db, lecture):
    before = CountingProvider.calls_total
    summary = orchestrator.run_window(db, SESSION_DATE,
                                      lecture_ids=[str(lecture["lecture_id"])])
    (item,) = summary["lectures"]
    return item, CountingProvider.calls_total - before


def _legacy_gate_only(original):
    """classify_delivery as it was before the speech guard."""
    def classify(**kwargs):
        kwargs["spoken_seconds"] = None
        return original(**kwargs)
    return classify


def test_a_published_delivered_lecture_is_reclassified_without_failing_the_cycle(
        db, monkeypatch):
    resolver, orchestrator = _orchestrator()
    lecture = _discovered_lecture(db)

    # --- CYCLE 1: legacy gate. Scored and published. -------------------------
    with monkeypatch.context() as patch:
        for module in (state_module, service_module):
            patch.setattr(module, "classify_delivery",
                          _legacy_gate_only(module.classify_delivery))
        item, calls = _cycle(orchestrator, db, lecture)
    assert item["error_code"] is None, item
    assert calls == 1
    ((session_id, *_scores),) = _sessions(db, lecture)
    assert _evaluations(db, lecture)[-1][1] == "COMPLETED"
    _commit_point(db)

    # --- the shipped code sees the answer as stale ---------------------------
    stage = _stage(resolver, db, lecture, QA_EVALUATION)
    assert stage["reason"] == "DELIVERY_POLICY_RECLASSIFIES_DELIVERED", stage

    # --- CYCLE 2: the speech guard. Re-judged, never silently republished. ---
    item, calls = _cycle(orchestrator, db, lecture)
    assert calls == 0
    assert item["actions"] == ["RUN_QA", "RENDER_QA"], item
    # The automated sync never overwrites a published row; a person decides.
    assert item["error_code"] == "LEGACY_ROW_WOULD_BE_UPDATED"
    assert _evaluations(db, lecture)[-1][1] == "NON_DELIVERED"
    assert _trainer_row(db, session_id)[0] != "Session not delivered"  # row untouched

    # --- the operator's explicit, lecture-scoped correction ------------------
    from app.db.repositories.qa_writer import (
        LegacyQaTargetRepository,
        RenderedPayloadRepository,
        WriterOwnershipRepository,
    )
    from app.rendering.evidence import RENDERER_VERSION
    from app.writer.modes import EXPLICIT_BACKFILL
    from app.writer.service import LegacyQaWriter

    LegacyQaWriter(payload_repository=RenderedPayloadRepository(),
                   legacy_repository=LegacyQaTargetRepository(),
                   ownership_repository=WriterOwnershipRepository(),
                   mode=EXPLICIT_BACKFILL, renderer_version=RENDERER_VERSION,
                   confirmed=True, allow_update_existing=True,
                   lecture_ids=[str(lecture["lecture_id"])]).plan_day(db, SESSION_DATE)
    assert _trainer_row(db, session_id) == ("Session not delivered", 0, 0, 11)
    assert len(_sessions(db, lecture)) == 1


def _trainer_row(db, session_id):
    return db.execute(
        "SELECT trainer, met_count, partial_count, not_met_count"
        "  FROM public.qa_doctors_sessions WHERE session_id = %s", (session_id,)).fetchone()


# ===========================================================================
# one lecture's failed STATEMENT must not cost the rest of the cycle
# ===========================================================================

def _ordinary_lecture(db, n):
    """A full-length delivered lecture on the same day."""
    from tests.integration.test_late_attendance_orchestrator import _cues
    lecture = seed_lecture(db, transcript_id=graph_id(940 + n), meeting_id=f"MTG-ISO-{n}",
                           subject=f"Isolation Lecture {n}", session_date=SESSION_DATE,
                           select=False)
    content = webvtt(_cues(()))
    TranscriptArtifactRepository().store_content(
        db, lecture["artifact_ids"][0], raw_text=content, content_sha256=sha(content),
        content_bytes=len(content.encode()), content_format="text/vtt",
        speaker_attribution=True)
    return lecture


def test_a_failed_statement_in_one_lecture_does_not_lose_the_others(db, monkeypatch):
    import psycopg

    from app.common.errors import DATABASE_ERROR, PlatformError
    from app.orchestration.runner import StageRunner

    _resolver, orchestrator = _orchestrator()
    broken, healthy = _ordinary_lecture(db, 1), _ordinary_lecture(db, 2)
    real_execute = StageRunner.execute

    def execute(self, connection, action, *, session_date, lecture_id):
        if action == "RUN_QA" and str(lecture_id) == str(broken["lecture_id"]):
            try:
                connection.execute("SELECT 1 / 0")      # aborts the transaction
            except psycopg.Error as exc:
                raise PlatformError(DATABASE_ERROR, "stage statement failed") from exc
        return real_execute(self, connection, action, session_date=session_date,
                            lecture_id=lecture_id)

    monkeypatch.setattr(StageRunner, "execute", execute)
    summary = orchestrator.run_window(
        db, SESSION_DATE,
        lecture_ids=[str(broken["lecture_id"]), str(healthy["lecture_id"])])

    by_id = {item["lecture_id"]: item for item in summary["lectures"]}
    assert summary["status"] != "ABORTED", summary["errors"]
    assert by_id[str(broken["lecture_id"])]["error_code"] == DATABASE_ERROR
    assert by_id[str(healthy["lecture_id"])]["error_code"] is None
    assert "SYNC_LEGACY_QA" in by_id[str(healthy["lecture_id"])]["actions"]
    assert len(_sessions(db, healthy)) == 1
    # The transaction is usable afterwards: the cycle can still commit.
    assert db.execute("SELECT 1").fetchone() == (1,)


# ===========================================================================
# a selected part whose content has not been fetched yet
# ===========================================================================

def test_a_combine_failure_is_stored_and_never_fails_the_cycle(db):
    _resolver, orchestrator = _orchestrator()
    # A recurring meeting: last week's transcript is stored, so the transcript
    # stage is satisfied, but today's has been discovered and not yet fetched.
    lecture = seed_lecture(db, meeting_id="MTG-NOCONTENT", subject="Not Yet Fetched",
                           session_date=SESSION_DATE, select=False,
                           artifacts=[graph_id(950), graph_id(951)])
    today, last_week = lecture["artifact_ids"]
    db.execute("UPDATE public.lecture_transcript_artifacts"
               "   SET provider_created_at = provider_created_at - interval '7 days',"
               "       provider_end_at = provider_end_at - interval '7 days'"
               " WHERE artifact_id = %s", (last_week,))
    content = webvtt(_mostly_silent_cues())
    TranscriptArtifactRepository().store_content(
        db, last_week, raw_text=content, content_sha256=sha(content),
        content_bytes=len(content.encode()), content_format="text/vtt",
        speaker_attribution=True)
    summary = orchestrator.run_window(db, SESSION_DATE,
                                      lecture_ids=[str(lecture["lecture_id"])])
    assert summary["status"] != "ABORTED", summary["errors"]
    status, parts, primary = db.execute(
        "SELECT selection_status, selected_part_count, primary_artifact_id"
        "  FROM public.lecture_transcript_selections WHERE lecture_id = %s",
        (lecture["lecture_id"],)).fetchone()
    assert (status, parts, primary) == ("COMBINE_FAILED", 0, None)
    assert db.execute("SELECT 1").fetchone() == (1,)
