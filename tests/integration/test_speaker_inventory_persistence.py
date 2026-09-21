"""
Phase 2C2 persistence against the real 2026-09-04 canonical cues.

Every test runs inside a transaction that is rolled back, so nothing here
leaves a row behind and no canonical evidence is touched.
"""
import uuid
from datetime import date

import psycopg
import pytest

from app.config.settings import Settings
from app.db.repositories.transcript_speakers import (
    LegacyTrainerDiagnosticRepository,
    SpeakerInventoryRunRepository,
    TranscriptSpeakerRepository,
)
from app.transcripts.speaker_service import SpeakerInventoryService
from app.transcripts.speakers import SPEAKER_INVENTORY_VERSION, normalize_speaker_label


TARGET = date(2026, 9, 4)


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _service(inventory_version=SPEAKER_INVENTORY_VERSION, legacy=False):
    return SpeakerInventoryService(
        speaker_repository=TranscriptSpeakerRepository(),
        run_repository=SpeakerInventoryRunRepository(),
        legacy_diagnostic_repository=LegacyTrainerDiagnosticRepository() if legacy else None,
        inventory_version=inventory_version,
    )


def _skip_without_cues(connection):
    if connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_cues").fetchone()[0] == 0:
        pytest.skip("no canonical cues persisted")


def test_every_document_yields_one_row_per_distinct_raw_label():
    connection = _connection()
    try:
        _skip_without_cues(connection)
        summary = _service().build_day(connection, TARGET)
        assert summary["documents_considered"] >= 1
        for document in summary["documents"]:
            expected = connection.execute(
                "SELECT count(DISTINCT speaker_label_raw) "
                "FROM public.lecture_transcript_cues "
                "WHERE document_id = %s AND speaker_label_raw IS NOT NULL",
                (document["document_id"],)).fetchone()[0]
            stored = connection.execute(
                "SELECT count(*) FROM public.lecture_transcript_speakers "
                "WHERE document_id = %s AND speaker_inventory_version = %s",
                (document["document_id"], SPEAKER_INVENTORY_VERSION)).fetchone()[0]
            assert stored == expected == document["distinct_raw_speaker_count"]
            # One row per label, never one per cue.
            assert stored < document["document_cue_count"]
    finally:
        connection.rollback()
        connection.close()


def test_aggregates_match_the_cues_they_came_from():
    connection = _connection()
    try:
        _skip_without_cues(connection)
        _service().build_day(connection, TARGET)
        mismatches = connection.execute("""
            SELECT count(*)
              FROM public.lecture_transcript_speakers s
              JOIN (SELECT document_id, speaker_label_raw,
                           count(*)               AS cue_count,
                           min(cue_index)         AS first_cue_index,
                           max(cue_index)         AS last_cue_index,
                           min(start_ms)          AS first_spoken_start_ms,
                           max(end_ms)            AS last_spoken_end_ms,
                           sum(end_ms - start_ms) AS gross_spoken_ms
                      FROM public.lecture_transcript_cues
                     WHERE speaker_label_raw IS NOT NULL
                     GROUP BY 1, 2) c
                ON c.document_id = s.document_id
               AND c.speaker_label_raw = s.speaker_label_raw
             WHERE s.cue_count            <> c.cue_count
                OR s.first_cue_index      <> c.first_cue_index
                OR s.last_cue_index       <> c.last_cue_index
                OR s.first_spoken_start_ms <> c.first_spoken_start_ms
                OR s.last_spoken_end_ms   <> c.last_spoken_end_ms
                OR s.gross_spoken_ms      <> c.gross_spoken_ms
        """).fetchone()[0]
        assert mismatches == 0

        # And every stored speaker actually joined to a cue group.
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_speakers").fetchone()[0] >= 1
    finally:
        connection.rollback()
        connection.close()


