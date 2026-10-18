/**
 * The Social Media API, as the backend sends it. Every opportunity is an AI
 * suggestion read from qa_social_media_opportunities; none is approved.
 */

export type SocialFormat = 'reel' | 'carousel' | 'quote_post' | 'educational_post'

export type FormatCounts = Record<SocialFormat, number> & { other: number }

export interface SocialLecture {
  session_id: string
  session_key: string
  subject: string | null
  trainer: string | null
  date: string | null
  recording_available: boolean
  recording_url: string | null
  opportunity_count: number
  format_counts: FormatCounts
  last_analyzed_at: string | null
}

export interface PaginatedSocialLectures {
  count: number
  next: string | null
  previous: string | null
  page: number
  page_size: number
  total_pages: number
  results: SocialLecture[]
}

export interface SocialSummary {
  lectures: number
  opportunity_count: number
  format_counts: FormatCounts
}

/** Cue numbers as saved; a time only where the row stores one for that cue. */
export interface SocialReelSegment {
  start_cue: number | null
  end_cue: number | null
  start: string | null
  end: string | null
}

/**
 * A reel's clip, read from PostgreSQL. `requested` means the webhook accepted
 * the request; only `ready` has a saved SharePoint link.
 */
export interface SocialClip {
  state: 'available' | 'requested' | 'ready' | 'failed' | 'stale' | 'unavailable'
  can_request: boolean
  reason: string | null
  clip_url: string | null
  clip_filename: string | null
  error: string | null
  requested_at: string | null
  updated_at: string | null
}

/**
 * The produced Positive Moments / OFSTED clip of the same moment (matched by
 * cue key AND exact times). It includes context before and after; it is not
 * an exact-length reel.
 */
export interface EvidenceClip {
  url: string
  filename: string | null
  duration_seconds: number
  context_before_seconds: number | null
  context_after_seconds: number | null
  evidence_type: string | null
}

export interface SocialOpportunity {
  opportunity_id: number
  opportunity_key: string
  start_cue: number
  end_cue: number
  start_time: string | null
  end_time: string | null
  duration_seconds: number | null
  speaker: string | null
  quote: string | null
  content_angle: string | null
  why_selected: string | null
  primary_format: string | null
  formats: string[]
  verification_status: string
  verification_reason: string | null
  discovery_source: string
  evidence_type: string | null
  analyzed_at: string
  updated_at: string
  /** The whole saved payload, unchanged. */
  opportunity_data: Record<string, unknown>
  reel_segments: SocialReelSegment[]
  reel_edit_mode: string | null
  reel_estimated_duration_seconds: number | null
  carousel_slide_outline: string[]
  also_in_evidence: boolean | null
  /** The stored recording opened at this start time, or null. Never a trimmed clip. */
  watch_url: string | null
  /** Null for opportunities that are not verified reels. */
  clip: SocialClip | null
  /** The existing evidence clip of the same moment, if one was produced. */
  evidence_clip: EvidenceClip | null
}

export interface SocialLectureDetail extends SocialLecture {
  meeting_id: string | null
  recording_filename: string | null
  recording_link_status: string | null
  opportunities: SocialOpportunity[]
}
