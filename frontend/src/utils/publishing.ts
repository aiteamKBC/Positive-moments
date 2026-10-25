/**
 * Lecture Publishing Monitor vocabulary: one backend token -> one phrase and one badge tone.
 *
 * Presentation only (see utils/labels.ts). Every state arrives already decided by
 * app/publisher/monitor.py; nothing here reads two fields to reach a conclusion. Captions are
 * optional for playback, so no caption or transcript token is ever given the error tone.
 */
import { humanise, type Tone } from './labels'

type Badge = { label: string; tone: Tone }
const badge = (label: string, tone: Tone): Badge => ({ label, tone })

function lookup(table: Record<string, Badge>, value?: string | null): Badge {
  if (!value) return badge('—', 'quiet')
  return table[value] ?? badge(humanise(value), 'neutral')
}

export const MONITOR_STATE: Record<string, Badge> = {
  PLAYABLE: badge('Playable', 'ok'),
  PLAYABLE_NO_CAPTIONS: badge('Playable · no captions', 'ok'),
  RELEASED_PLAYBACK_OFF: badge('Released · playback off', 'info'),
  READY_TO_RELEASE: badge('Ready · releasing', 'info'),
  PROCESSING: badge('Processing', 'info'),
  RETRY_SCHEDULED: badge('Retry scheduled', 'waiting'),
  WAITING_FOR_RECORDING: badge('Waiting for recording', 'waiting'),
  SCHEDULED: badge('Not finished yet', 'quiet'),
  NOT_TRACKED: badge('Not picked up yet', 'quiet'),
  BLOCKED_IDENTITY: badge('Blocked · identity', 'review'),
  VIDEO_FAILED: badge('Video failed', 'error'),
  REVIEW_REQUIRED: badge('Review required', 'review'),
  RELEASED_NOT_PLAYABLE: badge('Released · not playable', 'error'),
  WITHDRAWN: badge('Withdrawn', 'neutral'),
  NOT_APPLICABLE: badge('Not applicable', 'quiet'),
}

export const LMS_MATCH: Record<string, Badge> = {
  VERIFIED: badge('Matched', 'ok'),
  PENDING: badge('Matching', 'waiting'),
  AMBIGUOUS: badge('Ambiguous', 'review'),
  RESCHEDULED_CANDIDATE: badge('Rescheduled?', 'review'),
  NO_LMS_SESSION: badge('No LMS session', 'review'),
  LMS_RECORD_INACTIVE: badge('LMS inactive', 'review'),
  CONFLICT: badge('Conflict', 'review'),
  STALE: badge('Stale', 'review'),
  NO_MEETING_KEY: badge('No meeting key', 'review'),
  TEST_EXCLUDED: badge('Test', 'quiet'),
  OUT_OF_LMS_SCOPE: badge('Out of scope', 'quiet'),
  LI_DUPLICATE: badge('Duplicate', 'quiet'),
}

export const RECORDING: Record<string, Badge> = {
  LOCATED: badge('Located', 'ok'),
  WAITING: badge('Waiting', 'waiting'),
  RETRY_SCHEDULED: badge('Retrying', 'waiting'),
  PENDING: badge('Not checked', 'quiet'),
  REVIEW_REQUIRED: badge('Review', 'review'),
  PERMANENT_FAILURE: badge('Not found', 'error'),
  NOT_APPLICABLE: badge('Not applicable', 'quiet'),
}

export const BUNNY_IMPORT: Record<string, Badge> = {
  UPLOADED: badge('Uploaded', 'ok'),
  UPLOADING: badge('Uploading', 'info'),
  NOT_IMPORTED: badge('Not imported', 'quiet'),
  FAILED: badge('Failed', 'error'),
  REVIEW_REQUIRED: badge('Review', 'review'),
}

export const VIDEO: Record<string, Badge> = {
  VERIFIED: badge('Verified', 'ok'),
  IN_PROGRESS: badge('Encoding', 'info'),
  NOT_STARTED: badge('Not started', 'quiet'),
  FAILED: badge('Failed', 'error'),
  REVIEW_REQUIRED: badge('Review', 'review'),
}

export const TRANSCRIPT: Record<string, Badge> = {
  AVAILABLE: badge('Available', 'ok'),
  WAITING: badge('Waiting', 'waiting'),
  MISSING: badge('Missing', 'neutral'),
  NOT_STARTED: badge('Not started', 'quiet'),
}

export const CAPTIONS: Record<string, Badge> = {
  VERIFIED: badge('Verified', 'ok'),
  PARTIAL: badge('Partial', 'info'),
  PENDING: badge('Pending', 'waiting'),
  UNVERIFIED: badge('Unverified', 'neutral'),
  NOT_AVAILABLE: badge('Not available', 'quiet'),
}

export const RELEASE: Record<string, Badge> = {
  RELEASED: badge('Released', 'ok'),
  NOT_RELEASED: badge('Not released', 'quiet'),
  WITHDRAWN: badge('Withdrawn', 'neutral'),
}

export const HEALTH: Record<string, Badge> = {
  HEALTHY: badge('Healthy', 'ok'),
  DEGRADED: badge('Degraded', 'review'),
  STALE: badge('Not running', 'error'),
  STOPPED: badge('Stopped', 'error'),
  UNKNOWN: badge('Unknown', 'neutral'),
  OK: badge('OK', 'ok'),
  BUSY: badge('Busy', 'info'),
}

export const AUTO_RELEASE: Record<string, Badge> = {
  RUNNING_RECENTLY: badge('Running', 'ok'),
  NOT_RUNNING_RECENTLY: badge('Not running recently', 'review'),
  NEVER_RAN: badge('Never ran', 'neutral'),
}

export const label = {
  state: (v?: string | null) => lookup(MONITOR_STATE, v),
  lms: (v?: string | null) => lookup(LMS_MATCH, v),
  recording: (v?: string | null) => lookup(RECORDING, v),
  bunny: (v?: string | null) => lookup(BUNNY_IMPORT, v),
  video: (v?: string | null) => lookup(VIDEO, v),
  transcript: (v?: string | null) => lookup(TRANSCRIPT, v),
  captions: (v?: string | null) => lookup(CAPTIONS, v),
  release: (v?: string | null) => lookup(RELEASE, v),
  health: (v?: string | null) => lookup(HEALTH, v),
  autoRelease: (v?: string | null) => lookup(AUTO_RELEASE, v),
}

/** "45 s", "4 min", "2 h 5 min" - a heartbeat age, never a raw number of seconds. */
export function age(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} s`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.round((seconds % 3600) / 60)
  return minutes ? `${hours} h ${minutes} min` : `${hours} h`
}
