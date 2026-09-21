"""
Phase 3C2.3D integration tests: Perfect Lecture parity against the real
database.

Every test either asserts a read-only fact or does its work inside a
transaction that is deliberately rolled back. Nothing here leaves a row in
public.qa_perfect_lectures, public.qa_doctors_sessions or any coded table:
several tests assert that explicitly afterwards.
"""
from datetime import date

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.perfect_lectures import (
    LegacyPerfectLectureRepository,
    PerfectLectureOwnershipRepository,
    PerfectLectureResultRepository,
)
from app.db.repositories.qa_writer import RenderedPayloadRepository
from app.qa.perfect import (
    PENDING_ATTENDANCE_DATA,
    PERFECT_ELIGIBILITY_VERSION,
    PERFECT_ELIGIBILITY_VERSION_V2,
    evaluate,
)
from app.rendering.evidence import RENDERER_VERSION
from app.writer.modes import (
    CANARY_NEW_ONLY,
    DRY_RUN,
    PERFECT_NOT_ELIGIBLE,
    PERFECT_PROTECTED_EXISTING_LEGACY_ROW,
    PERFECT_WOULD_INSERT,
    PERFECT_WOULD_SKIP_IDENTICAL,
    PERFECT_WOULD_UPDATE,
)
from app.writer.mapping import _canonical
from app.writer.perfect_mapping import (
    PERFECT_MAPPING_VERSION,
    PERFECT_WRITER_VERSION,
    perfect_mapped_digest,
    perfect_row,
)
from app.writer.perfect_service import PerfectLecturePlanner


ANDREW = "25e85615-aa7a-5f49-bb40-078d7c7b65d0"
ANDREW_KEY = "2026-09-16|Andrew-Scheduling Professional (SP) Jan 2026"
FIRST_CANARY = "8c2874d7-3db5-5870-ae47-4f745a847540"
TARGET_DATE = date(2026, 9, 16)

# Phase 3C2.3E wrote Andrew to production for real, so the insert/protect/
# collision paths can no longer be exercised on Andrew's own key - it is
# legitimately occupied now. They run instead against a synthetic subject,
# which yields a lecture_key nobody owns while exercising byte-for-byte the
# same mapping and policy code.
SYNTHETIC_SUBJECT = "ZZZ Phase 3C2.3E synthetic subject"
SYNTHETIC_KEY = f"2026-09-16|{SYNTHETIC_SUBJECT}"


def _synthetic(rendered):
    return dict(rendered, subject=SYNTHETIC_SUBJECT)


class _Rollback(Exception):
    """Raised to unwind a transaction that must never commit."""


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _payload(connection, lecture_id):
    repository = RenderedPayloadRepository()
    rows = repository.load_sessions(connection, TARGET_DATE, RENDERER_VERSION)
    row = next(r for r in rows if str(r["lecture_id"]) == lecture_id)
    return row, repository.load_items(connection, row["rendered_session_id"])


def _planner(mode=DRY_RUN, lecture_ids=None, confirmed=False, persist=False,
             eligibility_version=PERFECT_ELIGIBILITY_VERSION):
    """
    Pinned to v1 on purpose.

    This file tests the LEGACY parity policy, and every row it reasons about
    was written under it. Phase 3C3E moved the platform default to
    `kbc_perfect_v2_attendance_required`, so the version these tests always
    meant is now stated rather than inherited - which is the whole reason the
    policy is versioned. The new default has its own coverage in
    tests/integration/test_unattended_readiness.py and
    tests/unit/test_attendance_policy_defaults.py.
    """
    return PerfectLecturePlanner(
        result_repository=PerfectLectureResultRepository(),
        ownership_repository=PerfectLectureOwnershipRepository(),
        legacy_repository=LegacyPerfectLectureRepository(),
        mode=mode, lecture_ids=lecture_ids, confirmed=confirmed,
        eligibility_version=eligibility_version,
        persist_shadow_result=persist)


def test_the_platform_default_policy_is_no_longer_the_one_this_file_pins():
    """Phase 3C3E. Stated here so the pinning above cannot become stale."""
    from app.qa.perfect import DEFAULT_PERFECT_ELIGIBILITY_VERSION
    assert DEFAULT_PERFECT_ELIGIBILITY_VERSION == PERFECT_ELIGIBILITY_VERSION_V2
    assert DEFAULT_PERFECT_ELIGIBILITY_VERSION != PERFECT_ELIGIBILITY_VERSION
    default = PerfectLecturePlanner(
        result_repository=PerfectLectureResultRepository(),
        ownership_repository=PerfectLectureOwnershipRepository(),
        legacy_repository=LegacyPerfectLectureRepository(), mode=DRY_RUN)
    assert default.eligibility_version == PERFECT_ELIGIBILITY_VERSION_V2


# --------------------------------------------------------------------------
# read-only facts
# --------------------------------------------------------------------------

def test_the_migration_added_two_coded_tables_and_changed_nothing_legacy():
    with _connection() as connection:
        for table in ("lecture_perfect_lecture_results",
                      "lecture_perfect_lecture_legacy_writes"):
            assert connection.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)).fetchone()[0]
        # qa_perfect_lectures keeps exactly the shape legacy expects.
        columns = [row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            " WHERE table_schema='public' AND table_name='qa_perfect_lectures' "
            " ORDER BY ordinal_position").fetchall()]
        assert columns == ["id", "lecture_key", "session_date", "subject", "module",
                           "trainer", "engagement", "attended_count", "met_count",
                           "recording_url", "recap_url", "detected_at", "meeting_id",
                           "session_id", "excel_synced_at"]
        triggers = connection.execute(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid "
            " WHERE r.relname='qa_perfect_lectures' AND NOT t.tgisinternal").fetchone()[0]
        assert triggers == 0
        connection.rollback()


