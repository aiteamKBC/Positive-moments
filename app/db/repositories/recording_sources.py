"""
P0-C persistence: bunny_recording_discoveries (one row per lecture) and
bunny_recording_sources (one row per Graph recording part), migration 027.

Every write is one short transaction run by the caller. The part rows of one
evaluation are written together with the lecture row, so a crash leaves the
previous evaluation intact or the new one complete - never half of each.

A part row that is LOCATED is never deleted or re-pointed here. Non-located
rows are diagnostics and are replaced wholesale by the next evaluation.
"""
from __future__ import annotations

import uuid

from psycopg.types.json import Jsonb

from app.common.errors import DATABASE_ERROR, PlatformError

RECORDING_SOURCE_NAMESPACE = uuid.UUID("2c1f6a8e-7d3b-5e1a-9b64-0f3a7c2d4e51")

STATUS_FOR_KIND = {"LOCATED": "LOCATED", "WAITING": "WAITING", "RETRY": "RETRY_SCHEDULED",
                   "REVIEW": "REVIEW_REQUIRED", "PERMANENT": "PERMANENT_FAILURE",
                   "NOT_APPLICABLE": "NOT_APPLICABLE"}

LOAD_DISCOVERY = """
SELECT status, reason_code, expected_part_count, located_part_count, located_at, last_verified_at,
       attempt_count, last_checked_at, review_reason
  FROM public.bunny_recording_discoveries WHERE lecture_id = %s
"""
LOAD_SOURCES = """
SELECT graph_recording_id, recording_part_index, locate_status, source_drive_id, source_item_id,
       source_filename, source_size_bytes, source_duration_seconds, call_id, content_correlation_id,
       recording_created_at, recording_end_at, located_at, last_verified_at, recording_source_id
  FROM public.bunny_recording_sources WHERE lecture_id = %s ORDER BY recording_part_index
"""
SOURCE_FIELDS = ("graph_recording_id", "part_index", "locate_status", "source_drive_id", "source_item_id",
                 "source_filename", "source_size_bytes", "source_duration_seconds", "call_id",
                 "content_correlation_id", "recording_created_at", "recording_end_at", "located_at",
                 "last_verified_at", "recording_source_id")

UPSERT_DISCOVERY = """
INSERT INTO public.bunny_recording_discoveries (
    lecture_id, discovery_version, status, reason_code, review_reason, expected_part_count,
    located_part_count, missing_part_count, call_ids, transcript_pairing, attempt_count,
    first_checked_at, last_checked_at, next_check_after, located_at, last_verified_at,
    last_error_code, last_http_status, metadata, updated_at)
VALUES (%(lecture_id)s, %(version)s, %(status)s, %(reason)s, %(review_reason)s, %(expected)s,
        %(located)s, %(missing)s, %(call_ids)s, %(pairing)s, 1, %(now)s, %(now)s, %(next_check)s,
        CASE WHEN %(status)s = 'LOCATED' THEN %(now)s::timestamptz END,
        CASE WHEN %(verified)s THEN %(now)s::timestamptz END,
        %(error_code)s, %(http_status)s, %(metadata)s, %(now)s)
ON CONFLICT (lecture_id) DO UPDATE SET
    discovery_version  = EXCLUDED.discovery_version,
    status             = EXCLUDED.status,
    reason_code        = EXCLUDED.reason_code,
    review_reason      = EXCLUDED.review_reason,
    expected_part_count = COALESCE(EXCLUDED.expected_part_count,
                                   public.bunny_recording_discoveries.expected_part_count),
    located_part_count = EXCLUDED.located_part_count,
    missing_part_count = COALESCE(EXCLUDED.missing_part_count,
                                  public.bunny_recording_discoveries.missing_part_count),
    call_ids           = CASE WHEN cardinality(EXCLUDED.call_ids) > 0 THEN EXCLUDED.call_ids
                              ELSE public.bunny_recording_discoveries.call_ids END,
    transcript_pairing = CASE WHEN EXCLUDED.transcript_pairing = 'NOT_CHECKED'
                              THEN public.bunny_recording_discoveries.transcript_pairing
                              ELSE EXCLUDED.transcript_pairing END,
    attempt_count      = public.bunny_recording_discoveries.attempt_count + 1,
    last_checked_at    = EXCLUDED.last_checked_at,
    next_check_after   = EXCLUDED.next_check_after,
    located_at         = CASE WHEN EXCLUDED.status = 'LOCATED'
                              THEN COALESCE(public.bunny_recording_discoveries.located_at, EXCLUDED.located_at)
                              END,
    last_verified_at   = COALESCE(EXCLUDED.last_verified_at, public.bunny_recording_discoveries.last_verified_at),
    last_error_code    = EXCLUDED.last_error_code,
    last_http_status   = EXCLUDED.last_http_status,
    metadata           = EXCLUDED.metadata,
    updated_at         = EXCLUDED.updated_at
RETURNING attempt_count
"""