def test_all_cues_with_a_label_are_accounted_for():
    connection = _connection()
    try:
        _skip_without_cues(connection)
        summary = _service().build_day(connection, TARGET)
        labelled = connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_cues c "
            "  JOIN public.lecture_transcript_documents d USING (document_id) "
            "  JOIN public.lecture_sessions l ON l.lecture_id = d.lecture_id "
            " WHERE l.session_date = %s AND c.speaker_label_raw IS NOT NULL", (TARGET,)
        ).fetchone()[0]
        assert summary["cues_aggregated"] == labelled
    finally:
        connection.rollback()
        connection.close()


def test_raw_labels_are_byte_identical_to_the_cue_labels():
    connection = _connection()
    try:
        _skip_without_cues(connection)
        _service().build_day(connection, TARGET)
        orphans = connection.execute("""
            SELECT count(*) FROM public.lecture_transcript_speakers s
             WHERE NOT EXISTS (
               SELECT 1 FROM public.lecture_transcript_cues c
                WHERE c.document_id = s.document_id
                  AND c.speaker_label_raw = s.speaker_label_raw)
        """).fetchone()[0]
        assert orphans == 0
    finally:
        connection.rollback()
        connection.close()


def test_normalized_labels_are_the_conservative_form():
    connection = _connection()
    try:
        _skip_without_cues(connection)
        _service().build_day(connection, TARGET)
        rows = connection.execute(
            "SELECT speaker_label_raw, speaker_label_normalized "
            "FROM public.lecture_transcript_speakers").fetchall()
        assert rows
        for raw, normalized in rows:
            assert normalized == normalize_speaker_label(raw)
    finally:
        connection.rollback()
        connection.close()


def test_overlap_relationship_between_gross_speech_and_duration_is_reported():
    connection = _connection()
    try:
        _skip_without_cues(connection)
        summary = _service().build_day(connection, TARGET)
        for document in summary["documents"]:
            stored_total = connection.execute(
                "SELECT coalesce(sum(gross_spoken_ms), 0) "
                "FROM public.lecture_transcript_speakers WHERE document_id = %s",
                (document["document_id"],)).fetchone()[0]
            assert stored_total == document["gross_spoken_ms_total"]
            assert document["gross_exceeds_document_duration"] == (
                document["gross_spoken_ms_total"] > (document["document_duration_ms"] or 0))
    finally:
        connection.rollback()
        connection.close()


def test_second_run_creates_no_duplicate_speaker_rows():
    connection = _connection()
    try:
        _skip_without_cues(connection)
        service = _service()
        first = service.build_day(connection, TARGET)
        second = service.build_day(connection, TARGET)

        written = first["speakers_created"] + first["speakers_updated"]
        assert written >= 1
        assert second["speakers_created"] == 0
        assert second["speakers_updated"] == written
        assert connection.execute(
            "SELECT count(*) FROM (SELECT document_id, speaker_inventory_version, "
            "        speaker_label_raw FROM public.lecture_transcript_speakers "
            " GROUP BY 1,2,3 HAVING count(*) > 1) t").fetchone()[0] == 0
        assert [d["distinct_raw_speaker_count"] for d in first["documents"]] == \
               [d["distinct_raw_speaker_count"] for d in second["documents"]]
    finally:
        connection.rollback()
        connection.close()


def test_a_second_inventory_version_coexists_without_reusing_ids():
    connection = _connection()
    try:
        _skip_without_cues(connection)
        _service().build_day(connection, TARGET)
        probe = _service(inventory_version="speaker_inventory_probe_v2")
        summary = probe.build_day(connection, TARGET)
        assert summary["speakers_created"] >= 1
        document_id = summary["documents"][0]["document_id"]
        versions = {row[0] for row in connection.execute(
            "SELECT speaker_inventory_version FROM public.lecture_transcript_speakers "
            "WHERE document_id = %s", (document_id,)).fetchall()}
        assert versions == {SPEAKER_INVENTORY_VERSION, "speaker_inventory_probe_v2"}
        shared = connection.execute(
            "SELECT count(*) FROM (SELECT speaker_id FROM public.lecture_transcript_speakers "
            " GROUP BY 1 HAVING count(DISTINCT speaker_inventory_version) > 1) t").fetchone()[0]
        assert shared == 0
    finally:
        connection.rollback()
        connection.close()


