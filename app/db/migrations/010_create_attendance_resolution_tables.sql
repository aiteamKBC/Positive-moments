-- ===========================================================================
-- Migration 010 (Phase 2C3): attendance provenance, person resolution and
-- deterministic speaker roles.
--
-- Purely additive. Nothing here alters public.lecture_transcript_cues or
-- public.lecture_transcript_speakers: no person id, no role, no attendance id
-- is ever written back onto parsed evidence or the speaker inventory, so
-- identity and role algorithms can both evolve without rewriting history.
--
-- public.kbc_attendance stays a READ-ONLY external source. This migration adds
-- no column, index, trigger or constraint to it.
--
-- TWO SEPARATE LAYERS, DELIBERATELY NOT FUSED:
--   lecture_transcript_speaker_identities  "this speaker matched this roster member"
--   lecture_transcript_speaker_roles       "this speaker is the trainer candidate"
-- They carry independent versions, so changing the matching algorithm cannot
-- silently change stored trainer/learner evidence.
-- ===========================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- Frozen attendance roster.
--
-- kbc_attendance is live and externally owned. A resolution that simply
-- re-read it would be silently reinterpreted by any later ingestion change, so
-- the roster actually used is snapshotted and keyed by a fingerprint of its
-- own contents. A changed roster creates a NEW snapshot; it never edits one.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_attendance_snapshots (
    snapshot_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE CASCADE,
    attendance_resolution_version text NOT NULL,

    -- The exact query contract that produced the roster.
    session_date date NOT NULL,
    module text NOT NULL,
    module_normalized text NOT NULL,

    source_row_count integer NOT NULL,
    present_row_count integer NOT NULL,
    excluded_bot_count integer NOT NULL,
    excluded_no_name_count integer NOT NULL,
    deduplicated_count integer NOT NULL,
    effective_member_count integer NOT NULL,

    -- SHA-256 over the sorted effective roster. Row order is excluded by
    -- construction; any member change changes this value.
    source_fingerprint text NOT NULL,
    bot_filter_version text NOT NULL,

    created_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_attendance_snapshots_key
        UNIQUE (lecture_id, attendance_resolution_version, source_fingerprint),
    CONSTRAINT lecture_attendance_snapshots_counts CHECK (
        source_row_count >= 0 AND present_row_count >= 0
        AND excluded_bot_count >= 0 AND excluded_no_name_count >= 0
        AND deduplicated_count >= 0 AND effective_member_count >= 0),
    CONSTRAINT lecture_attendance_snapshots_fingerprint
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS lecture_attendance_snapshots_lecture_idx
    ON public.lecture_attendance_snapshots (lecture_id, created_at DESC);

COMMENT ON TABLE public.lecture_attendance_snapshots IS
    'Frozen effective attendance roster used for one lecture resolution. Derived from the read-only external table public.kbc_attendance; a changed source produces a new snapshot, never an edit.';
COMMENT ON COLUMN public.lecture_attendance_snapshots.source_fingerprint IS
    'SHA-256 of version + session date + normalized module + the sorted effective member lines (external id, normalized name, email hash). Independent of database row order.';


-- ---------------------------------------------------------------------------
-- Snapshot members: the minimum needed to reproduce matching.
--
-- The email address itself is NOT stored. Legacy dedup and provenance need
-- only equality and change-detection, and a salt-free SHA-256 of the
-- normalized address gives both without holding personal contact data in a
-- derived table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_attendance_snapshot_members (
    member_id uuid PRIMARY KEY,
    snapshot_id uuid NOT NULL
        REFERENCES public.lecture_attendance_snapshots (snapshot_id) ON DELETE CASCADE,

    -- Stable identifier taken from the source row. Never inferred from a name.
    external_person_id text,
    -- Required verbatim: the legacy comparator operates on the display name.
    display_name_raw text NOT NULL,
    display_name_normalized text NOT NULL,
    email_sha256 text,
    -- Legacy `name|email` dedup key, retained so dedup is reproducible.
    dedup_key text NOT NULL,
    attendance_flag integer,

    created_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_attendance_snapshot_members_key UNIQUE (snapshot_id, dedup_key),
    CONSTRAINT lecture_attendance_snapshot_members_name CHECK (btrim(display_name_raw) <> ''),
    CONSTRAINT lecture_attendance_snapshot_members_email_hash
        CHECK (email_sha256 IS NULL OR email_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS lecture_attendance_snapshot_members_snapshot_idx
    ON public.lecture_attendance_snapshot_members (snapshot_id, display_name_normalized);

COMMENT ON TABLE public.lecture_attendance_snapshot_members IS
    'Effective attendance members of one snapshot, after the legacy blank-name, bot and name|email dedup rules. Not a person master: membership is scoped to the snapshot.';
COMMENT ON COLUMN public.lecture_attendance_snapshot_members.email_sha256 IS
    'SHA-256 of the normalized email, or NULL when absent. The address itself is deliberately not persisted.';


-- ---------------------------------------------------------------------------
-- Person resolution: "this speaker matched this roster member".
--
-- Scoped to (speaker, snapshot, resolver version). NOT a global person master:
-- no speaker is merged across lectures and no identity is inferred from a name.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_transcript_speaker_identities (
    resolution_id uuid PRIMARY KEY,
    speaker_id uuid NOT NULL
        REFERENCES public.lecture_transcript_speakers (speaker_id) ON DELETE CASCADE,
    attendance_snapshot_id uuid NOT NULL
        REFERENCES public.lecture_attendance_snapshots (snapshot_id) ON DELETE CASCADE,
    resolver_version text NOT NULL,

    resolution_status text NOT NULL,
    matched_source text,
    matched_member_id uuid
        REFERENCES public.lecture_attendance_snapshot_members (member_id) ON DELETE RESTRICT,
    matched_person_id text,
    match_method text,
    match_score numeric(4, 3),
    -- Candidates at the deciding stage. > 1 means the resolver refused to pick.
    candidate_count integer NOT NULL DEFAULT 0,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_speaker_identities_key
        UNIQUE (speaker_id, attendance_snapshot_id, resolver_version),
    CONSTRAINT lecture_transcript_speaker_identities_status CHECK (
        resolution_status IN ('EXACT_MATCH', 'NORMALIZED_EXACT_MATCH',
                              'LEGACY_FUZZY_MATCH', 'AMBIGUOUS', 'NO_MATCH',
                              'NOT_APPLICABLE')),
    -- A member is attached only by a decisive status, and every decisive
    -- status must attach one. AMBIGUOUS can never carry a person.
    CONSTRAINT lecture_transcript_speaker_identities_member_presence CHECK (
        (resolution_status IN ('EXACT_MATCH', 'NORMALIZED_EXACT_MATCH', 'LEGACY_FUZZY_MATCH')
         AND matched_member_id IS NOT NULL)
        OR (resolution_status IN ('AMBIGUOUS', 'NO_MATCH', 'NOT_APPLICABLE')
            AND matched_member_id IS NULL)),
    CONSTRAINT lecture_transcript_speaker_identities_candidates CHECK (candidate_count >= 0),
    CONSTRAINT lecture_transcript_speaker_identities_score
        CHECK (match_score IS NULL OR (match_score >= 0 AND match_score <= 1))
);

CREATE INDEX IF NOT EXISTS lecture_transcript_speaker_identities_snapshot_idx
    ON public.lecture_transcript_speaker_identities (attendance_snapshot_id, resolution_status);

COMMENT ON TABLE public.lecture_transcript_speaker_identities IS
    'Person resolution of one speaker inventory row against one frozen attendance snapshot under one resolver version. Not a global person identity and never a role.';
COMMENT ON COLUMN public.lecture_transcript_speaker_identities.candidate_count IS
    'Valid candidates at the deciding stage. More than one yields AMBIGUOUS: the resolver never picks by database order, unlike the legacy boolean test.';


-- ---------------------------------------------------------------------------
-- Role inference: deterministic, transcript-derived, separately versioned.
--
-- The key includes the resolver version and snapshot because the LEARNER rule
-- consumes person resolution. Without them, re-resolving with a new matching
-- algorithm would silently rewrite role evidence under an unchanged role
-- algorithm version.
--
-- No AI trainer override exists here. deterministic_trainer_source is always
-- VTT_TOP_SPEAKER; a future AI layer records a separate decision elsewhere.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_transcript_speaker_roles (
    role_id uuid PRIMARY KEY,
    speaker_id uuid NOT NULL
        REFERENCES public.lecture_transcript_speakers (speaker_id) ON DELETE CASCADE,
    role_algorithm_version text NOT NULL,
    -- Recorded, not consulted by the trainer rule: it makes the provenance of
    -- a LEARNER role explicit and auditable.
    resolver_version text NOT NULL,
    attendance_snapshot_id uuid NOT NULL
        REFERENCES public.lecture_attendance_snapshots (snapshot_id) ON DELETE CASCADE,

    role text NOT NULL,
    role_source text NOT NULL,
    -- 1 = most gross spoken milliseconds in the document.
    role_rank integer NOT NULL,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_speaker_roles_key UNIQUE (
        speaker_id, role_algorithm_version, resolver_version, attendance_snapshot_id),
    CONSTRAINT lecture_transcript_speaker_roles_role CHECK (
        role IN ('TRAINER_CANDIDATE', 'LEARNER', 'OTHER_OR_UNRESOLVED')),
    CONSTRAINT lecture_transcript_speaker_roles_rank CHECK (role_rank > 0)
);

CREATE INDEX IF NOT EXISTS lecture_transcript_speaker_roles_snapshot_idx
    ON public.lecture_transcript_speaker_roles (attendance_snapshot_id, role);

COMMENT ON TABLE public.lecture_transcript_speaker_roles IS
    'Deterministic speaker roles from the legacy lecturerFromVtt rule. Trainer candidacy is transcript-derived only; an attendance match never changes it.';
COMMENT ON COLUMN public.lecture_transcript_speaker_roles.role_source IS
    'VTT_TOP_SPEAKER for the trainer candidate. No AI trainer override is applied in this phase.';


-- ---------------------------------------------------------------------------
-- Run audit. Counts, versions and hash prefixes only: never names, emails,
-- rosters, transcript text, tokens or credentials.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_speaker_resolution_runs (
    run_id uuid PRIMARY KEY,
    target_date date NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    mode text NOT NULL DEFAULT 'SHADOW',
    status text NOT NULL,

    attendance_resolution_version text NOT NULL,
    resolver_version text NOT NULL,
    role_algorithm_version text NOT NULL,

    lectures_considered integer NOT NULL DEFAULT 0,
    snapshots_created integer NOT NULL DEFAULT 0,
    snapshots_reused integer NOT NULL DEFAULT 0,
    snapshot_members_written integer NOT NULL DEFAULT 0,
    speakers_considered integer NOT NULL DEFAULT 0,
    resolutions_created integer NOT NULL DEFAULT 0,
    resolutions_updated integer NOT NULL DEFAULT 0,
    roles_created integer NOT NULL DEFAULT 0,
    roles_updated integer NOT NULL DEFAULT 0,
    exact_matches integer NOT NULL DEFAULT 0,
    normalized_exact_matches integer NOT NULL DEFAULT 0,
    legacy_fuzzy_matches integer NOT NULL DEFAULT 0,
    ambiguous_matches integer NOT NULL DEFAULT 0,
    unmatched_speakers integer NOT NULL DEFAULT 0,
    error_count integer NOT NULL DEFAULT 0,

    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_speaker_resolution_runs_mode CHECK (mode IN ('SHADOW', 'DRY_RUN')),
    CONSTRAINT lecture_speaker_resolution_runs_status
        CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS lecture_speaker_resolution_runs_date_idx
    ON public.lecture_speaker_resolution_runs (target_date, started_at DESC);

COMMENT ON TABLE public.lecture_speaker_resolution_runs IS
    'Audit summaries for Phase 2C3 runs. No engagement metric is computed or stored in this phase.';

COMMIT;
