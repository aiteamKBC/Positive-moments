-- ===========================================================================
-- Migration 013 (Phase 3B): legacy QA output rendering and the LMS snapshot.
--
-- Purely additive and SHADOW ONLY. Nothing here writes to, mirrors, or creates
-- a view over qa_doctors_sessions, qa_doctors_checklist_items,
-- qa_perfect_lectures or qa_doctors_transcripts. Phase 3C owns any cutover.
--
-- public.kbc_users_data stays a READ-ONLY external source: this migration adds
-- no column, index, trigger or constraint to it. The roster it returned is
-- frozen into a fingerprinted snapshot so a later change to that live table
-- cannot silently rewrite a historical QA output.
--
-- Phase 3A evidence is never mutated by rendering; these tables only reference
-- it.
-- ===========================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Frozen LMS roster, from the legacy `Get LMS Students` query.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_lms_snapshots (
    snapshot_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE CASCADE,
    lms_snapshot_version text NOT NULL,

    -- The raw module string is what legacy wrote into lms_module; the
    -- normalized form is what it matched "Group" on.
    module text NOT NULL,
    module_normalized text NOT NULL,

    source_row_count integer NOT NULL,
    student_count integer NOT NULL,
    row_cap_reached boolean NOT NULL DEFAULT false,

    source_fingerprint text NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_lms_snapshots_key
        UNIQUE (lecture_id, lms_snapshot_version, source_fingerprint),
    CONSTRAINT lecture_lms_snapshots_counts
        CHECK (source_row_count >= 0 AND student_count >= 0),
    CONSTRAINT lecture_lms_snapshots_fingerprint
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS lecture_lms_snapshots_lecture_idx
    ON public.lecture_lms_snapshots (lecture_id, captured_at DESC);

COMMENT ON TABLE public.lecture_lms_snapshots IS
    'Frozen active-learner roster for one lecture module, from the read-only external public.kbc_users_data. A changed roster produces a new snapshot, never an edit.';


CREATE TABLE IF NOT EXISTS public.lecture_lms_snapshot_members (
    snapshot_member_id uuid PRIMARY KEY,
    snapshot_id uuid NOT NULL
        REFERENCES public.lecture_lms_snapshots (snapshot_id) ON DELETE CASCADE,
    -- kbc_users_data."ID", exactly as the legacy query selected it. Not a new
    -- global person identity and not joined to attendance ids here.
    external_learner_id bigint NOT NULL,
    -- Required: the legacy lms_students payload carries the display name.
    full_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT lecture_lms_snapshot_members_key
        UNIQUE (snapshot_id, external_learner_id, full_name),
    CONSTRAINT lecture_lms_snapshot_members_name CHECK (btrim(full_name) <> '')
);

COMMENT ON TABLE public.lecture_lms_snapshot_members IS
    'Distinct (ID, FullName) pairs of one LMS snapshot. Only the two fields the legacy query returned; no other kbc_users_data column and no email is copied.';


-- ---------------------------------------------------------------------------
-- Rendered legacy-compatible session payload.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_qa_rendered_sessions (
    rendered_session_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE CASCADE,
    evaluation_id uuid NOT NULL
        REFERENCES public.lecture_qa_evaluations (evaluation_id) ON DELETE CASCADE,
    lms_snapshot_id uuid
        REFERENCES public.lecture_lms_snapshots (snapshot_id) ON DELETE SET NULL,

    renderer_version text NOT NULL,
    render_status text NOT NULL,

    -- Compatibility identity only. lecture_id remains canonical.
    session_id text,
    meeting_id text,
    subject text,
    trainer text,
    legacy_date text,
    -- Carried beside legacy_date so a difference is visible, not hidden.
    canonical_session_date date,
    duration text,
    duration_score integer,
    engagement numeric(5, 2),
    engagement_score integer,
    attended_count integer,
    spoke_count integer,
    met_count integer,
    partial_count integer,
    not_met_count integer,
    teaching_quality_rating integer,
    teaching_quality_comments text,
    teaching_quality_evidence text,
    overall_judgement text,
    cancelled_session boolean NOT NULL DEFAULT false,

    lms_module text,
    lms_students_count integer,
    lms_students jsonb,

    strengths jsonb,
    areas_for_development jsonb,
    ksb_coverage jsonb,

    evidence_clip_count integer NOT NULL DEFAULT 0,
    rendered_block_count integer NOT NULL DEFAULT 0,

    source_fingerprint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_qa_rendered_sessions_key UNIQUE (source_fingerprint),
    CONSTRAINT lecture_qa_rendered_sessions_status CHECK (render_status IN (
        'RENDERED', 'RENDERED_NON_DELIVERED', 'SOURCE_QA_NOT_READY',
        'INCONSISTENT_EVIDENCE')),
    CONSTRAINT lecture_qa_rendered_sessions_counts CHECK (
        evidence_clip_count >= 0 AND rendered_block_count >= 0),
    CONSTRAINT lecture_qa_rendered_sessions_fingerprint
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS lecture_qa_rendered_sessions_lecture_idx
    ON public.lecture_qa_rendered_sessions (lecture_id, created_at DESC);

COMMENT ON TABLE public.lecture_qa_rendered_sessions IS
    'Shadow legacy-compatible QA session payload rendered from Phase 3A evidence. Never written to qa_doctors_sessions.';
COMMENT ON COLUMN public.lecture_qa_rendered_sessions.legacy_date IS
    'Legacy date semantics: the UTC calendar date of the selected transcript start. canonical_session_date holds the Cairo business date beside it.';


-- ---------------------------------------------------------------------------
-- Rendered legacy-compatible checklist rows.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_qa_rendered_checklist_items (
    rendered_item_id uuid PRIMARY KEY,
    rendered_session_id uuid NOT NULL
        REFERENCES public.lecture_qa_rendered_sessions (rendered_session_id) ON DELETE CASCADE,

    -- Legacy compatibility fields.
    session_id text,
    session_id_match text,
    checklist_order integer NOT NULL,
    checklist_item text NOT NULL,
    status text NOT NULL,
    -- The single legacy text field: rendered evidence merged with reasoning.
    evidence text,
    severity text,

    -- Structured provenance the legacy table had nowhere to put.
    rendered_evidence text,
    reasoning text,
    ai_status text,
    status_source text NOT NULL,
    evidence_clip_ids uuid[] NOT NULL DEFAULT '{}',
    cue_ids uuid[] NOT NULL DEFAULT '{}',
    rendered_block_count integer NOT NULL DEFAULT 0,

    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT lecture_qa_rendered_checklist_items_key
        UNIQUE (rendered_session_id, checklist_order),
    CONSTRAINT lecture_qa_rendered_checklist_items_order
        CHECK (checklist_order BETWEEN 1 AND 11),
    CONSTRAINT lecture_qa_rendered_checklist_items_status
        CHECK (status IN ('Met', 'Partially Met', 'Not Met')),
    CONSTRAINT lecture_qa_rendered_checklist_items_severity
        CHECK (severity IS NULL OR severity IN ('pass', 'warning', 'fail'))
);

COMMENT ON TABLE public.lecture_qa_rendered_checklist_items IS
    'Shadow legacy-compatible checklist rows, including session_id_match. Keeps rendered_evidence and reasoning separately so the single legacy evidence field loses no provenance.';
COMMENT ON COLUMN public.lecture_qa_rendered_checklist_items.cue_ids IS
    'Canonical cue ids every rendered quote came from, so a quote is traceable without duplicating transcript text.';


CREATE TABLE IF NOT EXISTS public.lecture_qa_render_runs (
    run_id uuid PRIMARY KEY,
    target_date date NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    status text NOT NULL,
    renderer_version text NOT NULL,
    lms_snapshot_version text NOT NULL,

    lectures_considered integer NOT NULL DEFAULT 0,
    sessions_rendered integer NOT NULL DEFAULT 0,
    sessions_reused integer NOT NULL DEFAULT 0,
    checklist_rows_rendered integer NOT NULL DEFAULT 0,
    evidence_clips_consumed integer NOT NULL DEFAULT 0,
    blocks_rendered integer NOT NULL DEFAULT 0,
    lms_snapshots_created integer NOT NULL DEFAULT 0,
    lms_snapshots_reused integer NOT NULL DEFAULT 0,
    not_ready_count integer NOT NULL DEFAULT 0,
    -- Must always be zero: Phase 3B renders, it never evaluates.
    provider_calls integer NOT NULL DEFAULT 0,
    error_count integer NOT NULL DEFAULT 0,

    -- Counts, ids and versions only: never learner names or transcript text.
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_qa_render_runs_status
        CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    CONSTRAINT lecture_qa_render_runs_no_provider CHECK (provider_calls = 0)
);

CREATE INDEX IF NOT EXISTS lecture_qa_render_runs_date_idx
    ON public.lecture_qa_render_runs (target_date, started_at DESC);

COMMENT ON TABLE public.lecture_qa_render_runs IS
    'Audit summaries for Phase 3B rendering runs. provider_calls is CHECK-constrained to zero: rendering never calls a model.';

COMMIT;
