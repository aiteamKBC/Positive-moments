"""
Phase 3A persistence against the real 2026-09-04 platform evidence.

Every test runs in a rolled-back transaction, and the provider is always a
stub: no test reaches a real model. Tests that assert on a freshly produced
evaluation pass force=True, because a stored successful evaluation is
otherwise reused by design and the stub would never run. The QA input assembly, the delivery gate,
persistence, idempotency and the legacy comparison are exercised against the
real persisted Phase 1/2 evidence.
"""
import uuid
from datetime import date

import psycopg
import pytest

from app.attendance.resolver import RESOLVER_VERSION
from app.attendance.roles import ROLE_ALGORITHM_VERSION
from app.attendance.roster import ATTENDANCE_ROSTER_V1, ATTENDANCE_ROSTER_V2
from app.config.settings import Settings
from app.db.repositories.qa_writer import GenerationAttemptRepository
from app.db.repositories.qa_shadow import (
    LegacyQaComparisonRepository,
    QaEvaluationRepository,
    QaInputRepository,
    QaRunRepository,
)
from app.engagement.calculator import ENGAGEMENT_ALGORITHM_VERSION
from app.qa.checklist import CHECKLIST_ITEMS, MET
from app.qa.provider import ProviderError
from app.qa.punctuality import LEGACY_CALL_BOUNDS
from app.qa.service import COMPLETED, MODEL_ERROR, PENDING, QaInputError, ShadowQaService

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
    "qa_doctors_sessions", "qa_doctors_checklist_items", "qa_perfect_lectures",
    "qa_doctors_transcripts", "qa_media_jobs", "qa_positive_clip_assets",
    "qa_lecture_split_plans", "qa_lecture_part_assets",
    "kbc_attendance", "kbc_users_data",
    "lecture_sessions", "lecture_transcript_cues", "lecture_transcript_speakers",
    "lecture_transcript_documents", "lecture_transcript_selections",
    "lecture_combined_transcripts", "lecture_attendance_snapshots",
    "lecture_transcript_speaker_identities", "lecture_transcript_speaker_roles",
    "lecture_engagement_metrics",
)


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


class StubProvider:
    """Returns a schema-valid response whose clips sit inside the real cues."""

    def __init__(self, error=None, clip=None):
        self.calls = 0
        self.error = error
        self.clip = clip
        self.prompts = []

    def complete_json(self, *, system_message, user_message):
        self.calls += 1
        self.prompts.append((system_message, user_message))
        if self.error:
            raise self.error
        rows = []
        for index, item in enumerate(CHECKLIST_ITEMS):
            clips = [self.clip] if (self.clip and index == 3) else []
            rows.append({"item": item, "status": MET, "evidence_clips": clips})
        return {
            "output": {
                "session_info": {"trainer": "Model Guess", "date": "2026-09-04"},
                "checklist_evaluation": rows,
                "overall_summary": {"strengths": [], "areas_for_improvement": [],
                                    "overall_judgement": "Shadow run."},
                "ksbs_covered": [],
                "teaching_quality": {"rating_1_5": 4, "comments": "Stub.",
                                     "evidence_clips": []},
            },
            "provider": "openai", "model_requested": "gpt-5.2",
            "model_reported": "gpt-5.2", "response_id": "resp_stub", "usage": {},
            "attempts": 1,
        }


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


def _service(provider=None, *, legacy=True, roster=ATTENDANCE_ROSTER_V2,
             engine_version="shadow_qa_v8_engine_v1", attempts=None,
             max_generations=3, punctuality_source=None):
    extra = ({"punctuality_source_version": punctuality_source}
             if punctuality_source else {})
    return ShadowQaService(
        **extra,
        attempt_repository=attempts, max_model_generations=max_generations,
        input_repository=QaInputRepository(),
        evaluation_repository=QaEvaluationRepository(),
        run_repository=QaRunRepository(),
        legacy_repository=LegacyQaComparisonRepository() if legacy else None,
        provider=provider, engine_version=engine_version,
        attendance_roster_version=roster,
        resolver_version=RESOLVER_VERSION, role_algorithm_version=ROLE_ALGORITHM_VERSION,
        engagement_algorithm_version=ENGAGEMENT_ALGORITHM_VERSION, model_name="gpt-5.2")


