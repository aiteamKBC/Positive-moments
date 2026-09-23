"""
Phase 3B persistence against the real 2026-09-04 evidence.

Every test runs in a rolled-back transaction. No test calls a model: the
renderer has no provider at all, and that is asserted rather than assumed.
"""
import json
import uuid
from datetime import date

import psycopg
import pytest

from app.db.repositories.qa_rendering import (
    LegacyRenderComparisonRepository,
    LmsSnapshotRepository,
    LmsSourceRepository,
    RenderInputRepository,
    RenderRunRepository,
    RenderedOutputRepository,
)
from app.config.settings import Settings
from app.qa.checklist import CHECKLIST_ITEMS
from app.rendering.evidence import NO_EVIDENCE, format_clips
from app.rendering.service import (
    RENDERED,
    RENDERED_NON_DELIVERED,
    SOURCE_QA_NOT_READY,
    QaRenderingService,
    RenderInputError,
)

# ---------------------------------------------------------------------------
# PRODUCTION-DATA ACCEPTANCE SUITE
#
# Every test in this module asserts behaviour against KBC's real historical
# evidence: named lectures, real session dates, real transcripts, the real
# legacy dataset. It is NOT part of the RC release gate and is deselected by
#     pytest tests/integration -m "not production_data"
# because on a database without that evidence it can only fail or pass
# vacuously - neither of which validates anything.
#
# The contracts in here that never needed real history have been moved to the
# self-contained gate modules (test_pipeline_contracts.py,
# test_platform_invariants.py, test_safety_fixes_integration.py).
#
# To run this suite, an approved acceptance dataset must be configured - never
# production. See docs/audits/QA_CORE_RC4_TEST_GATE_FINAL_2026-09-22.md.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.production_data


TARGET = date(2026, 9, 4)

PROTECTED_TABLES = (
    "kbc_users_data", "kbc_attendance", "qa_doctors_sessions",
    "qa_doctors_checklist_items", "qa_perfect_lectures", "qa_doctors_transcripts",
    "qa_media_jobs", "qa_positive_clip_assets", "qa_lecture_split_plans",
    "qa_lecture_part_assets", "lecture_qa_evaluations", "lecture_qa_checklist_items",
    "lecture_qa_evidence_clips", "lecture_transcript_cues", "lecture_transcript_speakers",
    "lecture_engagement_metrics", "lecture_attendance_snapshots",
)


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


class _Tracing:
    def __init__(self, inner, log):
        self._inner, self._log = inner, log

    def execute(self, statement, *args, **kwargs):
        self._log.append(str(statement))
        return self._inner.execute(statement, *args, **kwargs)

    def executemany(self, statement, *args, **kwargs):
        self._log.append(str(statement))
        return self._inner.executemany(statement, *args, **kwargs)

    def cursor(self, *args, **kwargs):
        return _Tracing(self._inner.cursor(*args, **kwargs), self._log)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _GrownLms(LmsSourceRepository):
    """Real roster plus one invented learner, to simulate LMS drift."""

    def load_students(self, connection, module_raw):
        source = super().load_students(connection, module_raw)
        return {"source_row_count": source["source_row_count"] + 1,
                "members": source["members"] + [
                    {"external_learner_id": -4242, "full_name": "Fixture Lmsdrift"}]}


def _service(*, legacy=True, refresh_lms=False, lms_source=None,
             renderer_version="legacy_qa_v8_renderer_v1"):
    return QaRenderingService(
        input_repository=RenderInputRepository(),
        lms_source_repository=lms_source or LmsSourceRepository(),
        lms_snapshot_repository=LmsSnapshotRepository(),
        output_repository=RenderedOutputRepository(),
        run_repository=RenderRunRepository(),
        legacy_repository=LegacyRenderComparisonRepository() if legacy else None,
        renderer_version=renderer_version, refresh_lms=refresh_lms)


def _require_evaluations(connection):
    ready = connection.execute(
        "SELECT count(*) FROM public.lecture_qa_evaluations "
        " WHERE qa_status IN ('COMPLETED', 'NON_DELIVERED')").fetchone()[0]
    if not ready:
        pytest.skip("no renderable Phase 3A evaluations")


