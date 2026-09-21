BEGIN;

CREATE TABLE IF NOT EXISTS public.qa_media_jobs (
  job_id bigserial PRIMARY KEY,
  job_key text NOT NULL UNIQUE,
  session_id text NOT NULL,
  job_type text NOT NULL CHECK (job_type IN ('positive_clip', 'lecture_part')),
  part_number integer,
  clip_key text,

  start_seconds numeric(12,3) NOT NULL CHECK (start_seconds >= 0),
  end_seconds numeric(12,3) NOT NULL CHECK (end_seconds > start_seconds),
  cut_mode text NOT NULL CHECK (cut_mode IN ('precise', 'fast_copy')),
  output_filename text NOT NULL,

  source_drive_id text NOT NULL,
  source_item_id text NOT NULL,
  destination_drive_id text NOT NULL,
  destination_folder_item_id text NOT NULL,

  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts >= 1),
  not_before timestamptz NOT NULL DEFAULT NOW(),

  locked_by text,
  locked_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,

  output_drive_id text,
  output_item_id text,
  output_web_url text,
  output_size_bytes bigint,
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT NOW(),
  updated_at timestamptz NOT NULL DEFAULT NOW(),

  CONSTRAINT qa_media_jobs_part_number_check CHECK (
    (job_type = 'lecture_part' AND part_number BETWEEN 1 AND 3)
    OR (job_type = 'positive_clip' AND part_number IS NULL)
  )
);

CREATE INDEX IF NOT EXISTS qa_media_jobs_pending_idx
  ON public.qa_media_jobs (status, not_before, created_at)
  WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS qa_media_jobs_session_idx
  ON public.qa_media_jobs (session_id, job_type, created_at DESC);

CREATE INDEX IF NOT EXISTS qa_media_jobs_processing_idx
  ON public.qa_media_jobs (locked_at)
  WHERE status = 'processing';

COMMIT;
