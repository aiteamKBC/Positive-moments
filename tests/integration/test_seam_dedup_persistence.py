"""
Phase 3C2.3B integration tests: the versioned v2 canonical document.

These run against the real database. Every test either rolls its transaction
back or asserts read-only facts, so no production row is created or changed.
The Phase 3C2 canary and all canonical v1 evidence must survive untouched.
"""
import hashlib
from datetime import date

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.transcript_documents import TranscriptDocumentRepository
from app.transcripts.seam import (
    DEDUP_POLICY_VERSION,
    SEAM_PARSER_VERSION,
    TranscriptPart,
    parse_parts_with_seam_dedup,
)
from app.transcripts.seam_service import SeamDedupService, SeamInputError
from app.transcripts.webvtt import PARSER_VERSION

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


ANDREW = "25e85615-aa7a-5f49-bb40-078d7c7b65d0"
CANARY = "8c2874d7-3db5-5870-ae47-4f745a847540"
CANARY_DIGEST = "5092175cee6d2a25be1236eb8d2ae56f536bded0950f8f8ff2968122878f3f43"


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _service():
    return SeamDedupService(document_repository=TranscriptDocumentRepository())


def _document(connection, lecture_id, version):
    return connection.execute(
        """SELECT document_id, cue_count, first_cue_start_ms, last_cue_end_ms,
                  duration_ms, parse_status, source_fingerprint, overlapping_cue_count
             FROM public.lecture_transcript_documents
            WHERE lecture_id = %s AND parser_version = %s""",
        (lecture_id, version)).fetchone()


def _cue_digest(connection, document_id):
    rows = connection.execute(
        """SELECT cue_index, start_ms, end_ms, cue_text_sha256,
                  coalesce(speaker_label_raw, '')
             FROM public.lecture_transcript_cues WHERE document_id = %s
            ORDER BY cue_index""", (document_id,)).fetchall()
    return len(rows), hashlib.sha256("".join(map(str, rows)).encode()).hexdigest()


def _require_andrew(connection):
    if _document(connection, ANDREW, PARSER_VERSION) is None:
        pytest.skip("the multi-part fixture is not present in this database")


# --- the two versions coexist -------------------------------------------------------

def test_v1_and_v2_documents_coexist_for_the_same_lecture():
    with _connection() as connection:
        _require_andrew(connection)
        v1 = _document(connection, ANDREW, PARSER_VERSION)
        v2 = _document(connection, ANDREW, SEAM_PARSER_VERSION)
        assert v1 is not None and v2 is not None
        assert v1[0] != v2[0], "each version must own a separate document"
        connection.rollback()


# --- v2 deduplicates, v1 does not ----------------------------------------------------

def test_v2_drops_only_the_duplicated_seam_cues():
    with _connection() as connection:
        _require_andrew(connection)
        v1 = _document(connection, ANDREW, PARSER_VERSION)
        v2 = _document(connection, ANDREW, SEAM_PARSER_VERSION)
        assert v2[1] < v1[1], "v2 must contain fewer cues than v1"
        assert v1[1] - v2[1] == 9
        connection.rollback()


def test_v2_preserves_the_call_relative_origin_and_the_span_duration():
    with _connection() as connection:
        _require_andrew(connection)
        v1 = _document(connection, ANDREW, PARSER_VERSION)
        v2 = _document(connection, ANDREW, SEAM_PARSER_VERSION)
        assert v2[2] == v1[2], "the 02:47 origin must not be rebased"
        assert v2[3] == v1[3]
        assert v2[4] == v2[3] - v2[2], "duration is a span, not measured from zero"
        assert v2[4] == v1[4]
        connection.rollback()


def test_no_later_part_cue_survives_inside_earlier_coverage():
    with _connection() as connection:
        _require_andrew(connection)
        document_id = _document(connection, ANDREW, SEAM_PARSER_VERSION)[0]
        rows = connection.execute("""
            SELECT (metadata ->> 'source_part_index')::int, start_ms, end_ms
              FROM public.lecture_transcript_cues WHERE document_id = %s""",
            (document_id,)).fetchall()
        coverage_end = max(end for part, _, end in rows if part == 1)
        intruders = [start for part, start, _ in rows if part > 1 and start < coverage_end]
        assert intruders == [], f"{len(intruders)} duplicate-coverage cues survived"
        connection.rollback()


