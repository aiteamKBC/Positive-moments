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
  recording_link?: RecordingLinkDetail
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

/* ---------------------------------------------------------------------------
 * QA Core RC2: Operations Backfill.
 *
 * A backfill is a durable RUN, not a request/response: one September day costs
 * seconds of Microsoft Graph, so both Preview and Start queue work and the
 * console polls. `mode` is what distinguishes a read-only inspection from a
 * real recovery; everything else about the two is identical.
 * ------------------------------------------------------------------------- */

export type BackfillMode = 'PREVIEW' | 'EXECUTE'

export type BackfillStatus =
  | 'PENDING' | 'RUNNING' | 'COMPLETED' | 'CANCEL_REQUESTED'
  | 'CANCELLED' | 'FAILED' | 'BLOCKED_LEGACY_QA_ACTIVE'

export interface BackfillRun {
  backfill_run_id: string
  requested_from: string
  requested_to: string
  status: BackfillStatus
  mode: BackfillMode
  created_by: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  current_business_date: string | null
  current_lecture_id: string | null
  current_lecture_label: string | null
  total_days: number
  completed_days: number
  progress_percent: number
  is_finished: boolean
  discovered_count: number
  matched_count: number
  processed_count: number
  already_complete_count: number
  waiting_count: number
  review_required_count: number
  failed_count: number
  suppressed_count: number
  cancel_requested: boolean
  error_summary: string | null
  runner_version: string | null
  heartbeat_at: string | null
  updated_at: string
}

export interface BackfillDay {
  business_date: string
  status: 'PENDING' | 'COMPLETED' | 'FAILED' | 'SKIPPED_CANCELLED' | 'DEFERRED_CYCLE_BUSY'
  pipeline_run_id: string | null
  calendar_events_considered: number
  matched_lectures: number
  newly_discovered: number
  already_complete: number
  processed: number
  waiting: number
  review_required: number
  failed: number
  suppressed: number
  graph_calls: number
  provider_calls: number
  error_code: string | null
  error_message: string | null
  duration_ms: number | null
  /** Set when the day's Recording Links evaluation failed; the day itself did not. */
  recording_error_code?: string | null
}

export interface BackfillListResponse {
  backfill_runs: BackfillRun[]
  active: BackfillRun | null
}

export interface BackfillDetailResponse {
  backfill_run: BackfillRun
  days: BackfillDay[]
  recording_links?: RecordingLinksSummary
}

/* ---------------------------------------------------------------------------
 * Recording Links.
 *
 * Every count is made by `app.recordings.coverage.summarize` over rows the
 * backfill runner stored. `outcome` is that module's one-word partition;
 * `recording_status` is the stage's own exact status, shown beside it.
 * ------------------------------------------------------------------------- */

export type RecordingOutcome =
  | 'ALREADY_LINKED' | 'WRITTEN' | 'EXACT_MATCH' | 'AMBIGUOUS'
  | 'BLOCKED_BY_EARLIER_STAGE' | 'REVIEW_REQUIRED' | 'NOT_APPLICABLE' | 'NOT_FOUND'
  | 'GRAPH_LOOKUP_FAILED' | 'DISCOVERY_FAILED' | 'NO_CODED_LECTURE'
  | 'NO_LEGACY_TARGET' | 'WAITING' | 'NOT_EVALUATED' | 'OTHER'

export interface RecordingCoverage {
  total: number
  not_applicable: number
  eligible: number
  already_linked: number
  missing_before: number
  exact_matched: number
  would_write: number
  written: number
  perfect_rows_updated: number
  perfect_rows_would_update: number
  ambiguous: number
  blocked_by_earlier_stage: number
  review_required: number
  not_found: number
  graph_lookup_failed: number
  discovery_failed: number
  no_coded_lecture: number
  no_legacy_target: number
  waiting: number
  not_evaluated: number
  other: number
  still_missing_after: number
  projected_missing_after_write: number
  coverage_percent_before: number | null
  coverage_percent_after: number | null
  projected_coverage_percent: number | null
  by_outcome: Partial<Record<RecordingOutcome, number>>
  reconciles: boolean
  recording_link_modes: string[]
}

export interface RecordingLinksSummary {
  coverage: RecordingCoverage
  evaluation: 'LIVE_PREVIEW' | 'EXECUTE'
  days_evaluated: number
  days_with_recording_errors: string[]
  recording_error_codes: string[]
  database_writes: number | null
  sharing_links_created: number | null
  provider_calls: number
}

export interface RecordingLinkItem {
  business_date: string
  item_key: string
  lecture_id: string | null
  subject: string | null
  population: 'CODED_LECTURE' | 'LEGACY_ROW_ONLY'
  recording_link_mode: string
  evaluation: 'LIVE_PREVIEW' | 'EXECUTE'
  outcome: RecordingOutcome
  recording_stage_state: StageState | null
  recording_status: string | null
  reason: string | null
  earlier_stage: string | null
  earlier_action: string | null
  would_write: boolean
  written: boolean
  perfect_row_updated: boolean
  perfect_row_would_update: boolean
  /** A folder NAME ("channel_recordings", "onedrive_recordings"), never a location. */
  source: string | null
  timestamp_difference_seconds: number | null
  candidate_file_count: number | null
  exact_candidate_count: number | null
  graph_lookup_status: string | null
  graph_http_status: number | null
  verification: string | null
  legacy_cancelled: boolean
  attempt_count: number | null
  next_attempt_after: string | null
  last_attempted_at: string | null
}

export interface BackfillRecordingLinksResponse {
  backfill_run_id: string
  mode: BackfillMode
  recording_links: RecordingLinksSummary
  items: RecordingLinkItem[]
}

/** The lecture detail's Recording Link block. No URL: availability is a yes/no. */
export interface RecordingLinkDetail {
  recording_link_mode: string
  state: StageState | null
  action: string | null
  reason: string | null
  owner: string | null
  recording_available: boolean
  last_status: string | null
  last_reason: string | null
  last_checked_at: string | null
  first_checked_at: string | null
  attempt_count: number | null
  next_attempt_after: string | null
  written_by_platform: boolean
  written_at: string | null
  source: string | null
  timestamp_difference_seconds: number | null
  candidate_file_count: number | null
  exact_candidate_count: number | null
  refused_to_guess: boolean
}

export interface BackfillCreatedResponse {
  backfill_run: BackfillRun
  detail: string
}
