-- ===========================================================================
-- PROPOSED migration 024: Bunny Stream video and caption tracking.
--
-- STATUS: PROPOSED, NOT APPROVED FOR PRODUCTION. It lives outside
-- app/db/migrations/ on purpose: tools/audit_qa_core_production.py derives its
-- "every migration table exists" contract from that folder, and
-- tools/bootstrap_integration_db.py applies everything in it. Moving this file
-- into app/db/migrations/ is the approval step; applying it to production is a
-- separate, explicit step. Rollback: 024_create_bunny_stream_tables.rollback.sql.
--
-- Purely additive. It creates three tables and alters nothing that exists. No
-- QA, Positive Moments, Case Study or Social Media table is touched.
--
-- WHY NEW TABLES
-- --------------
-- Production (inspected read-only 2026-10-21, 208 tables) has no Bunny,
-- streaming, caption or external-video table. The nearest concept,
-- positive_moment_media_assets, is a delivered CLIP keyed to a moment and a
-- SharePoint destination; a whole-recording Bunny import is a different thing.
-- The lecture and recording registries are REUSED, not duplicated:
--   lecture_sessions (lecture_id)          - the lecture
--   lecture_recording_links / qa_doctors_sessions (drive, item) - the file
--   lecture_transcript_artifact_contents (artifact_id, version_number) - text
--
-- IDENTITY
-- --------
-- The authoritative SOURCE identity of an import is
--     (bunny_library_id, recording_drive_id, recording_item_id)
-- among LIVE rows (retired_at IS NULL), and the authoritative DESTINATION
-- identity is
--     (bunny_library_id, bunny_video_id).
-- Amended for P0-A (2026-10-21, audit section 11): a confirmed-failed import
-- is RETIRED and retried as a new row, so source/part/token uniqueness are
-- partial indexes over live rows; visibility_state and bunny_status_history
-- were added. Still unapplied everywhere except rolled-back test transactions.
-- The title is a human-readable, deterministic label; it is never a key.
-- bunny_video_record_id is uuidv5 of the source identity, so a pilot manifest
-- row and its later database row carry the same primary key.
--
-- Bunny's URL Fetch answers without a video id. A row therefore exists, with
-- import_status SUBMITTING, BEFORE the fetch is sent, and bunny_video_id is
-- written the moment reconciliation identifies the video. A run that dies in
-- between finds SUBMITTING/SUBMISSION_UNCERTAIN and reconciles; it never
-- submits a second fetch blindly.
--
-- Nothing here stores a URL, a temporary download link, an API key or
-- transcript text.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.bunny_videos (
    bunny_video_record_id     uuid PRIMARY KEY,
    lecture_id                uuid NOT NULL
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE RESTRICT,
    recording_part_index      integer NOT NULL DEFAULT 1,
    recording_drive_id        text NOT NULL,
    recording_item_id         text NOT NULL,
    -- 'rec-' + first 8 hex of sha256(drive_id || 0x1f || item_id).
    recording_identity        text NOT NULL,

    bunny_library_id          bigint NOT NULL,
    bunny_video_id            uuid,
    video_title               text NOT NULL,

    import_method             text NOT NULL DEFAULT 'URL_FETCH',
    import_status             text NOT NULL,
    encoding_status           text NOT NULL DEFAULT 'NOT_STARTED',
    bunny_status_code         smallint,
    encode_progress           smallint,

    -- What Microsoft reported for the source file (driveItem metadata).
    source_file_name          text,
    source_mime_type          text,
    source_size_bytes         bigint,
    source_duration_seconds   numeric(12,3),

    -- What Bunny reports once encoding finishes.
    bunny_length_seconds      integer,
    bunny_storage_size_bytes  bigint,
    bunny_width               integer,
    bunny_height              integer,
    encoding_tier             text,
    output_codecs             text,
    available_resolutions     text,
    media_verification        jsonb NOT NULL DEFAULT '{}'::jsonb,

    import_submitted_at       timestamptz,
    video_identified_at       timestamptz,
    encoding_completed_at     timestamptz,
    media_verified_at         timestamptz,

    attempt_count             integer NOT NULL DEFAULT 0,
    last_error_code           text,
    last_error                text,
    next_attempt_after        timestamptz,

    -- A CONFIRMED-failed import (Bunny 400/422 on Fetch, status 5/6 after
    -- identification) is retired, never deleted or edited back to PENDING; the
    -- retry is a NEW row. The live-row uniqueness below ignores retired rows.
    retired_at                timestamptz,
    retire_reason             text,
    -- Every Bunny status code observed, oldest first: [{"code": 3, "at": ...}].
    bunny_status_history      jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- Learner visibility. READY_FOR_LMS is set only when the video, its current
    -- caption and its LMS match are all verified (application rule); the CHECK
    -- below enforces the part this table can see.
    visibility_state          text NOT NULL DEFAULT 'HIDDEN',

    -- Structured audit only. Never a URL, token, key or transcript text.
    metadata                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),

    -- One Bunny video -> one Microsoft recording, retired or not. Also the FK
    -- target below. (Source, part and token uniqueness are partial indexes
    -- over live rows, after the table.)
    CONSTRAINT bunny_videos_bunny_identity
        UNIQUE (bunny_library_id, bunny_video_id),

    CONSTRAINT bunny_videos_retired CHECK (
        (retired_at IS NULL) = (retire_reason IS NULL)),
    CONSTRAINT bunny_videos_visibility CHECK (
        visibility_state IN ('HIDDEN', 'READY_FOR_LMS', 'WITHDRAWN')),
    CONSTRAINT bunny_videos_visible_only_when_verified CHECK (
        visibility_state <> 'READY_FOR_LMS'
        OR (encoding_status = 'VERIFIED' AND retired_at IS NULL)),
    CONSTRAINT bunny_videos_status_history CHECK (
        jsonb_typeof(bunny_status_history) = 'array'),
    CONSTRAINT bunny_videos_no_urls CHECK (
        metadata::text !~* '(https?://|tempauth|[?&]sig=)'),

    CONSTRAINT bunny_videos_identity_format
        CHECK (recording_identity ~ '^rec-[0-9a-f]{8}$'),
    CONSTRAINT bunny_videos_part_index
        CHECK (recording_part_index >= 1),
    CONSTRAINT bunny_videos_title
        CHECK (length(video_title) BETWEEN 1 AND 200 AND position(recording_identity IN video_title) > 0),
    CONSTRAINT bunny_videos_import_method
        CHECK (import_method IN ('URL_FETCH')),
    CONSTRAINT bunny_videos_import_status CHECK (import_status IN (
        'PENDING', 'SUBMITTING', 'SUBMITTED', 'SUBMISSION_UNCERTAIN',
        'IDENTIFIED', 'FAILED', 'REVIEW_REQUIRED')),
    CONSTRAINT bunny_videos_encoding_status CHECK (encoding_status IN (
        'NOT_STARTED', 'IN_PROGRESS', 'FINISHED', 'VERIFIED', 'FAILED', 'REVIEW_REQUIRED')),
    -- A video that is identified or encoding must have its Bunny id recorded.
    CONSTRAINT bunny_videos_id_when_identified CHECK (
        bunny_video_id IS NOT NULL
        OR (import_status NOT IN ('IDENTIFIED') AND encoding_status = 'NOT_STARTED')),
    CONSTRAINT bunny_videos_status_code
        CHECK (bunny_status_code IS NULL OR bunny_status_code BETWEEN 0 AND 8),
    CONSTRAINT bunny_videos_progress
        CHECK (encode_progress IS NULL OR encode_progress BETWEEN 0 AND 100),
    CONSTRAINT bunny_videos_attempts CHECK (attempt_count >= 0)
);

