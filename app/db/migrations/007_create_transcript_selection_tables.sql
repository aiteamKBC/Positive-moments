-- ===========================================================================
-- Migration 007 (Phase 2B): transcript occurrence selection and the DERIVED
--                           combined transcript.
--
-- Purely additive. Phase 2A raw evidence is never written by these tables:
-- lecture_transcript_artifacts and lecture_transcript_artifact_contents stay
-- immutable, and the combined WebVTT lives in its own table so it can never be
-- mistaken for provider bytes.
--
-- IDEMPOTENCY. selection_id is uuidv5(lecture_id, selection_version), so one
-- lecture has exactly one current selection per algorithm version. Re-running
-- with identical inputs rewrites the same row, replaces the same part rows, and
-- produces the same combined hash - never a second copy.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.lecture_transcript_selections (
    selection_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE RESTRICT,
    selection_version text NOT NULL,

    -- The primary artifact. Its provider transcript ID is the value the legacy
    -- QA flow stored as qa_doctors_sessions.session_id, kept here purely for
    -- parity measurement; it is NOT an identity in the new architecture.
    primary_artifact_id uuid
        REFERENCES public.lecture_transcript_artifacts (artifact_id) ON DELETE RESTRICT,
    primary_provider_transcript_id text,

    selection_status text NOT NULL,
    candidate_count_before_date_filter integer NOT NULL DEFAULT 0,
    same_day_candidate_count integer NOT NULL DEFAULT 0,
    occurrence_window_candidate_count integer NOT NULL DEFAULT 0,
    selected_part_count integer NOT NULL DEFAULT 0,

    primary_overlap_seconds numeric,
    primary_duration_seconds numeric,
    primary_start_distance_seconds numeric,

    actual_start timestamptz,
    actual_end timestamptz,
    start_difference_minutes integer,
    end_difference_minutes integer,

    combined_content_sha256 text,
    combined_content_bytes integer,
    combined_duration_seconds numeric,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_selections_key UNIQUE (lecture_id, selection_version),
    CONSTRAINT lecture_transcript_selections_status CHECK (selection_status IN (
        'SELECTED', 'NO_CANDIDATES', 'NO_SAME_DAY_CANDIDATES',
        'NO_WINDOW_CANDIDATES', 'COMBINE_FAILED'
    )),
    CONSTRAINT lecture_transcript_selections_counts CHECK (
        candidate_count_before_date_filter >= 0 AND same_day_candidate_count >= 0
        AND occurrence_window_candidate_count >= 0 AND selected_part_count >= 0
    ),
    CONSTRAINT lecture_transcript_selections_sha CHECK (
        combined_content_sha256 IS NULL OR combined_content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    -- A selection either has a primary and parts, or it selected nothing.
    CONSTRAINT lecture_transcript_selections_primary CHECK (
        (selection_status = 'SELECTED' AND primary_artifact_id IS NOT NULL AND selected_part_count > 0)
        OR (selection_status <> 'SELECTED' AND selected_part_count = 0)
    )
);

CREATE INDEX IF NOT EXISTS lecture_transcript_selections_lecture_idx
    ON public.lecture_transcript_selections (lecture_id);

COMMENT ON TABLE public.lecture_transcript_selections IS
    'Phase 2B occurrence selection: which raw transcript artifacts belong to a canonical lecture. Derived data; Phase 2A raw evidence is untouched.';
COMMENT ON COLUMN public.lecture_transcript_selections.primary_provider_transcript_id IS
    'Provider transcript ID of the primary part. Compared against the legacy qa_doctors_sessions.session_id for parity only.';


CREATE TABLE IF NOT EXISTS public.lecture_transcript_selection_parts (
    selection_id uuid NOT NULL
        REFERENCES public.lecture_transcript_selections (selection_id) ON DELETE CASCADE,
    artifact_id uuid NOT NULL
        REFERENCES public.lecture_transcript_artifacts (artifact_id) ON DELETE RESTRICT,
    part_index integer NOT NULL,
    is_primary boolean NOT NULL DEFAULT false,
    part_offset_ms bigint NOT NULL DEFAULT 0,
    provider_transcript_id text NOT NULL,
    provider_call_id text,
    provider_created_at timestamptz,
    provider_end_at timestamptz,

    PRIMARY KEY (selection_id, artifact_id),
    CONSTRAINT lecture_transcript_selection_parts_index UNIQUE (selection_id, part_index),
    CONSTRAINT lecture_transcript_selection_parts_index_positive CHECK (part_index > 0),
    CONSTRAINT lecture_transcript_selection_parts_offset CHECK (part_offset_ms >= 0)
);

COMMENT ON TABLE public.lecture_transcript_selection_parts IS
    'The selected transcript parts of one selection, in chronological order, with the timeline offset applied during combination.';


CREATE TABLE IF NOT EXISTS public.lecture_combined_transcripts (
    combined_id uuid PRIMARY KEY,
    selection_id uuid NOT NULL
        REFERENCES public.lecture_transcript_selections (selection_id) ON DELETE CASCADE,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE RESTRICT,
    selection_version text NOT NULL,

    content_format text NOT NULL DEFAULT 'text/vtt',
    combined_content text NOT NULL,
    content_sha256 text NOT NULL,
    content_bytes integer NOT NULL,
    duration_seconds numeric NOT NULL,
    duration_minutes integer NOT NULL,
    parts_combined integer NOT NULL,
    speaker_attribution boolean,

    -- Hash of the ordered source artifact hashes: identical inputs plus the
    -- same algorithm version must yield the same derived result.
    source_fingerprint text NOT NULL,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_combined_transcripts_key UNIQUE (selection_id),
    CONSTRAINT lecture_combined_transcripts_sha CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT lecture_combined_transcripts_fingerprint CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT lecture_combined_transcripts_format CHECK (content_format IN ('text/vtt')),
    CONSTRAINT lecture_combined_transcripts_bytes CHECK (content_bytes >= 0),
    CONSTRAINT lecture_combined_transcripts_duration CHECK (duration_seconds >= 0)
);

CREATE INDEX IF NOT EXISTS lecture_combined_transcripts_lecture_idx
    ON public.lecture_combined_transcripts (lecture_id);

COMMENT ON TABLE public.lecture_combined_transcripts IS
    'DERIVED combined WebVTT produced by Phase 2B from raw Phase 2A parts. Never provider bytes, and never written back into raw artifact content.';
COMMENT ON COLUMN public.lecture_combined_transcripts.source_fingerprint IS
    'SHA-256 over the algorithm version and the ordered source artifact content hashes. Same inputs and version imply the same derived output.';


CREATE TABLE IF NOT EXISTS public.lecture_transcript_selection_runs (
    run_id uuid PRIMARY KEY,
    target_date date NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    mode text NOT NULL DEFAULT 'SHADOW',
    status text NOT NULL,
    selection_version text NOT NULL,

    lectures_considered integer NOT NULL DEFAULT 0,
    lectures_ready integer NOT NULL DEFAULT 0,
    lectures_skipped_not_ready integer NOT NULL DEFAULT 0,
    selections_created integer NOT NULL DEFAULT 0,
    selections_updated integer NOT NULL DEFAULT 0,
    selections_unchanged integer NOT NULL DEFAULT 0,
    combined_transcripts_created integer NOT NULL DEFAULT 0,
    combined_transcripts_reused integer NOT NULL DEFAULT 0,
    multi_part_selections integer NOT NULL DEFAULT 0,
    legacy_parity_matched integer NOT NULL DEFAULT 0,
    legacy_parity_total integer NOT NULL DEFAULT 0,
    error_count integer NOT NULL DEFAULT 0,

    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_selection_runs_mode CHECK (mode IN ('SHADOW', 'DRY_RUN')),
    CONSTRAINT lecture_transcript_selection_runs_status
        CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS lecture_transcript_selection_runs_date_idx
    ON public.lecture_transcript_selection_runs (target_date, started_at DESC);

COMMENT ON TABLE public.lecture_transcript_selection_runs IS
    'Audit summaries for Phase 2B selection runs. No transcript text, tokens, or secrets.';

COMMIT;