def test_the_v2_timeline_never_steps_backwards():
    with _connection() as connection:
        _require_andrew(connection)
        for version, allowed in ((SEAM_PARSER_VERSION, 0), (PARSER_VERSION, 1)):
            document_id = _document(connection, ANDREW, version)[0]
            starts = [row[0] for row in connection.execute(
                """SELECT start_ms FROM public.lecture_transcript_cues
                    WHERE document_id = %s ORDER BY cue_index""", (document_id,)).fetchall()]
            backwards = sum(1 for i in range(1, len(starts)) if starts[i] < starts[i - 1])
            assert backwards == allowed, (version, backwards)
        connection.rollback()


def test_within_part_overlap_is_preserved_rather_than_deduplicated():
    with _connection() as connection:
        _require_andrew(connection)
        v2 = _document(connection, ANDREW, SEAM_PARSER_VERSION)
        # 84 of v1's 93 overlapping cues are legitimate within-part overlap and
        # must survive; only the 9 cross-part duplicates go.
        assert v2[7] == 84
        connection.rollback()


# --- provenance and audit -------------------------------------------------------------

def test_every_v2_cue_carries_its_source_part_artifact_and_offset():
    with _connection() as connection:
        _require_andrew(connection)
        document_id = _document(connection, ANDREW, SEAM_PARSER_VERSION)[0]
        missing = connection.execute("""
            SELECT count(*) FROM public.lecture_transcript_cues
             WHERE document_id = %s
               AND NOT (metadata ? 'source_part_index' AND metadata ? 'source_artifact_id'
                        AND metadata ? 'applied_offset_ms' AND metadata ? 'raw_start_ms'
                        AND metadata ? 'source_cue_index')""", (document_id,)).fetchone()[0]
        assert missing == 0
        connection.rollback()


def test_the_document_metadata_records_the_seam_audit():
    with _connection() as connection:
        _require_andrew(connection)
        metadata = connection.execute(
            """SELECT metadata FROM public.lecture_transcript_documents
                WHERE lecture_id = %s AND parser_version = %s""",
            (ANDREW, SEAM_PARSER_VERSION)).fetchone()[0]
        assert metadata["dedup_policy_version"] == DEDUP_POLICY_VERSION
        assert metadata["part_count"] == 2
        assert metadata["seam_dropped_cue_count"] == 9
        seam = metadata["seams"][0]
        for key in ("earlier_part_index", "later_part_index", "earlier_coverage_end_ms",
                    "later_raw_first_start_ms", "later_shifted_first_start_ms",
                    "seam_overlap_ms", "later_cues_examined", "dropped_cue_count",
                    "boundary_straddling_dropped_count", "maximum_dropped_tail_ms",
                    "kept_cue_count"):
            assert key in seam, key
        assert seam["seam_overlap_ms"] == 61005
        assert seam["boundary_straddling_dropped_count"] == 1
        connection.rollback()


# --- nothing upstream was disturbed -----------------------------------------------------

def test_the_phase_2b_selection_and_offsets_are_unchanged():
    with _connection() as connection:
        _require_andrew(connection)
        row = connection.execute(
            """SELECT selection_id, selection_version, selected_part_count
                 FROM public.lecture_transcript_selections WHERE lecture_id = %s""",
            (ANDREW,)).fetchone()
        assert row[1] == "legacy_qa_v8_overlap_cluster_v1"
        assert row[2] == 2
        parts = connection.execute(
            """SELECT part_index, part_offset_ms, is_primary
                 FROM public.lecture_transcript_selection_parts
                WHERE selection_id = %s ORDER BY part_index""", (row[0],)).fetchall()
        assert [tuple(p) for p in parts] == [(1, 0, True), (2, 14336082, False)]
        connection.rollback()


