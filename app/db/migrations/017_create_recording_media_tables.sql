-- ===========================================================================
-- Migration 017 (Phase 6A): measured recording media, one row per part.
--
-- WHY THIS TABLE EXISTS AT ALL
-- ----------------------------
-- Phase 6A proved that a lecture's canonical transcript timeline does not map
-- onto a single recording file. Microsoft caps both a transcript and a
-- recording at four hours, so a long call produces SEVERAL transcript parts and
-- SEVERAL recordings - one recording per part - and consecutive recordings
-- overlap by about a minute.
--
-- Turning a transcript time into a media time therefore needs three facts per
-- part: where the part sits on the canonical timeline, which recording carries
-- it, and how long that recording really is. The first already exists in
-- lecture_transcript_selection_parts. The other two live here.
--
-- WHY THE DURATION IS STORED RATHER THAN READ EACH TIME
-- -----------------------------------------------------
-- Reading it means an authenticated range request against a multi-hundred-
-- megabyte file. Cutting one clip would pay for it twice, and a planner that
-- validates fifty boundaries would pay fifty times.
--
-- WHY IT IS 'MEASURED' AND NOT 'REPORTED'
-- ---------------------------------------
-- callRecording.endDateTime - createdDateTime happens to equal the real media
-- duration for every part measured in Phase 6A, but that is an observation,
-- not a contract, and the previous investigation stalled for a week on exactly
-- this kind of assumption. media_duration_source records HOW the number was
-- obtained so a future doubt can be settled by looking rather than guessing.
--
-- Nothing here stores a URL, a signed link or a token. A recording is
-- addressed by (user_id, meeting_id, recording_id) and the bytes are fetched
-- through the callRecording content route at the moment they are needed.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.lecture_recording_parts (
    recording_part_id        uuid PRIMARY KEY,
    lecture_id               uuid NOT NULL
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE CASCADE,

    -- Mirrors lecture_transcript_selection_parts.part_index, and the offset is
    -- that row's part_offset_ms. Copied rather than joined because the media
    -- timeline must stay readable even if a later selection supersedes this
    -- one; the selection_id below records which selection it came from.
    part_index               integer NOT NULL,
    canonical_offset_ms      bigint  NOT NULL,
    selection_id             uuid,

    -- How to fetch the bytes:
    --   GET /users/{user_id}/onlineMeetings/{meeting_id}
    --       /recordings/{recording_id}/content
    -- This is the callRecording content route. The driveItem route returns 403
    -- for this application registration and is deliberately not used.
    user_id                  text NOT NULL,
    meeting_id               text NOT NULL,
    recording_id             text NOT NULL,
    content_correlation_id   text,

    -- The measurement itself.
    media_duration_seconds   numeric(12,3) NOT NULL,
    media_start_time_seconds numeric(12,3),
    media_duration_source    text NOT NULL,
    container                text,
    video_codec              text,
    audio_codec              text,
    size_bytes               bigint,

    -- What Graph claimed, kept beside what was measured so a divergence is
    -- visible rather than silently resolved in favour of one of them.
    provider_created_at      timestamptz,
    provider_end_at          timestamptz,

    media_coordinate_version text NOT NULL,
    measured_at              timestamptz NOT NULL DEFAULT NOW(),
    created_at               timestamptz NOT NULL DEFAULT NOW(),
    updated_at               timestamptz NOT NULL DEFAULT NOW(),

    -- One recording per part of a lecture. A second row would mean two
    -- answers to "where is canonical time T", which is the ambiguity the
    -- whole transform exists to remove.
    CONSTRAINT lecture_recording_parts_identity UNIQUE (lecture_id, part_index),

    CONSTRAINT lecture_recording_parts_index_check
        CHECK (part_index >= 1),
    CONSTRAINT lecture_recording_parts_offset_check
        CHECK (canonical_offset_ms >= 0),
    -- A zero duration is what an unmeasured recording looks like. Letting one
    -- in would produce a timeline that accepts every request and yields
    -- nothing.
    CONSTRAINT lecture_recording_parts_duration_check
        CHECK (media_duration_seconds > 0),
    CONSTRAINT lecture_recording_parts_source_check
        CHECK (media_duration_source IN ('ffprobe', 'mp4_movie_header'))
);

CREATE INDEX IF NOT EXISTS lecture_recording_parts_lecture_idx
    ON public.lecture_recording_parts (lecture_id, part_index);

COMMENT ON TABLE public.lecture_recording_parts IS
    'Phase 6A. One recording per transcript part, with its MEASURED media duration. The input to the media coordinate transform. Stores no URL and no token.';
COMMENT ON COLUMN public.lecture_recording_parts.canonical_offset_ms IS
    'Where this part''s media time zero sits on the canonical transcript timeline. Equals lecture_transcript_selection_parts.part_offset_ms.';
COMMENT ON COLUMN public.lecture_recording_parts.media_duration_source IS
    'How the duration was obtained: ffprobe, or the MP4 movie header read over a range request. Never the Graph metadata window, and never the free-text meeting duration.';

COMMIT;
