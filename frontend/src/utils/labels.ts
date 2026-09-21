/**
 * The platform's vocabulary, rendered for people.
 *
 * WHAT THIS IS ALLOWED TO DO
 * --------------------------
 * Translate one backend token into one English phrase, and pick the badge
 * colour that phrase deserves. That is presentation.
 *
 * WHAT IT MUST NEVER DO
 * ---------------------
 * Decide anything. There is no rule here that reads several fields and
 * concludes a lecture is complete, eligible, attended or synced - every one of
 * those answers arrives from `app.orchestration` already made. A lookup table
 * that started inferring would be a second pipeline, written in TypeScript,
 * with no tests.
 *
 * Unknown tokens fall through to a readable version of the token itself rather
 * than to "Unknown", so a new backend state shows up as itself instead of
 * disappearing.
 */

export type Tone = 'ok' | 'waiting' | 'review' | 'error' | 'info' | 'brand' | 'neutral' | 'quiet'

export const TONE_CLASS: Record<Tone, string> = {
  ok: 'badge-ok',
  waiting: 'badge-waiting',
  review: 'badge-review',
  error: 'badge-error',
  info: 'badge-info',
  brand: 'badge-brand',
  neutral: 'badge-neutral',
  quiet: 'badge-quiet',
}

/** SCREAMING_SNAKE -> "Screaming snake". The fallback for anything unmapped. */
export function humanise(value?: string | null, fallback = '—') {
  if (!value) return fallback
  return value.replace(/_/g, ' ').toLowerCase().replace(/^./, (c) => c.toUpperCase())
}

// --- pipeline stages --------------------------------------------------------

export const STAGE_LABELS: Record<string, string> = {
  DISCOVERY: 'Calendar discovery',
  MEETING: 'Teams meeting match',
  TRANSCRIPT: 'Transcript retrieval',
  SELECTION: 'Transcript selection',
  CANONICAL_CUES: 'Canonical transcript',
  SPEAKERS: 'Speaker attribution',
  ATTENDANCE: 'Attendance',
  ENGAGEMENT: 'Engagement',
  QA_EVALUATION: 'Quality evaluation',
  QA_RENDER: 'Quality report',
  PERFECT_ELIGIBILITY: 'Perfect eligibility',
  LEGACY_QA_SYNC: 'QA sync to legacy',
  PERFECT_SYNC: 'Perfect sync to legacy',
  RECORDING_LINK: 'Recording link',
  EXCEL_SYNC: 'Excel sync',
}

/** One line saying what the stage is for, shown under its name. */
export const STAGE_NOTES: Record<string, string> = {
  DISCOVERY: 'The lecture was found on the teaching calendar.',
  MEETING: 'The calendar event was matched to a real Teams meeting.',
  TRANSCRIPT: 'Microsoft Graph returned the transcript content.',
  SELECTION: 'One transcript was chosen as the lecture of record.',
  CANONICAL_CUES: 'The selected transcript was parsed into timed cues.',
  SPEAKERS: 'Cues were attributed to named speakers.',
  ATTENDANCE: 'The attendance source was read and resolved.',
  ENGAGEMENT: 'Participation was measured from the canonical transcript.',
  QA_EVALUATION: 'The lecture was assessed against the QA checklist.',
  QA_RENDER: 'The assessment was rendered into the report format.',
  PERFECT_ELIGIBILITY: 'Perfect-lecture policy was applied to the result.',
  LEGACY_QA_SYNC: 'The report was written to the legacy QA record.',
  PERFECT_SYNC: 'The perfect outcome was written to the legacy record.',
  RECORDING_LINK: 'A durable recording link was stored. Owned by the legacy workflows.',
  EXCEL_SYNC: 'The outcome was stamped into the reporting workbook. Owned by the legacy workflows.',
}

export function stageLabel(name?: string | null) {
  return (name && STAGE_LABELS[name]) || humanise(name)
}

// --- stage states -----------------------------------------------------------