def _require_evidence(connection):
    rows = connection.execute("""
    SELECT count(*) FROM public.lecture_engagement_metrics e
      JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = e.attendance_snapshot_id
     WHERE s.attendance_resolution_version = %s
    """, (ATTENDANCE_ROSTER_V2,)).fetchone()[0]
    if not rows:
        pytest.skip("Phase 2C4 v2 engagement evidence is not present")


def _counts(connection):
    return {name: connection.execute(f"SELECT count(*) FROM public.{name}").fetchone()[0]
            for name in PROTECTED_TABLES}


# --- input assembly -----------------------------------------------------------

def test_inputs_come_from_the_v2_evidence_only():
    with _connection() as connection:
        _require_evidence(connection)
        rows = QaInputRepository().load_inputs(
            connection, TARGET, parser_version="webvtt_canonical_v1",
            attendance_roster_version=ATTENDANCE_ROSTER_V2,
            resolver_version=RESOLVER_VERSION,
            role_algorithm_version=ROLE_ALGORITHM_VERSION,
            engagement_algorithm_version=ENGAGEMENT_ALGORITHM_VERSION)
        assert len(rows) == 7
        assert {row["attendance_roster_version"] for row in rows} == {ATTENDANCE_ROSTER_V2}
        assert all(row["canonical_trainer"] for row in rows)
        assert all(row["combined_content"].startswith("WEBVTT") for row in rows)
        connection.rollback()


def test_v1_and_v2_engagement_differ_so_the_pin_matters():
    with _connection() as connection:
        _require_evidence(connection)
        rows = connection.execute("""
        SELECT s.attendance_resolution_version, sum(e.attended_count)
          FROM public.lecture_engagement_metrics e
          JOIN public.lecture_attendance_snapshots s ON s.snapshot_id = e.attendance_snapshot_id
         GROUP BY 1 ORDER BY 1
        """).fetchall()
        totals = dict(rows)
        if len(totals) > 1:
            # The pin is load-bearing: the two rules give different denominators.
            assert totals[ATTENDANCE_ROSTER_V1] != totals[ATTENDANCE_ROSTER_V2]
        connection.rollback()


# --- preview and the delivery gate --------------------------------------------

def test_preview_makes_no_call_and_writes_no_evaluation():
    provider = StubProvider()
    with _connection() as connection:
        _require_evidence(connection)
        before = connection.execute(
            "SELECT count(*) FROM public.lecture_qa_evaluations").fetchone()[0]
        summary = _service(provider).run_day(connection, TARGET)
        assert provider.calls == 0
        assert summary["mode"] == "PREVIEW"
        assert summary["lectures_considered"] == 7
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_qa_evaluations").fetchone()[0] == before
        connection.rollback()


def test_short_lecture_is_routed_to_the_non_delivered_path_from_live_duration():
    provider = StubProvider()
    with _connection() as connection:
        _require_evidence(connection)
        # force: a previously stored evaluation would otherwise be reused, and
        # the reuse path deliberately returns only its status, not the counts.
        summary = _service(provider).run_day(connection, TARGET, execute=True, force=True)
        short = [row for row in summary["lectures"]
                 if row["delivery_status"] == "NON_DELIVERED"]
        assert short, "expected at least one lecture under the 20-minute gate"
        for row in short:
            assert row["duration_minutes"] < 20
            assert row["ai_called"] is False
            assert row["provider_calls"] == 0
            assert row["qa_status"] == "NON_DELIVERED"
            assert row["met_count"] == 0 and row["not_met_count"] == 11
        # Provider calls only for delivered lectures.
        assert provider.calls == summary["delivered_count"]
        connection.rollback()


