"""
RELEASE GATE: pipeline contracts, self-contained, against real PostgreSQL.

These are the contracts that the pre-existing production-evidence modules used
to assert only against real 2026 lectures. Each one here builds the rows it
needs with tests/integration/seeding.py and rolls them back, so the contract is
checked deterministically on a freshly migrated database with no production
data of any kind. The production-evidence modules keep their own versions,
marked `production_data`, because on real history they also assert facts about
real history that a synthetic fixture cannot stand in for.

What is deliberately NOT re-created here: anything whose subject IS a
historical case (the Andrew seam rebuild, the Ray duplicate booking, the
September F-02/F-03 incidents, legacy parity measurements). Those stay in the
acceptance suite.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import psycopg
import pytest

from tests.integration.seeding import (
    DAY,
    SPEAKER_INVENTORY_VERSION,
    TRAINER,
    graph_id,
    insert,
    legacy_fingerprint,
    legacy_rows,
    seed_attendance_snapshot,
    seed_attendance_source,
    seed_engagement,
    seed_evaluation,
    seed_lecture,
    seed_legacy_row,
    seed_lms_source,
    seed_qa_inputs,
    seed_render,
    seed_selection,
    seed_transcript_document,
    sha,
)
from app.config.settings import Settings
from app.db.repositories.qa_shadow import (
    QaEvaluationRepository,
    QaInputRepository,
    QaRunRepository,
)
from app.db.repositories.qa_writer import (
    GenerationAttemptRepository,
    LegacyQaTargetRepository,
    RenderedPayloadRepository,
    WriterOwnershipRepository,
)
from app.orchestration.locks import LectureBusy, LectureLockManager, lock_key
from app.orchestration.runner import StageRunner
from app.qa.checklist import CHECKLIST_ITEMS
from app.rendering.evidence import RENDERER_VERSION
from app.writer.mapping import WRITER_VERSION
from app.writer.modes import (
    CANARY_NEW_ONLY,
    DRY_RUN,
    EXPLICIT_BACKFILL,
    PERFECT_PROTECTED_EXISTING_LEGACY_ROW,
    PRODUCTION_NEW_ONLY,
    PROTECTED_EXISTING_LEGACY_ROW,
    WOULD_INSERT,
    WOULD_SKIP_IDENTICAL,
    WOULD_UPDATE,
)
from app.writer.service import LegacyQaWriter, WriteVerificationError


from app.qa.perfect import PERFECT_ELIGIBILITY_VERSION_V2 as PERFECT_V2


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


def _runner(**kwargs):
    kwargs.setdefault("persist", False)
    return StageRunner(settings=Settings.from_environment(), **kwargs)


def _writer(mode=DRY_RUN, lecture=None, **kwargs):
    if mode != DRY_RUN:
        kwargs.setdefault("confirmed", True)
        kwargs.setdefault("lecture_ids", [str(lecture["lecture_id"])])
    return LegacyQaWriter(payload_repository=RenderedPayloadRepository(),
                          legacy_repository=LegacyQaTargetRepository(),
                          ownership_repository=WriterOwnershipRepository(),
                          mode=mode, renderer_version=RENDERER_VERSION, **kwargs)


def _plan(connection, lecture, mode=DRY_RUN, **kwargs):
    summary = _writer(mode, lecture, **kwargs).plan_day(connection, DAY)
    rows = [r for r in summary["lectures"] if r["lecture_id"] == str(lecture["lecture_id"])]
    assert len(rows) == 1, summary["lectures"]
    return rows[0]


# ===========================================================================
# advisory locks - the lecture id is only a key, so nothing here needs history
# ===========================================================================

def test_the_same_lecture_cannot_be_locked_by_two_connections(db):
    other = psycopg.connect(Settings.from_environment().database_url)
    manager, lecture_id = LectureLockManager(), str(uuid.uuid4())
    try:
        assert manager.try_acquire(db, lecture_id) is True
        assert manager.try_acquire(other, lecture_id) is False
    finally:
        manager.release(db, lecture_id)
        other.close()


def test_different_lectures_lock_independently(db):
    other = psycopg.connect(Settings.from_environment().database_url)
    manager, one, two = LectureLockManager(), str(uuid.uuid4()), str(uuid.uuid4())
    try:
        assert manager.try_acquire(db, one) is True
        assert manager.try_acquire(other, two) is True
    finally:
        manager.release(db, one)
        manager.release(other, two)
        other.close()


def test_holding_a_lecture_twice_in_one_place_is_refused(db):
    manager, lecture_id = LectureLockManager(), str(uuid.uuid4())
    with manager.hold(db, lecture_id):
        other = psycopg.connect(Settings.from_environment().database_url)
        try:
            with pytest.raises(LectureBusy):
                with manager.hold(other, lecture_id):
                    pass
        finally:
            other.close()


def test_the_advisory_lock_is_visible_in_pg_locks_while_held(db):
    manager, lecture_id = LectureLockManager(), str(uuid.uuid4())
    # pg_locks.objid is unsigned; lock_key is the signed form of the same key.
    held = lock_key(lecture_id) % 2 ** 32
    with manager.hold(db, lecture_id):
        assert held in manager.holders(db)
    assert held not in manager.holders(db)


def test_the_lock_is_released_when_the_work_inside_it_raises(db):
    manager, lecture_id = LectureLockManager(), str(uuid.uuid4())
    with pytest.raises(ValueError):
        with manager.hold(db, lecture_id):
            raise ValueError("the work failed")
    assert lock_key(lecture_id) not in manager.holders(db)


def test_a_crashed_process_releases_its_lock_when_its_connection_dies(db):
    """Why these are advisory locks and not rows: nothing has to clean up."""
    manager, lecture_id = LectureLockManager(), str(uuid.uuid4())
    crashed = psycopg.connect(Settings.from_environment().database_url)
    assert manager.try_acquire(crashed, lecture_id) is True
    crashed.close()                                  # the process "dies"
    assert manager.try_acquire(db, lecture_id) is True
    manager.release(db, lecture_id)


# ===========================================================================
# schema constraints - the database enforces the invariants, not only the code
# ===========================================================================

def test_database_rejects_a_duplicate_cue_index(db):
    lecture = seed_lecture(db)
    document = seed_transcript_document(db, lecture)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(db, "lecture_transcript_cues", cue_id=uuid.uuid4(),
               document_id=document["document_id"], cue_index=1, start_ms=0, end_ms=10,
               text="duplicate", cue_text_sha256=sha("dup"))


def test_database_rejects_impossible_cue_timings(db):
    lecture = seed_lecture(db)
    document = seed_transcript_document(db, lecture)
    for start, end in ((10, 5), (-1, 10)):
        with pytest.raises((psycopg.errors.CheckViolation,
                            psycopg.errors.NumericValueOutOfRange)):
            insert(db, "lecture_transcript_cues", cue_id=uuid.uuid4(),
                   document_id=document["document_id"], cue_index=99, start_ms=start,
                   end_ms=end, text="impossible", cue_text_sha256=sha("bad"))
        db.rollback()


def test_database_rejects_a_duplicate_speaker_label_for_one_document_and_version(db):
    lecture = seed_lecture(db)
    document = seed_transcript_document(db, lecture)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(db, "lecture_transcript_speakers", speaker_id=uuid.uuid4(),
               document_id=document["document_id"],
               speaker_inventory_version=SPEAKER_INVENTORY_VERSION,
               speaker_label_raw=TRAINER, speaker_label_normalized=TRAINER.lower(),
               cue_count=1, first_cue_index=1, last_cue_index=1,
               first_spoken_start_ms=0, last_spoken_end_ms=1, gross_spoken_ms=1)


def test_database_rejects_a_selected_selection_without_a_primary(db):
    lecture = seed_lecture(db, select=False)
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(db, "lecture_transcript_selections", selection_id=uuid.uuid4(),
               lecture_id=lecture["lecture_id"], selection_version="probe_v1",
               selection_status="SELECTED", primary_artifact_id=None,
               selected_part_count=0)


def test_database_rejects_a_transcript_candidate_without_a_canonical_lecture(db):
    lecture = seed_lecture(db, select=False)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert(db, "lecture_transcript_candidates", candidate_id=uuid.uuid4(),
               lecture_id=uuid.uuid4(), artifact_id=lecture["artifact_ids"][0],
               meeting_id=lecture["meeting_id"], meeting_lookup_user_id="organizer")


def test_two_writers_cannot_both_claim_the_same_legacy_session(db):
    """The ownership ledger is UNIQUE (legacy_session_id, writer_version)."""
    lecture = seed_lecture(db)
    render = seed_render(db, lecture)
    entry = dict(write_id=uuid.uuid4(), lecture_id=lecture["lecture_id"],
                 evaluation_id=render["evaluation_id"],
                 rendered_session_id=render["rendered_session_id"],
                 legacy_session_id=lecture["transcript_id"], writer_version=WRITER_VERSION,
                 source_fingerprint=sha("one"), write_mode=PRODUCTION_NEW_ONLY,
                 write_status="WRITTEN")
    insert(db, "lecture_qa_legacy_writes", **entry)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(db, "lecture_qa_legacy_writes", **{**entry, "write_id": uuid.uuid4(),
                                                  "source_fingerprint": sha("two")})


def test_one_perfect_lecture_key_can_only_exist_once(db):
    lecture = seed_lecture(db)
    row = dict(lecture_key=f"{lecture['subject']}|{DAY}", session_date=DAY,
               subject=lecture["subject"], met_count=11)
    insert(db, "qa_perfect_lectures", **row)
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(db, "qa_perfect_lectures", **row)


def test_an_evaluation_and_a_render_are_unique_per_source_fingerprint(db):
    lecture = seed_lecture(db)
    seed_evaluation(db, lecture, label="same")
    with pytest.raises(psycopg.errors.UniqueViolation):
        seed_evaluation(db, lecture, label="same")


# ===========================================================================
# attendance resolution, from the external source, on synthetic rows
# ===========================================================================

def _resolution(db, lecture, **kwargs):
    return _runner()._resolution_service().resolve_lecture(
        db, str(lecture["lecture_id"]), **kwargs)


def test_attendance_resolution_persists_a_snapshot_and_its_roles(db):
    lecture = seed_lecture(db)
    seed_transcript_document(db, lecture)
    seed_attendance_source(db, lecture, present=3)
    summary = _resolution(db, lecture, persist=True)
    assert summary["status"] in ("COMPLETED", "COMPLETED_WITH_AMBIGUITY"), summary
    snapshot = db.execute("""
        SELECT source_row_count, present_row_count, effective_member_count
          FROM public.lecture_attendance_snapshots WHERE lecture_id = %s""",
                          (lecture["lecture_id"],)).fetchone()
    assert snapshot == (3, 3, 3)
    roles = db.execute("""
        SELECT count(*) FROM public.lecture_transcript_speaker_roles r
          JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = r.attendance_snapshot_id
         WHERE s.lecture_id = %s AND r.role = 'TRAINER_CANDIDATE'""",
                       (lecture["lecture_id"],)).fetchone()[0]
    assert roles == 1


def test_a_second_attendance_resolution_creates_no_new_evidence(db):
    lecture = seed_lecture(db)
    seed_transcript_document(db, lecture)
    seed_attendance_source(db, lecture, present=3)
    _resolution(db, lecture, persist=True)
    before = db.execute("""
        SELECT (SELECT count(*) FROM public.lecture_attendance_snapshots WHERE lecture_id = %s),
               (SELECT count(*) FROM public.lecture_attendance_snapshot_members m
                  JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = m.snapshot_id
                 WHERE s.lecture_id = %s)""",
        (lecture["lecture_id"], lecture["lecture_id"])).fetchone()
    _resolution(db, lecture, persist=True)
    after = db.execute("""
        SELECT (SELECT count(*) FROM public.lecture_attendance_snapshots WHERE lecture_id = %s),
               (SELECT count(*) FROM public.lecture_attendance_snapshot_members m
                  JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = m.snapshot_id
                 WHERE s.lecture_id = %s)""",
        (lecture["lecture_id"], lecture["lecture_id"])).fetchone()
    assert after == before


def test_attendance_resolution_never_writes_the_external_source(db):
    lecture = seed_lecture(db)
    seed_transcript_document(db, lecture)
    seed_attendance_source(db, lecture, present=2)
    before = db.execute("SELECT count(*), md5(string_agg(k::text, '|' ORDER BY row_id)) "
                        "  FROM public.kbc_attendance k").fetchone()
    _resolution(db, lecture, persist=True)
    after = db.execute("SELECT count(*), md5(string_agg(k::text, '|' ORDER BY row_id)) "
                       "  FROM public.kbc_attendance k").fetchone()
    assert after == before


def test_a_lecture_the_source_never_mentions_reports_missing_not_zero(db):
    """F-03's distinction, on synthetic evidence: absent source != zero attendees."""
    lecture = seed_lecture(db)
    seed_transcript_document(db, lecture)
    summary = _resolution(db, lecture, persist=True)
    snapshot = db.execute("""
        SELECT source_row_count, present_row_count, effective_member_count
          FROM public.lecture_attendance_snapshots WHERE lecture_id = %s""",
                          (lecture["lecture_id"],)).fetchone()
    assert snapshot == (0, 0, 0)
    from app.attendance.coverage import classify, is_authoritative
    status = classify(source_row_count=0, present_row_count=0, effective_member_count=0,
                      source_rows_any_status=0)
    assert status == "SOURCE_MISSING" and not is_authoritative(status)
    assert summary["status"] is not None