def _counts(connection):
    return {name: connection.execute(f"SELECT count(*) FROM public.{name}").fetchone()[0]
            for name in PROTECTED_TABLES}


# --- 1-2. source gating --------------------------------------------------------

def test_renderer_consumes_completed_and_non_delivered_only():
    with _connection() as connection:
        _require_evaluations(connection)
        summary = _service().render_day(connection, TARGET)
        statuses = {row["evaluation_status"] for row in summary["lectures"]}
        assert statuses <= {"COMPLETED", "NON_DELIVERED"}
        for row in summary["lectures"]:
            assert row["render_status"] in (RENDERED, RENDERED_NON_DELIVERED)
        connection.rollback()


def test_an_unusable_evaluation_is_reported_not_evaluated():
    with _connection() as connection:
        _require_evaluations(connection)
        # Move one evaluation out of a renderable state inside this transaction.
        # Must be an evaluation on THIS test's date: other dates now carry
        # evaluations too, and rendering TARGET would not see them.
        target = connection.execute(
            "SELECT e.evaluation_id FROM public.lecture_qa_evaluations e"
            "  JOIN public.lecture_sessions l ON l.lecture_id = e.lecture_id"
            " WHERE e.qa_status = 'COMPLETED' AND l.session_date = %s"
            " ORDER BY e.evaluation_id LIMIT 1", (TARGET,)).fetchone()[0]
        connection.execute(
            "UPDATE public.lecture_qa_evaluations SET qa_status = 'MODEL_ERROR' "
            " WHERE evaluation_id = %s", (target,))
        summary = _service(legacy=False).render_day(connection, TARGET)
        not_ready = [row for row in summary["lectures"]
                     if row["render_status"] == SOURCE_QA_NOT_READY]
        assert len(not_ready) == 1
        assert summary["not_ready_count"] == 1
        assert summary["provider_calls"] == 0
        connection.rollback()


# --- 3. no provider ------------------------------------------------------------

def test_the_renderer_has_no_provider_and_records_zero_calls():
    with _connection() as connection:
        _require_evaluations(connection)
        service = _service()
        assert not hasattr(service, "provider")
        summary = service.render_day(connection, TARGET)
        assert summary["provider_calls"] == 0
        stored = connection.execute(
            "SELECT provider_calls FROM public.lecture_qa_render_runs "
            " WHERE run_id = %s", (uuid.UUID(summary["run_id"]),)).fetchone()[0]
        assert stored == 0
        connection.rollback()


# --- real rendering -------------------------------------------------------------

def test_every_lecture_renders_eleven_checklist_rows_with_canonical_strings():
    with _connection() as connection:
        _require_evaluations(connection)
        summary = _service().render_day(connection, TARGET)
        for row in summary["lectures"]:
            stored = connection.execute("""
            SELECT checklist_order, checklist_item, session_id_match, status, severity
              FROM public.lecture_qa_rendered_checklist_items
             WHERE rendered_session_id = %s ORDER BY checklist_order
            """, (uuid.UUID(row["rendered_session_id"]),)).fetchall()
            assert [item[0] for item in stored] == list(range(1, 12))
            assert [item[1] for item in stored] == list(CHECKLIST_ITEMS)
            assert all(item[2] == f"{row['session_id']}_{item[0]}" for item in stored)
            assert all(item[4] in ("pass", "warning", "fail") for item in stored)
        connection.rollback()


