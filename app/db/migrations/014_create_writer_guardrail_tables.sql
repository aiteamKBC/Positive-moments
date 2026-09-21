-- ===========================================================================
-- Migration 014 (Phase 3C1): legacy writer ownership/audit, and bounded
-- model-generation attempts.
--
-- Purely additive, and both tables belong to the CODED PLATFORM. No column,
-- index, trigger or constraint is added to public.qa_doctors_sessions or
-- public.qa_doctors_checklist_items: ownership is tracked here, never by
-- marking legacy production rows.
--
-- This migration does not write a single legacy row. Phase 3C1 is dry-run
-- only; a write-enabled mode is a later, explicitly authorised step.
-- ===========================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Which legacy rows the coded writer created, and with what source.
--
-- The existence of a row here for a legacy session_id is the ONLY evidence
-- that the coded writer owns that target. Anything else in the legacy tables
-- is history written by n8n and is protected.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_qa_legacy_writes (
    write_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE CASCADE,
    evaluation_id uuid NOT NULL
        REFERENCES public.lecture_qa_evaluations (evaluation_id) ON DELETE CASCADE,
    rendered_session_id uuid NOT NULL
        REFERENCES public.lecture_qa_rendered_sessions (rendered_session_id) ON DELETE CASCADE,

    -- The legacy compatibility key. Deliberately NOT a foreign key: the legacy
    -- table is external production state, and this table must survive
    -- independently of it.
    legacy_session_id text NOT NULL,

    writer_version text NOT NULL,
    source_fingerprint text NOT NULL,
    write_mode text NOT NULL,
    write_status text NOT NULL,

    legacy_session_created boolean NOT NULL DEFAULT false,
    legacy_session_updated boolean NOT NULL DEFAULT false,
    checklist_rows_created integer NOT NULL DEFAULT 0,
    checklist_rows_updated integer NOT NULL DEFAULT 0,

    pre_write_digest text,
    post_write_digest text,

    written_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- One ownership identity per legacy session: two concurrent writers cannot
    -- both claim the same target.
    CONSTRAINT lecture_qa_legacy_writes_owner UNIQUE (legacy_session_id, writer_version),
    CONSTRAINT lecture_qa_legacy_writes_mode CHECK (write_mode IN (
        'DRY_RUN', 'CANARY_NEW_ONLY', 'PRODUCTION_NEW_ONLY', 'EXPLICIT_BACKFILL')),
    CONSTRAINT lecture_qa_legacy_writes_status CHECK (write_status IN (
        'WRITTEN', 'UPDATED', 'NOOP', 'ROLLED_BACK', 'WRITE_VERIFICATION_FAILED')),
    CONSTRAINT lecture_qa_legacy_writes_counts CHECK (
        checklist_rows_created >= 0 AND checklist_rows_updated >= 0),
    CONSTRAINT lecture_qa_legacy_writes_fingerprint
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS lecture_qa_legacy_writes_lecture_idx
    ON public.lecture_qa_legacy_writes (lecture_id, written_at DESC);

COMMENT ON TABLE public.lecture_qa_legacy_writes IS
    'Ownership and audit for legacy QA rows created by the coded writer. A legacy session without a row here is legacy-owned history and is protected from update.';
COMMENT ON COLUMN public.lecture_qa_legacy_writes.legacy_session_id IS
    'Compatibility key only. lecture_id remains the canonical identity; no foreign key to the legacy table is declared.';


-- ---------------------------------------------------------------------------
-- Bounded model generations per QA source fingerprint.
--
-- Append-only: an invalid generation is evidence, not something to overwrite.
-- After MAX generations the Phase 3A evaluation becomes REVIEW_REQUIRED and
-- scheduled processing stops calling the model for that fingerprint.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_qa_generation_attempts (
    attempt_id uuid PRIMARY KEY,
    lecture_id uuid
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE CASCADE,
    source_fingerprint text NOT NULL,
    qa_engine_version text NOT NULL,
    prompt_version text NOT NULL,
    model_name text NOT NULL,

    -- 1-based, and unique per fingerprint so a concurrent second attempt
    -- cannot silently reuse a number.
    generation_number integer NOT NULL,
    outcome text NOT NULL,
    error_code text,
    structured_output_error_count integer NOT NULL DEFAULT 0,
    invalid_evidence_clip_count integer NOT NULL DEFAULT 0,
    forced boolean NOT NULL DEFAULT false,

    created_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_qa_generation_attempts_key
        UNIQUE (source_fingerprint, generation_number),
    CONSTRAINT lecture_qa_generation_attempts_number CHECK (generation_number > 0),
    CONSTRAINT lecture_qa_generation_attempts_outcome CHECK (outcome IN (
        'COMPLETED', 'MODEL_ERROR', 'INVALID_STRUCTURED_OUTPUT', 'INVALID_EVIDENCE',
        'REVIEW_REQUIRED')),
    CONSTRAINT lecture_qa_generation_attempts_fingerprint
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS lecture_qa_generation_attempts_fingerprint_idx
    ON public.lecture_qa_generation_attempts (source_fingerprint, generation_number);

COMMENT ON TABLE public.lecture_qa_generation_attempts IS
    'Append-only record of model generations per QA source fingerprint. Caps paid re-generation after repeated invalid output; earlier attempts are preserved as provenance.';

COMMIT;
