/** Historical Backfill (Publishing Monitor): the shapes of operations/publisher_backfill_views.py. */

export interface BackfillRecording {
  status: string
  reason?: string | null
  expected_parts?: number | null
  located_parts?: number | null
  verified_count: number
  candidates: number
  selected_part: number | null
  selection_rule: string | null
  selected_duration_seconds: number | null
}

export interface BackfillPreviewRow {
  lecture_id: string
  subject: string | null
  module: string | null
  session_date: string
  scheduled_start: string | null
  scheduled_end: string | null
  lms: { status: string; occurrence_id?: string | null }
  recording: BackfillRecording
  bunny: { status: string; video: string } | null
  release: string | null
  playback: { available: boolean; reason: string | null } | null
  monitor_state: string | null
  category: string
  proposed_action: string
  reason: string
  data_source: string
}

export interface BackfillPreviewSummary {
  total: number
  eligible: number
  already_published: number
  already_processing: number
  blocked: number
  skipped: number
  categories: Record<string, number>
  estimated_imports: { known: number; possible: number; upper_bound: number }
  known_selected_duration_seconds: number
  per_day: Record<string, { total: number; eligible: number }>
}

export interface BackfillPreview {
  date_from: string
  date_to: string
  days: number
  library_id: number
  generated_at: string
  data_source: string
  summary: BackfillPreviewSummary
  lectures: BackfillPreviewRow[]
  fingerprint: string
  preview_token: string
  token_ttl_seconds: number
  read_only: boolean
}

export interface BackfillCounts {
  total: number
  pending: number
  processing: number
  completed: number
  skipped: number
  blocked: number
  failed: number
  cancelled: number
}

export interface BackfillRun {
  run_id: string
  date_from: string
  date_to: string
  library_id: number
  status: string
  requested_by: string
  reason: string
  preview_summary: Partial<BackfillPreviewSummary>
  lms_status: string | null
  last_tick_at: string | null
  last_tick: Record<string, unknown>
  cancel_requested_at: string | null
  cancel_requested_by: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  counts?: BackfillCounts
  is_finished: boolean
  cancel_requested: boolean
}

export interface BackfillRuns {
  runs: BackfillRun[]
  active: BackfillRun | null
  operator: boolean
  library_id: number
  max_days: number
  today: string
  timezone: string
}

export interface PipelineStep {
  key: string
  label: string
  status: string
  done: boolean
  failed: boolean
}

export interface BackfillRunLecture {
  lecture_id: string
  session_date: string
  status: string
  initial_category: string
  reason: string | null
  stage: string | null
  retries: number
  started_at: string | null
  finished_at: string | null
  updated_at: string | null
  subject: string | null
  module: string | null
  scheduled_start: string | null
  scheduled_end: string | null
  monitor_state: string | null
  monitor_reason: string | null
  pipeline: PipelineStep[]
}

export interface BackfillRunDetail {
  run: BackfillRun
  lectures: BackfillRunLecture[]
  generated_at: string
  operator: boolean
}

export interface BackfillRetryResult {
  run_id: string
  lectures_reopened: number
  jobs_reopened: number
  refused: { lecture_id: string; job_kind: string; code: string }[]
  lms_rematch: boolean
}