def test_non_delivered_evaluation_persists_eleven_not_met_rows():
    with _connection() as connection:
        _require_evidence(connection)
        summary = _service(StubProvider()).run_day(connection, TARGET, execute=True, force=True)
        short = next(row for row in summary["lectures"]
                     if row["delivery_status"] == "NON_DELIVERED")
        rows = connection.execute("""
        SELECT checklist_order, checklist_item, status, status_source
          FROM public.lecture_qa_checklist_items
         WHERE evaluation_id = %s ORDER BY checklist_order
        """, (uuid.UUID(short["evaluation_id"]),)).fetchall()
        assert len(rows) == 11
        assert [row[1] for row in rows] == list(CHECKLIST_ITEMS)
        assert {row[2] for row in rows} == {"Not Met"}
        assert {row[3] for row in rows} == {"NON_DELIVERED"}
        stored = connection.execute("""
        SELECT canonical_trainer, engagement_score, duration_score, duration_text,
               teaching_quality_rating, cancelled_session, ai_called
          FROM public.lecture_qa_evaluations WHERE evaluation_id = %s
        """, (uuid.UUID(short["evaluation_id"]),)).fetchone()
        assert stored == ("Session not delivered", 0, 0, "0 minutes", 1, True, False)
        connection.rollback()


# --- delivered path, persistence and determinism -------------------------------

def test_delivered_lectures_persist_deterministic_overrides():
    with _connection() as connection:
        _require_evidence(connection)
        summary = _service(StubProvider()).run_day(connection, TARGET, execute=True, force=True)
        delivered = [row for row in summary["lectures"] if row["delivery_status"] == "DELIVERED"]
        assert delivered
        for row in delivered:
            stored = connection.execute("""
            SELECT ci.checklist_order, ci.status, ci.ai_status, ci.status_source
              FROM public.lecture_qa_checklist_items ci
             WHERE ci.evaluation_id = %s AND ci.checklist_order IN (1, 2, 7)
             ORDER BY ci.checklist_order
            """, (uuid.UUID(row["evaluation_id"]),)).fetchall()
            sources = {item[0]: item[3] for item in stored}
            assert sources[1] == "DETERMINISTIC_DURATION"
            assert sources[2] == "DETERMINISTIC_PUNCTUALITY"
            assert sources[7] == "DETERMINISTIC_ENGAGEMENT_PHASE_2C4"
            # The stub always says Met; the deterministic verdict is what is stored.
            assert {item[2] for item in stored} == {"Met"}
        connection.rollback()


def test_canonical_trainer_is_kept_and_the_model_guess_is_only_recorded():
    with _connection() as connection:
        _require_evidence(connection)
        summary = _service(StubProvider()).run_day(connection, TARGET, execute=True, force=True)
        # Scoped to THIS run's evaluations. A changed source fingerprint creates
        # new provenance rather than overwriting the old row (ON CONFLICT is on
        # source_fingerprint), so a date-wide query would also return the real
        # production evaluations and their real trainers.
        produced = [uuid.UUID(row["evaluation_id"]) for row in summary["lectures"]
                    if row.get("evaluation_id") and row.get("ai_called")]
        assert produced
        rows = connection.execute("""
        SELECT canonical_trainer, ai_suggested_trainer, trainer_source
          FROM public.lecture_qa_evaluations
         WHERE ai_called AND evaluation_id = ANY(%s)
        """, (produced,)).fetchall()
        assert rows
        for canonical, suggested, source in rows:
            assert canonical != "Model Guess"
            assert suggested == "Model Guess"
            assert source == "PHASE_2C3_VTT_TOP_SPEAKER"
        connection.rollback()


def test_evidence_clips_are_validated_against_the_real_cue_timeline():
    valid_clip = {"start": "00:00:30.000", "end": "00:00:40.000"}
    with _connection() as connection:
        _require_evidence(connection)
        _service(StubProvider(clip=valid_clip)).run_day(connection, TARGET, execute=True, force=True)
        rows = connection.execute("""
        SELECT validation_status, count(*) FROM public.lecture_qa_evidence_clips
         GROUP BY 1 ORDER BY 1
        """).fetchall()
        assert rows, "expected stored evidence clips"
        statuses = dict(rows)
        assert statuses.get("VALID", 0) >= 1
        connection.rollback()


