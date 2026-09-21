"""
Phase 2C1 persistence: canonical transcript documents and cues.

Reads the Phase 2B combined transcript layer; writes only the Phase 2C tables.
Cues are written with a single batched statement per document, never one
transaction or one round trip per cue.
"""
import json
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError


DOCUMENT_NAMESPACE = uuid.UUID("3a6c1f48-0d95-4b27-9e6a-8f21c47b5d30")
CUE_NAMESPACE = uuid.UUID("5b82e7d0-1c64-4a39-8b07-9d3f6a2e4c15")

# One row per combined transcript for the date. combined_content is selected
# because it is the exact evidence being parsed; nothing else is loaded.
LOAD_COMBINED = """
SELECT ct.combined_id, ct.selection_id, ct.lecture_id, ct.content_sha256,
       ct.source_fingerprint, ct.duration_seconds, ct.combined_content,
       l.subject, s.selection_version, s.primary_provider_transcript_id
  FROM public.lecture_combined_transcripts ct
  JOIN public.lecture_sessions l ON l.lecture_id = ct.lecture_id
  JOIN public.lecture_transcript_selections s ON s.selection_id = ct.selection_id
 WHERE l.session_date = %s
 ORDER BY l.scheduled_start, l.subject
"""


def document_identity(*, selection_id, source_content_sha256: str, parser_version: str) -> uuid.UUID:
    """
    Provenance identity: the evidence plus the code that produced the document.

    Changing the selection, the combined bytes, or the parser version yields a
    different document, so historical derived evidence is never silently
    mutated in place.
    """
    return uuid.uuid5(
        DOCUMENT_NAMESPACE,
        f"selection:{selection_id}\0content:{source_content_sha256}\0parser:{parser_version}",
    )