def test_the_raw_artifacts_were_never_rewritten():
    with _connection() as connection:
        _require_andrew(connection)
        rows = connection.execute("""
            SELECT a.content_version, ac.version_number, a.content_sha256, ac.content_sha256
              FROM public.lecture_transcript_selection_parts sp
              JOIN public.lecture_transcript_artifacts a ON a.artifact_id = sp.artifact_id
              JOIN public.lecture_transcript_artifact_contents ac ON ac.artifact_id = a.artifact_id
             WHERE sp.selection_id = (SELECT selection_id FROM public.lecture_transcript_selections
                                       WHERE lecture_id = %s)""", (ANDREW,)).fetchall()
        assert rows
        for content_version, version_number, artifact_sha, content_sha in rows:
            assert content_version == 1 and version_number == 1, "a new content version was written"
            assert artifact_sha == content_sha
        connection.rollback()


def test_the_v1_document_and_cues_are_untouched():
    with _connection() as connection:
        _require_andrew(connection)
        v1 = _document(connection, ANDREW, PARSER_VERSION)
        assert v1[1] == 601
        assert (v1[2], v1[3], v1[4]) == (10026775, 17195372, 7168597)
        assert v1[7] == 93, "v1 keeps the duplicated seam, exactly as legacy does"
        assert v1[6].startswith("158bb1a7ad9b09a9")
        connection.rollback()


# --- rebuilding is deterministic and scoped ----------------------------------------------

def test_rebuilding_twice_changes_nothing():
    with _connection() as connection:
        _require_andrew(connection)
        document_id = _document(connection, ANDREW, SEAM_PARSER_VERSION)[0]
        before = _cue_digest(connection, document_id)
        first = _service().rebuild_lecture(connection, ANDREW)
        after_one = _cue_digest(connection, document_id)
        second = _service().rebuild_lecture(connection, ANDREW)
        after_two = _cue_digest(connection, document_id)
        assert before == after_one == after_two
        assert first["seam_fingerprint"] == second["seam_fingerprint"]
        assert first["cue_count"] == second["cue_count"] == 592
        connection.rollback()


def test_a_dry_run_rebuild_writes_nothing():
    with _connection() as connection:
        _require_andrew(connection)
        before = connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_documents").fetchone()[0]
        result = _service().rebuild_lecture(connection, ANDREW, persist=False)
        assert "document_persistence" not in result
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_documents").fetchone()[0] == before
        connection.rollback()


def test_the_rebuild_makes_no_provider_or_graph_call():
    with _connection() as connection:
        _require_andrew(connection)
        result = _service().rebuild_lecture(connection, ANDREW, persist=False)
        assert result["graph_calls"] == 0
        assert result["provider_calls"] == 0
        assert result["legacy_qa_writes"] == 0
        assert result["raw_artifacts_written"] == 0
        assert result["selections_written"] == 0
        connection.rollback()


# --- single-part lectures are unaffected ---------------------------------------------------

def test_v2_reproduces_every_single_part_lecture_exactly():
    with _connection() as connection:
        rows = connection.execute("""
            SELECT d.document_id, sp.part_index, sp.part_offset_ms, ac.raw_content
              FROM public.lecture_transcript_selections sel
              JOIN public.lecture_transcript_documents d ON d.selection_id = sel.selection_id
                   AND d.parser_version = %s
              JOIN public.lecture_transcript_selection_parts sp ON sp.selection_id = sel.selection_id
              JOIN public.lecture_transcript_artifacts a ON a.artifact_id = sp.artifact_id
              JOIN public.lecture_transcript_artifact_contents ac ON ac.artifact_id = a.artifact_id
             WHERE sel.selected_part_count = 1""", (PARSER_VERSION,)).fetchall()
        if not rows:
            pytest.skip("no single-part lectures present")
        for document_id, index, offset, raw in rows:
            rebuilt = parse_parts_with_seam_dedup(
                [TranscriptPart(index, offset, raw, str(document_id), None)])
            stored = connection.execute("""
                SELECT cue_index, start_ms, end_ms, cue_text_sha256
                  FROM public.lecture_transcript_cues WHERE document_id = %s
                 ORDER BY cue_index""", (document_id,)).fetchall()
            assert rebuilt.seams == [] and rebuilt.dropped_cue_count == 0
            assert [(c.cue_index, c.start_ms, c.end_ms, c.cue_text_sha256)
                    for c in rebuilt.cues] == [tuple(r) for r in stored]
        connection.rollback()


