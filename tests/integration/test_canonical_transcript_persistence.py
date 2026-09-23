"""
Phase 2C1 persistence, provenance and idempotency.

Every test runs inside a transaction that is rolled back, so nothing here
leaves a row behind and no source evidence is touched.
"""
import uuid
from datetime import date

import psycopg
import pytest

from app.common.hashing import content_sha256
from app.config.settings import Settings
from app.db.repositories.transcript_documents import (
    TranscriptDocumentRepository,
    TranscriptParseRunRepository,
    document_identity,
)
from app.transcripts.document_service import CanonicalTranscriptService
from app.transcripts.webvtt import PARSED, PARSER_VERSION

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


def _service(parser_version=PARSER_VERSION):
    return CanonicalTranscriptService(
        document_repository=TranscriptDocumentRepository(),
        run_repository=TranscriptParseRunRepository(),
        parser_version=parser_version,
    )


def _skip_without_combined(connection):
    if connection.execute(
            "SELECT count(*) FROM public.lecture_combined_transcripts").fetchone()[0] == 0:
        pytest.skip("no Phase 2B combined transcripts persisted")


# --- provenance identity ---------------------------------------------------------


# --- real parse ------------------------------------------------------------------


def test_all_seven_combined_transcripts_parse_from_the_database_alone():
    connection = _connection()
    try:
        _skip_without_combined(connection)
        summary = _service().parse_day(connection, TARGET)
        assert summary["graph_calls"] == 0
        assert summary["combined_transcripts_considered"] >= 1
        assert summary["failed_count"] == 0
        for document in summary["documents"]:
            assert document["parse_status"] == PARSED
            assert document["cue_count"] > 0
            assert document["source_fingerprint_prefix"]
    finally:
        connection.rollback()
        connection.close()


def test_stored_cues_match_the_parsed_counts_and_are_ordered():
    connection = _connection()
    try:
        _skip_without_combined(connection)
        summary = _service().parse_day(connection, TARGET)
        for document in summary["documents"]:
            rows = connection.execute(
                "SELECT cue_index, start_ms, end_ms FROM public.lecture_transcript_cues "
                "WHERE document_id = %s ORDER BY cue_index",
                (document["document_id"],)).fetchall()
            assert len(rows) == document["cue_count"]
            assert [row[0] for row in rows] == list(range(1, len(rows) + 1))
            assert all(row[1] >= 0 and row[2] >= row[1] for row in rows)
    finally:
        connection.rollback()
        connection.close()


def test_duration_parity_with_phase_2b_is_stored_and_within_tolerance():
    connection = _connection()
    try:
        _skip_without_combined(connection)
        summary = _service().parse_day(connection, TARGET)
        assert summary["duration_parity_mismatches"] == 0
        for document in summary["documents"]:
            assert document["duration_parity_ok"] is True
            stored = connection.execute(
                "SELECT duration_ms, combined_duration_seconds, duration_difference_ms "
                "FROM public.lecture_transcript_documents WHERE document_id = %s",
                (document["document_id"],)).fetchone()
            assert stored[0] == document["parsed_duration_ms"]
            assert stored[2] == document["duration_difference_ms"]
    finally:
        connection.rollback()
        connection.close()


