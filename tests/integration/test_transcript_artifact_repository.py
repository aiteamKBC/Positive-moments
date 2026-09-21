"""
Phase 2A database behaviour, all inside a transaction that is rolled back.

Every test asserts against the real schema and then discards its writes, so no
Phase 2A test ever leaves a row behind.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from app.common.hashing import content_sha256, provider_transcript_identity
from app.config.settings import Settings
from app.db.repositories.ready_lectures import ReadyLectureRepository
from app.db.repositories.transcript_artifacts import TranscriptArtifactRepository
from app.db.repositories.transcript_runs import TranscriptAcquisitionRunRepository
from app.transcripts.models import PROVIDER_MICROSOFT_GRAPH, VTT


VTT_ONE = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Jane Doe>Hello.</v>\n"
VTT_TWO = VTT_ONE + b"\n00:00:03.000 --> 00:00:04.000\n<v Jane Doe>Again.</v>\n"


def _connection():
    settings = Settings.from_environment()
    if not settings.database_url:
        pytest.skip("DATABASE_URL is not configured")
    return psycopg.connect(settings.database_url)


def _any_lecture_id(connection):
    row = connection.execute(
        "SELECT lecture_id FROM public.lecture_sessions ORDER BY session_date DESC LIMIT 1"
    ).fetchone()
    if not row:
        pytest.skip("no canonical lecture rows to attach a transcript artifact to")
    return row[0]


def _register(repository, connection, lecture_id, transcript_id):
    artifact_id = provider_transcript_identity(
        provider=PROVIDER_MICROSOFT_GRAPH, provider_transcript_id=transcript_id)
    created = datetime(2026, 9, 4, 7, 58, tzinfo=timezone.utc)
    outcome = repository.upsert(
        connection, artifact_id, lecture_id,
        provider=PROVIDER_MICROSOFT_GRAPH, provider_transcript_id=transcript_id,
        meeting_id="meeting-test", meeting_lookup_user_id="user-object-id-test",
        provider_call_id="call-test", content_correlation_id="corr-test",
        provider_created_at=created, provider_end_at=created + timedelta(hours=2),
        artifact_status="DISCOVERED", metadata={"probe": True},
    )
    return artifact_id, outcome


def test_artifact_upsert_is_idempotent_and_content_versions_are_append_only():
    connection = _connection()
    repository = TranscriptArtifactRepository()
    transcript_id = f"transcript-{uuid.uuid4().hex}"
    try:
        lecture_id = _any_lecture_id(connection)
        artifact_id, first = _register(repository, connection, lecture_id, transcript_id)
        _, second = _register(repository, connection, lecture_id, transcript_id)
        assert (first, second) == ("created", "existing")
        assert connection.execute(
            "SELECT count(*) FROM public.lecture_transcript_artifacts WHERE artifact_id = %s",
            (artifact_id,)).fetchone()[0] == 1

        # First content version.
        digest_one = content_sha256(VTT_ONE)
        stored = repository.store_content(
            connection, artifact_id, raw_text=VTT_ONE.decode(), content_sha256=digest_one,
            content_bytes=len(VTT_ONE), content_format=VTT, speaker_attribution=True)
        assert stored == {"version_number": 1, "version_created": True}

        # Identical bytes must NOT create duplicate evidence.
        again = repository.store_content(
            connection, artifact_id, raw_text=VTT_ONE.decode(), content_sha256=digest_one,
            content_bytes=len(VTT_ONE), content_format=VTT, speaker_attribution=True)
        assert again == {"version_number": 1, "version_created": False}

        # Changed bytes append a version and preserve the old one.
        digest_two = content_sha256(VTT_TWO)
        changed = repository.store_content(
            connection, artifact_id, raw_text=VTT_TWO.decode(), content_sha256=digest_two,
            content_bytes=len(VTT_TWO), content_format=VTT, speaker_attribution=True)
        assert changed == {"version_number": 2, "version_created": True}

        versions = connection.execute(
            "SELECT version_number, content_sha256, raw_content "
            "FROM public.lecture_transcript_artifact_contents "
            "WHERE artifact_id = %s ORDER BY version_number", (artifact_id,)).fetchall()
        assert [row[0] for row in versions] == [1, 2]
        assert [row[1] for row in versions] == [digest_one, digest_two]
        assert versions[0][2] == VTT_ONE.decode()      # original evidence intact

        pointer = connection.execute(
            "SELECT content_sha256, content_version, content_bytes, artifact_status, "
            "       speaker_attribution, content_format "
            "FROM public.lecture_transcript_artifacts WHERE artifact_id = %s",
            (artifact_id,)).fetchone()
        assert pointer == (digest_two, 2, len(VTT_TWO), "CONTENT_STORED", True, VTT)
    finally:
        connection.rollback()
        connection.close()


def test_stored_content_is_hash_verifiable():
    connection = _connection()
    repository = TranscriptArtifactRepository()
    try:
        lecture_id = _any_lecture_id(connection)
        artifact_id, _ = _register(repository, connection, lecture_id,
                                   f"transcript-{uuid.uuid4().hex}")
        digest = content_sha256(VTT_ONE)
        repository.store_content(
            connection, artifact_id, raw_text=VTT_ONE.decode(), content_sha256=digest,
            content_bytes=len(VTT_ONE), content_format=VTT, speaker_attribution=True)
        raw, stored_hash = connection.execute(
            "SELECT raw_content, content_sha256 FROM public.lecture_transcript_artifact_contents "
            "WHERE artifact_id = %s", (artifact_id,)).fetchone()
        assert content_sha256(raw.encode("utf-8")) == stored_hash == digest
    finally:
        connection.rollback()
        connection.close()


def test_candidate_link_requires_an_existing_canonical_lecture():
    """The foreign key points at lecture_sessions, never at qa_doctors_sessions."""
    connection = _connection()
    try:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                "INSERT INTO public.lecture_transcript_candidates "
                "(candidate_id, lecture_id, artifact_id, meeting_id, meeting_lookup_user_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "m", "u"),
            )
    finally:
        connection.rollback()
        connection.close()


def test_database_rejects_an_incomplete_content_pointer():
    connection = _connection()
    repository = TranscriptArtifactRepository()
    try:
        lecture_id = _any_lecture_id(connection)
        artifact_id, _ = _register(repository, connection, lecture_id,
                                   f"transcript-{uuid.uuid4().hex}")
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "UPDATE public.lecture_transcript_artifacts "
                "SET content_sha256 = %s, content_version = 1 WHERE artifact_id = %s",
                ("not-a-sha", artifact_id),
            )
    finally:
        connection.rollback()
        connection.close()


def test_ready_lecture_repository_splits_ready_from_unresolved():
    connection = _connection()
    try:
        result = ReadyLectureRepository().load_day(connection, date(2026, 9, 4))
        assert result["considered"] == len(result["ready"]) + len(result["skipped"])
        for lecture in result["ready"]:
            assert lecture.meeting_id and lecture.meeting_lookup_user_id
        for skipped in result["skipped"]:
            assert skipped["skip_reason"] == "NOT_DOWNSTREAM_READY"
            assert skipped["downstream_ready"] is False
    finally:
        connection.rollback()
        connection.close()


def test_acquisition_run_audit_records_counters_and_rolls_back():
    connection = _connection()
    runs = TranscriptAcquisitionRunRepository()
    try:
        run_id = runs.start(connection, date(2026, 9, 4))
        runs.complete(connection, run_id, {
            "status": "COMPLETED", "lectures_considered": 8, "lectures_ready": 7,
            "lectures_skipped_not_ready": 1, "meetings_queried": 7,
            "meetings_with_zero_artifacts": 0, "meetings_with_artifacts": 7,
            "artifacts_discovered": 79, "artifacts_created": 79, "artifacts_existing": 0,
            "artifact_contents_fetched": 79, "content_versions_created": 79,
            "content_unchanged": 0, "admin_blocked_count": 0,
            "speaker_attribution_fallback_count": 0, "error_count": 0,
            "metadata": {"test": True},
        })
        row = connection.execute(
            "SELECT status, lectures_ready, artifacts_discovered, metadata "
            "FROM public.lecture_transcript_acquisition_runs WHERE run_id = %s",
            (run_id,)).fetchone()
        assert row[:3] == ("COMPLETED", 7, 79)
        # Audit metadata must never carry transcript text or credentials.
        assert "WEBVTT" not in str(row[3])
    finally:
        connection.rollback()
        connection.close()


def test_qa_doctors_transcripts_is_not_used_by_phase_2a():
    """Phase 2A must leave the legacy transcript table completely alone."""
    connection = _connection()
    try:
        before = connection.execute(
            "SELECT count(*) FROM public.qa_doctors_transcripts").fetchone()[0]
        repository = TranscriptArtifactRepository()
        lecture_id = _any_lecture_id(connection)
        artifact_id, _ = _register(repository, connection, lecture_id,
                                   f"transcript-{uuid.uuid4().hex}")
        repository.store_content(
            connection, artifact_id, raw_text=VTT_ONE.decode(),
            content_sha256=content_sha256(VTT_ONE), content_bytes=len(VTT_ONE),
            content_format=VTT, speaker_attribution=True)
        after = connection.execute(
            "SELECT count(*) FROM public.qa_doctors_transcripts").fetchone()[0]
        assert after == before
    finally:
        connection.rollback()
        connection.close()
