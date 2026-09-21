"""
Phase 2C1 orchestration: combined transcript -> canonical document and cues.

Entirely DB-backed. No Microsoft Graph, no calendar discovery, no Aptem query,
no reselection, and no reconstruction of the combined transcript from raw
artifacts. Phase 2B already produced the authoritative combined text; this
service only parses it.
"""
import logging
import time
import uuid
from datetime import date

from app.db.repositories.transcript_documents import document_identity
from app.transcripts.webvtt import (
    PARSED,
    PARSED_WITH_WARNINGS,
    PARSER_VERSION,
    parse_combined_webvtt,
)


# A parsed cue range can legitimately differ from the Phase 2B combined
# duration: Phase 2B rounds to whole minutes, and its range scan is the same
# arithmetic on the same text. Anything beyond a minute of rounding is treated
# as a real mismatch to investigate, never patched away.
DURATION_TOLERANCE_MS = 60_000


class CanonicalTranscriptService:
    def __init__(self, *, document_repository, run_repository,
                 parser_version: str = PARSER_VERSION):
        self.document_repository = document_repository
        self.run_repository = run_repository
        self.parser_version = parser_version
        self.log = logging.getLogger(__name__)

    def parse_day(self, connection, target_date: date, *, persist: bool = True) -> dict:
        started = time.monotonic()
        run_id = (self.run_repository.start(connection, target_date, self.parser_version)
                  if persist else uuid.uuid4())
        sources = self.document_repository.load_combined_for_date(connection, target_date)

        counters = {
            "combined_transcripts_considered": len(sources),
            "documents_created": 0, "documents_reused": 0,
            "cues_written": 0, "cues_reused": 0,
            "parsed_count": 0, "parsed_with_warnings_count": 0,
            "failed_count": 0, "duration_parity_mismatches": 0,
        }
        results = []

        for source in sources:
            parsed = parse_combined_webvtt(source["combined_content"])
            metrics = parsed.metrics
            document_id = document_identity(
                selection_id=source["selection_id"],
                source_content_sha256=source["content_sha256"],
                parser_version=self.parser_version,
            )

            combined_seconds = (float(source["duration_seconds"])
                                if source["duration_seconds"] is not None else None)
            parsed_ms = metrics.get("duration_ms")
            difference_ms = None
            if combined_seconds is not None and parsed_ms is not None:
                difference_ms = parsed_ms - int(round(combined_seconds * 1000))
            parity_ok = difference_ms is None or abs(difference_ms) <= DURATION_TOLERANCE_MS
            if not parity_ok:
                counters["duration_parity_mismatches"] += 1

            if parsed.status == PARSED:
                counters["parsed_count"] += 1
            elif parsed.status == PARSED_WITH_WARNINGS:
                counters["parsed_with_warnings_count"] += 1
            else:
                counters["failed_count"] += 1

            result = {
                "lecture_id": str(source["lecture_id"]),
                "subject": source["subject"],
                "selection_id": str(source["selection_id"]),
                "document_id": str(document_id),
                "parser_version": self.parser_version,
                "source_content_sha256_prefix": source["content_sha256"][:12],
                "source_fingerprint_prefix": source["source_fingerprint"][:12],
                "parse_status": parsed.status,
                "cue_count": metrics.get("cue_count", 0),
                "first_cue_start_ms": metrics.get("first_cue_start_ms"),
                "last_cue_end_ms": metrics.get("last_cue_end_ms"),
                "parsed_duration_ms": parsed_ms,
                "combined_duration_seconds": combined_seconds,
                "duration_difference_ms": difference_ms,
                "duration_parity_ok": parity_ok,
                "unique_raw_speaker_label_count": metrics.get("unique_raw_speaker_label_count", 0),
                "overlapping_cue_count": metrics.get("overlapping_cue_count", 0),
                "empty_text_cue_count": metrics.get("empty_text_cue_count", 0),
                "malformed_block_count": metrics.get("malformed_block_count", 0),
                "multi_speaker_cue_count": metrics.get("multi_speaker_cue_count", 0),
                "zero_length_cue_count": metrics.get("zero_length_cue_count", 0),
                "warning_count": metrics.get("warning_count", 0),
                # Codes and counts only: never cue text.
                "warning_codes": sorted({item["code"] for item in parsed.warnings}),
            }

            if persist:
                existing = self.document_repository.find_document(connection, document_id)
                reusable = (
                    existing is not None
                    and existing["parse_status"] == parsed.status
                    and existing["cue_count"] == metrics.get("cue_count", 0)
                    and existing["stored_cues"] == metrics.get("cue_count", 0)
                )
                outcome = self.document_repository.upsert_document(
                    connection, document_id,
                    lecture_id=source["lecture_id"], selection_id=source["selection_id"],
                    combined_id=source["combined_id"], parser_version=self.parser_version,
                    source_fingerprint=source["source_fingerprint"],
                    source_content_sha256=source["content_sha256"],
                    parse_status=parsed.status, metrics=metrics,
                    combined_duration_seconds=combined_seconds,
                    duration_difference_ms=difference_ms,
                    metadata={
                        "selection_version": source["selection_version"],
                        "primary_provider_transcript_id":
                            source["primary_provider_transcript_id"],
                        "duration_parity_ok": parity_ok,
                        "warnings": parsed.warnings[:50],
                        "zero_length_cue_count": metrics.get("zero_length_cue_count", 0),
                        "non_monotonic_cue_count": metrics.get("non_monotonic_cue_count", 0),
                        "cue_identifier_count": metrics.get("cue_identifier_count", 0),
                        "other_markup_tag_count": metrics.get("other_markup_tag_count", 0),
                    },
                )
                counters["documents_created" if outcome == "created" else "documents_reused"] += 1
                if reusable:
                    # Identical provenance already fully materialised: leave the
                    # stored cues exactly as they are.
                    counters["cues_reused"] += existing["stored_cues"]
                    result["cue_persistence"] = "reused"
                else:
                    written = self.document_repository.replace_cues(
                        connection, document_id, parsed.cues)
                    counters["cues_written"] += written
                    result["cue_persistence"] = "written"
                result["document_persistence"] = outcome
            results.append(result)

        summary = {
            "run_id": str(run_id), "target_date": target_date.isoformat(),
            "mode": "SHADOW" if persist else "DRY_RUN",
            "status": "COMPLETED", "parser_version": self.parser_version,
            **counters,
            "graph_calls": 0,
            "documents": results,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "metadata": {
                "parser_version": self.parser_version,
                "source": "PHASE_2B_COMBINED_TRANSCRIPTS_ONLY",
                "graph_calls": 0,
                "duration_tolerance_ms": DURATION_TOLERANCE_MS,
                "counters": counters,
            },
        }
        if persist:
            self.run_repository.complete(connection, run_id, summary)
        # Structured, non-sensitive fields only. No cue text, no speaker names.
        self.log.info("canonical transcript parse completed", extra={"fields": {
            "service": "canonical_transcript", "operation": "parse_day",
            "run_id": str(run_id), "status": summary["status"],
            "documents": len(results),
            "cues_written": counters["cues_written"],
            "duration_ms": summary["duration_ms"],
        }})
        return summary
