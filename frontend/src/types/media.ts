/**
 * Positive Moments Media API shapes. Every field arrives from the platform
 * already decided; nothing here is derived in the browser.
 *
 * No type carries a URL except the one-shot `{ url }` returned by the two
 * "open" endpoints, which is fetched only when the operator clicks.
 */

export type RunMode = 'PREVIEW' | 'ANALYZE' | 'RENDER' | 'RETRY_FAILED'

export interface MediaCounter {
  filter: string
  label: string
  count: number
}

export interface MediaRun {
  run_id: string
  requested_from: string
  requested_to: string
  lecture_id: string | null
  mode: RunMode
  status: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED'
  total_lectures: number
  processed_lectures: number
  counts: Record<string, number>
  created_by: string | null
  error_summary: string | null
  started_at: string | null
  completed_at: string | null
  created_at: string
}

export interface MediaLectureRow {
  lecture_id: string
  session_date: string
  subject: string | null
  trainer: string | null
  scheduled_start: string | null
  transcript_state: string | null
  recording_state: string | null
  recording_status: string | null
  recording_reason: string | null
  recording_rule: string | null
  recording_file_name: string | null
  recording_duration_seconds: number | null
  analysis_state: string | null
  moment_count: number | null
  media_state: string | null
  completed_clips: number
  review_items: number
  planned_clip_count: number | null
  estimated_credits: number | null
  needs_review: boolean | null
  refreshed_at: string | null
  filters: string[]
  job_counts: Record<string, number>
}

export interface CostPreview {
  is_estimate: true
  safe_clips_planned: number
  planned_output_minutes: number
  estimated_credits: number
  formula: string
  credits_per_output_minute: number
  render_policy: { max_width: number; max_height: number; frame_rate: number; output_format: string }
}

export interface MediaReadiness {
  render_provider: string
  render_provider_configured: boolean
  sharepoint_destination_configured: boolean
  webhook_configured: boolean
  analysis_model_configured: boolean
  problems: string[]
}

export interface MediaDashboard {
  date_from: string
  date_to: string
  analysis_policy_version: string
  counters: MediaCounter[]
  cost_preview: CostPreview
  active_runs: MediaRun[]
  recent_runs: MediaRun[]
  lectures: MediaLectureRow[]
  readiness: MediaReadiness
}

export interface DialogueLine {
  cue_index: number
  start_ms: number
  end_ms: number
  speaker: string | null
  role: 'trainer' | 'learner'
  positive: boolean
  text: string
}

export interface MomentJob {
  job_id: string
  status: string
  provider: string
  source_file_name: string | null
  source_duration_seconds: number | null
  evidence_start_ms: number
  evidence_end_ms: number
  media_offset_seconds: number | null
  alignment_method: string | null
  alignment_confidence: number | null
  alignment_status: string
  requested_media_start_seconds: number | null
  requested_media_end_seconds: number | null
  actual_media_start_seconds: number | null
  actual_media_end_seconds: number | null
  padding_before_requested_seconds: number | null
  padding_after_requested_seconds: number | null
  padding_before_applied_seconds: number | null
  padding_after_applied_seconds: number | null
  estimated_credits: number | null
  attempt_count: number
  max_attempts: number
  next_retry_at: string | null
  output_duration_seconds: number | null
  error_stage: string | null
  error_code: string | null
  error_message: string | null
  updated_at: string
}

export interface MomentAsset {
  asset_id: string
  output_filename: string
  duration_seconds: number | null
  size_bytes: number | null
  verification_status: string
  completed_at: string
}

export interface TimelineStep {
  stage: string
  label: string
  done: boolean
  at: string | null
  detail: string | null
  next_retry_at?: string | null
}

export interface PositiveMoment {
  moment_id: string
  moment_index: number
  category: string
  positive_speakers: string[]
  all_speakers: string[]
  conversation_type: string
  trainer_included: boolean
  positive_quote: string
  dialogue: DialogueLine[]
  start_cue: number
  end_cue: number
  evidence_start_ms: number
  evidence_end_ms: number
  reason: string | null
  source: 'coded_ai' | 'legacy_v5_import'
  selector_confidence: number | null
  verifier_verdict: string
  verifier_confidence: number | null
  job: MomentJob | null
  asset: MomentAsset | null
  timeline: TimelineStep[]
}

export interface LectureMediaDetail {
  lecture_id: string
  state: MediaLectureRow | null
  analysis: {
    analysis_id: string
    status: string
    source: string
    policy_version: string
    current_policy_version: string
    document_id: string
    transcript_fingerprint: string
    candidate_count: number
    structurally_valid_count: number
    accepted_count: number
    rejection_summary: Record<string, number>
    error_code: string | null
    analyzed_at: string | null
    is_current: boolean
    models: Record<string, { model: string | null; prompt_version: string | null }>
  } | null
  recording: {
    recording_state: string | null
    recording_status: string | null
    recording_reason: string | null
    recording_rule: string | null
    recording_file_name: string | null
    recording_duration_seconds: number | null
    recording_checked_at: string | null
  } | null
  moments: PositiveMoment[]
  totals: { moments: number; completed_clips: number; ready_to_render: number }
}
