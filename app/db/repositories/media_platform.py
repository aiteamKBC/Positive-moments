"""
Persistence for the coded Positive Moments media platform (migration 023).

Concurrency model
-----------------
* A job is claimed with `FOR UPDATE SKIP LOCKED` plus a lease (`locked_at`),
  so two runners - or a runner restarted mid-job - never process the same job
  at once, and a crashed runner's lease expires instead of stranding it.
* Every state change is COMPARE-AND-SET (`WHERE status = <expected>`). A
  duplicate webhook, a late poll and a retry can race; only the first valid
  transition wins and the others become no-ops.
* A run is claimed the same way; `positive_moment_run_items` records each
  finished lecture, so a restarted runner resumes where it stopped.

Nothing here stores a temporary URL, token or API key.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from app.common.errors import DATABASE_ERROR, PlatformError

JOB_FIELDS = (
    "job_id", "moment_id", "lecture_id", "provider", "plan_fingerprint", "source_drive_id",
    "source_item_id", "source_file_name", "source_duration_seconds", "evidence_start_ms",
    "evidence_end_ms", "media_offset_seconds", "alignment_method", "alignment_confidence",
    "alignment_status", "alignment_detail", "requested_media_start_seconds",
    "requested_media_end_seconds", "actual_media_start_seconds", "actual_media_end_seconds",
    "padding_before_requested_seconds", "padding_after_requested_seconds",
    "padding_before_applied_seconds", "padding_after_applied_seconds", "render_policy",
    "estimated_credits", "status", "resume_stage", "attempt_count", "max_attempts",
    "locked_at", "locked_by", "next_retry_at", "next_poll_at", "provider_render_id",
    "provider_status", "submitted_at", "render_completed_at", "output_duration_seconds",
    "output_size_bytes", "error_stage", "error_code", "error_message", "created_at",
    "updated_at")
JOB_COLUMNS = ", ".join(JOB_FIELDS)

ASSET_FIELDS = ("asset_id", "job_id", "moment_id", "lecture_id", "provider",
                "plan_fingerprint", "source_drive_id", "source_item_id", "output_filename",
                "destination_drive_id", "destination_item_id", "web_url", "duration_seconds",
                "size_bytes", "verification_status", "created_at", "completed_at")
ASSET_COLUMNS = ", ".join(ASSET_FIELDS)

RUN_FIELDS = ("run_id", "requested_from", "requested_to", "lecture_id", "mode", "status",
              "total_lectures", "processed_lectures", "counts", "created_by",
              "runner_version", "locked_by", "heartbeat_at", "error_summary", "started_at",
              "completed_at", "created_at", "updated_at")
RUN_COLUMNS = ", ".join(RUN_FIELDS)

STATE_FIELDS = ("lecture_id", "session_date", "subject", "trainer", "legacy_session_id",
                "transcript_state", "transcript_document_id", "recording_state",
                "recording_status", "recording_reason", "recording_rule",
                "recording_file_name", "recording_duration_seconds", "recording_checked_at",
                "analysis_state", "analysis_id", "moment_count", "media_state",
                "media_counts", "completed_clip_count", "planned_clip_count",
                "planned_output_seconds", "estimated_credits", "needs_review",
                "last_run_id", "refreshed_at")

# Pre-render states: a replan may supersede these freely - no money spent,
# nothing in flight.
PRE_RENDER = ("PLANNED", "WAITING_FOR_RECORDING", "WAITING_FOR_ALIGNMENT",
              "REVIEW_REQUIRED_ALIGNMENT", "READY_TO_RENDER")
IN_FLIGHT = ("QUEUED", "RENDERING", "RENDER_SUCCEEDED", "UPLOAD_PENDING", "UPLOADING")
TERMINAL_RUN = ("COMPLETED", "FAILED", "CANCELLED")

JOB_LEASE_SECONDS = 15 * 60
RUN_STALE_SECONDS = 10 * 60

CLAIM_JOB = f"""
UPDATE public.positive_moment_media_jobs j
   SET locked_by = %(runner)s, locked_at = now(), updated_at = now()
 WHERE j.job_id = (
        SELECT job_id FROM public.positive_moment_media_jobs
         WHERE (status = 'QUEUED'
                OR (status = 'RENDERING' AND coalesce(next_poll_at, now()) <= now())
                OR status IN ('RENDER_SUCCEEDED', 'UPLOAD_PENDING')
                OR (status = 'UPLOADING' AND locked_at < now() - make_interval(secs => %(lease)s))
                OR (status = 'FAILED_RETRYABLE' AND next_retry_at IS NOT NULL
                    AND next_retry_at <= now() AND attempt_count < max_attempts))
           AND (locked_at IS NULL OR locked_at < now() - make_interval(secs => %(lease)s))
         ORDER BY updated_at, job_id
         LIMIT 1
         FOR UPDATE SKIP LOCKED)
