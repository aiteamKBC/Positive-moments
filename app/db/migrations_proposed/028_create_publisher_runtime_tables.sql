-- ===========================================================================
-- PROPOSED migration 028: Continuous Bunny Publisher runtime (P0-D).
--
-- STATUS: PROPOSED, NOT APPROVED FOR PRODUCTION (see 024's header). Rollback:
-- 028_create_publisher_runtime_tables.rollback.sql. Requires 024-027.
--
-- Purely additive: four tables and two views; nothing existing is altered.
--
--   bunny_publisher_part_decisions      an operator's explicit INCLUDE / EXCLUDE of
--                                 one recording part (the fragment policy never
--                                 drops a part on its own)
--   bunny_publisher_schedule            when each periodic task last ran, so a daemon
--                                 restarted after a day offline knows what it missed
--   bunny_publisher_alerts              alert OUTBOX: one open alert per dedupe key;
--                                 delivery is not enabled in this phase
--   bunny_publisher_reconciliation_runs one row per LIGHT (6-hourly) / FULL (05:00 Cairo) run
--
-- No URLs anywhere: a CHECK refuses URL-like text in every jsonb column.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.bunny_publisher_part_decisions (
    lecture_id                uuid NOT NULL
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE RESTRICT,
    recording_part_index      integer NOT NULL,
    decision                  text NOT NULL,
    reason                    text NOT NULL,
    actor                     text NOT NULL,
    decided_at                timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lecture_id, recording_part_index),
    CONSTRAINT bunny_publisher_part_decisions_decision CHECK (decision IN ('INCLUDE', 'EXCLUDE')),
    CONSTRAINT bunny_publisher_part_decisions_part CHECK (recording_part_index >= 1),
    CONSTRAINT bunny_publisher_part_decisions_text CHECK (
        length(reason) BETWEEN 1 AND 500 AND length(actor) BETWEEN 1 AND 120
        AND reason !~* '(https?://|tempauth|[?&]sig=)')
);