/**
 * WAITING is amber, never red. A lecture waiting on an external source is not
 * broken - its next move belongs to somebody else - and colouring it like a
 * failure sends an operator chasing a problem that does not exist.
 *
 * NOT_APPLICABLE is the quietest tone on the page: it is settled, so it should
 * recede rather than compete with the stages that still need something.
 */
export const STAGE_STATE: Record<string, { label: string; short: string; tone: Tone }> = {
  COMPLETE: { label: 'Complete', short: 'Done', tone: 'ok' },
  MISSING: { label: 'Not started', short: 'To do', tone: 'neutral' },
  STALE: { label: 'Needs refresh', short: 'Stale', tone: 'info' },
  FAILED: { label: 'Failed', short: 'Failed', tone: 'error' },
  REVIEW_REQUIRED: { label: 'Review required', short: 'Review', tone: 'review' },
  WAITING: { label: 'Waiting', short: 'Waiting', tone: 'waiting' },
  NOT_APPLICABLE: { label: 'Not applicable', short: 'n/a', tone: 'quiet' },
}

export function stageState(state?: string | null) {
  return STAGE_STATE[state ?? ''] ?? { label: humanise(state), short: humanise(state), tone: 'neutral' as Tone }
}

// --- day buckets ------------------------------------------------------------

export const BUCKET: Record<string, { label: string; tone: Tone }> = {
  complete: { label: 'Complete', tone: 'ok' },
  waiting: { label: 'Waiting', tone: 'waiting' },
  review: { label: 'Needs review', tone: 'review' },
  failed: { label: 'Failed', tone: 'error' },
  in_progress: { label: 'In progress', tone: 'info' },
  suppressed_duplicate: { label: 'Duplicate calendar event suppressed', tone: 'quiet' },
}

export function bucketLabel(bucket?: string | null) {
  return BUCKET[bucket ?? '']?.label ?? humanise(bucket)
}

export function bucketTone(bucket?: string | null): Tone {
  return BUCKET[bucket ?? '']?.tone ?? 'neutral'
}

// --- next actions -----------------------------------------------------------

export const ACTION_LABELS: Record<string, string> = {
  RUN_DISCOVERY: 'Discover the lecture',
  RESOLVE_MEETING: 'Match the Teams meeting',
  ACQUIRE_TRANSCRIPT: 'Fetch the transcript',
  SELECT_TRANSCRIPT: 'Select the transcript',
  BUILD_CANONICAL_CUES: 'Build the canonical transcript',
  RESOLVE_SPEAKERS: 'Attribute speakers',
  RESOLVE_ATTENDANCE: 'Resolve attendance',
  CALCULATE_ENGAGEMENT: 'Measure engagement',
  RUN_QA: 'Run the quality evaluation',
  REFRESH_DETERMINISTIC_QA: 'Refresh the deterministic checks',
  REVALIDATE_EVIDENCE: 'Revalidate the evidence',
  RENDER_QA: 'Render the quality report',
  SUPPRESS_DUPLICATE_EVENT: 'Suppress the duplicate calendar event',
  SYNC_LEGACY_QA: 'Sync QA to the legacy record',
  WAIT_FOR_ATTENDANCE_SOURCE: 'Waiting for the attendance source',
  RECOVER_ATTENDANCE: 'Recover attendance',
  EVALUATE_PERFECT: 'Apply the Perfect policy',
  SYNC_PERFECT: 'Sync Perfect to the legacy record',
  WAIT_FOR_RECORDING: 'Waiting for the recording link',
  WAIT_FOR_EXCEL_SYNC: 'Waiting for the Excel sync',
  NOTHING_TO_DO: 'Nothing to do',
  MANUAL_REVIEW_REQUIRED: 'Needs a human decision',
}

export function actionLabel(action?: string | null) {
  return (action && ACTION_LABELS[action]) || humanise(action)
}

/** Actions that describe somebody else's turn, so they read calmly. */
export function actionTone(action?: string | null): Tone {
  if (!action) return 'neutral'
  if (action === 'NOTHING_TO_DO') return 'quiet'
  if (action === 'MANUAL_REVIEW_REQUIRED') return 'review'
  if (action.startsWith('WAIT_FOR')) return 'waiting'
  return 'brand'
}

