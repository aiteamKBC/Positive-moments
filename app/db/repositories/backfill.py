"""
Persistence for Operations Backfill runs (migration 020).

This repository holds INTENT and PROGRESS. It deliberately stores no lecture
outcome: those are written by the same orchestrator the nightly scheduler uses,
into `lecture_pipeline_runs` / `lecture_pipeline_run_items`. A day row here
carries the `pipeline_run_id` so the console can link to that audit rather than
keep a second copy of it that could drift.

Every method takes an open connection and commits nothing. The caller owns the
transaction boundary, which is what lets the runner commit exactly at a day
boundary - the point it can safely be interrupted.
"""
import uuid

from app.common.errors import DATABASE_ERROR, PlatformError

PENDING = "PENDING"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
CANCEL_REQUESTED = "CANCEL_REQUESTED"
CANCELLED = "CANCELLED"
FAILED = "FAILED"
BLOCKED_LEGACY_QA_ACTIVE = "BLOCKED_LEGACY_QA_ACTIVE"

# A run in one of these is finished and must never be resumed or mutated.
TERMINAL = (COMPLETED, CANCELLED, FAILED, BLOCKED_LEGACY_QA_ACTIVE)

DAY_PENDING = "PENDING"
DAY_COMPLETED = "COMPLETED"
DAY_FAILED = "FAILED"
DAY_SKIPPED_CANCELLED = "SKIPPED_CANCELLED"
DAY_DEFERRED_CYCLE_BUSY = "DEFERRED_CYCLE_BUSY"

MODE_PREVIEW = "PREVIEW"
MODE_EXECUTE = "EXECUTE"

RUN_FIELDS = (
    "backfill_run_id", "requested_from", "requested_to", "status", "mode",
    "created_by",
    "created_at", "started_at", "finished_at", "current_business_date",
    "current_lecture_id", "current_lecture_label", "total_days",
    "completed_days", "discovered_count", "matched_count", "processed_count",
    "already_complete_count", "waiting_count", "review_required_count",
    "failed_count", "suppressed_count", "cancel_requested",
    "cancel_requested_at", "cancel_requested_by", "error_summary",
    "runner_version", "heartbeat_at", "updated_at")

SELECT_RUN = f"SELECT {', '.join(RUN_FIELDS)} FROM public.backfill_runs"

DAY_FIELDS = (
    "business_date", "status", "pipeline_run_id", "calendar_events_considered",
    "matched_lectures", "newly_discovered", "already_complete", "processed",
    "waiting", "review_required", "failed", "suppressed", "graph_calls",
    "provider_calls", "error_code", "error_message", "started_at",
    "finished_at", "duration_ms")

INSERT_RUN = """
INSERT INTO public.backfill_runs (
    backfill_run_id, requested_from, requested_to, status, mode, created_by,
    current_business_date, total_days, runner_version)
VALUES (%(backfill_run_id)s, %(requested_from)s, %(requested_to)s, 'PENDING',
        %(mode)s, %(created_by)s, %(requested_from)s, %(total_days)s,
        %(runner_version)s)
RETURNING backfill_run_id
"""

# The claim. `FOR UPDATE SKIP LOCKED` means a second runner cannot take the
# same run, and the PENDING/RUNNING pair means a crashed runner's row is
# recoverable rather than stranded - resuming is safe because every day is
# idempotent through existing stage resolution.
CLAIM_RUN = """
WITH candidate AS (
    SELECT backfill_run_id FROM public.backfill_runs
     WHERE status IN ('PENDING', 'RUNNING', 'CANCEL_REQUESTED')
     ORDER BY created_at ASC
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE public.backfill_runs r
   SET status = CASE WHEN r.cancel_requested THEN 'CANCEL_REQUESTED' ELSE 'RUNNING' END,
       started_at = coalesce(r.started_at, now()),
       heartbeat_at = now(),
       runner_version = %(runner_version)s,
       updated_at = now()
  FROM candidate c
 WHERE r.backfill_run_id = c.backfill_run_id
RETURNING r.backfill_run_id
"""


def _rows(connection, statement, params, fields, what):
    try:
        found = connection.execute(statement, params).fetchall()
    except Exception as exc:
        raise PlatformError(DATABASE_ERROR, f"{what} read failed") from exc
    return [dict(zip(fields, row)) for row in found]