def test_andrew_is_perfect_in_shadow():
    with _connection() as connection:
        rendered, items = _payload(connection, ANDREW)
        facts = evaluate(rendered, items)
        assert facts["is_perfect"] is True
        assert facts["reason"] == "ELIGIBLE"
        assert (facts["met_count"], facts["partial_count"], facts["not_met_count"]) \
            == (11, 0, 0)
        assert facts["checklist_row_count"] == 11
        assert facts["distinct_order_count"] == 11
        assert facts["cancelled_session"] is False
        assert perfect_row(rendered)["lecture_key"] == ANDREW_KEY
        connection.rollback()


def test_the_first_production_canary_is_not_perfect_and_has_no_legacy_row():
    with _connection() as connection:
        rendered, items = _payload(connection, FIRST_CANARY)
        facts = evaluate(rendered, items)
        assert facts["is_perfect"] is False
        assert (facts["met_count"], facts["partial_count"], facts["not_met_count"]) \
            == (10, 0, 1)
        plan = _planner().plan_one(connection, rendered, items)
        assert plan["perfect_decision"] == PERFECT_NOT_ELIGIBLE
        # And legacy agrees: the row it would have keyed does not exist.
        assert connection.execute(
            "SELECT count(*) FROM public.qa_perfect_lectures WHERE lecture_key = %s",
            (plan["legacy_lecture_key"],)).fetchone()[0] == 0
        connection.rollback()


def test_the_andrew_canary_is_written_exactly_once_and_is_coded_owned():
    """
    Phase 3C2.3D asserted Andrew had no rows; Phase 3C2.3E wrote them on
    purpose. What still has to hold is that the canary exists exactly once,
    is owned by the coded platform, and never doubled.
    """
    with _connection() as connection:
        session_id, = connection.execute(
            "SELECT session_id FROM public.lecture_qa_rendered_sessions WHERE lecture_id = %s",
            (ANDREW,)).fetchone()
        counts = {
            "session": ("public.qa_doctors_sessions", "session_id", session_id, 1),
            "checklist": ("public.qa_doctors_checklist_items", "session_id", session_id, 11),
            "perfect": ("public.qa_perfect_lectures", "lecture_key", ANDREW_KEY, 1),
            "qa_ownership": ("public.lecture_qa_legacy_writes", "lecture_id", ANDREW, 1),
            "perfect_ownership": ("public.lecture_perfect_lecture_legacy_writes",
                                  "lecture_id", ANDREW, 1),
        }
        for label, (table, column, value, expected) in counts.items():
            actual = connection.execute(
                f"SELECT count(*) FROM {table} WHERE {column} = %s", (value,)).fetchone()[0]
            assert actual == expected, (label, actual, expected)
        # Exactly one result UNDER v1. Andrew also carries a v2 answer, because
        # the attendance-aware policy is what new work gets - that coexistence
        # is the point of versioning eligibility, not a duplicate.
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_perfect_lecture_results "
            " WHERE lecture_id = %s AND eligibility_version = %s",
            (ANDREW, PERFECT_ELIGIBILITY_VERSION)).fetchone()[0] == 1
        # Owned, not adopted: both audit rows say the coded writer created them.
        assert connection.execute(
            "SELECT write_status FROM public.lecture_qa_legacy_writes WHERE lecture_id = %s",
            (ANDREW,)).fetchone()[0] == "WRITTEN"
        # WRITTEN on creation, UPDATED after the Phase 3C2.3F mapping repair -
        # either way the coded platform owns it.
        assert connection.execute(
            "SELECT write_status FROM public.lecture_perfect_lecture_legacy_writes "
            " WHERE lecture_id = %s", (ANDREW,)).fetchone()[0] in ("WRITTEN", "UPDATED")
        connection.rollback()


def test_the_written_perfect_row_leaves_the_foreign_columns_alone():
    with _connection() as connection:
        recording_url, recap_url, excel_synced_at = connection.execute(
            "SELECT recording_url, recap_url, excel_synced_at "
            "  FROM public.qa_perfect_lectures WHERE lecture_key = %s",
            (ANDREW_KEY,)).fetchone()
        assert recording_url is None and recap_url is None
        # excel_synced_at may legitimately be set later by the active Excel
        # Sync workflow, but only once a recording_url exists.
        if excel_synced_at is not None:
            assert recording_url is not None
        connection.rollback()


def test_every_ineligible_production_row_is_explained_by_the_older_rule():
    """
    79 of the 140 live rows do not satisfy today's eleven-item rule. All of
    them have twelve checklist rows, i.e. they were correct under the rule of
    their day - which is exactly why the coded answer is versioned rather than
    treated as the one eternal truth.
    """
    with _connection() as connection:
        unexplained = connection.execute("""
        WITH agg AS (
          SELECT s.session_id, count(ci.*) AS rows,
                 count(*) FILTER (WHERE ci.status='Met') AS met, s.met_count,
                 lower(coalesce(s.cancelled_session,'false')) AS cancelled
            FROM public.qa_doctors_sessions s
            LEFT JOIN public.qa_doctors_checklist_items ci ON ci.session_id=s.session_id
           GROUP BY s.session_id, s.met_count, s.cancelled_session)
        SELECT count(*) FROM public.qa_perfect_lectures p
          LEFT JOIN agg a ON a.session_id = p.session_id
         WHERE (a.session_id IS NULL
                OR NOT (a.rows=11 AND a.met=11 AND a.met_count=11 AND a.cancelled<>'true'))
           AND coalesce(a.rows, -1) <> 12
        """).fetchone()[0]
        assert unexplained == 0
        connection.rollback()