def test_hallucinated_timestamp_is_recorded_not_repaired():
    far_future = {"start": "23:00:00.000", "end": "23:00:30.000"}
    with _connection() as connection:
        _require_evidence(connection)
        summary = _service(StubProvider(clip=far_future)).run_day(
            connection, TARGET, execute=True, force=True)
        delivered = [row for row in summary["lectures"] if row["ai_called"]]
        assert all(row["invalid_evidence_clip_count"] >= 1 for row in delivered)
        assert all(row["qa_status"] == "INVALID_EVIDENCE" for row in delivered)
        # Scoped to the evaluations this run produced: the table also holds
        # clips from real model runs, which are legitimately VALID.
        stored = connection.execute("""
        SELECT DISTINCT cl.validation_status
          FROM public.lecture_qa_evidence_clips cl
         WHERE cl.evaluation_id = ANY(%s)
        """, ([uuid.UUID(row["evaluation_id"]) for row in delivered],)).fetchall()
        assert [row[0] for row in stored] == ["OUT_OF_TRANSCRIPT_RANGE"]
        connection.rollback()


def test_provider_error_never_becomes_not_met_verdicts():
    provider = StubProvider(error=ProviderError("provider_http_error", "boom", http_status=503))
    with _connection() as connection:
        _require_evidence(connection)
        summary = _service(provider).run_day(connection, TARGET, execute=True, force=True)
        failed = [row for row in summary["lectures"] if row["qa_status"] == MODEL_ERROR]
        assert failed
        for row in failed:
            assert connection.execute(
                "SELECT count(*) FROM public.lecture_qa_checklist_items WHERE evaluation_id = %s",
                (uuid.UUID(row["evaluation_id"]),)).fetchone()[0] == 0
        connection.rollback()


def test_missing_provider_records_pending_with_the_deterministic_layer():
    with _connection() as connection:
        _require_evidence(connection)
        summary = _service(None).run_day(connection, TARGET, execute=True, force=True)
        pending = [row for row in summary["lectures"] if row["qa_status"] == PENDING]
        assert len(pending) == summary["delivered_count"]
        for row in pending:
            stored = connection.execute("""
            SELECT error_code, duration_score, canonical_trainer, final_item7_status,
                   ai_called, attended_count
              FROM public.lecture_qa_evaluations WHERE evaluation_id = %s
            """, (uuid.UUID(row["evaluation_id"]),)).fetchone()
            assert stored[0] == "provider_not_configured"
            assert stored[2] and stored[4] is False
        assert summary["provider_calls"] == 0
        connection.rollback()


# --- cost control and idempotency ---------------------------------------------

def test_second_run_reuses_evaluations_and_makes_no_new_calls():
    provider = StubProvider()
    with _connection() as connection:
        _require_evidence(connection)
        first = _service(provider).run_day(connection, TARGET, execute=True)
        calls_after_first = provider.calls
        rows_first = connection.execute("""
        SELECT evaluation_id, source_fingerprint, qa_status, final_item7_status,
               duration_score, canonical_trainer
          FROM public.lecture_qa_evaluations ORDER BY source_fingerprint
        """).fetchall()

        second = _service(provider).run_day(connection, TARGET, execute=True)
        assert provider.calls == calls_after_first, "unchanged inputs must not re-call"
        assert second["provider_calls"] == 0
        assert second["reused_evaluations"] == first["lectures_considered"]
        rows_second = connection.execute("""
        SELECT evaluation_id, source_fingerprint, qa_status, final_item7_status,
               duration_score, canonical_trainer
          FROM public.lecture_qa_evaluations ORDER BY source_fingerprint
        """).fetchall()
        assert rows_second == rows_first
        duplicates = connection.execute("""
        SELECT
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_qa_evaluations
             GROUP BY source_fingerprint HAVING count(*) > 1) a),
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_qa_checklist_items
             GROUP BY evaluation_id, checklist_order HAVING count(*) > 1) b),
          (SELECT count(*) FROM (SELECT 1 FROM public.lecture_qa_evidence_clips
             GROUP BY evaluation_id, clip_source, source_position, clip_index
             HAVING count(*) > 1) c)
        """).fetchone()
        assert duplicates == (0, 0, 0)
        connection.rollback()


def test_forced_re_evaluation_calls_the_model_again():
    provider = StubProvider()
    with _connection() as connection:
        _require_evidence(connection)
        _service(provider).run_day(connection, TARGET, execute=True, force=True)
        first_calls = provider.calls
        assert first_calls > 0
        _service(provider).run_day(connection, TARGET, execute=True, force=True)
        assert provider.calls == first_calls * 2
        connection.rollback()