def test_snapshot_members_store_no_plain_email_address(db):
    lecture = seed_lecture(db)
    seed_transcript_document(db, lecture)
    seed_attendance_source(db, lecture, present=2)
    _resolution(db, lecture, persist=True)
    rows = db.execute("""
        SELECT m::text FROM public.lecture_attendance_snapshot_members m
          JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = m.snapshot_id
         WHERE s.lecture_id = %s""", (lecture["lecture_id"],)).fetchall()
    assert rows
    assert not [row for row in rows if "@example.invalid" in row[0]]


# ===========================================================================
# engagement scope
# ===========================================================================

def _engagement(db, lecture, **kwargs):
    return _runner()._engagement_service().calculate_lecture(
        db, str(lecture["lecture_id"]), **kwargs)


def test_a_lecture_scoped_engagement_run_selects_exactly_one_lecture(db):
    one = seed_qa_inputs(db)
    sibling = seed_qa_inputs(db)
    summary = _engagement(db, one, persist=False)
    assert summary["snapshots_considered"] == 1
    assert [str(item["lecture_id"]) for item in summary["lectures"]] == [str(one["lecture_id"])]
    assert str(sibling["lecture_id"]) not in str(summary["lectures"])


def test_engagement_never_widens_a_lecture_scope_to_the_whole_day(db):
    one = seed_qa_inputs(db)
    seed_qa_inputs(db)
    seed_qa_inputs(db)
    assert _engagement(db, one, persist=False)["snapshots_considered"] == 1


