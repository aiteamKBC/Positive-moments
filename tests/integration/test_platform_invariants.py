"""
RELEASE GATE: platform invariants that never needed production evidence.

These contracts moved here from the production-evidence modules, where they sat
next to tests about real 2026 lectures but never depended on one: schema shape,
mapping arity, advisory cycle locks, identity derivations, and the structural
refusals ("this service has no provider at all", "an unknown lecture is
refused"). They are stated once, here, and the acceptance modules no longer
duplicate them.

Everything is either pure, or seeds the one row it needs and rolls it back.
"""
from __future__ import annotations

import uuid
from datetime import date

import psycopg
import pytest

from tests.integration.seeding import DAY, seed_lecture
from app.config.settings import Settings
from app.db.repositories.attendance_coverage import AttendanceCoverageRepository
from app.db.repositories.perfect_lectures import (
    LegacyPerfectLectureRepository,
    PerfectLectureOwnershipRepository,
    PerfectLectureResultRepository,
)
from app.db.repositories.qa_writer import GenerationAttemptRepository
from app.orchestration.locks import (
    CYCLE_LOCK_KEY,
    SchedulerCycleBusy,
    SchedulerCycleLock,
    lock_key,
)
from app.orchestration.runner import StageRunner
from app.qa.perfect import (
    PERFECT_ELIGIBILITY_VERSION,
    PERFECT_ELIGIBILITY_VERSION_V2,
)
from app.writer.mapping import SESSION_COLUMNS
from app.writer.modes import DRY_RUN
from app.writer.perfect_mapping import (
    CODED_OWNED_COLUMNS,
    FOREIGN_OWNED_COLUMNS,
    RECORDING_OWNED_COLUMNS,
)
from app.writer.perfect_service import PerfectLecturePlanner


def _connection():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    return psycopg.connect(url)


@pytest.fixture
def db():
    connection = _connection()
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _planner(version=PERFECT_ELIGIBILITY_VERSION_V2, **kwargs):
    return PerfectLecturePlanner(
        result_repository=PerfectLectureResultRepository(),
        ownership_repository=PerfectLectureOwnershipRepository(),
        legacy_repository=LegacyPerfectLectureRepository(),
        mode=DRY_RUN, eligibility_version=version, **kwargs)


# --- mapping arity ---------------------------------------------------------

def test_the_session_mapping_covers_twenty_two_columns():
    assert len(SESSION_COLUMNS) == 22


def test_the_perfect_mapping_still_owns_ten_columns_and_disowns_the_rest():
    assert len(CODED_OWNED_COLUMNS) == 10
    assert set(FOREIGN_OWNED_COLUMNS) == {"excel_synced_at", "detected_at", "id"}
    assert set(RECORDING_OWNED_COLUMNS) == {"recording_url", "recap_url"}


# --- structural refusals ---------------------------------------------------

def test_the_revalidation_service_has_no_provider_at_all():
    """Structurally incapable of buying a generation, not merely told not to."""
    runner = StageRunner(settings=Settings.from_environment(), persist=True)
    assert runner._qa_service(provider=None).provider is None


def test_the_recovery_qa_service_has_no_provider_at_all():
    """Attendance recovery is structurally incapable of buying a generation."""
    from app.attendance.recovery import AttendanceRecoveryService
    from app.db.repositories.attendance_coverage import AttendanceCoverageRepository as Coverage
    runner = StageRunner(settings=Settings.from_environment(), persist=False)
    service = AttendanceRecoveryService(
        resolution_service=runner._resolution_service(),
        engagement_service=runner._engagement_service(),
        qa_service=runner._qa_service(provider=None),
        coverage_repository=Coverage())
    assert service.qa_service.provider is None


def test_the_v1_roster_is_never_consumed_by_the_engine():
    from app.qa.service import QaInputError, ShadowQaService
    from app.attendance.resolver import RESOLVER_VERSION
    from app.attendance.roles import ROLE_ALGORITHM_VERSION
    from app.engagement.calculator import ENGAGEMENT_ALGORITHM_VERSION
    with pytest.raises(QaInputError):
        ShadowQaService(
            input_repository=None, evaluation_repository=None, run_repository=None,
            attendance_roster_version="attendance_roster_v1_include_makeup",
            resolver_version=RESOLVER_VERSION, role_algorithm_version=ROLE_ALGORITHM_VERSION,
            engagement_algorithm_version=ENGAGEMENT_ALGORITHM_VERSION, model_name="stub")


