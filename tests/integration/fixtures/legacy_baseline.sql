-- ===========================================================================
-- TEST-ONLY legacy baseline for the isolated integration database.
--
-- The platform migrations (app/db/migrations/001-020) never create the legacy
-- n8n tables; production has them already. An empty test database does not,
-- so this file creates the minimum shape the platform reads and writes:
--
--   qa_doctors_sessions, qa_doctors_checklist_items, qa_perfect_lectures,
--   qa_doctors_transcripts, qa_media_jobs, qa_positive_clip_assets
--
-- plus the two externally owned, read-only upstream sources the platform
-- SELECTs from, so attendance and rendering contracts can run on synthetic
-- rows rather than production evidence:
--
--   kbc_attendance, kbc_users_data
--
-- It is derived from the platform's own column contracts
-- (app/writer/mapping.py SESSION_COLUMNS / CHECKLIST_COLUMNS,
-- app/writer/perfect_mapping.py PERFECT_COLUMNS, app/db/repositories/*), and
-- from the automation migrations that ALTER these tables. It is NOT the
-- authoritative production DDL and is never applied anywhere but a database
-- that tools/integration_db_guard.py has proven to be a test database.
--
-- Applied BEFORE automation migrations 050/051/100/101, which extend it.
-- ===========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.qa_doctors_sessions (
    session_id                   text PRIMARY KEY,
    not_met_count                integer,
    partial_count                integer,
    met_count                    integer,
    meeting_id                   text,
    trainer                      text,
    duration                     text,
    "Engagement"                 numeric,
    date                         date,
    subject                      text,
    lms_students_count           integer,
    lms_module                   text,
    lms_students                 jsonb,
    duration_score               integer,
    engagement_score             integer,
    ksb_coverage                 jsonb,
    strengths                    jsonb,
    areas_for_development        jsonb,
    overall_judgement            text,
    teaching_quality_rating      integer,
    teaching_quality_comments    text,
    cancelled_session            text,
    cancellation_reason          text,
    -- owned by the Positive Clips workflow
    positive_clips               jsonb,
    clips_status                 text,
    clips_count                  integer,
    clips_analysis_completeness  text,
    -- owned by the recording-link workflow
    recording_url                text,
    recording_link_status        text,
    recording_item_id            text,
    recording_drive_id           text,
    recording_id                 text,
    recap_url                    text,
    transcript_url               text,
    created_at                   timestamptz NOT NULL DEFAULT now(),
    updated_at                   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.qa_doctors_checklist_items (
    session_id_match   text PRIMARY KEY,
    -- The checklist rows CASCADE with their session: app/db/repositories/
    -- qa_writer.delete_owned_session deletes only the session row and relies
    -- on the database to remove the eleven rows with it.
    session_id         text NOT NULL
        REFERENCES public.qa_doctors_sessions(session_id) ON DELETE CASCADE,
    checklist_item     text,
    status             text,
    checklist_order    integer,
    evidence           text,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS qa_doctors_checklist_items_session_idx
    ON public.qa_doctors_checklist_items (session_id);

CREATE TABLE IF NOT EXISTS public.qa_perfect_lectures (
    -- Column ORDER matters: tests/integration/test_perfect_lecture_persistence
    -- asserts the exact ordinal shape production has.
    id              bigserial PRIMARY KEY,
    lecture_key     text NOT NULL UNIQUE,
    session_date    date NOT NULL,
    subject         text NOT NULL,
    module          text,
    trainer         text,
    engagement      text,
    attended_count  integer,
    met_count       integer NOT NULL,
    recording_url   text,
    recap_url       text,
    detected_at     timestamptz NOT NULL DEFAULT now(),
    meeting_id      text,
    session_id      text,
    excel_synced_at timestamptz
);

CREATE TABLE IF NOT EXISTS public.qa_doctors_transcripts (
    id          bigserial PRIMARY KEY,
    session_id  text,
    transcript  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.qa_media_jobs (
    job_id                    bigserial PRIMARY KEY,
    job_key                   text UNIQUE,
    job_type                  text NOT NULL,
    session_id                text NOT NULL,
    clip_key                  text,
    part_number               integer,
    status                    text NOT NULL DEFAULT 'pending',
    attempt_count             integer NOT NULL DEFAULT 0,
    error                     text,
    not_before                timestamptz,
    start_seconds             numeric(12,3),
    end_seconds               numeric(12,3),
    output_web_url            text,
    output_size_bytes         bigint,
    metadata                  jsonb NOT NULL DEFAULT '{}'::jsonb,
    completed_at              timestamptz,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.qa_positive_clip_assets (
    asset_id          bigserial PRIMARY KEY,
    session_id        text NOT NULL,
    source_start      text,
    source_end        text,
    trim_status       text,
    clip_url          text,
    duration_seconds  numeric(12,3),
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- Externally owned, READ-ONLY upstream sources. The platform only ever SELECTs
-- from these two; they are created here so that attendance, engagement and
-- rendering contracts can be exercised against synthetic rows instead of
-- production evidence. Column names and quoting reproduce what the platform's
-- SQL asks for (app/db/repositories/attendance_resolution.py and
-- app/db/repositories/qa_rendering.py).
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.kbc_attendance (
    row_id             bigserial PRIMARY KEY,
    "ID"               text,
    "FullName"         text,
    "Email"            text,
    "Attendance"       integer,
    module             text,
    date               date,
    attendance_status  text
);
CREATE INDEX IF NOT EXISTS kbc_attendance_date_module_idx
    ON public.kbc_attendance (date, module);

CREATE TABLE IF NOT EXISTS public.kbc_users_data (
    row_id           bigserial PRIMARY KEY,
    "ID"             text,
    "FullName"       text,
    "Group"          text,
    "Program-Status" text
);

-- --------------------------------------------------------------------------
-- Externally owned, READ-ONLY upstream sources. The platform only ever SELECTs
-- from these two; they are created here so that attendance, engagement and
-- rendering contracts can be exercised against synthetic rows instead of
-- production evidence. Column names and quoting reproduce what the platform's
-- SQL asks for (app/db/repositories/attendance_resolution.py and
-- app/db/repositories/qa_rendering.py).
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.kbc_attendance (
    row_id             bigserial PRIMARY KEY,
    "ID"               text,
    "FullName"         text,
    "Email"            text,
    "Attendance"       integer,
    module             text,
    date               date,
    attendance_status  text
);
CREATE INDEX IF NOT EXISTS kbc_attendance_date_module_idx
    ON public.kbc_attendance (date, module);

CREATE TABLE IF NOT EXISTS public.kbc_users_data (
    row_id           bigserial PRIMARY KEY,
    "ID"             text,
    "FullName"       text,
    "Group"          text,
    "Program-Status" text
);

COMMIT;