def test_raw_speaker_labels_are_stored_without_identity_inference():
    connection = _connection()
    try:
        _skip_without_combined(connection)
        summary = _service().parse_day(connection, TARGET)
        document = summary["documents"][0]
        labels = connection.execute(
            "SELECT count(DISTINCT speaker_label_raw) "
            "FROM public.lecture_transcript_cues "
            "WHERE document_id = %s AND speaker_label_raw IS NOT NULL",
            (document["document_id"],)).fetchone()[0]
        assert labels == document["unique_raw_speaker_label_count"]
        # No identity columns exist at this layer at all.
        columns = {row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='lecture_transcript_cues'").fetchall()}
        assert not columns & {"user_id", "attendance_id", "speaker_id", "is_trainer",
                              "speaker_label_normalized", "lms_user_id"}
    finally:
        connection.rollback()
        connection.close()


def test_ai_in_project_control_parses_like_any_other_transcript():
    connection = _connection()
    try:
        _skip_without_combined(connection)
        before = connection.execute(
            "SELECT count(*) FROM public.qa_doctors_sessions").fetchone()[0]
        summary = _service().parse_day(connection, TARGET)
        match = [d for d in summary["documents"]
                 if d["subject"] == "AI in Project Control 2026"]
        if not match:
            pytest.skip("AI in Project Control 2026 is not in this dataset")
        assert match[0]["parse_status"] == PARSED
        assert match[0]["cue_count"] > 0
        # Its short duration is not a parser concern, and no QA row appears.
        assert connection.execute(
            "SELECT count(*) FROM public.qa_doctors_sessions").fetchone()[0] == before
    finally:
        connection.rollback()
        connection.close()


# --- idempotency ------------------------------------------------------------------


def test_second_run_reuses_documents_and_writes_no_duplicate_cues():
    connection = _connection()
    try:
        _skip_without_combined(connection)
        service = _service()
        first = service.parse_day(connection, TARGET)
        second = service.parse_day(connection, TARGET)

        assert second["documents_created"] == 0
        assert second["documents_reused"] == first["documents_created"] + first["documents_reused"]
        # Identical provenance: cues are left alone rather than rewritten.
        assert second["cues_written"] == 0
        assert second["cues_reused"] == sum(d["cue_count"] for d in second["documents"])
        assert all(d["cue_persistence"] == "reused" for d in second["documents"])

        assert [d["document_id"] for d in first["documents"]] == \
               [d["document_id"] for d in second["documents"]]
        assert [d["cue_count"] for d in first["documents"]] == \
               [d["cue_count"] for d in second["documents"]]

        assert connection.execute(
            "SELECT count(*) FROM (SELECT selection_id, source_content_sha256, parser_version "
            "  FROM public.lecture_transcript_documents "
            " GROUP BY 1,2,3 HAVING count(*) > 1) t").fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM (SELECT document_id, cue_index "
            "  FROM public.lecture_transcript_cues "
            " GROUP BY 1,2 HAVING count(*) > 1) t").fetchone()[0] == 0
    finally:
        connection.rollback()
        connection.close()


def test_a_changed_source_creates_a_new_document_and_preserves_the_old_one():
    """Changed combined bytes must never overwrite historical derived evidence."""
    connection = _connection()
    try:
        _skip_without_combined(connection)
        service = _service()
        original = service.parse_day(connection, TARGET)
        first_document = original["documents"][0]

        # Simulate a reselection producing different combined content. Only the
        # Phase 2B row is touched, and the whole test is rolled back.
        combined_id, content = connection.execute(
            "SELECT combined_id, combined_content FROM public.lecture_combined_transcripts "
            "WHERE selection_id = %s", (first_document["selection_id"],)).fetchone()
        revised = content + "\n\n99:00:00.000 --> 99:00:01.000\n<v Jane Doe>appended</v>\n"
        connection.execute(
            "UPDATE public.lecture_combined_transcripts "
            "SET combined_content = %s, content_sha256 = %s WHERE combined_id = %s",
            (revised, content_sha256(revised.encode("utf-8")), combined_id))

        reparsed = service.parse_day(connection, TARGET)
        changed = [d for d in reparsed["documents"]
                   if d["selection_id"] == first_document["selection_id"]][0]

        assert changed["document_id"] != first_document["document_id"]
        assert changed["cue_count"] == first_document["cue_count"] + 1
        # The original document and its cues are untouched.
        old = connection.execute(
            "SELECT cue_count FROM public.lecture_transcript_documents WHERE document_id = %s",
            (first_document["document_id"],)).fetchone()
        assert old is not None and old[0] == first_document["cue_count"]
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_cues WHERE document_id = %s",
            (first_document["document_id"],)).fetchone()[0] == first_document["cue_count"]
    finally:
        connection.rollback()
        connection.close()


