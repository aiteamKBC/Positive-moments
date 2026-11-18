"""
Manual transcript upload: the reads it validates against and the provenance it records.

NO NEW TABLE. An uploaded transcript is stored exactly where a Graph transcript is: as an
append-only content version of the Graph transcript ARTIFACT Teams listed for the lecture
(lecture_transcript_artifact_contents, through TranscriptArtifactRepository.store_content).
What makes it recognisably manual is its provenance, kept on the lecture <-> artifact row,
public.lecture_transcript_candidates.metadata -> 'manual_upload':

    source, upload_id, lecture_id, artifact_id, provider_transcript_id, content_sha256,
    content_bytes, content_version, uploaded_at, uploaded_by, original_filename,
    validator_version, file facts (cues, duration, speakers), and later `processing`.

That row is the right home for three reasons:
  * it names the EXACT lecture the file was confirmed for, and the exact transcript;
  * Graph ingestion never rewrites it - its upsert only refreshes last_seen_at - whereas the
    artifact's own metadata is replaced on every listing, which would erase the provenance;
  * "the stored content is the manual upload" is then a plain join:
    candidate.metadata.manual_upload.content_sha256 = artifact.content_sha256.

Never transcript text in metadata: counts, hashes and identities only.
"""
import json

from app.common.errors import DATABASE_ERROR, PlatformError


LOAD_LECTURE = """
SELECT l.lecture_id, l.subject, l.module, l.session_date, l.scheduled_start, l.scheduled_end,
       l.meeting_id, l.meeting_lookup_user_id, l.downstream_ready, l.is_cancelled,
       l.metadata -> 'duplicate_suppression' IS NOT NULL,
       l.meeting_lookup_context_source, l.join_url_oid_hint_status
  FROM public.lecture_sessions l
 WHERE l.lecture_id = %s
"""

# Everything about one artifact the validation and the console need, from the lecture's side.
ARTIFACT_STATE = """
SELECT a.artifact_id, a.provider_transcript_id, a.artifact_status, a.content_version,
       a.content_sha256, a.content_bytes, a.provider_created_at, a.provider_end_at,
       a.content_fetched_at, a.metadata ->> 'content_fetch_reason', c.metadata -> 'manual_upload'
  FROM public.lecture_transcript_artifacts a
  LEFT JOIN public.lecture_transcript_candidates c
    ON c.artifact_id = a.artifact_id AND c.lecture_id = %s
 WHERE a.artifact_id = ANY(%s::uuid[])
"""

# Which transcripts (and their lectures) already hold these exact bytes, in ANY version.
CONTENT_OWNERS = """
SELECT DISTINCT v.artifact_id, c.lecture_id, l.subject, l.session_date
  FROM public.lecture_transcript_artifact_contents v
  LEFT JOIN public.lecture_transcript_candidates c ON c.artifact_id = v.artifact_id
  LEFT JOIN public.lecture_sessions l ON l.lecture_id = c.lecture_id
 WHERE v.content_sha256 = %s
"""

UPLOADS_FOR_LECTURE = """
SELECT c.artifact_id, c.metadata -> 'manual_upload', a.content_sha256
  FROM public.lecture_transcript_candidates c
  JOIN public.lecture_transcript_artifacts a ON a.artifact_id = c.artifact_id
 WHERE c.lecture_id = %s AND c.metadata ? 'manual_upload'
 ORDER BY c.metadata -> 'manual_upload' ->> 'uploaded_at'
"""

# Lectures that MAY need a manual upload: a live, meeting-resolved lecture with at least one
# Teams transcript listed for it whose content was never stored. A prefilter only - the verdict
# is ManualTranscriptUploadService.target(), the same rule the per-lecture console applies.
LECTURES_MISSING_CONTENT = """
SELECT l.lecture_id
  FROM public.lecture_sessions l
 WHERE NOT l.is_cancelled
   AND l.metadata -> 'duplicate_suppression' IS NULL
   AND l.downstream_ready AND l.meeting_id IS NOT NULL AND l.meeting_lookup_user_id IS NOT NULL
   AND (%(date_from)s::date IS NULL OR l.session_date >= %(date_from)s::date)
   AND (%(date_to)s::date IS NULL OR l.session_date <= %(date_to)s::date)
   AND EXISTS (SELECT 1
                 FROM public.lecture_transcript_candidates c
                 JOIN public.lecture_transcript_artifacts a ON a.artifact_id = c.artifact_id
                WHERE c.lecture_id = l.lecture_id AND COALESCE(a.content_version, 0) = 0)
 ORDER BY l.session_date DESC, l.scheduled_start DESC NULLS LAST, l.lecture_id
 LIMIT %(limit)s
"""