-- One Microsoft recording -> at most one LIVE Bunny video per library.
CREATE UNIQUE INDEX IF NOT EXISTS bunny_videos_live_source_identity
    ON public.bunny_videos (bunny_library_id, recording_drive_id, recording_item_id)
    WHERE retired_at IS NULL;
-- Several recordings of one lecture stay distinct by part (live rows).
CREATE UNIQUE INDEX IF NOT EXISTS bunny_videos_live_part_identity
    ON public.bunny_videos (bunny_library_id, lecture_id, recording_part_index)
    WHERE retired_at IS NULL;
-- Reconciliation finds a fetched video by this token in its title, so a
-- (2^-32) collision between live rows must be a hard failure, never an
-- ambiguous match. A retired row keeps its token; reconciliation must skip
-- the bunny_video_ids of retired rows.
CREATE UNIQUE INDEX IF NOT EXISTS bunny_videos_live_identity_token
    ON public.bunny_videos (bunny_library_id, recording_identity)
    WHERE retired_at IS NULL;

CREATE INDEX IF NOT EXISTS bunny_videos_lecture_idx
    ON public.bunny_videos (lecture_id, recording_part_index);
CREATE INDEX IF NOT EXISTS bunny_videos_due_idx
    ON public.bunny_videos (import_status, encoding_status, next_attempt_after);

COMMENT ON TABLE public.bunny_videos IS
    'One Bunny Stream video per Microsoft recording file per library. Source identity (library, drive, item); destination identity (library, video id). No URLs, keys or transcript text.';
COMMENT ON COLUMN public.bunny_videos.bunny_video_id IS
    'Bunny video GUID. NULL only until URL Fetch reconciliation identifies the video; written immediately when known.';
COMMENT ON COLUMN public.bunny_videos.video_title IS
    'Deterministic label "{subject} | {date} | {lecture8} | {rec-xxxxxxxx}". Never a key.';