def test_the_lecture_key_collision_hazard_is_real_and_measured():
    with _connection() as connection:
        collisions = connection.execute("""
        SELECT count(*) FROM (
          SELECT s.date::date AS d, s.subject FROM public.qa_doctors_sessions s
           GROUP BY 1,2 HAVING count(DISTINCT s.session_id) > 1) t
        """).fetchone()[0]
        # One pair exists today. If this ever reaches zero the guard is still
        # correct; if it grows, the guard is what stops a cross-write.
        assert collisions >= 1
        # Andrew's key is not one of them.
        assert connection.execute("""
        SELECT count(DISTINCT session_id) FROM public.qa_doctors_sessions
         WHERE date::date = %s AND subject = %s
        """, (TARGET_DATE, ANDREW_KEY.split("|", 1)[1])).fetchone()[0] <= 1
        connection.rollback()


# --------------------------------------------------------------------------
# dry run
# --------------------------------------------------------------------------

def test_the_dry_run_reports_a_perfect_action_and_writes_nothing():
    with _connection() as connection:
        before = connection.execute(
            "SELECT count(*) FROM public.qa_perfect_lectures").fetchone()[0]
        results_before = connection.execute(
            "SELECT count(*) FROM public.lecture_perfect_lecture_results").fetchone()[0]
        rendered, items = _payload(connection, ANDREW)

        # Andrew is written and coded-owned, so the honest decision is now a
        # no-op rather than an insert.
        written = _planner(lecture_ids=[ANDREW]).plan_one(connection, rendered, items)
        assert written["perfect_decision"] == PERFECT_WOULD_SKIP_IDENTICAL
        assert written["is_perfect"] is True
        assert written["coded_owned"] is True

        # The insert decision still has to be reachable, on a key nobody owns.
        fresh = _planner().plan_one(connection, _synthetic(rendered), items)
        assert fresh["perfect_decision"] == PERFECT_WOULD_INSERT
        assert fresh["recording_url_written"] is False
        assert fresh["lecture_key_collision"] is False

        assert connection.execute(
            "SELECT count(*) FROM public.qa_perfect_lectures").fetchone()[0] == before
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_perfect_lecture_results"
        ).fetchone()[0] == results_before
        connection.rollback()


# --------------------------------------------------------------------------
# write behaviour, always inside a rolled-back transaction
# --------------------------------------------------------------------------

def test_a_real_write_is_idempotent_and_leaves_no_trace_after_rollback():
    with _connection() as connection:
        rendered, items = _payload(connection, ANDREW)
        rendered = _synthetic(rendered)
        planner = _planner(mode=CANARY_NEW_ONLY, lecture_ids=[ANDREW], confirmed=True)
        try:
            with connection.transaction():
                first = planner.plan_one(connection, rendered, items)
                assert first["perfect_decision"] == PERFECT_WOULD_INSERT
                assert first["perfect_write_status"] == "WRITTEN"
                row = connection.execute(
                    "SELECT met_count, session_id, meeting_id, recording_url, "
                    "       excel_synced_at, module, trainer "
                    "  FROM public.qa_perfect_lectures WHERE lecture_key = %s",
                    (SYNTHETIC_KEY,)).fetchone()
                assert row is not None
                assert row[0] == 11
                assert row[1] == rendered["session_id"]
                # Recording and Excel columns are left to their owners.
                assert row[3] is None and row[4] is None

                # A second identical run must do nothing at all.
                second = planner.plan_one(connection, rendered, items)
                assert second["perfect_decision"] == PERFECT_WOULD_SKIP_IDENTICAL
                assert connection.execute(
                    "SELECT count(*) FROM public.qa_perfect_lectures "
                    " WHERE lecture_key = %s", (SYNTHETIC_KEY,)).fetchone()[0] == 1
                assert connection.execute(
                    "SELECT count(*) FROM public.lecture_perfect_lecture_legacy_writes"
                    " WHERE legacy_lecture_key = %s", (SYNTHETIC_KEY,)).fetchone()[0] == 1
                raise _Rollback
        except _Rollback:
            pass
        assert connection.execute(
            "SELECT count(*) FROM public.qa_perfect_lectures WHERE lecture_key = %s",
            (SYNTHETIC_KEY,)).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_perfect_lecture_legacy_writes"
            " WHERE legacy_lecture_key = %s", (SYNTHETIC_KEY,)).fetchone()[0] == 0
        connection.rollback()


def test_a_recording_url_written_by_another_workflow_survives_our_update():
    with _connection() as connection:
        rendered, items = _payload(connection, ANDREW)
        rendered = _synthetic(rendered)
        planner = _planner(mode=CANARY_NEW_ONLY, lecture_ids=[ANDREW], confirmed=True)
        try:
            with connection.transaction():
                planner.plan_one(connection, rendered, items)
                # The Master's recording branch does its job.
                connection.execute(
                    "UPDATE public.qa_perfect_lectures "
                    "   SET recording_url = %s, excel_synced_at = now() "
                    " WHERE lecture_key = %s",
                    ("https://example.invalid/recording", SYNTHETIC_KEY))
                # A later QA correction changes the fingerprint but stays perfect.
                changed = dict(rendered, source_fingerprint="b" * 64)
                again = planner.plan_one(connection, changed, items)
                assert again["perfect_decision"] == PERFECT_WOULD_UPDATE
                row = connection.execute(
                    "SELECT recording_url, excel_synced_at IS NOT NULL "
                    "  FROM public.qa_perfect_lectures WHERE lecture_key = %s",
                    (SYNTHETIC_KEY,)).fetchone()
                assert row[0] == "https://example.invalid/recording"
                assert row[1] is True
                # And rollback now refuses to delete it.
                outcome = planner.rollback_owned_row(connection, SYNTHETIC_KEY)
                assert outcome["status"] == "BLOCKED_DOWNSTREAM_DEPENDENCY"
                assert set(outcome["downstream_dependencies"]) == {
                    "RECORDING_URL_POPULATED", "EXCEL_SYNCED"}
                raise _Rollback
        except _Rollback:
            pass
        assert connection.execute(
            "SELECT count(*) FROM public.qa_perfect_lectures WHERE lecture_key = %s",
            (SYNTHETIC_KEY,)).fetchone()[0] == 0
        connection.rollback()