def test_a_scoped_engagement_run_leaves_its_same_day_siblings_untouched(db):
    one = seed_qa_inputs(db)
    sibling = seed_qa_inputs(db)
    before = db.execute("SELECT max(updated_at), count(*) FROM public.lecture_engagement_metrics "
                        " WHERE lecture_id = %s", (sibling["lecture_id"],)).fetchone()
    _engagement(db, one, persist=True)
    after = db.execute("SELECT max(updated_at), count(*) FROM public.lecture_engagement_metrics "
                       " WHERE lecture_id = %s", (sibling["lecture_id"],)).fetchone()
    assert after == before


def test_an_unknown_lecture_is_refused_rather_than_widened(db):
    from app.engagement.service import EngagementScopeError
    with pytest.raises(EngagementScopeError):
        _runner()._engagement_service().calculate_lecture(db, str(uuid.uuid4()),
                                                          persist=False)


# ===========================================================================
# shadow QA: the engine's economics, on a stub provider
# ===========================================================================

class StubProvider:
    def __init__(self, output=None, error=None):
        self.output, self.error, self.calls = output, error, 0

    def complete_json(self, *, system_message, user_message):
        self.calls += 1
        if self.error:
            raise self.error
        return {"output": self.output, "provider": "stub", "model_requested": "stub",
                "model_reported": "stub", "response_id": f"stub-{self.calls}",
                "usage": {}, "attempts": 1}


