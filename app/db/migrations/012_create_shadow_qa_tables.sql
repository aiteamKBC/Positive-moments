-- ===========================================================================
-- Migration 012 (Phase 3A): shadow QA engine.
--
-- Purely additive and SHADOW ONLY. Nothing here writes, mirrors or creates a
-- view over public.qa_doctors_sessions, public.qa_doctors_checklist_items,
-- public.qa_perfect_lectures or public.qa_doctors_transcripts; those stay
-- untouched legacy production tables that Phase 3C will cut over later.
--
-- The evaluation is traceable to every input that can change it: selection,
-- combined transcript, canonical document, attendance snapshot, engagement
-- result, prompt version and hash, model identity, structured schema version
-- and engine version. A material change creates a NEW evaluation rather than
-- overwriting the old one.
--
-- Deterministic Items 1, 2 and 7 are owned by code. Each checklist row keeps
-- both the model's answer and the final status, with the source recorded, so
-- an AI change can never silently rewrite a deterministic verdict.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.lecture_qa_evaluations (
    evaluation_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE CASCADE,
    document_id uuid
        REFERENCES public.lecture_transcript_documents (document_id) ON DELETE CASCADE,
    selection_id uuid
        REFERENCES public.lecture_transcript_selections (selection_id) ON DELETE CASCADE,
    attendance_snapshot_id uuid
        REFERENCES public.lecture_attendance_snapshots (snapshot_id) ON DELETE CASCADE,
    engagement_id uuid
        REFERENCES public.lecture_engagement_metrics (engagement_id) ON DELETE CASCADE,

    qa_engine_version text NOT NULL,
    prompt_version text NOT NULL,
    prompt_sha256 text NOT NULL,
    structured_schema_version text NOT NULL,
    model_provider text,
    model_name text,
    model_reported text,
    -- The roster rule this evaluation consumed. Pinned, never "latest".
    attendance_roster_version text NOT NULL,

    qa_status text NOT NULL,
    delivery_status text NOT NULL,
    ai_called boolean NOT NULL DEFAULT false,
    provider_attempts integer NOT NULL DEFAULT 0,
    error_code text,

    -- Deterministic session facts.
    primary_provider_transcript_id text,
    meeting_id text,
    duration_minutes integer,
    duration_score integer,
    duration_text text,
    start_difference_minutes integer,
    end_difference_minutes integer,
    cancelled_session boolean NOT NULL DEFAULT false,

    -- Identity: deterministic trainer wins; the model's guess is diagnostic.
    canonical_trainer text,
    canonical_trainer_speaker_id uuid
        REFERENCES public.lecture_transcript_speakers (speaker_id) ON DELETE SET NULL,
    ai_suggested_trainer text,
    trainer_source text,

    -- Engagement is consumed from Phase 2C4, never recomputed here.
    attended_count integer,
    spoke_count integer,
    engagement_percentage numeric(5, 2),
    engagement_score integer,
    ai_item7_status text,
    final_item7_status text,
    item7_override_applied boolean NOT NULL DEFAULT false,

    met_count integer,
    partial_count integer,
    not_met_count integer,
    teaching_quality_rating integer,
    teaching_quality_comments text,
    overall_judgement text,

    evidence_clip_count integer NOT NULL DEFAULT 0,
    invalid_evidence_clip_count integer NOT NULL DEFAULT 0,
    structured_output_error_count integer NOT NULL DEFAULT 0,
    review_reason text,

    source_fingerprint text NOT NULL,
    -- The model's structured answer, kept whole for audit. Never the
    -- transcript, never a token, never a header.
    ai_raw_output jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT lecture_qa_evaluations_key UNIQUE (source_fingerprint),
    CONSTRAINT lecture_qa_evaluations_status CHECK (qa_status IN (
        'PENDING', 'NON_DELIVERED', 'COMPLETED', 'MODEL_ERROR',
        'INVALID_STRUCTURED_OUTPUT', 'INVALID_EVIDENCE', 'REVIEW_REQUIRED')),
    CONSTRAINT lecture_qa_evaluations_delivery
        CHECK (delivery_status IN ('DELIVERED', 'NON_DELIVERED')),
    CONSTRAINT lecture_qa_evaluations_item7 CHECK (
        final_item7_status IS NULL
        OR final_item7_status IN ('Met', 'Partially Met', 'Not Met')),
    CONSTRAINT lecture_qa_evaluations_counts CHECK (
        evidence_clip_count >= 0 AND invalid_evidence_clip_count >= 0
        AND structured_output_error_count >= 0 AND provider_attempts >= 0),
    CONSTRAINT lecture_qa_evaluations_rating CHECK (
        teaching_quality_rating IS NULL OR teaching_quality_rating BETWEEN 1 AND 5),
    -- A non-delivered session must never have called the model.
    CONSTRAINT lecture_qa_evaluations_non_delivered CHECK (
        delivery_status <> 'NON_DELIVERED' OR (NOT ai_called AND cancelled_session)),
    CONSTRAINT lecture_qa_evaluations_fingerprint
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS lecture_qa_evaluations_lecture_idx
    ON public.lecture_qa_evaluations (lecture_id, created_at DESC);

COMMENT ON TABLE public.lecture_qa_evaluations IS
    'Shadow QA evaluations. Never written to the legacy qa_doctors_* tables; Phase 3C owns any cutover.';
COMMENT ON COLUMN public.lecture_qa_evaluations.canonical_trainer IS
    'Deterministic Phase 2C3 VTT trainer candidate. An AI-suggested trainer is stored separately and never overwrites this.';
COMMENT ON COLUMN public.lecture_qa_evaluations.final_item7_status IS
    'From the Phase 2C4 engagement result when item7_override_applied; otherwise the AI fallback, matching legacy.';


CREATE TABLE IF NOT EXISTS public.lecture_qa_checklist_items (
    checklist_row_id uuid PRIMARY KEY,
    evaluation_id uuid NOT NULL
        REFERENCES public.lecture_qa_evaluations (evaluation_id) ON DELETE CASCADE,
    checklist_order integer NOT NULL,
    checklist_item text NOT NULL,
    status text NOT NULL,
    -- What the model said, kept even when code overrode it.
    ai_status text,
    status_source text NOT NULL,
    reasoning text,
    evidence_text text,
    evidence_clip_count integer NOT NULL DEFAULT 0,
    invalid_evidence_clip_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT lecture_qa_checklist_items_key UNIQUE (evaluation_id, checklist_order),
    CONSTRAINT lecture_qa_checklist_items_order CHECK (checklist_order BETWEEN 1 AND 11),
    CONSTRAINT lecture_qa_checklist_items_status
        CHECK (status IN ('Met', 'Partially Met', 'Not Met')),
    CONSTRAINT lecture_qa_checklist_items_ai_status
        CHECK (ai_status IS NULL OR ai_status IN ('Met', 'Partially Met', 'Not Met'))
);

COMMENT ON TABLE public.lecture_qa_checklist_items IS
    'Eleven shadow checklist rows per evaluation, in legacy order with the legacy item strings. status_source records whether code or the model decided.';


CREATE TABLE IF NOT EXISTS public.lecture_qa_evidence_clips (
    clip_id uuid PRIMARY KEY,
    evaluation_id uuid NOT NULL
        REFERENCES public.lecture_qa_evaluations (evaluation_id) ON DELETE CASCADE,
    -- 'checklist', 'strengths', 'areas_for_improvement', 'ksb', 'teaching_quality'.
    clip_source text NOT NULL,
    source_position integer NOT NULL,
    clip_index integer NOT NULL,
    start_text text,
    end_text text,
    start_ms bigint,
    end_ms bigint,
    speaker_label text,
    validation_status text NOT NULL,
    overlapping_cue_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT lecture_qa_evidence_clips_key
        UNIQUE (evaluation_id, clip_source, source_position, clip_index),
    CONSTRAINT lecture_qa_evidence_clips_status CHECK (validation_status IN (
        'VALID', 'OUT_OF_TRANSCRIPT_RANGE', 'NO_CUE_OVERLAP',
        'SHORTER_THAN_MINIMUM', 'END_NOT_AFTER_START', 'UNPARSEABLE_TIMESTAMP'))
);

CREATE INDEX IF NOT EXISTS lecture_qa_evidence_clips_evaluation_idx
    ON public.lecture_qa_evidence_clips (evaluation_id, validation_status);

COMMENT ON TABLE public.lecture_qa_evidence_clips IS
    'Model-returned evidence time ranges validated against the canonical cue timeline. Invalid ranges are recorded, never silently repaired. Phase 3B renders evidence text from these.';


CREATE TABLE IF NOT EXISTS public.lecture_qa_runs (
    run_id uuid PRIMARY KEY,
    target_date date NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    mode text NOT NULL,
    status text NOT NULL,
    qa_engine_version text NOT NULL,
    prompt_version text NOT NULL,
    model_name text,

    lectures_considered integer NOT NULL DEFAULT 0,
    delivered_count integer NOT NULL DEFAULT 0,
    non_delivered_count integer NOT NULL DEFAULT 0,
    provider_calls integer NOT NULL DEFAULT 0,
    reused_evaluations integer NOT NULL DEFAULT 0,
    evaluations_created integer NOT NULL DEFAULT 0,
    evaluations_updated integer NOT NULL DEFAULT 0,
    review_required_count integer NOT NULL DEFAULT 0,
    error_count integer NOT NULL DEFAULT 0,

    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_qa_runs_mode CHECK (mode IN ('PREVIEW', 'SHADOW')),
    CONSTRAINT lecture_qa_runs_status CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS lecture_qa_runs_date_idx
    ON public.lecture_qa_runs (target_date, started_at DESC);

COMMENT ON TABLE public.lecture_qa_runs IS
    'Audit summaries for Phase 3A shadow QA runs, including provider call counts. No secrets and no transcript text.';

COMMIT;
