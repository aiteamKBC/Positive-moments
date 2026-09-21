"""
Phase 6E: read the real media state for the console.

WHAT "REAL" MEANS HERE
----------------------
A clip is READY when the worker uploaded it and the asset row carries a durable
SharePoint URL - not when a plan exists, and not when a job was queued. The UI
must never report "ready" from the existence of an intention, so every status
in this module comes from `qa_positive_clip_assets.trim_status` or
`qa_media_jobs.status`, and the two are reported separately rather than merged
into one optimistic word.

WHAT IT REFUSES TO EXPOSE
-------------------------
No temporary Graph download URL, no upload-session URL, no token, no source
drive identifier. The only link that leaves this module is the durable
SharePoint `clip_url` / `output_web_url` the platform already persists.
"""
from app.common.errors import DATABASE_ERROR, PlatformError


# A delivered asset, joined to whatever job most recently described it. The
# job is a LEFT join because 17 of the existing assets predate the queue.
MOMENTS = """
SELECT c.ord::int                              AS clip_index,
       c.clip ->> 'start'                      AS source_start,
       c.clip ->> 'end'                        AS source_end,
       c.clip ->> 'speaker'                    AS speaker,
       c.clip ->> 'category'                   AS category,
       coalesce(c.clip ->> 'positive_quote',
                c.clip ->> 'quote')            AS quote,
       c.clip ->> 'reason'                     AS reason,
       a.trim_status                           AS clip_status,
       a.clip_url                              AS clip_url,
       a.duration_seconds                      AS clip_duration_seconds,
       j.status                                AS job_status,
       j.attempt_count                         AS job_attempts,
       j.error                                 AS job_error,
       j.updated_at                            AS job_updated_at
  FROM public.qa_doctors_sessions d
  CROSS JOIN LATERAL jsonb_array_elements(d.positive_clips)
       WITH ORDINALITY AS c(clip, ord)
  LEFT JOIN public.qa_positive_clip_assets a
         ON a.session_id = d.session_id
        AND a.source_start = c.clip ->> 'start'
        AND a.source_end   = c.clip ->> 'end'
  LEFT JOIN LATERAL (
        SELECT mj.status, mj.attempt_count, mj.error, mj.updated_at
          FROM public.qa_media_jobs mj
         WHERE mj.session_id = d.session_id
           AND mj.job_type = 'positive_clip'
           AND mj.clip_key = d.session_id || ':'
               || (c.clip ->> 'start_cue') || ':' || (c.clip ->> 'end_cue')
         ORDER BY mj.job_id DESC LIMIT 1) j ON TRUE
 WHERE d.session_id = %s
   AND jsonb_typeof(d.positive_clips) = 'array'
 ORDER BY c.ord
"""

MOMENT_FIELDS = ("clip_index", "source_start", "source_end", "speaker",
                 "category", "quote", "reason", "clip_status", "clip_url",
                 "clip_duration_seconds", "job_status", "job_attempts",
                 "job_error", "job_updated_at")

PARTS = """
SELECT j.part_number,
       j.status,
       j.attempt_count,
       j.error,
       j.start_seconds,
       j.end_seconds,
       j.output_web_url,
       j.output_size_bytes,
       j.completed_at,
       j.metadata ->> 'part_title'               AS part_title,
       j.metadata ->> 'canonical_start_seconds'  AS canonical_start_seconds,
       j.metadata ->> 'canonical_end_seconds'    AS canonical_end_seconds,
       j.metadata ->> 'split_version'            AS split_version,
       j.metadata ->> 'media_coordinate_version' AS media_coordinate_version
  FROM public.qa_media_jobs j
 WHERE j.session_id = %s AND j.job_type = 'lecture_part'
 ORDER BY j.part_number
"""

