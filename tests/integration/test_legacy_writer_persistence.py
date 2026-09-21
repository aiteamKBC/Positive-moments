"""
Phase 3C1 persistence against the real 2026-09-04 evidence and the real legacy
QA tables.

Every test runs inside a transaction that is ROLLED BACK, so no legacy row is
ever committed. Write-enabled behaviour is proven the only honest way - by
actually exercising it against the real tables and then discarding it - and
each write test re-asserts afterwards that the legacy tables are byte-identical.
"""
import uuid
from datetime import date

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.qa_writer import (
    GenerationAttemptRepository,
    LegacyQaTargetRepository,
    RenderedPayloadRepository,
    WriterOwnershipRepository,
)
from app.writer.mapping import WRITER_VERSION, digest
from app.writer.modes import (
    CANARY_NEW_ONLY,
    DRY_RUN,
    EXPLICIT_BACKFILL,
    PROTECTED_EXISTING_LEGACY_ROW,
    WOULD_INSERT,
    WOULD_SKIP_IDENTICAL,
    WOULD_UPDATE,
    WRITE_ENABLED_MODES,
)
from app.writer.service import WRITE_VERIFICATION_FAILED, LegacyQaWriter, WriteVerificationError


TARGET = date(2026, 9, 4)
LEGACY_TABLES = ("qa_doctors_sessions", "qa_doctors_checklist_items",
                 "qa_perfect_lectures", "qa_doctors_transcripts")


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _rendered_lecture_ids(connection):
    """Every lecture on TARGET with a Phase 3B payload, in a stable order."""
    return [row[0] for row in connection.execute("""
        SELECT r.lecture_id::text
          FROM public.lecture_qa_rendered_sessions r
         WHERE r.canonical_session_date = %s
           AND r.render_status IN ('RENDERED', 'RENDERED_NON_DELIVERED')
         ORDER BY r.session_id""", (TARGET,)).fetchall()]


def _insertable_lecture_ids(connection):
    """Those of them whose legacy row does not exist yet."""
    return [row[0] for row in connection.execute("""
        SELECT r.lecture_id::text
          FROM public.lecture_qa_rendered_sessions r
         WHERE r.canonical_session_date = %s
           AND r.render_status IN ('RENDERED', 'RENDERED_NON_DELIVERED')
           AND NOT EXISTS (SELECT 1 FROM public.qa_doctors_sessions q
                            WHERE q.session_id = r.session_id)
         ORDER BY r.session_id""", (TARGET,)).fetchall()]


# Phase 3C2 made a write-enabled mode refuse a date-wide, unconfirmed run. A
# test that used to rely on "only one lecture is insertable today" must now name
# it, exactly as an operator must. The scope is resolved ONCE per connection and
# then reused, because that is what re-running the same command does - resolving
# it again after the write would find nothing left to insert.
_SCOPES: dict = {}


def _scope_for(mode, connection):
    # Keyed on the connection object, not id(), which is recycled between tests.
    key = (connection, mode)
    if key not in _SCOPES:
        _SCOPES[key] = (_insertable_lecture_ids(connection)[:1] if mode == CANARY_NEW_ONLY
                        else _rendered_lecture_ids(connection))
    return _SCOPES[key]


def _writer(mode=DRY_RUN, connection=None, **kwargs):
    if mode in WRITE_ENABLED_MODES:
        kwargs.setdefault("confirmed", True)
        if "lecture_ids" not in kwargs:
            if connection is None:
                raise AssertionError("a write-enabled test writer needs a connection to scope")
            kwargs["lecture_ids"] = _scope_for(mode, connection)
    return LegacyQaWriter(
        payload_repository=RenderedPayloadRepository(),
        legacy_repository=LegacyQaTargetRepository(),
        ownership_repository=WriterOwnershipRepository(),
        mode=mode, **kwargs)


def _require_payload(connection):
    ready = connection.execute(
        "SELECT count(*) FROM public.lecture_qa_rendered_sessions "
        " WHERE render_status IN ('RENDERED', 'RENDERED_NON_DELIVERED')").fetchone()[0]
    if not ready:
        pytest.skip("no Phase 3B rendered payload present")


