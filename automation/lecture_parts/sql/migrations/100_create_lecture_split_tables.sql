-- ===========================================================================
-- Migration 100: lecture three-part splitting tables.
--
-- Entirely separate from the positive-clip pipeline. Nothing here touches
-- qa_positive_clip_assets, qa_media_jobs or qa_doctors_sessions.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.qa_lecture_split_plans (
    split_plan_id              bigserial PRIMARY KEY,
    split_plan_key             text NOT NULL UNIQUE,
    session_id                 text NOT NULL
        REFERENCES public.qa_doctors_sessions(session_id),
    split_version              text NOT NULL DEFAULT 'semantic_balanced_v1',
    media_origin               text,

    recording_duration_seconds numeric(12,3),

    cut_1_cue                  integer,
    cut_1_seconds              numeric(12,3),
    cut_1_reason               text,

    cut_2_cue                  integer,
    cut_2_seconds              numeric(12,3),
    cut_2_reason               text,

    part_1_title               text,
    part_1_summary             text,
    part_2_title               text,
    part_2_summary             text,
    part_3_title               text,
    part_3_summary             text,

    planner_status             text NOT NULL DEFAULT 'planned',
    planner_model              text,
    planner_confidence         numeric(4,3),
    planner_metadata           jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at                 timestamptz NOT NULL DEFAULT NOW(),
    updated_at                 timestamptz NOT NULL DEFAULT NOW(),

    CONSTRAINT qa_lecture_split_plans_session_version_key
        UNIQUE (session_id, split_version),

    CONSTRAINT qa_lecture_split_plans_status_check
        CHECK (planner_status IN ('planned', 'rejected', 'superseded')),

    CONSTRAINT qa_lecture_split_plans_origin_check
        CHECK (media_origin IS NULL OR media_origin IN ('history', 'live')),

    CONSTRAINT qa_lecture_split_plans_confidence_check
        CHECK (planner_confidence IS NULL OR planner_confidence BETWEEN 0 AND 1),

    -- A 'planned' row must be fully resolved and internally consistent.
    -- A 'rejected' row records why the AI output failed and needs no cuts.
    CONSTRAINT qa_lecture_split_plans_planned_complete_check CHECK (
        planner_status <> 'planned' OR (
            cut_1_cue IS NOT NULL AND cut_2_cue IS NOT NULL
            AND cut_1_seconds IS NOT NULL AND cut_2_seconds IS NOT NULL
            AND recording_duration_seconds IS NOT NULL
            AND cut_1_cue < cut_2_cue
            AND cut_1_seconds > 0
            AND cut_2_seconds > cut_1_seconds
            AND recording_duration_seconds > cut_2_seconds
        )
    )
);

CREATE INDEX IF NOT EXISTS qa_lecture_split_plans_session_idx
    ON public.qa_lecture_split_plans (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS qa_lecture_split_plans_status_idx
    ON public.qa_lecture_split_plans (planner_status);

CREATE TABLE IF NOT EXISTS public.qa_lecture_part_assets (
    part_asset_id         bigserial PRIMARY KEY,
    split_plan_id         bigint NOT NULL
        REFERENCES public.qa_lecture_split_plans(split_plan_id) ON DELETE CASCADE,
    session_id            text NOT NULL,
    split_version         text NOT NULL,
    part_number           integer NOT NULL,

    source_start_seconds  numeric(12,3) NOT NULL,
    source_end_seconds    numeric(12,3) NOT NULL,
    duration_seconds      numeric(12,3) NOT NULL,

    source_drive_id       text NOT NULL,
    source_item_id        text NOT NULL,

    output_filename       text,
    output_drive_id       text,
    output_item_id        text,
    output_web_url        text,
    output_size_bytes     bigint,

    status                text NOT NULL DEFAULT 'pending',
    error                 text,
    attempt_count         integer NOT NULL DEFAULT 0,

    created_at            timestamptz NOT NULL DEFAULT NOW(),
    updated_at            timestamptz NOT NULL DEFAULT NOW(),

    CONSTRAINT qa_lecture_part_assets_identity_key
        UNIQUE (session_id, split_version, part_number),

    CONSTRAINT qa_lecture_part_assets_part_number_check
        CHECK (part_number BETWEEN 1 AND 3),
    CONSTRAINT qa_lecture_part_assets_status_check
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    CONSTRAINT qa_lecture_part_assets_range_check
        CHECK (source_end_seconds > source_start_seconds AND source_start_seconds >= 0),
    CONSTRAINT qa_lecture_part_assets_attempt_check
        CHECK (attempt_count >= 0)
);

CREATE INDEX IF NOT EXISTS qa_lecture_part_assets_session_idx
    ON public.qa_lecture_part_assets (session_id, split_version, part_number);
CREATE INDEX IF NOT EXISTS qa_lecture_part_assets_status_idx
    ON public.qa_lecture_part_assets (status);

COMMENT ON TABLE public.qa_lecture_split_plans IS
    'AI-proposed, code-validated three-part split boundaries for a lecture. One row per (session_id, split_version).';
COMMENT ON TABLE public.qa_lecture_part_assets IS
    'Delivered lecture-part media. Identity is (session_id, split_version, part_number). Never mixed with qa_positive_clip_assets.';

COMMIT;
