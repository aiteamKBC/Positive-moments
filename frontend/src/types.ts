export interface Summary {
  processed_lectures: number
  lectures_with_positive_clips: number
  lectures_without_positive_clips: number
  total_positive_clips: number
  recordings_available: number
  recordings_missing: number
  /** Clip PRODUCTION, which is not the same question as clip detection. */
  lectures_with_ready_clips: number
  ready_clips: number
}

export interface MomentCategory {
  category: string
  count: number
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
  top_categories: MomentCategory[]
  moment_count: number
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

  // --- evidence, as the API read it from the stored clip ----------------
  /** The type stored as `evidence_type`, if any. */
  evidence_type: EvidenceType | null
  /** The verified final type, falling back to `evidence_type`, then a legacy reason prefix. */
  final_evidence_type: EvidenceType | null
  evidence_type_source: 'verified' | 'evidence_type' | 'reason_prefix' | null
  selector_evidence_type: EvidenceType | null
  evidence_type_reclassified: boolean | null
  selector_reason: string | null
  semantic_verdict: string | null
  semantic_status: SemanticStatus
  semantic_confidence: number | null
  semantic_reason: string | null
  semantic_advisory_pass: boolean | null
  feedback_scope: string | null
  conversation_type: string | null
  trainer_included: boolean | null

  // --- social media: a proposal, never a publishing approval -------------
  social_media_primary_format: string | null
  social_media_formats: string[] | null
  /** The recognised formats (quote_post, reel, carousel) this clip supports. */
  social_formats: SocialFormat[]
  is_social_candidate: boolean
  social_media_ready: boolean | null
  social_media_rationale: string | null
  social_verification_status: string | null
  social_verifier_confidence: number | null
  social_verifier_reason: string | null
  reel_edit_mode: string | null
  reel_segments: ReelSegment[] | null
  reel_estimated_duration_seconds: number | null
  carousel_slide_outline: string[] | null

  /** null when the clip has no cues and so cannot be reviewed. */
  review: ClipReview | null
}

export type EvidenceType = 'POSITIVE_MOMENT' | 'OFSTED_CASE_STUDY' | 'REJECT'
export type SemanticStatus = 'accepted' | 'rejected' | 'not_verified' | 'unrecognised'
export type SocialFormat = 'quote_post' | 'reel' | 'carousel'
export type ReviewStatus = 'pending' | 'approved' | 'rejected'

export interface ReelSegment {
  start_cue: number | null
  end_cue: number | null
  /** From the clip's stored dialogue; null when no line carries the cue. */
  start: string | null
  end: string | null
}

export interface ClipReview {
  status: ReviewStatus
  reviewer: string | null
  reviewed_at: string | null
  note: string
}

export interface EvidenceSummary {
  total: number
  ai_accepted: number
  ai_rejected: number
  not_verified: number
  positive_moments: number
  ofsted_case_studies: number
  rejected_as_evidence: number
  unclassified: number
  social_candidates: number
  reel: number
  carousel: number
  quote_post: number
}

export interface LectureDetail extends Lecture {
  meeting_id: string | null
  clips_analyzed_at: string | null
  clips_analysis_completeness: string | null
  clips_error: string | null
  recording_filename: string | null
  recording_link_updated_at: string | null
  clips: PositiveClip[]
  evidence_summary: EvidenceSummary
  review_summary: Record<ReviewStatus, number>
  /** May the signed-in person approve or reject clips? */
  can_review: boolean
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
