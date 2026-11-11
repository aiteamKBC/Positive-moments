-- ===========================================================================
-- Migration 023: the coded Positive Moments media platform.
--
-- WHY NEW TABLES AND NOT qa_media_jobs / qa_positive_clip_assets
-- ---------------------------------------------------------------
-- Those two tables are owned by the n8n Positive Clips producer and its FFmpeg
-- worker contract (automation/positive_clips). Their identities are the legacy
-- `clip_key` / `job_key`, their status vocabulary is the worker's, and their
-- rows are written by n8n endpoints. Repurposing them would make one table
-- mean two things and let either writer corrupt the other's state. They are
-- read (never written) by this platform; everything coded lives here.
--
-- WHAT THIS HOLDS
-- ---------------
--   positive_moment_analyses      one evidence-intelligence analysis of one
--                                 canonical transcript document
--   positive_moments              the validated, verified moments it accepted
--   positive_moment_media_jobs    one render+delivery job per moment plan
--   positive_moment_media_events  the job's audit timeline
--   positive_moment_media_assets  the delivered SharePoint MP4 (durable)
--   positive_moment_runs          operator-requested batch runs (UI)
--   positive_moment_run_items     per-lecture progress of a run (resumable)
--   positive_moment_lecture_states  the dashboard's per-lecture snapshot
--
-- WHAT IT NEVER HOLDS
-- -------------------
-- No API key, no OAuth token, no temporary Graph download URL, no Creatomate
-- output URL, no chain-of-thought. The only URL stored is the durable
-- SharePoint webUrl of a delivered asset.
--
-- Additive and idempotent: CREATE ... IF NOT EXISTS throughout; nothing
-- existing is altered.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.positive_moment_analyses (
    analysis_id                 uuid PRIMARY KEY,
    lecture_id                  uuid NOT NULL
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE CASCADE,
    document_id                 uuid NOT NULL,
    document_source_fingerprint text NOT NULL,
    -- sha256 over the ordered (cue_index, start_ms, end_ms, speaker, text) of
    -- the document's cues: the content this analysis actually read.
    transcript_fingerprint      text NOT NULL,
    analysis_policy_version     text NOT NULL,
    -- sha256(policy, document, transcript fingerprint, source): identity.
    input_fingerprint           text NOT NULL,
    source                      text NOT NULL,
    status                      text NOT NULL,
    candidate_count             integer NOT NULL DEFAULT 0,
    structurally_valid_count    integer NOT NULL DEFAULT 0,
    accepted_count              integer NOT NULL DEFAULT 0,
    -- Counts by rejection code only. Never model reasoning.
    rejection_summary           jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- provider, model, prompt versions, token usage. No key, no prompt text.
    model_metadata              jsonb NOT NULL DEFAULT '{}'::jsonb,
    legacy_source_fingerprint   text,
    error_code                  text,
    error_message               text,
    started_at                  timestamptz NOT NULL DEFAULT now(),
    completed_at                timestamptz,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT positive_moment_analyses_identity UNIQUE (lecture_id, input_fingerprint),
    CONSTRAINT positive_moment_analyses_source_check
        CHECK (source IN ('coded_ai', 'legacy_v5_import')),
    CONSTRAINT positive_moment_analyses_status_check CHECK (status IN (
        'ANALYZING', 'NO_POSITIVE_MOMENTS', 'MOMENTS_FOUND', 'REVIEW_REQUIRED', 'FAILED'))
);
CREATE INDEX IF NOT EXISTS positive_moment_analyses_lecture_idx
    ON public.positive_moment_analyses (lecture_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.positive_moments (
    moment_id                   uuid PRIMARY KEY,
    analysis_id                 uuid NOT NULL
        REFERENCES public.positive_moment_analyses(analysis_id) ON DELETE CASCADE,
    lecture_id                  uuid NOT NULL
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE CASCADE,
    document_id                 uuid NOT NULL,
    transcript_fingerprint      text NOT NULL,
    analysis_policy_version     text NOT NULL,
    moment_fingerprint          text NOT NULL,
    moment_index                integer NOT NULL,
    start_cue                   integer NOT NULL,
    end_cue                     integer NOT NULL,
    evidence_start_ms           bigint NOT NULL,
    evidence_end_ms             bigint NOT NULL,
    all_speakers                jsonb NOT NULL DEFAULT '[]'::jsonb,
    positive_speakers           jsonb NOT NULL DEFAULT '[]'::jsonb,
    trainer_included            boolean NOT NULL DEFAULT false,
    conversation_type           text NOT NULL,
    category                    text NOT NULL,
    -- Rebuilt from lecture_transcript_cues, never model text.
    exact_quote                 text NOT NULL,
    positive_quote              text NOT NULL,
    dialogue                    jsonb NOT NULL DEFAULT '[]'::jsonb,
    reason                      text,
    selector_confidence         numeric(5,4),
    verifier_verdict            text NOT NULL,
    verifier_confidence         numeric(5,4),
    source                      text NOT NULL,
    status                      text NOT NULL DEFAULT 'ACCEPTED',
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT positive_moments_range UNIQUE (analysis_id, start_cue, end_cue),
    CONSTRAINT positive_moments_fingerprint UNIQUE (analysis_id, moment_fingerprint),
    CONSTRAINT positive_moments_cues_check CHECK (end_cue >= start_cue AND start_cue > 0),
    CONSTRAINT positive_moments_time_check CHECK (evidence_end_ms > evidence_start_ms),
    CONSTRAINT positive_moments_category_check CHECK (category IN (
        'trainer', 'teaching_method', 'content', 'support', 'learning_experience')),
    CONSTRAINT positive_moments_source_check
        CHECK (source IN ('coded_ai', 'legacy_v5_import'))
);
CREATE INDEX IF NOT EXISTS positive_moments_lecture_idx
    ON public.positive_moments (lecture_id, analysis_id, moment_index);

CREATE TABLE IF NOT EXISTS public.positive_moment_media_jobs (
    job_id                      uuid PRIMARY KEY,
    moment_id                   uuid NOT NULL
        REFERENCES public.positive_moments(moment_id) ON DELETE CASCADE,
    lecture_id                  uuid NOT NULL
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE CASCADE,
    provider                    text NOT NULL,
    -- sha256 over everything that decides the output bytes: moment
    -- fingerprint, source drive/item, alignment offset, final range, render
    -- policy. Same plan -> same job; a changed input -> a new job.
    plan_fingerprint            text NOT NULL,
    source_drive_id             text,
    source_item_id              text,
    source_file_name            text,
    source_duration_seconds     numeric(12,3),
    evidence_start_ms           bigint NOT NULL,
    evidence_end_ms             bigint NOT NULL,
    media_offset_seconds        numeric(12,3),
    alignment_method            text,
    alignment_confidence        numeric(5,4),
    alignment_status            text NOT NULL,
    alignment_detail            jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_media_start_seconds numeric(12,3),
    requested_media_end_seconds numeric(12,3),
    actual_media_start_seconds  numeric(12,3),
    actual_media_end_seconds    numeric(12,3),
    padding_before_requested_seconds numeric(8,3),
    padding_after_requested_seconds  numeric(8,3),
    padding_before_applied_seconds   numeric(8,3),
    padding_after_applied_seconds    numeric(8,3),
    render_policy               jsonb NOT NULL DEFAULT '{}'::jsonb,
    estimated_credits           numeric(12,3),
    status                      text NOT NULL,
    -- Where a FAILED_RETRYABLE job resumes: SUBMIT, POLL or UPLOAD.
    resume_stage                text,
    attempt_count               integer NOT NULL DEFAULT 0,
    max_attempts                integer NOT NULL DEFAULT 6,
    locked_at                   timestamptz,
    locked_by                   text,
    next_retry_at               timestamptz,
    next_poll_at                timestamptz,
    provider_render_id          text,
    provider_status             text,
    submitted_at                timestamptz,
    render_completed_at         timestamptz,
    output_duration_seconds     numeric(12,3),
    output_size_bytes           bigint,
    error_stage                 text,
    error_code                  text,
    error_message               text,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT positive_moment_media_jobs_plan UNIQUE (plan_fingerprint),
    CONSTRAINT positive_moment_media_jobs_status_check CHECK (status IN (
        'PLANNED', 'WAITING_FOR_RECORDING', 'WAITING_FOR_ALIGNMENT',
        'REVIEW_REQUIRED_ALIGNMENT', 'READY_TO_RENDER', 'QUEUED', 'RENDERING',
        'RENDER_SUCCEEDED', 'UPLOAD_PENDING', 'UPLOADING', 'COMPLETED',
        'FAILED_RETRYABLE', 'FAILED_FINAL', 'SUPERSEDED'))
);
CREATE UNIQUE INDEX IF NOT EXISTS positive_moment_media_jobs_render_idx
    ON public.positive_moment_media_jobs (provider, provider_render_id)
    WHERE provider_render_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS positive_moment_media_jobs_work_idx
    ON public.positive_moment_media_jobs (status, next_retry_at, next_poll_at);
CREATE INDEX IF NOT EXISTS positive_moment_media_jobs_lecture_idx
    ON public.positive_moment_media_jobs (lecture_id, moment_id);

CREATE TABLE IF NOT EXISTS public.positive_moment_media_events (
    event_id                    bigserial PRIMARY KEY,
    job_id                      uuid NOT NULL
        REFERENCES public.positive_moment_media_jobs(job_id) ON DELETE CASCADE,
    stage                       text NOT NULL,
    status                      text NOT NULL,
    detail                      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS positive_moment_media_events_job_idx
    ON public.positive_moment_media_events (job_id, event_id);

CREATE TABLE IF NOT EXISTS public.positive_moment_media_assets (
    asset_id                    uuid PRIMARY KEY,
    job_id                      uuid NOT NULL
        REFERENCES public.positive_moment_media_jobs(job_id) ON DELETE CASCADE,
    moment_id                   uuid NOT NULL
        REFERENCES public.positive_moments(moment_id) ON DELETE CASCADE,
    lecture_id                  uuid NOT NULL
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE CASCADE,
    provider                    text NOT NULL,
    plan_fingerprint            text NOT NULL,
    source_drive_id             text,
    source_item_id              text,
    output_filename             text NOT NULL,
    destination_drive_id        text NOT NULL,
    destination_item_id         text NOT NULL,
    web_url                     text NOT NULL,
    duration_seconds            numeric(12,3),
    size_bytes                  bigint,
    verification_status         text NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    completed_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT positive_moment_media_assets_job UNIQUE (job_id),
    CONSTRAINT positive_moment_media_assets_plan UNIQUE (plan_fingerprint),
    CONSTRAINT positive_moment_media_assets_item UNIQUE (destination_drive_id, destination_item_id),
    CONSTRAINT positive_moment_media_assets_url_check CHECK (web_url LIKE 'https://%')
);
CREATE INDEX IF NOT EXISTS positive_moment_media_assets_lecture_idx
    ON public.positive_moment_media_assets (lecture_id, moment_id);

CREATE TABLE IF NOT EXISTS public.positive_moment_runs (
    run_id                      uuid PRIMARY KEY,
    requested_from              date NOT NULL,
    requested_to                date NOT NULL,
    lecture_id                  uuid,
    mode                        text NOT NULL,
    status                      text NOT NULL,
    total_lectures              integer NOT NULL DEFAULT 0,
    processed_lectures          integer NOT NULL DEFAULT 0,
    counts                      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by                  text,
    runner_version              text,
    locked_by                   text,
    heartbeat_at                timestamptz,
    error_summary               text,
    started_at                  timestamptz,
    completed_at                timestamptz,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT positive_moment_runs_mode_check CHECK (mode IN (
        'PREVIEW', 'ANALYZE', 'RENDER', 'RETRY_FAILED')),
    CONSTRAINT positive_moment_runs_status_check CHECK (status IN (
        'PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    CONSTRAINT positive_moment_runs_range_check CHECK (requested_to >= requested_from)
);
CREATE INDEX IF NOT EXISTS positive_moment_runs_status_idx
    ON public.positive_moment_runs (status, created_at);

CREATE TABLE IF NOT EXISTS public.positive_moment_run_items (
    run_id                      uuid NOT NULL
        REFERENCES public.positive_moment_runs(run_id) ON DELETE CASCADE,
    lecture_id                  uuid NOT NULL,
    status                      text NOT NULL,
    outcome                     text,
    detail                      jsonb NOT NULL DEFAULT '{}'::jsonb,
    completed_at                timestamptz,
    PRIMARY KEY (run_id, lecture_id)
);

CREATE TABLE IF NOT EXISTS public.positive_moment_lecture_states (
    lecture_id                  uuid PRIMARY KEY
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE CASCADE,
    session_date                date NOT NULL,
    subject                     text,
    trainer                     text,
    legacy_session_id           text,
    transcript_state            text NOT NULL,
    transcript_document_id      uuid,
    recording_state             text NOT NULL,
    recording_status            text,
    recording_reason            text,
    recording_rule              text,
    recording_file_name         text,
    recording_duration_seconds  numeric(12,3),
    recording_checked_at        timestamptz,
    analysis_state              text NOT NULL,
    analysis_id                 uuid,
    moment_count                integer NOT NULL DEFAULT 0,
    media_state                 text NOT NULL,
    media_counts                jsonb NOT NULL DEFAULT '{}'::jsonb,
    completed_clip_count        integer NOT NULL DEFAULT 0,
    planned_clip_count          integer NOT NULL DEFAULT 0,
    planned_output_seconds      numeric(12,3) NOT NULL DEFAULT 0,
    estimated_credits           numeric(12,3) NOT NULL DEFAULT 0,
    needs_review                boolean NOT NULL DEFAULT false,
    last_run_id                 uuid,
    refreshed_at                timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS positive_moment_lecture_states_date_idx
    ON public.positive_moment_lecture_states (session_date);

COMMENT ON TABLE public.positive_moment_media_jobs IS
    'Coded Positive Moments render + SharePoint delivery jobs. Not the n8n qa_media_jobs queue. Stores no temporary URL, token or API key.';
COMMENT ON TABLE public.positive_moment_media_assets IS
    'Delivered Positive Moment clips: durable SharePoint DriveItem identity. The only URL stored is the SharePoint webUrl.';

COMMIT;
