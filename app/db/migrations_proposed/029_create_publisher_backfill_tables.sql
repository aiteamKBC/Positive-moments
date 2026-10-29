-- ===========================================================================
-- PROPOSED migration 029: Historical Backfill runs for the Bunny publisher (2026-10-29).
--
-- Rollback: 029_create_publisher_backfill_tables.rollback.sql (refuses while runs exist). Requires 024-028.
--
-- Purely additive: two tables; nothing existing is altered.
--
--   bunny_backfill_runs    one operator-confirmed historical run over an INCLUSIVE range of
--                          Africa/Cairo business dates (lecture_sessions.session_date). At most
--                          ONE run is active at a time (partial unique index): two concurrent
--                          start requests cannot both succeed.
--   bunny_backfill_items   one row per lecture of the run, with the run's own view of it. The
--                          publishing work itself is the publisher's ordinary jobs and rows
--                          (bunny_publisher_jobs, bunny_videos, ...): a run never owns a video,
--                          a caption or a release, so two runs - or a run and the daily
--                          publisher - can never create a second one.
--
-- Codes, counts and identities only: a CHECK refuses URL-like text in every jsonb column, and
-- nothing here holds a Graph URL, a Bunny key or a file name.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.bunny_backfill_runs (
    run_id                    uuid PRIMARY KEY,
    date_from                 date NOT NULL,
    date_to                   date NOT NULL,
    library_id                integer NOT NULL,
    status                    text NOT NULL DEFAULT 'QUEUED',
    requested_by              text NOT NULL,
    reason                    text NOT NULL DEFAULT '',
    -- What the operator confirmed: the preview's fingerprint and its counts.
    preview_fingerprint       text NOT NULL,
    preview_summary           jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- The coordinator's progress markers.
    lms_matched_at            timestamptz,
    lms_status                text,
    last_tick_at              timestamptz,
    last_tick                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    cancel_requested_at       timestamptz,
    cancel_requested_by       text,
    created_at                timestamptz NOT NULL DEFAULT now(),
    started_at                timestamptz,
    finished_at               timestamptz,
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT bunny_backfill_runs_range CHECK (date_to >= date_from AND date_to - date_from <= 366),
    CONSTRAINT bunny_backfill_runs_status CHECK (status IN (
        'QUEUED', 'RUNNING', 'COMPLETED', 'CANCELLED', 'FAILED')),
    CONSTRAINT bunny_backfill_runs_finished CHECK (
        (status IN ('COMPLETED', 'CANCELLED', 'FAILED')) = (finished_at IS NOT NULL)),
    CONSTRAINT bunny_backfill_runs_actor CHECK (length(requested_by) BETWEEN 1 AND 150),
    CONSTRAINT bunny_backfill_runs_text CHECK (
        reason !~* '(https?://|tempauth|[?&]sig=)' AND length(reason) <= 300),
    CONSTRAINT bunny_backfill_runs_json CHECK (
        jsonb_typeof(preview_summary) = 'object' AND jsonb_typeof(last_tick) = 'object'
        AND preview_summary::text !~* '(https?://|tempauth|[?&]sig=)'
        AND last_tick::text !~* '(https?://|tempauth|[?&]sig=)')
);

-- One active historical run at a time, whatever its range.
CREATE UNIQUE INDEX IF NOT EXISTS bunny_backfill_runs_one_active
    ON public.bunny_backfill_runs ((true)) WHERE status IN ('QUEUED', 'RUNNING');
CREATE INDEX IF NOT EXISTS bunny_backfill_runs_created_idx ON public.bunny_backfill_runs (created_at DESC);

COMMENT ON TABLE public.bunny_backfill_runs IS
    'Operator-confirmed historical publishing runs (inclusive Cairo date range). One active at a time. No URLs.';


CREATE TABLE IF NOT EXISTS public.bunny_backfill_items (
    run_id                    uuid NOT NULL REFERENCES public.bunny_backfill_runs(run_id) ON DELETE RESTRICT,
    lecture_id                uuid NOT NULL REFERENCES public.lecture_sessions(lecture_id) ON DELETE RESTRICT,
    session_date              date NOT NULL,
    status                    text NOT NULL,
    -- The preview category the operator confirmed, and the coordinator's latest reason / stage.
    initial_category          text NOT NULL,
    reason                    text,
    stage                     text,
    retries                   integer NOT NULL DEFAULT 0,
    detail                    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                timestamptz NOT NULL DEFAULT now(),
    started_at                timestamptz,
    finished_at               timestamptz,
    updated_at                timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, lecture_id),
    CONSTRAINT bunny_backfill_items_status CHECK (status IN (
        'PENDING', 'PROCESSING', 'COMPLETED', 'SKIPPED', 'BLOCKED', 'FAILED', 'CANCELLED')),
    CONSTRAINT bunny_backfill_items_finished CHECK (
        (status IN ('PENDING', 'PROCESSING')) = (finished_at IS NULL)),
    CONSTRAINT bunny_backfill_items_codes CHECK (
        initial_category ~ '^[A-Z][A-Z0-9_]{2,63}$' AND (reason IS NULL OR length(reason) <= 200)
        AND (stage IS NULL OR stage ~ '^[A-Z][A-Z0-9_]{2,63}$')),
    CONSTRAINT bunny_backfill_items_detail CHECK (
        jsonb_typeof(detail) = 'object' AND detail::text !~* '(https?://|tempauth|[?&]sig=)'
        AND coalesce(reason, '') !~* '(https?://|tempauth|[?&]sig=)')
);

CREATE INDEX IF NOT EXISTS bunny_backfill_items_open_idx
    ON public.bunny_backfill_items (run_id, session_date) WHERE status IN ('PENDING', 'PROCESSING');
CREATE INDEX IF NOT EXISTS bunny_backfill_items_lecture_idx ON public.bunny_backfill_items (lecture_id);

COMMENT ON TABLE public.bunny_backfill_items IS
    'One lecture of a historical run. The publishing work is the publisher''s own jobs/rows; never a second copy.';

COMMIT;