def test_a_new_engine_version_creates_new_provenance_without_deleting_the_old():
    provider = StubProvider()
    with _connection() as connection:
        _require_evidence(connection)
        all_before = {row[0] for row in connection.execute(
            "SELECT source_fingerprint FROM public.lecture_qa_evaluations"
            " WHERE lecture_id IN (SELECT lecture_id FROM public.lecture_sessions"
            "                       WHERE session_date = %s)", (TARGET,)).fetchall()}
        first = _service(provider).run_day(connection, TARGET, execute=True)
        second = _service(provider, engine_version="shadow_qa_probe_v2").run_day(
            connection, TARGET, execute=True)
        all_after = {row[0] for row in connection.execute(
            "SELECT source_fingerprint FROM public.lecture_qa_evaluations"
            " WHERE lecture_id IN (SELECT lecture_id FROM public.lecture_sessions"
            "                       WHERE session_date = %s)", (TARGET,)).fetchall()}

        # Compare the two runs' OWN fingerprints, not the whole date: earlier
        # production evaluations are also on this date and must be left alone.
        def fingerprints(summary):
            return {row["source_fingerprint_prefix"] for row in summary["lectures"]
                    if row.get("source_fingerprint_prefix")}

        one, two = fingerprints(first), fingerprints(second)
        assert one and len(one) == len(two)
        assert one.isdisjoint(two), "a new engine version must not reuse provenance"
        # Nothing was deleted, and both runs' rows now coexist with the old ones.
        assert all_before.issubset(all_after)
        assert len(all_after) >= len(all_before) + len(one) + len(two) - len(
            all_before & (one | two))
        connection.rollback()


# --- legacy comparison and safety ----------------------------------------------

def test_legacy_deterministic_parity_is_reported():
    with _connection() as connection:
        _require_evidence(connection)
        summary = _service(StubProvider()).run_day(connection, TARGET, execute=True, force=True)
        parity = summary["legacy_comparison"]
        assert parity["qa_rows"] == 6
        for key in ("session_id_parity", "meeting_id_parity", "trainer_parity",
                    "duration_parity", "attended_count_parity", "engagement_parity",
                    "engagement_score_parity", "item1_parity", "item7_parity"):
            matched, total = (int(part) for part in parity[key].split(" / "))
            assert total == 6
            assert matched == 6, (key, parity[key])
        connection.rollback()


def test_item2_differences_are_explained_by_the_legacy_timezone_defect():
    """
    Legacy parsed naive Cairo scheduled times as UTC, so its punctuality inputs
    were shifted by a whole number of hours and almost every session looked as
    if it had ended hours early. Our Item 2 is computed from the corrected
    Phase 2B timing, so a difference is expected - but every difference must be
    attributable to that constant offset, not to our rule.
    """
    with _connection() as connection:
        _require_evidence(connection)
        # Measured under the LEGACY punctuality source on purpose. Phase 3C3B
        # made the source an explicit version, and the new one deliberately
        # measures the lecture rather than the call - so under it the start and
        # end offsets no longer move together, and this test would be
        # conflating two different differences. The timezone claim is about
        # legacy's inputs, so it is tested against legacy's own bounds.
        summary = _service(StubProvider(), punctuality_source=LEGACY_CALL_BOUNDS
                           ).run_day(connection, TARGET, execute=True, force=True)
        rows = summary["legacy_comparison"]["rows"]
        offsets = set()
        for row in rows:
            if row.get("status") == "NO_SHADOW_RESULT":
                continue
            assert row["legacy_start_difference_minutes"] is not None, row["subject"]
            start_offset = (row["new_start_difference_minutes"]
                            - row["legacy_start_difference_minutes"])
            end_offset = (row["new_end_difference_minutes"]
                          - row["legacy_end_difference_minutes"])
            # The same shift on both ends of every lecture.
            assert start_offset == end_offset, row["subject"]
            offsets.add(start_offset)
            if row["item2_match"] == "NO":
                assert start_offset != 0, row["subject"]
        assert len(offsets) == 1, offsets
        offset = offsets.pop()
        assert offset != 0 and offset % 60 == 0, offset
        connection.rollback()


