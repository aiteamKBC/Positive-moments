"""
Phase 2B orchestration: selection, combination, persistence, and legacy parity.

Entirely DB-backed. For a date whose evidence Phase 2A already stored, this
service makes NO Microsoft Graph calls at all - the whole pipeline is replayable
offline from PostgreSQL, which is the point of the Phase 2A raw registry.
"""
import logging
import time
import uuid
from datetime import date

from app.common.hashing import content_sha256, selection_identity
from app.lectures.matching import normalize_group
from app.transcripts.combine import combine_transcript_parts
from app.transcripts.selection import (
    SELECTED,
    SELECTION_VERSION,
    combined_source_fingerprint,
    select_transcript_parts,
)


class TranscriptSelectionService:
    def __init__(self, *, lecture_repository, selection_repository, run_repository,
                 qa_evidence_repository=None, selection_version: str = SELECTION_VERSION):
        self.lecture_repository = lecture_repository
        self.selection_repository = selection_repository
        self.run_repository = run_repository
        self.qa_evidence_repository = qa_evidence_repository
        self.selection_version = selection_version
        self.log = logging.getLogger(__name__)

    def select_day(self, connection, target_date: date, *, persist: bool = True) -> dict:
        started = time.monotonic()
        run_id = (
            self.run_repository.start(connection, target_date, self.selection_version)
            if persist else uuid.uuid4()
        )
        inputs = self.lecture_repository.load_day(connection, target_date)
        ready, skipped = inputs["ready"], inputs["skipped"]

        counters = {
            "lectures_considered": inputs["considered"],
            "lectures_ready": len(ready),
            "lectures_skipped_not_ready": len(skipped),
            "selections_created": 0, "selections_updated": 0, "selections_unchanged": 0,
            "combined_transcripts_created": 0, "combined_transcripts_reused": 0,
            "multi_part_selections": 0, "legacy_parity_matched": 0,
            "legacy_parity_total": 0, "error_count": 0,
        }
        results: list[dict] = []

        for lecture in ready:
            candidates = self.selection_repository.load_candidates(connection, lecture.lecture_id)
            outcome = select_transcript_parts(
                candidates,
                scheduled_start=lecture.scheduled_start,
                scheduled_end=lecture.scheduled_end,
                target_date=lecture.session_date,
                meeting_id=lecture.meeting_id,
            )
            result = {
                "lecture_id": str(lecture.lecture_id),
                "subject": lecture.subject,
                "module": lecture.module,
                "scheduled_start": lecture.scheduled_start.isoformat(),
                "scheduled_end": lecture.scheduled_end.isoformat(),
                "selection_status": outcome.status,
                **outcome.diagnostics,
            }
            if outcome.status != SELECTED:
                result["selected_parts_count"] = 0
                results.append(result)
                if persist:
                    selection_id = selection_identity(
                        lecture_id=lecture.lecture_id, selection_version=self.selection_version)
                    written = self.selection_repository.upsert_selection(
                        connection, selection_id, lecture.lecture_id,
                        selection_version=self.selection_version, primary_artifact_id=None,
                        primary_provider_transcript_id=None, selection_status=outcome.status,
                        diagnostics=outcome.diagnostics, parts=[], combined=None)
                    counters[f"selections_{'created' if written == 'created' else 'updated'}"] += 1
                continue

            combined, part_rows, combine_error = self._combine(connection, outcome)
            if combine_error:
                counters["error_count"] += 1
                result["combine_error"] = combine_error
                result["selection_status"] = "COMBINE_FAILED"

            if len(outcome.parts) > 1:
                counters["multi_part_selections"] += 1

            result.update({
                "selected_parts_count": len(outcome.parts),
                "combined_duration_seconds": combined["duration_seconds"] if combined else None,
                "combined_duration_minutes": combined["duration_minutes"] if combined else None,
                "combined_content_bytes": combined["content_bytes"] if combined else None,
                "combined_content_sha256_prefix": combined["content_sha256"][:12] if combined else None,
                "combined_from_parts": combined["parts_combined"] if combined else 0,
                "is_multi_part": bool(combined and combined["parts_combined"] > 1),
                "part_offsets_ms": [row["part_offset_ms"] for row in part_rows],
            })

            if persist:
                selection_id = selection_identity(
                    lecture_id=lecture.lecture_id, selection_version=self.selection_version)
                written = self.selection_repository.upsert_selection(
                    connection, selection_id, lecture.lecture_id,
                    selection_version=self.selection_version,
                    primary_artifact_id=outcome.primary.candidate.artifact_id,
                    primary_provider_transcript_id=outcome.primary.candidate.provider_transcript_id,
                    selection_status=result["selection_status"],
                    diagnostics=outcome.diagnostics, parts=part_rows, combined=combined)
                counters[f"selections_{'created' if written == 'created' else 'updated'}"] += 1
                if combined:
                    stored = self.selection_repository.upsert_combined(
                        connection, selection_id, lecture.lecture_id,
                        selection_version=self.selection_version, combined=combined,
                        source_fingerprint=combined["source_fingerprint"],
                        speaker_attribution=combined["speaker_attribution"],
                        metadata={"part_offsets_ms": combined["part_offsets_ms"],
                                  "selection_method": self.selection_version})
                    counters["combined_transcripts_reused" if stored == "reused"
                             else "combined_transcripts_created"] += 1
                result["selection_id"] = str(selection_id)
                result["persistence"] = written
            results.append(result)

        parity = None
        if self.qa_evidence_repository is not None:
            parity = self._parity(connection, target_date, results)
            counters["legacy_parity_matched"] = parity["matched"]
            counters["legacy_parity_total"] = parity["qa_sessions"]

        summary = {
            "run_id": str(run_id), "target_date": target_date.isoformat(),
            "mode": "SHADOW" if persist else "DRY_RUN",
            "status": "COMPLETED", "selection_version": self.selection_version,
            **counters,
            "graph_calls": 0,
            "skipped_lectures": skipped,
            "lectures": results,
            "legacy_qa_parity": parity,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "metadata": {
                "selection_version": self.selection_version,
                "source": "PERSISTED_PHASE_2A_ARTIFACTS_ONLY",
                "graph_calls": 0,
                "legacy_reference":
                    "automation/legacy_n8n/QA_One_Lecture_Safe_Exact_Recording_v8.json",
                "counters": counters,
                "legacy_qa_parity": parity,
            },
        }
        if persist:
            self.run_repository.complete(connection, run_id, summary)
        # Structured, non-sensitive fields only. Never transcript text.
        self.log.info("transcript selection completed", extra={"fields": {
            "service": "transcript_selection", "operation": "select_day",
            "run_id": str(run_id), "status": summary["status"],
            "lectures_ready": counters["lectures_ready"],
            "legacy_parity": f"{counters['legacy_parity_matched']}/{counters['legacy_parity_total']}",
            "duration_ms": summary["duration_ms"],
        }})
        return summary

    def _combine(self, connection, outcome):
        """Load each selected part's stored raw VTT and combine it. No Graph."""
        sources = []
        part_rows = []
        for part in outcome.parts:
            stored = self.selection_repository.load_current_content(
                connection, part.candidate.artifact_id)
            raw_text = stored[0] if stored else None
            sources.append((part, raw_text, stored[1] if stored else None,
                            stored[2] if stored else None))

        usable = [(part.start, raw) for part, raw, _sha, _attr in sources if raw]
        combined = None
        error = None
        offsets: list[int] = []
        if usable:
            try:
                result = combine_transcript_parts(usable)
                offsets = result.part_offsets_ms
                digest = content_sha256(result.text.encode("utf-8"))
                fingerprint = combined_source_fingerprint(
                    self.selection_version, [sha for _p, _r, sha, _a in sources])
                combined = {
                    "text": result.text, "content_sha256": digest,
                    "content_bytes": len(result.text.encode("utf-8")),
                    "duration_seconds": result.duration_seconds,
                    "duration_minutes": result.duration_minutes,
                    "parts_combined": result.parts_combined,
                    "part_offsets_ms": result.part_offsets_ms,
                    "source_fingerprint": fingerprint,
                    "speaker_attribution": all(
                        bool(attr) for _p, raw, _s, attr in sources if raw),
                }
            except ValueError as exc:
                error = str(exc)
        else:
            error = "no stored raw content for the selected parts"

        for index, (part, raw, _sha, _attr) in enumerate(sources):
            part_rows.append({
                "artifact_id": part.candidate.artifact_id,
                "provider_transcript_id": part.candidate.provider_transcript_id,
                "provider_call_id": part.candidate.provider_call_id,
                "provider_created_at": part.start,
                "provider_end_at": part.end,
                "is_primary": part.candidate.provider_transcript_id
                == outcome.primary.candidate.provider_transcript_id,
                "part_offset_ms": offsets[index] if index < len(offsets) and raw else 0,
            })
        return combined, part_rows, error

    def _parity(self, connection, target_date, results) -> dict:
        """
        Does the coded selector pick the artifact legacy QA recorded?

        qa_doctors_sessions is read-only evidence; session_id is never
        reinterpreted as an identity for the new architecture.
        """
        qa_rows = self.qa_evidence_repository.load_day(connection, target_date)
        by_subject = {normalize_group(item["subject"]): item for item in results}
        rows = []
        matched = 0
        for qa_row in qa_rows:
            subject = qa_row.get("subject") or ""
            result = by_subject.get(normalize_group(subject))
            selected = result.get("primary_transcript_id") if result else None
            is_match = bool(selected) and selected == qa_row["session_id"]
            matched += is_match
            rows.append({
                "qa_subject": subject,
                "lecture_id": result["lecture_id"] if result else None,
                "candidate_count": result.get("candidate_count_before_date_filter") if result else 0,
                "same_day_candidate_count": result.get("same_day_candidate_count") if result else 0,
                "window_candidate_count": result.get("occurrence_window_candidate_count") if result else 0,
                "selected_primary_transcript_id": selected,
                "legacy_session_id": qa_row["session_id"],
                "match": "YES" if is_match else "NO",
            })
        return {
            "qa_sessions": len(qa_rows), "matched": matched,
            "summary": f"{matched} / {len(qa_rows)}", "rows": rows,
        }