// --- attendance -------------------------------------------------------------

export const ATTENDANCE_COVERAGE: Record<string, string> = {
  SOURCE_AVAILABLE_WITH_MEMBERS: 'Attendance source available',
  SOURCE_MISSING: 'No attendance record has arrived',
  SOURCE_PARTIAL_OR_INVALID: 'Attendance record is partial or invalid',
}

// --- Perfect ----------------------------------------------------------------

export const PERFECT_REASONS: Record<string, { label: string; tone: Tone }> = {
  ELIGIBLE: { label: 'Eligible', tone: 'ok' },
  PENDING_ATTENDANCE_DATA: { label: 'Pending attendance', tone: 'waiting' },
  NOT_ELIGIBLE_CANCELLED: { label: 'Not eligible — cancelled', tone: 'quiet' },
  NOT_ELIGIBLE_CHECKLIST_ROW_COUNT: { label: 'Not eligible — checklist incomplete', tone: 'neutral' },
  NOT_ELIGIBLE_DUPLICATE_ORDER: { label: 'Not eligible — duplicate checklist order', tone: 'neutral' },
  NOT_ELIGIBLE_STATUS_NOT_ALL_MET: { label: 'Not eligible — not every item met', tone: 'neutral' },
  NOT_ELIGIBLE_MET_COUNT: { label: 'Not eligible — met count', tone: 'neutral' },
  NOT_ELIGIBLE_PARTIAL_COUNT: { label: 'Not eligible — partial items', tone: 'neutral' },
  NOT_ELIGIBLE_NOT_MET_COUNT: { label: 'Not eligible — unmet items', tone: 'neutral' },
  NOT_ELIGIBLE_MISSING_IDENTIFIERS: { label: 'Not eligible — missing identifiers', tone: 'neutral' },
}

export function perfectReason(reason?: string | null) {
  // No reason at all means the policy has not been applied yet, which is not
  // the same as "not eligible" and must not be rendered as an em dash.
  if (!reason) return { label: 'Not yet decided', tone: 'quiet' as Tone }
  return PERFECT_REASONS[reason] ?? { label: humanise(reason), tone: 'neutral' as Tone }
}

// --- pipeline runs ----------------------------------------------------------

export const RUN_TYPES: Record<string, string> = {
  MANUAL: 'Manual pipeline run',
  SCHEDULED: 'Scheduled pipeline run',
}

export const RUN_STATUS: Record<string, { label: string; tone: Tone }> = {
  RUNNING: { label: 'Running', tone: 'info' },
  COMPLETED: { label: 'Completed', tone: 'ok' },
  COMPLETED_WITH_FAILURES: { label: 'Completed with failures', tone: 'review' },
  FAILED: { label: 'Failed', tone: 'error' },
  BLOCKED_LEGACY_QA_ACTIVE: { label: 'Blocked — legacy QA active', tone: 'waiting' },
  BLOCKED_WRITER_INTEGRITY: { label: 'Blocked — writer integrity', tone: 'error' },
  ABORTED: { label: 'Aborted', tone: 'neutral' },
}

export function runTypeLabel(value?: string | null) {
  return (value && RUN_TYPES[value]) || `${humanise(value)} pipeline run`
}

export function runStatus(value?: string | null) {
  return RUN_STATUS[value ?? ''] ?? { label: humanise(value), tone: 'neutral' as Tone }
}

// --- retry / guarded actions ------------------------------------------------

export const RETRY_REASONS: Record<string, string> = {
  NOTHING_TO_RETRY: 'Everything the platform owns is already done.',
  WAITING_ON_EXTERNAL_SOURCE: 'The next step belongs to an external source, not to a retry.',
  MANUAL_REVIEW_REQUIRED: 'A human decision is needed before this can resume.',
  SUPPRESSED_DUPLICATE: 'This is a suppressed duplicate calendar event.',
}

export function retryReason(value?: string | null) {
  return (value && RETRY_REASONS[value]) || humanise(value, '')
}

// --- recording --------------------------------------------------------------

export function recordingLabel(available: boolean) {
  return available ? 'Recording available' : 'Recording link pending'
}