def test_database_rejects_a_duplicate_label_for_one_document_and_version():
    connection = _connection()
    try:
        _skip_without_cues(connection)
        summary = _service().build_day(connection, TARGET)
        document_id = summary["documents"][0]["document_id"]
        existing = connection.execute(
            "SELECT speaker_label_raw FROM public.lecture_transcript_speakers "
            "WHERE document_id = %s LIMIT 1", (document_id,)).fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                "INSERT INTO public.lecture_transcript_speakers "
                "(speaker_id, document_id, speaker_inventory_version, speaker_label_raw, "
                " speaker_label_normalized, cue_count, first_cue_index, last_cue_index, "
                " first_spoken_start_ms, last_spoken_end_ms, gross_spoken_ms) "
                "VALUES (%s, %s, %s, %s, 'x', 1, 1, 1, 0, 1, 1)",
                (uuid.uuid4(), document_id, SPEAKER_INVENTORY_VERSION, existing))
    finally:
        connection.rollback()
        connection.close()


def test_canonical_documents_and_cues_are_never_modified():
    connection = _connection()
    try:
        _skip_without_cues(connection)

        def fingerprint():
            cues = connection.execute(
                "SELECT count(*), coalesce(md5(string_agg(cue_text_sha256, ',' "
                "        ORDER BY document_id, cue_index)), ''), "
                "       coalesce(sum(end_ms - start_ms), 0) "
                "FROM public.lecture_transcript_cues").fetchone()
            documents = connection.execute(
                "SELECT count(*), coalesce(sum(cue_count), 0), "
                "       coalesce(max(updated_at)::text, '') "
                "FROM public.lecture_transcript_documents").fetchone()
            return cues, documents

        before = fingerprint()
        _service(legacy=True).build_day(connection, TARGET)
        assert fingerprint() == before
    finally:
        connection.rollback()
        connection.close()


def test_legacy_trainer_diagnostic_is_read_only_and_assigns_no_role():
    connection = _connection()
    try:
        _skip_without_cues(connection)
        before = connection.execute(
            "SELECT count(*), coalesce(md5(string_agg(coalesce(trainer, ''), ',' "
            "        ORDER BY session_id)), '') "
            "FROM public.qa_doctors_sessions WHERE date = %s", (TARGET,)).fetchone()
        summary = _service(legacy=True).build_day(connection, TARGET)
        diagnostic = summary["legacy_trainer_diagnostic"]
        assert diagnostic is not None
        assert diagnostic["qa_rows"] >= 1
        assert all(row["legacy_trainer_present_as_exact_raw_label"] in ("YES", "NO")
                   for row in diagnostic["rows"])
        # qa_doctors_sessions is untouched.
        assert connection.execute(
            "SELECT count(*), coalesce(md5(string_agg(coalesce(trainer, ''), ',' "
            "        ORDER BY session_id)), '') "
            "FROM public.qa_doctors_sessions WHERE date = %s", (TARGET,)).fetchone() == before
        # No role column exists on the speaker table at all.
        columns = {row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' "
            "  AND table_name='lecture_transcript_speakers'").fetchall()}
        assert not columns & {"role", "is_trainer", "is_learner", "person_id",
                              "attendance_id", "lms_user_id", "speaker_share_percent"}
    finally:
        connection.rollback()
        connection.close()


def test_external_attendance_and_lms_tables_are_never_read_for_identity():
    """Phase 2C2 resolves no person, so it must not consult those systems."""
    connection = _connection()
    try:
        _skip_without_cues(connection)
        statements = []
        original = psycopg.Connection.execute

        def recording(self, query, params=None, **kwargs):
            statements.append(str(query))
            return original(self, query, params, **kwargs)

        psycopg.Connection.execute = recording
        try:
            _service(legacy=True).build_day(connection, TARGET)
        finally:
            psycopg.Connection.execute = original

        combined = " ".join(statements).lower()
        assert "kbc_attendance" not in combined
        assert "kbc_users_data" not in combined
        assert "aptem_auto_extracting" not in combined
    finally:
        connection.rollback()
        connection.close()
