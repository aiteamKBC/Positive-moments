-- ===========================================================================
-- Migration 009 (Phase 2C2): canonical per-document speaker inventory.
--
-- Purely additive. public.lecture_transcript_cues stays IMMUTABLE: no person
-- id, trainer flag, attendance id, or matching result is ever added to it, so
-- identity algorithms can evolve without rewriting parsed evidence.
--
-- SCOPE. A row here is "one distinct RAW WebVTT speaker label inside ONE
-- canonical transcript document". It is NOT a human being. The same person may
-- carry different Teams labels on different days, and different people may
-- carry similar names, so nothing is merged across documents and no role,
-- attendance, or LMS identity is inferred at this layer.
--
-- No mapping table for cues is created: a speaker's cues are already reachable
-- by (document_id, speaker_label_raw), so duplicating thousands of cue rows
-- would add storage and a consistency risk without adding a single guarantee.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.lecture_transcript_speakers (
    speaker_id uuid PRIMARY KEY,
    document_id uuid NOT NULL
        REFERENCES public.lecture_transcript_documents (document_id) ON DELETE CASCADE,
    speaker_inventory_version text NOT NULL,

    -- Exactly as it appeared in the cue. Never rewritten.
    speaker_label_raw text NOT NULL,
    -- Conservative matching aid only: NFKC, whitespace collapse, casefold.
    -- Never an identity, and never used to merge two raw labels.
    speaker_label_normalized text NOT NULL,

    cue_count integer NOT NULL,
    first_cue_index integer NOT NULL,
    last_cue_index integer NOT NULL,
    first_spoken_start_ms bigint NOT NULL,
    last_spoken_end_ms bigint NOT NULL,
    -- sum(end_ms - start_ms) over this speaker's cues. Because Teams
    -- transcripts contain overlapping speech, the sum of this column across a
    -- document MAY legitimately exceed the document duration. That is correct
    -- and is never normalised away.
    gross_spoken_ms bigint NOT NULL,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_speakers_key
        UNIQUE (document_id, speaker_inventory_version, speaker_label_raw),
    CONSTRAINT lecture_transcript_speakers_cue_count CHECK (cue_count > 0),
    CONSTRAINT lecture_transcript_speakers_cue_index CHECK (
        first_cue_index > 0 AND last_cue_index >= first_cue_index),
    CONSTRAINT lecture_transcript_speakers_timing CHECK (
        first_spoken_start_ms >= 0 AND last_spoken_end_ms >= first_spoken_start_ms),
    CONSTRAINT lecture_transcript_speakers_gross CHECK (gross_spoken_ms >= 0),
    CONSTRAINT lecture_transcript_speakers_label_not_blank
        CHECK (btrim(speaker_label_raw) <> '')
);

CREATE INDEX IF NOT EXISTS lecture_transcript_speakers_document_idx
    ON public.lecture_transcript_speakers (document_id, gross_spoken_ms DESC);
-- Phase 2C3 will look speakers up by their normalized form when it starts
-- matching people. The index exists; the matching does not.
CREATE INDEX IF NOT EXISTS lecture_transcript_speakers_normalized_idx
    ON public.lecture_transcript_speakers (speaker_label_normalized);

COMMENT ON TABLE public.lecture_transcript_speakers IS
    'One distinct raw WebVTT speaker label per canonical transcript document. Not a person: no role, attendance, LMS, or cross-document identity is implied.';
COMMENT ON COLUMN public.lecture_transcript_speakers.speaker_label_raw IS
    'The provider label exactly as stored on the canonical cues. Evidence, never rewritten.';
COMMENT ON COLUMN public.lecture_transcript_speakers.speaker_label_normalized IS
    'Conservative NFKC + whitespace-collapse + casefold form. A future matching aid only; two raw labels sharing it are reported as a collision, never merged.';
COMMENT ON COLUMN public.lecture_transcript_speakers.gross_spoken_ms IS
    'Sum of cue durations for this label. Overlapping speech means the per-document total may exceed the document duration; that is expected and correct.';


CREATE TABLE IF NOT EXISTS public.lecture_transcript_speaker_runs (
    run_id uuid PRIMARY KEY,
    target_date date NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    mode text NOT NULL DEFAULT 'SHADOW',
    status text NOT NULL,
    speaker_inventory_version text NOT NULL,

    documents_considered integer NOT NULL DEFAULT 0,
    speakers_created integer NOT NULL DEFAULT 0,
    speakers_updated integer NOT NULL DEFAULT 0,
    cues_aggregated integer NOT NULL DEFAULT 0,
    unassigned_cue_count integer NOT NULL DEFAULT 0,
    normalization_collision_count integer NOT NULL DEFAULT 0,
    documents_with_overlap_excess integer NOT NULL DEFAULT 0,
    error_count integer NOT NULL DEFAULT 0,

    -- Structured audit only: counts and codes, never names or transcript text.
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT lecture_transcript_speaker_runs_mode CHECK (mode IN ('SHADOW', 'DRY_RUN')),
    CONSTRAINT lecture_transcript_speaker_runs_status
        CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS lecture_transcript_speaker_runs_date_idx
    ON public.lecture_transcript_speaker_runs (target_date, started_at DESC);

COMMENT ON TABLE public.lecture_transcript_speaker_runs IS
    'Audit summaries for Phase 2C2 speaker inventory runs. No speaker names, transcript text, tokens, or secrets.';

COMMIT;