RETURNING {', '.join('j.' + f for f in JOB_FIELDS)}
"""

CLAIM_RUN = f"""
UPDATE public.positive_moment_runs r
   SET status = 'RUNNING', locked_by = %(runner)s, heartbeat_at = now(),
       started_at = coalesce(r.started_at, now()), runner_version = %(version)s,
       updated_at = now()
 WHERE r.run_id = (
        SELECT run_id FROM public.positive_moment_runs
         WHERE status = 'PENDING'
            OR (status = 'RUNNING'
                AND coalesce(heartbeat_at, created_at) < now() - make_interval(secs => %(stale)s))
         ORDER BY created_at, run_id
         LIMIT 1
         FOR UPDATE SKIP LOCKED)
RETURNING {', '.join('r.' + f for f in RUN_FIELDS)}
"""


def _db(message):
    return PlatformError(DATABASE_ERROR, message)


def _one(connection, sql, params, fields):
    try:
        row = connection.execute(sql, params).fetchone()
    except Exception as exc:
        raise _db("media platform read failed") from exc
    return dict(zip(fields, row)) if row else None


def _all(connection, sql, params, fields):
    try:
        return [dict(zip(fields, row)) for row in connection.execute(sql, params).fetchall()]
    except Exception as exc:
        raise _db("media platform read failed") from exc


def _exec(connection, sql, params):
    try:
        return connection.execute(sql, params)
    except Exception as exc:
        raise _db("media platform write failed") from exc


class MediaPlatformRepository:

    # -- lectures ----------------------------------------------------------------

    def lectures_in_range(self, connection, date_from, date_to) -> list[str]:
        rows = _all(connection, """
            SELECT l.lecture_id FROM public.lecture_sessions l
             WHERE l.session_date BETWEEN %s AND %s
               AND l.metadata -> 'duplicate_suppression' IS NULL
             ORDER BY l.session_date, l.scheduled_start, l.lecture_id
        """, (date_from, date_to), ("lecture_id",))
        return [str(row["lecture_id"]) for row in rows]

    def lecture(self, connection, lecture_id) -> dict | None:
        return _one(connection, """
            SELECT lecture_id, session_date, subject, meeting_id, scheduled_start,
                   scheduled_end, is_cancelled
              FROM public.lecture_sessions WHERE lecture_id = %s
        """, (str(lecture_id),), ("lecture_id", "session_date", "subject", "meeting_id",
                                  "scheduled_start", "scheduled_end", "is_cancelled"))

    def legacy_recording(self, connection, legacy_session_id) -> dict | None:
        if not legacy_session_id:
            return None
        return _one(connection, """
            SELECT session_id, trainer, recording_url, recording_item_id,
                   recording_drive_id, recording_filename, recording_link_status,
                   recording_link_updated_at
              FROM public.qa_doctors_sessions WHERE session_id = %s
        """, (legacy_session_id,), ("session_id", "trainer", "recording_url",
                                    "recording_item_id", "recording_drive_id",
                                    "recording_filename", "recording_link_status",
                                    "recording_link_updated_at"))

    def trainer_for(self, connection, lecture) -> str | None:
        row = _one(connection, """
            SELECT q.trainer FROM public.qa_doctors_sessions q
             WHERE q.meeting_id = %s AND q.date = %s ORDER BY q.session_id LIMIT 1
        """, (lecture.get("meeting_id"), lecture.get("session_date")), ("trainer",))
        return row["trainer"] if row else None

    def coded_recording_link(self, connection, lecture_id) -> dict | None:
        return _one(connection, """
            SELECT status, stage_state, reason, recording_item_id, recording_drive_id,
                   recording_filename, metadata, last_attempted_at, next_attempt_after
              FROM public.lecture_recording_links WHERE lecture_id = %s
        """, (str(lecture_id),), ("status", "stage_state", "reason", "recording_item_id",
                                  "recording_drive_id", "recording_filename", "metadata",
                                  "last_attempted_at", "next_attempt_after"))

    def transcript_parts(self, connection, lecture_id) -> list[dict]:
        from app.media.recordings import SELECTED_PARTS
        return _all(connection, SELECTED_PARTS, (str(lecture_id),),
                    ("part_index", "part_offset_ms", "selection_id",
                     "content_correlation_id", "provider_created_at", "provider_end_at",
                     "meeting_id", "meeting_lookup_user_id"))

    # -- jobs ----------------------------------------------------------------------

    def job(self, connection, job_id, *, for_update=False) -> dict | None:
        return _one(connection, f"SELECT {JOB_COLUMNS} FROM public.positive_moment_media_jobs "
                                f"WHERE job_id = %s{' FOR UPDATE' if for_update else ''}",
                    (str(job_id),), JOB_FIELDS)

    def job_by_plan(self, connection, plan_fingerprint) -> dict | None:
        return _one(connection, f"SELECT {JOB_COLUMNS} FROM public.positive_moment_media_jobs "
                                "WHERE plan_fingerprint = %s", (plan_fingerprint,), JOB_FIELDS)

    def active_job_for_moment(self, connection, moment_id) -> dict | None:
        return _one(connection, f"""
            SELECT {JOB_COLUMNS} FROM public.positive_moment_media_jobs
             WHERE moment_id = %s AND status <> 'SUPERSEDED'
             ORDER BY created_at DESC, job_id LIMIT 1
        """, (str(moment_id),), JOB_FIELDS)

    def jobs_for_lecture(self, connection, lecture_id, *, include_superseded=False) -> list[dict]:
        return _all(connection, f"""
            SELECT {JOB_COLUMNS} FROM public.positive_moment_media_jobs
             WHERE lecture_id = %s {'' if include_superseded else "AND status <> 'SUPERSEDED'"}
             ORDER BY created_at, job_id
        """, (str(lecture_id),), JOB_FIELDS)

    def insert_job(self, connection, values: dict) -> dict:
        job_id = uuid.uuid4()
        params = {**{f: None for f in JOB_FIELDS}, **values, "job_id": job_id}
        for key in ("alignment_detail", "render_policy"):
            params[key] = Jsonb(params.get(key) or {})
        insertable = [f for f in JOB_FIELDS if f not in (
            "created_at", "updated_at", "attempt_count", "max_attempts")]
        _exec(connection, f"""
            INSERT INTO public.positive_moment_media_jobs ({', '.join(insertable)})
            VALUES ({', '.join(f'%({f})s' for f in insertable)})
            ON CONFLICT (plan_fingerprint) DO NOTHING
        """, params)
        return self.job_by_plan(connection, values["plan_fingerprint"])

    def transition(self, connection, job_id, *, expected, status, event_stage=None,
                   event_detail=None, **fields) -> bool:
        """Compare-and-set: move `expected` -> `status`. False if someone moved first."""
        expected = (expected,) if isinstance(expected, str) else tuple(expected)
        assignments = ["status = %(status)s", "updated_at = now()"]
        params = {"job_id": str(job_id), "expected": list(expected), "status": status}
        for key, value in fields.items():
            if key not in JOB_FIELDS:
                raise ValueError(f"unknown job field {key}")
            if key in ("alignment_detail", "render_policy"):
                value = Jsonb(value or {})
            assignments.append(f"{key} = %({key})s")
            params[key] = value
        cursor = _exec(connection, f"""
            UPDATE public.positive_moment_media_jobs SET {', '.join(assignments)}
             WHERE job_id = %(job_id)s AND status = ANY(%(expected)s)
        """, params)
        moved = cursor.rowcount == 1
        if moved:
            self.event(connection, job_id, event_stage or status, status, event_detail or {})
        return moved

    def release(self, connection, job_id) -> None:
        _exec(connection, "UPDATE public.positive_moment_media_jobs SET locked_at = NULL, "
                          "locked_by = NULL WHERE job_id = %s", (str(job_id),))

    def claim_job(self, connection, runner: str, lease_seconds=JOB_LEASE_SECONDS) -> dict | None:
        return _one(connection, CLAIM_JOB, {"runner": runner, "lease": lease_seconds}, JOB_FIELDS)

    def job_by_render(self, connection, provider, render_id) -> dict | None:
        return _one(connection, f"SELECT {JOB_COLUMNS} FROM public.positive_moment_media_jobs "
                                "WHERE provider = %s AND provider_render_id = %s",
                    (provider, str(render_id)), JOB_FIELDS)

    def event(self, connection, job_id, stage, status, detail=None) -> None:
        _exec(connection, """
            INSERT INTO public.positive_moment_media_events (job_id, stage, status, detail)
            VALUES (%s, %s, %s, %s)
        """, (str(job_id), stage, status, Jsonb(detail or {})))

    def events(self, connection, job_ids) -> list[dict]:
        ids = [str(value) for value in job_ids]
        if not ids:
            return []
        return _all(connection, """
            SELECT job_id, stage, status, detail, created_at
              FROM public.positive_moment_media_events
             WHERE job_id = ANY(%s::uuid[]) ORDER BY event_id
        """, (ids,), ("job_id", "stage", "status", "detail", "created_at"))

    # -- assets --------------------------------------------------------------------

    def asset_by_plan(self, connection, plan_fingerprint) -> dict | None:
        return _one(connection, f"SELECT {ASSET_COLUMNS} FROM public.positive_moment_media_assets "
                                "WHERE plan_fingerprint = %s", (plan_fingerprint,), ASSET_FIELDS)

    def asset(self, connection, asset_id) -> dict | None:
        return _one(connection, f"SELECT {ASSET_COLUMNS} FROM public.positive_moment_media_assets "
                                "WHERE asset_id = %s", (str(asset_id),), ASSET_FIELDS)

    def assets_for_lecture(self, connection, lecture_id) -> list[dict]:
        return _all(connection, f"SELECT {ASSET_COLUMNS} FROM public.positive_moment_media_assets "
                                "WHERE lecture_id = %s ORDER BY completed_at",
                    (str(lecture_id),), ASSET_FIELDS)

    def insert_asset(self, connection, values: dict) -> dict:
        _exec(connection, f"""
            INSERT INTO public.positive_moment_media_assets (
                asset_id, job_id, moment_id, lecture_id, provider, plan_fingerprint,
                source_drive_id, source_item_id, output_filename, destination_drive_id,
                destination_item_id, web_url, duration_seconds, size_bytes,
                verification_status)
            VALUES (%(asset_id)s, %(job_id)s, %(moment_id)s, %(lecture_id)s, %(provider)s,
                    %(plan_fingerprint)s, %(source_drive_id)s, %(source_item_id)s,
                    %(output_filename)s, %(destination_drive_id)s, %(destination_item_id)s,
                    %(web_url)s, %(duration_seconds)s, %(size_bytes)s,
                    %(verification_status)s)
            ON CONFLICT DO NOTHING
        """, {"asset_id": uuid.uuid4(), **values})
        return self.asset_by_plan(connection, values["plan_fingerprint"])

    # -- runs ------------------------------------------------------------------------

    def create_run(self, connection, *, requested_from, requested_to, mode, created_by,
                   lecture_id=None) -> dict:
        run_id = uuid.uuid4()
        _exec(connection, """
            INSERT INTO public.positive_moment_runs (run_id, requested_from, requested_to,
                lecture_id, mode, status, created_by)
            VALUES (%s, %s, %s, %s, %s, 'PENDING', %s)
        """, (run_id, requested_from, requested_to,
              str(lecture_id) if lecture_id else None, mode, created_by))
        return self.run(connection, run_id)

    def run(self, connection, run_id) -> dict | None:
        return _one(connection, f"SELECT {RUN_COLUMNS} FROM public.positive_moment_runs "
                                "WHERE run_id = %s", (str(run_id),), RUN_FIELDS)

    def recent_runs(self, connection, limit=20) -> list[dict]:
        return _all(connection, f"SELECT {RUN_COLUMNS} FROM public.positive_moment_runs "
                                "ORDER BY created_at DESC LIMIT %s", (limit,), RUN_FIELDS)

    def active_runs(self, connection) -> list[dict]:
        return _all(connection, f"SELECT {RUN_COLUMNS} FROM public.positive_moment_runs "
                                "WHERE status IN ('PENDING', 'RUNNING') ORDER BY created_at",
                    (), RUN_FIELDS)

    def claim_run(self, connection, runner, version, stale_seconds=RUN_STALE_SECONDS):
        return _one(connection, CLAIM_RUN, {"runner": runner, "version": version,
                                            "stale": stale_seconds}, RUN_FIELDS)

    def run_items_done(self, connection, run_id) -> set:
        rows = _all(connection, "SELECT lecture_id FROM public.positive_moment_run_items "
                                "WHERE run_id = %s AND status = 'DONE'", (str(run_id),),
                    ("lecture_id",))
        return {str(row["lecture_id"]) for row in rows}

    def record_run_item(self, connection, run_id, lecture_id, *, status, outcome, detail) -> None:
        _exec(connection, """
            INSERT INTO public.positive_moment_run_items (run_id, lecture_id, status, outcome,
                detail, completed_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (run_id, lecture_id) DO UPDATE SET status = EXCLUDED.status,
                outcome = EXCLUDED.outcome, detail = EXCLUDED.detail,
                completed_at = EXCLUDED.completed_at
        """, (str(run_id), str(lecture_id), status, outcome, Jsonb(detail or {})))

    def heartbeat_run(self, connection, run_id, *, runner, total=None, processed=None,
                      counts=None) -> None:
        _exec(connection, """
            UPDATE public.positive_moment_runs
               SET heartbeat_at = now(), updated_at = now(),
                   total_lectures = coalesce(%(total)s, total_lectures),
                   processed_lectures = coalesce(%(processed)s, processed_lectures),
                   counts = coalesce(%(counts)s, counts)
             WHERE run_id = %(run_id)s AND locked_by = %(runner)s
        """, {"run_id": str(run_id), "runner": runner, "total": total, "processed": processed,
              "counts": Jsonb(counts) if counts is not None else None})

    def finish_run(self, connection, run_id, *, status, error_summary=None) -> None:
        _exec(connection, """
            UPDATE public.positive_moment_runs
               SET status = %s, completed_at = now(), updated_at = now(),
                   error_summary = %s, locked_by = NULL
             WHERE run_id = %s AND status IN ('PENDING', 'RUNNING')
        """, (status, (error_summary or "")[:500] or None, str(run_id)))

    # -- lecture snapshot ---------------------------------------------------------------

    def save_state(self, connection, state: dict) -> None:
        params = {f: state.get(f) for f in STATE_FIELDS}
        params["media_counts"] = Jsonb(params.get("media_counts") or {})
        params["refreshed_at"] = datetime.now(timezone.utc)
        for key, default in (("moment_count", 0), ("completed_clip_count", 0),
                             ("planned_clip_count", 0), ("planned_output_seconds", 0),
                             ("estimated_credits", 0), ("needs_review", False)):
            if params.get(key) is None:
                params[key] = default
        columns = ", ".join(STATE_FIELDS)
        values = ", ".join(f"%({f})s" for f in STATE_FIELDS)
        updates = ", ".join(f"{f} = EXCLUDED.{f}" for f in STATE_FIELDS if f != "lecture_id")
        _exec(connection, f"""
            INSERT INTO public.positive_moment_lecture_states ({columns}) VALUES ({values})
            ON CONFLICT (lecture_id) DO UPDATE SET {updates}
        """, params)

    def states(self, connection, date_from, date_to) -> list[dict]:
        return _all(connection, f"""
            SELECT l.lecture_id, l.session_date, l.subject, l.scheduled_start,
                   {', '.join('s.' + f for f in STATE_FIELDS if f not in ('lecture_id', 'session_date', 'subject'))}
              FROM public.lecture_sessions l
              LEFT JOIN public.positive_moment_lecture_states s ON s.lecture_id = l.lecture_id
             WHERE l.session_date BETWEEN %s AND %s
               AND l.metadata -> 'duplicate_suppression' IS NULL
             ORDER BY l.session_date, l.scheduled_start, l.lecture_id
        """, (date_from, date_to),
            ("lecture_id", "session_date", "subject", "scheduled_start")
            + tuple(f for f in STATE_FIELDS if f not in ("lecture_id", "session_date", "subject")))

    def state(self, connection, lecture_id) -> dict | None:
        return _one(connection, f"SELECT {', '.join(STATE_FIELDS)} FROM "
                                "public.positive_moment_lecture_states WHERE lecture_id = %s",
                    (str(lecture_id),), STATE_FIELDS)

    def job_counts_by_lecture(self, connection, lecture_ids) -> dict:
        ids = [str(value) for value in lecture_ids]
        if not ids:
            return {}
        rows = _all(connection, """
            SELECT lecture_id, status, count(*), coalesce(sum(estimated_credits), 0),
                   coalesce(sum(actual_media_end_seconds - actual_media_start_seconds), 0)
              FROM public.positive_moment_media_jobs
             WHERE lecture_id = ANY(%s::uuid[]) AND status <> 'SUPERSEDED'
             GROUP BY lecture_id, status
        """, (ids,), ("lecture_id", "status", "count", "credits", "seconds"))
        found: dict = {}
        for row in rows:
            found.setdefault(str(row["lecture_id"]), {})[row["status"]] = {
                "count": int(row["count"]), "credits": float(row["credits"]),
                "seconds": float(row["seconds"])}
        return found


def retry_delay(attempt_count: int) -> timedelta:
    """5 min, 15 min, 45 min, then every 2 h."""
    minutes = min(5 * (3 ** max(attempt_count - 1, 0)), 120)
    return timedelta(minutes=minutes)
