"""
Phase 2C2 orchestration: canonical cues -> per-document speaker inventory.

Entirely DB-backed. No Microsoft Graph, no WebVTT reparsing, no calendar or
Aptem discovery, no attendance lookup, no person matching, no role assignment,
and no engagement calculation.
"""
import logging
import time
import uuid
from datetime import date

from app.lectures.matching import normalize_group
from app.transcripts.speakers import (
    SPEAKER_INVENTORY_VERSION,
    find_normalization_collisions,
    normalize_speaker_label,
    speaker_identity,
)
from app.transcripts.webvtt import PARSER_VERSION


class SpeakerInventoryService:
    def __init__(self, *, speaker_repository, run_repository,
                 legacy_diagnostic_repository=None,
                 inventory_version: str = SPEAKER_INVENTORY_VERSION,
                 parser_version: str = PARSER_VERSION):
        self.speaker_repository = speaker_repository
        self.run_repository = run_repository
        self.legacy_diagnostic_repository = legacy_diagnostic_repository
        self.inventory_version = inventory_version
        self.parser_version = parser_version
        self.log = logging.getLogger(__name__)

    def build_day(self, connection, target_date: date, *, persist: bool = True) -> dict:
        started = time.monotonic()
        run_id = (self.run_repository.start(connection, target_date, self.inventory_version)
                  if persist else uuid.uuid4())

        documents = self.speaker_repository.load_documents(
            connection, target_date, self.parser_version)
        document_ids = [item["document_id"] for item in documents]
        aggregates = self.speaker_repository.aggregate_speakers(connection, document_ids)
        unassigned = self.speaker_repository.count_unassigned_cues(connection, document_ids)

        counters = {
            "documents_considered": len(documents),
            "speakers_created": 0, "speakers_updated": 0, "cues_aggregated": 0,
            "unassigned_cue_count": 0, "normalization_collision_count": 0,
            "documents_with_overlap_excess": 0, "error_count": 0,
        }
        results = []

        for document in documents:
            rows = aggregates.get(document["document_id"], [])
            raw_labels = [row["speaker_label_raw"] for row in rows]
            collisions = find_normalization_collisions(raw_labels)

            speakers = [
                {
                    "speaker_id": speaker_identity(
                        document_id=document["document_id"],
                        speaker_label_raw=row["speaker_label_raw"],
                        inventory_version=self.inventory_version),
                    "speaker_label_raw": row["speaker_label_raw"],
                    "speaker_label_normalized":
                        normalize_speaker_label(row["speaker_label_raw"]),
                    **{key: row[key] for key in (
                        "cue_count", "first_cue_index", "last_cue_index",
                        "first_spoken_start_ms", "last_spoken_end_ms", "gross_spoken_ms")},
                    "metadata": {"speaker_inventory_version": self.inventory_version},
                }
                for row in rows
            ]
            # Flag, never merge: a collision is a human decision for Phase 2C3.
            if collisions:
                colliding = {item["speaker_label_normalized"] for item in collisions}
                for speaker in speakers:
                    if speaker["speaker_label_normalized"] in colliding:
                        speaker["metadata"]["normalized_label_collision"] = True

            aggregated_cues = sum(row["cue_count"] for row in rows)
            gross_total = sum(row["gross_spoken_ms"] for row in rows)
            document_duration = document["duration_ms"] or 0
            # Overlapping speech makes this legitimate, not an error.
            overlap_excess = gross_total > document_duration

            counters["cues_aggregated"] += aggregated_cues
            counters["unassigned_cue_count"] += unassigned.get(document["document_id"], 0)
            counters["normalization_collision_count"] += len(collisions)
            counters["documents_with_overlap_excess"] += overlap_excess

            result = {
                "lecture_id": str(document["lecture_id"]),
                "subject": document["subject"],
                "document_id": str(document["document_id"]),
                "parser_version": document["parser_version"],
                "speaker_inventory_version": self.inventory_version,
                "document_cue_count": document["cue_count"],
                "cues_aggregated": aggregated_cues,
                "distinct_raw_speaker_count": len(rows),
                "unassigned_speaker_cue_count": unassigned.get(document["document_id"], 0),
                "normalization_collision_count": len(collisions),
                "gross_spoken_ms_total": gross_total,
                "document_duration_ms": document["duration_ms"],
                "gross_exceeds_document_duration": overlap_excess,
                "gross_to_duration_ratio": (
                    round(gross_total / document_duration, 4) if document_duration else None),
            }

            if persist:
                outcome = self.speaker_repository.upsert_speakers(
                    connection, document["document_id"], self.inventory_version, speakers)
                counters["speakers_created"] += outcome["created"]
                counters["speakers_updated"] += outcome["updated"]
                result["speaker_rows_created"] = outcome["created"]
                result["speaker_rows_updated"] = outcome["updated"]
                result["speaker_rows_removed"] = outcome["removed"]
            else:
                result["speaker_rows_created"] = 0
                result["speaker_rows_updated"] = 0
            results.append(result)

        legacy_diagnostic = None
        if self.legacy_diagnostic_repository is not None:
            legacy_diagnostic = self._legacy_trainer_diagnostic(
                connection, target_date, documents, aggregates)

        summary = {
            "run_id": str(run_id), "target_date": target_date.isoformat(),
            "mode": "SHADOW" if persist else "DRY_RUN", "status": "COMPLETED",
            "speaker_inventory_version": self.inventory_version,
            "parser_version": self.parser_version,
            **counters,
            "graph_calls": 0, "webvtt_reparsed": False,
            "attendance_lookups": 0, "person_matches": 0, "roles_assigned": 0,
            "documents": results,
            "legacy_trainer_diagnostic": legacy_diagnostic,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "metadata": {
                "speaker_inventory_version": self.inventory_version,
                "parser_version": self.parser_version,
                "source": "CANONICAL_CUES_ONLY",
                "graph_calls": 0,
                "identity_inference_performed": False,
                "engagement_calculated": False,
                "counters": counters,
            },
        }
        if persist:
            self.run_repository.complete(connection, run_id, summary)
        # Counts only. No speaker names, no cue text.
        self.log.info("speaker inventory completed", extra={"fields": {
            "service": "speaker_inventory", "operation": "build_day",
            "run_id": str(run_id), "status": summary["status"],
            "documents": len(results),
            "speakers_created": counters["speakers_created"],
            "duration_ms": summary["duration_ms"],
        }})
        return summary

    def _legacy_trainer_diagnostic(self, connection, target_date, documents, aggregates) -> dict:
        """
        READ-ONLY: does the legacy trainer string appear among canonical labels?

        Exact comparisons only, on the raw label and on the conservative
        normalized form. No fuzzy matching, no role assignment, and the
        inventory is never adjusted to improve the result.
        """
        legacy_rows = self.legacy_diagnostic_repository.load_trainers(connection, target_date)
        by_subject = {normalize_group(item["subject"]): item for item in documents}
        rows = []
        exact_raw = exact_normalized = 0
        for legacy in legacy_rows:
            document = by_subject.get(normalize_group(legacy["subject"] or ""))
            labels = [row["speaker_label_raw"]
                      for row in aggregates.get(document["document_id"], [])] if document else []
            trainer = legacy["trainer"] or ""
            raw_hit = trainer in labels
            normalized_hit = normalize_speaker_label(trainer) in {
                normalize_speaker_label(label) for label in labels}
            exact_raw += raw_hit
            exact_normalized += normalized_hit
            rows.append({
                "subject": legacy["subject"],
                "document_id": str(document["document_id"]) if document else None,
                "canonical_speaker_count": len(labels),
                "legacy_trainer_present_as_exact_raw_label": "YES" if raw_hit else "NO",
                "legacy_trainer_present_as_exact_normalized_label":
                    "YES" if normalized_hit else "NO",
                # Length only: the name itself is not echoed into reports.
                "legacy_trainer_label_length": len(trainer),
            })
        return {
            "qa_rows": len(legacy_rows),
            "exact_raw_matches": exact_raw,
            "exact_normalized_matches": exact_normalized,
            "summary_raw": f"{exact_raw} / {len(legacy_rows)}",
            "summary_normalized": f"{exact_normalized} / {len(legacy_rows)}",
            "note": "Diagnostic only. No role assigned, no fuzzy matching, "
                    "no inventory adjustment.",
            "rows": rows,
        }