def test_our_item2_follows_the_corrected_timing_not_the_legacy_status():
    from app.qa.deterministic import item2_status
    with _connection() as connection:
        _require_evidence(connection)
        _service(StubProvider()).run_day(connection, TARGET, execute=True, force=True)
        rows = connection.execute("""
        SELECT e.start_difference_minutes, e.end_difference_minutes, ci.status
          FROM public.lecture_qa_evaluations e
          JOIN public.lecture_qa_checklist_items ci
            ON ci.evaluation_id = e.evaluation_id AND ci.checklist_order = 2
         WHERE e.delivery_status = 'DELIVERED'
        """).fetchall()
        assert rows
        for start, end, status in rows:
            assert status == item2_status(start, end)
        connection.rollback()


def test_legacy_and_external_tables_are_untouched():
    with _connection() as connection:
        _require_evidence(connection)
        before = _counts(connection)
        qa_digest = connection.execute(
            "SELECT md5(string_agg(session_id || coalesce(trainer, '') || "
            "       coalesce(\"Engagement\"::text, ''), ',' ORDER BY session_id)) "
            "  FROM public.qa_doctors_sessions").fetchone()[0]
        item_digest = connection.execute(
            "SELECT md5(string_agg(session_id_match || coalesce(status, ''), ',' "
            "       ORDER BY session_id_match)) FROM public.qa_doctors_checklist_items"
        ).fetchone()[0]

        _service(StubProvider()).run_day(connection, TARGET, execute=True)
        _service(StubProvider()).run_day(connection, TARGET, execute=True)

        assert _counts(connection) == before
        assert connection.execute(
            "SELECT md5(string_agg(session_id || coalesce(trainer, '') || "
            "       coalesce(\"Engagement\"::text, ''), ',' ORDER BY session_id)) "
            "  FROM public.qa_doctors_sessions").fetchone()[0] == qa_digest
        assert connection.execute(
            "SELECT md5(string_agg(session_id_match || coalesce(status, ''), ',' "
            "       ORDER BY session_id_match)) FROM public.qa_doctors_checklist_items"
        ).fetchone()[0] == item_digest
        connection.rollback()


def test_no_statement_touches_graph_attendance_or_lms_and_no_qa_write():
    executed: list[str] = []
    with _connection() as connection:
        _require_evidence(connection)
        _service(StubProvider()).run_day(_Tracing(connection, executed), TARGET, execute=True)
        connection.rollback()
    assert executed
    joined = " ".join(executed).lower()
    assert "kbc_attendance" not in joined
    assert "kbc_users_data" not in joined
    assert "aptem_auto_extracting" not in joined
    writes = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "CREATE", "DROP")
    allowed = ("LECTURE_QA_EVALUATIONS", "LECTURE_QA_CHECKLIST_ITEMS",
               "LECTURE_QA_EVIDENCE_CLIPS", "LECTURE_QA_RUNS")
    for statement in executed:
        upper = " ".join(statement.split()).upper()
        if upper.startswith(writes):
            assert any(table in upper for table in allowed), statement
            assert "QA_DOCTORS" not in upper and "QA_PERFECT" not in upper, statement


def test_run_summary_carries_no_transcript_text_or_personal_names():
    import json
    with _connection() as connection:
        _require_evidence(connection)
        summary = _service(StubProvider()).run_day(connection, TARGET, execute=True)
        serialized = json.dumps(summary, default=str)
        assert "WEBVTT" not in serialized
        names = {row[0] for row in connection.execute(
            "SELECT DISTINCT speaker_label_raw FROM public.lecture_transcript_speakers"
        ).fetchall()}
        for value in names:
            if value and len(value.strip()) > 3:
                assert value not in serialized, value
        for key in ("graph_calls", "live_attendance_queries", "lms_queries",
                    "legacy_qa_writes"):
            assert summary[key] == 0
        connection.rollback()


def test_prompt_contains_the_transcript_but_the_run_audit_does_not():
    provider = StubProvider()
    with _connection() as connection:
        _require_evidence(connection)
        summary = _service(provider).run_day(connection, TARGET, execute=True, force=True)
        system, user = provider.prompts[0]
        assert system.startswith("You are an educational QA evaluator")
        assert "TRANSCRIPT:\nWEBVTT" in user
        audit = connection.execute(
            "SELECT metadata FROM public.lecture_qa_runs WHERE run_id = %s",
            (uuid.UUID(summary["run_id"]),)).fetchone()[0]
        assert "WEBVTT" not in str(audit)
        assert audit["prompt_version"] == "legacy_qa_v8_prompt_v1"
        connection.rollback()