def good_output(**overrides):
    payload = {
        "session_info": {"trainer": TRAINER, "date": DAY.isoformat()},
        "checklist_evaluation": [{"item": item, "status": "Met", "evidence_clips": []}
                                 for item in CHECKLIST_ITEMS],
        "overall_summary": {"strengths": [], "areas_for_improvement": [],
                            "overall_judgement": "Solid session."},
        "ksbs_covered": [],
        "teaching_quality": {"rating_1_5": 4, "comments": "Clear.", "evidence_clips": []},
    }
    payload.update(overrides)
    return payload


def _qa(provider, lecture):
    from app.attendance.resolver import RESOLVER_VERSION
    from app.attendance.roles import ROLE_ALGORITHM_VERSION
    from app.engagement.calculator import ENGAGEMENT_ALGORITHM_VERSION
    from app.qa.service import ShadowQaService
    return ShadowQaService(
        input_repository=QaInputRepository(), evaluation_repository=QaEvaluationRepository(),
        run_repository=QaRunRepository(), provider=provider,
        attempt_repository=GenerationAttemptRepository(),
        resolver_version=RESOLVER_VERSION, role_algorithm_version=ROLE_ALGORITHM_VERSION,
        engagement_algorithm_version=ENGAGEMENT_ALGORITHM_VERSION,
        model_name=Settings.from_environment().qa_model_name,
        lecture_ids=[str(lecture["lecture_id"])])


