-- ===========================================================================
-- Migration 021: the coded RECORDING_LINK stage's durable state.
--
-- WHY THIS EXISTS
-- ---------------
-- Until now RECORDING_LINK was observed only: the recording branch of n8n
-- QA Master Daily v8 filled `qa_doctors_sessions.recording_url` and the
-- coded platform merely reported whether it had. The stage is now executable,
-- and an executable stage needs to remember ONE thing the legacy row cannot
-- hold: what the last evaluation concluded, and when it may be asked again.
--
-- Without it every scheduler cycle would re-query Graph for every lecture
-- whose recording is ambiguous, still uploading, or not reachable - forever.
-- With it: transient outcomes back off and are bounded, review outcomes stay
-- put until a human acts, and a written link is never re-evaluated.
--
-- WHAT IT DOES NOT HOLD
-- ---------------------
-- No recording URL, no token, no transcript text, no learner data. The URL
-- lives only where it always has, in the legacy row; `recording_url_written`
-- records that THIS stage put it there.
--
-- One row per lecture, overwritten by each evaluation. The per-cycle history
-- already lives in lecture_pipeline_run_items.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.lecture_recording_links (
    lecture_id                   uuid PRIMARY KEY
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE CASCADE,
    legacy_session_id            text,
    link_version                 text        NOT NULL,
    status                       text        NOT NULL,
    stage_state                  text        NOT NULL,
    reason                       text,
    graph_lookup_status          text,
    graph_http_status            integer,
    graph_recording_count        integer,
    candidate_file_count         integer,
    exact_candidate_count        integer,
    timestamp_difference_seconds numeric(10, 3),
    match_method                 text,
    recording_item_id            text,
    recording_drive_id           text,
    recording_filename           text,
    recording_url_written        boolean     NOT NULL DEFAULT false,
    attempt_count                integer     NOT NULL DEFAULT 0,
    first_attempted_at           timestamptz NOT NULL DEFAULT now(),
    last_attempted_at            timestamptz NOT NULL DEFAULT now(),
    next_attempt_after           timestamptz,
    written_at                   timestamptz,
    metadata                     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT lecture_recording_links_stage_state_check CHECK (stage_state IN (
        'COMPLETE', 'NOT_APPLICABLE', 'WAITING', 'REVIEW_REQUIRED', 'READY'))
);

CREATE INDEX IF NOT EXISTS lecture_recording_links_due_idx
    ON public.lecture_recording_links (stage_state, next_attempt_after);

COMMENT ON TABLE public.lecture_recording_links IS
    'Coded RECORDING_LINK stage state: last evaluation, bounded retry schedule, write provenance. No URLs or secrets.';

COMMIT;