# --- Phase 3C1 hardening: bounded model generations ---------------------------

class _AlwaysInvalid(StubProvider):
    """A provider whose output always fails evidence validation."""

    def complete_json(self, *, system_message, user_message):
        response = super().complete_json(system_message=system_message,
                                         user_message=user_message)
        rows = response["output"]["checklist_evaluation"]
        rows[3]["evidence_clips"] = [{"start": "23:00:00.000", "end": "23:00:30.000"}]
        return response


def test_three_invalid_generations_close_the_fingerprint_and_stop_calling():
    provider = _AlwaysInvalid()
    attempts = GenerationAttemptRepository()
    with _connection() as connection:
        _require_evidence(connection)
        statuses = []
        for _ in range(3):
            summary = _service(provider, attempts=attempts).run_day(
                connection, TARGET, execute=True, force=True)
            statuses.append([row["qa_status"] for row in summary["lectures"]
                             if row.get("ai_called")])
        assert provider.calls > 0
        # Generation 3 flips the delivered lectures to the terminal state.
        assert all(status == "REVIEW_REQUIRED" for status in statuses[-1])
        recorded = connection.execute("""
        SELECT source_fingerprint, count(*) FROM public.lecture_qa_generation_attempts
         WHERE lecture_id IN (SELECT lecture_id FROM public.lecture_sessions
                               WHERE session_date = %s)
         GROUP BY 1
        """, (TARGET,)).fetchall()
        assert recorded and all(row[1] == 3 for row in recorded)

        # A normal run afterwards must not call the provider again.
        calls_before = provider.calls
        after = _service(provider, attempts=attempts).run_day(
            connection, TARGET, execute=True)
        assert provider.calls == calls_before
        assert after["provider_calls"] == 0
        review = [row for row in after["lectures"]
                  if row["qa_status"] == "REVIEW_REQUIRED"]
        assert review
        assert all(row.get("review_reason") == "MAX_GENERATIONS_EXHAUSTED"
                   for row in review)
        connection.rollback()


def test_an_explicit_force_may_still_retry_after_the_cap():
    provider = _AlwaysInvalid()
    attempts = GenerationAttemptRepository()
    with _connection() as connection:
        _require_evidence(connection)
        for _ in range(3):
            _service(provider, attempts=attempts).run_day(
                connection, TARGET, execute=True, force=True)
        calls_before = provider.calls
        forced = _service(provider, attempts=attempts).run_day(
            connection, TARGET, execute=True, force=True)
        assert provider.calls > calls_before
        assert forced["provider_calls"] > 0
        # The earlier attempts are preserved, not replaced.
        counts = connection.execute(
            "SELECT count(*) FROM public.lecture_qa_generation_attempts"
            " WHERE lecture_id IN (SELECT lecture_id FROM public.lecture_sessions"
            "                       WHERE session_date = %s)", (TARGET,)).fetchone()[0]
        assert counts > 3
        connection.rollback()


def test_successful_evaluations_are_never_capped_or_mutated():
    provider = StubProvider()
    attempts = GenerationAttemptRepository()
    with _connection() as connection:
        _require_evidence(connection)
        first = _service(provider, attempts=attempts).run_day(
            connection, TARGET, execute=True, force=True)
        completed = [row for row in first["lectures"] if row["qa_status"] == COMPLETED]
        assert completed
        # A completed generation is recorded but never closes the fingerprint.
        outcomes = connection.execute(
            "SELECT DISTINCT outcome FROM public.lecture_qa_generation_attempts"
            " WHERE lecture_id IN (SELECT lecture_id FROM public.lecture_sessions"
            "                       WHERE session_date = %s)", (TARGET,)).fetchall()
        assert [row[0] for row in outcomes] == ["COMPLETED"]
        second = _service(provider, attempts=attempts).run_day(
            connection, TARGET, execute=True)
        assert second["provider_calls"] == 0
        assert all(row["qa_status"] != "REVIEW_REQUIRED" for row in second["lectures"])
        connection.rollback()