class TranscriptDocumentRepository:
    def load_combined_for_date(self, connection, target_date) -> list[dict]:
        try:
            rows = connection.execute(LOAD_COMBINED, (target_date,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "combined transcript query failed") from exc
        return [
            {"combined_id": row[0], "selection_id": row[1], "lecture_id": row[2],
             "content_sha256": row[3], "source_fingerprint": row[4],
             "duration_seconds": row[5], "combined_content": row[6],
             "subject": row[7], "selection_version": row[8],
             "primary_provider_transcript_id": row[9]}
            for row in rows
        ]

    def find_document(self, connection, document_id) -> dict | None:
        try:
            row = connection.execute(
                "SELECT document_id, parse_status, cue_count, "
                "       (SELECT count(*) FROM public.lecture_transcript_cues c "
                "         WHERE c.document_id = d.document_id) AS stored_cues "
                "  FROM public.lecture_transcript_documents d WHERE document_id = %s",
                (document_id,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript document lookup failed") from exc
        if not row:
            return None
        return {"document_id": row[0], "parse_status": row[1],
                "cue_count": row[2], "stored_cues": row[3]}

    def upsert_document(self, connection, document_id, *, lecture_id, selection_id,
                        combined_id, parser_version, source_fingerprint,
                        source_content_sha256, parse_status, metrics,
                        combined_duration_seconds, duration_difference_ms,
                        metadata) -> str:
        try:
            row = connection.execute("""
            INSERT INTO public.lecture_transcript_documents (
                document_id, lecture_id, selection_id, combined_id, parser_version,
                source_fingerprint, source_content_sha256, parse_status, cue_count,
                first_cue_start_ms, last_cue_end_ms, duration_ms,
                unique_raw_speaker_label_count, empty_text_cue_count,
                overlapping_cue_count, malformed_block_count, multi_speaker_cue_count,
                warning_count, combined_duration_seconds, duration_difference_ms, metadata
            ) VALUES (
                %(document_id)s, %(lecture_id)s, %(selection_id)s, %(combined_id)s,
                %(parser_version)s, %(source_fingerprint)s, %(source_content_sha256)s,
                %(parse_status)s, %(cue_count)s, %(first_cue_start_ms)s,
                %(last_cue_end_ms)s, %(duration_ms)s, %(unique_raw_speaker_label_count)s,
                %(empty_text_cue_count)s, %(overlapping_cue_count)s,
                %(malformed_block_count)s, %(multi_speaker_cue_count)s, %(warning_count)s,
                %(combined_duration_seconds)s, %(duration_difference_ms)s, %(metadata)s::jsonb
            )
            ON CONFLICT (document_id) DO UPDATE SET
                parse_status = EXCLUDED.parse_status,
                cue_count = EXCLUDED.cue_count,
                first_cue_start_ms = EXCLUDED.first_cue_start_ms,
                last_cue_end_ms = EXCLUDED.last_cue_end_ms,
                duration_ms = EXCLUDED.duration_ms,
                unique_raw_speaker_label_count = EXCLUDED.unique_raw_speaker_label_count,
                empty_text_cue_count = EXCLUDED.empty_text_cue_count,
                overlapping_cue_count = EXCLUDED.overlapping_cue_count,
                malformed_block_count = EXCLUDED.malformed_block_count,
                multi_speaker_cue_count = EXCLUDED.multi_speaker_cue_count,
                warning_count = EXCLUDED.warning_count,
                combined_duration_seconds = EXCLUDED.combined_duration_seconds,
                duration_difference_ms = EXCLUDED.duration_difference_ms,
                metadata = EXCLUDED.metadata, updated_at = now()
            RETURNING (xmax = 0) AS inserted
            """, {
                "document_id": document_id, "lecture_id": lecture_id,
                "selection_id": selection_id, "combined_id": combined_id,
                "parser_version": parser_version,
                "source_fingerprint": source_fingerprint,
                "source_content_sha256": source_content_sha256,
                "parse_status": parse_status,
                "cue_count": metrics.get("cue_count", 0),
                "first_cue_start_ms": metrics.get("first_cue_start_ms"),
                "last_cue_end_ms": metrics.get("last_cue_end_ms"),
                "duration_ms": metrics.get("duration_ms"),
                "unique_raw_speaker_label_count": metrics.get("unique_raw_speaker_label_count", 0),
                "empty_text_cue_count": metrics.get("empty_text_cue_count", 0),
                "overlapping_cue_count": metrics.get("overlapping_cue_count", 0),
                "malformed_block_count": metrics.get("malformed_block_count", 0),
                "multi_speaker_cue_count": metrics.get("multi_speaker_cue_count", 0),
                "warning_count": metrics.get("warning_count", 0),
                "combined_duration_seconds": combined_duration_seconds,
                "duration_difference_ms": duration_difference_ms,
                "metadata": json.dumps(metadata, default=str),
            }).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript document upsert failed") from exc
        return "created" if row[0] else "updated"

    def replace_cues(self, connection, document_id, cues) -> int:
        """
        Write one document's cues in a single batched statement.

        Cues are replaced wholesale so a reparse can never leave a stale tail
        behind, and cue_id is deterministic, so the same document and index
        always resolve to the same row.
        """
        if not cues:
            try:
                connection.execute(
                    "DELETE FROM public.lecture_transcript_cues WHERE document_id = %s",
                    (document_id,))
            except Exception as exc:
                raise PlatformError(DATABASE_ERROR, "transcript cue delete failed") from exc
            return 0
        rows = [
            (uuid.uuid5(CUE_NAMESPACE, f"{document_id}\0{cue.cue_index}"), document_id,
             cue.cue_index, cue.start_ms, cue.end_ms, cue.speaker_label_raw,
             cue.text, cue.cue_text_sha256, json.dumps(cue.metadata, default=str))
            for cue in cues
        ]
        try:
            connection.execute(
                "DELETE FROM public.lecture_transcript_cues WHERE document_id = %s",
                (document_id,))
            with connection.cursor() as cursor:
                cursor.executemany("""
                INSERT INTO public.lecture_transcript_cues (
                    cue_id, document_id, cue_index, start_ms, end_ms,
                    speaker_label_raw, text, cue_text_sha256, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """, rows)
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript cue batch insert failed") from exc
        return len(rows)


class TranscriptParseRunRepository:
    COUNTERS = (
        "combined_transcripts_considered", "documents_created", "documents_reused",
        "cues_written", "cues_reused", "parsed_count", "parsed_with_warnings_count",
        "failed_count", "duration_parity_mismatches",
    )

    def start(self, connection, target_date, parser_version, mode="SHADOW") -> uuid.UUID:
        run_id = uuid.uuid4()
        try:
            connection.execute(
                "INSERT INTO public.lecture_transcript_parse_runs "
                "(run_id, target_date, mode, status, parser_version) "
                "VALUES (%s, %s, %s, 'RUNNING', %s)",
                (run_id, target_date, mode, parser_version))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not create parse run") from exc
        return run_id

    def complete(self, connection, run_id, summary) -> None:
        assignments = ", ".join(f"{name} = %s" for name in self.COUNTERS)
        values = [int(summary.get(name, 0)) for name in self.COUNTERS]
        try:
            connection.execute(
                "UPDATE public.lecture_transcript_parse_runs "
                f"SET completed_at = now(), status = %s, {assignments}, metadata = %s::jsonb "
                "WHERE run_id = %s",
                [summary["status"], *values,
                 json.dumps(summary.get("metadata", {}), default=str), run_id])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not complete parse run") from exc
