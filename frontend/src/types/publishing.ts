/**
 * Lecture Publishing Monitor API shapes (backend/operations/publishing_monitor_views.py,
 * app/publisher/monitor.py). Every status is a backend token; the page only presents it.
 */

export interface PublishingKpi {
  key: string
  label: string
  definition: string
  count: number
}

export interface PublishingSummary {
  date_from: string
  date_to: string
  generated_at: string
  library_id: number
  playback_enabled: boolean
  total: number
  kpis: PublishingKpi[]
  states: Record<string, number>
  playable_without_captions: number
}

export interface PublishingRow {
  lecture_id: string
  session_date: string
  scheduled_start: string | null
  scheduled_end: string | null
  subject: string | null
  module: string | null
  lms: { status: string; occurrence_id: string | null; link_current: boolean }
  recording: { status: string; reason: string | null; expected_parts: number | null; located_parts: number | null }
  bunny: { status: string; parts: number }
  video: { status: string }
  transcript: { status: string; retry_at: string | null }
  captions: { status: string }
  release: { status: string; released_at: string | null }
  playback: { available: boolean; reason: string | null }
  state: string
  reason: string
  needs_attention: boolean
  automatic: boolean
  retry_at: string | null
  updated_at: string | null
}

export interface PublishingPage {
  date_from: string
  date_to: string
  generated_at: string
  range_total: number
  total: number
  page: number
  page_size: number
  results: PublishingRow[]
}

export interface WorkerHealth {
  name: string
  state: string
  seen_at: string
  age_seconds: number
}

export interface TaskHealth {
  last_started_at: string | null
  last_finished_at: string | null
  last_succeeded_at: string | null
  last_status: string | null
  started_age_seconds: number | null
  detail: Record<string, unknown>
}

export interface PublisherHealth {
  checked_at: string
  status: 'HEALTHY' | 'DEGRADED' | 'STALE' | 'STOPPED' | 'UNKNOWN'
  library_id: number | null
  heartbeat_library_id?: number | null
  playback_enabled: boolean
  mode: string | null
  daemon_version: string | null
  scheduler: { state: string; seen_at: string; age_seconds: number; started_at: string } | null
  workers: WorkerHealth[]
  workers_healthy: number
  workers_seen: number
  tasks: Record<string, TaskHealth>
  auto_release: 'RUNNING_RECENTLY' | 'NOT_RUNNING_RECENTLY' | 'NEVER_RAN'
  open_alerts: Record<string, number>
}

export interface PublishingFilters {
  q?: string
  kpi?: string
  state?: string
  video?: string
  playback?: string
  captions?: string
  transcript?: string
  attention?: boolean
}

export interface DeliveryPart {
  sequence: number
  source_part_index: number
  classification: string | null
  duration_seconds: number | null
  bunny: { library_id: number; video_id: string } | null
  encoding: string
  resolutions: string[]
  visibility: string | null
  captions: { language: string; label: string; status: string; version: number | null; content_verified: boolean }[]
}

export interface TimelineEvent {
  event_id: number
  job_id: string | null
  event: string
  actor: string
  detail: Record<string, unknown>
  at: string
}

export interface PublishingJob {
  job_id: string
  job_kind: string
  subject_key: string
  status: string
  attempt_count: number
  max_attempts: number
  next_attempt_at: string | null
  last_error_code: string | null
  created_at: string
  updated_at: string
  finished_at: string | null
}

export interface PublishingDetail {
  row: PublishingRow
  lms: {
    match: { status: string; rule_version: string | null; requires_review: boolean; last_evaluated_at: string | null } | null
    links: { occurrence_id: string; live_session_id: string | null; module_catalogue_id: string | null
             session_number: number | null; link_role: string; is_current: boolean }[]
  }
  recording: {
    discovery: Record<string, string | number | null> | null
    parts: { part_index: number; locate_status: string; classification: string | null; recorded_start: string | null
             recorded_end: string | null; duration_seconds: number | null; transcript_paired: boolean }[]
  }
  delivery: {
    state: string
    blocking: string[]
    caption_status: string
    release: string
    released_at: string | null
    withdrawn_at: string | null
    parts: DeliveryPart[]
    excluded_parts: { source_part_index: number; reason: string }[]
  } | null
  videos: { recording_part_index: number; bunny_video_id: string | null; import_status: string
            encoding_status: string; visibility_state: string; last_error_code: string | null
            attempt_count: number }[]
  transcript: { selection_status: string | null; status: string }
  jobs: PublishingJob[]
  timeline: TimelineEvent[]
  library_id: number
}