CREATE TABLE IF NOT EXISTS public.bunny_publisher_schedule (
    task                      text PRIMARY KEY,
    last_started_at           timestamptz,
    last_finished_at          timestamptz,
    last_succeeded_at         timestamptz,
    last_status               text,
    last_detail               jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT bunny_publisher_schedule_task CHECK (task ~ '^[A-Z][A-Z0-9_]{2,63}$'),
    CONSTRAINT bunny_publisher_schedule_status CHECK (
        last_status IS NULL OR last_status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED')),
    CONSTRAINT bunny_publisher_schedule_detail CHECK (
        jsonb_typeof(last_detail) = 'object' AND last_detail::text !~* '(https?://|tempauth|[?&]sig=)')
);


CREATE TABLE IF NOT EXISTS public.bunny_publisher_alerts (
    alert_id                  uuid PRIMARY KEY,
    severity                  text NOT NULL,
    kind                      text NOT NULL,
    dedupe_key                text NOT NULL,
    lecture_id                uuid REFERENCES public.lecture_sessions(lecture_id) ON DELETE RESTRICT,
    summary                   text NOT NULL,
    detail                    jsonb NOT NULL DEFAULT '{}'::jsonb,
    status                    text NOT NULL DEFAULT 'OPEN',
    notification_status           text NOT NULL DEFAULT 'NOT_SENT',
    occurrences               integer NOT NULL DEFAULT 1,
    first_seen_at             timestamptz NOT NULL DEFAULT now(),
    last_seen_at              timestamptz NOT NULL DEFAULT now(),
    resolved_at               timestamptz,
    CONSTRAINT bunny_publisher_alerts_severity CHECK (severity IN ('CRITICAL', 'WARNING', 'DIGEST')),
    CONSTRAINT bunny_publisher_alerts_kind CHECK (kind ~ '^[A-Z][A-Z0-9_]{2,63}$'),
    CONSTRAINT bunny_publisher_alerts_status CHECK (status IN ('OPEN', 'RESOLVED')),
    CONSTRAINT bunny_publisher_alerts_resolved CHECK ((status = 'RESOLVED') = (resolved_at IS NOT NULL)),
    -- DISABLED: a destination is configured but delivery is switched off (this phase).
    CONSTRAINT bunny_publisher_alerts_notification CHECK (notification_status IN ('NOT_SENT', 'DISABLED', 'SENT', 'FAILED')),
    CONSTRAINT bunny_publisher_alerts_occurrences CHECK (occurrences >= 1),
    CONSTRAINT bunny_publisher_alerts_no_url CHECK (
        jsonb_typeof(detail) = 'object' AND detail::text !~* '(https?://|tempauth|[?&]sig=)'
        AND summary !~* '(https?://|tempauth|[?&]sig=)')
);
CREATE UNIQUE INDEX IF NOT EXISTS bunny_publisher_alerts_one_open
    ON public.bunny_publisher_alerts (dedupe_key) WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS bunny_publisher_alerts_open_idx
    ON public.bunny_publisher_alerts (severity, last_seen_at) WHERE status = 'OPEN';


CREATE TABLE IF NOT EXISTS public.bunny_publisher_reconciliation_runs (
    run_id                    uuid PRIMARY KEY,
    kind                      text NOT NULL,
    started_at                timestamptz NOT NULL DEFAULT now(),
    finished_at               timestamptz,
    status                    text NOT NULL DEFAULT 'RUNNING',
    summary                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT bunny_publisher_reconciliation_runs_kind CHECK (kind IN ('LIGHT', 'FULL')),
    CONSTRAINT bunny_publisher_reconciliation_runs_status CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT bunny_publisher_reconciliation_runs_finished CHECK ((status = 'RUNNING') = (finished_at IS NULL)),
    CONSTRAINT bunny_publisher_reconciliation_runs_summary CHECK (
        jsonb_typeof(summary) = 'object' AND summary::text !~* '(https?://|tempauth|[?&]sig=)')
);


-- One row per lecture: where each independent track stands. Readiness (every
-- included part has a VERIFIED video and a VERIFIED current caption) is
-- computed per library in app/publisher/progress.py, which reads this view.
CREATE OR REPLACE VIEW public.bunny_publisher_lecture_coverage_v AS
SELECT l.lecture_id, l.session_date, l.scheduled_start, l.scheduled_end, l.subject,
       l.is_cancelled, l.metadata -> 'duplicate_suppression' IS NOT NULL AS duplicate_suppressed,
       COALESCE(m.match_status, 'PENDING') AS lms_status,
       (SELECT k.lms_occurrence_id FROM public.bunny_lms_occurrence_links k
         WHERE k.lecture_id = l.lecture_id AND k.is_current AND k.link_role = 'PRIMARY') AS lms_occurrence_id,
       COALESCE((SELECT s.selection_status FROM public.lecture_transcript_selections s
                  WHERE s.lecture_id = l.lecture_id ORDER BY s.updated_at DESC LIMIT 1), 'PENDING')
           AS transcript_selection_status,
       COALESCE(d.status, 'PENDING') AS recording_status,
       d.reason_code AS recording_reason, d.expected_part_count, d.located_part_count
  FROM public.lecture_sessions l
  LEFT JOIN public.bunny_lms_match_evaluations m ON m.lecture_id = l.lecture_id AND m.is_current
  LEFT JOIN public.bunny_recording_discoveries d ON d.lecture_id = l.lecture_id;


-- The exception queue: everything that needs a human, from every track.
CREATE OR REPLACE VIEW public.bunny_publisher_exceptions_v AS
SELECT 'JOB' AS source, j.lecture_id, j.job_kind AS item, j.status, j.last_error_code AS reason,
       j.updated_at AS at, j.job_id::text AS reference
  FROM public.bunny_publisher_jobs j WHERE j.status IN ('REVIEW_REQUIRED', 'FAILED')
UNION ALL
SELECT 'RECORDING', d.lecture_id, 'RECORDING', d.status, d.review_reason, d.last_checked_at, NULL
  FROM public.bunny_recording_discoveries d WHERE d.status IN ('REVIEW_REQUIRED', 'PERMANENT_FAILURE')
UNION ALL
SELECT 'LMS', e.lecture_id, 'LMS_MATCH', e.match_status, e.match_status, e.last_evaluated_at, NULL
  FROM public.bunny_lms_match_evaluations e WHERE e.is_current AND e.requires_review
UNION ALL
SELECT 'VIDEO', v.lecture_id, 'PART_' || v.recording_part_index, v.import_status || '/' || v.encoding_status,
       v.last_error_code, v.updated_at, v.bunny_video_record_id::text
  FROM public.bunny_videos v
 WHERE v.retired_at IS NULL AND (v.import_status IN ('FAILED', 'REVIEW_REQUIRED')
                                 OR v.encoding_status IN ('FAILED', 'REVIEW_REQUIRED'))
UNION ALL
SELECT 'CAPTION', c.lecture_id, c.language_code, c.publication_status, c.last_error_code, c.updated_at,
       c.publication_id::text
  FROM public.bunny_caption_publications c
 WHERE c.publication_status IN ('REJECTED', 'FAILED', 'REVIEW_REQUIRED')
UNION ALL
SELECT 'ALERT', a.lecture_id, a.kind, a.severity, a.summary, a.last_seen_at, a.alert_id::text
  FROM public.bunny_publisher_alerts a WHERE a.status = 'OPEN' AND a.severity <> 'DIGEST';

COMMIT;