# --- the production canary is none of this phase's business ----------------------------------

def test_the_first_production_canary_is_untouched():
    from app.writer import mapping
    with _connection() as connection:
        session_id = connection.execute(
            """SELECT session_id FROM public.lecture_qa_rendered_sessions
                WHERE lecture_id = %s""", (CANARY,)).fetchone()
        if session_id is None:
            pytest.skip("the canary payload is not present")
        session_id = session_id[0]
        session = dict(zip(mapping.SESSION_COLUMNS, connection.execute(
            "SELECT " + ", ".join(f'"{c}"' for c in mapping.SESSION_COLUMNS) +
            " FROM public.qa_doctors_sessions WHERE session_id = %s", (session_id,)).fetchone()))
        items = [dict(zip(mapping.CHECKLIST_COLUMNS, row)) for row in connection.execute(
            "SELECT " + ", ".join(mapping.CHECKLIST_COLUMNS) +
            " FROM public.qa_doctors_checklist_items WHERE session_id = %s"
            " ORDER BY checklist_order", (session_id,)).fetchall()]
        assert len(items) == 11
        assert mapping.digest(session, items) == CANARY_DIGEST
        assert connection.execute(
            """SELECT write_status FROM public.lecture_qa_legacy_writes
                WHERE legacy_session_id = %s""", (session_id,)).fetchone()[0] == "WRITTEN"
        connection.rollback()


def test_the_multi_part_legacy_rows_exist_exactly_once_and_are_coded_owned():
    """
    Phase 3C2.3B asserted no QA evaluation or render; 3C2.3C produced both;
    3C2.3E wrote the legacy rows for real. Each assertion was replaced by the
    next invariant that still matters. What matters now is that the seam work
    reached production exactly once and under coded ownership - a second row,
    or a row nobody owns, would mean the seam pipeline wrote twice.
    """
    with _connection() as connection:
        session_id = connection.execute(
            """SELECT primary_provider_transcript_id
                 FROM public.lecture_transcript_selections WHERE lecture_id = %s""",
            (ANDREW,)).fetchone()
        if session_id is None:
            pytest.skip("the multi-part fixture is not present in this database")
        session_id = session_id[0]
        for table, column, expected in (
                ("qa_doctors_sessions", "session_id", 1),
                ("qa_doctors_checklist_items", "session_id", 11),
                ("qa_perfect_lectures", "session_id", 1),
                ("lecture_qa_legacy_writes", "legacy_session_id", 1)):
            assert connection.execute(
                f"SELECT count(*) FROM public.{table} WHERE {column} = %s",
                (session_id,)).fetchone()[0] == expected, table
        # The legacy session id written is the seam selection's primary
        # provider transcript id, not some other part's.
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_qa_legacy_writes "
            " WHERE lecture_id = %s AND write_status = 'WRITTEN'",
            (ANDREW,)).fetchone()[0] == 1
        connection.rollback()


def test_any_qa_evaluation_for_the_multi_part_lecture_uses_the_v2_document():
    """If Phase 3A has run for it, it must have consumed the deduplicated document."""
    with _connection() as connection:
        rows = connection.execute("""
            SELECT d.parser_version FROM public.lecture_qa_evaluations e
              JOIN public.lecture_transcript_documents d ON d.document_id = e.document_id
             WHERE e.lecture_id = %s""", (ANDREW,)).fetchall()
        assert all(row[0] == SEAM_PARSER_VERSION for row in rows), rows
        connection.rollback()