-- ---------------------------------------------------------------------------
-- Caption publications: every distinct caption ever prepared for a video and
-- language, with its exact provenance. History is append-only by design; a
-- changed source or transform is a NEW row, never an edit of an old one.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.bunny_caption_publications (
    publication_id            uuid PRIMARY KEY,
    bunny_library_id          bigint NOT NULL,
    bunny_video_id            uuid NOT NULL,
    lecture_id                uuid NOT NULL
        REFERENCES public.lecture_sessions(lecture_id) ON DELETE RESTRICT,
    language_code             text NOT NULL,
    caption_label             text NOT NULL,

    source_selection_id       uuid
        REFERENCES public.lecture_transcript_selections(selection_id) ON DELETE RESTRICT,
    source_artifact_id        uuid NOT NULL,
    source_content_version    integer NOT NULL,
    source_content_sha256     text NOT NULL,

    caption_sha256            text NOT NULL,
    caption_bytes             integer NOT NULL,
    transform_version         text NOT NULL,
    alignment_version         text,
    alignment_method          text,
    applied_offset_ms         bigint NOT NULL,
    source_cue_count          integer NOT NULL,
    published_cue_count       integer NOT NULL,
    removed_voice_tag_count   integer NOT NULL DEFAULT 0,

    publication_status        text NOT NULL,
    attempt_count             integer NOT NULL DEFAULT 0,
    last_error_code           text,
    last_error                text,
    next_attempt_after        timestamptz,
    -- Bunny's CaptionValidationModel: valid, errorList, warningList.
    bunny_validation          jsonb NOT NULL DEFAULT '{}'::jsonb,
    bunny_caption_version     integer,
    readback                  jsonb NOT NULL DEFAULT '{}'::jsonb,

    upload_started_at         timestamptz,
    published_at              timestamptz,
    verified_at               timestamptz,
    superseded_at             timestamptz,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT bunny_caption_publications_video_fk
        FOREIGN KEY (bunny_library_id, bunny_video_id)
        REFERENCES public.bunny_videos (bunny_library_id, bunny_video_id)
        ON DELETE RESTRICT,
    -- The exact stored transcript version the caption was built from.
    CONSTRAINT bunny_caption_publications_source_fk
        FOREIGN KEY (source_artifact_id, source_content_version)
        REFERENCES public.lecture_transcript_artifact_contents (artifact_id, version_number)
        ON DELETE RESTRICT,
    -- Same video, language, bytes and transform = the same publication.
    CONSTRAINT bunny_caption_publications_idempotency
        UNIQUE (bunny_library_id, bunny_video_id, language_code, caption_sha256, transform_version),
    -- FK target for the current-caption pointer (ties pointer to its video+lang).
    CONSTRAINT bunny_caption_publications_pointer_key
        UNIQUE (publication_id, bunny_library_id, bunny_video_id, language_code),

    CONSTRAINT bunny_caption_publications_language
        CHECK (language_code ~ '^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$'),
    CONSTRAINT bunny_caption_publications_sha CHECK (
        source_content_sha256 ~ '^[0-9a-f]{64}$' AND caption_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT bunny_caption_publications_counts CHECK (
        caption_bytes > 0 AND source_cue_count >= 0 AND published_cue_count >= 0
        AND removed_voice_tag_count >= 0 AND attempt_count >= 0 AND source_content_version > 0),
    CONSTRAINT bunny_caption_publications_status CHECK (publication_status IN (
        'PREPARED', 'UPLOADING', 'UPLOAD_UNCERTAIN', 'PUBLISHED', 'VERIFIED',
        'REJECTED', 'FAILED', 'SUPERSEDED', 'REVIEW_REQUIRED')),
    CONSTRAINT bunny_caption_publications_published_at CHECK (
        publication_status NOT IN ('PUBLISHED', 'VERIFIED') OR published_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS bunny_caption_publications_video_idx
    ON public.bunny_caption_publications (bunny_library_id, bunny_video_id, language_code, created_at DESC);
CREATE INDEX IF NOT EXISTS bunny_caption_publications_source_idx
    ON public.bunny_caption_publications (source_artifact_id, source_content_version);

COMMENT ON TABLE public.bunny_caption_publications IS
    'Every caption prepared for a Bunny video+language, with source artifact version, hashes, transform and offset. History; the live one is named by bunny_caption_current.';


-- ---------------------------------------------------------------------------
-- The CURRENT caption per video and language, separate from the history. One
-- row per (library, video, language); switching versions is one UPDATE of
-- publication_id. The composite FK guarantees the pointer can only name a
-- publication of the SAME video and language. That the named publication is
-- PUBLISHED/VERIFIED is an application invariant (asserted in the writer).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.bunny_caption_current (
    bunny_library_id          bigint NOT NULL,
    bunny_video_id            uuid NOT NULL,
    language_code             text NOT NULL,
    publication_id            uuid NOT NULL,
    made_current_at           timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (bunny_library_id, bunny_video_id, language_code),
    CONSTRAINT bunny_caption_current_publication UNIQUE (publication_id),
    CONSTRAINT bunny_caption_current_fk
        FOREIGN KEY (publication_id, bunny_library_id, bunny_video_id, language_code)
        REFERENCES public.bunny_caption_publications
            (publication_id, bunny_library_id, bunny_video_id, language_code)
        ON DELETE RESTRICT
);

COMMENT ON TABLE public.bunny_caption_current IS
    'Which caption publication is live on Bunny for each video and language. History stays in bunny_caption_publications.';

COMMIT;
