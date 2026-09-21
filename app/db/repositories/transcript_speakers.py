"""
Phase 2C2 persistence: canonical speaker inventory.

Reads only lecture_transcript_documents and lecture_transcript_cues; writes
only lecture_transcript_speakers and lecture_transcript_speaker_runs. Canonical
cues are never modified.

Aggregation runs as a single grouped SQL statement per date, so cue rows are
never loaded into application memory, and speaker rows are written with one
batched statement per document.
"""
import json
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError


LOAD_DOCUMENTS = """
SELECT d.document_id, d.lecture_id, l.subject, d.cue_count, d.duration_ms,
       d.parser_version, d.source_content_sha256, d.parse_status
  FROM public.lecture_transcript_documents d
  JOIN public.lecture_sessions l ON l.lecture_id = d.lecture_id
 WHERE l.session_date = %s AND d.parser_version = %s
 ORDER BY l.scheduled_start, l.subject
"""

# One grouped pass over the cues of every document for the date. Cue rows stay
# in the database; only the per-speaker aggregate crosses the wire.
AGGREGATE_SPEAKERS = """
SELECT document_id,
       speaker_label_raw,
       count(*)                    AS cue_count,
       min(cue_index)              AS first_cue_index,
       max(cue_index)              AS last_cue_index,
       min(start_ms)               AS first_spoken_start_ms,
       max(end_ms)                 AS last_spoken_end_ms,
       sum(end_ms - start_ms)      AS gross_spoken_ms
  FROM public.lecture_transcript_cues
 WHERE document_id = ANY(%s) AND speaker_label_raw IS NOT NULL
 GROUP BY document_id, speaker_label_raw
 ORDER BY document_id, speaker_label_raw
"""

# Cues carrying no usable speaker label (for example conflicting voice tags)
# are counted, never given a fabricated speaker row.
COUNT_UNASSIGNED = """
SELECT document_id, count(*)
  FROM public.lecture_transcript_cues
 WHERE document_id = ANY(%s) AND speaker_label_raw IS NULL
 GROUP BY document_id
"""


