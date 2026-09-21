-- ===========================================================================
-- Migration 008 (Phase 2C1): canonical transcript document and cue model.
--
-- Purely additive. It reads nothing and replaces nothing: Phase 2A raw
-- artifacts and the Phase 2B combined transcript remain the source evidence
-- and are never written by this layer.
--
-- PROVENANCE IDENTITY. A canonical document is NOT keyed by lecture. It is
-- keyed by the exact evidence and code that produced it:
--
--     (selection_id, source_content_sha256, parser_version)
--
-- so a reselected lecture, a changed combined transcript, or a new parser
-- version each produce a NEW document row and leave the previous derived
-- evidence intact. source_fingerprint is carried through from Phase 2B
-- unchanged - no second fingerprint convention is invented here.
--
-- Cue timestamps are OFFSETS in whole milliseconds on the combined transcript
-- timeline. They are not wall-clock instants and carry no timezone.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.lecture_transcript_documents (
    document_id uuid PRIMARY KEY,
    lecture_id uuid NOT NULL
        REFERENCES public.lecture_sessions (lecture_id) ON DELETE RESTRICT,
    selection_id uuid NOT NULL
        REFERENCES public.lecture_transcript_selections (selection_id) ON DELETE RESTRICT,
    combined_id uuid NOT NULL
        REFERENCES public.lecture_combined_transcripts (combined_id) ON DELETE RESTRICT,

    parser_version text NOT NULL,
    -- Carried verbatim from lecture_combined_transcripts.
    source_fingerprint text NOT NULL,
    source_content_sha256 text NOT NULL,

    parse_status text NOT NULL,
    cue_count integer NOT NULL DEFAULT 0,
    first_cue_start_ms bigint,
    last_cue_end_ms bigint,
    duration_ms bigint,

    unique_raw_speaker_label_count integer NOT NULL DEFAULT 0,
    empty_text_cue_count integer NOT NULL DEFAULT 0,
    overlapping_cue_count integer NOT NULL DEFAULT 0,
    malformed_block_count integer NOT NULL DEFAULT 0,
    multi_speaker_cue_count integer NOT NULL DEFAULT 0,
    warning_count integer NOT NULL DEFAULT 0,

    -- Phase 2B duration, kept alongside the parsed range so parity is a stored
    -- fact rather than something a report has to recompute.
    combined_duration_seconds numeric,
    duration_difference_ms bigint,

    parsed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_documents_provenance_key
        UNIQUE (selection_id, source_content_sha256, parser_version),
    CONSTRAINT lecture_transcript_documents_status CHECK (parse_status IN (
        'PARSED', 'PARSED_WITH_WARNINGS', 'EMPTY_TRANSCRIPT',
        'INVALID_WEBVTT', 'UNSUPPORTED_STRUCTURE', 'ERROR'
    )),
    CONSTRAINT lecture_transcript_documents_sha
        CHECK (source_content_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT lecture_transcript_documents_fingerprint
        CHECK (source_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT lecture_transcript_documents_counts CHECK (
        cue_count >= 0 AND unique_raw_speaker_label_count >= 0
        AND empty_text_cue_count >= 0 AND overlapping_cue_count >= 0
        AND malformed_block_count >= 0 AND multi_speaker_cue_count >= 0
        AND warning_count >= 0
    ),
    CONSTRAINT lecture_transcript_documents_range CHECK (
        (first_cue_start_ms IS NULL AND last_cue_end_ms IS NULL AND duration_ms IS NULL)
        OR (first_cue_start_ms >= 0 AND last_cue_end_ms >= first_cue_start_ms
            AND duration_ms = last_cue_end_ms - first_cue_start_ms)
    )
);

CREATE INDEX IF NOT EXISTS lecture_transcript_documents_lecture_idx
    ON public.lecture_transcript_documents (lecture_id, parsed_at DESC);
CREATE INDEX IF NOT EXISTS lecture_transcript_documents_selection_idx
    ON public.lecture_transcript_documents (selection_id);
CREATE INDEX IF NOT EXISTS lecture_transcript_documents_combined_idx
    ON public.lecture_transcript_documents (combined_id);

COMMENT ON TABLE public.lecture_transcript_documents IS
    'Canonical parsed transcript documents derived from lecture_combined_transcripts. Derived evidence: the combined transcript and the raw artifacts remain the source of truth.';
COMMENT ON COLUMN public.lecture_transcript_documents.source_fingerprint IS
    'Carried verbatim from lecture_combined_transcripts.source_fingerprint. Not a second fingerprint convention.';
COMMENT ON COLUMN public.lecture_transcript_documents.source_content_sha256 IS
    'SHA-256 of the exact combined transcript text that was parsed, so a document is always traceable to the bytes it came from.';
COMMENT ON COLUMN public.lecture_transcript_documents.duration_ms IS
    'last_cue_end_ms - first_cue_start_ms. An offset span on the combined timeline, not a wall-clock duration.';


CREATE TABLE IF NOT EXISTS public.lecture_transcript_cues (
    cue_id uuid PRIMARY KEY,
    document_id uuid NOT NULL
        REFERENCES public.lecture_transcript_documents (document_id) ON DELETE CASCADE,
    cue_index integer NOT NULL,

    start_ms bigint NOT NULL,
    end_ms bigint NOT NULL,

    -- Provider label exactly as it appeared. NULL when absent or ambiguous.
    -- No LMS, attendance, trainer/learner, or alias resolution happens here.
    speaker_label_raw text,
    text text NOT NULL,
    cue_text_sha256 text NOT NULL,

    created_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_cues_index_key UNIQUE (document_id, cue_index),
    CONSTRAINT lecture_transcript_cues_index_positive CHECK (cue_index > 0),
    CONSTRAINT lecture_transcript_cues_start CHECK (start_ms >= 0),
    CONSTRAINT lecture_transcript_cues_order CHECK (end_ms >= start_ms),
    CONSTRAINT lecture_transcript_cues_sha CHECK (cue_text_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS lecture_transcript_cues_document_idx
    ON public.lecture_transcript_cues (document_id, cue_index);
CREATE INDEX IF NOT EXISTS lecture_transcript_cues_speaker_idx
    ON public.lecture_transcript_cues (document_id, speaker_label_raw)
    WHERE speaker_label_raw IS NOT NULL;

COMMENT ON TABLE public.lecture_transcript_cues IS
    'Canonical ordered cues of one parsed transcript document. Timestamps are millisecond offsets on the combined transcript timeline.';
COMMENT ON COLUMN public.lecture_transcript_cues.speaker_label_raw IS
    'Raw provider voice-tag label, or NULL when the cue had none or carried conflicting labels. Never an inferred or matched identity.';
COMMENT ON COLUMN public.lecture_transcript_cues.text IS
    'Canonical spoken text: WebVTT markup removed and HTML entities decoded, wording otherwise untouched.';


CREATE TABLE IF NOT EXISTS public.lecture_transcript_parse_runs (
    run_id uuid PRIMARY KEY,
    target_date date NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    mode text NOT NULL DEFAULT 'SHADOW',
    status text NOT NULL,
    parser_version text NOT NULL,

    combined_transcripts_considered integer NOT NULL DEFAULT 0,
    documents_created integer NOT NULL DEFAULT 0,
    documents_reused integer NOT NULL DEFAULT 0,
    cues_written integer NOT NULL DEFAULT 0,
    cues_reused integer NOT NULL DEFAULT 0,
    parsed_count integer NOT NULL DEFAULT 0,
    parsed_with_warnings_count integer NOT NULL DEFAULT 0,
    failed_count integer NOT NULL DEFAULT 0,
    duration_parity_mismatches integer NOT NULL DEFAULT 0,

    -- Structured audit only. Never transcript text.
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_parse_runs_mode CHECK (mode IN ('SHADOW', 'DRY_RUN')),
    CONSTRAINT lecture_transcript_parse_runs_status
        CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS lecture_transcript_parse_runs_date_idx
    ON public.lecture_transcript_parse_runs (target_date, started_at DESC);

COMMENT ON TABLE public.lecture_transcript_parse_runs IS
    'Audit summaries for Phase 2C1 canonical parse runs. No transcript text, tokens, or secrets.';

COMMIT;
