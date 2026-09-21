"""
Phase 2A: raw transcript acquisition and artifact registry.

    lecture_sessions (read-only)
      -> Graph transcript listing, in the persisted user context
      -> lecture_transcript_artifacts
      -> lecture_transcript_artifact_contents (raw bytes + SHA-256)

It STOPS there. No primary selection, no multi-part combination, no timeline
shifting, no cue parsing, no QA, no Positive Clips, no Lecture Split, no AI, no
media jobs.
"""
import logging
import time
import uuid
from datetime import date

from app.common.hashing import content_sha256, provider_transcript_identity
from app.transcripts.models import (
    BLOCKED_TRANSCRIPT_ACCESS,
    CONTENT_FETCH_ERROR,
    CONTENT_STORED,
    CONTENT_STORED_NO_SPEAKER_ATTRIBUTION,
    DISCOVERED,
    ERROR,
    NO_TRANSCRIPTS_AVAILABLE,
    PROVIDER_MICROSOFT_GRAPH,
    TRANSCRIPT_NOT_FOUND,
    TRANSCRIPT_NOT_READY,
    TRANSCRIPTS_FOUND,
)


BLOCKED_ARTIFACT_STATUSES = {BLOCKED_TRANSCRIPT_ACCESS, TRANSCRIPT_NOT_FOUND,
                             TRANSCRIPT_NOT_READY, CONTENT_FETCH_ERROR}