DELETE_UNLOCATED = """
DELETE FROM public.bunny_recording_sources WHERE lecture_id = %s AND locate_status <> 'LOCATED'
"""
UPSERT_SOURCE = """
INSERT INTO public.bunny_recording_sources (
    recording_source_id, lecture_id, recording_part_index, graph_recording_id, call_id,
    content_correlation_id, recording_created_at, recording_end_at, source_drive_id, source_item_id,
    source_filename, source_size_bytes, source_duration_seconds, source_modified_at, discovery_sources,
    verification_method, file_name_lead_seconds, duration_difference_seconds, media_origin, classification,
    locate_status, transcript_artifact_id, transcript_part_index, transcript_pairing, located_at,
    last_checked_at, last_verified_at, updated_at)
VALUES (%(recording_source_id)s, %(lecture_id)s, %(part_index)s, %(graph_recording_id)s, %(call_id)s,
        %(content_correlation_id)s, %(created_at)s, %(end_at)s, %(drive_id)s, %(item_id)s, %(filename)s,
        %(size_bytes)s, %(duration)s, %(modified_at)s, %(sources)s, %(method)s, %(lead)s, %(difference)s,
        %(origin)s, %(classification)s, %(status)s, %(transcript_artifact_id)s, %(transcript_part_index)s,
        %(transcript_pairing)s, CASE WHEN %(status)s = 'LOCATED' THEN %(now)s::timestamptz END, %(now)s,
        CASE WHEN %(status)s = 'LOCATED' THEN %(now)s::timestamptz END, %(now)s)
ON CONFLICT (recording_source_id) DO UPDATE SET
    recording_part_index = EXCLUDED.recording_part_index,
    call_id = EXCLUDED.call_id,
    content_correlation_id = EXCLUDED.content_correlation_id,
    recording_created_at = EXCLUDED.recording_created_at,
    recording_end_at = EXCLUDED.recording_end_at,
    source_drive_id = EXCLUDED.source_drive_id,
    source_item_id = EXCLUDED.source_item_id,
    source_filename = EXCLUDED.source_filename,
    source_size_bytes = EXCLUDED.source_size_bytes,
    source_duration_seconds = EXCLUDED.source_duration_seconds,
    source_modified_at = EXCLUDED.source_modified_at,
    discovery_sources = EXCLUDED.discovery_sources,
    verification_method = EXCLUDED.verification_method,
    file_name_lead_seconds = EXCLUDED.file_name_lead_seconds,
    duration_difference_seconds = EXCLUDED.duration_difference_seconds,
    media_origin = EXCLUDED.media_origin,
    classification = EXCLUDED.classification,
    locate_status = EXCLUDED.locate_status,
    transcript_artifact_id = COALESCE(EXCLUDED.transcript_artifact_id,
                                      public.bunny_recording_sources.transcript_artifact_id),
    transcript_part_index = COALESCE(EXCLUDED.transcript_part_index,
                                     public.bunny_recording_sources.transcript_part_index),
    transcript_pairing = COALESCE(EXCLUDED.transcript_pairing, public.bunny_recording_sources.transcript_pairing),
    located_at = CASE WHEN EXCLUDED.locate_status = 'LOCATED'
                      THEN COALESCE(public.bunny_recording_sources.located_at, EXCLUDED.located_at) END,
    last_checked_at = EXCLUDED.last_checked_at,
    last_verified_at = COALESCE(EXCLUDED.last_verified_at, public.bunny_recording_sources.last_verified_at),
    updated_at = EXCLUDED.updated_at
"""
TOUCH_SOURCE = """
UPDATE public.bunny_recording_sources SET last_checked_at = %s
 WHERE lecture_id = %s AND graph_recording_id = %s
"""
VERIFY_SOURCE = """
UPDATE public.bunny_recording_sources
   SET locate_status = %(status)s, last_checked_at = %(now)s,
       last_verified_at = CASE WHEN %(status)s = 'LOCATED' THEN %(now)s::timestamptz ELSE last_verified_at END,
       located_at = CASE WHEN %(status)s = 'LOCATED' THEN located_at END,
       updated_at = %(now)s
 WHERE lecture_id = %(lecture_id)s AND graph_recording_id = %(graph_recording_id)s
"""
LINK_TRANSCRIPT = """
UPDATE public.bunny_recording_sources
   SET transcript_artifact_id = %s, transcript_part_index = %s, transcript_pairing = %s, updated_at = now()
 WHERE lecture_id = %s AND graph_recording_id = %s
"""
# Who else already owns one of these recordings or files?
FOREIGN_OWNERS = """
SELECT count(*) FROM public.bunny_recording_sources
 WHERE lecture_id <> %(lecture_id)s
   AND (graph_recording_id = ANY(%(recordings)s::text[])
        OR (source_drive_id, source_item_id) IN (SELECT * FROM unnest(%(drives)s::text[], %(items)s::text[])))
"""
TRANSCRIPT_PARTS = """
SELECT a.artifact_id, p.part_index, a.provider_call_id, a.content_correlation_id, a.provider_created_at
  FROM public.lecture_transcript_selections s
  JOIN public.lecture_transcript_selection_parts p ON p.selection_id = s.selection_id
  JOIN public.lecture_transcript_artifacts a ON a.artifact_id = p.artifact_id
 WHERE s.lecture_id = %s AND s.selection_status = 'SELECTED' AND s.selection_version = %s
 ORDER BY p.part_index
"""
COVERAGE = """
SELECT lecture_id, session_date, recording_status, reason_code, review_reason, expected_part_count,
       located_part_count, missing_part_count, unresolved_part_rows, last_checked_at
  FROM public.bunny_recording_coverage_v
 WHERE session_date BETWEEN %s AND %s
 ORDER BY session_date, lecture_id
"""
COVERAGE_FIELDS = ("lecture_id", "session_date", "recording_status", "reason_code", "review_reason",
                   "expected_part_count", "located_part_count", "missing_part_count", "unresolved_part_rows",
                   "last_checked_at")