def test_a_seam_rebuild_of_a_lecture_without_selected_parts_is_refused(db):
    from app.db.repositories.transcript_documents import TranscriptDocumentRepository
    from app.transcripts.seam_service import SeamDedupService, SeamInputError
    service = SeamDedupService(document_repository=TranscriptDocumentRepository())
    with pytest.raises(SeamInputError, match="no Phase 2B selected parts"):
        service.rebuild_lecture(db, str(uuid.uuid4()))


def test_an_attendance_recovery_of_an_unknown_lecture_is_refused_rather_than_widened(db):
    from app.attendance.service import AttendanceScopeError
    runner = StageRunner(settings=Settings.from_environment(), persist=False)
    with pytest.raises(AttendanceScopeError):
        runner._resolution_service().resolve_lecture(db, str(uuid.uuid4()), persist=False)


def test_rendering_a_day_with_no_evidence_fails_loudly(db):
    from app.rendering.service import RenderInputError
    runner = StageRunner(settings=Settings.from_environment(), persist=False)
    with pytest.raises(RenderInputError):
        runner._rendering_service(uuid.uuid4()).render_day(db, date(2019, 1, 1))


def test_an_unknown_lecture_is_unknown_and_never_an_authoritative_zero(db):
    from app.attendance.coverage import is_authoritative
    from app.attendance.service import ATTENDANCE_RESOLUTION_VERSION
    coverage = AttendanceCoverageRepository().for_lecture(
        db, str(uuid.uuid4()),
        attendance_resolution_version=ATTENDANCE_RESOLUTION_VERSION)
    assert coverage["snapshot_present"] is False
    assert is_authoritative(coverage["attendance_coverage_status"]) is False


# --- the Perfect policy ----------------------------------------------------

def test_the_v2_planner_can_never_exist_without_a_coverage_reader():
    """A policy that needs evidence must never run without the means to read it."""
    planner = _planner()
    assert planner.coverage_repository is not None
    assert hasattr(planner.coverage_repository, "for_lecture")
    assert _planner(PERFECT_ELIGIBILITY_VERSION).eligibility_version \
        == PERFECT_ELIGIBILITY_VERSION


def test_the_platform_default_policy_is_the_attendance_aware_one():
    from app.qa.perfect import DEFAULT_PERFECT_ELIGIBILITY_VERSION
    assert DEFAULT_PERFECT_ELIGIBILITY_VERSION == PERFECT_ELIGIBILITY_VERSION_V2
    assert DEFAULT_PERFECT_ELIGIBILITY_VERSION != PERFECT_ELIGIBILITY_VERSION
    default = PerfectLecturePlanner(
        result_repository=PerfectLectureResultRepository(),
        ownership_repository=PerfectLectureOwnershipRepository(),
        legacy_repository=LegacyPerfectLectureRepository(), mode=DRY_RUN)
    assert default.eligibility_version == PERFECT_ELIGIBILITY_VERSION_V2


# --- schema shape ----------------------------------------------------------

def test_the_perfect_migration_added_two_coded_tables_and_changed_nothing_legacy(db):
    for table in ("lecture_perfect_lecture_results",
                  "lecture_perfect_lecture_legacy_writes"):
        assert db.execute("SELECT to_regclass(%s) IS NOT NULL",
                          (f"public.{table}",)).fetchone()[0]
    columns = [row[0] for row in db.execute(
        "SELECT column_name FROM information_schema.columns "
        " WHERE table_schema='public' AND table_name='qa_perfect_lectures' "
        " ORDER BY ordinal_position").fetchall()]
    assert columns == ["id", "lecture_key", "session_date", "subject", "module",
                       "trainer", "engagement", "attended_count", "met_count",
                       "recording_url", "recap_url", "detected_at", "meeting_id",
                       "session_id", "excel_synced_at"]
    triggers = db.execute(
        "SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid "
        " WHERE r.relname='qa_perfect_lectures' AND NOT t.tgisinternal").fetchone()[0]
    assert triggers == 0


def test_the_ownership_unique_constraint_exists(db):
    definition = db.execute("""
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
         WHERE conrelid = 'public.lecture_qa_legacy_writes'::regclass
           AND contype = 'u'""").fetchone()[0]
    assert "legacy_session_id" in definition and "writer_version" in definition


def test_a_second_parser_version_needs_no_migration(db):
    """v1 and v2 documents coexist because provenance already includes the parser."""
    definition = db.execute("""
        SELECT pg_get_constraintdef(con.oid) FROM pg_constraint con
          JOIN pg_class rel ON rel.oid = con.conrelid
         WHERE rel.relname = 'lecture_transcript_documents'
           AND con.conname = 'lecture_transcript_documents_provenance_key'""").fetchone()
    assert definition is not None
    assert "parser_version" in definition[0]