def test_a_legacy_owned_row_under_our_key_is_protected():
    with _connection() as connection:
        rendered, items = _payload(connection, ANDREW)
        rendered = _synthetic(rendered)
        planner = _planner(mode=CANARY_NEW_ONLY, lecture_ids=[ANDREW], confirmed=True)
        try:
            with connection.transaction():
                # Legacy n8n got there first, with the SAME session_id, so this
                # is ownership rather than a key collision.
                connection.execute("""
                INSERT INTO public.qa_perfect_lectures
                       (lecture_key, session_date, subject, met_count, session_id)
                VALUES (%s, %s, %s, 11, %s)
                """, (SYNTHETIC_KEY, TARGET_DATE, SYNTHETIC_SUBJECT,
                      rendered["session_id"]))
                plan = planner.plan_one(connection, rendered, items)
                assert plan["perfect_decision"] == PERFECT_PROTECTED_EXISTING_LEGACY_ROW
                assert plan.get("perfect_write_status") is None
                # Untouched: still exactly the row legacy inserted.
                assert connection.execute(
                    "SELECT trainer, module FROM public.qa_perfect_lectures "
                    " WHERE lecture_key = %s", (SYNTHETIC_KEY,)).fetchone() == (None, None)
                raise _Rollback
        except _Rollback:
            pass
        connection.rollback()


def test_a_key_already_pointing_at_another_session_blocks_the_write():
    with _connection() as connection:
        rendered, items = _payload(connection, ANDREW)
        rendered = _synthetic(rendered)
        planner = _planner(mode=CANARY_NEW_ONLY, lecture_ids=[ANDREW], confirmed=True)
        try:
            with connection.transaction():
                connection.execute("""
                INSERT INTO public.qa_perfect_lectures
                       (lecture_key, session_date, subject, met_count, session_id)
                VALUES (%s, %s, %s, 11, %s)
                """, (SYNTHETIC_KEY, TARGET_DATE, SYNTHETIC_SUBJECT,
                      "SOMEONE-ELSES-SESSION"))
                plan = planner.plan_one(connection, rendered, items)
                assert plan["perfect_decision"] == "PERFECT_BLOCKED_KEY_COLLISION"
                assert plan["lecture_key_collision"] is True
                assert connection.execute(
                    "SELECT session_id FROM public.qa_perfect_lectures "
                    " WHERE lecture_key = %s",
                    (SYNTHETIC_KEY,)).fetchone()[0] == "SOMEONE-ELSES-SESSION"
                raise _Rollback
        except _Rollback:
            pass
        connection.rollback()


def test_perfect_to_not_perfect_supersedes_without_deleting():
    with _connection() as connection:
        rendered, items = _payload(connection, ANDREW)
        rendered = _synthetic(rendered)
        planner = _planner(mode=CANARY_NEW_ONLY, lecture_ids=[ANDREW], confirmed=True)
        try:
            with connection.transaction():
                planner.plan_one(connection, rendered, items)
                # A later QA correction turns one item Not Met.
                degraded_items = [dict(item) for item in items]
                degraded_items[-1]["status"] = "Not Met"
                degraded = dict(rendered, met_count=10, not_met_count=1,
                                source_fingerprint="c" * 64)
                plan = planner.plan_one(connection, degraded, degraded_items)
                assert plan["perfect_decision"] == "PERFECT_SUPERSEDED_NOT_PERFECT"
                assert plan["legacy_row_deleted"] is False
                # The legacy row is still standing.
                assert connection.execute(
                    "SELECT count(*) FROM public.qa_perfect_lectures "
                    " WHERE lecture_key = %s", (SYNTHETIC_KEY,)).fetchone()[0] == 1
                # And the divergence is recorded rather than resolved.
                status, = connection.execute(
                    "SELECT write_status FROM public.lecture_perfect_lecture_legacy_writes"
                    " WHERE legacy_lecture_key = %s", (SYNTHETIC_KEY,)).fetchone()
                assert status == "SUPERSEDED_NOT_PERFECT"
                is_perfect, = connection.execute(
                    "SELECT is_perfect FROM public.lecture_perfect_lecture_results "
                    " WHERE lecture_id = %s AND eligibility_version = %s",
                    (ANDREW, PERFECT_ELIGIBILITY_VERSION)).fetchone()
                assert is_perfect is False
                raise _Rollback
        except _Rollback:
            pass
        connection.rollback()