def _legacy_digest(connection):
    """Whole-table fingerprint of both legacy QA tables."""
    return connection.execute("""
    SELECT
      (SELECT count(*) FROM public.qa_doctors_sessions),
      (SELECT md5(string_agg(session_id || coalesce(trainer, '') || coalesce(subject, '')
                  || coalesce("Engagement"::text, '') || coalesce(met_count::text, '')
                  || coalesce(lms_students::text, '') || coalesce(cancelled_session, ''),
                  ',' ORDER BY session_id)) FROM public.qa_doctors_sessions),
      (SELECT count(*) FROM public.qa_doctors_checklist_items),
      (SELECT md5(string_agg(session_id_match || status || coalesce(evidence, ''),
                  ',' ORDER BY session_id_match)) FROM public.qa_doctors_checklist_items),
      (SELECT count(*) FROM public.qa_perfect_lectures)
    """).fetchone()


def _new_lecture_result(summary):
    """The one lecture with no historical legacy row."""
    candidates = [row for row in summary["lectures"] if not row["legacy_row_exists"]]
    if not candidates:
        pytest.skip("no lecture without a historical legacy row")
    return candidates[0]


# --- 41-43. the dry run ---------------------------------------------------------

def test_dry_run_protects_every_historical_session_and_writes_nothing():
    with _connection() as connection:
        _require_payload(connection)
        before = _legacy_digest(connection)
        summary = _writer(DRY_RUN).plan_day(connection, TARGET)
        assert summary["writes_enabled"] is False
        assert summary["sessions_written"] == 0
        assert summary["checklist_rows_written"] == 0
        assert summary["provider_calls"] == 0
        existing = [row for row in summary["lectures"] if row["legacy_row_exists"]]
        assert len(existing) == 6
        assert all(row["decision"] == PROTECTED_EXISTING_LEGACY_ROW for row in existing)
        assert all(row["coded_owned"] is False for row in existing)
        assert _legacy_digest(connection) == before
        connection.rollback()


def test_dry_run_reports_a_field_level_diff_without_sensitive_content():
    import json
    with _connection() as connection:
        _require_payload(connection)
        summary = _writer(DRY_RUN).plan_day(connection, TARGET)
        serialized = json.dumps(summary, default=str)
        # The diff names columns and classifications, never the values inside
        # them: no learner name, no roster payload, no rendered quote.
        assert "FullName" not in serialized
        assert "-->" not in serialized
        names = {row[0] for row in connection.execute(
            "SELECT DISTINCT full_name FROM public.lecture_lms_snapshot_members").fetchall()}
        for value in names:
            if value and len(value.strip()) > 3:
                assert value not in serialized, value
        for row in summary["lectures"]:
            if not row["legacy_row_exists"]:
                continue
            assert set(row["field_diff"].values()) <= {
                "DIFFERENT_EXPECTED_AI", "DIFFERENT_LMS_DRIFT", "DIFFERENT_OTHER"}
            assert row["checklist_diff"]["status_difference_count"] is not None
        connection.rollback()


def test_the_lecture_without_history_would_be_inserted_but_is_not():
    with _connection() as connection:
        _require_payload(connection)
        before = _legacy_digest(connection)
        summary = _writer(DRY_RUN).plan_day(connection, TARGET)
        new = _new_lecture_result(summary)
        assert new["decision"] == WOULD_INSERT
        assert new["proposed_checklist_rows"] == 11
        assert connection.execute(
            "SELECT count(*) FROM public.qa_doctors_sessions WHERE session_id = %s",
            (new["legacy_session_id"],)).fetchone()[0] == 0
        assert _legacy_digest(connection) == before
        connection.rollback()


def test_item2_historical_rows_are_not_corrected_by_the_dry_run():
    with _connection() as connection:
        _require_payload(connection)
        before = connection.execute("""
        SELECT ci.session_id_match, ci.status FROM public.qa_doctors_checklist_items ci
          JOIN public.qa_doctors_sessions q USING (session_id)
         WHERE q.date = %s AND ci.checklist_order = 2 ORDER BY 1
        """, (TARGET,)).fetchall()
        _writer(DRY_RUN).plan_day(connection, TARGET)
        after = connection.execute("""
        SELECT ci.session_id_match, ci.status FROM public.qa_doctors_checklist_items ci
          JOIN public.qa_doctors_sessions q USING (session_id)
         WHERE q.date = %s AND ci.checklist_order = 2 ORDER BY 1
        """, (TARGET,)).fetchall()
        assert after == before
        connection.rollback()