PART_FIELDS = ("part_number", "status", "attempt_count", "error",
               "media_start_seconds", "media_end_seconds", "output_web_url",
               "output_size_bytes", "completed_at", "part_title",
               "canonical_start_seconds", "canonical_end_seconds",
               "split_version", "media_coordinate_version")

SPLIT_PLAN = """
SELECT p.split_version, p.planner_status, p.planner_model, p.planner_confidence,
       p.cut_1_seconds, p.cut_2_seconds,
       p.part_1_title, p.part_2_title, p.part_3_title
  FROM public.qa_lecture_split_plans p
 WHERE p.session_id = %s
 ORDER BY p.split_plan_id DESC
 LIMIT 1
"""

SPLIT_FIELDS = ("split_version", "planner_status", "planner_model",
                "planner_confidence", "cut_1_seconds", "cut_2_seconds",
                "part_1_title", "part_2_title", "part_3_title")

# The operations queue view. Deliberately no source identifiers and no URLs
# other than the durable output one.
JOBS = """
SELECT j.job_id, j.job_type, j.part_number, j.status, j.attempt_count,
       j.max_attempts, j.cut_mode, j.output_filename, j.output_web_url,
       j.output_size_bytes, j.created_at, j.started_at, j.completed_at,
       j.not_before, j.error,
       j.metadata ->> 'subject'      AS subject,
       j.metadata ->> 'trainer'      AS trainer,
       j.metadata ->> 'lecture_date' AS lecture_date,
       round(j.end_seconds - j.start_seconds, 3) AS requested_duration_seconds
  FROM public.qa_media_jobs j
 WHERE (%(status)s IS NULL OR j.status = %(status)s)
 ORDER BY CASE j.status WHEN 'processing' THEN 0 WHEN 'failed' THEN 1
                        WHEN 'pending' THEN 2 ELSE 3 END,
          j.job_id DESC
 LIMIT %(limit)s
"""

JOB_FIELDS = ("job_id", "job_type", "part_number", "status", "attempt_count",
              "max_attempts", "cut_mode", "output_filename", "output_web_url",
              "output_size_bytes", "created_at", "started_at", "completed_at",
              "not_before", "error", "subject", "trainer", "lecture_date",
              "requested_duration_seconds")

JOB_COUNTS = """
SELECT j.status, j.job_type, count(*)::int
  FROM public.qa_media_jobs j
 GROUP BY 1, 2
"""


def _rows(connection, statement, params, fields, what):
    try:
        rows = connection.execute(statement, params).fetchall()
    except Exception as exc:
        raise PlatformError(DATABASE_ERROR, f"{what} read failed") from exc
    return [dict(zip(fields, row)) for row in rows]


class MediaStateRepository:
    """Read-only. Every value here is worker or asset truth, never a plan."""

    def moments(self, connection, session_id: str) -> list[dict]:
        return _rows(connection, MOMENTS, (session_id,), MOMENT_FIELDS,
                     "positive moment")

    def parts(self, connection, session_id: str) -> list[dict]:
        return _rows(connection, PARTS, (session_id,), PART_FIELDS,
                     "lecture part")

    def split_plan(self, connection, session_id: str) -> dict | None:
        found = _rows(connection, SPLIT_PLAN, (session_id,), SPLIT_FIELDS,
                      "split plan")
        return found[0] if found else None

    def jobs(self, connection, *, status: str | None = None,
             limit: int = 100) -> list[dict]:
        return _rows(connection, JOBS, {"status": status, "limit": limit},
                     JOB_FIELDS, "media job")

    def job_counts(self, connection) -> dict:
        try:
            rows = connection.execute(JOB_COUNTS).fetchall()
        except Exception as exc:
            raise PlatformError(DATABASE_ERROR, "media job count failed") from exc
        counts: dict = {"by_status": {}, "by_type": {}}
        for status, job_type, total in rows:
            counts["by_status"][status] = counts["by_status"].get(status, 0) + total
            counts["by_type"].setdefault(job_type, {})[status] = total
        return counts