def test_not_perfect_to_perfect_records_the_transition_in_provenance():
    with _connection() as connection:
        rendered, items = _payload(connection, FIRST_CANARY)
        planner = _planner(lecture_ids=[FIRST_CANARY], persist=True)
        # Phase 3C3B persists an answer for every finalized render, so this
        # lecture already has one. What must survive the rollback is that
        # baseline, not an empty table.
        baseline = connection.execute(
            "SELECT count(*) FROM public.lecture_perfect_lecture_results "
            " WHERE lecture_id = %s", (FIRST_CANARY,)).fetchone()[0]
        try:
            with connection.transaction():
                first = planner.plan_one(connection, rendered, items)
                assert first["is_perfect"] is False
                # The same lecture later passes every item.
                fixed_items = [dict(item, status="Met") for item in items]
                fixed = dict(rendered, met_count=11, not_met_count=0,
                             source_fingerprint="d" * 64)
                second = planner.plan_one(connection, fixed, fixed_items)
                assert second["is_perfect"] is True
                assert second["perfect_decision"] == PERFECT_WOULD_INSERT
                metadata, = connection.execute(
                    "SELECT metadata FROM public.lecture_perfect_lecture_results "
                    " WHERE lecture_id = %s AND eligibility_version = %s",
                    (FIRST_CANARY, PERFECT_ELIGIBILITY_VERSION)).fetchone()
                transitions = metadata["transitions"]
                assert transitions[-1]["from_is_perfect"] is False
                assert transitions[-1]["to_is_perfect"] is True
                # Still a dry run: no legacy row was created.
                assert connection.execute(
                    "SELECT count(*) FROM public.qa_perfect_lectures "
                    " WHERE lecture_key = %s",
                    (second["legacy_lecture_key"],)).fetchone()[0] == 0
                raise _Rollback
        except _Rollback:
            pass
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_perfect_lecture_results "
            " WHERE lecture_id = %s", (FIRST_CANARY,)).fetchone()[0] == baseline
        connection.rollback()


def test_the_shadow_result_is_deterministic_and_recomputable_without_ai():
    """
    Recomputing Andrew's answer from the frozen payload must land on the same
    deterministic result_id and the same fingerprint as the row the real
    canary wrote - that is what makes a lost Perfect Lecture row repairable
    without re-running the model.
    """
    with _connection() as connection:
        rendered, items = _payload(connection, ANDREW)
        planner = _planner(lecture_ids=[ANDREW], persist=True)
        try:
            with connection.transaction():
                first = planner.plan_one(connection, rendered, items)
                second = planner.plan_one(connection, rendered, items)
                assert first["result_id"] == second["result_id"]
                assert connection.execute(
                    "SELECT count(*) FROM public.lecture_perfect_lecture_results "
                    " WHERE lecture_id = %s AND eligibility_version = %s",
                    (ANDREW, PERFECT_ELIGIBILITY_VERSION)).fetchone()[0] == 1
                stored, = connection.execute(
                    "SELECT source_fingerprint FROM public.lecture_perfect_lecture_results"
                    " WHERE lecture_id = %s AND eligibility_version = %s",
                    (ANDREW, PERFECT_ELIGIBILITY_VERSION)).fetchone()
                assert stored == rendered["source_fingerprint"]
                raise _Rollback
        except _Rollback:
            pass
        connection.rollback()


def test_the_combined_canary_plan_reports_both_targets():
    from app.db.repositories.qa_writer import (
        LegacyQaTargetRepository,
        WriterOwnershipRepository,
    )
    from app.writer.service import LegacyQaWriter
    with _connection() as connection:
        writer = LegacyQaWriter(
            payload_repository=RenderedPayloadRepository(),
            legacy_repository=LegacyQaTargetRepository(),
            ownership_repository=WriterOwnershipRepository(),
            mode=DRY_RUN, lecture_ids=[ANDREW],
            perfect_planner=_planner(lecture_ids=[ANDREW]))
        summary = writer.plan_day(connection, TARGET_DATE)
        assert summary["lectures_considered"] == 1
        assert summary["perfect_lecture_planned"] is True
        # Andrew is written and coded-owned, so both targets are now no-ops -
        # and a dry run still writes nothing either way.
        assert summary["would_skip_identical"] == 1
        assert summary["perfect_would_skip_identical"] == 1
        assert summary["sessions_written"] == 0
        assert summary["perfect_rows_written"] == 0
        lecture = summary["lectures"][0]
        assert lecture["proposed_checklist_rows"] == 11
        assert lecture["perfect_lecture"]["perfect_decision"] == PERFECT_WOULD_SKIP_IDENTICAL
        assert lecture["perfect_lecture"]["perfect_writer_version"] \
            == PERFECT_WRITER_VERSION
        connection.rollback()


def test_the_rendered_loader_supplies_every_field_the_perfect_mapping_reads():
    """
    Phase 3C2.3E regression.

    The real defect this catches: `perfect_row` read `attended_count` with
    `.get()`, but `RenderedPayloadRepository.LOAD_RENDERED` never selected
    that column, so the mapping silently produced NULL and Andrew's first
    production Perfect Lecture row was written with attended_count = NULL
    instead of 7. The unit tests all passed because they built the rendered
    dict by hand and supplied the key.

    Comparing the loader against the rendered table directly is what closes
    it: a mapped column that the loader does not fetch can never again reach
    production as a silent NULL.
    """
    with _connection() as connection:
        rendered, _ = _payload(connection, ANDREW)
        stored = connection.execute("""
        SELECT attended_count, engagement, met_count, trainer, lms_module, meeting_id,
               session_id, legacy_date, subject
          FROM public.lecture_qa_rendered_sessions WHERE lecture_id = %s
        """, (ANDREW,)).fetchone()
        names = ("attended_count", "engagement", "met_count", "trainer", "lms_module",
                 "meeting_id", "session_id", "legacy_date", "subject")
        for name, value in zip(names, stored):
            assert name in rendered, f"the loader does not fetch {name}"
            assert rendered[name] == value, (name, rendered[name], value)

        mapped = perfect_row(rendered)
        assert mapped["attended_count"] == 7
        # Every coded-owned column that has a value in the rendered row must
        # survive the mapping; a None here means a dropped column.
        for column in ("attended_count", "engagement", "met_count", "trainer",
                       "module", "meeting_id", "session_id", "session_date", "subject"):
            assert mapped[column] is not None, column
        connection.rollback()


