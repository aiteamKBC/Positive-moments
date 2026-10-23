-- ===========================================================================
-- PROPOSED migration 026: Continuous Bunny Publisher queue, events, heartbeats.
--
-- STATUS: PROPOSED, NOT APPROVED FOR PRODUCTION (see 024's header). Rollback:
-- 026_create_publisher_queue_tables.rollback.sql. Independent of 024/025.
--
-- Purely additive: three tables; nothing existing is altered. Design: audit
-- section 11 ("bunny_publisher_jobs", "bunny_publisher_events", "bunny_publisher_heartbeats")
-- and the claim pattern already proven in positive_moment_media_jobs (023):
-- FOR UPDATE SKIP LOCKED + a lease, compare-and-set transitions.
--
-- One LIVE job per (job_kind, subject_key) is a partial unique index, so a
-- planner that enqueues the same work twice - or two planners racing - can
-- never create two live jobs for one thing.
--
-- Payloads and event details are identity only (lecture ids, drive/item ids,
-- hashes). A CHECK refuses URL-like text: a temporary download URL lives only
-- in an import job's stack frame (audit section 14).
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.bunny_publisher_jobs (
    job_id                    uuid PRIMARY KEY,
    job_kind                  text NOT NULL,
    -- What the job is about, e.g. 'lecture:<uuid>' or 'lecture:<uuid>:part:2'.
    subject_key               text NOT NULL,
    lecture_id                uuid
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE RESTRICT,
    priority                  smallint NOT NULL DEFAULT 100,
    status                    text NOT NULL DEFAULT 'QUEUED',
    payload                   jsonb NOT NULL DEFAULT '{}'::jsonb,

    attempt_count             integer NOT NULL DEFAULT 0,
    max_attempts              integer NOT NULL DEFAULT 8,
    next_attempt_at           timestamptz,
    locked_by                 text,
    locked_at                 timestamptz,
    last_error_code           text,
    last_error                text,

    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    finished_at               timestamptz,

    CONSTRAINT bunny_publisher_jobs_kind CHECK (job_kind IN (
        'MATCH_LMS', 'RECONCILE_LMS_COVERAGE', 'ACQUIRE_TRANSCRIPT', 'RESOLVE_RECORDING',
        'IMPORT_VIDEO', 'POLL_ENCODING', 'PUBLISH_CAPTION', 'RECONCILE_BUNNY')),
    CONSTRAINT bunny_publisher_jobs_status CHECK (status IN (
        'QUEUED', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED', 'REVIEW_REQUIRED', 'CANCELLED')),
    CONSTRAINT bunny_publisher_jobs_subject CHECK (length(subject_key) BETWEEN 1 AND 200),
    CONSTRAINT bunny_publisher_jobs_priority CHECK (priority BETWEEN 0 AND 1000),
    CONSTRAINT bunny_publisher_jobs_attempts CHECK (attempt_count >= 0 AND max_attempts BETWEEN 1 AND 100),
    -- A running job is leased; a waiting job has a time; a finished job is finished.
    CONSTRAINT bunny_publisher_jobs_running_locked CHECK (
        (status = 'RUNNING') = (locked_by IS NOT NULL AND locked_at IS NOT NULL)),
    CONSTRAINT bunny_publisher_jobs_retry_time CHECK (status <> 'RETRY_WAIT' OR next_attempt_at IS NOT NULL),
    CONSTRAINT bunny_publisher_jobs_finished CHECK (
        (status IN ('SUCCEEDED', 'FAILED', 'REVIEW_REQUIRED', 'CANCELLED')) = (finished_at IS NOT NULL)),
    CONSTRAINT bunny_publisher_jobs_payload CHECK (
        jsonb_typeof(payload) = 'object' AND payload::text !~* '(https?://|tempauth|[?&]sig=)')
);

-- One live job per thing.
CREATE UNIQUE INDEX IF NOT EXISTS bunny_publisher_jobs_one_live
    ON public.bunny_publisher_jobs (job_kind, subject_key)
    WHERE status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT');
-- The claim scan: due work, best priority first.
CREATE INDEX IF NOT EXISTS bunny_publisher_jobs_due_idx
    ON public.bunny_publisher_jobs (status, priority DESC, next_attempt_at)
    WHERE status IN ('QUEUED', 'RUNNING', 'RETRY_WAIT');
CREATE INDEX IF NOT EXISTS bunny_publisher_jobs_lecture_idx
    ON public.bunny_publisher_jobs (lecture_id, job_kind) WHERE lecture_id IS NOT NULL;

COMMENT ON TABLE public.bunny_publisher_jobs IS
    'Durable publisher queue. One live job per (job_kind, subject_key); claimed with FOR UPDATE SKIP LOCKED + lease. No URLs in payload.';


CREATE TABLE IF NOT EXISTS public.bunny_publisher_events (
    event_id                  bigserial PRIMARY KEY,
    job_id                    uuid REFERENCES public.bunny_publisher_jobs(job_id) ON DELETE RESTRICT,
    lecture_id                uuid REFERENCES public.lecture_sessions(lecture_id) ON DELETE RESTRICT,
    event                     text NOT NULL,
    actor                     text NOT NULL DEFAULT 'publisher',
    detail                    jsonb NOT NULL DEFAULT '{}'::jsonb,
    at                        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bunny_publisher_events_event CHECK (event ~ '^[A-Z][A-Z0-9_]{2,63}$'),
    CONSTRAINT bunny_publisher_events_detail CHECK (
        jsonb_typeof(detail) = 'object' AND detail::text !~* '(https?://|tempauth|[?&]sig=)')
);

CREATE INDEX IF NOT EXISTS bunny_publisher_events_job_idx ON public.bunny_publisher_events (job_id, event_id);
CREATE INDEX IF NOT EXISTS bunny_publisher_events_lecture_idx ON public.bunny_publisher_events (lecture_id, event_id);

COMMENT ON TABLE public.bunny_publisher_events IS
    'Append-only publisher audit trail (operator decisions included). No URLs, keys or transcript text.';


CREATE TABLE IF NOT EXISTS public.bunny_publisher_heartbeats (
    worker_id                 text PRIMARY KEY,
    started_at                timestamptz NOT NULL DEFAULT now(),
    seen_at                   timestamptz NOT NULL DEFAULT now(),
    version                   text NOT NULL,
    current_job_id            uuid,
    detail                    jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT bunny_publisher_heartbeats_worker CHECK (length(worker_id) BETWEEN 1 AND 120),
    CONSTRAINT bunny_publisher_heartbeats_seen CHECK (seen_at >= started_at),
    CONSTRAINT bunny_publisher_heartbeats_detail CHECK (
        jsonb_typeof(detail) = 'object' AND detail::text !~* '(https?://|tempauth|[?&]sig=)')
);

COMMENT ON TABLE public.bunny_publisher_heartbeats IS
    'Liveness per publisher worker; the reconciler alerts when seen_at is older than 5 minutes.';

COMMIT;
