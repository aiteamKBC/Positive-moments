import json
import uuid
from datetime import date

from app.common.errors import DATABASE_ERROR, PlatformError


class DiscoveryRunRepository:
    def start(self, connection, target_date: date, mode: str = "SHADOW") -> uuid.UUID:
        run_id = uuid.uuid4()
        try:
            connection.execute(
                "INSERT INTO public.lecture_discovery_runs (run_id, target_date, mode, status) VALUES (%s, %s, %s, 'RUNNING')",
                (run_id, target_date, mode),
            )
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not create discovery run") from exc
        return run_id

    def complete(self, connection, run_id: uuid.UUID, summary: dict) -> None:
        try:
            connection.execute("""
            UPDATE public.lecture_discovery_runs
               SET completed_at = now(), status = %s,
                   calendar_events_found = %s, teams_events_found = %s,
                   online_meetings_resolved = %s, active_group_matches = %s,
                   unmatched_count = %s, error_count = %s, metadata = %s::jsonb
             WHERE run_id = %s
            """,
                (summary["status"], summary["calendar_events_found"], summary["teams_events_found"],
                 summary["online_meetings_resolved"], summary["active_group_matches"],
                 summary["unmatched_count"], summary["error_count"],
                 json.dumps(summary.get("metadata", {})), run_id),
            )
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "could not complete discovery run") from exc