def test_the_written_perfect_row_is_compared_against_the_corrected_mapping():
    """
    The row Andrew's canary wrote predates the loader fix, so it still holds
    attended_count = NULL. This records the divergence explicitly rather than
    letting it pass unnoticed, and will start failing the moment the row is
    repaired - at which point the assertion should be tightened to equality.
    """
    with _connection() as connection:
        rendered, _ = _payload(connection, ANDREW)
        expected = perfect_row(rendered)
        actual = connection.execute(
            "SELECT attended_count FROM public.qa_perfect_lectures WHERE lecture_key = %s",
            (ANDREW_KEY,)).fetchone()[0]
        assert expected["attended_count"] == 7
        assert actual in (None, 7), actual
        if actual is None:
            # Known, reported, and the only field that differs.
            # _canonical is the same normaliser the writer verifies with: the
            # legacy column is a date while the rendered value is TEXT, and
            # numeric scale differs, so a raw != would report false drift.
            differing = [column for column in ("lecture_key", "session_date", "subject",
                                               "module", "trainer", "engagement",
                                               "met_count", "meeting_id", "session_id")
                         if _canonical(connection.execute(
                             f'SELECT "{column}" FROM public.qa_perfect_lectures '
                             " WHERE lecture_key = %s", (ANDREW_KEY,)).fetchone()[0])
                         != _canonical(expected[column])]
            assert differing == [], differing
        connection.rollback()


# --------------------------------------------------------------------------
# Phase 3C2.3F: mapping-aware idempotency against the real database
# --------------------------------------------------------------------------

def test_andrew_carries_the_repaired_attended_count_and_its_provenance():
    with _connection() as connection:
        rendered, _ = _payload(connection, ANDREW)
        expected = perfect_row(rendered)
        live = connection.execute(
            "SELECT attended_count, recording_url, recap_url, excel_synced_at "
            "  FROM public.qa_perfect_lectures WHERE lecture_key = %s",
            (ANDREW_KEY,)).fetchone()
        assert expected["attended_count"] == 7
        assert live[0] == 7
        # The repair owned attended_count and nothing else.
        assert live[1] is None and live[2] is None and live[3] is None

        status, metadata = connection.execute(
            "SELECT write_status, metadata "
            "  FROM public.lecture_perfect_lecture_legacy_writes WHERE lecture_id = %s",
            (ANDREW,)).fetchone()
        assert status == "UPDATED"
        assert metadata["mapping_version"] == PERFECT_MAPPING_VERSION
        assert metadata["mapped_digest"] == perfect_mapped_digest(expected)
        # The evidence that the first write carried the OLD digest survives.
        repairs = metadata["repairs"]
        assert len(repairs) == 1
        assert repairs[0]["reason"] == "MAPPING_OUTPUT_CHANGED"
        assert repairs[0]["previous_post_write_digest"] != repairs[0]["new_post_write_digest"]
        connection.rollback()


def test_the_repair_did_not_duplicate_ownership_or_the_result():
    with _connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_perfect_lecture_legacy_writes "
            " WHERE lecture_id = %s", (ANDREW,)).fetchone()[0] == 1
        # One result under v1. A v2 answer beside it is the versioned policy
        # working, not the repair duplicating anything.
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_perfect_lecture_results "
            " WHERE lecture_id = %s AND eligibility_version = %s",
            (ANDREW, PERFECT_ELIGIBILITY_VERSION)).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM public.qa_perfect_lectures WHERE lecture_key = %s",
            (ANDREW_KEY,)).fetchone()[0] == 1
        connection.rollback()


def test_andrew_is_now_a_noop_on_both_targets():
    """Gate F closed: proposed mapped digest equals the target's and the audit's."""
    with _connection() as connection:
        rendered, items = _payload(connection, ANDREW)
        plan = _planner(lecture_ids=[ANDREW]).plan_one(connection, rendered, items)
        assert plan["perfect_decision"] == PERFECT_WOULD_SKIP_IDENTICAL
        assert plan["perfect_update_reason"] is None
        assert plan["source_fingerprint_matches"] is True
        assert plan["mapped_output_matches"] is True
        assert plan["proposed_mapped_digest"] == plan["target_mapped_digest"]
        assert plan["proposed_mapped_digest"] == plan["recorded_mapped_digest"]
        assert plan["field_diff"] == []
        connection.rollback()


def test_a_mapping_change_reopens_the_update_even_with_a_frozen_source():
    """
    The generic proof, on the live coded-owned row: hold the source fingerprint
    constant, move only the mapping version, and the decision must flip from
    no-op to update.
    """
    with _connection() as connection:
        rendered, items = _payload(connection, ANDREW)
        drifted = PerfectLecturePlanner(
            result_repository=PerfectLectureResultRepository(),
            ownership_repository=PerfectLectureOwnershipRepository(),
            legacy_repository=LegacyPerfectLectureRepository(),
            mode=DRY_RUN, mapping_version="legacy_qa_v8_perfect_mapping_v_future")
        plan = drifted.plan_one(connection, rendered, items)
        assert plan["source_fingerprint_matches"] is True
        # The mapped VALUES are unchanged, so the target still agrees; it is
        # the recorded contract version that no longer does.
        assert plan["target_mapped_digest_agrees"] is True
        assert plan["recorded_mapped_digest_agrees"] is False
        assert plan["perfect_decision"] == PERFECT_WOULD_UPDATE
        assert plan["perfect_update_reason"] == "MAPPING_OUTPUT_CHANGED"
        connection.rollback()


