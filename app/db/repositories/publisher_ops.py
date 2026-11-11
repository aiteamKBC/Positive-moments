"""
Publisher runtime state (migration 028): periodic-task schedule, alert outbox,
reconciliation runs and operator part decisions. No network calls; every
method runs inside the caller's transaction (or autocommit statement).
"""
from __future__ import annotations

import uuid

from psycopg.types.json import Jsonb

SCHEDULE_FIELDS = ("task", "last_started_at", "last_finished_at", "last_succeeded_at", "last_status", "last_detail")
ALERT_FIELDS = ("alert_id", "severity", "kind", "dedupe_key", "lecture_id", "summary", "detail", "status",
                "notification_status", "occurrences", "first_seen_at", "last_seen_at", "resolved_at")


class PublisherScheduleRepository:

    def get(self, connection, task: str) -> dict | None:
        row = connection.execute(f"SELECT {', '.join(SCHEDULE_FIELDS)} FROM public.bunny_publisher_schedule "
                                 "WHERE task = %s", (task,)).fetchone()
        return dict(zip(SCHEDULE_FIELDS, row)) if row else None

    def all(self, connection) -> list[dict]:
        rows = connection.execute(f"SELECT {', '.join(SCHEDULE_FIELDS)} FROM public.bunny_publisher_schedule "
                                  "ORDER BY task").fetchall()
        return [dict(zip(SCHEDULE_FIELDS, r)) for r in rows]

    def started(self, connection, task: str, now) -> None:
        connection.execute("""
            INSERT INTO public.bunny_publisher_schedule (task, last_started_at, last_status, updated_at)
            VALUES (%s, %s, 'RUNNING', %s)
            ON CONFLICT (task) DO UPDATE SET last_started_at = EXCLUDED.last_started_at, last_status = 'RUNNING',
                updated_at = EXCLUDED.updated_at""", (task, now, now))

    def finished(self, connection, task: str, now, *, status: str, detail: dict | None = None) -> None:
        connection.execute("""
            UPDATE public.bunny_publisher_schedule
               SET last_finished_at = %s, last_status = %s, last_detail = %s, updated_at = %s,
                   last_succeeded_at = CASE WHEN %s = 'SUCCEEDED' THEN %s ELSE last_succeeded_at END
             WHERE task = %s""", (now, status, Jsonb(detail or {}), now, status, now, task))


class PublisherAlertRepository:

    def raise_alert(self, connection, *, severity: str, kind: str, dedupe_key: str, summary: str,
                    detail: dict | None = None, lecture_id=None, now=None) -> tuple[dict, bool]:
        """(alert, created). An open alert with the same dedupe key is refreshed, never duplicated."""
        row = connection.execute(f"""
            INSERT INTO public.bunny_publisher_alerts (alert_id, severity, kind, dedupe_key, lecture_id, summary, detail,
                                                 first_seen_at, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()), coalesce(%s, now()))
            ON CONFLICT (dedupe_key) WHERE status = 'OPEN' DO UPDATE
               SET occurrences = public.bunny_publisher_alerts.occurrences + 1, last_seen_at = EXCLUDED.last_seen_at,
                   severity = EXCLUDED.severity, summary = EXCLUDED.summary, detail = EXCLUDED.detail
            RETURNING {', '.join(ALERT_FIELDS)}, (xmax = 0)""",
            (uuid.uuid4(), severity, kind, dedupe_key[:300], lecture_id, summary[:500], Jsonb(detail or {}),
             now, now)).fetchone()
        return dict(zip(ALERT_FIELDS, row[:-1])), bool(row[-1])

    def resolve_absent(self, connection, kind: str, present_keys, now=None) -> int:
        """Resolve this kind's open alerts whose condition is no longer observed."""
        return connection.execute(
            "UPDATE public.bunny_publisher_alerts SET status = 'RESOLVED', resolved_at = coalesce(%s, now()) "
            "WHERE status = 'OPEN' AND kind = %s AND NOT (dedupe_key = ANY(%s))",
            (now, kind, list(present_keys))).rowcount

    def mark_delivery(self, connection, alert_id, notification_status: str) -> None:
        connection.execute("UPDATE public.bunny_publisher_alerts SET notification_status = %s WHERE alert_id = %s",
                           (notification_status, alert_id))

    def open_alerts(self, connection, *, severity=None, limit: int = 500) -> list[dict]:
        rows = connection.execute(
            f"SELECT {', '.join(ALERT_FIELDS)} FROM public.bunny_publisher_alerts WHERE status = 'OPEN' "
            "AND (%s::text IS NULL OR severity = %s) ORDER BY severity, last_seen_at DESC LIMIT %s",
            (severity, severity, limit)).fetchall()
        return [dict(zip(ALERT_FIELDS, r)) for r in rows]


class PublisherReconciliationRepository:

    def start(self, connection, kind: str) -> uuid.UUID:
        run_id = uuid.uuid4()
        connection.execute("INSERT INTO public.bunny_publisher_reconciliation_runs (run_id, kind) VALUES (%s, %s)",
                           (run_id, kind))
        return run_id

    def finish(self, connection, run_id, *, status: str, summary: dict) -> None:
        connection.execute("UPDATE public.bunny_publisher_reconciliation_runs SET status = %s, finished_at = now(), "
                           "summary = %s WHERE run_id = %s", (status, Jsonb(summary), run_id))

    def latest(self, connection, kind: str) -> dict | None:
        row = connection.execute("SELECT run_id, kind, started_at, finished_at, status, summary "
                                 "FROM public.bunny_publisher_reconciliation_runs WHERE kind = %s "
                                 "ORDER BY started_at DESC LIMIT 1", (kind,)).fetchone()
        return dict(zip(("run_id", "kind", "started_at", "finished_at", "status", "summary"), row)) if row else None


class PartDecisionRepository:

    def decisions(self, connection, lecture_id) -> dict[int, str]:
        rows = connection.execute("SELECT recording_part_index, decision FROM public.bunny_publisher_part_decisions "
                                  "WHERE lecture_id = %s", (lecture_id,)).fetchall()
        return {r[0]: r[1] for r in rows}

    def decide(self, connection, lecture_id, part_index: int, *, decision: str, reason: str, actor: str) -> None:
        connection.execute("""
            INSERT INTO public.bunny_publisher_part_decisions (lecture_id, recording_part_index, decision, reason, actor)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (lecture_id, recording_part_index) DO UPDATE
               SET decision = EXCLUDED.decision, reason = EXCLUDED.reason, actor = EXCLUDED.actor,
                   decided_at = now()""", (lecture_id, part_index, decision, reason, actor))