class BackfillRepository:

    # -- creation ------------------------------------------------------------

    def create(self, connection, *, requested_from, requested_to, created_by,
               total_days, runner_version, mode=MODE_EXECUTE) -> str:
        run_id = uuid.uuid4()
        try:
            connection.execute(INSERT_RUN, {
                "backfill_run_id": run_id, "requested_from": requested_from,
                "requested_to": requested_to, "created_by": created_by,
                "mode": mode, "total_days": total_days,
                "runner_version": runner_version})
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "backfill run create failed") from exc
        return str(run_id)

    # -- reading -------------------------------------------------------------

    def get(self, connection, run_id) -> dict | None:
        found = _rows(connection, SELECT_RUN + " WHERE backfill_run_id = %s",
                      (run_id,), RUN_FIELDS, "backfill run")
        return found[0] if found else None

    def recent(self, connection, limit: int = 25) -> list[dict]:
        return _rows(connection,
                     SELECT_RUN + " ORDER BY created_at DESC LIMIT %s",
                     (limit,), RUN_FIELDS, "backfill run")

    def active(self, connection) -> dict | None:
        """The one run a runner should be working on, if any."""
        found = _rows(
            connection,
            SELECT_RUN + " WHERE status IN ('PENDING','RUNNING','CANCEL_REQUESTED')"
            " ORDER BY created_at ASC LIMIT 1",
            (), RUN_FIELDS, "backfill run")
        return found[0] if found else None

    def days(self, connection, run_id) -> list[dict]:
        return _rows(
            connection,
            f"SELECT {', '.join(DAY_FIELDS)} FROM public.backfill_run_days"
            " WHERE backfill_run_id = %s ORDER BY business_date",
            (run_id,), DAY_FIELDS, "backfill day")

    # -- the runner's own transitions ----------------------------------------

    def claim(self, connection, *, runner_version) -> str | None:
        try:
            row = connection.execute(
                CLAIM_RUN, {"runner_version": runner_version}).fetchone()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "backfill claim failed") from exc
        return str(row[0]) if row else None

    def heartbeat(self, connection, run_id, *, business_date=None,
                  lecture_id=None, lecture_label=None) -> None:
        connection.execute("""
            UPDATE public.backfill_runs
               SET heartbeat_at = now(), updated_at = now(),
                   current_business_date = coalesce(%(business_date)s, current_business_date),
                   current_lecture_id = %(lecture_id)s,
                   current_lecture_label = %(lecture_label)s
             WHERE backfill_run_id = %(run_id)s""",
            {"run_id": run_id, "business_date": business_date,
             "lecture_id": lecture_id, "lecture_label": lecture_label})

    def record_day(self, connection, run_id, *, business_date, status,
                   pipeline_run_id=None, counts=None, graph_calls=0,
                   provider_calls=0, error_code=None, error_message=None,
                   duration_ms=None) -> None:
        """
        Upsert the day summary. Upsert rather than insert because a resumed run
        re-processes the day it was interrupted on, and that second attempt is
        the one worth keeping.
        """
        values = dict(counts or {})
        connection.execute("""
            INSERT INTO public.backfill_run_days (
                backfill_run_id, business_date, status, pipeline_run_id,
                calendar_events_considered, matched_lectures, newly_discovered,
                already_complete, processed, waiting, review_required, failed,
                suppressed, graph_calls, provider_calls, error_code,
                error_message, started_at, finished_at, duration_ms)
            VALUES (%(run_id)s, %(business_date)s, %(status)s, %(pipeline_run_id)s,
                    %(calendar_events_considered)s, %(matched_lectures)s,
                    %(newly_discovered)s, %(already_complete)s, %(processed)s,
                    %(waiting)s, %(review_required)s, %(failed)s, %(suppressed)s,
                    %(graph_calls)s, %(provider_calls)s, %(error_code)s,
                    %(error_message)s, now(), now(), %(duration_ms)s)
            ON CONFLICT (backfill_run_id, business_date) DO UPDATE SET
                status = EXCLUDED.status,
                pipeline_run_id = EXCLUDED.pipeline_run_id,
                calendar_events_considered = EXCLUDED.calendar_events_considered,
                matched_lectures = EXCLUDED.matched_lectures,
                newly_discovered = EXCLUDED.newly_discovered,
                already_complete = EXCLUDED.already_complete,
                processed = EXCLUDED.processed,
                waiting = EXCLUDED.waiting,
                review_required = EXCLUDED.review_required,
                failed = EXCLUDED.failed,
                suppressed = EXCLUDED.suppressed,
                graph_calls = EXCLUDED.graph_calls,
                provider_calls = EXCLUDED.provider_calls,
                error_code = EXCLUDED.error_code,
                error_message = EXCLUDED.error_message,
                finished_at = now(),
                duration_ms = EXCLUDED.duration_ms""",
            {"run_id": run_id, "business_date": business_date, "status": status,
             "pipeline_run_id": pipeline_run_id, "graph_calls": graph_calls,
             "provider_calls": provider_calls, "error_code": error_code,
             "error_message": error_message, "duration_ms": duration_ms,
             **{key: int(values.get(key, 0)) for key in (
                 "calendar_events_considered", "matched_lectures",
                 "newly_discovered", "already_complete", "processed", "waiting",
                 "review_required", "failed", "suppressed")}})

    def advance(self, connection, run_id, *, next_business_date, counts) -> None:
        """
        One day finished. Move the checkpoint and add this day's counts.

        The checkpoint moves only AFTER the day's work is recorded, in the same
        transaction, so a crash between the two is impossible: either the day
        happened and the checkpoint moved, or neither did and the day is
        re-run - which existing stage resolution makes a no-op.
        """
        connection.execute("""
            UPDATE public.backfill_runs
               SET completed_days = completed_days + 1,
                   current_business_date = %(next_business_date)s,
                   discovered_count       = discovered_count + %(discovered)s,
                   matched_count          = matched_count + %(matched)s,
                   processed_count        = processed_count + %(processed)s,
                   already_complete_count = already_complete_count + %(already_complete)s,
                   waiting_count          = waiting_count + %(waiting)s,
                   review_required_count  = review_required_count + %(review_required)s,
                   failed_count           = failed_count + %(failed)s,
                   suppressed_count       = suppressed_count + %(suppressed)s,
                   current_lecture_id = NULL, current_lecture_label = NULL,
                   heartbeat_at = now(), updated_at = now()
             WHERE backfill_run_id = %(run_id)s""",
            {"run_id": run_id, "next_business_date": next_business_date,
             **{key: int((counts or {}).get(key, 0)) for key in (
                 "discovered", "matched", "processed", "already_complete",
                 "waiting", "review_required", "failed", "suppressed")}})

    def finish(self, connection, run_id, *, status, error_summary=None) -> None:
        connection.execute("""
            UPDATE public.backfill_runs
               SET status = %(status)s, finished_at = now(), updated_at = now(),
                   heartbeat_at = now(), error_summary = %(error_summary)s,
                   current_lecture_id = NULL, current_lecture_label = NULL,
                   current_business_date = CASE WHEN %(status)s = 'COMPLETED'
                                                THEN NULL
                                                ELSE current_business_date END
             WHERE backfill_run_id = %(run_id)s""",
            {"run_id": run_id, "status": status, "error_summary": error_summary})

    # -- cancellation --------------------------------------------------------

    def request_cancel(self, connection, run_id, *, requested_by) -> bool:
        """
        Cooperative. Sets a flag the runner checks at each safe boundary; it
        never interrupts work in flight. Returns False when the run has already
        finished, so the API can say so instead of pretending it cancelled one.
        """
        row = connection.execute("""
            UPDATE public.backfill_runs
               SET cancel_requested = true, cancel_requested_at = now(),
                   cancel_requested_by = %(requested_by)s,
                   status = CASE WHEN status = 'RUNNING' THEN 'CANCEL_REQUESTED'
                                 ELSE status END,
                   updated_at = now()
             WHERE backfill_run_id = %(run_id)s
               AND status IN ('PENDING', 'RUNNING')
            RETURNING backfill_run_id""",
            {"run_id": run_id, "requested_by": requested_by}).fetchone()
        return row is not None

    def cancel_requested(self, connection, run_id) -> bool:
        row = connection.execute(
            "SELECT cancel_requested FROM public.backfill_runs"
            " WHERE backfill_run_id = %s", (run_id,)).fetchone()
        return bool(row and row[0])