def test_rendered_evidence_is_reproducible_from_clips_and_canonical_cues():
    """Re-render every stored checklist evidence string from its own sources."""
    with _connection() as connection:
        _require_evaluations(connection)
        summary = _service(legacy=False).render_day(connection, TARGET)
        inputs = RenderInputRepository()
        checked = 0
        for row in summary["lectures"]:
            if row["render_status"] != RENDERED:
                continue
            evaluation = connection.execute(
                "SELECT document_id FROM public.lecture_qa_evaluations WHERE evaluation_id = %s",
                (uuid.UUID(row["evaluation_id"]),)).fetchone()
            cues = inputs.load_cues(connection, evaluation[0])
            clips = inputs.load_clips(connection, uuid.UUID(row["evaluation_id"]))
            by_key = {}
            for item in clips:
                by_key.setdefault((item["clip_source"], item["source_position"]), []).append(item)
            for order, stored in connection.execute("""
            SELECT checklist_order, rendered_evidence
              FROM public.lecture_qa_rendered_checklist_items
             WHERE rendered_session_id = %s AND checklist_order <> 7
             ORDER BY checklist_order
            """, (uuid.UUID(row["rendered_session_id"]),)).fetchall():
                expected = format_clips(
                    sorted(by_key.get(("checklist", order), []),
                           key=lambda clip: clip["clip_index"]), cues)["text"]
                assert expected == stored, (row["subject"], order)
                checked += 1
        assert checked > 0
        connection.rollback()


def test_every_rendered_quote_traces_back_to_stored_cue_ids():
    with _connection() as connection:
        _require_evaluations(connection)
        summary = _service(legacy=False).render_day(connection, TARGET)
        orphan = connection.execute("""
        SELECT count(*) FROM (
          SELECT unnest(ci.cue_ids) AS cue_id, rs.evaluation_id
            FROM public.lecture_qa_rendered_checklist_items ci
            JOIN public.lecture_qa_rendered_sessions rs USING (rendered_session_id)) refs
          JOIN public.lecture_qa_evaluations e ON e.evaluation_id = refs.evaluation_id
         WHERE NOT EXISTS (SELECT 1 FROM public.lecture_transcript_cues c
                            WHERE c.cue_id = refs.cue_id AND c.document_id = e.document_id)
        """).fetchone()[0]
        assert orphan == 0
        assert summary["blocks_rendered"] > 0
        connection.rollback()


def test_item7_evidence_comes_from_persisted_engagement_evidence():
    with _connection() as connection:
        _require_evaluations(connection)
        summary = _service(legacy=False).render_day(connection, TARGET)
        checked = 0
        for row in summary["lectures"]:
            if row["render_status"] != RENDERED:
                continue
            evidence, override = connection.execute("""
            SELECT ci.rendered_evidence, e.item7_override_applied
              FROM public.lecture_qa_rendered_checklist_items ci
              JOIN public.lecture_qa_rendered_sessions rs USING (rendered_session_id)
              JOIN public.lecture_engagement_metrics e ON e.engagement_id = (
                   SELECT engagement_id FROM public.lecture_qa_evaluations
                    WHERE evaluation_id = rs.evaluation_id)
             WHERE ci.rendered_session_id = %s AND ci.checklist_order = 7
            """, (uuid.UUID(row["rendered_session_id"]),)).fetchone()
            if override:
                assert evidence.startswith("Engagement score = ")
                assert "Students who attended but did not speak" in evidence
                checked += 1
        assert checked > 0
        connection.rollback()


def test_item7_rendering_never_queries_live_attendance():
    executed: list[str] = []
    with _connection() as connection:
        _require_evaluations(connection)
        _service().render_day(_Tracing(connection, executed), TARGET)
        connection.rollback()
    assert executed
    assert "kbc_attendance" not in " ".join(executed).lower()


def test_non_delivered_lecture_renders_the_cancelled_payload():
    with _connection() as connection:
        _require_evaluations(connection)
        summary = _service(legacy=False).render_day(connection, TARGET)
        cancelled = [row for row in summary["lectures"]
                     if row["render_status"] == RENDERED_NON_DELIVERED]
        if not cancelled:
            pytest.skip("no non-delivered evaluation present")
        for row in cancelled:
            stored = connection.execute("""
            SELECT cancelled_session, met_count, partial_count, not_met_count,
                   lms_snapshot_id, lms_students_count
              FROM public.lecture_qa_rendered_sessions WHERE rendered_session_id = %s
            """, (uuid.UUID(row["rendered_session_id"]),)).fetchone()
            assert stored[0] is True
            assert (stored[1], stored[2], stored[3]) == (0, 0, 11)
            # The legacy cancelled path never consumed LMS values.
            assert stored[4] is None and stored[5] is None
            statuses = connection.execute("""
            SELECT DISTINCT status FROM public.lecture_qa_rendered_checklist_items
             WHERE rendered_session_id = %s
            """, (uuid.UUID(row["rendered_session_id"]),)).fetchall()
            assert [item[0] for item in statuses] == ["Not Met"]
        connection.rollback()


