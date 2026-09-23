"""
F-01 / F-02 / F-03 at repository + database + orchestration level.

Unlike the rest of tests/integration, which reads real production evidence,
this module SEEDS its own synthetic lectures into the isolated test database
(tools/integration_db_guard + tests/conftest.py guarantee that is the only
database reachable). Every test runs in one transaction that is rolled back.

Nothing is stubbed between the code under test and PostgreSQL: the real
LegacyQaWriter, LegacyOccurrenceGuard, PipelineStateResolver, repositories,
SQL, constraints and savepoints run against real tables. Only the QA model
provider is a stub, because calling it is exactly what F-03 must prevent.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from tests.integration.seeding import (
    DAY,
    graph_id,
    insert,
    legacy_fingerprint,
    legacy_rows,
    seed_lecture,
    seed_legacy_row,
    seed_qa_inputs,
    seed_render,
    sha,
)
from app.attendance.coverage import is_authoritative
from app.attendance.resolver import RESOLVER_VERSION
from app.attendance.roles import ROLE_ALGORITHM_VERSION
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
from app.db.repositories.transcript_selections import TranscriptSelectionRepository
from app.engagement.calculator import ENGAGEMENT_ALGORITHM_VERSION
from app.orchestration.factory import build_resolver
from app.orchestration.runner import StageRunner
from app.orchestration.stages import DEFAULT_MAX_PASSES, EXECUTABLE_STAGES, MAX_PASS_MARGIN
from app.qa.checklist import CHECKLIST_ITEMS
from app.qa.inputs import REQUIRED_ATTENDANCE_ROSTER_VERSION
from app.qa.perfect import PUBLISHABLE_PERFECT_ELIGIBILITY_VERSIONS
from app.qa.service import WAITING_FOR_ATTENDANCE_SOURCE, ShadowQaService
from app.rendering.evidence import RENDERER_VERSION
from app.transcripts.identity import canonical_key, canonical_transcript_identity
from app.transcripts.selection import SELECTION_VERSION
from app.transcripts.webvtt import PARSER_VERSION
from app.writer.legacy_identity import (
    AMBIGUOUS,
    FOREIGN_SAME_OCCURRENCE,
    OWNED_SAME_OCCURRENCE,
)
from app.writer.modes import (
    BLOCKED_AMBIGUOUS_LEGACY_IDENTITY,
    DRY_RUN,
    EXPLICIT_BACKFILL,
    PRODUCTION_NEW_ONLY,
    PROTECTED_EXISTING_LEGACY_ROW,
    WOULD_INSERT,
    WOULD_SKIP_IDENTICAL,
    WriterModeError,
)
from app.writer.service import LegacyQaWriter


PERFECT_V1 = "legacy_qa_v8_perfect_v1"


# ---------------------------------------------------------------------------
# connection: the approved test database only, always rolled back
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    url = Settings.from_environment().database_url
    if not url:
        pytest.skip("no approved test database (TEST_DATABASE_URL)")
    connection = psycopg.connect(url)
    try:
        name = connection.execute("SELECT current_database()").fetchone()[0]
        assert "test" in name, "refusing: not the isolated test database"
        yield connection
    finally:
        connection.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# fixtures: every row below is synthetic (tests/integration/seeding.py)
# ---------------------------------------------------------------------------

def writer(mode=DRY_RUN, lecture=None, **kwargs):
    if mode != DRY_RUN:
        kwargs.setdefault("confirmed", True)
        kwargs.setdefault("lecture_ids", [str(lecture["lecture_id"])])
    return LegacyQaWriter(payload_repository=RenderedPayloadRepository(),
                          legacy_repository=LegacyQaTargetRepository(),
                          ownership_repository=WriterOwnershipRepository(),
                          mode=mode, renderer_version=RENDERER_VERSION, **kwargs)


def plan(connection, lecture, mode=DRY_RUN, **kwargs):
    summary = writer(mode, lecture, **kwargs).plan_day(connection, DAY)
    rows = [r for r in summary["lectures"] if r["lecture_id"] == str(lecture["lecture_id"])]
    assert len(rows) == 1, summary["lectures"]
    return rows[0]


def resolver_legacy_stage(connection, lecture):
    state = build_resolver(probe=False).for_lecture(connection, str(lecture["lecture_id"]))
    return state["stages"]["LEGACY_QA_SYNC"]


# ---------------------------------------------------------------------------
# E. variant and canonical ids are one transcript - in the database too
# ---------------------------------------------------------------------------

def test_E_the_two_spellings_decode_to_one_identity():
    canonical, variant = graph_id(1, variant=False), graph_id(1, variant=True)
    assert canonical != variant
    assert canonical.endswith("VjI=") and variant.endswith("VjLA")
    a, b = canonical_transcript_identity(canonical), canonical_transcript_identity(variant)
    assert a.key == b.key and a.key.startswith("teams:v4:")
    assert canonical_key(graph_id(2, variant=False)) != a.key


def test_E_twin_artifacts_collapse_to_one_candidate_through_real_sql(db):
    canonical, variant = graph_id(3, variant=False), graph_id(3, variant=True)
    other = graph_id(4, variant=False)
    lecture = seed_lecture(db, transcript_id=canonical, meeting_id="MTG-E",
                           artifacts=[canonical, variant, other])
    raw = db.execute("SELECT count(*) FROM public.lecture_transcript_candidates "
                     " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchone()[0]
    kept = TranscriptSelectionRepository().load_candidates(db, lecture["lecture_id"])
    assert raw == 3
    assert len(kept) == 2                        # the twins became one; the other stayed
    keys = {canonical_key(c.provider_transcript_id) for c in kept}
    assert keys == {canonical_key(canonical), canonical_key(other)}


# ---------------------------------------------------------------------------
# A. foreign canonical row + variant payload -> PROTECTED, never a second row
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n8n_spelling_variant", [False, True])
@pytest.mark.parametrize("mode", [DRY_RUN, PRODUCTION_NEW_ONLY])
def test_A_a_foreign_row_under_the_other_spelling_is_protected(db, mode,
                                                               n8n_spelling_variant):
    n = 10 + int(n8n_spelling_variant)
    n8n_id = graph_id(n, variant=n8n_spelling_variant)
    payload_id = graph_id(n, variant=not n8n_spelling_variant)
    lecture = seed_lecture(db, transcript_id=payload_id, meeting_id=f"MTG-A{n}")
    seed_render(db, lecture)
    seed_legacy_row(db, n8n_id, meeting_id=f"MTG-A{n}")
    before = legacy_fingerprint(db)

    row = plan(db, lecture, mode)

    assert row["decision"] == PROTECTED_EXISTING_LEGACY_ROW
    assert row["same_occurrence_verdict"] == FOREIGN_SAME_OCCURRENCE
    assert row["legacy_session_id"] == n8n_id
    assert legacy_fingerprint(db) == before                  # byte-for-byte untouched
    assert [r[0] for r in legacy_rows(db, f"MTG-A{n}")] == [n8n_id]
    assert db.execute("SELECT count(*) FROM public.lecture_qa_legacy_writes "
                      " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchone()[0] == 0


def test_A_explicit_backfill_with_update_allowed_still_protects_the_foreign_row(db):
    n8n_id, payload_id = graph_id(12, variant=False), graph_id(12, variant=True)
    lecture = seed_lecture(db, transcript_id=payload_id, meeting_id="MTG-A12")
    seed_render(db, lecture)
    seed_legacy_row(db, n8n_id, meeting_id="MTG-A12")
    before = legacy_fingerprint(db)
    row = plan(db, lecture, EXPLICIT_BACKFILL, allow_update_existing=True)
    assert row["decision"] == PROTECTED_EXISTING_LEGACY_ROW
    assert legacy_fingerprint(db) == before


# ---------------------------------------------------------------------------
# B. owned row under an equivalent id -> retarget, never a second row
# ---------------------------------------------------------------------------

def _publish_then_reserialize(db, n):
    """The coded writer publishes under the canonical id; Graph then re-spells it."""
    canonical, variant = graph_id(n, variant=False), graph_id(n, variant=True)
    meeting = f"MTG-B{n}"
    lecture = seed_lecture(db, transcript_id=canonical, meeting_id=meeting)
    seed_render(db, lecture, label="first")
    first = plan(db, lecture, PRODUCTION_NEW_ONLY)
    assert first["decision"] == WOULD_INSERT
    assert [r[0] for r in legacy_rows(db, meeting)] == [canonical]
    # Re-rendered under the variant spelling, with a changed payload. The first
    # render is KEPT - the renderer never deletes one, and the ownership row
    # references it - and is moved to a superseded renderer version, which is
    # how a renderer upgrade leaves it, so the writer sees one current payload.
    db.execute("UPDATE public.lecture_qa_rendered_sessions "
               "   SET renderer_version = 'legacy_qa_v8_renderer_v0_superseded' "
               " WHERE lecture_id = %s", (lecture["lecture_id"],))
    db.execute("UPDATE public.lecture_transcript_selections "
               "   SET primary_provider_transcript_id = %s WHERE lecture_id = %s",
               (variant, lecture["lecture_id"]))
    seed_render(db, lecture, session_id=variant, label="second")
    return lecture, canonical, variant, meeting


def test_B_an_owned_row_under_the_old_spelling_is_the_target(db):
    lecture, canonical, variant, meeting = _publish_then_reserialize(db, 20)
    for mode in (DRY_RUN, PRODUCTION_NEW_ONLY):
        row = plan(db, lecture, mode)
        assert row["same_occurrence_verdict"] == OWNED_SAME_OCCURRENCE
        assert row["legacy_session_id"] == canonical
        assert row["decision"] != WOULD_INSERT
    assert [r[0] for r in legacy_rows(db, meeting)] == [canonical]


def test_B_an_approved_update_lands_in_place_on_the_published_id(db):
    lecture, canonical, variant, meeting = _publish_then_reserialize(db, 21)
    row = plan(db, lecture, EXPLICIT_BACKFILL, allow_update_existing=True)
    assert row["same_occurrence_verdict"] == OWNED_SAME_OCCURRENCE
    assert row["legacy_session_id"] == canonical
    # still exactly one session row, and its checklist is keyed on that id
    assert [r[0] for r in legacy_rows(db, meeting)] == [canonical]
    keys = [r[0] for r in db.execute(
        "SELECT session_id_match FROM public.qa_doctors_checklist_items "
        " WHERE session_id = %s ORDER BY checklist_order", (canonical,)).fetchall()]
    assert keys == [f"{canonical}_{order}" for order in range(1, 12)]
    assert db.execute("SELECT count(*) FROM public.qa_doctors_checklist_items "
                      " WHERE session_id = %s", (variant,)).fetchone()[0] == 0
    owners = db.execute("SELECT legacy_session_id FROM public.lecture_qa_legacy_writes "
                        " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchall()
    assert owners == [(canonical,)]


# ---------------------------------------------------------------------------
# C. ambiguous occurrence -> review, no write
# ---------------------------------------------------------------------------

def test_C_two_rows_for_one_occurrence_are_ambiguous_and_never_written(db):
    canonical, variant = graph_id(30, variant=False), graph_id(30, variant=True)
    lecture = seed_lecture(db, transcript_id=canonical, meeting_id="MTG-C30")
    seed_render(db, lecture, session_id=graph_id(31, variant=False))
    # Two legacy rows claim this occurrence: an identity match and an
    # unproven same-meeting row. Neither can be chosen safely.
    seed_legacy_row(db, variant, meeting_id="MTG-C30")
    seed_legacy_row(db, graph_id(32, variant=False), meeting_id="MTG-C30")
    before = legacy_fingerprint(db)
    for mode in (DRY_RUN, PRODUCTION_NEW_ONLY):
        row = plan(db, lecture, mode)
        assert row["same_occurrence_verdict"] == AMBIGUOUS
        assert row["decision"] == BLOCKED_AMBIGUOUS_LEGACY_IDENTITY
    assert legacy_fingerprint(db) == before
    stage = resolver_legacy_stage(db, lecture)
    assert stage["state"] == "REVIEW_REQUIRED"
    assert stage.get("reason") == "LEGACY_IDENTITY_AMBIGUOUS"


def test_C_same_meeting_same_day_without_proof_is_ambiguous(db):
    lecture = seed_lecture(db, transcript_id=graph_id(33, variant=False),
                           meeting_id="MTG-C33")
    seed_render(db, lecture)
    seed_legacy_row(db, graph_id(34, variant=False), meeting_id="MTG-C33")
    before = legacy_fingerprint(db)
    row = plan(db, lecture, PRODUCTION_NEW_ONLY)
    assert row["decision"] == BLOCKED_AMBIGUOUS_LEGACY_IDENTITY
    assert legacy_fingerprint(db) == before


# ---------------------------------------------------------------------------
# D. the writer and the PipelineStateResolver agree
# ---------------------------------------------------------------------------

def _scenario(db, name, n):
    canonical, variant = graph_id(n, variant=False), graph_id(n, variant=True)
    meeting = f"MTG-D{n}"
    lecture = seed_lecture(db, transcript_id=variant, meeting_id=meeting)
    seed_render(db, lecture)
    if name == "foreign_variant":
        seed_legacy_row(db, canonical, meeting_id=meeting)
    elif name == "foreign_exact":
        seed_legacy_row(db, variant, meeting_id=meeting)
    elif name == "ambiguous":
        seed_legacy_row(db, graph_id(n + 500, variant=False), meeting_id=meeting)
    elif name == "other_meeting_same_day":
        seed_legacy_row(db, graph_id(n + 600, variant=False), meeting_id="SOMEONE-ELSE")
    return lecture


@pytest.mark.parametrize("name,expected", [
    ("new", WOULD_INSERT),
    ("other_meeting_same_day", WOULD_INSERT),
    ("foreign_variant", PROTECTED_EXISTING_LEGACY_ROW),
    ("foreign_exact", PROTECTED_EXISTING_LEGACY_ROW),
    ("ambiguous", BLOCKED_AMBIGUOUS_LEGACY_IDENTITY),
])
def test_D_writer_and_resolver_agree_on_the_same_occurrence(db, name, expected):
    n = 40 + ["new", "other_meeting_same_day", "foreign_variant",
              "foreign_exact", "ambiguous"].index(name)
    lecture = _scenario(db, name, n)
    decision = plan(db, lecture, DRY_RUN)["decision"]
    stage = resolver_legacy_stage(db, lecture)
    assert decision == expected
    # An automatic insert is offered by the resolver exactly when the writer
    # would insert: the one question they must never answer differently.
    assert (decision == WOULD_INSERT) == (stage["state"] == "MISSING"), stage
    if expected == PROTECTED_EXISTING_LEGACY_ROW:
        assert stage["state"] == "NOT_APPLICABLE"
        assert stage["reason"] == "LEGACY_ROW_NOT_CODED_OWNED"
    if expected == BLOCKED_AMBIGUOUS_LEGACY_IDENTITY:
        assert stage["state"] == "REVIEW_REQUIRED"


def test_D_the_resolver_sees_an_owned_equivalent_row_as_ours_not_missing(db):
    lecture, canonical, variant, meeting = _publish_then_reserialize(db, 50)
    stage = resolver_legacy_stage(db, lecture)
    assert stage["state"] != "MISSING"
    assert stage.get("legacy_session_id") == canonical
    assert plan(db, lecture, DRY_RUN)["decision"] != WOULD_INSERT


# ---------------------------------------------------------------------------
# F. Perfect v1 can never publish in a write mode
# ---------------------------------------------------------------------------

def _perfect_rows(db):
    return db.execute("SELECT count(*) FROM public.qa_perfect_lectures").fetchone()[0]


@pytest.mark.parametrize("mode", ["CANARY_NEW_ONLY", PRODUCTION_NEW_ONLY, EXPLICIT_BACKFILL])
def test_F_the_scheduler_writer_refuses_perfect_v1_in_every_write_mode(db, mode):
    lecture = seed_lecture(db, transcript_id=graph_id(60, variant=False), meeting_id="MTG-F")
    seed_render(db, lecture)
    before, perfect_before = legacy_fingerprint(db), _perfect_rows(db)
    runner = StageRunner(settings=Settings.from_environment(),
                         perfect_eligibility_version=PERFECT_V1, persist=False)
    with pytest.raises(WriterModeError):
        runner._writer(lecture["lecture_id"], mode=mode, confirmed=True)
    assert legacy_fingerprint(db) == before
    assert _perfect_rows(db) == perfect_before
    assert PERFECT_V1 not in PUBLISHABLE_PERFECT_ELIGIBILITY_VERSIONS


def test_F_a_v1_dry_run_still_reproduces_history_and_writes_nothing(db):
    lecture = seed_lecture(db, transcript_id=graph_id(61, variant=False), meeting_id="MTG-F1")
    seed_render(db, lecture)
    before, perfect_before = legacy_fingerprint(db), _perfect_rows(db)
    runner = StageRunner(settings=Settings.from_environment(),
                         perfect_eligibility_version=PERFECT_V1, persist=False)
    summary = runner._writer(lecture["lecture_id"], mode=DRY_RUN).plan_day(db, DAY)
    assert summary["writes_enabled"] is False
    assert legacy_fingerprint(db) == before and _perfect_rows(db) == perfect_before


def test_F_the_cli_refuses_v1_with_a_write_mode_before_opening_a_connection(isolation_state):
    from app.cli.main import main
    opened = len(isolation_state["connect"].opened)
    code = main(["write-legacy-qa", "--date", DAY.isoformat(),
                 "--mode", "CANARY_NEW_ONLY", "--lecture-id", str(uuid.uuid4()),
                 "--confirm-write", "--perfect-policy", PERFECT_V1])
    assert code != 0
    assert len(isolation_state["connect"].opened) == opened


# ---------------------------------------------------------------------------
# G/H. shadow QA against real inputs: SOURCE_MISSING waits, authoritative works
# ---------------------------------------------------------------------------

class StubProvider:
    def __init__(self):
        self.calls = 0

    def complete_json(self, *, system_message, user_message):
        self.calls += 1
        return {"output": {
            "session_info": {"trainer": "Morgan Trainerfield", "date": DAY.isoformat()},
            "checklist_evaluation": [{"item": item, "status": "Met", "evidence_clips": []}
                                     for item in CHECKLIST_ITEMS],
            "overall_summary": {"strengths": [], "areas_for_improvement": [],
                                "overall_judgement": "Solid session."},
            "ksbs_covered": [],
            "teaching_quality": {"rating_1_5": 4, "comments": "Clear.", "evidence_clips": []},
        }, "provider": "stub", "model_requested": "stub", "model_reported": "stub",
            "response_id": "stub-1", "usage": {}, "attempts": 1}


def qa_service(provider, lecture):
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


@pytest.mark.parametrize("counts,status", [
    (dict(source_rows=0, present_rows=0, members=0), "SOURCE_MISSING"),
    (dict(source_rows=0, present_rows=0, members=0, any_status=3), "SOURCE_PARTIAL_OR_INVALID"),
    (dict(source_rows=4, present_rows=4, members=0), "SOURCE_PARTIAL_OR_INVALID"),
])
def test_G_non_authoritative_attendance_buys_nothing_and_persists_nothing(db, counts, status):
    lecture = seed_qa_inputs(db, **counts)
    provider = StubProvider()
    summary = qa_service(provider, lecture).run_day(db, DAY, execute=True)
    [result] = summary["lectures"]
    assert result["attendance_coverage_status"] == status      # the SQL carried the counts
    assert not is_authoritative(status)
    assert result["qa_status"] == WAITING_FOR_ATTENDANCE_SOURCE
    assert result["persisted"] is False
    assert provider.calls == 0                                  # no model call
    assert _evaluations(db, lecture) == []                      # no evaluation row at all
    assert db.execute("SELECT count(*) FROM public.lecture_qa_generation_attempts "
                      " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchone()[0] == 0


def test_H_authoritative_attendance_still_evaluates_and_persists_normally(db):
    lecture = seed_qa_inputs(db, source_rows=7, present_rows=7, members=7)
    provider = StubProvider()
    summary = qa_service(provider, lecture).run_day(db, DAY, execute=True)
    [result] = summary["lectures"]
    # The same predicate the resolver and Perfect use - not a copy of it.
    assert is_authoritative(result["attendance_coverage_status"])
    assert result["qa_status"] != WAITING_FOR_ATTENDANCE_SOURCE
    assert provider.calls == 1
    assert _evaluations(db, lecture) == [(result["qa_status"],)]
    assert result["qa_status"] == "COMPLETED", result
    items = db.execute("""
        SELECT count(*) FROM public.lecture_qa_checklist_items c
          JOIN public.lecture_qa_evaluations e ON e.evaluation_id = c.evaluation_id
         WHERE e.lecture_id = %s""", (lecture["lecture_id"],)).fetchone()[0]
    assert items == 11


# ---------------------------------------------------------------------------
# I. RC3: a recovered lecture reaches the compatibility table exactly once
# ---------------------------------------------------------------------------

def test_I_the_pass_cap_still_covers_the_whole_stage_chain():
    assert DEFAULT_MAX_PASSES == len(EXECUTABLE_STAGES) + MAX_PASS_MARGIN
    assert DEFAULT_MAX_PASSES >= len(EXECUTABLE_STAGES) + 1


def test_I_a_new_lecture_is_projected_once_and_a_rerun_is_identical(db):
    lecture = seed_lecture(db, transcript_id=graph_id(95, variant=False), meeting_id="MTG-I")
    seed_render(db, lecture)
    first = plan(db, lecture, PRODUCTION_NEW_ONLY)
    assert first["decision"] == WOULD_INSERT
    rows = legacy_rows(db, "MTG-I")
    assert len(rows) == 1 and rows[0][0] == lecture["transcript_id"]
    assert db.execute("SELECT count(*) FROM public.qa_doctors_checklist_items "
                      " WHERE session_id = %s", (rows[0][0],)).fetchone()[0] == 11
    owner = db.execute("SELECT write_status FROM public.lecture_qa_legacy_writes "
                       " WHERE lecture_id = %s", (lecture["lecture_id"],)).fetchall()
    assert owner == [("WRITTEN",)]
    stage = resolver_legacy_stage(db, lecture)
    assert stage["state"] == "COMPLETE"
    again = plan(db, lecture, PRODUCTION_NEW_ONLY)
    assert again["decision"] == WOULD_SKIP_IDENTICAL
    assert legacy_rows(db, "MTG-I") == rows


def test_I_the_projection_leaves_the_clips_and_recording_columns_alone(db):
    lecture = seed_lecture(db, transcript_id=graph_id(96, variant=False), meeting_id="MTG-I2")
    seed_render(db, lecture)
    plan(db, lecture, PRODUCTION_NEW_ONLY)
    db.execute("UPDATE public.qa_doctors_sessions SET recording_url = 'https://keep.invalid', "
               " clips_status = 'done' WHERE meeting_id = 'MTG-I2'")
    db.execute("UPDATE public.lecture_qa_rendered_sessions SET met_count = 9, "
               " source_fingerprint = %s WHERE lecture_id = %s",
               (sha("changed"), lecture["lecture_id"]))
    plan(db, lecture, EXPLICIT_BACKFILL, allow_update_existing=True)
    kept = db.execute("SELECT recording_url, clips_status, met_count FROM "
                      " public.qa_doctors_sessions WHERE meeting_id = 'MTG-I2'").fetchall()
    assert kept == [("https://keep.invalid", "done", 9)]
