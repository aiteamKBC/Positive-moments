-- ===========================================================================
-- Migration 011 (Phase 2C4): deterministic learner engagement.
--
-- Purely additive. Derived only from persisted Phase 2C3 evidence: attendance
-- snapshots, snapshot members, speaker identity resolutions and speaker
-- roles. Nothing here reads, writes, indexes or triggers public.kbc_attendance,
-- and nothing here alters any Phase 2C3 table or any legacy QA table.
--
-- learner_engagement_status is a deterministic RECOMMENDATION reproducing the
-- legacy Item 7 override. It is never written into qa_doctors_checklist_items;
-- Phase 3 decides how to consume it.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.lecture_engagement_metrics (
    engagement_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE CASCADE,
    document_id uuid NOT NULL
        REFERENCES public.lecture_transcript_documents (document_id) ON DELETE CASCADE,
    attendance_snapshot_id uuid NOT NULL
        REFERENCES public.lecture_attendance_snapshots (snapshot_id) ON DELETE CASCADE,

    engagement_algorithm_version text NOT NULL,
    trainer_exclusion_version text NOT NULL,
    resolver_version text NOT NULL,
    role_algorithm_version text NOT NULL,

    calculation_status text NOT NULL,
    trainer_speaker_id uuid
        REFERENCES public.lecture_transcript_speakers (speaker_id) ON DELETE CASCADE,
    trainer_exclusion_status text NOT NULL,
    trainer_attendance_match_count integer NOT NULL,

    attendance_before_trainer_exclusion integer NOT NULL,
    trainer_excluded_count integer NOT NULL,
    -- NULL only when the trainer exclusion is ambiguous: no defensible denominator.
    attended_count integer,

    resolved_learner_speaker_count integer NOT NULL,
    -- Unique attendance members with at least one safely resolved LEARNER speaker.
    spoke_count integer NOT NULL,
    silent_count integer NOT NULL,
    ambiguous_speaker_count integer NOT NULL,
    unresolved_speaker_count integer NOT NULL,

    -- Legacy Number((spoke / attended * 100).toFixed(2)).
    engagement_percentage numeric(5, 2),
    engagement_score integer,
    -- Legacy Item 7 override value; NULL when legacy applied no override.
    learner_engagement_status text,
    item7_override_applied boolean NOT NULL,

    source_fingerprint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    -- Counts, versions and ids only. Never names, emails or transcript text.
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_engagement_metrics_key UNIQUE (
        document_id, attendance_snapshot_id, resolver_version,
        role_algorithm_version, engagement_algorithm_version, source_fingerprint),
    CONSTRAINT lecture_engagement_metrics_status CHECK (calculation_status IN (
        'CALCULATED', 'CALCULATED_WITH_AMBIGUITY', 'REVIEW_AMBIGUITY_MAY_CHANGE_RESULT',
        'NO_ATTENDED_LEARNERS', 'TRAINER_ATTENDANCE_AMBIGUOUS')),
    CONSTRAINT lecture_engagement_metrics_trainer_status CHECK (trainer_exclusion_status IN (
        'TRAINER_NOT_IN_ATTENDANCE', 'TRAINER_EXCLUDED', 'NO_TRAINER_CANDIDATE',
        'TRAINER_ATTENDANCE_AMBIGUOUS')),
    CONSTRAINT lecture_engagement_metrics_item7 CHECK (
        learner_engagement_status IS NULL
        OR learner_engagement_status IN ('Met', 'Partially Met', 'Not Met')),
    CONSTRAINT lecture_engagement_metrics_counts CHECK (
        attendance_before_trainer_exclusion >= 0 AND trainer_excluded_count >= 0
        AND trainer_attendance_match_count >= 0
        AND resolved_learner_speaker_count >= 0 AND spoke_count >= 0
        AND silent_count >= 0 AND ambiguous_speaker_count >= 0
        AND unresolved_speaker_count >= 0
        AND (attended_count IS NULL OR (attended_count >= 0
             AND spoke_count <= attended_count
             AND spoke_count + silent_count = attended_count))),
    CONSTRAINT lecture_engagement_metrics_percentage CHECK (
        engagement_percentage IS NULL
        OR (engagement_percentage >= 0 AND engagement_percentage <= 100)),
    CONSTRAINT lecture_engagement_metrics_score CHECK (
        engagement_score IS NULL OR engagement_score BETWEEN 1 AND 5),
    -- An ambiguous trainer exclusion never produces a number.
    CONSTRAINT lecture_engagement_metrics_ambiguous_trainer CHECK (
        calculation_status <> 'TRAINER_ATTENDANCE_AMBIGUOUS'
        OR (attended_count IS NULL AND engagement_percentage IS NULL
            AND engagement_score IS NULL AND learner_engagement_status IS NULL)),
    -- "Nobody attended" is explicit and never carries an Item 7 override.
    CONSTRAINT lecture_engagement_metrics_no_attendees CHECK (
        calculation_status <> 'NO_ATTENDED_LEARNERS'
        OR (attended_count = 0 AND learner_engagement_status IS NULL
            AND NOT item7_override_applied)),
    CONSTRAINT lecture_engagement_metrics_fingerprint
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS lecture_engagement_metrics_lecture_idx
    ON public.lecture_engagement_metrics (lecture_id, created_at DESC);

COMMENT ON TABLE public.lecture_engagement_metrics IS
    'Deterministic legacy-parity learner engagement per attendance snapshot, from persisted Phase 2C3 evidence only. Never written to QA tables.';
COMMENT ON COLUMN public.lecture_engagement_metrics.spoke_count IS
    'Unique attendance snapshot members with a safely resolved LEARNER speaker. Legacy counted speaker labels; duplicate aliases of one learner count once here.';
COMMENT ON COLUMN public.lecture_engagement_metrics.learner_engagement_status IS
    'Deterministic Item 7 recommendation (>75 Met, >=50 Partially Met, else Not Met). NULL when no learners attended, matching legacy, where the AI result stood.';


CREATE TABLE IF NOT EXISTS public.lecture_engagement_participants (
    participant_id uuid PRIMARY KEY,
    engagement_id uuid NOT NULL
        REFERENCES public.lecture_engagement_metrics (engagement_id) ON DELETE CASCADE,
    -- A reference, never a copy: no name or email is duplicated here.
    snapshot_member_id uuid NOT NULL
        REFERENCES public.lecture_attendance_snapshot_members (member_id) ON DELETE CASCADE,
    participation_status text NOT NULL,
    matched_speaker_count integer NOT NULL DEFAULT 0,
    first_speaker_id uuid
        REFERENCES public.lecture_transcript_speakers (speaker_id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT lecture_engagement_participants_key UNIQUE (engagement_id, snapshot_member_id),
    CONSTRAINT lecture_engagement_participants_status CHECK (participation_status IN (
        'SPOKE', 'SILENT', 'EXCLUDED_TRAINER', 'UNDETERMINED')),
    CONSTRAINT lecture_engagement_participants_speakers CHECK (
        (participation_status = 'SPOKE' AND matched_speaker_count > 0
         AND first_speaker_id IS NOT NULL)
        OR (participation_status <> 'SPOKE' AND matched_speaker_count = 0
            AND first_speaker_id IS NULL))
);

COMMENT ON TABLE public.lecture_engagement_participants IS
    'Per-member participation for one engagement result: SPOKE, SILENT (deterministic silent learner), EXCLUDED_TRAINER, or UNDETERMINED when the trainer exclusion was ambiguous. Member references only.';


CREATE TABLE IF NOT EXISTS public.lecture_engagement_runs (
    run_id uuid PRIMARY KEY,
    target_date date NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    mode text NOT NULL DEFAULT 'SHADOW',
    status text NOT NULL,
    engagement_algorithm_version text NOT NULL,
    resolver_version text NOT NULL,
    role_algorithm_version text NOT NULL,

    snapshots_considered integer NOT NULL DEFAULT 0,
    engagements_created integer NOT NULL DEFAULT 0,
    engagements_updated integer NOT NULL DEFAULT 0,
    participants_written integer NOT NULL DEFAULT 0,
    review_required_count integer NOT NULL DEFAULT 0,
    error_count integer NOT NULL DEFAULT 0,

    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_engagement_runs_mode CHECK (mode IN ('SHADOW', 'DRY_RUN')),
    CONSTRAINT lecture_engagement_runs_status
        CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS lecture_engagement_runs_date_idx
    ON public.lecture_engagement_runs (target_date, started_at DESC);

COMMENT ON TABLE public.lecture_engagement_runs IS
    'Audit summaries for Phase 2C4 runs. Counts and versions only; no QA write and no AI call ever happens in this phase.';

COMMIT;