# --- LMS snapshot ----------------------------------------------------------------

def test_lms_snapshot_matches_the_legacy_query_semantics():
    """
    A snapshot is a snapshot: it records the roster as it was when captured,
    and the live LMS table keeps moving underneath it - learners change group
    and leave "active" every day. So the legacy-query comparison is made only
    against snapshots captured in THIS run, where "then" and "now" are the
    same moment. The structural invariants below hold for every snapshot ever
    written, historical ones included.
    """
    with _connection() as connection:
        _require_evaluations(connection)
        existing = {row[0] for row in connection.execute(
            "SELECT snapshot_id FROM public.lecture_lms_snapshots").fetchall()}
        # A forced refresh re-reads the live table, so anything new here was
        # captured from the roster as it stands right now.
        _service(legacy=False, refresh_lms=True).render_day(connection, TARGET)
        rows = connection.execute("""
        SELECT s.snapshot_id, s.module, s.module_normalized, s.student_count,
               s.source_row_count, s.row_cap_reached
          FROM public.lecture_lms_snapshots s
         WHERE s.lms_snapshot_version = 'legacy_qa_v8_active_lms_roster_v1'
        """).fetchall()
        assert rows
        captured_now = [row for row in rows if row[0] not in existing]
        for snapshot_id, module, normalized, student_count, source_rows, capped in rows:
            fresh = snapshot_id not in existing
            members = connection.execute("""
            SELECT external_learner_id, full_name FROM public.lecture_lms_snapshot_members
             WHERE snapshot_id = %s
            """, (snapshot_id,)).fetchall()
            assert len(members) == student_count
            # Distinct pairs, no blank names, cap respected.
            assert len({(item[0], item[1]) for item in members}) == len(members)
            assert all(item[1].strip() for item in members)
            assert source_rows <= 500 and capped == (source_rows >= 500)
            if not fresh:
                # Historical snapshot: frozen by design, and the live roster
                # has moved on. Nothing to compare it against.
                continue
            # Every member is genuinely active in that module, right now.
            mismatched = connection.execute("""
            SELECT count(*) FROM public.lecture_lms_snapshot_members m
             WHERE m.snapshot_id = %s
               AND NOT EXISTS (
                 SELECT 1 FROM public.kbc_users_data k
                  WHERE k."ID" = m.external_learner_id AND k."FullName" = m.full_name
                    AND lower(coalesce(k."Program-Status", '')) = 'active'
                    AND lower(btrim(regexp_replace(replace(coalesce(k."Group", ''), '&amp;', '&'),
                                                   '\\s+', ' ', 'g'))) = %s)
            """, (snapshot_id, normalized)).fetchone()[0]
            assert mismatched == 0
        # The forced refresh above must actually have captured something,
        # otherwise the live-roster comparison silently tested nothing.
        assert captured_now
        connection.rollback()


def test_rendering_after_a_snapshot_exists_does_not_query_the_lms_table():
    with _connection() as connection:
        _require_evaluations(connection)
        _service(legacy=False).render_day(connection, TARGET)     # captures
        executed: list[str] = []
        summary = _service(legacy=False).render_day(_Tracing(connection, executed), TARGET)
        assert "kbc_users_data" not in " ".join(executed).lower()
        assert summary["lms_snapshots_created"] == 0
        assert summary["lms_snapshots_reused"] > 0
        connection.rollback()


