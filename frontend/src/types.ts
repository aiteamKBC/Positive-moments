export interface Summary {
  processed_lectures: number
  lectures_with_positive_clips: number
  lectures_without_positive_clips: number
  total_positive_clips: number
  recordings_available: number
  recordings_missing: number
}

export interface Lecture {
  session_id: string
  session_key: string
  subject: string | null
  trainer: string | null
  date: string | null
  clips_status: string | null
  positive_clips_count: number | null
  has_positive_clips: boolean | null
  recording_available: boolean
  recording_url: string | null
  recording_link_status: string | null
  has_ready_clips: boolean
  ready_clips_count: number
}

export interface DialogueLine {
  speaker?: string
  text?: string
  quote?: string
  start?: string
}

export interface SemanticVerification {
  verdict?: string
  confidence?: number
  reason?: string
}

export interface ClipAsset {
  id: string
  clip_index: number
  filename: string | null
  url: string
  status: string
  source_start: string
  source_end: string
  duration_seconds: number
  created_at: string
  uploaded_at: string
}

export interface PositiveClip {
  start?: string
  end?: string
  positive_quote?: string
  quote?: string
  speaker?: string
  positive_speakers: unknown[]
  other_speakers: unknown[]
  category?: string
  feedback_target?: string
  reason?: string
  confidence?: number
  duration_seconds?: number
  dialogue: Array<DialogueLine | string>
  semantic_verification: SemanticVerification
  original_index: number
  start_cue?: number
  end_cue?: number
  clip_asset: ClipAsset | null
}

export interface LectureDetail extends Lecture {
  meeting_id: string | null
  clips_analyzed_at: string | null
  clips_analysis_completeness: string | null
  clips_error: string | null
  recording_filename: string | null
  recording_link_updated_at: string | null
  clips: PositiveClip[]
}

export interface PaginatedLectures {
  count: number
  next: string | null
  previous: string | null
  page: number
  page_size: number
  total_pages: number
  results: Lecture[]
}