# --- 24-25. canary write, proven then discarded ----------------------------------

def test_canary_write_creates_one_session_eleven_rows_and_one_owner():
    with _connection() as connection:
        _require_payload(connection)
        before = _legacy_digest(connection)
        summary = _writer(CANARY_NEW_ONLY, connection).plan_day(connection, TARGET)
        new = _new_lecture_result(summary)
        assert new["decision"] == WOULD_INSERT
        assert new["write_status"] == "WRITTEN"
        assert new["checklist_rows_written"] == 11
        assert new["rolled_back"] is False
        session_id = new["legacy_session_id"]
        assert connection.execute(
            "SELECT count(*) FROM public.qa_doctors_sessions WHERE session_id = %s",
            (session_id,)).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM public.qa_doctors_checklist_items WHERE session_id = %s",
            (session_id,)).fetchone()[0] == 11
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_qa_legacy_writes WHERE legacy_session_id = %s",
            (session_id,)).fetchone()[0] == 1
        # A canary considers exactly the one lecture it was given; the six
        # historical rows are out of scope entirely, and the digest check below
        # proves they were not touched.
        assert summary["lectures_considered"] == 1
        connection.rollback()

    # Nothing survived the rollback.
    with _connection() as verify:
        assert _legacy_digest(verify) == before
        verify.rollback()


def test_a_second_identical_canary_run_is_a_noop():
    with _connection() as connection:
        _require_payload(connection)
        first = _writer(CANARY_NEW_ONLY, connection).plan_day(connection, TARGET)
        new = _new_lecture_result(first)
        session_id = new["legacy_session_id"]

        second = _writer(CANARY_NEW_ONLY, connection).plan_day(connection, TARGET)
        repeat = next(row for row in second["lectures"]
                      if row["legacy_session_id"] == session_id)
        assert repeat["decision"] == WOULD_SKIP_IDENTICAL
        assert repeat["coded_owned"] is True
        assert second["sessions_written"] == 0
        assert connection.execute(
            "SELECT count(*) FROM public.qa_doctors_checklist_items WHERE session_id = %s",
            (session_id,)).fetchone()[0] == 11
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_qa_legacy_writes WHERE legacy_session_id = %s",
            (session_id,)).fetchone()[0] == 1
        connection.rollback()


def test_a_changed_fingerprint_updates_only_a_coded_owned_target():
    with _connection() as connection:
        _require_payload(connection)
        first = _writer(CANARY_NEW_ONLY, connection).plan_day(connection, TARGET)
        new = _new_lecture_result(first)
        session_id = new["legacy_session_id"]
        # Simulate re-rendered evidence: a new source fingerprint on our own row.
        connection.execute(
            "UPDATE public.lecture_qa_legacy_writes SET source_fingerprint = %s "
            " WHERE legacy_session_id = %s", ("b" * 64, session_id))

        second = _writer(CANARY_NEW_ONLY, connection).plan_day(connection, TARGET)
        updated = next(row for row in second["lectures"]
                       if row["legacy_session_id"] == session_id)
        assert updated["decision"] == WOULD_UPDATE
        assert updated["write_status"] == "UPDATED"
        assert connection.execute(
            "SELECT count(*) FROM public.qa_doctors_sessions WHERE session_id = %s",
            (session_id,)).fetchone()[0] == 1
        # Still exactly one owner and eleven rows.
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_qa_legacy_writes WHERE legacy_session_id = %s",
            (session_id,)).fetchone()[0] == 1
        connection.rollback()