def test_an_unchanged_roster_reuses_the_same_snapshot():
    """
    Two forced refreshes back to back. The first may legitimately create
    snapshots - the live LMS roster genuinely changes from one day to the
    next - but between the first and the second nothing can have changed, so
    the second must reuse every snapshot and create none. Asserting that the
    FIRST refresh creates nothing would be asserting that an external table
    never moves, which is not an invariant of this system.
    """
    with _connection() as connection:
        _require_evaluations(connection)
        _service(legacy=False, refresh_lms=True).render_day(connection, TARGET)
        before = {row[0] for row in connection.execute(
            "SELECT snapshot_id FROM public.lecture_lms_snapshots").fetchall()}
        summary = _service(legacy=False, refresh_lms=True).render_day(connection, TARGET)
        after = {row[0] for row in connection.execute(
            "SELECT snapshot_id FROM public.lecture_lms_snapshots").fetchall()}
        assert after == before
        assert summary["lms_snapshots_created"] == 0
        connection.rollback()


def test_a_changed_roster_creates_new_snapshot_provenance():
    with _connection() as connection:
        _require_evaluations(connection)
        _service(legacy=False).render_day(connection, TARGET)
        before = {row[0] for row in connection.execute(
            "SELECT snapshot_id FROM public.lecture_lms_snapshots").fetchall()}
        summary = _service(legacy=False, refresh_lms=True,
                           lms_source=_GrownLms()).render_day(connection, TARGET)
        after = {row[0] for row in connection.execute(
            "SELECT snapshot_id FROM public.lecture_lms_snapshots").fetchall()}
        assert before.issubset(after), "historical snapshots must survive"
        assert summary["lms_snapshots_created"] == len(after - before) > 0
        connection.rollback()


def test_the_lms_table_is_never_written():
    executed: list[str] = []
    with _connection() as connection:
        _require_evaluations(connection)
        before = connection.execute(
            "SELECT count(*), md5(string_agg(\"ID\"::text || coalesce(\"FullName\", ''), "
            "       ',' ORDER BY \"ID\", \"FullName\")) FROM public.kbc_users_data").fetchone()
        _service(refresh_lms=True).render_day(_Tracing(connection, executed), TARGET)
        after = connection.execute(
            "SELECT count(*), md5(string_agg(\"ID\"::text || coalesce(\"FullName\", ''), "
            "       ',' ORDER BY \"ID\", \"FullName\")) FROM public.kbc_users_data").fetchone()
        connection.rollback()
    assert before == after
    writes = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CREATE", "DROP")
    for statement in executed:
        upper = " ".join(statement.split()).upper()
        if upper.startswith(writes):
            assert "KBC_USERS_DATA" not in upper, statement


# --- compatibility payload ----------------------------------------------------------

def test_session_compatibility_payload_matches_legacy_deterministic_fields():
    with _connection() as connection:
        _require_evaluations(connection)
        summary = _service().render_day(connection, TARGET)
        parity = summary["legacy_comparison"]
        assert parity["qa_rows"] == 6
        for key in ("session_id_parity", "meeting_id_parity", "subject_parity",
                    "trainer_parity", "date_parity", "duration_parity",
                    "duration_score_parity", "engagement_parity",
                    "engagement_score_parity", "cancelled_session_parity",
                    "lms_module_parity", "teaching_quality_rating_parity"):
            matched, total = (int(part) for part in parity[key].split(" / "))
            assert matched == total == 6, (key, parity[key])
        structure = parity["checklist_structure"]
        assert structure["eleven_rows"] == "6 / 6"
        assert structure["item_strings_match"] == "6 / 6"
        assert structure["session_id_match_format"] == "6 / 6"
        connection.rollback()


def test_corrected_item2_is_kept_and_the_legacy_value_is_carried_separately():
    with _connection() as connection:
        _require_evaluations(connection)
        summary = _service().render_day(connection, TARGET)
        rows = summary["legacy_comparison"]["rows"]
        assert rows
        for row in rows:
            if row.get("status") == "NO_RENDERED_OUTPUT":
                continue
            # Both values are reported; they are never merged.
            assert "canonical_item2_status" in row and "legacy_historical_item2_status" in row
        # The rendered status always equals the Phase 3A final status.
        mismatched = connection.execute("""
        SELECT count(*)
          FROM public.lecture_qa_rendered_checklist_items ci
          JOIN public.lecture_qa_rendered_sessions rs USING (rendered_session_id)
          JOIN public.lecture_qa_checklist_items src
            ON src.evaluation_id = rs.evaluation_id
           AND src.checklist_order = ci.checklist_order
         WHERE ci.status <> src.status
        """).fetchone()[0]
        assert mismatched == 0
        connection.rollback()


