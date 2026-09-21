-- ===========================================================================
-- Migration 004 (Phase 2A): raw Microsoft Graph transcript artifact registry.
--
-- Purely additive. It creates three new tables and touches nothing that
-- already exists. In particular public.qa_doctors_transcripts is left exactly
-- as it is: it is NOT repurposed, altered, or written to.
--
-- SCOPE. This layer is EVIDENCE ACQUISITION only. It records every transcript
-- artifact Microsoft Graph exposes for a canonical lecture's onlineMeeting,
-- byte-for-byte, with a hash. It deliberately does NOT select a primary
-- transcript, combine parts, shift timelines, or parse cues - all of that is
-- Phase 2B and later.
--
-- DISCOVERY LINKAGE, NOT OCCURRENCE ATTRIBUTION. A Teams *series*
-- onlineMeeting exposes the transcripts of every occurrence of the series, so
-- listing transcripts for one canonical lecture returns artifacts belonging to
-- other dates too. `lecture_id` therefore records WHICH CANONICAL LECTURE'S
-- MEETING EXPOSED the artifact. It is not a claim that the transcript belongs
-- to that occurrence. Phase 2B performs occurrence attribution by overlap with
-- the scheduled window.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.lecture_transcript_artifacts (
    artifact_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE RESTRICT,

    provider text NOT NULL DEFAULT 'MICROSOFT_GRAPH',
    provider_transcript_id text NOT NULL,

    -- Provenance of the call that produced this artifact, carried straight
    -- through from Phase 1 and from the Graph payload.
    meeting_id text NOT NULL,
    meeting_lookup_user_id text NOT NULL,
    provider_call_id text,
    content_correlation_id text,

    provider_created_at timestamptz,
    provider_end_at timestamptz,

    artifact_status text NOT NULL,

    -- Denormalized pointer to the CURRENT content version. Full history lives
    -- in public.lecture_transcript_artifact_contents.
    content_format text,
    speaker_attribution boolean,
    content_sha256 text,
    content_bytes integer,
    content_version integer NOT NULL DEFAULT 0,

    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    content_fetched_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    -- Structured provider/audit metadata ONLY. Never transcript text.
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_artifacts_provider_key
        UNIQUE (provider, provider_transcript_id, lecture_id),
    CONSTRAINT lecture_transcript_artifacts_status CHECK (artifact_status IN (
        'DISCOVERED',
        'CONTENT_STORED',
        'CONTENT_STORED_NO_SPEAKER_ATTRIBUTION',
        'TRANSCRIPT_NOT_READY',
        'TRANSCRIPT_NOT_FOUND',
        'BLOCKED_TRANSCRIPT_ACCESS',
        'CONTENT_FETCH_ERROR'
    )),
    CONSTRAINT lecture_transcript_artifacts_content_format CHECK (
        content_format IS NULL OR content_format IN ('text/vtt')
    ),
    CONSTRAINT lecture_transcript_artifacts_content_bytes CHECK (
        content_bytes IS NULL OR content_bytes >= 0
    ),
    CONSTRAINT lecture_transcript_artifacts_content_version CHECK (content_version >= 0),
    -- A stored content pointer must be complete and hash-verifiable.
    CONSTRAINT lecture_transcript_artifacts_content_complete CHECK (
        (content_sha256 IS NULL AND content_bytes IS NULL AND content_version = 0)
        OR (content_sha256 ~ '^[0-9a-f]{64}$' AND content_bytes IS NOT NULL
            AND content_format IS NOT NULL AND speaker_attribution IS NOT NULL
            AND content_version > 0)
    ),
    CONSTRAINT lecture_transcript_artifacts_time_order CHECK (
        provider_end_at IS NULL OR provider_created_at IS NULL
        OR provider_end_at >= provider_created_at
    )
);

CREATE INDEX IF NOT EXISTS lecture_transcript_artifacts_lecture_idx
    ON public.lecture_transcript_artifacts (lecture_id, provider_created_at);
CREATE INDEX IF NOT EXISTS lecture_transcript_artifacts_meeting_idx
    ON public.lecture_transcript_artifacts (meeting_id);
-- Phase 2B looks artifacts up by the provider transcript ID, which is also the
-- value the legacy QA flow stored as qa_doctors_sessions.session_id.
CREATE INDEX IF NOT EXISTS lecture_transcript_artifacts_provider_transcript_idx
    ON public.lecture_transcript_artifacts (provider_transcript_id);

COMMENT ON TABLE public.lecture_transcript_artifacts IS
    'Raw Microsoft Graph transcript artifacts discovered for canonical lectures. Evidence acquisition only: no selection, combination, timeline shifting, or cue parsing.';
COMMENT ON COLUMN public.lecture_transcript_artifacts.lecture_id IS
    'The canonical lecture whose onlineMeeting exposed this artifact. A series meeting exposes every occurrence''s transcripts, so this is discovery linkage, NOT occurrence attribution.';
COMMENT ON COLUMN public.lecture_transcript_artifacts.provider_transcript_id IS
    'Graph callTranscript id. The legacy QA flow stored this value as qa_doctors_sessions.session_id; that identity design is deliberately NOT adopted here.';
