"""
Phase 2B persistence: selections, selected parts, and derived combined text.

Reads Phase 2A raw evidence; never writes it. Writes only
lecture_transcript_selections, lecture_transcript_selection_parts,
lecture_combined_transcripts, and lecture_transcript_selection_runs.
"""
import json
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError
from app.transcripts.identity import canonical_key
from app.transcripts.selection import CandidateArtifact


COMBINED_NAMESPACE = uuid.UUID("7d41e0b9-3f52-4c86-9a17-6be2c8d05f34")

LOAD_CANDIDATES = """
SELECT a.artifact_id, a.provider_transcript_id, a.provider_created_at, a.provider_end_at,
       a.provider_call_id, a.meeting_id, a.content_sha256, a.content_bytes,
       a.first_seen_at
  FROM public.lecture_transcript_candidates c
  JOIN public.lecture_transcript_artifacts a ON a.artifact_id = c.artifact_id
 WHERE c.lecture_id = %s
 ORDER BY a.provider_created_at, a.provider_transcript_id
"""

LOAD_CONTENT = """
SELECT v.raw_content, v.content_sha256, v.speaker_attribution
  FROM public.lecture_transcript_artifacts a
  JOIN public.lecture_transcript_artifact_contents v
    ON v.artifact_id = a.artifact_id AND v.version_number = a.content_version
 WHERE a.artifact_id = %s
"""