def test_counts_reflect_the_final_statuses_including_overrides():
    with _connection() as connection:
        _require_evaluations(connection)
        _service(legacy=False).render_day(connection, TARGET)
        bad = connection.execute("""
        SELECT count(*) FROM public.lecture_qa_rendered_sessions rs
         WHERE rs.met_count <> (SELECT count(*) FROM public.lecture_qa_rendered_checklist_items ci
                                 WHERE ci.rendered_session_id = rs.rendered_session_id
                                   AND ci.status = 'Met')
            OR rs.partial_count <> (SELECT count(*) FROM public.lecture_qa_rendered_checklist_items ci
                                     WHERE ci.rendered_session_id = rs.rendered_session_id
                                       AND ci.status = 'Partially Met')
            OR rs.not_met_count <> (SELECT count(*) FROM public.lecture_qa_rendered_checklist_items ci
                                     WHERE ci.rendered_session_id = rs.rendered_session_id
                                       AND ci.status = 'Not Met')
        """).fetchone()[0]
        assert bad == 0
        connection.rollback()


# --- idempotency and safety -----------------------------------------------------------

def test_second_run_reuses_everything_and_creates_no_duplicates():
    with _connection() as connection:
        _require_evaluations(connection)
        first = _service().render_day(connection, TARGET)
        before = connection.execute("""
        SELECT rendered_session_id, source_fingerprint, met_count, not_met_count,
               lms_students_count, md5(lms_students::text)
          FROM public.lecture_qa_rendered_sessions ORDER BY source_fingerprint
        """).fetchall()
        items_before = connection.execute("""
        SELECT rendered_item_id, status, evidence FROM public.lecture_qa_rendered_checklist_items
         ORDER BY rendered_item_id
        """).fetchall()

        second = _service().render_day(connection, TARGET)
        assert second["sessions_rendered"] == 0
        assert second["sessions_reused"] == first["lectures_considered"]
        assert second["lms_snapshots_created"] == 0
        assert second["provider_calls"] == 0
        assert connection.execute("""
        SELECT rendered_session_id, source_fingerprint, met_count, not_met_count,
               lms_students_count, md5(lms_students::text)
          FROM public.lecture_qa_rendered_sessions ORDER BY source_fingerprint
        """).fetchall() == before
        assert connection.execute("""
        SELECT rendered_item_id, status, evidence FROM public.lecture_qa_rendered_checklist_items
         ORDER BY rendered_item_id
        """).fetchall() == items_before
        duplicates = connection.execute("""
        SELECT
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_qa_rendered_sessions
             GROUP BY source_fingerprint HAVING count(*) > 1) a),
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_qa_rendered_checklist_items
             GROUP BY rendered_session_id, checklist_order HAVING count(*) > 1) b),
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_lms_snapshots
             GROUP BY lecture_id, lms_snapshot_version, source_fingerprint
             HAVING count(*) > 1) c),
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_lms_snapshot_members
             GROUP BY snapshot_id, external_learner_id, full_name HAVING count(*) > 1) d)
        """).fetchone()
        assert duplicates == (0, 0, 0, 0)
        connection.rollback()


def test_a_new_renderer_version_creates_new_provenance():
    with _connection() as connection:
        _require_evaluations(connection)
        _service(legacy=False).render_day(connection, TARGET)
        before = {row[0] for row in connection.execute(
            "SELECT source_fingerprint FROM public.lecture_qa_rendered_sessions"
            " WHERE canonical_session_date = %s", (TARGET,)).fetchall()}
        _service(legacy=False, renderer_version="probe_renderer_v2").render_day(
            connection, TARGET)
        after = {row[0] for row in connection.execute(
            "SELECT source_fingerprint FROM public.lecture_qa_rendered_sessions"
            " WHERE canonical_session_date = %s", (TARGET,)).fetchall()}
        assert before.issubset(after)
        assert len(after - before) == len(before)
        connection.rollback()