COMMENT ON COLUMN public.lecture_transcript_artifacts.content_version IS
    'Current version number in lecture_transcript_artifact_contents. 0 means no content has been stored yet.';
COMMENT ON COLUMN public.lecture_transcript_artifacts.metadata IS
    'Structured provider and audit metadata only. Never transcript text, tokens, or headers.';


-- ---------------------------------------------------------------------------
-- Content versions. Raw bytes live here, never in metadata JSON.
--
-- Provenance rule: a re-fetch that yields the SAME sha256 reuses the existing
-- version (no duplicate evidence). A re-fetch that yields DIFFERENT content
-- appends a new version and leaves every earlier version intact.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_transcript_artifact_contents (
    content_id uuid PRIMARY KEY,
    artifact_id uuid NOT NULL
        REFERENCES public.lecture_transcript_artifacts (artifact_id) ON DELETE RESTRICT,
    version_number integer NOT NULL,

    content_format text NOT NULL,
    speaker_attribution boolean NOT NULL,
    raw_content text NOT NULL,
    content_sha256 text NOT NULL,
    content_bytes integer NOT NULL,

    first_fetched_at timestamptz NOT NULL DEFAULT now(),
    last_fetched_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT lecture_transcript_artifact_contents_version_key
        UNIQUE (artifact_id, version_number),
    -- The same bytes are never stored twice for one artifact.
    CONSTRAINT lecture_transcript_artifact_contents_hash_key
        UNIQUE (artifact_id, content_sha256),
    CONSTRAINT lecture_transcript_artifact_contents_version_positive
        CHECK (version_number > 0),
    CONSTRAINT lecture_transcript_artifact_contents_sha CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT lecture_transcript_artifact_contents_bytes CHECK (content_bytes >= 0),
    CONSTRAINT lecture_transcript_artifact_contents_format CHECK (content_format IN ('text/vtt'))
);

CREATE INDEX IF NOT EXISTS lecture_transcript_artifact_contents_artifact_idx
    ON public.lecture_transcript_artifact_contents (artifact_id, version_number DESC);

COMMENT ON TABLE public.lecture_transcript_artifact_contents IS
    'Append-only raw transcript content versions. Identical content is reused, changed content appends a version, and nothing is ever overwritten or deleted.';
COMMENT ON COLUMN public.lecture_transcript_artifact_contents.raw_content IS
    'Provider bytes decoded as UTF-8 and stored verbatim: not cleaned, combined, re-timed, or normalized. Hash-verifiable via content_sha256.';


-- ---------------------------------------------------------------------------
-- Acquisition run audit.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.lecture_transcript_acquisition_runs (
    run_id uuid PRIMARY KEY,
    target_date date NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    mode text NOT NULL DEFAULT 'SHADOW',
    status text NOT NULL,

    lectures_considered integer NOT NULL DEFAULT 0,
    lectures_ready integer NOT NULL DEFAULT 0,
    lectures_skipped_not_ready integer NOT NULL DEFAULT 0,

    meetings_queried integer NOT NULL DEFAULT 0,
    meetings_with_zero_artifacts integer NOT NULL DEFAULT 0,
    meetings_with_artifacts integer NOT NULL DEFAULT 0,

    artifacts_discovered integer NOT NULL DEFAULT 0,
    artifacts_created integer NOT NULL DEFAULT 0,
    artifacts_existing integer NOT NULL DEFAULT 0,
    artifact_contents_fetched integer NOT NULL DEFAULT 0,
    content_versions_created integer NOT NULL DEFAULT 0,
    content_unchanged integer NOT NULL DEFAULT 0,

    admin_blocked_count integer NOT NULL DEFAULT 0,
    speaker_attribution_fallback_count integer NOT NULL DEFAULT 0,
    error_count integer NOT NULL DEFAULT 0,

    -- Structured audit only. No tokens, headers, secrets, or transcript text.
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_acquisition_runs_mode CHECK (mode IN ('SHADOW', 'DRY_RUN')),
    CONSTRAINT lecture_transcript_acquisition_runs_status
        CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    CONSTRAINT lecture_transcript_acquisition_runs_counts CHECK (
        lectures_considered >= 0 AND lectures_ready >= 0 AND lectures_skipped_not_ready >= 0
        AND meetings_queried >= 0 AND meetings_with_zero_artifacts >= 0
        AND meetings_with_artifacts >= 0 AND artifacts_discovered >= 0
        AND artifacts_created >= 0 AND artifacts_existing >= 0
        AND artifact_contents_fetched >= 0 AND content_versions_created >= 0
        AND content_unchanged >= 0 AND admin_blocked_count >= 0
        AND speaker_attribution_fallback_count >= 0 AND error_count >= 0
    )
);

CREATE INDEX IF NOT EXISTS lecture_transcript_acquisition_runs_date_idx
    ON public.lecture_transcript_acquisition_runs (target_date, started_at DESC);

COMMENT ON TABLE public.lecture_transcript_acquisition_runs IS
    'Audit summaries for Phase 2A raw transcript acquisition runs. Contains no credentials, tokens, or transcript text.';

COMMIT;