def test_a_recording_enrichment_alone_never_reopens_the_write():
    """
    A recording_url or excel_synced_at appearing after the write is another
    system doing its job. It must not read as drift - otherwise the writer
    would rewrite the row forever.
    """
    with _connection() as connection:
        rendered, items = _payload(connection, ANDREW)
        planner = _planner(lecture_ids=[ANDREW])
        try:
            with connection.transaction():
                connection.execute(
                    "UPDATE public.qa_perfect_lectures "
                    "   SET recording_url = %s, excel_synced_at = now() "
                    " WHERE lecture_key = %s",
                    ("https://example.invalid/recording", ANDREW_KEY))
                plan = planner.plan_one(connection, rendered, items)
                assert plan["perfect_decision"] == PERFECT_WOULD_SKIP_IDENTICAL
                assert plan["mapped_output_matches"] is True
                assert plan["foreign_fields"]["recording_url"] \
                    == "https://example.invalid/recording"
                assert plan["foreign_fields"]["excel_synced_at"] is not None
                raise _Rollback
        except _Rollback:
            pass
        assert connection.execute(
            "SELECT recording_url, excel_synced_at FROM public.qa_perfect_lectures "
            " WHERE lecture_key = %s", (ANDREW_KEY,)).fetchone() == (None, None)
        connection.rollback()


def test_no_other_coded_written_perfect_row_carries_the_attended_count_bug():
    with _connection() as connection:
        rows = connection.execute(
            "SELECT lecture_id, legacy_lecture_key "
            "  FROM public.lecture_perfect_lecture_legacy_writes").fetchall()
        assert len(rows) >= 1
        for lecture_id, lecture_key in rows:
            live = connection.execute(
                "SELECT attended_count FROM public.qa_perfect_lectures "
                " WHERE lecture_key = %s", (lecture_key,)).fetchone()
            source = connection.execute(
                "SELECT attended_count FROM public.lecture_qa_rendered_sessions "
                " WHERE lecture_id = %s", (lecture_id,)).fetchone()
            assert live is not None and source is not None, lecture_key
            assert live[0] == source[0], (lecture_key, live[0], source[0])
        connection.rollback()


# --------------------------------------------------------------------------
# Phase 3C3B: eligibility is persisted for EVERY finalized render
# --------------------------------------------------------------------------

def test_every_finalized_render_has_a_persisted_eligibility_result():
    """
    The pilot's observability gap: a NOT_ELIGIBLE lecture used to leave no row,
    so Operations could not tell "not eligible" from "never computed".

    Phase 3C3D made the policy versioned, so the invariant is "under SOME
    policy version" rather than "under v1". Pinning v1 specifically would now
    mean a lecture processed under the attendance-aware policy counted as
    unobserved, which is the opposite of what this test is for.
    """
    with _connection() as connection:
        missing = connection.execute("""
        SELECT count(*) FROM public.lecture_qa_rendered_sessions rs
          JOIN public.lecture_qa_evaluations e ON e.evaluation_id = rs.evaluation_id
         WHERE rs.render_status IN ('RENDERED', 'RENDERED_NON_DELIVERED')
           AND e.qa_status IN ('COMPLETED', 'NON_DELIVERED')
           AND NOT EXISTS (
             SELECT 1 FROM public.lecture_perfect_lecture_results r
              WHERE r.lecture_id = rs.lecture_id)
        """).fetchone()[0]
        assert missing == 0
        connection.rollback()


def test_the_v1_pilot_day_is_still_fully_covered_by_the_v1_policy():
    """And 2026-09-16, processed entirely under v1, must stay so."""
    with _connection() as connection:
        missing = connection.execute("""
        SELECT count(*) FROM public.lecture_qa_rendered_sessions rs
          JOIN public.lecture_qa_evaluations e ON e.evaluation_id = rs.evaluation_id
          JOIN public.lecture_sessions l ON l.lecture_id = rs.lecture_id
         WHERE l.session_date = %s
           AND rs.render_status IN ('RENDERED', 'RENDERED_NON_DELIVERED')
           AND e.qa_status IN ('COMPLETED', 'NON_DELIVERED')
           AND NOT EXISTS (
             SELECT 1 FROM public.lecture_perfect_lecture_results r
              WHERE r.lecture_id = rs.lecture_id
                AND r.eligibility_version = %s)
        """, (TARGET_DATE, PERFECT_ELIGIBILITY_VERSION)).fetchone()[0]
        assert missing == 0
        connection.rollback()


def test_the_pilot_day_has_one_result_per_lecture_both_outcomes_represented():
    with _connection() as connection:
        rows = connection.execute("""
        SELECT r.is_perfect, count(*) FROM public.lecture_perfect_lecture_results r
          JOIN public.lecture_sessions l ON l.lecture_id = r.lecture_id
         WHERE l.session_date = %s AND r.eligibility_version = %s GROUP BY 1
        """, (TARGET_DATE, PERFECT_ELIGIBILITY_VERSION)).fetchall()
        counts = {row[0]: row[1] for row in rows}
        assert sum(counts.values()) == 7
        assert counts.get(True, 0) >= 1 and counts.get(False, 0) >= 1
        connection.rollback()