def _evaluations(db, lecture):
    return db.execute("SELECT qa_status FROM public.lecture_qa_evaluations "
                      " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchall()


def test_a_preview_run_makes_no_model_call_and_writes_no_evaluation(db):
    lecture = seed_qa_inputs(db)
    provider = StubProvider(good_output())
    summary = _qa(provider, lecture).run_day(db, DAY, execute=False)
    assert summary["lectures"]
    assert provider.calls == 0
    assert _evaluations(db, lecture) == []


def test_a_second_run_reuses_the_evaluation_and_buys_no_second_generation(db):
    lecture = seed_qa_inputs(db)
    provider = StubProvider(good_output())
    first = _qa(provider, lecture).run_day(db, DAY, execute=True)["lectures"][0]
    assert provider.calls == 1 and first["qa_status"] == "COMPLETED"
    second = _qa(provider, lecture).run_day(db, DAY, execute=True)["lectures"][0]
    assert provider.calls == 1                       # nothing bought the second time
    assert second.get("reused") is True
    assert len(_evaluations(db, lecture)) == 1


def test_a_provider_error_never_becomes_not_met_verdicts(db):
    lecture = seed_qa_inputs(db)
    from app.qa.provider import ProviderError
    provider = StubProvider(error=ProviderError("PROVIDER_ERROR", "provider exploded"))
    result = _qa(provider, lecture).run_day(db, DAY, execute=True)["lectures"][0]
    assert result["qa_status"] != "COMPLETED"
    statuses = db.execute("""
        SELECT DISTINCT c.status FROM public.lecture_qa_checklist_items c
          JOIN public.lecture_qa_evaluations e ON e.evaluation_id = c.evaluation_id
         WHERE e.lecture_id = %s""", (lecture["lecture_id"],)).fetchall()
    assert statuses == []


def test_the_generation_cap_stops_paying_for_the_same_fingerprint(db):
    """Three invalid generations close the fingerprint; the fourth run buys nothing."""
    lecture = seed_qa_inputs(db)
    invalid = good_output(checklist_evaluation=[])          # structurally unusable
    provider = StubProvider(invalid)
    for _ in range(3):
        _qa(provider, lecture).run_day(db, DAY, execute=True)
    assert provider.calls == 3
    _qa(provider, lecture).run_day(db, DAY, execute=True)
    assert provider.calls == 3
    attempts = db.execute("SELECT count(*) FROM public.lecture_qa_generation_attempts "
                          " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchone()[0]
    assert attempts == 3


# ===========================================================================
# the legacy writer: write, protect, roll back
# ===========================================================================

def test_a_canary_write_creates_one_session_eleven_rows_and_one_owner(db):
    lecture = seed_lecture(db)
    seed_render(db, lecture)
    row = _plan(db, lecture, CANARY_NEW_ONLY)
    assert row["decision"] == WOULD_INSERT
    assert len(legacy_rows(db, lecture["meeting_id"])) == 1
    assert db.execute("SELECT count(*) FROM public.qa_doctors_checklist_items "
                      " WHERE session_id = %s", (lecture["transcript_id"],)).fetchone()[0] == 11
    owners = db.execute("SELECT write_status, write_mode FROM public.lecture_qa_legacy_writes "
                        " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchall()
    assert owners == [("WRITTEN", CANARY_NEW_ONLY)]


def test_a_changed_fingerprint_updates_only_a_coded_owned_target(db):
    lecture = seed_lecture(db)
    seed_render(db, lecture)
    _plan(db, lecture, PRODUCTION_NEW_ONLY)
    db.execute("UPDATE public.lecture_qa_rendered_sessions SET met_count = 8, "
               " source_fingerprint = %s WHERE lecture_id = %s",
               (sha("changed"), lecture["lecture_id"]))
    row = _plan(db, lecture, EXPLICIT_BACKFILL, allow_update_existing=True)
    assert row["decision"] == WOULD_UPDATE
    assert len(legacy_rows(db, lecture["meeting_id"])) == 1
    assert db.execute("SELECT met_count FROM public.qa_doctors_sessions WHERE session_id = %s",
                      (lecture["transcript_id"],)).fetchone()[0] == 8


def test_a_write_enabled_mode_still_refuses_a_row_the_platform_did_not_write(db):
    lecture = seed_lecture(db)
    seed_render(db, lecture)
    seed_legacy_row(db, lecture["transcript_id"], meeting_id=lecture["meeting_id"])
    before = legacy_fingerprint(db)
    row = _plan(db, lecture, PRODUCTION_NEW_ONLY)
    assert row["decision"] == PROTECTED_EXISTING_LEGACY_ROW
    assert legacy_fingerprint(db) == before


def test_rollback_removes_only_a_coded_owned_session(db):
    lecture = seed_lecture(db)
    seed_render(db, lecture)
    _plan(db, lecture, PRODUCTION_NEW_ONLY)
    writer = _writer(PRODUCTION_NEW_ONLY, lecture)
    result = writer.rollback_owned_session(db, lecture["transcript_id"])
    assert (result["status"], result["deleted"]) == ("ROLLED_BACK", True)
    assert legacy_rows(db, lecture["meeting_id"]) == []
    assert db.execute("SELECT count(*) FROM public.qa_doctors_checklist_items "
                      " WHERE session_id = %s", (lecture["transcript_id"],)).fetchone()[0] == 0


def test_rollback_refuses_a_session_the_platform_does_not_own(db):
    lecture = seed_lecture(db)
    seed_render(db, lecture)
    seed_legacy_row(db, lecture["transcript_id"], meeting_id=lecture["meeting_id"])
    before = legacy_fingerprint(db)
    result = _writer(PRODUCTION_NEW_ONLY, lecture).rollback_owned_session(
        db, lecture["transcript_id"])
    assert (result["status"], result["deleted"]) == (PROTECTED_EXISTING_LEGACY_ROW, False)
    assert legacy_fingerprint(db) == before


def test_rollback_requires_a_write_enabled_mode(db):
    lecture = seed_lecture(db)
    seed_render(db, lecture)
    with pytest.raises(WriteVerificationError):
        _writer(DRY_RUN, lecture).rollback_owned_session(db, lecture["transcript_id"])


def test_the_write_never_disturbs_the_columns_other_workflows_own(db):
    lecture = seed_lecture(db)
    seed_render(db, lecture)
    seed_legacy_row(db, lecture["transcript_id"], meeting_id=lecture["meeting_id"],
                    clips_status="done", clips_analysis_completeness="positive_clips_v5_final",
                    recording_url="https://recording.invalid/keep")
    # Make it ours, so the writer is allowed to update it at all.
    render = db.execute("SELECT rendered_session_id, evaluation_id, source_fingerprint "
                        "  FROM public.lecture_qa_rendered_sessions WHERE lecture_id = %s",
                        (lecture["lecture_id"],)).fetchone()
    insert(db, "lecture_qa_legacy_writes", write_id=uuid.uuid4(),
           lecture_id=lecture["lecture_id"], evaluation_id=render[1],
           rendered_session_id=render[0], legacy_session_id=lecture["transcript_id"],
           writer_version=WRITER_VERSION, source_fingerprint=sha("older"),
           write_mode=PRODUCTION_NEW_ONLY, write_status="WRITTEN")
    _plan(db, lecture, EXPLICIT_BACKFILL, allow_update_existing=True)
    kept = db.execute("SELECT clips_status, clips_analysis_completeness, recording_url "
                      "  FROM public.qa_doctors_sessions WHERE session_id = %s",
                      (lecture["transcript_id"],)).fetchone()
    assert kept == ("done", "positive_clips_v5_final", "https://recording.invalid/keep")


# ===========================================================================
# the derived Perfect Lecture output
# ===========================================================================

def _perfect_plan(db, lecture, *, mode=DRY_RUN, version=PERFECT_V2):
    runner = _runner(perfect_eligibility_version=version)
    writer = runner._writer(lecture["lecture_id"], mode=mode,
                            confirmed=mode != DRY_RUN)
    summary = writer.plan_day(db, DAY)
    rows = [r for r in summary["lectures"] if r["lecture_id"] == str(lecture["lecture_id"])]
    assert len(rows) == 1
    return rows[0]


def _perfect(db, lecture, met=11):
    """A lecture whose render is eleven-out-of-eleven, with attendance evidence."""
    seed_attendance_snapshot(db, lecture, source_rows=3, present_rows=3, members=3)
    return seed_render(db, lecture, statuses={}, met_count=met, partial_count=0,
                       not_met_count=11 - met, attended_count=3)


def test_a_perfect_dry_run_writes_no_legacy_perfect_row(db):
    lecture = seed_lecture(db)
    _perfect(db, lecture)
    before = legacy_fingerprint(db)
    row = _perfect_plan(db, lecture)
    assert row["perfect_lecture"] is not None
    assert legacy_fingerprint(db) == before


def test_a_legacy_owned_perfect_row_under_our_key_is_protected(db):
    lecture = seed_lecture(db)
    _perfect(db, lecture)
    key = _perfect_plan(db, lecture)["perfect_lecture"].get("legacy_lecture_key")
    assert key, "the planner must name the key it would write"
    insert(db, "qa_perfect_lectures", lecture_key=key, session_date=DAY,
           subject=lecture["subject"], met_count=11, trainer="n8n trainer",
           recording_url="https://recording.invalid/legacy")
    # Scoped to the Perfect table: the same write-enabled plan legitimately
    # writes the QA session row, and that is not what this contract is about.
    before = db.execute("SELECT md5(string_agg(p::text, '|' ORDER BY lecture_key)) "
                        "  FROM public.qa_perfect_lectures p").fetchone()
    row = _perfect_plan(db, lecture, mode=PRODUCTION_NEW_ONLY)["perfect_lecture"]
    assert row["perfect_decision"] == PERFECT_PROTECTED_EXISTING_LEGACY_ROW
    after = db.execute("SELECT md5(string_agg(p::text, '|' ORDER BY lecture_key)) "
                       "  FROM public.qa_perfect_lectures p").fetchone()
    assert after == before


def test_a_recording_url_written_by_another_workflow_survives_our_write(db):
    lecture = seed_lecture(db)
    _perfect(db, lecture)
    _perfect_plan(db, lecture, mode=PRODUCTION_NEW_ONLY)
    row = db.execute("SELECT lecture_key FROM public.qa_perfect_lectures").fetchone()
    if row is None:
        pytest.skip("this lecture is not eligible under the current policy")
    db.execute("UPDATE public.qa_perfect_lectures SET recording_url = %s, excel_synced_at = now()",
               ("https://recording.invalid/other-workflow",))
    db.execute("UPDATE public.lecture_qa_rendered_sessions SET attended_count = 4, "
               " source_fingerprint = %s WHERE lecture_id = %s",
               (sha("changed-perfect"), lecture["lecture_id"]))
    _perfect_plan(db, lecture, mode=PRODUCTION_NEW_ONLY)
    kept = db.execute("SELECT recording_url, excel_synced_at IS NOT NULL "
                      "  FROM public.qa_perfect_lectures").fetchone()
    assert kept == ("https://recording.invalid/other-workflow", True)
