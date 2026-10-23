-- ===========================================================================
-- PROPOSED migration 027: per-part recording sources (P0-C recording discovery).
--
-- STATUS: PROPOSED, NOT APPROVED FOR PRODUCTION (see 024's header). Rollback:
-- 027_create_recording_source_tables.rollback.sql. Independent of 024-026.
--
-- WHY NOT NEW COLUMNS ON lecture_recording_parts
-- ----------------------------------------------
-- lecture_recording_parts (017-019) is the MEASURED media timeline: every row
-- needs canonical_offset_ms (a transcript fact) and a media duration read by
-- ffprobe or the MP4 movie header, and MediaTimeline / the lecture-part
-- producer read every row as a timeline part. Recording discovery must work
-- BEFORE any transcript exists and never downloads media, so a discovery row
-- there would either violate its NOT NULLs or be mistaken for a measurement.
-- 019's source_drive_id/source_item_id stay the media pipeline's to fill;
-- bunny_recording_part_sources_v below offers them the verified location,
-- joined on (lecture_id, recording_id), without writing that table.
--
-- Purely additive: two tables and two views; nothing existing is altered.
--
-- One row per Graph callRecording of a lecture (its "recording part"), in
-- recording order, whatever its state: a part Graph reports but SharePoint
-- does not yet show is a NOT_FOUND row, so a missing part is visible rather
-- than absent. The source file is unique across ALL lectures: one SharePoint
-- file can never be the recording of two lectures (a shared LMS occurrence is
-- an explicit bunny_lms_occurrence_links mapping, 025, never a second copy).
--
-- No URLs: no webUrl, no download URL, no recordingContentUrl. A CHECK refuses
-- URL-like text in metadata.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.bunny_recording_discoveries (
    lecture_id                uuid PRIMARY KEY
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE CASCADE,
    discovery_version         text NOT NULL,
    status                    text NOT NULL,
    reason_code               text NOT NULL,
    review_reason             text,

    -- Coverage: Graph recordings found for THIS occurrence, and how many of
    -- them have a verified SharePoint file. NULL expected = Graph not read.
    expected_part_count       integer,
    located_part_count        integer NOT NULL DEFAULT 0,
    missing_part_count        integer,
    call_ids                  text[] NOT NULL DEFAULT '{}',
    transcript_pairing        text NOT NULL DEFAULT 'NOT_CHECKED',

    attempt_count             integer NOT NULL DEFAULT 0,
    first_checked_at          timestamptz NOT NULL DEFAULT now(),
    last_checked_at           timestamptz NOT NULL DEFAULT now(),
    next_check_after          timestamptz,
    located_at                timestamptz,
    last_verified_at          timestamptz,
    last_error_code           text,
    last_http_status          integer,
    metadata                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at                timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bunny_recording_discoveries_status CHECK (status IN (
        'WAITING', 'RETRY_SCHEDULED', 'LOCATED', 'REVIEW_REQUIRED', 'PERMANENT_FAILURE',
        'NOT_APPLICABLE')),
    CONSTRAINT bunny_recording_discoveries_pairing CHECK (transcript_pairing IN (
        'NOT_CHECKED', 'NO_TRANSCRIPT', 'PAIRED', 'PARTIAL', 'CONFLICT')),
    CONSTRAINT bunny_recording_discoveries_counts CHECK (
        located_part_count >= 0
        AND (expected_part_count IS NULL OR expected_part_count >= 0)
        AND (missing_part_count IS NULL OR missing_part_count >= 0)
        AND (expected_part_count IS NULL OR located_part_count <= expected_part_count)),
    -- LOCATED means EVERY part: never one part standing in for the lecture.
    CONSTRAINT bunny_recording_discoveries_located CHECK (
        status <> 'LOCATED' OR (expected_part_count >= 1
                                AND located_part_count = expected_part_count
                                AND missing_part_count = 0 AND located_at IS NOT NULL)),
    CONSTRAINT bunny_recording_discoveries_review CHECK (
        status <> 'REVIEW_REQUIRED' OR review_reason IS NOT NULL),
    CONSTRAINT bunny_recording_discoveries_metadata CHECK (
        jsonb_typeof(metadata) = 'object' AND metadata::text !~* '(https?://|tempauth|[?&]sig=)')
);

CREATE INDEX IF NOT EXISTS bunny_recording_discoveries_due_idx
    ON public.bunny_recording_discoveries (status, next_check_after);


CREATE TABLE IF NOT EXISTS public.bunny_recording_sources (
    recording_source_id       uuid PRIMARY KEY,
    lecture_id                uuid NOT NULL
        REFERENCES public.bunny_recording_discoveries(lecture_id) ON DELETE CASCADE,
    recording_part_index      integer NOT NULL,

    -- The Graph callRecording (metadata only).
    graph_recording_id        text NOT NULL,
    call_id                   text NOT NULL,
    content_correlation_id    text,
    recording_created_at      timestamptz NOT NULL,
    recording_end_at          timestamptz,

    -- The SharePoint / OneDrive MP4 holding it. Set once a file is matched.
    source_drive_id           text,
    source_item_id            text,
    source_filename           text,
    source_size_bytes         bigint,
    source_duration_seconds   numeric(12,3),
    source_modified_at        timestamptz,
    discovery_sources         text[] NOT NULL DEFAULT '{}',

    -- How it was proven.
    verification_method       text NOT NULL,
    file_name_lead_seconds    numeric(10,3),
    duration_difference_seconds numeric(12,3),
    media_origin              text,
    classification            text,
    locate_status             text NOT NULL,

    -- The transcript part of the same stretch of call, when one is stored.
    transcript_artifact_id    uuid
        REFERENCES public.lecture_transcript_artifacts(artifact_id) ON DELETE SET NULL,
    transcript_part_index     integer,
    transcript_pairing        text,

    located_at                timestamptz,
    last_checked_at           timestamptz NOT NULL DEFAULT now(),
    last_verified_at          timestamptz,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bunny_recording_sources_index CHECK (recording_part_index >= 1),
    CONSTRAINT bunny_recording_sources_status CHECK (locate_status IN (
        'LOCATED', 'NOT_FOUND', 'AMBIGUOUS', 'DURATION_PENDING', 'DURATION_MISMATCH',
        'SOURCE_MISSING', 'SOURCE_CHANGED')),
    CONSTRAINT bunny_recording_sources_pair CHECK (
        (source_drive_id IS NULL) = (source_item_id IS NULL)),
    CONSTRAINT bunny_recording_sources_located CHECK (
        locate_status <> 'LOCATED' OR (source_item_id IS NOT NULL AND source_duration_seconds > 0
                                       AND recording_end_at IS NOT NULL AND located_at IS NOT NULL
                                       AND media_origin IS NOT NULL)),
    CONSTRAINT bunny_recording_sources_origin CHECK (
        media_origin IS NULL OR media_origin IN ('graph_created_origin', 'file_name_origin')),
    CONSTRAINT bunny_recording_sources_classification CHECK (
        classification IS NULL OR classification IN ('FULL', 'PARTIAL', 'FRAGMENT', 'UNKNOWN')),
    CONSTRAINT bunny_recording_sources_transcript_pairing CHECK (
        transcript_pairing IS NULL OR transcript_pairing IN ('content_correlation_id', 'created_at')),
    CONSTRAINT bunny_recording_sources_times CHECK (
        recording_end_at IS NULL OR recording_end_at > recording_created_at),
    CONSTRAINT bunny_recording_sources_no_url CHECK (
        coalesce(source_filename, '') !~* '(https?://|tempauth|[?&]sig=)')
);

-- One row per part of a lecture.
CREATE UNIQUE INDEX IF NOT EXISTS bunny_recording_sources_part
    ON public.bunny_recording_sources (lecture_id, recording_part_index);
-- A Graph recording belongs to ONE lecture ...
CREATE UNIQUE INDEX IF NOT EXISTS bunny_recording_sources_recording_owner
    ON public.bunny_recording_sources (graph_recording_id);
-- ... and so does a SharePoint file.
CREATE UNIQUE INDEX IF NOT EXISTS bunny_recording_sources_file_owner
    ON public.bunny_recording_sources (source_drive_id, source_item_id)
    WHERE source_item_id IS NOT NULL;

COMMENT ON TABLE public.bunny_recording_discoveries IS
    'P0-C. One row per lecture: recording discovery state, part coverage, bounded retry schedule. No URLs.';
COMMENT ON TABLE public.bunny_recording_sources IS
    'P0-C. One row per Graph callRecording part of a lecture, with its verified SharePoint/OneDrive file. No URLs.';


-- Every eligible (ended, not cancelled, not an LI duplicate) lecture, with its
-- recording coverage. PENDING = never evaluated. The reconciler reads this.
CREATE OR REPLACE VIEW public.bunny_recording_coverage_v AS
SELECT l.lecture_id, l.session_date, l.scheduled_end,
       COALESCE(d.status, 'PENDING') AS recording_status,
       d.reason_code, d.review_reason, d.expected_part_count, d.located_part_count,
       d.missing_part_count, d.transcript_pairing, d.last_checked_at, d.last_verified_at,
       d.next_check_after,
       (SELECT count(*) FROM public.bunny_recording_sources s
         WHERE s.lecture_id = l.lecture_id AND s.locate_status <> 'LOCATED') AS unresolved_part_rows
  FROM public.lecture_sessions l
  LEFT JOIN public.bunny_recording_discoveries d ON d.lecture_id = l.lecture_id
 WHERE NOT l.is_cancelled
   AND l.metadata -> 'duplicate_suppression' IS NULL
   AND l.scheduled_end < now();

-- The verified location of each MEASURED part (017-019), without writing it.
CREATE OR REPLACE VIEW public.bunny_recording_part_sources_v AS
SELECT p.recording_part_id, p.lecture_id, p.part_index, p.recording_id,
       s.recording_source_id, s.recording_part_index, s.source_drive_id, s.source_item_id,
       s.located_at
  FROM public.lecture_recording_parts p
  JOIN public.bunny_recording_sources s
    ON s.lecture_id = p.lecture_id AND s.graph_recording_id = p.recording_id
 WHERE s.locate_status = 'LOCATED';

COMMIT;
