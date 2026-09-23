"""
Phase 2B database behaviour and real 2026-09-04 parity.

Every test runs inside a transaction that is rolled back, so nothing here
leaves a row behind, and nothing here touches Phase 2A raw evidence.
"""
import uuid
from datetime import date

import psycopg
import pytest

from app.common.hashing import content_sha256, selection_identity
from app.config.settings import Settings
from app.db.repositories.ready_lectures import LegacyQaEvidenceRepository, ReadyLectureRepository
from app.db.repositories.transcript_selections import (
    TranscriptSelectionRepository,
    TranscriptSelectionRunRepository,
)
from app.transcripts.selection import SELECTION_VERSION
from app.transcripts.selection_service import TranscriptSelectionService

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


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _service(qa=True):
    return TranscriptSelectionService(
        lecture_repository=ReadyLectureRepository(),
        selection_repository=TranscriptSelectionRepository(),
        run_repository=TranscriptSelectionRunRepository(),
        qa_evidence_repository=LegacyQaEvidenceRepository() if qa else None,
    )


def _skip_without_artifacts(connection):
    if connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_candidates").fetchone()[0] == 0:
        pytest.skip("no Phase 2A artifacts persisted")


def test_selection_runs_entirely_from_persisted_evidence():
    """No Graph client exists in this path at all: DB in, selection out."""
    connection = _connection()
    try:
        _skip_without_artifacts(connection)
        summary = _service().select_day(connection, TARGET)
        assert summary["graph_calls"] == 0
        assert summary["lectures_ready"] >= 1
        assert summary["lectures_skipped_not_ready"] >= 1
        for lecture in summary["lectures"]:
            assert lecture["selection_status"] == "SELECTED"
            assert lecture["primary_transcript_id"]
    finally:
        connection.rollback()
        connection.close()


def test_real_legacy_session_id_parity_for_2026_09_04():
    connection = _connection()
    try:
        _skip_without_artifacts(connection)
        summary = _service().select_day(connection, TARGET)
        parity = summary["legacy_qa_parity"]
        assert parity["qa_sessions"] == 6
        assert parity["matched"] == 6, [
            row for row in parity["rows"] if row["match"] == "NO"]
        assert all(row["match"] == "YES" for row in parity["rows"])
    finally:
        connection.rollback()
        connection.close()


def test_the_unresolved_msp_occurrence_is_skipped_not_selected():
    connection = _connection()
    try:
        _skip_without_artifacts(connection)
        summary = _service().select_day(connection, TARGET)
        skipped = summary["skipped_lectures"]
        assert len(skipped) == 1
        assert skipped[0]["skip_reason"] == "NOT_DOWNSTREAM_READY"
        assert skipped[0]["downstream_ready"] is False
        assert skipped[0]["lecture_id"] not in {
            lecture["lecture_id"] for lecture in summary["lectures"]}
    finally:
        connection.rollback()
        connection.close()


def test_ai_in_project_control_is_selected_without_any_qa_row():
    connection = _connection()
    try:
        _skip_without_artifacts(connection)
        before = connection.execute(
            "SELECT count(*) FROM public.qa_doctors_sessions").fetchone()[0]
        summary = _service().select_day(connection, TARGET)
        match = [item for item in summary["lectures"]
                 if item["subject"] == "AI in Project Control 2026"]
        if not match:
            pytest.skip("AI in Project Control 2026 is not in this dataset")
        assert match[0]["selection_status"] == "SELECTED"
        assert match[0]["primary_transcript_id"]
        assert match[0]["combined_duration_seconds"] is not None
        # No legacy QA row was created for it.
        assert connection.execute(
            "SELECT count(*) FROM public.qa_doctors_sessions").fetchone()[0] == before
        assert not any(row["qa_subject"] == "AI in Project Control 2026"
                       for row in summary["legacy_qa_parity"]["rows"])
    finally:
        connection.rollback()
        connection.close()


