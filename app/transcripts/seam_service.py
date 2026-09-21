"""
Phase 3C2.3B: build one lecture's canonical v2 (seam-deduplicated) document.

Scoped to a single lecture on purpose. v2 is not a global replacement for the
canonical parser - it is an explicitly-bound alternative for the multi-part
lectures whose parts overlap. Every v1 document, cue and fingerprint is left
exactly as it is, and the Phase 2B selection this reads from is never written.
"""
import logging
import time
import uuid

from app.db.repositories.transcript_documents import document_identity
from app.transcripts.seam import (
    DEDUP_POLICY_VERSION,
    SEAM_PARSER_VERSION,
    TranscriptPart,
    parse_parts_with_seam_dedup,
    seam_fingerprint,
)


class SeamInputError(RuntimeError):
    """The lecture cannot be reparsed under v2 from persisted evidence."""


LOAD_PARTS_SQL = """
SELECT sel.selection_id, comb.combined_id, sel.selection_version,
       sel.primary_provider_transcript_id, sel.selected_part_count,
       comb.content_sha256, comb.duration_seconds, comb.source_fingerprint,
       sp.part_index, sp.part_offset_ms, sp.is_primary,
       sp.provider_transcript_id, a.artifact_id, ac.raw_content
  FROM public.lecture_transcript_selections sel
  JOIN public.lecture_combined_transcripts comb ON comb.selection_id = sel.selection_id
  JOIN public.lecture_transcript_selection_parts sp ON sp.selection_id = sel.selection_id
  JOIN public.lecture_transcript_artifacts a ON a.artifact_id = sp.artifact_id
  JOIN public.lecture_transcript_artifact_contents ac ON ac.artifact_id = a.artifact_id
 WHERE sel.lecture_id = %s
 ORDER BY sp.part_index
"""


class SeamDedupService:
    def __init__(self, *, document_repository,
                 parser_version: str = SEAM_PARSER_VERSION):
        self.document_repository = document_repository
        self.parser_version = parser_version
        self.log = logging.getLogger(__name__)

    def rebuild_lecture(self, connection, lecture_id, *, persist: bool = True) -> dict:
        """Reparse ONE lecture from its selected parts and store it under v2."""
        started = time.monotonic()
        rows = connection.execute(LOAD_PARTS_SQL, (lecture_id,)).fetchall()
        if not rows:
            raise SeamInputError(f"no Phase 2B selected parts for lecture {lecture_id}")

        columns = [
            "selection_id", "combined_id", "selection_version",
            "primary_provider_transcript_id", "selected_part_count",
            "content_sha256", "duration_seconds", "source_fingerprint",
            "part_index", "part_offset_ms", "is_primary",
            "provider_transcript_id", "artifact_id", "raw_content",
        ]
        records = [dict(zip(columns, row)) for row in rows]
        head = records[0]
        if len(records) != head["selected_part_count"]:
            # Reparsing a partial selection would silently drop a part.
            raise SeamInputError(
                f"selection declares {head['selected_part_count']} parts, "
                f"found {len(records)} with content")

        parts = [TranscriptPart(
            part_index=record["part_index"],
            offset_ms=record["part_offset_ms"],
            content=record["raw_content"],
            artifact_id=str(record["artifact_id"]),
            provider_transcript_id=record["provider_transcript_id"],
        ) for record in records]

        document = parse_parts_with_seam_dedup(parts)
        metrics = document.metrics
        fingerprint = seam_fingerprint(parts)
        document_id = document_identity(
            selection_id=head["selection_id"],
            source_content_sha256=head["content_sha256"],
            parser_version=self.parser_version,
        )

        combined_seconds = (float(head["duration_seconds"])
                            if head["duration_seconds"] is not None else None)
        parsed_ms = metrics.get("duration_ms")
        difference_ms = (parsed_ms - int(round(combined_seconds * 1000))
                         if combined_seconds is not None and parsed_ms is not None else None)

        result = {
            "lecture_id": str(lecture_id),
            "selection_id": str(head["selection_id"]),
            "document_id": str(document_id),
            "parser_version": self.parser_version,
            "dedup_policy_version": DEDUP_POLICY_VERSION,
            "seam_fingerprint": fingerprint,
            "parse_status": document.status,
            "part_count": len(parts),
            "part_offsets_ms": [part.offset_ms for part in parts],
            "cue_count": metrics.get("cue_count", 0),
            "first_cue_start_ms": metrics.get("first_cue_start_ms"),
            "last_cue_end_ms": metrics.get("last_cue_end_ms"),
            "duration_ms": parsed_ms,
            "combined_duration_seconds": combined_seconds,
            # v1's combined duration still includes the duplicated seam, so a
            # difference here is the expected consequence of deduplicating.
            "duration_difference_ms": difference_ms,
            "seam_dropped_cue_count": document.dropped_cue_count,
            "seams": [seam.as_dict() for seam in document.seams],
            "warning_codes": sorted({item["code"] for item in document.warnings}),
            # Boundary markers for this phase.
            "graph_calls": 0, "provider_calls": 0, "legacy_qa_writes": 0,
            "raw_artifacts_written": 0, "selections_written": 0,
        }

        if persist:
            outcome = self.document_repository.upsert_document(
                connection, document_id,
                lecture_id=lecture_id, selection_id=head["selection_id"],
                combined_id=head["combined_id"], parser_version=self.parser_version,
                source_fingerprint=head["source_fingerprint"],
                source_content_sha256=head["content_sha256"],
                parse_status=document.status, metrics=metrics,
                combined_duration_seconds=combined_seconds,
                duration_difference_ms=difference_ms,
                metadata={
                    "selection_version": head["selection_version"],
                    "primary_provider_transcript_id": head["primary_provider_transcript_id"],
                    "dedup_policy_version": DEDUP_POLICY_VERSION,
                    "seam_fingerprint": fingerprint,
                    "part_count": len(parts),
                    "seam_dropped_cue_count": document.dropped_cue_count,
                    "seams": [seam.as_dict() for seam in document.seams],
                    "warnings": document.warnings[:50],
                    "non_monotonic_cue_count": metrics.get("non_monotonic_cue_count", 0),
                    "zero_length_cue_count": metrics.get("zero_length_cue_count", 0),
                    "source": "PHASE_2B_SELECTED_PARTS_RAW_ARTIFACTS",
                },
            )
            written = self.document_repository.replace_cues(
                connection, document_id, document.cues)
            result["document_persistence"] = outcome
            result["cues_written"] = written

        result["duration_ms_elapsed"] = round((time.monotonic() - started) * 1000)
        # Counts and ids only: no cue text, no speaker names.
        self.log.info("seam dedup rebuild completed", extra={"fields": {
            "service": "seam_dedup", "operation": "rebuild_lecture",
            "lecture_id": str(lecture_id), "parser_version": self.parser_version,
            "cue_count": result["cue_count"],
            "seam_dropped_cue_count": document.dropped_cue_count,
        }})
        return result
