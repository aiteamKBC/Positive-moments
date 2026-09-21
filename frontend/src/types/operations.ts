/**
 * Phase 5A: the Operations API contract, as TypeScript.
 *
 * These mirror what `app.orchestration` returns. They deliberately use the
 * platform's own vocabulary - COMPLETE, WAITING, NOT_APPLICABLE and the rest -
 * rather than inventing friendlier UI names, because a second vocabulary is a
 * second place for the meaning of "done" to drift.
 */

/** The seven stage states. WAITING is not a failure and never renders as one. */
export type StageState =
  | 'COMPLETE'
  | 'MISSING'
  | 'STALE'
  | 'FAILED'
  | 'REVIEW_REQUIRED'
  | 'WAITING'
  | 'NOT_APPLICABLE'

/** The day buckets. `suppressed_duplicate` is a calendar event, not a lecture. */
export type Bucket =
  | 'complete'
  | 'waiting'
  | 'review'
  | 'failed'
  | 'in_progress'
  | 'suppressed_duplicate'

export const STAGE_ORDER = [
  'DISCOVERY',
  'MEETING',
  'TRANSCRIPT',
  'SELECTION',
  'CANONICAL_CUES',
  'SPEAKERS',
  'ATTENDANCE',
  'ENGAGEMENT',
  'QA_EVALUATION',
  'QA_RENDER',
  'PERFECT_ELIGIBILITY',
  'LEGACY_QA_SYNC',
  'PERFECT_SYNC',
  'RECORDING_LINK',
  'EXCEL_SYNC',
] as const

export type StageName = (typeof STAGE_ORDER)[number]

/** One stage. Everything beyond `state` and `action` is stage-specific detail. */
export interface Stage {
  state: StageState
  action: string | null
  reason?: string | null
  [key: string]: unknown
}

export interface Navigation {
  date: string
  previous_date: string
  next_date: string
  today: string
  is_today: boolean
  is_future: boolean
  timezone: string
}

/** A row in the day's lecture table. */
export interface LectureRow {
  lecture_id: string
  subject: string
  session_date: string
  module: string | null
  scheduled_start: string | null
  bucket: Bucket
  next_action: string
  next_executable_action: string
  blocking_stage: string | null
  attendance_coverage_status: string | null
  attendance_source_authoritative: boolean
  perfect_reason: string | null
  reason_codes: Record<string, string>
  stages: Record<StageName, StageState>
  is_suppressed_duplicate: boolean
  duplicate_winner_lecture_id: string | null
}

export interface DayReport extends Navigation_Holder {
  session_date: string
  orchestration_version: string
  canonical_lecture_count: number
  business_lecture_count: number
  suppressed_duplicate_count: number
  complete_count: number
  waiting_count: number
  review_count: number
  failed_count: number
  in_progress_count: number
  legacy_synced_count: number
  legacy_not_coded_owned_count: number
  perfect_eligible_count: number
  perfect_pending_attendance_count: number
  perfect_synced_count: number
  recording_missing_count: number
  excel_pending_count: number
  attendance_waiting_count: number
  by_next_action: Record<string, number>
  by_stage: Record<StageName, Record<string, number>>
  lectures: LectureRow[]
}

interface Navigation_Holder {
  navigation: Navigation
}

export interface LectureListResponse extends Navigation_Holder {
  session_date: string
  business_lecture_count: number
  canonical_lecture_count: number
  suppressed_duplicate_count: number
  lectures: LectureRow[]
  suppressed_duplicates?: LectureRow[]
}

export interface RetryEligibility {
  retry_eligible: boolean
  retry_reason: string | null
  automated?: boolean
}

export interface ForceReprocessEligibility {
  semantics: string
  force_reprocess_eligible: boolean
  refusal_reasons: string[]
  available_to_scheduler: boolean
  requires_explicit_operator_action: boolean
}

export interface RunHistoryItem {
  [key: string]: unknown
}

export interface LectureDetail {
  lecture_id: string
  subject: string
  session_date: string
  module: string | null
  scheduled_start: string | null
  bucket: Bucket
  attendance_coverage_status: string | null
  attendance_source_authoritative: boolean
  is_suppressed_duplicate: boolean
  duplicate_resolution: DuplicateResolution | null
  orchestration_version: string
  stage_order: StageName[]
  stages: Record<StageName, Stage>
  next_action: string
  next_executable_action: string
  blocking_stage: string | null
  versions: Record<string, string | string[]>
  retry_eligibility: RetryEligibility
  force_reprocess_eligibility: ForceReprocessEligibility
  run_history: RunHistoryItem[]
}