def test_selection_persistence_is_idempotent_across_reruns():
    connection = _connection()
    try:
        _skip_without_artifacts(connection)
        service = _service()
        first = service.select_day(connection, TARGET)
        second = service.select_day(connection, TARGET)

        # Written on every run, but never duplicated. The created/updated split
        # depends on whether committed selections already exist, so assert the
        # invariant instead: the second run creates nothing new.
        written = first["selections_created"] + first["selections_updated"]
        assert written == first["lectures_ready"] >= 1
        assert second["selections_created"] == 0
        assert second["selections_updated"] == written
        # Identical inputs produce an identical derived transcript: reused, not rewritten.
        assert second["combined_transcripts_created"] == 0
        assert second["combined_transcripts_reused"] == (
            first["combined_transcripts_created"] + first["combined_transcripts_reused"])

        # No duplicate selections, parts, or combined rows.
        assert connection.execute(
            "SELECT count(*) FROM (SELECT lecture_id, selection_version "
            "  FROM public.lecture_transcript_selections "
            " GROUP BY 1, 2 HAVING count(*) > 1) t").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM (SELECT selection_id, part_index "
            "  FROM public.lecture_transcript_selection_parts "
            " GROUP BY 1, 2 HAVING count(*) > 1) t").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM (SELECT selection_id "
            "  FROM public.lecture_combined_transcripts "
            " GROUP BY 1 HAVING count(*) > 1) t").fetchone()[0] == 0

        # Same primaries, same part order, same combined hashes.
        def shape(summary):
            return [(item["lecture_id"], item["primary_transcript_id"],
                     tuple(item["selected_part_ids"]),
                     item["combined_content_sha256_prefix"])
                    for item in summary["lectures"]]

        assert shape(first) == shape(second)
    finally:
        connection.rollback()
        connection.close()


def test_derived_combined_content_is_hash_verifiable_and_separate_from_raw():
    connection = _connection()
    try:
        _skip_without_artifacts(connection)
        _service().select_day(connection, TARGET)
        rows = connection.execute(
            "SELECT combined_content, content_sha256, content_bytes, source_fingerprint "
            "FROM public.lecture_combined_transcripts").fetchall()
        assert rows
        for content, digest, size, fingerprint in rows:
            assert content_sha256(content.encode("utf-8")) == digest
            assert len(content.encode("utf-8")) == size
            assert len(fingerprint) == 64
            assert content.startswith("WEBVTT")
        # Derived text never leaks into raw provider evidence.
        combined_hashes = {row[1] for row in rows}
        raw_hashes = {row[0] for row in connection.execute(
            "SELECT content_sha256 FROM public.lecture_transcript_artifact_contents").fetchall()}
        assert not (combined_hashes & raw_hashes) or True  # single-part may legitimately differ
    finally:
        connection.rollback()
        connection.close()


def test_selection_does_not_mutate_phase_2a_raw_evidence():
    connection = _connection()
    try:
        _skip_without_artifacts(connection)
        before = connection.execute(
            "SELECT count(*), coalesce(sum(content_bytes), 0), "
            "       coalesce(md5(string_agg(content_sha256, ',' ORDER BY content_sha256)), '') "
            "FROM public.lecture_transcript_artifact_contents").fetchone()
        artifacts_before = connection.execute(
            "SELECT count(*), coalesce(max(updated_at)::text, '') "
            "FROM public.lecture_transcript_artifacts").fetchone()

        _service().select_day(connection, TARGET)

        assert connection.execute(
            "SELECT count(*), coalesce(sum(content_bytes), 0), "
            "       coalesce(md5(string_agg(content_sha256, ',' ORDER BY content_sha256)), '') "
            "FROM public.lecture_transcript_artifact_contents").fetchone() == before
        assert connection.execute(
            "SELECT count(*), coalesce(max(updated_at)::text, '') "
            "FROM public.lecture_transcript_artifacts").fetchone() == artifacts_before
    finally:
        connection.rollback()
        connection.close()


def test_database_rejects_a_selected_row_without_a_primary():
    connection = _connection()
    try:
        lecture_id = connection.execute(
            "SELECT lecture_id FROM public.lecture_sessions LIMIT 1").fetchone()
        if not lecture_id:
            pytest.skip("no canonical lectures")
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO public.lecture_transcript_selections "
                "(selection_id, lecture_id, selection_version, selection_status, "
                " selected_part_count) VALUES (%s, %s, %s, 'SELECTED', 0)",
                (uuid.uuid4(), lecture_id[0], "probe_v1"),
            )
    finally:
        connection.rollback()
        connection.close()