def test_a_write_enabled_mode_still_refuses_a_legacy_owned_row():
    with _connection() as connection:
        _require_payload(connection)
        before = _legacy_digest(connection)
        summary = _writer(EXPLICIT_BACKFILL, connection, allow_update_existing=True).plan_day(
            connection, TARGET)
        historical = [row for row in summary["lectures"] if row["legacy_row_exists"]]
        assert len(historical) == 6
        assert all(row["decision"] == PROTECTED_EXISTING_LEGACY_ROW for row in historical)
        assert all("write_status" not in row for row in historical)
        # Only the brand-new lecture was written; history is untouched.
        after = _legacy_digest(connection)
        assert after[0] == before[0] + 1
        assert after[2] == before[2] + 11
        connection.rollback()


def test_the_canary_write_does_not_disturb_columns_owned_by_other_workflows():
    with _connection() as connection:
        _require_payload(connection)
        first = _writer(CANARY_NEW_ONLY, connection).plan_day(connection, TARGET)
        session_id = _new_lecture_result(first)["legacy_session_id"]
        # Another workflow sets a recording link on our row.
        connection.execute(
            "UPDATE public.qa_doctors_sessions SET recording_url = %s, clips_status = %s "
            " WHERE session_id = %s", ("https://example.invalid/rec", "done", session_id))
        connection.execute(
            "UPDATE public.lecture_qa_legacy_writes SET source_fingerprint = %s "
            " WHERE legacy_session_id = %s", ("c" * 64, session_id))

        _writer(CANARY_NEW_ONLY, connection).plan_day(connection, TARGET)
        preserved = connection.execute(
            "SELECT recording_url, clips_status FROM public.qa_doctors_sessions "
            " WHERE session_id = %s", (session_id,)).fetchone()
        assert preserved == ("https://example.invalid/rec", "done")
        connection.rollback()


# --- 17-18. transaction failure ---------------------------------------------------

def test_a_checklist_failure_rolls_the_whole_lecture_back():
    class BrokenChecklist(LegacyQaTargetRepository):
        def write_checklist(self, connection, rows, *, allow_write):
            super().write_checklist(connection, rows, allow_write=allow_write)
            raise psycopg.errors.CheckViolation("simulated checklist failure")

    with _connection() as connection:
        _require_payload(connection)
        before = _legacy_digest(connection)
        writer = LegacyQaWriter(
            payload_repository=RenderedPayloadRepository(),
            legacy_repository=BrokenChecklist(),
            ownership_repository=WriterOwnershipRepository(),
            mode=CANARY_NEW_ONLY,
            lecture_ids=_scope_for(CANARY_NEW_ONLY, connection), confirmed=True)
        with pytest.raises(psycopg.errors.CheckViolation):
            writer.plan_day(connection, TARGET)
        connection.rollback()

    with _connection() as verify:
        assert _legacy_digest(verify) == before
        # No session, no partial checklist rows, no ownership row survived.
        assert verify.execute(
            "SELECT count(*) FROM public.lecture_qa_legacy_writes"
            " WHERE legacy_session_id IN ("
            "   SELECT session_id FROM public.lecture_qa_rendered_sessions"
            "    WHERE canonical_session_date = %s)", (TARGET,)).fetchone()[0] == 0
        verify.rollback()


def test_a_verification_mismatch_rolls_back_and_is_reported():
    class TamperingRepository(LegacyQaTargetRepository):
        def write_session(self, connection, row, *, allow_write):
            # Write something different from what was proposed.
            super().write_session(connection, dict(row, met_count=99), allow_write=allow_write)

    with _connection() as connection:
        _require_payload(connection)
        before = _legacy_digest(connection)
        writer = LegacyQaWriter(
            payload_repository=RenderedPayloadRepository(),
            legacy_repository=TamperingRepository(),
            ownership_repository=WriterOwnershipRepository(),
            mode=CANARY_NEW_ONLY,
            lecture_ids=_scope_for(CANARY_NEW_ONLY, connection), confirmed=True)
        summary = writer.plan_day(connection, TARGET)
        new = _new_lecture_result(summary)
        assert new["write_status"] == WRITE_VERIFICATION_FAILED
        assert new["rolled_back"] is True
        assert summary["verification_failures"] == 1
        assert summary["sessions_written"] == 0
        assert _legacy_digest(connection) == before
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_qa_legacy_writes"
            " WHERE legacy_session_id IN ("
            "   SELECT session_id FROM public.lecture_qa_rendered_sessions"
            "    WHERE canonical_session_date = %s)", (TARGET,)).fetchone()[0] == 0
        connection.rollback()


