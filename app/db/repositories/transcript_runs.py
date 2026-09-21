import json
import uuid
from datetime import date

from app.common.errors import DATABASE_ERROR, PlatformError


COUNTERS = (
    "lectures_considered", "lectures_ready", "lectures_skipped_not_ready",
    "meetings_queried", "meetings_with_zero_artifacts", "meetings_with_artifacts",
    "artifacts_discovered", "artifacts_created", "artifacts_existing",
    "artifact_contents_fetched", "content_versions_created", "content_unchanged",
    "admin_blocked_count", "speaker_attribution_fallback_count", "error_count",
)


class TranscriptAcquisitionRunRepository:
    """Audit summaries only. Never tokens, headers, secrets, or transcript text."""

    def start(self, connection, target_date: date, mode: str = "SHADOW") -> uuid.UUID:
        run_id = uuid.uuid4()
        try:
            connection.execute(
                "INSERT INTO public.lecture_transcript_acquisition_runs "
                "(run_id, target_date, mode, status) VALUES (%s, %s, %s, 'RUNNING')",
                (run_id, target_date, mode),
            )
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not create transcript acquisition run") from exc
        return run_id

    def complete(self, connection, run_id: uuid.UUID, summary: dict) -> None:
        assignments = ", ".join(f"{name} = %s" for name in COUNTERS)
        values = [int(summary.get(name, 0)) for name in COUNTERS]
        try:
            connection.execute(
                "UPDATE public.lecture_transcript_acquisition_runs "
                f"SET completed_at = now(), status = %s, {assignments}, metadata = %s::jsonb "
                "WHERE run_id = %s",
                [summary["status"], *values, json.dumps(summary.get("metadata", {})), run_id],
            )
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not complete transcript acquisition run") from exc