# --- identity derivations --------------------------------------------------

def test_document_identity_is_provenance_not_lecture():
    from app.transcripts.webvtt import PARSER_VERSION
    from app.db.repositories.transcript_documents import document_identity
    base = dict(selection_id="sel-1", source_content_sha256="a" * 64,
                parser_version=PARSER_VERSION)
    first = document_identity(**base)
    assert first == document_identity(**base)
    assert first != document_identity(**{**base, "selection_id": "sel-2"})
    assert first != document_identity(**{**base, "source_content_sha256": "b" * 64})
    assert first != document_identity(**{**base, "parser_version": "webvtt_canonical_v2"})


def test_selection_id_is_stable_per_lecture_and_version():
    from app.common.hashing import selection_identity
    from app.transcripts.selection import SELECTION_VERSION
    first = selection_identity(lecture_id="lec-1", selection_version=SELECTION_VERSION)
    assert first == selection_identity(lecture_id="lec-1", selection_version=SELECTION_VERSION)
    assert first != selection_identity(lecture_id="lec-2", selection_version=SELECTION_VERSION)
    assert first != selection_identity(lecture_id="lec-1", selection_version="other_v2")


# --- the scheduler cycle lock ----------------------------------------------

def test_two_scheduler_cycles_cannot_run_at_once():
    lock, first, second = SchedulerCycleLock(), _connection(), _connection()
    try:
        assert lock.try_acquire(first) is True
        assert lock.try_acquire(second) is False
    finally:
        first.close()
        second.close()


def test_a_crashed_cycle_releases_the_cycle_lock():
    lock, crashed = SchedulerCycleLock(), _connection()
    assert lock.try_acquire(crashed) is True
    crashed.close()
    survivor = _connection()
    try:
        assert lock.try_acquire(survivor) is True
    finally:
        survivor.close()


def test_the_cycle_lock_raises_rather_than_queueing():
    lock, holder, waiter = SchedulerCycleLock(), _connection(), _connection()
    try:
        with lock.hold(holder):
            with pytest.raises(SchedulerCycleBusy):
                with lock.hold(waiter):
                    pass
    finally:
        holder.close()
        waiter.close()


def test_the_cycle_key_cannot_collide_with_any_lecture_key(db):
    """Seeded rather than swept: the property is about the key space."""
    seeded = [str(seed_lecture(db, select=False)["lecture_id"]) for _ in range(3)]
    stored = [str(row[0]) for row in db.execute(
        "SELECT lecture_id FROM public.lecture_sessions").fetchall()]
    assert set(seeded) <= set(stored)
    assert CYCLE_LOCK_KEY not in {lock_key(value) for value in stored}


# --- the generation ledger -------------------------------------------------

def test_generation_attempts_are_append_only_and_numbered(db):
    repository = GenerationAttemptRepository()
    fingerprint = "e" * 64
    base = {"lecture_id": None, "source_fingerprint": fingerprint,
            "qa_engine_version": "engine", "prompt_version": "prompt",
            "model_name": "stub", "outcome": "INVALID_EVIDENCE"}
    assert repository.count(db, fingerprint) == 0
    assert [repository.record(db, base) for _ in range(3)] == [1, 2, 3]
    assert repository.count(db, fingerprint) == 3
    numbers = db.execute(
        "SELECT generation_number FROM public.lecture_qa_generation_attempts "
        " WHERE source_fingerprint = %s ORDER BY generation_number",
        (fingerprint,)).fetchall()
    assert [row[0] for row in numbers] == [1, 2, 3]


def test_the_engagement_cli_requires_exactly_one_scope():
    """A date or a lecture, never both and never neither."""
    from app.cli.main import build_parser
    parser = build_parser()
    lecture_id = str(uuid.uuid4())
    with pytest.raises(SystemExit):
        parser.parse_args(["calculate-engagement"])
    with pytest.raises(SystemExit):
        parser.parse_args(["calculate-engagement", "--date", DAY.isoformat(),
                           "--lecture-id", lecture_id])
    scoped = parser.parse_args(["calculate-engagement", "--lecture-id", lecture_id])
    assert scoped.lecture_id == lecture_id and scoped.date is None
    dated = parser.parse_args(["calculate-engagement", "--date", DAY.isoformat()])
    assert dated.lecture_id is None and dated.date == DAY