class TranscriptSelectionRepository:
    def load_candidates(self, connection, lecture_id) -> list[CandidateArtifact]:
        """Phase 2A artifacts visible to this lecture. No Graph call."""
        try:
            rows = connection.execute(LOAD_CANDIDATES, (lecture_id,)).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript candidate query failed") from exc
        return [
            CandidateArtifact(
                artifact_id=row[0], provider_transcript_id=row[1],
                provider_created_at=row[2], provider_end_at=row[3],
                provider_call_id=row[4], meeting_id=row[5],
                content_sha256=row[6], content_bytes=row[7],
            )
            for row in collapse_equivalent_transcripts(rows)
        ]

    def load_current_content(self, connection, artifact_id) -> tuple[str, str, bool] | None:
        """Raw stored WebVTT for an artifact's current content version."""
        try:
            row = connection.execute(LOAD_CONTENT, (artifact_id,)).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript content query failed") from exc
        return (row[0], row[1], row[2]) if row else None

    def upsert_selection(self, connection, selection_id, lecture_id, *, selection_version,
                         primary_artifact_id, primary_provider_transcript_id, selection_status,
                         diagnostics, parts, combined) -> str:
        """
        Replace this lecture's selection for this algorithm version in place.

        Parts are rewritten wholesale so a changed selection can never leave a
        stale part row behind.
        """
        try:
            row = connection.execute("""
            INSERT INTO public.lecture_transcript_selections (
                selection_id, lecture_id, selection_version, primary_artifact_id,
                primary_provider_transcript_id, selection_status,
                candidate_count_before_date_filter, same_day_candidate_count,
                occurrence_window_candidate_count, selected_part_count,
                primary_overlap_seconds, primary_duration_seconds,
                primary_start_distance_seconds, actual_start, actual_end,
                start_difference_minutes, end_difference_minutes,
                combined_content_sha256, combined_content_bytes,
                combined_duration_seconds, metadata
            ) VALUES (
                %(selection_id)s, %(lecture_id)s, %(selection_version)s, %(primary_artifact_id)s,
                %(primary_provider_transcript_id)s, %(selection_status)s,
                %(candidate_count_before_date_filter)s, %(same_day_candidate_count)s,
                %(occurrence_window_candidate_count)s, %(selected_part_count)s,
                %(primary_overlap_seconds)s, %(primary_duration_seconds)s,
                %(primary_start_distance_seconds)s, %(actual_start)s, %(actual_end)s,
                %(start_difference_minutes)s, %(end_difference_minutes)s,
                %(combined_content_sha256)s, %(combined_content_bytes)s,
                %(combined_duration_seconds)s, %(metadata)s::jsonb
            )
            ON CONFLICT (selection_id) DO UPDATE SET
                primary_artifact_id = EXCLUDED.primary_artifact_id,
                primary_provider_transcript_id = EXCLUDED.primary_provider_transcript_id,
                selection_status = EXCLUDED.selection_status,
                candidate_count_before_date_filter = EXCLUDED.candidate_count_before_date_filter,
                same_day_candidate_count = EXCLUDED.same_day_candidate_count,
                occurrence_window_candidate_count = EXCLUDED.occurrence_window_candidate_count,
                selected_part_count = EXCLUDED.selected_part_count,
                primary_overlap_seconds = EXCLUDED.primary_overlap_seconds,
                primary_duration_seconds = EXCLUDED.primary_duration_seconds,
                primary_start_distance_seconds = EXCLUDED.primary_start_distance_seconds,
                actual_start = EXCLUDED.actual_start, actual_end = EXCLUDED.actual_end,
                start_difference_minutes = EXCLUDED.start_difference_minutes,
                end_difference_minutes = EXCLUDED.end_difference_minutes,
                combined_content_sha256 = EXCLUDED.combined_content_sha256,
                combined_content_bytes = EXCLUDED.combined_content_bytes,
                combined_duration_seconds = EXCLUDED.combined_duration_seconds,
                metadata = EXCLUDED.metadata, updated_at = now()
            RETURNING (xmax = 0) AS inserted
            """, {
                "selection_id": selection_id, "lecture_id": lecture_id,
                "selection_version": selection_version,
                "primary_artifact_id": primary_artifact_id,
                "primary_provider_transcript_id": primary_provider_transcript_id,
                "selection_status": selection_status,
                "candidate_count_before_date_filter": diagnostics.get("candidate_count_before_date_filter", 0),
                "same_day_candidate_count": diagnostics.get("same_day_candidate_count", 0),
                "occurrence_window_candidate_count": diagnostics.get("occurrence_window_candidate_count", 0),
                "selected_part_count": len(parts),
                "primary_overlap_seconds": diagnostics.get("primary_overlap_seconds"),
                "primary_duration_seconds": diagnostics.get("primary_duration_seconds"),
                "primary_start_distance_seconds": diagnostics.get("primary_start_distance_seconds"),
                "actual_start": diagnostics.get("actual_start"),
                "actual_end": diagnostics.get("actual_end"),
                "start_difference_minutes": diagnostics.get("start_difference_minutes"),
                "end_difference_minutes": diagnostics.get("end_difference_minutes"),
                "combined_content_sha256": combined.get("content_sha256") if combined else None,
                "combined_content_bytes": combined.get("content_bytes") if combined else None,
                "combined_duration_seconds": combined.get("duration_seconds") if combined else None,
                "metadata": json.dumps(diagnostics, default=str),
            }).fetchone()

            connection.execute(
                "DELETE FROM public.lecture_transcript_selection_parts WHERE selection_id = %s",
                (selection_id,))
            for index, part in enumerate(parts, start=1):
                connection.execute("""
                INSERT INTO public.lecture_transcript_selection_parts (
                    selection_id, artifact_id, part_index, is_primary, part_offset_ms,
                    provider_transcript_id, provider_call_id, provider_created_at, provider_end_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    selection_id, part["artifact_id"], index, part["is_primary"],
                    part["part_offset_ms"], part["provider_transcript_id"],
                    part["provider_call_id"], part["provider_created_at"], part["provider_end_at"],
                ))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "transcript selection upsert failed") from exc
        return "created" if row[0] else "updated"

    def upsert_combined(self, connection, selection_id, lecture_id, *, selection_version,
                        combined, source_fingerprint, speaker_attribution, metadata) -> str:
        """
        Store the derived combined transcript.

        Identical inputs produce an identical `source_fingerprint` and
        `content_sha256`; in that case the existing row is reused untouched
        rather than rewritten, so reruns create no duplicate derived evidence.
        """
        try:
            existing = connection.execute(
                "SELECT content_sha256, source_fingerprint "
                "FROM public.lecture_combined_transcripts WHERE selection_id = %s",
                (selection_id,)).fetchone()
            if existing and existing[0] == combined["content_sha256"] \
                    and existing[1] == source_fingerprint:
                return "reused"
            connection.execute("""
            INSERT INTO public.lecture_combined_transcripts (
                combined_id, selection_id, lecture_id, selection_version, combined_content,
                content_sha256, content_bytes, duration_seconds, duration_minutes,
                parts_combined, speaker_attribution, source_fingerprint, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (selection_id) DO UPDATE SET
                combined_content = EXCLUDED.combined_content,
                content_sha256 = EXCLUDED.content_sha256,
                content_bytes = EXCLUDED.content_bytes,
                duration_seconds = EXCLUDED.duration_seconds,
                duration_minutes = EXCLUDED.duration_minutes,
                parts_combined = EXCLUDED.parts_combined,
                speaker_attribution = EXCLUDED.speaker_attribution,
                source_fingerprint = EXCLUDED.source_fingerprint,
                metadata = EXCLUDED.metadata, updated_at = now()
            """, (
                uuid.uuid5(COMBINED_NAMESPACE, str(selection_id)), selection_id, lecture_id,
                selection_version, combined["text"], combined["content_sha256"],
                combined["content_bytes"], combined["duration_seconds"],
                combined["duration_minutes"], combined["parts_combined"],
                speaker_attribution, source_fingerprint, json.dumps(metadata, default=str),
            ))
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "combined transcript upsert failed") from exc
        return "created" if not existing else "updated"


class TranscriptSelectionRunRepository:
    COUNTERS = (
        "lectures_considered", "lectures_ready", "lectures_skipped_not_ready",
        "selections_created", "selections_updated", "selections_unchanged",
        "combined_transcripts_created", "combined_transcripts_reused",
        "multi_part_selections", "legacy_parity_matched", "legacy_parity_total",
        "error_count",
    )

    def start(self, connection, target_date, selection_version, mode="SHADOW") -> uuid.UUID:
        run_id = uuid.uuid4()
        try:
            connection.execute(
                "INSERT INTO public.lecture_transcript_selection_runs "
                "(run_id, target_date, mode, status, selection_version) "
                "VALUES (%s, %s, %s, 'RUNNING', %s)",
                (run_id, target_date, mode, selection_version),
            )
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not create selection run") from exc
        return run_id

    def complete(self, connection, run_id, summary) -> None:
        assignments = ", ".join(f"{name} = %s" for name in self.COUNTERS)
        values = [int(summary.get(name, 0)) for name in self.COUNTERS]
        try:
            connection.execute(
                "UPDATE public.lecture_transcript_selection_runs "
                f"SET completed_at = now(), status = %s, {assignments}, metadata = %s::jsonb "
                "WHERE run_id = %s",
                [summary["status"], *values, json.dumps(summary.get("metadata", {}), default=str), run_id],
            )
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not complete selection run") from exc


def collapse_equivalent_transcripts(rows) -> list:
    """
    One candidate per real transcript, however many ways Graph has spelled it.

    Graph re-serialized its transcript ids on 2026-09-22, so a lecture can see
    the SAME transcript as two artifacts. Left alone, the selector ranks them
    identically and its part-attachment rule (same call id) attaches both - the
    same transcript twice, every cue duplicated - or picks whichever sorts
    first, so the legacy session_id can change between runs with nothing about
    the lecture having changed.

    The representative is the spelling the platform saw FIRST (earliest
    first_seen_at, then artifact_id). That makes the choice stable against
    every future re-serialization: a new spelling can arrive, but it can never
    displace the one a lecture is already published under.

    Ids that do not decode keep their raw identity and are never collapsed.
    Row order is preserved, so the selector's own tie-breaking is unchanged.
    """
    rows = list(rows)
    keeper = {}
    for row in rows:
        key = canonical_key(row[1])
        rank = (row[8] is None, row[8], str(row[0]))
        if key not in keeper or rank < keeper[key][0]:
            keeper[key] = (rank, row[0])
    kept = {artifact for _, artifact in keeper.values()}
    return [row for row in rows if row[0] in kept]