class TranscriptSpeakerRepository:
    def load_documents(self, connection, target_date, parser_version) -> list[dict]:
        try:
            rows = connection.execute(
                LOAD_DOCUMENTS, (target_date, parser_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "canonical document query failed") from exc
        return [
            {"document_id": row[0], "lecture_id": row[1], "subject": row[2],
             "cue_count": row[3], "duration_ms": row[4], "parser_version": row[5],
             "source_content_sha256": row[6], "parse_status": row[7]}
            for row in rows
        ]

    def aggregate_speakers(self, connection, document_ids) -> dict:
        """Per-document, per-raw-label cue aggregates, computed by PostgreSQL."""
        if not document_ids:
            return {}
        try:
            rows = connection.execute(AGGREGATE_SPEAKERS, (list(document_ids),)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "speaker aggregation query failed") from exc
        grouped: dict = {}
        for row in rows:
            grouped.setdefault(row[0], []).append({
                "speaker_label_raw": row[1], "cue_count": row[2],
                "first_cue_index": row[3], "last_cue_index": row[4],
                "first_spoken_start_ms": row[5], "last_spoken_end_ms": row[6],
                "gross_spoken_ms": int(row[7]),
            })
        return grouped

    def count_unassigned_cues(self, connection, document_ids) -> dict:
        if not document_ids:
            return {}
        try:
            rows = connection.execute(COUNT_UNASSIGNED, (list(document_ids),)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "unassigned cue query failed") from exc
        return {row[0]: row[1] for row in rows}

    def existing_speaker_ids(self, connection, document_id, inventory_version) -> set:
        try:
            rows = connection.execute(
                "SELECT speaker_id FROM public.lecture_transcript_speakers "
                " WHERE document_id = %s AND speaker_inventory_version = %s",
                (document_id, inventory_version)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "speaker lookup failed") from exc
        return {row[0] for row in rows}

    def upsert_speakers(self, connection, document_id, inventory_version, speakers) -> dict:
        """
        Write one document's inventory in a single batched statement.

        Stale rows for this document and version are removed first, so a
        reparse that changes the label set can never leave an orphan speaker.
        """
        before = self.existing_speaker_ids(connection, document_id, inventory_version)
        incoming = {row["speaker_id"] for row in speakers}
        try:
            obsolete = before - incoming
            if obsolete:
                connection.execute(
                    "DELETE FROM public.lecture_transcript_speakers "
                    " WHERE document_id = %s AND speaker_inventory_version = %s "
                    "   AND speaker_id = ANY(%s)",
                    (document_id, inventory_version, list(obsolete)))
            if speakers:
                with connection.cursor() as cursor:
                    cursor.executemany("""
                    INSERT INTO public.lecture_transcript_speakers (
                        speaker_id, document_id, speaker_inventory_version,
                        speaker_label_raw, speaker_label_normalized, cue_count,
                        first_cue_index, last_cue_index, first_spoken_start_ms,
                        last_spoken_end_ms, gross_spoken_ms, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (speaker_id) DO UPDATE SET
                        speaker_label_normalized = EXCLUDED.speaker_label_normalized,
                        cue_count = EXCLUDED.cue_count,
                        first_cue_index = EXCLUDED.first_cue_index,
                        last_cue_index = EXCLUDED.last_cue_index,
                        first_spoken_start_ms = EXCLUDED.first_spoken_start_ms,
                        last_spoken_end_ms = EXCLUDED.last_spoken_end_ms,
                        gross_spoken_ms = EXCLUDED.gross_spoken_ms,
                        metadata = EXCLUDED.metadata, updated_at = now()
                    """, [
                        (row["speaker_id"], document_id, inventory_version,
                         row["speaker_label_raw"], row["speaker_label_normalized"],
                         row["cue_count"], row["first_cue_index"], row["last_cue_index"],
                         row["first_spoken_start_ms"], row["last_spoken_end_ms"],
                         row["gross_spoken_ms"], json.dumps(row.get("metadata", {}),
                                                            default=str))
                        for row in speakers
                    ])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "speaker inventory upsert failed") from exc
        created = len(incoming - before)
        return {"created": created, "updated": len(incoming) - created,
                "removed": len(before - incoming)}


class SpeakerInventoryRunRepository:
    COUNTERS = (
        "documents_considered", "speakers_created", "speakers_updated",
        "cues_aggregated", "unassigned_cue_count", "normalization_collision_count",
        "documents_with_overlap_excess", "error_count",
    )

    def start(self, connection, target_date, inventory_version, mode="SHADOW") -> uuid.UUID:
        run_id = uuid.uuid4()
        try:
            connection.execute(
                "INSERT INTO public.lecture_transcript_speaker_runs "
                "(run_id, target_date, mode, status, speaker_inventory_version) "
                "VALUES (%s, %s, %s, 'RUNNING', %s)",
                (run_id, target_date, mode, inventory_version))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not create speaker run") from exc
        return run_id

    def complete(self, connection, run_id, summary) -> None:
        assignments = ", ".join(f"{name} = %s" for name in self.COUNTERS)
        values = [int(summary.get(name, 0)) for name in self.COUNTERS]
        try:
            connection.execute(
                "UPDATE public.lecture_transcript_speaker_runs "
                f"SET completed_at = now(), status = %s, {assignments}, metadata = %s::jsonb "
                "WHERE run_id = %s",
                [summary["status"], *values,
                 json.dumps(summary.get("metadata", {}), default=str), run_id])
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not complete speaker run") from exc


class LegacyTrainerDiagnosticRepository:
    """
    READ-ONLY diagnostic. Reads qa_doctors_sessions.trainer to report whether
    the legacy trainer string appears among canonical speaker labels.

    It assigns no role, changes no inventory, and writes nothing.
    """

    def load_trainers(self, connection, target_date) -> list[dict]:
        try:
            rows = connection.execute(
                "SELECT subject, trainer FROM public.qa_doctors_sessions "
                " WHERE date = %s ORDER BY subject", (target_date,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "legacy trainer query failed") from exc
        return [{"subject": row[0], "trainer": row[1]} for row in rows]