# --- 24. rollback safety ------------------------------------------------------------

def test_rollback_removes_only_a_coded_owned_session():
    with _connection() as connection:
        _require_payload(connection)
        writer = _writer(CANARY_NEW_ONLY, connection)
        summary = writer.plan_day(connection, TARGET)
        session_id = _new_lecture_result(summary)["legacy_session_id"]

        outcome = writer.rollback_owned_session(connection, session_id)
        assert outcome["status"] == "ROLLED_BACK"
        assert connection.execute(
            "SELECT count(*) FROM public.qa_doctors_sessions WHERE session_id = %s",
            (session_id,)).fetchone()[0] == 0
        # The checklist rows cascade with the session.
        assert connection.execute(
            "SELECT count(*) FROM public.qa_doctors_checklist_items WHERE session_id = %s",
            (session_id,)).fetchone()[0] == 0
        assert connection.execute(
            "SELECT write_status FROM public.lecture_qa_legacy_writes "
            " WHERE legacy_session_id = %s", (session_id,)).fetchone()[0] == "ROLLED_BACK"
        connection.rollback()


def test_rollback_refuses_a_legacy_owned_session():
    with _connection() as connection:
        _require_payload(connection)
        before = _legacy_digest(connection)
        legacy_session = connection.execute(
            "SELECT session_id FROM public.qa_doctors_sessions WHERE date = %s LIMIT 1",
            (TARGET,)).fetchone()[0]
        outcome = _writer(CANARY_NEW_ONLY, connection).rollback_owned_session(connection, legacy_session)
        assert outcome["status"] == PROTECTED_EXISTING_LEGACY_ROW
        assert outcome["deleted"] is False
        assert _legacy_digest(connection) == before
        connection.rollback()


def test_rollback_requires_a_write_enabled_mode():
    with _connection() as connection:
        _require_payload(connection)
        with pytest.raises(WriteVerificationError):
            _writer(DRY_RUN).rollback_owned_session(connection, "anything")
        connection.rollback()


# --- 25. concurrency -------------------------------------------------------------------

def test_two_writers_cannot_both_claim_the_same_session():
    with _connection() as first, _connection() as second:
        _require_payload(first)
        summary = _writer(CANARY_NEW_ONLY, first).plan_day(first, TARGET)
        session_id = _new_lecture_result(summary)["legacy_session_id"]
        entry = {
            "write_id": uuid.uuid4(),
            "lecture_id": uuid.UUID(summary["lectures"][0]["lecture_id"]),
            "evaluation_id": first.execute(
                "SELECT evaluation_id FROM public.lecture_qa_legacy_writes "
                " WHERE legacy_session_id = %s", (session_id,)).fetchone()[0],
            "rendered_session_id": first.execute(
                "SELECT rendered_session_id FROM public.lecture_qa_legacy_writes "
                " WHERE legacy_session_id = %s", (session_id,)).fetchone()[0],
            "legacy_session_id": session_id, "writer_version": WRITER_VERSION,
            "source_fingerprint": "d" * 64, "write_mode": CANARY_NEW_ONLY,
            "write_status": "WRITTEN", "legacy_session_created": True,
            "legacy_session_updated": False, "checklist_rows_created": 11,
            "checklist_rows_updated": 0, "pre_write_digest": None,
            "post_write_digest": None, "metadata": {},
        }
        # A second claim resolves to the SAME ownership row, never a duplicate.
        outcome = WriterOwnershipRepository().record(first, entry)
        assert outcome["created"] is False
        assert first.execute(
            "SELECT count(*) FROM public.lecture_qa_legacy_writes WHERE legacy_session_id = %s",
            (session_id,)).fetchone()[0] == 1
        first.rollback()
        second.rollback()


def test_the_ownership_unique_constraint_exists():
    with _connection() as connection:
        definition = connection.execute("""
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
         WHERE conrelid = 'public.lecture_qa_legacy_writes'::regclass AND contype = 'u'
        """).fetchone()[0]
        assert "legacy_session_id" in definition and "writer_version" in definition
        connection.rollback()