def test_a_non_eligible_result_has_a_reason_and_no_legacy_row():
    with _connection() as connection:
        rows = connection.execute("""
        SELECT r.lecture_id, r.reason, r.legacy_lecture_key, r.source_fingerprint,
               r.met_count, r.partial_count, r.not_met_count
          FROM public.lecture_perfect_lecture_results r
         WHERE r.is_perfect = false AND r.eligibility_version = %s
        """, (PERFECT_ELIGIBILITY_VERSION,)).fetchall()
        assert rows
        for lecture_id, reason, key, fingerprint, met, partial, not_met in rows:
            assert reason.startswith("NOT_ELIGIBLE") or reason == "INVALID_CHECKLIST_STRUCTURE"
            assert len(fingerprint) == 64
            # The frozen counts travel with the answer.
            assert met is not None and partial is not None and not_met is not None
            # And no legacy Perfect row or ownership was created for it.
            assert connection.execute(
                "SELECT count(*) FROM public.qa_perfect_lectures WHERE lecture_key = %s",
                (key,)).fetchone()[0] == 0
            assert connection.execute(
                "SELECT count(*) FROM public.lecture_perfect_lecture_legacy_writes "
                " WHERE lecture_id = %s", (lecture_id,)).fetchone()[0] == 0
        connection.rollback()


def test_a_pending_attendance_result_never_creates_a_legacy_row_of_its_own():
    """
    Phase 3C3D. PENDING_ATTENDANCE_DATA is not a NOT_ELIGIBLE reason and it
    carries a different invariant: it must never be the CAUSE of a legacy row
    or of Perfect ownership.

    A lecture may still hold a legacy row written earlier under v1 - Martech
    does - and that row stays exactly as it was. What must not exist is
    ownership claimed under the attendance-aware policy.
    """
    with _connection() as connection:
        rows = connection.execute("""
        SELECT r.lecture_id, r.reason, r.legacy_lecture_key
          FROM public.lecture_perfect_lecture_results r
         WHERE r.eligibility_version = %s AND r.reason = %s
        """, (PERFECT_ELIGIBILITY_VERSION_V2, PENDING_ATTENDANCE_DATA)).fetchall()
        assert rows, "the recovery phase must have produced at least one"
        for lecture_id, _reason, key in rows:
            assert connection.execute("""
                SELECT count(*) FROM public.lecture_perfect_lecture_legacy_writes
                 WHERE lecture_id = %s AND eligibility_version = %s""",
                (lecture_id, PERFECT_ELIGIBILITY_VERSION_V2)).fetchone()[0] == 0
            # And it is never recorded as perfect.
            assert connection.execute("""
                SELECT is_perfect FROM public.lecture_perfect_lecture_results
                 WHERE lecture_id = %s AND eligibility_version = %s""",
                (lecture_id, PERFECT_ELIGIBILITY_VERSION_V2)).fetchone()[0] is False
        connection.rollback()


def test_persisting_the_same_answer_twice_moves_no_timestamp():
    """
    A true NOOP. A computed_at that advanced on every rerun would reintroduce
    exactly the "what did this run change?" problem that lecture-scoped
    engagement was built to remove.
    """
    with _connection() as connection:
        rendered, items = _payload(connection, ANDREW)
        planner = _planner(lecture_ids=[ANDREW], persist=True)
        before = connection.execute(
            "SELECT result_id, computed_at, updated_at, source_fingerprint "
            "  FROM public.lecture_perfect_lecture_results "
            " WHERE lecture_id = %s AND eligibility_version = %s",
            (ANDREW, PERFECT_ELIGIBILITY_VERSION)).fetchone()
        assert before is not None
        try:
            with connection.transaction():
                outcome = planner.plan_one(connection, rendered, items)
                assert outcome["shadow_result_created"] is False
                after = connection.execute(
                    "SELECT result_id, computed_at, updated_at, source_fingerprint "
                    "  FROM public.lecture_perfect_lecture_results "
                    " WHERE lecture_id = %s AND eligibility_version = %s",
                    (ANDREW, PERFECT_ELIGIBILITY_VERSION)).fetchone()
                assert after == before
                assert connection.execute(
                    "SELECT count(*) FROM public.lecture_perfect_lecture_results "
                    " WHERE lecture_id = %s AND eligibility_version = %s",
                    (ANDREW, PERFECT_ELIGIBILITY_VERSION)).fetchone()[0] == 1
                raise _Rollback
        except _Rollback:
            pass
        connection.rollback()


def test_a_dry_run_without_persist_still_writes_nothing():
    with _connection() as connection:
        before = connection.execute(
            "SELECT count(*) FROM public.lecture_perfect_lecture_results").fetchone()[0]
        rendered, items = _payload(connection, ANDREW)
        _planner(lecture_ids=[ANDREW], persist=False).plan_one(connection, rendered, items)
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_perfect_lecture_results"
        ).fetchone()[0] == before
        connection.rollback()


def test_the_two_policies_coexist_on_the_same_lecture_without_colliding():
    """
    Why every count above now names a version.

    Andrew holds an answer under each policy. They are different questions -
    "was this perfect under the legacy rule?" and "is it perfect under the rule
    that requires attendance evidence?" - and the unique key is
    (lecture_id, eligibility_version) precisely so both can be true at once.
    An unqualified count over that table answers neither.
    """
    with _connection() as connection:
        rows = connection.execute(
            "SELECT eligibility_version, count(*) "
            "  FROM public.lecture_perfect_lecture_results "
            " WHERE lecture_id = %s GROUP BY 1", (ANDREW,)).fetchall()
        connection.rollback()
    by_version = dict(rows)
    assert by_version.get(PERFECT_ELIGIBILITY_VERSION) == 1
    assert all(count == 1 for count in by_version.values()), by_version
    assert len(by_version) >= 1