def recording_source_id(lecture_id, graph_recording_id: str) -> uuid.UUID:
    return uuid.uuid5(RECORDING_SOURCE_NAMESPACE, f"{lecture_id}:{graph_recording_id}")


def _db(message, exc):
    return PlatformError(DATABASE_ERROR, message)


class RecordingSourceRepository:
    """No Graph calls; every method runs inside the caller's transaction."""

    def load(self, connection, lecture_id) -> tuple[dict | None, dict]:
        row = connection.execute(LOAD_DISCOVERY, (str(lecture_id),)).fetchone()
        discovery = None if row is None else dict(zip(
            ("status", "reason_code", "expected_part_count", "located_part_count", "located_at",
             "last_verified_at", "attempt_count", "last_checked_at", "review_reason"), row))
        sources = {r[0]: dict(zip(SOURCE_FIELDS, r))
                   for r in connection.execute(LOAD_SOURCES, (str(lecture_id),)).fetchall()}
        return discovery, sources

    def transcript_parts(self, connection, lecture_id, selection_version: str) -> list[tuple]:
        return connection.execute(TRANSCRIPT_PARTS, (str(lecture_id), selection_version)).fetchall()

    def foreign_owners(self, connection, lecture_id, mappings) -> int:
        files = [m.candidate.physical_id for m in mappings if m.candidate is not None]
        return connection.execute(FOREIGN_OWNERS, {
            "lecture_id": str(lecture_id), "recordings": [m.recording.recording_id for m in mappings],
            "drives": [f[0] for f in files], "items": [f[1] for f in files]}).fetchone()[0]

    def record(self, connection, lecture_id, *, version: str, kind: str, reason: str, review_reason,
               expected, located: int, missing, call_ids, pairing: str, next_check, now,
               verified: bool = False, error_code=None, http_status=None, metadata=None) -> int:
        try:
            return connection.execute(UPSERT_DISCOVERY, {
                "lecture_id": str(lecture_id), "version": version, "status": STATUS_FOR_KIND[kind],
                "reason": reason, "review_reason": review_reason, "expected": expected, "located": located,
                "missing": missing, "call_ids": sorted(call_ids or ()), "pairing": pairing,
                "now": now, "next_check": next_check, "verified": verified, "error_code": error_code,
                "http_status": http_status, "metadata": Jsonb(metadata or {})}).fetchone()[0]
        except Exception as exc:
            raise _db("recording discovery write failed", exc) from exc

    def replace_parts(self, connection, lecture_id, mappings, *, method: str, now, kept_reason: str) -> None:
        """Replace the lecture's non-located part rows with this evaluation's; LOCATED rows are upserted."""
        try:
            connection.execute(DELETE_UNLOCATED, (str(lecture_id),))
            for m in mappings:
                if m.reason == kept_reason:
                    connection.execute(TOUCH_SOURCE, (now, str(lecture_id), m.recording.recording_id))
                    continue
                c = m.candidate
                connection.execute(UPSERT_SOURCE, {
                    "recording_source_id": recording_source_id(lecture_id, m.recording.recording_id),
                    "lecture_id": str(lecture_id), "part_index": m.part_index,
                    "graph_recording_id": m.recording.recording_id, "call_id": m.recording.call_id,
                    "content_correlation_id": m.recording.content_correlation_id,
                    "created_at": m.recording.created_at, "end_at": m.recording.end_at,
                    "drive_id": c.drive_id if c else None, "item_id": c.item_id if c else None,
                    "filename": c.name if c else None, "size_bytes": c.size_bytes if c else None,
                    "duration": c.duration_seconds if c else None, "modified_at": c.modified_at if c else None,
                    "sources": list(c.sources or (c.source,)) if c else [], "method": method,
                    "lead": m.lead_seconds, "difference": m.duration_difference_seconds, "origin": m.media_origin,
                    "classification": m.classification, "status": m.locate_status,
                    "transcript_artifact_id": m.transcript_artifact_id,
                    "transcript_part_index": m.transcript_part_index, "transcript_pairing": m.transcript_pairing,
                    "now": now})
        except Exception as exc:
            raise _db("recording source write failed", exc) from exc

    def mark_verified(self, connection, lecture_id, statuses: dict, *, now) -> None:
        """Verification results for stored parts: LOCATED (re-verified), SOURCE_MISSING or SOURCE_CHANGED."""
        try:
            for graph_recording_id, status in statuses.items():
                connection.execute(VERIFY_SOURCE, {"status": status, "now": now, "lecture_id": str(lecture_id),
                                                   "graph_recording_id": graph_recording_id})
        except Exception as exc:
            raise _db("recording source verification write failed", exc) from exc

    def link_transcripts(self, connection, lecture_id, links: dict) -> None:
        """{graph_recording_id: (artifact_id, transcript part index, pairing method)} for stored parts."""
        try:
            for graph_recording_id, (artifact_id, part_index, method) in links.items():
                connection.execute(LINK_TRANSCRIPT, (artifact_id, part_index, method, str(lecture_id),
                                                     graph_recording_id))
        except Exception as exc:
            raise _db("recording transcript link write failed", exc) from exc

    def coverage(self, connection, date_from, date_to) -> list[dict]:
        return [dict(zip(COVERAGE_FIELDS, r)) for r in connection.execute(COVERAGE, (date_from, date_to)).fetchall()]