LOCK_ARTIFACT = """
SELECT artifact_id FROM public.lecture_transcript_artifacts
 WHERE artifact_id = %s FOR UPDATE NOWAIT
"""

LOCK_SELECTION = """
SELECT selection_id FROM public.lecture_transcript_selections
 WHERE lecture_id = %s AND selection_version = %s FOR UPDATE NOWAIT
"""

RECORD_UPLOAD = """
UPDATE public.lecture_transcript_candidates
   SET metadata = metadata || jsonb_build_object('manual_upload', %s::jsonb)
 WHERE lecture_id = %s AND artifact_id = %s
RETURNING candidate_id
"""

RECORD_PROCESSING = """
UPDATE public.lecture_transcript_candidates
   SET metadata = jsonb_set(metadata, '{manual_upload,processing}', %s::jsonb, true)
 WHERE lecture_id = %s AND metadata ? 'manual_upload'
"""


class ManualTranscriptRepository:
    def load_lecture(self, connection, lecture_id) -> dict | None:
        try:
            row = connection.execute(LOAD_LECTURE, (str(lecture_id),)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "lecture lookup failed") from exc
        if row is None:
            return None
        keys = ("lecture_id", "subject", "module", "session_date", "scheduled_start",
                "scheduled_end", "meeting_id", "meeting_lookup_user_id", "downstream_ready",
                "is_cancelled", "is_suppressed_duplicate", "meeting_lookup_context_source",
                "join_url_oid_hint_status")
        return dict(zip(keys, row))

    def artifact_states(self, connection, lecture_id, artifact_ids) -> dict:
        ids = [str(item) for item in artifact_ids]
        if not ids:
            return {}
        try:
            rows = connection.execute(ARTIFACT_STATE, (str(lecture_id), ids)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript artifact state query failed") from exc
        states = {}
        for row in rows:
            manual = row[10]
            states[str(row[0])] = {
                "artifact_id": str(row[0]), "provider_transcript_id": row[1],
                "artifact_status": row[2], "content_version": row[3], "content_sha256": row[4],
                "content_bytes": row[5], "provider_created_at": row[6], "provider_end_at": row[7],
                "content_fetched_at": row[8], "content_fetch_reason": row[9],
                "manual_upload": manual,
                "content_source": ("MANUAL_UPLOAD" if manual and row[4]
                                   and manual.get("content_sha256") == row[4]
                                   else "MICROSOFT_GRAPH" if row[4] else None),
            }
        return states

    def content_owners(self, connection, content_sha256) -> list[dict]:
        try:
            rows = connection.execute(CONTENT_OWNERS, (content_sha256,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript content lookup failed") from exc
        return [{"artifact_id": str(row[0]), "lecture_id": str(row[1]) if row[1] else None,
                 "subject": row[2], "session_date": row[3]} for row in rows]

    def uploads_for_lecture(self, connection, lecture_id) -> list[dict]:
        try:
            rows = connection.execute(UPLOADS_FOR_LECTURE, (str(lecture_id),)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "manual upload provenance query failed") from exc
        return [{**(row[1] or {}), "artifact_id": str(row[0]),
                 "is_current_content": bool(row[1]) and row[1].get("content_sha256") == row[2]}
                for row in rows]

    def lectures_missing_content(self, connection, *, date_from=None, date_to=None,
                                 limit: int = 100) -> list[str]:
        try:
            rows = connection.execute(LECTURES_MISSING_CONTENT, {
                "date_from": date_from, "date_to": date_to, "limit": int(limit)}).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "missing transcript lecture query failed") from exc
        return [str(row[0]) for row in rows]

    def lock_artifact(self, connection, artifact_id) -> None:
        """The artifact row, NOWAIT: a writer holding it (QA's open cycle) refuses at once."""
        connection.execute(LOCK_ARTIFACT, (str(artifact_id),))

    def lock_selection(self, connection, lecture_id, selection_version) -> None:
        connection.execute(LOCK_SELECTION, (str(lecture_id), selection_version))

    def record_upload(self, connection, lecture_id, artifact_id, provenance: dict) -> None:
        try:
            row = connection.execute(RECORD_UPLOAD, (json.dumps(provenance), str(lecture_id),
                                                     str(artifact_id))).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "manual upload provenance write failed") from exc
        if row is None:
            # Validation proved the link exists; losing it in between is not something to paper over.
            raise PlatformError(DATABASE_ERROR, "the lecture no longer lists this transcript")

    def record_processing(self, connection, lecture_id, outcome: dict) -> None:
        try:
            connection.execute(RECORD_PROCESSING, (json.dumps(outcome), str(lecture_id)))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "manual upload processing record failed") from exc