def test_a_new_parser_version_coexists_with_the_previous_document():
    connection = _connection()
    try:
        _skip_without_combined(connection)
        first = _service().parse_day(connection, TARGET)
        second = _service(parser_version="webvtt_canonical_v2_probe").parse_day(
            connection, TARGET)

        assert second["documents_created"] == second["combined_transcripts_considered"]
        ids_v1 = {d["document_id"] for d in first["documents"]}
        ids_v2 = {d["document_id"] for d in second["documents"]}
        assert not (ids_v1 & ids_v2)
        # Both versions remain queryable for the same lecture.
        lecture_id = first["documents"][0]["lecture_id"]
        versions = {row[0] for row in connection.execute(
            "SELECT parser_version FROM public.lecture_transcript_documents "
            "WHERE lecture_id = %s", (lecture_id,)).fetchall()}
        assert versions == {PARSER_VERSION, "webvtt_canonical_v2_probe"}
    finally:
        connection.rollback()
        connection.close()


# --- database guarantees ------------------------------------------------------------


def test_database_rejects_a_duplicate_cue_index():
    connection = _connection()
    try:
        _skip_without_combined(connection)
        summary = _service().parse_day(connection, TARGET)
        document_id = summary["documents"][0]["document_id"]
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                "INSERT INTO public.lecture_transcript_cues "
                "(cue_id, document_id, cue_index, start_ms, end_ms, text, cue_text_sha256) "
                "VALUES (%s, %s, 1, 0, 1, 'dup', %s)",
                (uuid.uuid4(), document_id, "0" * 64))
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.parametrize("start_ms, end_ms", [(-1, 10), (10, 5)])
def test_database_rejects_impossible_cue_timings(start_ms, end_ms):
    connection = _connection()
    try:
        _skip_without_combined(connection)
        summary = _service().parse_day(connection, TARGET)
        document_id = summary["documents"][0]["document_id"]
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO public.lecture_transcript_cues "
                "(cue_id, document_id, cue_index, start_ms, end_ms, text, cue_text_sha256) "
                "VALUES (%s, %s, 999999, %s, %s, 'x', %s)",
                (uuid.uuid4(), document_id, start_ms, end_ms, "0" * 64))
    finally:
        connection.rollback()
        connection.close()


def test_cues_are_written_in_one_batched_statement_per_document(monkeypatch):
    """Hundreds of lectures must not mean one round trip per cue."""
    connection = _connection()
    try:
        _skip_without_combined(connection)
        repository = TranscriptDocumentRepository()
        calls = {"executemany": 0}
        original = psycopg.Cursor.executemany

        def counting(self, *args, **kwargs):
            calls["executemany"] += 1
            return original(self, *args, **kwargs)

        monkeypatch.setattr(psycopg.Cursor, "executemany", counting)
        # A fresh parser version forces new provenance, so cues are actually
        # written regardless of what previous runs already committed.
        service = CanonicalTranscriptService(
            document_repository=repository, run_repository=TranscriptParseRunRepository(),
            parser_version="webvtt_batch_probe_v1")
        summary = service.parse_day(connection, TARGET)
        documents_with_cues = sum(1 for d in summary["documents"] if d["cue_count"])
        assert documents_with_cues >= 1
        # One batched statement per document, never one per cue.
        assert calls["executemany"] == documents_with_cues
        assert summary["cues_written"] > 100
    finally:
        connection.rollback()
        connection.close()


# --- source evidence is untouched ------------------------------------------------------


def test_parsing_does_not_mutate_phase_2a_or_phase_2b_evidence():
    connection = _connection()
    try:
        _skip_without_combined(connection)

        def fingerprint():
            raw = connection.execute(
                "SELECT count(*), coalesce(sum(content_bytes), 0), "
                "       coalesce(md5(string_agg(content_sha256, ',' ORDER BY content_sha256)), '') "
                "FROM public.lecture_transcript_artifact_contents").fetchone()
            combined = connection.execute(
                "SELECT count(*), coalesce(sum(content_bytes), 0), "
                "       coalesce(md5(string_agg(content_sha256, ',' ORDER BY content_sha256)), ''), "
                "       coalesce(max(updated_at)::text, '') "
                "FROM public.lecture_combined_transcripts").fetchone()
            selections = connection.execute(
                "SELECT count(*), coalesce(max(updated_at)::text, '') "
                "FROM public.lecture_transcript_selections").fetchone()
            return raw, combined, selections

        before = fingerprint()
        _service().parse_day(connection, TARGET)
        assert fingerprint() == before
    finally:
        connection.rollback()
        connection.close()
