-- ===========================================================================
-- Migration 015 (Phase 3C2.3D): Perfect Lecture parity.
--
-- Purely additive, and BOTH tables belong to the CODED PLATFORM.
--
-- Nothing here touches public.qa_perfect_lectures: no column, index, trigger,
-- constraint or default is added to it. That table stays exactly the shape the
-- legacy n8n QA child and the Master recording branch expect, and ownership is
-- tracked here instead of by marking a legacy production row.
--
-- Why two tables rather than one, and rather than reusing
-- public.lecture_qa_legacy_writes:
--
--   * eligibility and legacy synchronisation are two different lifecycles. A
--     lecture always has an eligibility answer (including "not perfect"); it
--     only has a legacy row when the answer is "perfect" AND we chose to
--     project it. The Operations layer must be able to distinguish
--     "NOT ELIGIBLE" from "ELIGIBLE but legacy sync MISSING".
--   * lecture_qa_legacy_writes is uniquely keyed on
--     (legacy_session_id, writer_version). The Perfect Lecture target is a
--     different legacy row keyed on lecture_key, with its own external
--     writers (recording enrichment, Excel sync). Overloading one audit row
--     with two targets would make "which target did this status refer to?"
--     unanswerable.
--
-- This migration writes no legacy row of any kind.
-- ===========================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The coded platform's own Perfect Lecture answer.
--
-- Canonically keyed by lecture_id. qa_perfect_lectures is NOT the source of
-- truth for this: it is a legacy compatibility output that this table can
-- regenerate at any time WITHOUT re-running the model, because the eligibility
-- inputs are the already-frozen Phase 3A/3B result.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_perfect_lecture_results (
    result_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE CASCADE,
    evaluation_id uuid NOT NULL
        REFERENCES public.lecture_qa_evaluations (evaluation_id) ON DELETE CASCADE,
    rendered_session_id uuid NOT NULL
        REFERENCES public.lecture_qa_rendered_sessions (rendered_session_id) ON DELETE CASCADE,

    -- Which rule produced this answer. The legacy table still holds 79 rows
    -- written under a 12-item checklist; a versioned answer is how a rule
    -- change stays legible instead of silently reinterpreting history.
    eligibility_version text NOT NULL,
    -- The Phase 3B fingerprint the answer was derived from. Equal fingerprint
    -- plus equal version means the answer cannot have changed.
    source_fingerprint text NOT NULL,

    is_perfect boolean NOT NULL,
    reason text NOT NULL,

    -- The observed facts, frozen, so the answer is auditable without
    -- recomputing anything.
    met_count integer NOT NULL,
    partial_count integer NOT NULL,
    not_met_count integer NOT NULL,
    checklist_row_count integer NOT NULL,
    distinct_order_count integer NOT NULL,
    cancelled_session boolean NOT NULL,

    -- Derived legacy compatibility key. Not an identity: see the comment below.
    legacy_lecture_key text NOT NULL,
    legacy_session_id text NOT NULL,

    computed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_perfect_lecture_results_key
        UNIQUE (lecture_id, eligibility_version),
    CONSTRAINT lecture_perfect_lecture_results_fingerprint
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT lecture_perfect_lecture_results_counts CHECK (
        met_count >= 0 AND partial_count >= 0 AND not_met_count >= 0
        AND checklist_row_count >= 0 AND distinct_order_count >= 0)
);

CREATE INDEX IF NOT EXISTS lecture_perfect_lecture_results_perfect_idx
    ON public.lecture_perfect_lecture_results (is_perfect, computed_at DESC);
CREATE INDEX IF NOT EXISTS lecture_perfect_lecture_results_key_idx
    ON public.lecture_perfect_lecture_results (legacy_lecture_key);

COMMENT ON TABLE public.lecture_perfect_lecture_results IS
    'Coded-platform Perfect Lecture eligibility, derived from the frozen Phase 3A/3B result. Canonical by lecture_id; public.qa_perfect_lectures is a downstream compatibility output, never the source of truth.';
COMMENT ON COLUMN public.lecture_perfect_lecture_results.legacy_lecture_key IS
    'Compatibility key "YYYY-MM-DD|subject", reproducing the legacy child exactly. It is NOT an identity: it is known to collide (one date+subject pair already maps to two legacy sessions), so lecture_id remains canonical.';

-- ---------------------------------------------------------------------------
-- 2. Ownership and audit for the legacy Perfect Lecture row.
--
-- A qa_perfect_lectures row without a row here is legacy-owned history and is
-- protected from update AND from deletion by the coded platform, in every
-- mode.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_perfect_lecture_legacy_writes (
    perfect_write_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE CASCADE,
    result_id uuid NOT NULL
        REFERENCES public.lecture_perfect_lecture_results (result_id) ON DELETE CASCADE,
    evaluation_id uuid NOT NULL
        REFERENCES public.lecture_qa_evaluations (evaluation_id) ON DELETE CASCADE,

    -- Compatibility keys only. Deliberately NOT foreign keys: the legacy table
    -- is external production state and this audit must survive independently
    -- of it, including after a legacy row is removed by someone else.
    legacy_lecture_key text NOT NULL,
    legacy_session_id text NOT NULL,

    writer_version text NOT NULL,
    eligibility_version text NOT NULL,
    source_fingerprint text NOT NULL,
    write_mode text NOT NULL,
    write_status text NOT NULL,

    legacy_row_created boolean NOT NULL DEFAULT false,
    legacy_row_updated boolean NOT NULL DEFAULT false,

    pre_write_digest text,
    post_write_digest text,

    written_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- One owner per legacy key per writer version: a concurrent second writer
    -- conflicts instead of creating a second owner.
    CONSTRAINT lecture_perfect_lecture_legacy_writes_owner
        UNIQUE (legacy_lecture_key, writer_version),
    CONSTRAINT lecture_perfect_lecture_legacy_writes_fingerprint
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT lecture_perfect_lecture_legacy_writes_mode CHECK (write_mode IN (
        'DRY_RUN', 'CANARY_NEW_ONLY', 'PRODUCTION_NEW_ONLY', 'EXPLICIT_BACKFILL')),
    -- SUPERSEDED_NOT_PERFECT records a PERFECT -> NOT PERFECT transition
    -- WITHOUT deleting anything: the legacy row is left standing as history
    -- and the change is visible here. Deletion is a separate, explicit,
    -- ownership-proving rollback.
    CONSTRAINT lecture_perfect_lecture_legacy_writes_status CHECK (write_status IN (
        'WRITTEN', 'UPDATED', 'NOOP', 'ROLLED_BACK', 'SUPERSEDED_NOT_PERFECT',
        'WRITE_VERIFICATION_FAILED'))
);

CREATE INDEX IF NOT EXISTS lecture_perfect_lecture_legacy_writes_lecture_idx
    ON public.lecture_perfect_lecture_legacy_writes (lecture_id, written_at DESC);

COMMENT ON TABLE public.lecture_perfect_lecture_legacy_writes IS
    'Ownership and audit for public.qa_perfect_lectures rows created by the coded platform. Absence of a row here means the legacy row is legacy-owned and must never be updated or deleted by the coded platform.';
COMMENT ON COLUMN public.lecture_perfect_lecture_legacy_writes.write_status IS
    'SUPERSEDED_NOT_PERFECT is the safe PERFECT -> NOT PERFECT outcome: the coded answer flipped, the legacy historical row was left untouched, and the divergence is recorded rather than resolved by deletion.';

COMMIT;