def test_phase_3a_evidence_and_legacy_tables_are_untouched():
    with _connection() as connection:
        _require_evaluations(connection)
        before = _counts(connection)
        evaluation_digest = connection.execute("""
        SELECT md5(string_agg(evaluation_id::text || qa_status || source_fingerprint
                              || updated_at, ',' ORDER BY evaluation_id))
          FROM public.lecture_qa_evaluations""").fetchone()[0]
        checklist_digest = connection.execute("""
        SELECT md5(string_agg(checklist_row_id::text || status, ',' ORDER BY checklist_row_id))
          FROM public.lecture_qa_checklist_items""").fetchone()[0]
        legacy_digest = connection.execute("""
        SELECT md5(string_agg(session_id || coalesce(trainer, '')
                              || coalesce(lms_students::text, ''), ',' ORDER BY session_id))
          FROM public.qa_doctors_sessions""").fetchone()[0]

        _service().render_day(connection, TARGET)
        _service().render_day(connection, TARGET)

        assert _counts(connection) == before
        assert connection.execute("""
        SELECT md5(string_agg(evaluation_id::text || qa_status || source_fingerprint
                              || updated_at, ',' ORDER BY evaluation_id))
          FROM public.lecture_qa_evaluations""").fetchone()[0] == evaluation_digest
        assert connection.execute("""
        SELECT md5(string_agg(checklist_row_id::text || status, ',' ORDER BY checklist_row_id))
          FROM public.lecture_qa_checklist_items""").fetchone()[0] == checklist_digest
        assert connection.execute("""
        SELECT md5(string_agg(session_id || coalesce(trainer, '')
                              || coalesce(lms_students::text, ''), ',' ORDER BY session_id))
          FROM public.qa_doctors_sessions""").fetchone()[0] == legacy_digest
        connection.rollback()


def test_only_the_phase_3b_tables_are_written():
    executed: list[str] = []
    with _connection() as connection:
        _require_evaluations(connection)
        _service(refresh_lms=True).render_day(_Tracing(connection, executed), TARGET)
        connection.rollback()
    writes = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CREATE", "DROP")
    allowed = ("LECTURE_LMS_SNAPSHOTS", "LECTURE_LMS_SNAPSHOT_MEMBERS",
               "LECTURE_QA_RENDERED_SESSIONS", "LECTURE_QA_RENDERED_CHECKLIST_ITEMS",
               "LECTURE_QA_RENDER_RUNS")
    for statement in executed:
        upper = " ".join(statement.split()).upper()
        if upper.startswith(writes):
            assert any(table in upper for table in allowed), statement
            assert "QA_DOCTORS" not in upper and "QA_PERFECT" not in upper, statement
            assert "LECTURE_QA_EVALUATIONS" not in upper, statement
            assert "LECTURE_QA_CHECKLIST_ITEMS" not in upper, statement
            assert "LECTURE_QA_EVIDENCE_CLIPS" not in upper, statement


def test_the_run_audit_carries_no_learner_names_or_quotes():
    with _connection() as connection:
        _require_evaluations(connection)
        summary = _service().render_day(connection, TARGET)
        audit = connection.execute(
            "SELECT metadata FROM public.lecture_qa_render_runs WHERE run_id = %s",
            (uuid.UUID(summary["run_id"]),)).fetchone()[0]
        serialized = json.dumps(audit)
        names = {row[0] for row in connection.execute(
            "SELECT DISTINCT full_name FROM public.lecture_lms_snapshot_members").fetchall()}
        for value in names:
            if value and len(value.strip()) > 3:
                assert value not in serialized
        assert "-->" not in serialized
        # The summary itself carries no rendered quotes either.
        assert "-->" not in json.dumps(summary, default=str)
        connection.rollback()