export interface PipelineRun {
  run_id: string
  [key: string]: unknown
}

export interface RunsResponse {
  count: number
  runs: PipelineRun[]
}

export interface LectureListSlice {
  session_date: string
  count: number
  lectures: LectureRow[]
}

/**
 * Descriptive registry metadata, from `/operations/directory/`.
 *
 * It carries no pipeline state on purpose: the Lectures workspace joins it to
 * the day report on `lecture_id` rather than inferring a status from it.
 */
export interface DirectoryEntry {
  lecture_id: string
  subject: string
  module: string | null
  session_date: string
  scheduled_start: string | null
  scheduled_end: string | null
  legacy_session_id: string | null
  legacy_session_key: string | null
  trainer: string | null
  clips_status: string | null
  positive_clips_count: number | null
  clips_analysis_completeness: string | null
  recording_url: string | null
  recording_link_status: string | null
}

export interface DirectoryResponse {
  date_from: string
  date_to: string
  count: number
  lectures: DirectoryEntry[]
}

export interface CalendarDay {
  session_date: string
  lecture_count: number
  suppressed_duplicate_count: number
  business_lecture_count: number
}

export interface CalendarResponse {
  date_from: string
  date_to: string
  timezone: string
  /** The most recent day on or before today that actually carries lectures. */
  latest_lecture_date: string | null
  days: CalendarDay[]
}

export interface DuplicateResolution {
  case: string
  role: string
  winner_lecture_id: string | null
  winner_meeting_id?: string | null
  requires_manual_review?: boolean
  duplicate_resolution_version?: string | null
  resolved_at?: string | null
}

/**
 * Phase 6E: delivered media.
 *
 * `clip_status` is the ASSET's status - it is only ever "completed" when the
 * worker uploaded the file. `job_status` is the queue's separate opinion.
 * They are deliberately not merged: a queued job is not a clip, and the UI
 * must never report "ready" from a plan.
 */
export interface PositiveMomentMedia {
  clip_index: number
  source_start: string | null
  source_end: string | null
  speaker: string | null
  category: string | null
  quote: string | null
  reason: string | null
  clip_status: string | null
  /** Durable SharePoint link, or null. Never a signed or temporary URL. */
  clip_url: string | null
  clip_duration_seconds: number | null
  job_status: string | null
  job_attempts: number | null
  job_error: string | null
  job_updated_at: string | null
}

export interface LecturePartMedia {
  part_number: number
  status: string
  attempt_count: number
  error: string | null
  media_start_seconds: number
  media_end_seconds: number
  output_web_url: string | null
  output_size_bytes: number | null
  completed_at: string | null
  part_title: string | null
  canonical_start_seconds: string | null
  canonical_end_seconds: string | null
  split_version: string | null
  media_coordinate_version: string | null
}

export interface SplitPlanSummary {
  split_version: string
  planner_status: string
  planner_model: string | null
  planner_confidence: number | null
  cut_1_seconds: number | null
  cut_2_seconds: number | null
  part_1_title: string | null
  part_2_title: string | null
  part_3_title: string | null
}

export interface LectureMedia {
  lecture_id: string
  legacy_session_id: string | null
  positive_moments: PositiveMomentMedia[]
  lecture_parts: LecturePartMedia[]
  split_plan: SplitPlanSummary | null
}

export interface MediaJob {
  job_id: number
  job_type: string
  part_number: number | null
  status: string
  attempt_count: number
  max_attempts: number
  cut_mode: string
  output_filename: string | null
  output_web_url: string | null
  output_size_bytes: number | null
  created_at: string | null
  started_at: string | null
  completed_at: string | null
  not_before: string | null
  error: string | null
  subject: string | null
  trainer: string | null
  lecture_date: string | null
  requested_duration_seconds: number | null
}

export interface MediaJobsResponse {
  counts: {
    by_status: Record<string, number>
    by_type: Record<string, Record<string, number>>
  }
  jobs: MediaJob[]
}