class TranscriptAcquisitionService:
    def __init__(self, *, gateway, lecture_repository, artifact_repository,
                 run_repository, qa_evidence_repository=None):
        self.gateway = gateway
        self.lecture_repository = lecture_repository
        self.artifact_repository = artifact_repository
        self.run_repository = run_repository
        self.qa_evidence_repository = qa_evidence_repository
        self.log = logging.getLogger(__name__)

    def acquire_day(self, connection, target_date: date, *, persist: bool = True,
                    fetch_content: bool = True) -> dict:
        started = time.monotonic()
        run_id = self.run_repository.start(connection, target_date) if persist else uuid.uuid4()

        inputs = self.lecture_repository.load_day(connection, target_date)
        ready, skipped = inputs["ready"], inputs["skipped"]

        counters = {
            "lectures_considered": inputs["considered"],
            "lectures_ready": len(ready),
            "lectures_skipped_not_ready": len(skipped),
            "meetings_queried": 0, "meetings_with_zero_artifacts": 0,
            "meetings_with_artifacts": 0, "artifacts_discovered": 0,
            "artifacts_created": 0, "artifacts_existing": 0,
            "artifact_contents_fetched": 0, "content_versions_created": 0,
            "content_unchanged": 0, "admin_blocked_count": 0,
            "speaker_attribution_fallback_count": 0, "error_count": 0,
        }
        lecture_results: list[dict] = []
        discovered_ids_by_lecture: dict[str, list[str]] = {}

        for lecture in ready:
            counters["meetings_queried"] += 1
            listing = self.gateway.list_transcripts(
                lecture.meeting_lookup_user_id, lecture.meeting_id
            )
            result = {
                "lecture_id": str(lecture.lecture_id),
                "subject": lecture.subject,
                "module": lecture.module,
                "scheduled_start": lecture.scheduled_start.isoformat(),
                "scheduled_end": lecture.scheduled_end.isoformat(),
                # Meeting resolution provenance, carried from Phase 1.
                "meeting_lookup_user_id": lecture.meeting_lookup_user_id,
                "meeting_lookup_context_source": lecture.meeting_lookup_context_source,
                "join_url_oid_hint_status": lecture.join_url_oid_hint_status,
                "listing_status": listing.status,
                "listing_reason": listing.reason,
                "listing_http_status": listing.http_status,
                "artifact_count": len(listing.artifacts),
                "artifacts": [],
            }

            if listing.status == NO_TRANSCRIPTS_AVAILABLE:
                # A successful, empty provider answer. Not an error.
                counters["meetings_with_zero_artifacts"] += 1
                result["status"] = NO_TRANSCRIPTS_AVAILABLE
                lecture_results.append(result)
                continue
            if listing.status != TRANSCRIPTS_FOUND:
                if listing.status == BLOCKED_TRANSCRIPT_ACCESS:
                    counters["admin_blocked_count"] += 1
                    result["status"] = BLOCKED_TRANSCRIPT_ACCESS
                else:
                    counters["error_count"] += 1
                    result["status"] = ERROR
                lecture_results.append(result)
                continue

            counters["meetings_with_artifacts"] += 1
            counters["artifacts_discovered"] += len(listing.artifacts)
            discovered_ids_by_lecture[str(lecture.lecture_id)] = [
                artifact.provider_transcript_id for artifact in listing.artifacts
            ]

            stored = attributed = unattributed = 0
            for artifact in listing.artifacts:
                outcome = self._acquire_artifact(
                    connection, lecture, artifact, counters,
                    persist=persist, fetch_content=fetch_content,
                )
                result["artifacts"].append(outcome)
                if outcome["artifact_status"] == CONTENT_STORED:
                    stored += 1
                    attributed += 1
                elif outcome["artifact_status"] == CONTENT_STORED_NO_SPEAKER_ATTRIBUTION:
                    stored += 1
                    unattributed += 1

            if not fetch_content:
                result["status"] = TRANSCRIPTS_FOUND
            elif stored and not unattributed:
                result["status"] = CONTENT_STORED
            elif stored and unattributed:
                result["status"] = CONTENT_STORED_NO_SPEAKER_ATTRIBUTION
            elif any(item["artifact_status"] == BLOCKED_TRANSCRIPT_ACCESS
                     for item in result["artifacts"]):
                result["status"] = BLOCKED_TRANSCRIPT_ACCESS
            elif any(item["artifact_status"] == TRANSCRIPT_NOT_READY
                     for item in result["artifacts"]):
                result["status"] = TRANSCRIPT_NOT_READY
            else:
                result["status"] = ERROR
            result["speaker_attributed_artifacts"] = attributed
            result["unattributed_artifacts"] = unattributed
            lecture_results.append(result)

        qa_parity = None
        if self.qa_evidence_repository is not None:
            qa_parity = self._qa_parity(
                connection, target_date, lecture_results, discovered_ids_by_lecture
            )

        summary = {
            "run_id": str(run_id),
            "target_date": target_date.isoformat(),
            "mode": "SHADOW" if persist else "DRY_RUN",
            "status": "COMPLETED",
            **counters,
            "canonical_lectures": inputs["considered"],
            "meetings_with_one_artifact": sum(
                1 for item in lecture_results if item["artifact_count"] == 1),
            "meetings_with_multiple_artifacts": sum(
                1 for item in lecture_results if item["artifact_count"] > 1),
            "skipped_lectures": skipped,
            "lectures": lecture_results,
            "legacy_qa_parity": qa_parity,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "metadata": {
                "write_policy": "phase2a_transcript_tables_only",
                "input_contract": "lecture_sessions.downstream_ready",
                "user_context": "lecture_sessions.meeting_lookup_user_id",
                "listing_scope": "SERIES_MEETING_EXPOSES_ALL_OCCURRENCES",
                "selection_performed": False,
                "combination_performed": False,
                "timeline_shifted": False,
                "legacy_qa_parity": qa_parity,
                "counters": counters,
            },
        }
        if persist:
            self.run_repository.complete(connection, run_id, summary)
        # Structured, non-sensitive fields only. Never transcript text.
        self.log.info("transcript acquisition completed", extra={"fields": {
            "service": "transcript_acquisition", "operation": "acquire_day",
            "run_id": str(run_id), "status": summary["status"],
            "lectures_ready": counters["lectures_ready"],
            "artifacts_discovered": counters["artifacts_discovered"],
            "duration_ms": summary["duration_ms"],
        }})
        return summary

    def _acquire_artifact(self, connection, lecture, artifact, counters, *,
                          persist: bool, fetch_content: bool) -> dict:
        # Provider identity: the same recurring-series transcript reached from
        # two lectures is ONE artifact row with one copy of the raw bytes.
        artifact_id = provider_transcript_identity(
            provider=PROVIDER_MICROSOFT_GRAPH,
            provider_transcript_id=artifact.provider_transcript_id,
        )
        outcome = {
            "artifact_id": str(artifact_id),
            "provider_transcript_id": artifact.provider_transcript_id,
            "provider_created_at": artifact.provider_created_at.isoformat()
            if artifact.provider_created_at else None,
            "provider_end_at": artifact.provider_end_at.isoformat()
            if artifact.provider_end_at else None,
            "provider_call_id": artifact.provider_call_id,
            "content_correlation_id": artifact.content_correlation_id,
            "artifact_status": DISCOVERED,
            "content_available": False,
            "content_format": None,
            "speaker_attribution": None,
            "content_bytes": None,
            "content_sha256_prefix": None,
            "content_version": None,
            "content_version_created": False,
            "registry_outcome": None,
        }

        if persist:
            outcome["registry_outcome"] = self.artifact_repository.upsert(
                connection, artifact_id, lecture.lecture_id,
                provider=PROVIDER_MICROSOFT_GRAPH,
                provider_transcript_id=artifact.provider_transcript_id,
                meeting_id=lecture.meeting_id,
                meeting_lookup_user_id=lecture.meeting_lookup_user_id,
                provider_call_id=artifact.provider_call_id,
                content_correlation_id=artifact.content_correlation_id,
                provider_created_at=artifact.provider_created_at,
                provider_end_at=artifact.provider_end_at,
                artifact_status=DISCOVERED,
                metadata=artifact.metadata,
            )
            counters["artifacts_created"] += outcome["registry_outcome"] == "created"
            counters["artifacts_existing"] += outcome["registry_outcome"] == "existing"

        if not fetch_content:
            return outcome

        content = self.gateway.fetch_content(
            lecture.meeting_lookup_user_id, lecture.meeting_id,
            artifact.provider_transcript_id,
        )
        if content.used_speaker_attribution_fallback:
            counters["speaker_attribution_fallback_count"] += 1

        if content.raw is None:
            outcome["artifact_status"] = content.status
            outcome["content_reason"] = content.reason
            outcome["content_http_status"] = content.http_status
            if content.status == BLOCKED_TRANSCRIPT_ACCESS:
                counters["admin_blocked_count"] += 1
            elif content.status not in (TRANSCRIPT_NOT_READY, TRANSCRIPT_NOT_FOUND):
                counters["error_count"] += 1
            if persist:
                self.artifact_repository.mark_content_failure(
                    connection, artifact_id, content.status, content.reason
                )
            return outcome

        counters["artifact_contents_fetched"] += 1
        digest = content_sha256(content.raw)
        outcome.update({
            "artifact_status": CONTENT_STORED if content.speaker_attribution
            else CONTENT_STORED_NO_SPEAKER_ATTRIBUTION,
            "content_available": True,
            "content_format": content.content_format,
            "speaker_attribution": content.speaker_attribution,
            "content_bytes": len(content.raw),
            # Short prefix only; the full hash lives in the database.
            "content_sha256_prefix": digest[:12],
            "used_speaker_attribution_fallback": content.used_speaker_attribution_fallback,
        })
        if content.reason:
            outcome["content_reason"] = content.reason

        if not persist:
            return outcome

        previous = self.artifact_repository.current_content_hash(connection, artifact_id)
        stored = self.artifact_repository.store_content(
            connection, artifact_id,
            raw_text=content.raw.decode("utf-8", errors="replace"),
            content_sha256=digest, content_bytes=len(content.raw),
            content_format=content.content_format,
            speaker_attribution=bool(content.speaker_attribution),
        )
        outcome["content_version"] = stored["version_number"]
        outcome["content_version_created"] = stored["version_created"]
        if stored["version_created"]:
            counters["content_versions_created"] += 1
            if previous is not None and previous != digest:
                outcome["content_changed"] = True
        else:
            counters["content_unchanged"] += 1
        return outcome

    def _qa_parity(self, connection, target_date, lecture_results, discovered_ids_by_lecture) -> dict:
        """
        Read-only parity: is each legacy session_id among the provider transcript
        IDs Phase 2A rediscovered for the matching canonical lecture?

        qa_doctors_sessions is never modified and session_id is never
        reinterpreted as an identity for the new architecture.
        """
        from app.lectures.matching import normalize_group

        qa_rows = self.qa_evidence_repository.load_day(connection, target_date)
        by_subject = {
            normalize_group(item["subject"]): item for item in lecture_results
        }
        all_discovered = {
            transcript_id
            for ids in discovered_ids_by_lecture.values() for transcript_id in ids
        }
        rows = []
        reproduced = 0
        for qa_row in qa_rows:
            subject = qa_row.get("subject") or ""
            match = by_subject.get(normalize_group(subject))
            lecture_ids = discovered_ids_by_lecture.get(
                match["lecture_id"], []) if match else []
            present = qa_row["session_id"] in lecture_ids
            reproduced += present
            rows.append({
                "qa_subject": subject,
                "legacy_session_id": qa_row["session_id"],
                "matched_lecture_id": match["lecture_id"] if match else None,
                "discovered_artifact_count": len(lecture_ids),
                "legacy_session_id_present_in_discovered_artifacts": "YES" if present else "NO",
                "present_anywhere_in_run": qa_row["session_id"] in all_discovered,
            })
        return {
            "qa_sessions": len(qa_rows),
            "legacy_session_ids_reproduced": reproduced,
            "summary": f"{reproduced} / {len(qa_rows)}",
            "rows": rows,
        }