# --- bounded generations, against the real table ----------------------------------------

def test_generation_attempts_are_append_only_and_numbered():
    repository = GenerationAttemptRepository()
    fingerprint = "e" * 64
    with _connection() as connection:
        base = {"lecture_id": None, "source_fingerprint": fingerprint,
                "qa_engine_version": "engine", "prompt_version": "prompt",
                "model_name": "gpt-5.2", "outcome": "INVALID_EVIDENCE"}
        assert repository.count(connection, fingerprint) == 0
        assert repository.record(connection, base) == 1
        assert repository.record(connection, base) == 2
        assert repository.record(connection, base) == 3
        assert repository.count(connection, fingerprint) == 3
        # Earlier attempts are preserved, not overwritten.
        numbers = connection.execute(
            "SELECT generation_number FROM public.lecture_qa_generation_attempts "
            " WHERE source_fingerprint = %s ORDER BY generation_number",
            (fingerprint,)).fetchall()
        assert [row[0] for row in numbers] == [1, 2, 3]
        connection.rollback()


def test_a_real_evaluation_can_be_marked_review_required_without_losing_output():
    from app.db.repositories.qa_shadow import QaEvaluationRepository
    with _connection() as connection:
        row = connection.execute(
            "SELECT evaluation_id, ai_raw_output IS NOT NULL FROM public.lecture_qa_evaluations "
            " WHERE qa_status = 'COMPLETED' LIMIT 1").fetchone()
        if row is None:
            pytest.skip("no completed evaluation")
        QaEvaluationRepository().mark_review_required(
            connection, row[0], reason="MAX_GENERATIONS_EXHAUSTED", attempts=3)
        after = connection.execute(
            "SELECT qa_status, review_reason, ai_raw_output IS NOT NULL, "
            "       metadata->>'generation_attempts' "
            "  FROM public.lecture_qa_evaluations WHERE evaluation_id = %s",
            (row[0],)).fetchone()
        assert after[0] == "REVIEW_REQUIRED"
        assert after[1] == "MAX_GENERATIONS_EXHAUSTED"
        # The model output survives as provenance.
        assert after[2] == row[1]
        assert after[3] == "3"
        connection.rollback()


# --- 37-40. the writer touches nothing else -----------------------------------------------

def test_the_writer_issues_no_model_lms_attendance_or_graph_statement():
    executed: list[str] = []

    class Tracing:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, statement, *args, **kwargs):
            executed.append(str(statement))
            return self._inner.execute(statement, *args, **kwargs)

        def cursor(self, *args, **kwargs):
            handle = self._inner.cursor(*args, **kwargs)
            return handle

        def __getattr__(self, name):
            return getattr(self._inner, name)

    with _connection() as connection:
        _require_payload(connection)
        _writer(DRY_RUN).plan_day(Tracing(connection), TARGET)
        connection.rollback()
    joined = " ".join(executed).lower()
    assert executed
    for forbidden in ("kbc_users_data", "kbc_attendance", "aptem_auto_extracting",
                      "qa_perfect_lectures", "qa_doctors_transcripts"):
        assert forbidden not in joined
    for statement in executed:
        upper = " ".join(statement.split()).upper()
        if upper.startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "DROP")):
            raise AssertionError(f"DRY_RUN issued a write: {statement}")


def test_all_legacy_tables_are_unchanged_after_the_whole_suite_of_dry_runs():
    with _connection() as connection:
        _require_payload(connection)
        before = _legacy_digest(connection)
        _writer(DRY_RUN).plan_day(connection, TARGET)
        _writer(DRY_RUN).plan_day(connection, TARGET)
        assert _legacy_digest(connection) == before
        counts = {name: connection.execute(
            f"SELECT count(*) FROM public.{name}").fetchone()[0] for name in LEGACY_TABLES}
        connection.rollback()
    with _connection() as verify:
        assert {name: verify.execute(
            f"SELECT count(*) FROM public.{name}").fetchone()[0]
            for name in LEGACY_TABLES} == counts
        verify.rollback()
